"""The agency's lifecycle settings and legal holds, and archiving a client (AA-942; design §3.5, §3.6, R23).

- **Settings** (owner): ``trash_days`` 1-30 (default 7), ``auto_archive_months`` 12-120 (default 36, est.).
- **Legal holds** (owner places and releases the agency's; a hold AA placed, actor ``platform:*``, is released only
  by AA, crm-010 trigger). While active, nothing that reaches the subject is deleted (``crm.erase_subject`` refuses,
  the daily run skips and counts it).
- **Archive** (R23): hidden from lists, search and group sends; every automated send is stopped at the dispatcher
  (``app.dispatch``); history kept. Owner and agents archive; a client archived after inactivity is
  ``auto_inactive`` (the daily run), and the owner then decides whether to delete it (Settings > Data and privacy).
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.lifecycle import rules, window
from app.tenancy import TenantContext

SETTINGS_SQL = "SELECT trash_days, auto_archive_months FROM crm.tenant_settings WHERE tenant_id = $1"
UPSERT_SQL = """
INSERT INTO crm.tenant_settings (tenant_id, trash_days, auto_archive_months)
VALUES ($1, coalesce($2, 7), coalesce($3, 36))
ON CONFLICT (tenant_id) DO UPDATE
   SET trash_days = coalesce($2, crm.tenant_settings.trash_days),
       auto_archive_months = coalesce($3, crm.tenant_settings.auto_archive_months)
RETURNING trash_days, auto_archive_months
"""
HOLDS_SQL = """
SELECT hold_id, scope, subject_id, reason_code, status, placed_by, created_at, released_at, released_by
  FROM crm.legal_hold WHERE deleted_at IS NULL ORDER BY (status = 'active') DESC, created_at DESC LIMIT 100
"""
PLACE_SQL = """
INSERT INTO crm.legal_hold (scope, subject_id, reason_code) VALUES ($1, $2, $3)
RETURNING hold_id, scope, subject_id, reason_code, status, placed_by, created_at, released_at, released_by
"""
RELEASE_SQL = """
UPDATE crm.legal_hold SET status = 'released', released_at = now(), released_by = $2
 WHERE hold_id = $1 AND status = 'active'
RETURNING hold_id, scope, subject_id, reason_code, status, placed_by, created_at, released_at, released_by
"""
SUBJECT_SQL = {
    "trip": "SELECT 1 FROM crm.trip WHERE trip_id = $1",
    "traveler": "SELECT 1 FROM crm.traveler WHERE traveler_id = $1",
}
ACTIVE_HOLD_SQL = "SELECT crm.legal_hold_blocks($1, $2)"
ARCHIVE_SQL = """
UPDATE crm.traveler SET archived_at = now(), archive_reason = $2, archived_by = $3
 WHERE traveler_id = $1 AND deleted_at IS NULL AND archived_at IS NULL
RETURNING archived_at
"""
UNARCHIVE_SQL = """
UPDATE crm.traveler SET archived_at = NULL, archive_reason = NULL, archived_by = NULL
 WHERE traveler_id = $1 AND deleted_at IS NULL AND archived_at IS NOT NULL
