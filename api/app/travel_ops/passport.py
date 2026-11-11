"""The passport check against the trip (AA-1070 item crm-15; crm-055 ``crm.tenant_settings.passport_valid_months``).

A traveller's passport is good for a trip when it is still valid on the trip's end date plus the agency's
``passport_valid_months`` (default 6, the common entry rule of the region; 0..24). Per traveller on the trip:

- ``ok``: the expiry on file is on or after that day;
- ``expires_too_soon``: it is before that day (a **passport issue**: the badge, the pipeline filter, the workflow
  event);
- ``missing``: no expiry on file (the Stage 3 progress already counts it as a gap);
- ``no_trip_end``: the trip has no end date yet (nothing to check against).

The expiry is opened server-side only; the API answers the status and the day the passport must reach, never the
expiry itself. Nothing is stored: the check runs when a page asks, so a passport or date change shows at once.

The hourly workflow tick (:func:`booked_events`) fires the trip event ``passport.invalid_for_trip`` for a trip that
entered Booked in the last :data:`EVENT_WINDOW_DAYS` days with a passport issue, once per entry into Booked (the
AA library template ``passport_invalid_for_trip`` opens an urgent task on it).

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import calendar
import datetime as dt
import uuid
from collections import Counter
from typing import Any

from app.errors import Invalid
from app.tenancy import AnyContext
from app.vault import Vault
from app.vault.errors import DecryptionDenied

DEFAULT_MONTHS = 6
STATUSES = ("ok", "expires_too_soon", "missing", "no_trip_end")
ISSUE = "expires_too_soon"
# The stages where a passport issue is shown on the board (a lead has no trip end worth checking yet).
CHECK_STAGES = ("booked", "pre_departure", "travelling")
EVENT_NAME = "passport.invalid_for_trip"
EVENT_WINDOW_DAYS = 3
MAX_TRAVELLERS = 2000
_EVENT_NS = uuid.UUID("6f0c9a5e-1070-4a35-9a8b-5a1e5f0a1070")

MONTHS_SQL = "SELECT passport_valid_months FROM crm.tenant_settings"
MONTHS_UPSERT_SQL = """
INSERT INTO crm.tenant_settings (passport_valid_months) VALUES ($1)
ON CONFLICT (tenant_id) DO UPDATE SET passport_valid_months = EXCLUDED.passport_valid_months
"""
MAX_MONTHS = 24
PARTY_SQL = """
SELECT pp.trip_id, pp.traveler_id, pp.role, t.passport_expiry_enc, tr.end_date
  FROM crm.trip_traveler pp
  JOIN crm.traveler t ON t.traveler_id = pp.traveler_id AND t.deleted_at IS NULL
  JOIN crm.trip tr ON tr.trip_id = pp.trip_id AND tr.deleted_at IS NULL
 WHERE pp.trip_id = ANY($1::uuid[]) AND pp.deleted_at IS NULL
 ORDER BY pp.trip_id, pp.role = 'lead' DESC, pp.created_at, pp.traveler_id
 LIMIT $2
"""
STAGE_TRIPS_SQL = """
SELECT trip_id FROM crm.trip
 WHERE stage = ANY($1::text[]) AND deleted_at IS NULL AND end_date IS NOT NULL
 ORDER BY start_date NULLS LAST, trip_id
 LIMIT $2
"""
BOOKED_SQL = """
SELECT trip_id, stage_entered_at FROM crm.trip
 WHERE stage = 'booked' AND deleted_at IS NULL AND end_date IS NOT NULL
   AND stage_entered_at > now() - make_interval(days => $1)
 ORDER BY stage_entered_at, trip_id
 LIMIT $2
"""


def add_months(day: dt.date, months: int) -> dt.date:
    """``day`` plus ``months`` calendar months; a day past the month's end clamps to its last day."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def needed_until(end_date: dt.date | None, months: int) -> dt.date | None:
    return add_months(end_date, months) if end_date is not None else None


def status(expiry: dt.date | None, end_date: dt.date | None, months: int) -> str:
    if end_date is None:
        return "no_trip_end"
    if expiry is None:
        return "missing"
    return "ok" if expiry >= add_months(end_date, months) else ISSUE


async def valid_months(ctx: AnyContext) -> int:
    value = await ctx.conn.fetchval(MONTHS_SQL)
    return int(value) if value is not None else DEFAULT_MONTHS


