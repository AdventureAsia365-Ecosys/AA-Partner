"""The agency's trash window (AA-942; design §3.5, Fix E3): ``crm.tenant_settings.trash_days``, default 7. A leaf module
(only type imports from the app) so every module that trashes a row can read it."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.tenancy import LifecycleContext, TenantContext

DEFAULT_TRASH_DAYS = 7
TRASH_DAYS_SQL = "SELECT trash_days FROM crm.tenant_settings WHERE tenant_id = $1"


async def trash_days(ctx: TenantContext | LifecycleContext) -> int:
    days = await ctx.conn.fetchval(TRASH_DAYS_SQL, ctx.tenant_id)
    return int(days) if days is not None else DEFAULT_TRASH_DAYS
