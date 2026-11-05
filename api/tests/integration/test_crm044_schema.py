"""crm-044 (AA-969 part 2, root ADR 0019 d16) on PostgreSQL 15 with the RDS role shape: the pilot marker.

- registered, and a re-run as crm_owner is a no-op;
- ``crm.tenant.is_pilot`` defaults to false;
- only the CIS account feed actor sets or clears it; a member, the L1 console (another platform actor) and a seeder are
  refused with CRM04 ``platform_only``; an update that leaves it unchanged is never checked;
- at most one pilot agency.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import asyncpg
import pytest

from tests.integration.conftest import OWNER
from tests.integration.test_crm010_schema import _ctx

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "044_crm_tenant_pilot_marker.sql"
ACCOUNT_FEED = "platform:feed-tenant-account"


async def _agency(pool: asyncpg.Pool) -> uuid.UUID:
    tenant = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, "seed:test")
        await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant)
    return tenant


async def _set(pool: asyncpg.Pool, tenant: uuid.UUID, actor: str, value: bool) -> bool:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        await conn.execute("UPDATE crm.tenant SET is_pilot = $2 WHERE tenant_id = $1", tenant, value)
        return bool(await conn.fetchval("SELECT is_pilot FROM crm.tenant WHERE tenant_id = $1", tenant))


async def _clear(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    await _set(pool, tenant, ACCOUNT_FEED, False)


async def test_crm044_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-044'") == 1
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval(
        "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'crm.tenant'::regclass AND tgname LIKE 'tenant_pilot_marker%'"
    ) == 2  # fmt: skip


async def test_a_new_agency_is_not_the_pilot(app_pool: asyncpg.Pool) -> None:
    tenant = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, "seed:test")
        assert await conn.fetchval("SELECT is_pilot FROM crm.tenant WHERE tenant_id = $1", tenant) is False


async def test_only_the_account_feed_sets_or_clears_the_marker(app_pool: asyncpg.Pool) -> None:
    tenant = await _agency(app_pool)
    try:
        for actor in ("member:00000000-0000-4000-8000-000000000001", "platform:staff-sub-1", "seed:test", "job:x"):
            with pytest.raises(asyncpg.PostgresError, match="platform_only") as err:
                await _set(app_pool, tenant, actor, True)
            assert err.value.sqlstate == "CRM04"
        assert await _set(app_pool, tenant, ACCOUNT_FEED, True) is True
        # an update that keeps the marker as it is (another column) is not checked
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, tenant, "platform:staff-sub-1")
            await conn.execute("UPDATE crm.tenant SET display_name = 'Pilot Coast Travel' WHERE tenant_id = $1", tenant)
        with pytest.raises(asyncpg.PostgresError, match="platform_only"):
            await _set(app_pool, tenant, "platform:staff-sub-1", False)  # nobody else clears it either
    finally:
        await _clear(app_pool, tenant)


async def test_an_insert_marked_pilot_needs_the_account_feed(app_pool: asyncpg.Pool) -> None:
    tenant = uuid.uuid4()
    async with app_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError, match="platform_only"):
            async with conn.transaction():
                await _ctx(conn, tenant, "seed:test")
                await conn.execute("INSERT INTO crm.tenant (tenant_id, is_pilot) VALUES ($1, true)", tenant)


async def test_at_most_one_pilot_agency(app_pool: asyncpg.Pool) -> None:
    first, second = await _agency(app_pool), await _agency(app_pool)
    try:
        await _set(app_pool, first, ACCOUNT_FEED, True)
        with pytest.raises(asyncpg.UniqueViolationError):
            await _set(app_pool, second, ACCOUNT_FEED, True)
    finally:
        await _clear(app_pool, first)
