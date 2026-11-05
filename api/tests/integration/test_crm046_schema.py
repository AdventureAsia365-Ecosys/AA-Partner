"""crm-046 (AA-1030, a contract file) on PostgreSQL with the RDS role shape: ``crm.tenant_settings.retail_markup_pct``
(default ``{}``), its shape rule ``crm.retail_markup_ok`` (known product types, 0-300 %, at most 2 decimals), the
grants (``crm_app`` writes it, ``crm_support_reader`` does not read it), and :mod:`app.proposals.retail` reading and
merging it through a real tenant transaction as ``crm_app``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import asyncpg
import pytest

from app.proposals import retail
from app.tenancy import TenantContext

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_crm046_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-046'") == 1
    default = await admin.fetchval(
        "SELECT column_default FROM information_schema.columns WHERE table_schema = 'crm'"
        " AND table_name = 'tenant_settings' AND column_name = 'retail_markup_pct'"
    )
    assert default == "'{}'::jsonb"


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("{}", True),
        ('{"trip_plan": 20}', True),
        ('{"hotel_room": 12.5, "meal": 0, "transfer": 300}', True),
        ('{"spa": 5}', False),
        ('{"trip_plan": 301}', False),
        ('{"trip_plan": -1}', False),
        ('{"trip_plan": 1.234}', False),
        ('{"trip_plan": "20"}', False),
        ("[]", False),
    ],
)
async def test_the_shape_rule(admin: asyncpg.Connection, value: str, ok: bool) -> None:
    assert await admin.fetchval("SELECT crm.retail_markup_ok($1::jsonb) AS ok", value) is ok


async def test_grants(admin: asyncpg.Connection) -> None:
    for privilege in ("INSERT", "UPDATE", "SELECT"):
        assert await admin.fetchval(
            "SELECT has_column_privilege('crm_app', 'crm.tenant_settings', 'retail_markup_pct', $1) AS ok", privilege
        )
    assert not await admin.fetchval(
        "SELECT has_column_privilege('crm_support_reader', 'crm.tenant_settings', 'retail_markup_pct', 'SELECT') AS ok"
    )


async def test_crm_app_reads_and_merges_through_a_tenant(app_pool: asyncpg.Pool) -> None:
    tenant_id = uuid.uuid4()
    async with app_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
            await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant_id)
            ctx = TenantContext(conn=conn, tenant_id=tenant_id, member_id=uuid.uuid4(), member_role="owner")
            assert await retail.read(ctx) == {"retail_markup_pct": {}, "kinds": list(retail.KINDS)}
            await retail.update(ctx, {"trip_plan": Decimal("20"), "hotel_room": Decimal("12.50")})
            await retail.update(ctx, {"hotel_room": None, "meal": Decimal("7.25")})
            assert (await retail.read(ctx))["retail_markup_pct"] == {"trip_plan": "20", "meal": "7.25"}
            assert await retail.markups(ctx) == {"trip_plan": Decimal("20"), "meal": Decimal("7.25")}
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE crm.tenant_settings SET retail_markup_pct = '{\"trip_plan\": 400}'::jsonb"
                    )
        finally:
            await tx.rollback()
