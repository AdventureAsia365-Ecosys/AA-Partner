"""Settings > Online booking (AA-1072, item crm-02): the owner's "Book now" switch and the links to use.

``tenant_settings.public_booking_enabled`` (crm-057, default off: the business-owner default, clients book through
an agent until the owner turns it on). The answer lists the agency's active intake forms: the public tour page books
for the agency whose form key its link carries (``<client host>/trip/<tour id>?f=<form key>``), so the owner copies
a key from here. Owner only (``PAYMENT_SETTINGS``); the page shows no client data.

AA-1092 (crm-062): ``tenant_settings.public_price_per_minute``, the price checks a minute the agency's Book now takes
from every address together (the policy value of the per-agency ceiling, ``app.routers.public`` ``post_booking_price``):
default :data:`DEFAULT_PRICE_PER_AGENCY_MINUTE`, the owner sets :data:`MIN_PRICE_PER_AGENCY_MINUTE` to
:data:`MAX_PRICE_PER_AGENCY_MINUTE` here (the same bounds as the column CHECK). Each API task counts on its own (the CRM
API has no shared limiter store), so with N tasks an agency gets up to N times its value.
"""

from __future__ import annotations

from typing import Any

from app.errors import Invalid
from app.settings import Settings
from app.tenancy import TenantContext

# The crm-062 column default and CHECK bounds. The default applies only where no settings row exists yet (the column
# default fills every row); the bounds answer a 422 before the database's CHECK would.
DEFAULT_PRICE_PER_AGENCY_MINUTE = 60
MIN_PRICE_PER_AGENCY_MINUTE = 10
MAX_PRICE_PER_AGENCY_MINUTE = 600

READ_SQL = "SELECT public_booking_enabled, public_price_per_minute FROM crm.tenant_settings"
UPSERT_SQL = """
INSERT INTO crm.tenant_settings (tenant_id, public_booking_enabled)
VALUES (current_setting('app.tenant_id')::uuid, $1)
ON CONFLICT (tenant_id) DO UPDATE SET public_booking_enabled = EXCLUDED.public_booking_enabled
"""
CEILING_SQL = """
INSERT INTO crm.tenant_settings (tenant_id, public_price_per_minute)
VALUES (current_setting('app.tenant_id')::uuid, $1)
ON CONFLICT (tenant_id) DO UPDATE SET public_price_per_minute = EXCLUDED.public_price_per_minute
"""
FORMS_SQL = """
SELECT label, public_key FROM crm.intake_form WHERE status = 'active' AND deleted_at IS NULL
 ORDER BY created_at, intake_form_id
 LIMIT 20
"""


async def read(ctx: TenantContext, settings: Settings) -> dict[str, Any]:
    origin = settings.messaging_hooks_origin()
    row = await ctx.conn.fetchrow(READ_SQL)
    return {
        "enabled": bool(row and row["public_booking_enabled"]),
        "price_checks_per_minute": int(row["public_price_per_minute"]) if row else DEFAULT_PRICE_PER_AGENCY_MINUTE,
        "price_checks_bounds": {
            "min": MIN_PRICE_PER_AGENCY_MINUTE,
            "max": MAX_PRICE_PER_AGENCY_MINUTE,
            "default": DEFAULT_PRICE_PER_AGENCY_MINUTE,
        },
        "page_base": f"{origin}/trip/" if origin else None,
        "forms": [{"label": r["label"], "public_key": r["public_key"]} for r in await ctx.conn.fetch(FORMS_SQL)],
    }


async def update(
    ctx: TenantContext, settings: Settings, enabled: Any, price_checks_per_minute: Any = None
) -> dict[str, Any]:
    """The switch, and (when given) the price checks a minute; each change is audited with the field it changed."""
    if enabled is not None and not isinstance(enabled, bool):
        raise Invalid("invalid_enabled", "true or false")
    ceiling = price_checks_per_minute
    if ceiling is not None and (
        isinstance(ceiling, bool)
        or not isinstance(ceiling, int)
        or not MIN_PRICE_PER_AGENCY_MINUTE <= ceiling <= MAX_PRICE_PER_AGENCY_MINUTE
    ):
        raise Invalid(
            "invalid_price_checks_per_minute",
            f"a whole number from {MIN_PRICE_PER_AGENCY_MINUTE} to {MAX_PRICE_PER_AGENCY_MINUTE}",
        )
    if enabled is None and ceiling is None:
        raise Invalid("nothing_to_change", "send enabled or price_checks_per_minute")
    if enabled is not None:
        await ctx.conn.execute(UPSERT_SQL, enabled)
        ctx.audit("update", "allowed", "tenant_settings", str(ctx.tenant_id), fields=("public_booking_enabled",))
    if ceiling is not None:
        await ctx.conn.execute(CEILING_SQL, ceiling)
        ctx.audit("update", "allowed", "tenant_settings", str(ctx.tenant_id), fields=("public_price_per_minute",))
    return await read(ctx, settings)
