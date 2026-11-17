"""AA-1126 L3 (crm-076) on PostgreSQL with the RDS role shape: the entitlement pull projects the CRM plan's AI budget
(crm.ai_budget_usd_monthly) into crm.tenant_entitlement.ai_budget_usd; the agency's budget and ceiling follow it in the
agency's own request transaction (crm_app, RLS); the owner's budget moves with a new plan budget while it still equals
the previous one, and stays when the owner chose another. Invented agencies only."""

from __future__ import annotations

from decimal import Decimal

import asyncpg
import pytest

from app.llm import budget_limits
from app.tenancy import tenant_tx
from tests.integration.test_crm005_intake import _agency
from tests.integration.test_crm043_entitlement_feed import JOB, Cis, _job, cis  # noqa: F401 — the fixture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENTS = {"crm.core": None, "crm.ai_assist": None, "crm.seats": "3.00"}


async def _owner_budget(pool: asyncpg.Pool, owner, tenant) -> Decimal | None:  # noqa: ANN001
    async with tenant_tx(pool, owner, tenant) as ctx:
        return await ctx.conn.fetchval("SELECT ai_budget_usd FROM crm.tenant_settings WHERE deleted_at IS NULL")


async def test_the_plan_budget_is_projected_and_enforced(app_pool: asyncpg.Pool, cis: Cis) -> None:  # noqa: F811
    tenant, people = await _agency(app_pool)
    owner = people[0][0]
    cis.account(tenant, ents=ENTS | {"crm.ai_budget_usd_monthly": "12.00"})
    assert (await _job(cis)(app_pool, JOB))["applied_count"] == 1
    async with tenant_tx(app_pool, owner, tenant) as ctx:
        assert await budget_limits.plan_budget(ctx) == Decimal("12.00")
        assert await budget_limits.budget_ceiling(ctx) == Decimal("12.00")
        row = await ctx.conn.fetchrow("SELECT ai_budget_usd FROM crm.tenant_settings WHERE deleted_at IS NULL")
        assert await budget_limits.agency_budget(ctx, row) <= Decimal("12.00")
    stored = await _owner_budget(app_pool, owner, tenant)
    if stored is not None:  # the agency's settings row existed at the default: it followed the plan
        assert stored in (Decimal("12.00"), budget_limits.default_budget_usd())


async def test_the_owner_budget_follows_a_new_plan_budget_only_while_unchanged(
    app_pool: asyncpg.Pool,
    cis: Cis,  # noqa: F811
) -> None:
    tenant, people = await _agency(app_pool)
    owner = people[0][0]
    async with tenant_tx(app_pool, owner, tenant) as ctx:
        await ctx.conn.execute(
            "INSERT INTO crm.tenant_settings (ai_enabled, ai_budget_usd) VALUES (false, $1) "
            "ON CONFLICT (tenant_id) DO UPDATE SET ai_budget_usd = EXCLUDED.ai_budget_usd",
            budget_limits.default_budget_usd(),
        )
    cis.account(tenant, ents=ENTS | {"crm.ai_budget_usd_monthly": "10.00"})
    await _job(cis)(app_pool, JOB)
    assert await _owner_budget(app_pool, owner, tenant) == Decimal("10.00")  # was at the default: follows
    async with tenant_tx(app_pool, owner, tenant) as ctx:
        await ctx.conn.execute("UPDATE crm.tenant_settings SET ai_budget_usd = 4 WHERE deleted_at IS NULL")
    cis.account(tenant, ents=ENTS | {"crm.ai_budget_usd_monthly": "20.00"})
    await _job(cis)(app_pool, JOB)
    assert await _owner_budget(app_pool, owner, tenant) == Decimal("4.00")  # the owner's own choice stays
    async with tenant_tx(app_pool, owner, tenant) as ctx:
        assert await budget_limits.budget_ceiling(ctx) == Decimal("20.00")