RETURNING traveler_id
"""
TRAVELER_SQL = "SELECT archived_at FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL"


def _owner(ctx: TenantContext, action: str, kind: str, object_id: str | None = None) -> None:
    if ctx.member_role != "owner":
        ctx.audit(action, "denied", kind, object_id, reason="owner_only")
        raise Forbidden("owner_only", "only the agency owner changes this")


async def settings(ctx: TenantContext) -> dict[str, int]:
    row = await ctx.conn.fetchrow(SETTINGS_SQL, ctx.tenant_id)
    return {
        "trash_days": int(row["trash_days"]) if row else window.DEFAULT_TRASH_DAYS,
        "auto_archive_months": int(row["auto_archive_months"]) if row else rules.DEFAULT_AUTO_ARCHIVE_MONTHS,
    }


async def update_settings(ctx: TenantContext, *, trash_days: int | None, auto_archive_months: int | None) -> dict:
    _owner(ctx, "update", "tenant_settings")
    if trash_days is not None and not rules.MIN_TRASH_DAYS <= trash_days <= rules.MAX_TRASH_DAYS:
        raise Invalid("invalid_trash_days", f"{rules.MIN_TRASH_DAYS} to {rules.MAX_TRASH_DAYS} days")
    if auto_archive_months is not None and not (
        rules.MIN_AUTO_ARCHIVE_MONTHS <= auto_archive_months <= rules.MAX_AUTO_ARCHIVE_MONTHS
    ):
        raise Invalid(
            "invalid_auto_archive_months", f"{rules.MIN_AUTO_ARCHIVE_MONTHS} to {rules.MAX_AUTO_ARCHIVE_MONTHS} months"
        )
    row = await ctx.conn.fetchrow(UPSERT_SQL, ctx.tenant_id, trash_days, auto_archive_months)
    changed = tuple(f for f, v in (("trash_days", trash_days), ("auto_archive_months", auto_archive_months)) if v)
    ctx.audit("update", "allowed", "tenant_settings", fields=changed)
    return {"trash_days": int(row["trash_days"]), "auto_archive_months": int(row["auto_archive_months"])}


def _hold_view(r: Any) -> dict[str, Any]:
    return {
        "hold_id": str(r["hold_id"]),
        "scope": r["scope"],
        "subject_id": str(r["subject_id"]) if r["subject_id"] else None,
        "reason_code": r["reason_code"],
        "status": r["status"],
        "placed_by_aa": r["placed_by"].startswith("platform:"),
        "created_at": r["created_at"].isoformat(),
        "released_at": r["released_at"].isoformat() if r["released_at"] else None,
    }


async def holds(ctx: TenantContext) -> list[dict[str, Any]]:
    return [_hold_view(r) for r in await ctx.conn.fetch(HOLDS_SQL)]


async def place_hold(ctx: TenantContext, scope: str, subject_id: uuid.UUID | None, reason_code: str) -> dict:
    _owner(ctx, "create", "legal_hold")
    if scope not in rules.HOLD_SCOPES:
        raise Invalid("invalid_scope", ", ".join(rules.HOLD_SCOPES))
    if reason_code not in rules.HOLD_REASONS:
        raise Invalid("invalid_reason", ", ".join(rules.HOLD_REASONS))
    if (scope == "tenant") != (subject_id is None):
        raise Invalid("invalid_subject", "an agency hold names no subject; a trip or client hold names one")
    if subject_id is not None and not await ctx.conn.fetchval(SUBJECT_SQL[scope], subject_id):
        raise NotFound(f"{scope}_not_found")
    try:
        async with ctx.conn.transaction():
            row = await ctx.conn.fetchrow(PLACE_SQL, scope, subject_id, reason_code)
    except asyncpg.UniqueViolationError:
        raise Conflict("hold_exists", "this subject is already on hold") from None
    ctx.audit("create", "allowed", "legal_hold", str(row["hold_id"]), reason=reason_code)
    return _hold_view(row)


async def release_hold(ctx: TenantContext, hold_id: uuid.UUID) -> dict:
    _owner(ctx, "update", "legal_hold", str(hold_id))
    try:
        async with ctx.conn.transaction():
            row = await ctx.conn.fetchrow(RELEASE_SQL, hold_id, f"member:{ctx.member_id}")
    except asyncpg.PostgresError as exc:
        if getattr(exc, "sqlstate", None) == "CRM17":
            ctx.audit("update", "denied", "legal_hold", str(hold_id), reason="hold_platform_only")
            raise Forbidden("hold_platform_only", "AA placed this hold; AA releases it") from None
        raise
    if row is None:
        raise NotFound("hold_not_found")
    ctx.audit("update", "allowed", "legal_hold", str(hold_id), reason="released")
    return _hold_view(row)


async def held(ctx: TenantContext, scope: str, subject_id: uuid.UUID | None) -> bool:
    return bool(await ctx.conn.fetchval(ACTIVE_HOLD_SQL, scope, subject_id))


async def archive(ctx: TenantContext, traveler_id: uuid.UUID, reason: str) -> dict[str, Any]:
    if ctx.member_role not in ("owner", "agent"):
        ctx.audit("archive", "denied", "traveler", str(traveler_id), reason="not_allowed_for_role")
        raise Forbidden("not_allowed_for_role", "owners and agents archive clients")
    if reason not in rules.ARCHIVE_REASONS or reason == "auto_inactive":
        raise Invalid("invalid_reason", ", ".join(r for r in rules.ARCHIVE_REASONS if r != "auto_inactive"))
    archived_at = await ctx.conn.fetchval(ARCHIVE_SQL, traveler_id, reason, f"member:{ctx.member_id}")
    if archived_at is None:
        if await ctx.conn.fetchrow(TRAVELER_SQL, traveler_id) is None:
            raise NotFound("traveler_not_found")
        raise Conflict("already_archived")
    ctx.audit("archive", "allowed", "traveler", str(traveler_id), reason=reason)
    return {"traveler_id": str(traveler_id), "archived_at": archived_at.isoformat(), "archive_reason": reason}


async def unarchive(ctx: TenantContext, traveler_id: uuid.UUID) -> dict[str, Any]:
    if ctx.member_role not in ("owner", "agent"):
        ctx.audit("unarchive", "denied", "traveler", str(traveler_id), reason="not_allowed_for_role")
        raise Forbidden("not_allowed_for_role", "owners and agents archive clients")
    if await ctx.conn.fetchval(UNARCHIVE_SQL, traveler_id) is None:
        raise NotFound("not_archived")
    ctx.audit("unarchive", "allowed", "traveler", str(traveler_id))
    return {"traveler_id": str(traveler_id), "archived_at": None}
