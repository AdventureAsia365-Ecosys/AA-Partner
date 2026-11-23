"""AA-1152 (crm-086 / crm-087): :mod:`app.proposals.rounding` reads and merges the agency's rounding steps and its
planner prices switch through a real tenant transaction as ``crm_app``, and the add-on drawer's AA tour read
(:data:`app.proposals.aa_addons.TOUR_SQL`) runs on the real tables (the migration itself: ``test_crm086_schema.py``).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import asyncpg
import pytest

from app.proposals import aa_addons, rounding
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
            first = await rounding.settings(ctx)
            assert first.show_planner_prices is True and first.chosen == {} and first.step("USD") == Decimal("1")
            await rounding.update(ctx, {"steps": {"USD": "5", "VND": "50000"}})
            await rounding.update(ctx, {"steps": {"VND": None}, "show_planner_prices": False})
            now = await rounding.settings(ctx)
            assert now.chosen == {"USD": Decimal("5")} and now.show_planner_prices is False
            assert now.step("VND") == Decimal("10000")  # the platform default again
            assert await conn.fetchval(aa_addons.TOUR_SQL, uuid.uuid4(), uuid.uuid4()) is None
        finally:
            await tx.rollback()
