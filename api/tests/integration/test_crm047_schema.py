"""crm-047 (AA-1038 R5 close-out, a contract file) on PostgreSQL with the RDS role shape:
``crm.tenant_entitlement.tp_crm_leads`` (default false), registered and re-runnable as crm_owner, readable and writable
by ``crm_app`` under the agency's RLS, written by a platform actor (the account feed) and refused to a member (the
crm-008 platform-only guard, CRM04), never readable by ``crm_support_reader``. The projection writing it
(:mod:`app.cis.entitlement_feed`) and the lead pull reading it (:mod:`app.tripplanner.leads`) are tested in the code PR.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import asyncpg
import pytest

from tests.integration.conftest import OWNER
from tests.integration.test_crm010_schema import _ctx

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "047_crm_tp_crm_leads.sql"
ACCOUNT_FEED = "platform:feed-tenant-account"


async def _agency(pool: asyncpg.Pool) -> uuid.UUID:
    tenant = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, "seed:test")
        await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant)
    return tenant


async def test_crm047_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-047'") == 1
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    row = await admin.fetchrow(
        "SELECT column_default AS d, is_nullable AS n, data_type AS t FROM information_schema.columns"
        " WHERE table_schema = 'crm' AND table_name = 'tenant_entitlement' AND column_name = 'tp_crm_leads'"
    )
    assert dict(row) == {"d": "false", "n": "NO", "t": "boolean"}


async def test_grants(admin: asyncpg.Connection) -> None:
    for privilege in ("INSERT", "UPDATE", "SELECT"):
        assert await admin.fetchval(
            "SELECT has_column_privilege('crm_app', 'crm.tenant_entitlement', 'tp_crm_leads', $1) AS ok", privilege
        )
    assert not await admin.fetchval(
        "SELECT has_column_privilege('crm_support_reader', 'crm.tenant_entitlement', 'tp_crm_leads', 'SELECT') AS ok"
    )


async def test_the_account_feed_writes_it_a_member_cannot(app_pool: asyncpg.Pool) -> None:
    tenant = await _agency(app_pool)
    upsert = (
        "INSERT INTO crm.tenant_entitlement (plan, member_limit, allowed_features, projected_at, tp_crm_leads) "
        "VALUES ('core', 5, ARRAY[]::text[], now(), true) "
        "ON CONFLICT (tenant_id) DO UPDATE SET tp_crm_leads = EXCLUDED.tp_crm_leads"
    )
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, ACCOUNT_FEED)
        await conn.execute(upsert)
        assert await conn.fetchval("SELECT tp_crm_leads AS on_crm FROM crm.tenant_entitlement") is True
    # another agency's context sees no row (RLS)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, uuid.uuid4(), ACCOUNT_FEED)
        assert await conn.fetchval("SELECT count(*) AS n FROM crm.tenant_entitlement") == 0
    with pytest.raises(asyncpg.PostgresError, match="platform_only") as err:
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, tenant, "member:00000000-0000-4000-8000-000000000001")
            await conn.execute("UPDATE crm.tenant_entitlement SET tp_crm_leads = false")
    assert err.value.sqlstate == "CRM04"


async def test_an_existing_row_reads_false(app_pool: asyncpg.Pool) -> None:
    tenant = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, ACCOUNT_FEED)
        await conn.execute(
            "INSERT INTO crm.tenant_entitlement (plan, member_limit, allowed_features)"
            " VALUES ('core', 3, ARRAY[]::text[])"
        )
        assert await conn.fetchval("SELECT tp_crm_leads AS on_crm FROM crm.tenant_entitlement") is False
