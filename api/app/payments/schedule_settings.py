"""Settings > Payments, the payment schedule template (AA-1067, item crm-08; crm-050
``crm.tenant_settings.schedule_template``): the lines a new builder version starts with. NULL = the AA default (30 %
at accept, 40 % 60 days before departure, the rest 30 days before), :data:`app.payments.schedule_plan.DEFAULT_TEMPLATE`.
Owner only to change (``PAYMENT_SETTINGS``, the route checks the role); the shape rules are
:func:`app.payments.schedule_plan.check_template`.
"""

from __future__ import annotations

import json
from typing import Any

from app.payments import schedule_plan
from app.tenancy import AnyContext, TenantContext

READ_SQL = "SELECT schedule_template FROM crm.tenant_settings"
UPSERT_SQL = """
INSERT INTO crm.tenant_settings (schedule_template) VALUES ($1::jsonb)
ON CONFLICT (tenant_id) DO UPDATE SET schedule_template = EXCLUDED.schedule_template
"""


def _view(raw: Any) -> dict[str, Any]:
    lines = json.loads(raw) if isinstance(raw, str) else raw
    return {
        "template": lines if lines else [dict(line) for line in schedule_plan.DEFAULT_TEMPLATE],
        "is_default": not lines,
        "aa_default": [dict(line) for line in schedule_plan.DEFAULT_TEMPLATE],
    }


async def read(ctx: AnyContext) -> dict[str, Any]:
    return _view(await ctx.conn.fetchval(READ_SQL))


async def update(ctx: TenantContext, template: Any) -> dict[str, Any]:
    """``template``: the lines (checked), or None to go back to the AA default."""
    lines = None if template is None else schedule_plan.check_template(template)
    await ctx.conn.execute(UPSERT_SQL, json.dumps(lines) if lines is not None else None)
    ctx.audit("update", "allowed", "tenant_settings", str(ctx.tenant_id), fields=("schedule_template",))
    return _view(lines)