async def settings_view(ctx: AnyContext) -> dict[str, Any]:
    return {"passport_valid_months": await valid_months(ctx), "default": DEFAULT_MONTHS, "max": MAX_MONTHS}


async def update_settings(ctx: AnyContext, months: Any) -> dict[str, Any]:
    """Settings > Travel (owner): how many months a passport must outlast the trip."""
    if isinstance(months, bool) or not isinstance(months, int) or not 0 <= months <= MAX_MONTHS:
        raise Invalid("invalid_passport_valid_months", f"0 to {MAX_MONTHS}")
    await ctx.conn.execute(MONTHS_UPSERT_SQL, months)
    ctx.audit("update", "allowed", "tenant_settings", None, fields=("passport_valid_months",))
    return await settings_view(ctx)


async def _expiry(vault: Vault, row: Any) -> dt.date | None:
    blob = row["passport_expiry_enc"]
    if blob is None:
        return None
    try:
        return dt.date.fromisoformat(await vault.decrypt("traveler", "passport_expiry_enc", row["traveler_id"], blob))
    except (ValueError, DecryptionDenied):
        return None  # an unreadable value reads as not on file (the decrypt refusal is audited by the vault)


async def checks(ctx: AnyContext, vault: Vault, trip_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Per trip, the status of each traveller (lead first): ``{traveler_id, role, status}``."""
    if not trip_ids:
        return {}
    months = await valid_months(ctx)
    out: dict[uuid.UUID, list[dict[str, Any]]] = {t: [] for t in trip_ids}
    for row in await ctx.conn.fetch(PARTY_SQL, list(trip_ids), MAX_TRAVELLERS):
        expiry = await _expiry(vault, row)
        out.setdefault(row["trip_id"], []).append(
            {
                "traveler_id": str(row["traveler_id"]),
                "role": row["role"],
                "status": status(expiry, row["end_date"], months),
            }
        )
    return out


async def trip_view(ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, end_date: dt.date | None) -> dict[str, Any]:
    """The trip party's passport check: the rule, the day passports must reach, each traveller's status."""
    months = await valid_months(ctx)
    travellers = (await checks(ctx, vault, [trip_id])).get(trip_id, [])
    until = needed_until(end_date, months)
    return {
        "valid_months": months,
        "valid_until_needed": until.isoformat() if until else None,
        "issues": sum(1 for t in travellers if t["status"] == ISSUE),
        "travellers": travellers,
    }


async def issue_counts(ctx: AnyContext, vault: Vault, trip_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """How many travellers of each trip have a passport issue (trips without one are left out)."""
    found = await checks(ctx, vault, trip_ids)
    counts = {t: sum(1 for p in people if p["status"] == ISSUE) for t, people in found.items()}
    return {t: n for t, n in counts.items() if n}


async def issue_trips(ctx: AnyContext, vault: Vault, limit: int = 500) -> dict[uuid.UUID, int]:
    """The trips of :data:`CHECK_STAGES` with a passport issue (the pipeline filter)."""
    trips = [r["trip_id"] for r in await ctx.conn.fetch(STAGE_TRIPS_SQL, list(CHECK_STAGES), limit)]
    return await issue_counts(ctx, vault, trips)


def event_id(trip_id: uuid.UUID, entered_at: dt.datetime) -> uuid.UUID:
    """The event id of one entry into Booked: the same entry never starts a rule twice."""
    return uuid.uuid5(_EVENT_NS, f"{trip_id}:{entered_at.isoformat()}")


async def booked_events(ctx: AnyContext, vault: Vault, fire: Any, limit: int = 200) -> Counter[str]:
    """Fire ``passport.invalid_for_trip`` (``fire(ctx, trip_id, name, event_id)``, the workflow trigger) for each
    trip that entered Booked in the last :data:`EVENT_WINDOW_DAYS` days and has a passport issue."""
    counts: Counter[str] = Counter()
    rows = await ctx.conn.fetch(BOOKED_SQL, EVENT_WINDOW_DAYS, limit)
    if not rows:
        return counts
    issues = await issue_counts(ctx, vault, [r["trip_id"] for r in rows])
    for r in rows:
        if r["trip_id"] in issues:
            counts["passport_events"] += 1
            counts["passport_runs_started"] += await fire(
                ctx, r["trip_id"], EVENT_NAME, event_id(r["trip_id"], r["stage_entered_at"])
            )
    return counts
