"""crm-013 (AA-978, CRM-P1) on PostgreSQL 15 with the RDS role shape: the CIS membership feed's storage.

- ``crm.tenant.lock_source``: a lock that names no source is AA's (``aa_ops``), leaving ``locked`` clears it, only
  the feed actor names a ``cis_*`` source and only a platform actor names any (CRM21).
- ``crm.membership_feed_state``: one platform row, crm_app reads and updates it, never inserts or deletes.
- ``crm.membership_inbox``: one row per CIS event (a replay is refused by the key), per agency under FORCE RLS,
  append-only for crm_app, no reader grant, a type outside the contract only on a rejected row.
- The agency erase deletes the agency's inbox rows (and only those), so offboarding never fails with CRM16.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "013_crm_membership_feed.sql"
FEED = "platform:feed-tenant-membership"


async def _status(pool: asyncpg.Pool, tenant: uuid.UUID, actor: str, sql: str, *args: object) -> asyncpg.Record:
    """One crm.tenant write as crm_app under ``actor``, then the status and source it left."""
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        await conn.execute(sql, tenant, *args)
        return await conn.fetchrow("SELECT status, lock_source FROM crm.tenant WHERE tenant_id = $1", tenant)


async def _inbox(
    pool: asyncpg.Pool, tenant: uuid.UUID, event_id: uuid.UUID, event_type: str | None, outcome: str, reason: str
) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, FEED)
        await conn.execute(
            "INSERT INTO crm.membership_inbox (event_id, event_type, outcome, reason_code) VALUES ($1, $2, $3, $4)",
            event_id,
            event_type,
            outcome,
            reason,
        )


async def test_crm013_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-013'") == 1
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM crm.membership_feed_state") == 1


async def test_a_lock_without_a_source_is_aa_ops_and_leaving_locked_clears_it(app_pool: asyncpg.Pool) -> None:
    """The L1 console's lock / unlock needs no code change (file header items 1a, 1b)."""
    ids = await _agency(app_pool)
    staff, sql = "platform:staff-sub-1", "UPDATE crm.tenant SET status = $2 WHERE tenant_id = $1"
    locked = await _status(app_pool, ids["tenant"], staff, sql, "locked")
    assert dict(locked) == {"status": "locked", "lock_source": "aa_ops"}
    active = await _status(app_pool, ids["tenant"], staff, sql, "active")
    assert dict(active) == {"status": "active", "lock_source": None}


async def test_the_feed_names_a_cis_source_and_an_unlock_clears_it(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    lock = "UPDATE crm.tenant SET status = 'locked', lock_source = $2 WHERE tenant_id = $1"
    assert dict(await _status(app_pool, t, FEED, lock, "cis_tenant")) == {
        "status": "locked",
        "lock_source": "cis_tenant",
    }
    # The last owner removed while CIS had the agency locked: the source moves to the stricter one.
    move = "UPDATE crm.tenant SET lock_source = $2 WHERE tenant_id = $1"
    assert (await _status(app_pool, t, FEED, move, "cis_membership"))["lock_source"] == "cis_membership"
    # A source while not locked is cleared, never stored (the CHECK backs the trigger).
    unlock = "UPDATE crm.tenant SET status = 'active', lock_source = $2 WHERE tenant_id = $1"
    unlocked = await _status(app_pool, t, FEED, unlock, "cis_tenant")
    assert dict(unlocked) == {"status": "active", "lock_source": None}


@pytest.mark.parametrize(
    ("actor", "source"),
    [
        ("platform:staff-sub-1", "cis_tenant"),  # AA staff never pretend to be CIS
        ("platform:staff-sub-1", "cis_membership"),
        ("member:00000000-0000-4000-8000-000000000001", "aa_ops"),  # a member names no source at all
        ("job:crm_lifecycle", "aa_ops"),
    ],
)
async def test_only_the_feed_names_a_cis_source_and_only_a_platform_actor_any(
    app_pool: asyncpg.Pool, actor: str, source: str
) -> None:
    ids = await _agency(app_pool)
    with pytest.raises(asyncpg.PostgresError, match="lock_source_forbidden") as err:
        await _status(
            app_pool,
            ids["tenant"],
            actor,
            "UPDATE crm.tenant SET status = 'locked', lock_source = $2 WHERE tenant_id = $1",
            source,
        )
    assert err.value.sqlstate == "CRM21"


async def test_an_unscoped_session_is_not_restricted(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(ids["tenant"]))
        await admin.execute("UPDATE crm.tenant SET status = 'locked', lock_source = 'cis_tenant'")
        assert await admin.fetchval("SELECT lock_source FROM crm.tenant") == "cis_tenant"
        await admin.execute("UPDATE crm.tenant SET status = 'active'")
        assert await admin.fetchval("SELECT lock_source FROM crm.tenant") is None


async def test_the_feed_state_is_one_row_crm_app_only_updates(app_pool: asyncpg.Pool) -> None:
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE crm.membership_feed_state SET feed_cursor = 'm_000042', last_ok_at = now(), failures = 0"
            )
            row = await conn.fetchrow("SELECT feed_cursor, failures FROM crm.membership_feed_state")
            assert dict(row) == {"feed_cursor": "m_000042", "failures": 0}
        for sql in (
            "INSERT INTO crm.membership_feed_state (singleton) VALUES (true)",
            "DELETE FROM crm.membership_feed_state",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(sql)
        for value in ("has space", "", "x" * 201):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE crm.membership_feed_state SET feed_cursor = $1", value)
        with pytest.raises(asyncpg.CheckViolationError):  # an error class needs its time (and the other way round)
            await conn.execute("UPDATE crm.membership_feed_state SET last_error_class = 'cis_unavailable'")
        await conn.execute("UPDATE crm.membership_feed_state SET feed_cursor = NULL, last_ok_at = NULL")


async def test_the_inbox_keeps_one_row_per_event_per_agency(app_pool: asyncpg.Pool) -> None:
    a, b = await _agency(app_pool), await _agency(app_pool)
    event = uuid.uuid4()
    await _inbox(app_pool, a["tenant"], event, "membership.removed", "applied", "member_disabled")
    with pytest.raises(asyncpg.UniqueViolationError):  # a replayed event is refused by its key
        await _inbox(app_pool, a["tenant"], event, "membership.removed", "applied", "member_disabled")
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, a["tenant"], FEED)
        status = await conn.execute(
            "INSERT INTO crm.membership_inbox (event_id, event_type, outcome, reason_code) "
            "VALUES ($1, 'membership.removed', 'applied', 'member_disabled') ON CONFLICT (event_id) DO NOTHING",
            event,
        )
        assert status == "INSERT 0 0"
    assert await _count(app_pool, a["tenant"], "membership_inbox") == 1
    assert await _count(app_pool, b["tenant"], "membership_inbox") == 0  # FORCE RLS: another agency sees nothing
    # CIS also reports tenants with no CRM agency: recorded under that id, no foreign key.
    await _inbox(app_pool, uuid.uuid4(), uuid.uuid4(), "tenant.created", "ignored", "unknown_agency")


