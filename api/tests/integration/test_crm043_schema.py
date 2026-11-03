"""crm-043 (AA-1028 R1 slice 2) on PostgreSQL 15 with the RDS role shape: the storage of crm_entitlement_pull.

- registered, and a re-run as crm_owner is a no-op (one feed state row);
- ``crm.entitlement_feed_state``: one platform row, crm_app reads and updates it, never inserts or deletes; the
  reconcile's own failure count is never negative;
- ``crm.tenant_entitlement.projected_at``: written by a platform actor (the account feed), refused to a member
  (the crm-008 platform-only guard, CRM04);
- the lock source rule: the account feed actor names ``cis_tenant`` (and an unlock clears it), never
  ``cis_membership``; the membership feed keeps both; AA staff still name neither.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import asyncpg
import pytest

from tests.integration.conftest import OWNER
from tests.integration.test_crm010_schema import _ctx

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "043_crm_entitlement_feed.sql"
ACCOUNT_FEED = "platform:feed-tenant-account"
MEMBERSHIP_FEED = "platform:feed-tenant-membership"


async def _agency(pool: asyncpg.Pool) -> uuid.UUID:
    tenant = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, "seed:test")
        await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant)
    return tenant


async def _as(pool: asyncpg.Pool, tenant: uuid.UUID, actor: str, sql: str, *args: object) -> asyncpg.Record | None:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        await conn.execute(sql, *args)
        return await conn.fetchrow("SELECT status, lock_source FROM crm.tenant WHERE tenant_id = $1", tenant)


async def test_crm043_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-043'") == 1
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM crm.entitlement_feed_state") == 1


async def test_the_feed_state_is_one_row_crm_app_only_updates(app_pool: asyncpg.Pool) -> None:
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE crm.entitlement_feed_state SET feed_cursor = 'a_000042', last_reconcile_at = now(), "
                "failures = 0"
            )
            row = await conn.fetchrow("SELECT feed_cursor, failures FROM crm.entitlement_feed_state")
            assert dict(row) == {"feed_cursor": "a_000042", "failures": 0}
            await conn.execute("UPDATE crm.entitlement_feed_state SET feed_cursor = NULL, last_reconcile_at = NULL")
        for sql in (
            "INSERT INTO crm.entitlement_feed_state (singleton) VALUES (true)",
            "DELETE FROM crm.entitlement_feed_state",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(sql)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE crm.entitlement_feed_state SET feed_cursor = 'has space'")
        # the reconcile's own failure count (review fix): never negative, reset to 0 by a clean pass
        await conn.execute("UPDATE crm.entitlement_feed_state SET reconcile_failures = 0")
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE crm.entitlement_feed_state SET reconcile_failures = -1")


async def test_projected_at_is_written_by_the_account_feed_only(app_pool: asyncpg.Pool) -> None:
    tenant = await _agency(app_pool)
    upsert = (
        "INSERT INTO crm.tenant_entitlement (plan, member_limit, allowed_features, projected_at) "
        "VALUES ('core', 5, ARRAY['ai_assist'], now()) ON CONFLICT (tenant_id) DO UPDATE SET projected_at = now()"
    )
    await _as(app_pool, tenant, ACCOUNT_FEED, upsert)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, ACCOUNT_FEED)
        row = await conn.fetchrow("SELECT plan, member_limit, projected_at FROM crm.tenant_entitlement")
        assert row["plan"] == "core" and row["member_limit"] == 5 and row["projected_at"] is not None
    with pytest.raises(asyncpg.PostgresError, match="platform_only") as err:
        await _as(app_pool, tenant, "member:00000000-0000-4000-8000-000000000001", upsert)
    assert err.value.sqlstate == "CRM04"


async def test_the_account_feed_names_cis_tenant_and_never_cis_membership(app_pool: asyncpg.Pool) -> None:
    tenant = await _agency(app_pool)
    lock = "UPDATE crm.tenant SET status = 'locked', lock_source = $2 WHERE tenant_id = $1"
    assert dict(await _as(app_pool, tenant, ACCOUNT_FEED, lock, tenant, "cis_tenant")) == {
        "status": "locked",
        "lock_source": "cis_tenant",
    }
    with pytest.raises(asyncpg.PostgresError, match="lock_source_forbidden") as err:
        await _as(app_pool, tenant, ACCOUNT_FEED, "UPDATE crm.tenant SET lock_source = $2 WHERE tenant_id = $1",
                  tenant, "cis_membership")  # fmt: skip
    assert err.value.sqlstate == "CRM21"
    unlock = "UPDATE crm.tenant SET status = 'active' WHERE tenant_id = $1"
    assert dict(await _as(app_pool, tenant, ACCOUNT_FEED, unlock, tenant)) == {"status": "active", "lock_source": None}
    # the membership feed keeps both sources; AA staff name neither
    assert (await _as(app_pool, tenant, MEMBERSHIP_FEED, lock, tenant, "cis_membership"))["lock_source"] == (
        "cis_membership"
    )
    await _as(app_pool, tenant, "platform:staff-sub-1", unlock, tenant)
    for source in ("cis_tenant", "cis_membership"):
        with pytest.raises(asyncpg.PostgresError, match="lock_source_forbidden"):
            await _as(app_pool, tenant, "platform:staff-sub-1", lock, tenant, source)
