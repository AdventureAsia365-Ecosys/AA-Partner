"""The AI budget limits of an agency (AA-1096 L4, crm-064; AA-1126 L3, crm-076), read by the gateway, the agency's
Settings > AI, the L1 usage meter and the support reads.

The ceiling, first match wins (:func:`ceiling`; the CIS Platform › Settings CRM tab and the support reads show the
same order, so the admin page never lists a value the gateway does not apply):

1. **The agency's own ceiling**: an administrator's per-agency value of ``crm.ai.budget_max_usd`` (a
   ``platform+tenant`` key, read in the agency's own context). It wins over the plan (AA-1126 review fix, S283): an
   administrator who gave one agency more (or less) keeps that decision when a plan budget arrives.
2. **The plan's AI budget** (AA-1126 L3): ``crm.tenant_entitlement.ai_budget_usd``, the CIS plan feature
   ``crm.ai_budget_usd_monthly`` of the agency's CRM plan (or its Tenant 360 override), projected by
   ``crm_entitlement_pull`` (:mod:`app.cis.entitlement_feed`). It is also the budget of an agency with no settings
   row; the projection moves the owner's budget with it (see the feed).
3. **The platform value** of ``crm.ai.budget_max_usd`` (and ``crm.ai.budget_default_usd`` for the budget of an agency
   without a settings row): the fallback when the plan gives none (an agency not projected yet, a plan without the
   key).

The gateway enforces ``min(agency budget, ceiling)``: lowering the ceiling (the plan or the setting) lowers every budget
above it at once. A plan budget that cannot be read (a role without the column's grant, crm-076 not applied) falls back
to the settings: logged, never raised, inside a savepoint so the caller's transaction goes on.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from app import platform_settings as ps
from app.tenancy import AnyContext

log = logging.getLogger(__name__)

PLAN_BUDGET_SQL = "SELECT ai_budget_usd FROM crm.tenant_entitlement WHERE deleted_at IS NULL"


def default_budget_usd() -> Decimal:
    """The monthly AI budget of an agency with no settings row when its plan gives none (AA-1096 L4:
    ``crm.ai.budget_default_usd``, 2 USD, admin-editable)."""
    return ps.decimal(ps.AI_BUDGET_DEFAULT)


async def plan_budget(ctx: AnyContext) -> Decimal | None:
    """The monthly AI budget of the agency's CRM plan (crm-076), or None when the plan gives none or it cannot be
    read."""
    try:
        async with ctx.conn.transaction():
            value = await ctx.conn.fetchval(PLAN_BUDGET_SQL)
    except Exception as exc:  # noqa: BLE001 — the settings apply
        log.warning("crm_ai_plan_budget_unreadable error=%s", type(exc).__name__)
        return None
    return Decimal(value) if value is not None else None


async def new_agency_budget(ctx: AnyContext) -> Decimal:
    """The budget of an agency with no settings row: its plan's, else ``crm.ai.budget_default_usd``."""
    plan = await plan_budget(ctx)
    return plan if plan is not None else default_budget_usd()


SOURCE_AGENCY, SOURCE_PLAN, SOURCE_SETTINGS = "agency", "plan", "settings"


async def ceiling(ctx: AnyContext) -> tuple[Decimal, str]:
    """The highest monthly AI budget of this agency and where it comes from (module docstring): ``agency`` (an
    administrator's own value for it), ``plan`` (its CRM plan) or ``settings`` (the platform value)."""
    own = await ps.tenant_get(ctx.conn, ps.AI_BUDGET_MAX, ctx.tenant_id)
    if ps.resolve(ps.AI_BUDGET_MAX, ctx.tenant_id).source == "tenant":
        return ps.as_decimal(own), SOURCE_AGENCY
    plan = await plan_budget(ctx)
    if plan is not None:
        return plan, SOURCE_PLAN
    return ps.as_decimal(own), SOURCE_SETTINGS


async def budget_ceiling(ctx: AnyContext) -> Decimal:
    """The highest monthly AI budget of this agency (:func:`ceiling`)."""
    return (await ceiling(ctx))[0]


async def agency_budget(ctx: AnyContext, row: Any) -> Decimal:
    """The budget the gateway enforces: the agency's own (or, without a settings row, its plan's or the default), never
    above its ceiling."""
    stored = Decimal(row["ai_budget_usd"]) if row else await new_agency_budget(ctx)
    return min(stored, await budget_ceiling(ctx))


async def budget_source(ctx: AnyContext) -> str:
    """``plan`` when the agency's plan gives its AI budget ceiling, else ``settings`` (the platform value or an
    administrator's own value for the agency): Settings > AI says which."""
    return SOURCE_PLAN if (await ceiling(ctx))[1] == SOURCE_PLAN else SOURCE_SETTINGS
