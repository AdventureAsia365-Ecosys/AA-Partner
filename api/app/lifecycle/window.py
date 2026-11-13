"""The agency's trash window (AA-942; design §3.5, Fix E3): ``crm.tenant_settings.trash_days``; an agency without a
settings row gets the platform default (AA-1096 L4: ``crm.agency_defaults.trash_days``, 7, admin-editable). A leaf
module (only type imports from the app, and the settings reader) so every module that trashes a row can read it."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.tenancy import LifecycleContext, TenantContext

from app import platform_settings as ps

TRASH_DAYS_SQL = "SELECT trash_days FROM crm.tenant_settings WHERE tenant_id = $1"


async def trash_days(ctx: TenantContext | LifecycleContext) -> int:
    days = await ctx.conn.fetchval(TRASH_DAYS_SQL, ctx.tenant_id)
    return int(days) if days is not None else int(ps.get(ps.TRASH_DAYS_DEFAULT))