async def test_the_inbox_is_append_only_for_crm_app_and_closed_to_readers(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    await _inbox(app_pool, ids["tenant"], uuid.uuid4(), "tenant.locked", "applied", "agency_locked")
    for sql in (
        "UPDATE crm.membership_inbox SET outcome = 'ignored'",
        "DELETE FROM crm.membership_inbox",
        "TRUNCATE crm.membership_inbox",
    ):
        async with app_pool.acquire() as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await _ctx(conn, ids["tenant"], FEED)
                    await conn.execute(sql)
    async with app_pool.acquire() as conn:
        for reader in READERS:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                for table in ("crm.membership_inbox", "crm.membership_feed_state"):
                    assert not await conn.fetchval(
                        "SELECT has_table_privilege($1, $2, $3)", reader, table, privilege
                    ), (reader, table, privilege)


@pytest.mark.parametrize(
    ("event_type", "outcome", "reason"),
    [
        ("membership.granted", "applied", "member_disabled"),  # not a contract type
        (None, "ignored", "informational"),  # no type: only a rejected row
        ("tenant.locked", "done", "agency_locked"),  # not an outcome
        ("tenant.locked", "applied", "Agency Locked"),  # a reason is a code
    ],
)
async def test_the_inbox_takes_contract_types_outcomes_and_codes_only(
    app_pool: asyncpg.Pool, event_type: str | None, outcome: str, reason: str
) -> None:
    ids = await _agency(app_pool)
    with pytest.raises(asyncpg.CheckViolationError):
        await _inbox(app_pool, ids["tenant"], uuid.uuid4(), event_type, outcome, reason)


async def test_a_rejected_event_may_name_no_type(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    await _inbox(app_pool, ids["tenant"], uuid.uuid4(), None, "rejected", "contract_violation")
    assert await _count(app_pool, ids["tenant"], "membership_inbox") == 1


async def test_the_agency_erase_takes_its_inbox_rows_only(app_pool: asyncpg.Pool) -> None:
    """SCHEDULE §5 erasure gap: without the step the tenant erase refuses with CRM16 (rows left)."""
    ids, other = await _agency(app_pool), await _agency(app_pool)
    for _ in range(2):
        await _inbox(app_pool, ids["tenant"], uuid.uuid4(), "membership.role_changed", "ignored", "informational")
    await _inbox(app_pool, other["tenant"], uuid.uuid4(), "membership.role_changed", "ignored", "informational")
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    dry = await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"], dry_run=True)
    assert dry["counts"]["membership_inbox"] == 2
    done = await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert done["counts"]["membership_inbox"] == 2
    assert await _count(app_pool, ids["tenant"], "membership_inbox") == 0
    assert await _count(app_pool, other["tenant"], "membership_inbox") == 1


async def test_crm_app_cannot_delete_inbox_rows_through_the_owner_path(admin: asyncpg.Connection) -> None:
    """Only crm.erase_subject (crm_owner + app.erasing) deletes; a plain owner session is refused by the trigger."""
    tenant = uuid.uuid4()
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await admin.execute(
            "INSERT INTO crm.membership_inbox (event_id, event_type, outcome, reason_code) "
            "VALUES ($1, 'tenant.created', 'ignored', 'unknown_agency')",
            uuid.uuid4(),
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
            async with admin.transaction():
                await admin.execute("DELETE FROM crm.membership_inbox")


async def test_the_erase_text_keeps_the_crm012_tables_and_names_the_inbox(admin: asyncpg.Connection) -> None:
    """crm-014 / crm-015 replace crm.erase_subject again; they must start from this text (S250 schedule §1)."""
    source = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    for table in ("client_link", "meeting_slot", "membership_inbox"):
        assert f"'{table}'" in source
