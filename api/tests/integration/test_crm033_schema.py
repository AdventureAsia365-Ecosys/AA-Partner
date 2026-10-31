"""crm-033 (AA-971 CRM-13b AA support L2 + break-glass L3) on PostgreSQL with the RDS role shape.

- ``crm.support_grant`` / ``crm.support_session``: base template (``test_crm002``), FORCE RLS across two agencies,
  crm_app SELECT / INSERT / UPDATE only (no DELETE), nothing for the other readers; the CHECKs (a grant of 1 to 7 days,
  a break-glass session of at most an hour with a listed reason, the ticket pattern) and one active row per agency.
- ``crm.guard_support_access`` (CRM43 / CRM44): only an owner grants or revokes, for themselves; a session is opened
  and ended by its own staff actor, a support session only inside the active grant (not past it, the same
  show_identity); an owner revokes a support session, never a break-glass one; any actor marks an expired row
  expired, not a live one; the owner notice is recorded once; no row is deleted by crm_app.
- ``crm_support_reader``: SELECT on the named columns of four non-F1 tables only (no trip title, no write anywhere,
  no F1 table), and through FORCE RLS one agency only.
- ``crm.erase_subject``: the crm-027 text plus the two tables; an agency erase deletes their rows.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import ISS, _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "033_crm_support_access.sql"
NEW_TABLES = ("support_grant", "support_session")
STAFF_ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTWORKF"
STAFF_SUB = "5b1c0d9e-0000-4000-8000-00000000beef"
OTHER_SUB = "5b1c0d9e-0000-4000-8000-00000000cafe"
READER_COLUMNS = {
    "tenant": {"tenant_id", "status", "display_name", "default_locale"},
    "trip_stage_history": {
        "history_id",
        "tenant_id",
        "trip_id",
        "from_stage",
        "to_stage",
        "reason_code",
        "override",
        "override_reason_code",
        "at",
        "created_by",
    },  # fmt: skip
}


async def _reapply(admin: asyncpg.Connection) -> None:
    """Runs crm-033 again (a no-op). Also restores its erase_subject after a test that re-applied an older file."""
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")


async def _in(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str, role: str | None = None) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        if role is not None:
            await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        return await conn.fetchval(sql, *args)


async def _refused(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, **kw: Any) -> str:
    try:
        await _in(pool, tenant, sql, *args, **kw)
    except asyncpg.PostgresError as exc:
        return exc.sqlstate if exc.sqlstate.startswith("CRM") else type(exc).__name__
    raise AssertionError(f"not refused: {sql}")


GRANT_SQL = (
    "INSERT INTO crm.support_grant (expires_at, granted_by_member_id, show_identity) "
    "VALUES (now() + make_interval(days => $1), $2, $3) RETURNING support_grant_id"
)
SESSION_SQL = (
    "INSERT INTO crm.support_session (kind, support_grant_id, staff_iss, staff_sub, ticket_ref, reason_code, "
    "show_identity, expires_at) VALUES ($1, $2, $3, $4, $5, $6, $7, now() + make_interval(mins => $8)) "
    "RETURNING support_session_id"
)


async def _grant(pool: asyncpg.Pool, ids: dict[str, Any], *, days: int = 1, show: bool = False) -> uuid.UUID:
    member = ids["member"]
    return await _in(pool, ids["tenant"], GRANT_SQL, days, member, show, actor=f"member:{member}", role="owner")


async def _session(
    pool: asyncpg.Pool,
    tenant: uuid.UUID,
    grant: uuid.UUID | None,
    *,
    kind: str = "support",
    sub: str = STAFF_SUB,
    minutes: int = 60,
    show: bool = False,
    reason: str | None = None,
    actor: str | None = None,
) -> uuid.UUID:
    return await _in(
        pool,
        tenant,
        SESSION_SQL,
        kind,
        grant,
        STAFF_ISS,
        sub,
        "SUP-1042",
        reason,
        show,
        minutes,
        actor=actor or f"{kind}:{sub}",
    )


async def _as_owner(admin: asyncpg.Connection, tenant: uuid.UUID, sql: str, *args: Any) -> Any:
    """crm_owner moves time (the guard does not bind it): a window that has passed."""
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        return await admin.fetchval(sql, *args)


async def test_crm033_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-033'") == 1
    await _reapply(admin)
    await _reapply(admin)
    erase = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    # The crm-027 text whole, plus the two tables, the sessions before the grants before the members.
    assert "'export_job', 'data_request'" in erase and erase.count("['calendar_feed', '']") == 1
    assert erase.index("['support_session', '']") < erase.index("['support_grant', '']") < erase.index("['member', '']")
    transitions = await admin.fetch(
        "SELECT table_name, from_status, to_status FROM crm.status_transition WHERE table_name LIKE 'support_%'"
    )
    assert {(r["table_name"], r["from_status"], r["to_status"]) for r in transitions} == {
        ("support_grant", "active", "revoked"),
        ("support_grant", "active", "expired"),
        ("support_session", "active", "ended"),
        ("support_session", "active", "revoked"),
        ("support_session", "active", "expired"),
    }


@pytest.mark.parametrize("table", NEW_TABLES)
async def test_privileges_rls_and_keys(admin: asyncpg.Connection, table: str) -> None:
    for privilege, want in (("SELECT", True), ("INSERT", True), ("UPDATE", True), ("DELETE", False),
                            ("TRUNCATE", False)):  # fmt: skip
        got = await admin.fetchval("SELECT has_table_privilege('crm_app', $1, $2)", f"crm.{table}", privilege)
        assert got is want, (table, privilege)
    for reader in READERS:
        readable = await admin.fetchval("SELECT has_any_column_privilege($1, $2, 'SELECT')", reader, f"crm.{table}")
        assert not readable, (reader, table)
    assert await admin.fetchval(
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = $1::regclass", f"crm.{table}"
    )


async def test_the_support_reader_reads_named_columns_of_four_tables_only(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(
        "SELECT c.relname AS name FROM pg_class c WHERE c.relnamespace = 'crm'::regnamespace "
        "AND c.relkind IN ('r', 'p', 'v', 'm') AND has_any_column_privilege('crm_support_reader', c.oid, 'SELECT')"
    )
    assert sorted(r["name"] for r in rows) == ["tenant", "tenant_settings", "trip", "trip_stage_history"]
    for table, want in READER_COLUMNS.items():
        cols = await admin.fetch(
            "SELECT a.attname AS name FROM pg_attribute a WHERE a.attrelid = $1::regclass AND a.attnum > 0 "
            "AND NOT a.attisdropped AND has_column_privilege('crm_support_reader', a.attrelid, a.attnum, 'SELECT')",
            f"crm.{table}",
        )
        assert {c["name"] for c in cols} == want, table
    for table, column in (("trip", "title"), ("trip", "mail_token"), ("trip", "import_ref_bidx"),
                          ("tenant_settings", "data_request_contact"), ("tenant_settings", "consent_text"),
                          ("tenant_settings", "refund_owner_threshold_base")):  # fmt: skip
        assert not await admin.fetchval(
            "SELECT has_column_privilege('crm_support_reader', $1, $2, 'SELECT')", f"crm.{table}", column
        ), (table, column)
    writes = await admin.fetchval(
        "SELECT count(*) FROM pg_class c, unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) p "
        "WHERE c.relnamespace = 'crm'::regnamespace AND c.relkind IN ('r', 'p', 'v') "
        "AND has_table_privilege('crm_support_reader', c.oid, p)"
    )
    assert writes == 0


async def test_the_support_reader_sees_one_agency_and_no_title(app_pool: asyncpg.Pool) -> None:
    a, b = await _agency(app_pool), await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, a["tenant"])
        await conn.execute("SET LOCAL ROLE crm_support_reader")
        trips = await conn.fetch("SELECT trip_id, stage FROM crm.trip")
        assert [r["trip_id"] for r in trips] == [a["trip"]]
        assert await conn.fetchval("SELECT count(*) FROM crm.trip WHERE trip_id = $1", b["trip"]) == 0
    refused = (
        "SELECT title FROM crm.trip",
        "SELECT count(*) FROM crm.traveler",
        "SELECT count(*) FROM crm.trip_traveler",
        "SELECT count(*) FROM crm.support_grant",
        "UPDATE crm.trip SET stage = stage",
        "INSERT INTO crm.audit_event (actor_kind) VALUES ('support')",
    )
    for sql in refused:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with app_pool.acquire() as conn, conn.transaction():
                await _ctx(conn, a["tenant"])
                await conn.execute("SET LOCAL ROLE crm_support_reader")
                await conn.execute(sql)


async def test_only_an_owner_grants_and_the_window_is_one_to_seven_days(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t, owner = ids["tenant"], ids["member"]
    agent = await _in(
        app_pool,
        t,
        "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, 'agent', 'active') "
        "RETURNING member_id",
        ISS,
        f"sub-{uuid.uuid4()}",
        actor="seed:test",
    )
    assert await _refused(app_pool, t, GRANT_SQL, 1, agent, False, actor=f"member:{agent}", role="agent") == "CRM43"
    # An owner grants only in their own name.
    assert await _refused(app_pool, t, GRANT_SQL, 1, agent, False, actor=f"member:{owner}", role="owner") == "CRM43"
    assert await _refused(app_pool, t, GRANT_SQL, 1, owner, False, actor="job:crm_lifecycle") == "CRM43"
    for days in (0, 8):
        refused = await _refused(app_pool, t, GRANT_SQL, days, owner, False, actor=f"member:{owner}", role="owner")
        assert refused == "CheckViolationError", days
    grant = await _grant(app_pool, ids, days=7, show=True)
    assert grant is not None
    # One active grant per agency.
    again = await _refused(app_pool, t, GRANT_SQL, 1, owner, False, actor=f"member:{owner}", role="owner")
    assert again == "UniqueViolationError"
    # The window and scope never change; an agent cannot revoke; an owner can, naming themselves.
    widen = "UPDATE crm.support_grant SET show_identity = false WHERE support_grant_id = $1"
    assert await _refused(app_pool, t, widen, grant, actor=f"member:{owner}", role="owner") == "CRM43"
    revoke = (
        "UPDATE crm.support_grant SET status = 'revoked', ended_at = now(), ended_by = $2 WHERE support_grant_id = $1"
    )
    by_agent = await _refused(app_pool, t, revoke, grant, f"member:{agent}", actor=f"member:{agent}", role="agent")
    assert by_agent == "CRM43"
    # ended_by must name the acting owner.
    assert await _refused(app_pool, t, revoke, grant, "member:x", actor=f"member:{owner}", role="owner") == "CRM43"
    await _in(app_pool, t, revoke, grant, f"member:{owner}", actor=f"member:{owner}", role="owner")
    status = await _in(app_pool, t, "SELECT status FROM crm.support_grant WHERE support_grant_id = $1", grant,
                       actor="job:test")  # fmt: skip
    assert status == "revoked"
    delete = "DELETE FROM crm.support_grant WHERE support_grant_id = $1"
    assert await _refused(app_pool, t, delete, grant, actor=f"member:{owner}", role="owner") == (
        "InsufficientPrivilegeError"
    )


async def test_a_live_grant_is_not_expired_but_a_passed_one_is(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    grant = await _grant(app_pool, ids)
    expire = (
        "UPDATE crm.support_grant SET status = 'expired', ended_at = now(), ended_by = 'job:test' "
        "WHERE support_grant_id = $1"
    )
    assert await _refused(app_pool, t, expire, grant, actor="job:test") == "CRM43"
    await _as_owner(
        admin,
        t,
        "UPDATE crm.support_grant SET starts_at = starts_at - interval '2 days', "
        "expires_at = expires_at - interval '2 days' WHERE support_grant_id = $1",
        grant,
    )
    await _in(app_pool, t, expire, grant, actor="job:test")


async def test_a_support_session_lives_inside_the_grant(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    grant = await _grant(app_pool, ids, show=True)
    # Another actor than the row's own staff member, and a session past the grant, are refused.
    assert await _refused(app_pool, t, SESSION_SQL, "support", grant, STAFF_ISS, STAFF_SUB, "SUP-1", None, True, 60,
                          actor=f"support:{OTHER_SUB}") == "CRM44"  # fmt: skip
    assert await _refused(app_pool, t, SESSION_SQL, "support", grant, STAFF_ISS, STAFF_SUB, "SUP-1", None, True,
                          3 * 24 * 60, actor=f"support:{STAFF_SUB}") == "CRM44"  # fmt: skip
    # show_identity follows the grant.
    assert await _refused(app_pool, t, SESSION_SQL, "support", grant, STAFF_ISS, STAFF_SUB, "SUP-1", None, False, 60,
                          actor=f"support:{STAFF_SUB}") == "CRM44"  # fmt: skip
    # A bad ticket is a CHECK.
    assert await _refused(app_pool, t, SESSION_SQL, "support", grant, STAFF_ISS, STAFF_SUB, "drop table", None, True,
                          60, actor=f"support:{STAFF_SUB}") == "CheckViolationError"  # fmt: skip
    session = await _session(app_pool, t, grant, show=True)
    # One staff member at a time.
    clash = await _refused(app_pool, t, SESSION_SQL, "support", grant, STAFF_ISS, OTHER_SUB, "SUP-2", None, True, 60,
                           actor=f"support:{OTHER_SUB}")  # fmt: skip
    assert clash == "UniqueViolationError"
    # The staff member's identity, ticket and window are fixed.
    stretch = "UPDATE crm.support_session SET expires_at = expires_at + interval '1 day' WHERE support_session_id = $1"
    assert await _refused(app_pool, t, stretch, session, actor=f"support:{STAFF_SUB}") == "CRM44"
    end = "UPDATE crm.support_session SET status = $2, ended_at = now(), ended_by = $3 WHERE support_session_id = $1"
    # Another staff member cannot end it; a live session is not expired; the owner revokes it.
    other = f"support:{OTHER_SUB}"
    assert await _refused(app_pool, t, end, session, "ended", other, actor=other) == "CRM44"
    assert await _refused(app_pool, t, end, session, "expired", "job:test", actor="job:test") == "CRM44"
    owner = f"member:{ids['member']}"
    await _in(app_pool, t, end, session, "revoked", owner, actor=owner, role="owner")
    # After the grant is revoked, a new session is refused.
    await _in(
        app_pool,
        t,
        "UPDATE crm.support_grant SET status = 'revoked', ended_at = now(), ended_by = $2 WHERE support_grant_id = $1",
        grant,
        owner,
        actor=owner,
        role="owner",
    )
    assert await _refused(app_pool, t, SESSION_SQL, "support", grant, STAFF_ISS, STAFF_SUB, "SUP-3", None, True, 60,
                          actor=f"support:{STAFF_SUB}") == "CRM44"  # fmt: skip


async def test_a_session_of_a_revoked_grant_may_be_marked_expired(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    owner = f"member:{ids['member']}"
    grant = await _grant(app_pool, ids)
    session = await _session(app_pool, t, grant)
    await _in(
        app_pool,
        t,
        "UPDATE crm.support_grant SET status = 'revoked', ended_at = now(), ended_by = $2 WHERE support_grant_id = $1",
        grant,
        owner,
        actor=owner,
        role="owner",
    )
    await _in(
        app_pool,
        t,
        "UPDATE crm.support_session SET status = 'expired', ended_at = now(), ended_by = $2 "
        "WHERE support_session_id = $1",
        session,
        f"support:{STAFF_SUB}",
        actor=f"support:{STAFF_SUB}",
    )


async def test_break_glass_needs_a_reason_and_at_most_an_hour(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    common = ("breakglass", None, STAFF_ISS, STAFF_SUB, "INC-7")
    bg = f"breakglass:{STAFF_SUB}"
    assert await _refused(app_pool, t, SESSION_SQL, *common, None, True, 30, actor=bg) == "CheckViolationError"
    assert await _refused(app_pool, t, SESSION_SQL, *common, "curiosity", True, 30, actor=bg) == "CheckViolationError"
    assert await _refused(app_pool, t, SESSION_SQL, *common, "service_outage", True, 61, actor=bg) == (
        "CheckViolationError"
    )
    assert await _refused(app_pool, t, SESSION_SQL, *common, "service_outage", False, 30, actor=bg) == (
        "CheckViolationError"
    )
    # A support actor cannot open a break-glass row.
    sup = f"support:{STAFF_SUB}"
    assert await _refused(app_pool, t, SESSION_SQL, *common, "service_outage", True, 30, actor=sup) == "CRM44"
    session = await _session(app_pool, t, None, kind="breakglass", minutes=60, show=True, reason="security_incident")
    # The owner cannot end a break-glass session; the notice is written once; its staff member ends it.
    owner = f"member:{ids['member']}"
    end = "UPDATE crm.support_session SET status = $2, ended_at = now(), ended_by = $3 WHERE support_session_id = $1"
    assert await _refused(app_pool, t, end, session, "revoked", owner, actor=owner, role="owner") == "CRM44"
    notice = "UPDATE crm.support_session SET owner_notified_at = now() WHERE support_session_id = $1"
    await _in(app_pool, t, notice, session, actor=bg)
    assert await _refused(app_pool, t, notice, session, actor=bg) == "CRM44"
    await _in(app_pool, t, end, session, "ended", bg, actor=bg)


async def test_rows_stay_in_their_agency(app_pool: asyncpg.Pool) -> None:
    a, b = await _agency(app_pool), await _agency(app_pool)
    grant = await _grant(app_pool, a)
    await _session(app_pool, a["tenant"], grant)
    assert await _count(app_pool, a["tenant"], "support_grant") == 1
    assert await _count(app_pool, b["tenant"], "support_grant") == 0
    assert await _count(app_pool, b["tenant"], "support_session") == 0
    # A session of agency B cannot name agency A's grant (same-agency key).
    refused = await _refused(app_pool, b["tenant"], SESSION_SQL, "support", grant, STAFF_ISS, STAFF_SUB, "SUP-9",
                             None, False, 60, actor=f"support:{STAFF_SUB}")  # fmt: skip
    assert refused in {"CRM44", "ForeignKeyViolationError"}


async def test_the_agency_erase_deletes_both_tables(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    await _reapply(admin)  # an earlier test file may have re-applied an older erase_subject
    ids = await _agency(app_pool)
    t = ids["tenant"]
    grant = await _grant(app_pool, ids)
    await _session(app_pool, t, grant)
    await _session(app_pool, t, None, kind="breakglass", minutes=15, show=True, reason="data_corruption")
    await _in(app_pool, t, "UPDATE crm.tenant SET status = 'offboarding'", actor="platform:test")
    done = await _erase(app_pool, t, "tenant", t)
    assert done["counts"]["support_session"] == 2 and done["counts"]["support_grant"] == 1
    assert await _count(app_pool, t, "support_grant") == 0 and await _count(app_pool, t, "member") == 0
