"""What a live AA support / break-glass session may read (AA-971, design v3 §4.8 L2 / L3, S-04 "Agency UI, read
only; F1 masked unless the owner ticked show identity").

Every function opens :func:`app.tenancy.support_tx` (role ``crm_support_reader``: named columns of ``crm.tenant``,
``crm.tenant_settings``, ``crm.trip`` and ``crm.trip_stage_history`` only, no write privilege; crm-033), so a revoked
grant or a passed window refuses the very next call, and writes one ``support_read`` event in the agency's audit per
call (the reason names the view; a list names the ids it showed, design §4.4). Queries name their columns: the reader
has no ``SELECT *`` (a trip title, a mail token or the data-request contact are not granted at all).

The party of a trip (names) is shown only when the session shows identity (a support session whose owner ticked it;
never break-glass, which has no MFA step-up in the app and skips the owner's consent: crm-033 CHECK):
:func:`app.tenancy.as_login_role` steps out of the reader for that one read, the display names are opened with the
agency's vault key, and one ``reveal`` event names the travellers and the field. Never anything else of a traveller.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import asyncpg

from app.auth.route_guard import Caller
from app.errors import Invalid, NotFound
from app.tenancy import SupportContext, as_login_role, support_tx
from app.vault import KeyProvider, Vault

MAX_PAGE = 100
STAGES = (
    "inquiry",
    "qualifying",
    "proposal",
    "booked",
    "pre_departure",
    "travelling",
    "completed",
    "lost",
    "cancelled",
)

TENANT_SQL = "SELECT tenant_id, status, display_name, default_locale FROM crm.tenant WHERE tenant_id = $1"
SETTINGS_SQL = """
SELECT ai_enabled, ai_budget_usd, timezone, base_currency, booking_validation_level, fx_mode, trash_days,
       auto_archive_months, stage2_auto_send, invoice_due_days, service_fee_enabled, onboarding_later, onboarding_done
  FROM crm.tenant_settings WHERE deleted_at IS NULL
"""
PIPELINE_SQL = """
SELECT stage, count(*)::int AS n FROM crm.trip WHERE deleted_at IS NULL GROUP BY stage
"""
TRIP_COLUMNS = (
    "trip_id, stage, stage_entered_at, waiting_on, start_date, end_date, month, nights, destinations, party_adults, "
    "party_child_ages, party_type, owner_member_id, lead_source, lost_reason_code, cancel_reason_code, "
    "last_inquiry_at, last_activity_at, created_at"
)
TRIPS_SQL = f"""
SELECT {TRIP_COLUMNS} FROM crm.trip
 WHERE deleted_at IS NULL AND ($1::text IS NULL OR stage = $1)
   AND ($2::timestamptz IS NULL OR (last_activity_at, trip_id) < ($2, $3::uuid))
 ORDER BY last_activity_at DESC, trip_id DESC
 LIMIT $4
"""  # noqa: S608 — fixed column list, no input
TRIP_SQL = f"SELECT {TRIP_COLUMNS} FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"  # noqa: S608
HISTORY_SQL = """
SELECT history_id, from_stage, to_stage, reason_code, override, override_reason_code, at, created_by
  FROM crm.trip_stage_history WHERE trip_id = $1 ORDER BY at, history_id
"""
# Run as crm_app inside as_login_role (F1): ids, the role on the trip and the sealed display name only.
PARTY_SQL = """
SELECT tt.traveler_id, tt.role, t.display_name_enc
  FROM crm.trip_traveler tt JOIN crm.traveler t ON t.traveler_id = tt.traveler_id
 WHERE tt.trip_id = $1 AND tt.deleted_at IS NULL AND t.deleted_at IS NULL
 ORDER BY (tt.role = 'lead') DESC, tt.created_at, tt.traveler_id
