"""The AI budget limits of an agency (AA-1096 L4, crm-064): admin-editable platform settings, read by the gateway, the
agency's Settings > AI and the L1 usage meter.

- ``crm.ai.budget_default_usd``: the budget of an agency without a settings row (and of a new agency);
- ``crm.ai.budget_max_usd``: the highest budget an owner may set, a ``platform+tenant`` key (an administrator may give
  one agency its own ceiling), read in the agency's own context; ``AI_BUDGET_MAX_USD`` overrides it for one release.

The gateway enforces ``min(agency budget, ceiling)``: lowering the ceiling lowers every budget above it at once.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app import platform_settings as ps
from app.tenancy import AnyContext


def default_budget_usd() -> Decimal:
    """The monthly AI budget of an agency with no settings row (AA-1096 L4: ``crm.ai.budget_default_usd``, 2 USD,
    admin-editable)."""
    return ps.decimal(ps.AI_BUDGET_DEFAULT)


async def budget_ceiling(ctx: AnyContext) -> Decimal:
    """The highest monthly AI budget of this agency (``crm.ai.budget_max_usd``: the platform value or the agency's own,
    read in its context; ``AI_BUDGET_MAX_USD`` overrides it for one release)."""
    return ps.as_decimal(await ps.tenant_get(ctx.conn, ps.AI_BUDGET_MAX, ctx.tenant_id))


async def agency_budget(ctx: AnyContext, row: Any) -> Decimal:
    """The budget the gateway enforces: the agency's own (or the default without a settings row), never above its
    ceiling (an administrator who lowers the ceiling lowers every budget above it at once)."""
    stored = Decimal(row["ai_budget_usd"]) if row else default_budget_usd()
    return min(stored, await budget_ceiling(ctx))
