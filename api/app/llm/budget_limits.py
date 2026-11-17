"""The AI budget limits of an agency (AA-1096 L4, crm-064; AA-1126 L3, crm-076), read by the gateway, the agency's
Settings > AI, the L1 usage meter and the support reads.

- **The plan's AI budget** (AA-1126 L3): ``crm.tenant_entitlement.ai_budget_usd``, the CIS plan feature
  ``crm.ai_budget_usd_monthly`` of the agency's CRM plan (or its Tenant 360 override), projected by
  ``crm_entitlement_pull`` (:mod:`app.cis.entitlement_feed`). When set it is both the agency's ceiling and the budget of
  an agency with no settings row; the projection also moves the owner's budget with it (see the feed).
- The platform settings, the fallback when the plan gives none (an agency not projected yet, a plan without the key):
  ``crm.ai.budget_default_usd`` (the budget of an agency without a settings row) and ``crm.ai.budget_max_usd`` (the
  highest budget an owner may set, a ``platform+tenant`` key read in the agency's own context).

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


async def budget_ceiling(ctx: AnyContext) -> Decimal:
    """The highest monthly AI budget of this agency: its plan's (AA-1126 L3), else ``crm.ai.budget_max_usd`` (the
    platform value or the agency's own, read in its context)."""
    plan = await plan_budget(ctx)
    if plan is not None:
        return plan
    return ps.as_decimal(await ps.tenant_get(ctx.conn, ps.AI_BUDGET_MAX, ctx.tenant_id))


async def agency_budget(ctx: AnyContext, row: Any) -> Decimal:
    """The budget the gateway enforces: the agency's own (or, without a settings row, its plan's or the default), never
    above its ceiling."""
    stored = Decimal(row["ai_budget_usd"]) if row else await new_agency_budget(ctx)
    return min(stored, await budget_ceiling(ctx))


def budget_source(plan: Decimal | None) -> str:
    """``plan`` when the agency's plan gives its AI budget, else ``settings`` (Settings > AI says which)."""
    return "plan" if plan is not None else "settings"
