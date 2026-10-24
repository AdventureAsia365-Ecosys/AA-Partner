"""Settings > Forms, the CRM-15 part (AA-943): the agency's timezone (the date rule's "today", call-slot windows),
Stage 2 auto-send and the call-slot rules the inquiry form offers (R15). Owner only (``SETTINGS_FORMS_MANAGE``).

``crm.tenant_settings`` has one row per agency (crm-005; an agency without one runs on the defaults). The database
checks the shapes (crm-012 ``tenant_settings_timezone_chk``, ``crm.call_slot_rules_ok``); this module answers 422 with
a field code first and checks the timezone against ``pg_timezone_names``.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from app.errors import Invalid
from app.tenancy import AnyContext, TenantContext

DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"
READ_SQL = "SELECT timezone, stage2_auto_send, call_slot_rules FROM crm.tenant_settings"
ZONE_SQL = "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name = $1)"
UPSERT_SQL = """
INSERT INTO crm.tenant_settings (timezone, stage2_auto_send, call_slot_rules) VALUES ($1, $2, $3::jsonb)
ON CONFLICT (tenant_id) DO UPDATE
   SET timezone = EXCLUDED.timezone, stage2_auto_send = EXCLUDED.stage2_auto_send,
       call_slot_rules = EXCLUDED.call_slot_rules
"""


async def read(ctx: AnyContext) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(READ_SQL)
    if row is None:
        return {"timezone": DEFAULT_TIMEZONE, "stage2_auto_send": False, "call_slot_rules": {}}
    rules = row["call_slot_rules"]
    return {
        "timezone": row["timezone"],
        "stage2_auto_send": row["stage2_auto_send"],
        "call_slot_rules": json.loads(rules) if isinstance(rules, str) else dict(rules or {}),
    }


async def update(ctx: TenantContext, changes: dict[str, Any]) -> dict[str, Any]:
    current = await read(ctx)
    merged = {**current, **{k: v for k, v in changes.items() if k in current}}
    if not isinstance(merged["stage2_auto_send"], bool):
        raise Invalid("invalid_stage2_auto_send", "true or false")
    zone = merged["timezone"]
    if not isinstance(zone, str) or not await ctx.conn.fetchval(ZONE_SQL, zone):
        raise Invalid("invalid_timezone", "an IANA timezone name, e.g. Asia/Ho_Chi_Minh")
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(UPSERT_SQL, zone, merged["stage2_auto_send"], json.dumps(merged["call_slot_rules"]))
    except asyncpg.CheckViolationError:
        raise Invalid(
            "invalid_call_slot_rules",
            "windows (day 1-7, start < end, HH:MM), duration_min 10-240, horizon_days 1-60",
        ) from None
    ctx.audit("update", "allowed", "tenant_settings", None, fields=tuple(sorted(changes)))
    return await read(ctx)
