"""crm-027 (AA-961 CRM-30 duty of care + calendar) on PostgreSQL with the RDS role shape.

- ``crm.incident_flag`` / ``crm.calendar_feed``: base template, FORCE RLS (an isolation probe across two agencies),
  crm_app SELECT / INSERT / UPDATE only, nothing for the readers; the CHECKs and the status moves.
- ``crm.guard_incident_flag`` (CRM41): an agency member never raises or clears an AA incident; a raised flag keeps its
  departure, code and event.
- ``crm.guard_calendar_feed`` (CRM42) and ``calendar_feed_token_lookup``: a member creates only their own feed, only
  that member or an owner revokes it, the token / member / scope / expiry never change, the feed's own fetch touches
  only its counters; the lookup finds a live feed by its hash before any agency is known, never a revoked or expired
  one.
- ``crm.erase_subject`` (the crm-026 text + two agency-scope tables): an agency erase deletes both tables' rows.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import ISS, _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "027_crm_duty_of_care_calendar.sql"
NEW_TABLES = ("incident_flag", "calendar_feed")
JOB = "job:crm_booking_sync"


async def _reapply(admin: asyncpg.Connection) -> None:
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")


async def _in(
    pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = JOB, role: str | None = None
) -> Any:
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


async def _flag(pool: asyncpg.Pool, tenant: uuid.UUID, departure: str = "dep-2026-11-14-hg", **kw: Any) -> uuid.UUID:
    return await _in(
        pool,
        tenant,
        "INSERT INTO crm.incident_flag (departure_id, incident_code, raised_at, raised_event_id) "
        "VALUES ($1, $2, now(), $3) RETURNING incident_flag_id",
        departure,
        kw.get("code", "road"),
        kw.get("event", f"ev_{uuid.uuid4().hex[:12]}"),
        actor=kw.get("actor", JOB),
    )


async def _member(pool: asyncpg.Pool, tenant: uuid.UUID, role: str = "agent") -> uuid.UUID:
    return await _in(
        pool,
        tenant,
        "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, $3, 'active') "
        "RETURNING member_id",
        ISS,
        f"sub-{uuid.uuid4()}",
        role,
        actor="seed:test",
    )


async def _feed(pool: asyncpg.Pool, tenant: uuid.UUID, member: uuid.UUID, token: bytes | None = None) -> uuid.UUID:
    return await _in(
        pool,
        tenant,
        "INSERT INTO crm.calendar_feed (member_id, token_hash) VALUES ($1, $2) RETURNING calendar_feed_id",
        member,
        hashlib.sha256(token or os.urandom(32)).digest(),
        actor=f"member:{member}",
    )


async def test_crm027_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-027'") == 1
    await _reapply(admin)
    erase = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    # The crm-026 text whole (its keep list included), plus the two agency-scope tables, the feeds before the members.
    assert "'export_job', 'data_request'" in erase and "signature_event_kept" in erase
    assert erase.count("['incident_flag', '']") == 1 and erase.count("['calendar_feed', '']") == 1
    assert erase.index("['calendar_feed', '']") < erase.index("['member', '']")


@pytest.mark.parametrize("table", NEW_TABLES)
async def test_privileges_rls_and_keys(admin: asyncpg.Connection, table: str) -> None:
    for privilege, want in (("SELECT", True), ("INSERT", True), ("UPDATE", True), ("DELETE", False),
                            ("TRUNCATE", False)):  # fmt: skip
        got = await admin.fetchval("SELECT has_table_privilege('crm_app', $1, $2)", f"crm.{table}", privilege)
        assert got is want, (table, privilege)
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege)
    assert await admin.fetchval(
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = $1::regclass", f"crm.{table}"
    )
    refs = await admin.fetch(
        "SELECT confrelid::regclass::text AS ref FROM pg_constraint WHERE conrelid = $1::regclass AND contype = 'f'",
        f"crm.{table}",
    )
    # Neither names a trip or a traveller (no trip / traveller erase step needed).
    assert {r["ref"] for r in refs} <= {"crm.tenant", "crm.member"}


async def test_isolation_probe_across_agencies(app_pool: asyncpg.Pool) -> None:
    a = await _agency(app_pool)
    b = await _agency(app_pool)
    await _flag(app_pool, a["tenant"])
    await _feed(app_pool, a["tenant"], a["member"])
    for table in NEW_TABLES:
        assert await _count(app_pool, a["tenant"], table) == 1
        assert await _count(app_pool, b["tenant"], table) == 0
    # No tenant set = no row; a write naming another agency is refused by the policy.
    async with app_pool.acquire() as conn, conn.transaction():
        assert await conn.fetchval("SELECT count(*) FROM crm.incident_flag") == 0
        assert await conn.fetchval("SELECT count(*) FROM crm.calendar_feed") == 0
    assert (
        await _refused(
            app_pool,
            b["tenant"],
            "INSERT INTO crm.incident_flag (tenant_id, departure_id, incident_code, raised_at, raised_event_id) "
            "VALUES ($1, 'dep-x', 'road', now(), 'ev_x')",
            a["tenant"],
        )
        == "InsufficientPrivilegeError"
    )


async def test_incident_flag_checks_moves_and_source(app_pool: asyncpg.Pool) -> None:
    t = (await _agency(app_pool))["tenant"]
    member = f"member:{uuid.uuid4()}"
    # Card R: an agency member never raises or clears an AA incident.
    raise_sql = (
        "INSERT INTO crm.incident_flag (departure_id, incident_code, raised_at, raised_event_id) "
        "VALUES ('dep-1', 'road', now(), 'ev_m')"
    )
    assert await _refused(app_pool, t, raise_sql, actor=member) == "CRM41"
    flag = await _flag(app_pool, t, "dep-1", event="ev_1")
    clear = "UPDATE crm.incident_flag SET status = 'cleared', cleared_at = now() WHERE incident_flag_id = $1"
    assert await _refused(app_pool, t, clear, flag, actor=member) == "CRM41"
    # Codes are the contract's fixed list; the id has the line's shape; one open flag per departure; one per event.
    with pytest.raises(asyncpg.CheckViolationError):
        await _flag(app_pool, t, "dep-2", code="guest fell ill")
    with pytest.raises(asyncpg.CheckViolationError):
        await _flag(app_pool, t, "dep 2 with spaces")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _flag(app_pool, t, "dep-1")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _flag(app_pool, t, "dep-3", event="ev_1")
    # Cleared needs its time; a raised flag keeps its code; cleared never reopens (a new incident is a new row).
    no_time = "UPDATE crm.incident_flag SET status = 'cleared' WHERE incident_flag_id = $1"
    assert await _refused(app_pool, t, no_time, flag) == "CheckViolationError"
    recode = "UPDATE crm.incident_flag SET incident_code = 'weather' WHERE incident_flag_id = $1"
    assert await _refused(app_pool, t, recode, flag) == "CRM41"
    await _in(app_pool, t, clear, flag)
    reopen = "UPDATE crm.incident_flag SET status = 'open', cleared_at = NULL WHERE incident_flag_id = $1"
    assert await _refused(app_pool, t, reopen, flag) == "CheckViolationError"
    await _flag(app_pool, t, "dep-1", event="ev_2")  # the departure's next incident
    assert await _refused(app_pool, t, "DELETE FROM crm.incident_flag WHERE true") == "InsufficientPrivilegeError"


async def test_calendar_feed_guard(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t, owner = ids["tenant"], ids["member"]
    agent = await _member(app_pool, t)
    other = await _member(app_pool, t)
    insert = "INSERT INTO crm.calendar_feed (member_id, token_hash) VALUES ($1, $2)"
    # A member subscribes for themselves only; a job never creates one; the token is a 32-byte hash.
    assert await _refused(app_pool, t, insert, other, os.urandom(32), actor=f"member:{agent}") == "CRM42"
    assert await _refused(app_pool, t, insert, agent, os.urandom(32), actor=JOB) == "CRM42"
    assert await _refused(app_pool, t, insert, agent, os.urandom(16), actor=f"member:{agent}") == "CheckViolationError"
    feed = await _feed(app_pool, t, agent)
    # One active feed per member.
    with pytest.raises(asyncpg.UniqueViolationError):
        await _feed(app_pool, t, agent)
    # Fixed: token, scope, expiry.
    for sql in (
        "UPDATE crm.calendar_feed SET scope = 'agency' WHERE calendar_feed_id = $1",
        "UPDATE crm.calendar_feed SET token_hash = sha256('x') WHERE calendar_feed_id = $1",
        "UPDATE crm.calendar_feed SET expires_at = expires_at + interval '1 day' WHERE calendar_feed_id = $1",
    ):
        assert await _refused(app_pool, t, sql, feed, actor=f"member:{agent}") == "CRM42", sql
    revoke = (
        "UPDATE crm.calendar_feed SET status = 'revoked', revoked_at = now(), revoked_by = $2 "
        "WHERE calendar_feed_id = $1"
    )
    # Another agent may not revoke it; the fetch may only count itself; a member may not touch the counters.
    by_other = await _refused(app_pool, t, revoke, feed, f"member:{other}", actor=f"member:{other}", role="agent")
    assert by_other == "CRM42"
    count = (
        "UPDATE crm.calendar_feed SET fetch_count = fetch_count + 1, last_fetched_at = now() "
        "WHERE calendar_feed_id = $1"
    )
    assert await _refused(app_pool, t, count, feed, actor=f"member:{agent}") == "CRM42"
    assert await _refused(app_pool, t, count, feed, actor=f"calendar_feed:{uuid.uuid4()}") == "CRM42"
    await _in(app_pool, t, count, feed, actor=f"calendar_feed:{feed}")
    assert await _refused(app_pool, t, revoke, feed, "x", actor=f"calendar_feed:{feed}") == "CRM42"
    # The owner may revoke an agent's feed; revoked is final; a new feed for the member is fine afterwards.
    await _in(app_pool, t, revoke, feed, f"member:{owner}", actor=f"member:{owner}", role="owner")
    back = (
        "UPDATE crm.calendar_feed SET status = 'active', revoked_at = NULL, revoked_by = NULL "
        "WHERE calendar_feed_id = $1"
    )
    assert await _refused(app_pool, t, back, feed, actor=f"member:{owner}", role="owner") == "CheckViolationError"
    await _feed(app_pool, t, agent)
    assert await _refused(app_pool, t, "DELETE FROM crm.calendar_feed WHERE true") == "InsufficientPrivilegeError"


async def _lookup(pool: asyncpg.Pool, token: bytes) -> asyncpg.Record | None:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.calendar_feed_hash', $1, true)", hashlib.sha256(token).hexdigest())
        return await conn.fetchrow("SELECT calendar_feed_id, tenant_id, member_id FROM crm.calendar_feed")


async def test_token_lookup_finds_live_feeds_only(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t, owner = ids["tenant"], ids["member"]
    token = os.urandom(32)
    feed = await _feed(app_pool, t, owner, token)
    row = await _lookup(app_pool, token)
    assert row is not None and row["calendar_feed_id"] == feed and row["tenant_id"] == t
    assert await _lookup(app_pool, os.urandom(32)) is None
    # Without the setting nothing is visible (no agency set either).
    async with app_pool.acquire() as conn, conn.transaction():
        assert await conn.fetchval("SELECT count(*) FROM crm.calendar_feed") == 0
    # SELECT only: the lookup never lets a write through.
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.calendar_feed_hash', $1, true)", hashlib.sha256(token).hexdigest())
        await conn.execute("SELECT set_config('app.actor', $1, true)", f"calendar_feed:{feed}")
        assert await conn.execute("UPDATE crm.calendar_feed SET fetch_count = 1") == "UPDATE 0"
    await _in(
        app_pool,
        t,
        "UPDATE crm.calendar_feed SET status = 'revoked', revoked_at = now(), revoked_by = 'member:x' "
        "WHERE calendar_feed_id = $1",
        feed,
        actor=f"member:{owner}",
        role="owner",
    )
    assert await _lookup(app_pool, token) is None


async def test_the_agency_erase_deletes_both_tables(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    await _flag(app_pool, t)
    await _feed(app_pool, t, ids["member"])
    await _in(app_pool, t, "UPDATE crm.tenant SET status = 'offboarding'", actor="platform:test")
    done = await _erase(app_pool, t, "tenant", t)
    assert done["counts"]["incident_flag"] == 1 and done["counts"]["calendar_feed"] == 1
    assert await _count(app_pool, t, "incident_flag") == 0 and await _count(app_pool, t, "calendar_feed") == 0
    assert await _count(app_pool, t, "member") == 0
