"""Settings > Online booking (AA-1072, item crm-02): the owner's "Book now" switch and the links to use.

``tenant_settings.public_booking_enabled`` (crm-057, default off: the business-owner default, clients book through
an agent until the owner turns it on). The answer lists the agency's active intake forms: the public tour page books
for the agency whose form key its link carries (``<client host>/trip/<tour id>?f=<form key>``), so the owner copies
a key from here. Owner only (``PAYMENT_SETTINGS``); the page shows no client data.
"""

from __future__ import annotations

from typing import Any

from app.errors import Invalid
from app.settings import Settings
from app.tenancy import TenantContext

READ_SQL = "SELECT public_booking_enabled FROM crm.tenant_settings"
UPSERT_SQL = """
INSERT INTO crm.tenant_settings (tenant_id, public_booking_enabled)
VALUES (current_setting('app.tenant_id')::uuid, $1)
ON CONFLICT (tenant_id) DO UPDATE SET public_booking_enabled = EXCLUDED.public_booking_enabled
"""
FORMS_SQL = """
SELECT label, public_key FROM crm.intake_form WHERE status = 'active' AND deleted_at IS NULL
 ORDER BY created_at, intake_form_id
 LIMIT 20
"""


async def read(ctx: TenantContext, settings: Settings) -> dict[str, Any]:
    origin = settings.messaging_hooks_origin()
    return {
        "enabled": bool(await ctx.conn.fetchval(READ_SQL)),
        "page_base": f"{origin}/trip/" if origin else None,
        "forms": [{"label": r["label"], "public_key": r["public_key"]} for r in await ctx.conn.fetch(FORMS_SQL)],
    }


async def update(ctx: TenantContext, settings: Settings, enabled: Any) -> dict[str, Any]:
    if not isinstance(enabled, bool):
        raise Invalid("invalid_enabled", "true or false")
    await ctx.conn.execute(UPSERT_SQL, enabled)
    ctx.audit("update", "allowed", "tenant_settings", str(ctx.tenant_id), fields=("public_booking_enabled",))
    return await read(ctx, settings)