"""


def _iso(value: dt.datetime | dt.date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _session(ctx: SupportContext) -> dict[str, Any]:
    return {
        "kind": ctx.kind,
        "session_id": str(ctx.session_id),
        "show_identity": ctx.show_identity,
        "expires_at": _iso(ctx.expires_at),
    }


def _trip(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "trip_id": str(r["trip_id"]),
        "stage": r["stage"],
        "stage_entered_at": _iso(r["stage_entered_at"]),
        "waiting_on": r["waiting_on"],
        "start_date": _iso(r["start_date"]),
        "end_date": _iso(r["end_date"]),
        "month": _iso(r["month"]),
        "nights": r["nights"],
        "destinations": list(r["destinations"] or ()),
        "party_adults": r["party_adults"],
        "party_children": len(r["party_child_ages"] or ()),
        "party_type": r["party_type"],
        "owner_member_id": str(r["owner_member_id"]) if r["owner_member_id"] else None,
        "lead_source": r["lead_source"],
        "lost_reason_code": r["lost_reason_code"],
        "cancel_reason_code": r["cancel_reason_code"],
        "last_inquiry_at": _iso(r["last_inquiry_at"]),
        "last_activity_at": _iso(r["last_activity_at"]),
        "created_at": _iso(r["created_at"]),
    }


async def overview(pool: asyncpg.Pool, caller: Caller, tenant_id: uuid.UUID) -> dict[str, Any]:
    async with support_tx(pool, caller, tenant_id) as ctx:
        tenant = await ctx.conn.fetchrow(TENANT_SQL, tenant_id)
        settings = await ctx.conn.fetchrow(SETTINGS_SQL)
        pipeline = {r["stage"]: r["n"] for r in await ctx.conn.fetch(PIPELINE_SQL)}
        ctx.audit("support_read", "allowed", "tenant", str(tenant_id), reason="overview")
        return {
            "tenant_id": str(tenant_id),
            "agency_name": tenant["display_name"],
            "status": tenant["status"],
            "default_locale": tenant["default_locale"],
            "settings": (
                {
                    "ai_enabled": settings["ai_enabled"],
                    "ai_budget_usd": str(settings["ai_budget_usd"]),
                    "timezone": settings["timezone"],
                    "base_currency": settings["base_currency"],
                    "booking_validation_level": settings["booking_validation_level"],
                    "fx_mode": settings["fx_mode"],
                    "trash_days": settings["trash_days"],
                    "auto_archive_months": settings["auto_archive_months"],
                    "stage2_auto_send": settings["stage2_auto_send"],
                    "invoice_due_days": settings["invoice_due_days"],
                    "service_fee_enabled": settings["service_fee_enabled"],
                    "onboarding_later": list(settings["onboarding_later"] or ()),
                    "onboarding_done": list(settings["onboarding_done"] or ()),
                }
                if settings is not None
                else None
            ),
            "pipeline": {stage: pipeline.get(stage, 0) for stage in STAGES},
            "session": _session(ctx),
        }


def parse_cursor(value: str | None) -> tuple[dt.datetime | None, uuid.UUID | None]:
    """``<last_activity_at ISO>|<trip_id>`` of the last row of the previous page."""
    if not value:
        return None, None
    try:
        at, trip = value.split("|", 1)
        parsed = dt.datetime.fromisoformat(at)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)), uuid.UUID(trip)
    except ValueError as exc:
        raise Invalid("invalid_cursor") from exc


async def trips(
    pool: asyncpg.Pool, caller: Caller, tenant_id: uuid.UUID, *, stage: str | None, cursor: str | None, limit: int
) -> dict[str, Any]:
    if stage is not None and stage not in STAGES:
        raise Invalid("invalid_stage", f"one of {', '.join(STAGES)}")
    limit = max(1, min(limit, MAX_PAGE))
    after_at, after_id = parse_cursor(cursor)
    async with support_tx(pool, caller, tenant_id) as ctx:
        rows = await ctx.conn.fetch(TRIPS_SQL, stage, after_at, after_id, limit + 1)
        page, more = rows[:limit], len(rows) > limit
        ctx.audit(
            "support_read",
            "allowed",
            "trip",
            reason="trips",
            object_ids=tuple(str(r["trip_id"]) for r in page),
        )
        last = page[-1] if page and more else None
        return {
            "items": [_trip(r) for r in page],
            "next_cursor": f"{last['last_activity_at'].isoformat()}|{last['trip_id']}" if last else None,
            "session": _session(ctx),
        }


async def trip(
    pool: asyncpg.Pool, caller: Caller, tenant_id: uuid.UUID, trip_id: uuid.UUID, *, keys: KeyProvider | None
) -> dict[str, Any]:
    async with support_tx(pool, caller, tenant_id) as ctx:
        row = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
        if row is None:
            raise NotFound("trip_not_found")
        history = await ctx.conn.fetch(HISTORY_SQL, trip_id)
        ctx.audit("support_read", "allowed", "trip", str(trip_id), reason="trip")
        party: list[dict[str, Any]] | None = None
        if ctx.show_identity and keys is not None:
            party = await _party(ctx, keys, trip_id)
        return {
            **_trip(row),
            "stage_history": [
                {
                    "history_id": str(h["history_id"]),
                    "from_stage": h["from_stage"],
                    "to_stage": h["to_stage"],
                    "reason_code": h["reason_code"],
                    "override": h["override"],
                    "override_reason_code": h["override_reason_code"],
                    "at": _iso(h["at"]),
                    "by": h["created_by"],
                }
                for h in history
            ],
            # None = names hidden (the owner did not tick "show identity"); a list = the party, each name audited.
            "party": party,
            "identity_hidden": party is None,
            "session": _session(ctx),
        }


async def _party(ctx: SupportContext, keys: KeyProvider, trip_id: uuid.UUID) -> list[dict[str, Any]]:
    vault = Vault(keys, ctx)  # type: ignore[arg-type] — same shape as a TenantContext (conn, tenant, DEK memos, audit)
    out: list[dict[str, Any]] = []
    async with as_login_role(ctx):
        for r in await ctx.conn.fetch(PARTY_SQL, trip_id):
            name = await vault.decrypt(
                "traveler", "display_name_enc", r["traveler_id"], r["display_name_enc"], tenant_id=ctx.tenant_id
            )
            out.append({"traveler_id": str(r["traveler_id"]), "role": r["role"], "display_name": name})
    if out:
        ctx.audit(
            "reveal",
            "allowed",
            "traveler",
            fields=("display_name",),
            reason=ctx.kind,
            object_ids=tuple(p["traveler_id"] for p in out),
        )
    return out
