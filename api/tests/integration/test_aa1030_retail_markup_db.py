"""AA-1030 (crm-046): :mod:`app.proposals.retail` reads and merges the agency's retail markup through a real tenant
transaction as ``crm_app`` (the migration itself: ``test_crm046_schema.py``).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import asyncpg
import pytest

from app.proposals import retail
from app.tenancy import TenantContext

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


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
