"""Duty of care (AA-961, card CRM-30; design v3 R37 "Where are my clients + incident flag", A-02 Travelling now, F9).

- :func:`travelling`: the agency's booked, pre-departure and travelling trips that overlap a window (today in the
  agency's timezone, ``days`` long), grouped by region. A trip without an end date is open-ended (a ``travelling``
  trip stays travelling until someone sets its return, design F10.1), and a ``completed`` trip counts through its
  return day (the stage job completes a trip on its end date, while the travellers may still be in the country).
  Where a trip is on a day comes from, in order: the AA line running that day (its tour's country in the AA catalog
  cache), the accepted proposal's day plan (``place`` of the day), then the trip's first destination. Travellers are
  listed **once** with every trip of theirs in the window (card E: two overlapping trips) and carry no name: only ids
  and which contact fields exist. Names, phone, e-mail and the emergency contact open through a logged reveal
  (:func:`bulk_reveal`, or the one-field reveal of the client page).
- AA incident flags (``crm.incident_flag``, written by the Booking feed): every open flag with the agency's affected
  trips (live trips with a booking line on the departure, read now: an agency without one sees nothing).
- :func:`reach`: the travellers in one region on one day, the list an agent works through by hand (no messages are
  sent from here; WhatsApp / SMS is a later module).
- :func:`bulk_reveal`: name and contact fields of up to :data:`MAX_BULK` travellers at once. Above
  :data:`REASON_THRESHOLD` travellers an explicit reason is required (card R), and every traveller opened counts in the
  member's reveals of the last hour (:mod:`app.observability.reveal_meter`): a request that would pass
  :data:`HOURLY_LIMIT` is 429 ``reveal_rate_limited`` before anything is decrypted. One ``reveal`` audit event per
  traveller (the fields opened, the reason).

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from typing import Any

from app.errors import Invalid, NotFound, RateLimited
from app.observability import reveal_meter
from app.tenancy import TenantContext
from app.vault import Vault

DEFAULT_TZ = "Asia/Ho_Chi_Minh"
MAX_DAYS = 31
LIVE_STAGES = ("booked", "pre_departure", "travelling")
# The stage job moves a travelling trip to completed on its end date: it still counts that day (see TRIPS_SQL).
SHOWN_STAGES = (*LIVE_STAGES, "completed")
MAX_TRIPS = 500
# Bulk reveal (card R): up to this many travellers a reason code is taken as "duty_of_care"; above it the member picks.
REASON_THRESHOLD = 5
MAX_BULK = 50
# Travellers a member may open in an hour, one-field reveals included (the reveal meter's alarm threshold).
HOURLY_LIMIT = reveal_meter.BULK_REVEAL_THRESHOLD
BULK_FIELDS = ("display_name", "phone", "email", "emergency_contact")
BULK_COLUMNS = {
    "display_name": "display_name_enc",
    "phone": "phone_enc",
    "email": "email_enc",
    "emergency_contact": "emergency_contact_enc",
}
SENSITIVE_FIELDS = frozenset({"emergency_contact"})
DEFAULT_REASON = "duty_of_care"
BULK_REASONS = ("duty_of_care", "incident_response", "contact_client", "ops_handoff", "client_request")
# trip.destinations codes (app.intake.stage1.DESTINATIONS) -> the country the region is named by.
DESTINATION_COUNTRY = {
    "vietnam": "VN",
    "laos": "LA",
    "cambodia": "KH",
    "thailand": "TH",
    "myanmar": "MM",
    "bhutan": "BT",
    "nepal": "NP",
    "indonesia": "ID",
}
UNKNOWN_REGION = "unknown"

TODAY_SQL = """
SELECT (now() AT TIME ZONE coalesce((SELECT timezone FROM crm.tenant_settings LIMIT 1), $1))::date AS today,
       coalesce((SELECT timezone FROM crm.tenant_settings LIMIT 1), $1) AS tz
"""
TRIPS_SQL = """
SELECT t.trip_id, t.title, t.stage, t.start_date, t.end_date, t.destinations, t.owner_member_id
  FROM crm.trip t
 WHERE t.deleted_at IS NULL AND t.stage = ANY($1::text[]) AND t.start_date IS NOT NULL
   AND t.start_date <= $3
   AND CASE WHEN t.stage = 'completed' THEN t.end_date BETWEEN $2 AND $5
            ELSE t.end_date IS NULL OR t.end_date >= $2 END
 ORDER BY t.start_date, t.trip_id
 LIMIT $4
"""
LINES_SQL = """
SELECT l.booking_line_id, l.trip_id, l.title, l.departure_id, l.service_from, l.service_to, l.supplier_kind,
       c.country_code
  FROM crm.booking_line l
  LEFT JOIN crm.aa_catalog_tour c ON c.source_system = 'cis' AND c.source_id = l.aa_tour_source_id
 WHERE l.trip_id = ANY($1::uuid[]) AND l.deleted_at IS NULL AND l.status <> 'cancelled' AND l.service_from IS NOT NULL
 ORDER BY l.service_from, l.booking_line_id
"""
DAYS_SQL = """
SELECT d.trip_id, d.day_no, d.place, d.title
  FROM crm.proposal_day d
  JOIN crm.proposal p ON p.proposal_id = d.proposal_id AND p.status = 'accepted' AND p.deleted_at IS NULL
 WHERE d.trip_id = ANY($1::uuid[]) AND d.deleted_at IS NULL AND d.day_no >= 1
"""
TRAVELLERS_SQL = """
SELECT tt.trip_id, tt.traveler_id, tt.role,
       tr.phone_enc IS NOT NULL AS has_phone, tr.email_enc IS NOT NULL AS has_email,
       tr.emergency_contact_enc IS NOT NULL AS has_emergency_contact,
       EXISTS (SELECT 1 FROM crm.traveler_health h WHERE h.traveler_id = tt.traveler_id AND h.deleted_at IS NULL)
           AS has_health_info
  FROM crm.trip_traveler tt JOIN crm.traveler tr ON tr.traveler_id = tt.traveler_id AND tr.deleted_at IS NULL
 WHERE tt.trip_id = ANY($1::uuid[]) AND tt.deleted_at IS NULL
 ORDER BY tt.traveler_id, tt.trip_id
"""
OPEN_FLAGS_SQL = """
SELECT incident_flag_id, departure_id, incident_code, raised_at
  FROM crm.incident_flag WHERE status = 'open' AND deleted_at IS NULL
 ORDER BY raised_at DESC, incident_flag_id
 LIMIT 100
"""
FLAG_TRIPS_SQL = """
SELECT DISTINCT l.departure_id, t.trip_id, t.title, t.stage, t.start_date, t.end_date
  FROM crm.booking_line l JOIN crm.trip t ON t.trip_id = l.trip_id
 WHERE l.departure_id = ANY($1::text[]) AND l.status <> 'cancelled' AND l.deleted_at IS NULL
   AND t.deleted_at IS NULL AND t.stage NOT IN ('lost', 'cancelled')
   AND (t.stage <> 'completed' OR t.end_date >= $2)
 ORDER BY l.departure_id, t.start_date, t.trip_id
"""
TRIP_FLAGS_SQL = """
SELECT f.incident_flag_id, f.departure_id, f.incident_code, f.status, f.raised_at, f.cleared_at
  FROM crm.incident_flag f
 WHERE f.deleted_at IS NULL
   AND f.departure_id IN (SELECT l.departure_id FROM crm.booking_line l
                           WHERE l.trip_id = $1 AND l.departure_id IS NOT NULL AND l.status <> 'cancelled'
                             AND l.deleted_at IS NULL)
   AND (f.status = 'open' OR f.cleared_at > now() - interval '14 days')
 ORDER BY f.status DESC, f.raised_at DESC
 LIMIT 20
"""
TRIP_SQL = "SELECT trip_id FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
BULK_SQL = """
SELECT traveler_id, display_name_enc, phone_enc, email_enc, emergency_contact_enc
  FROM crm.traveler WHERE traveler_id = ANY($1::uuid[]) AND deleted_at IS NULL
"""


def _iso(value: dt.date | dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def today(ctx: TenantContext) -> tuple[dt.date, str]:
    row = await ctx.conn.fetchrow(TODAY_SQL, DEFAULT_TZ)
    return row["today"], row["tz"]


def _window(start: dt.date, days: int) -> tuple[dt.date, dt.date]:
    if not 1 <= days <= MAX_DAYS:
        raise Invalid("invalid_days", f"1 to {MAX_DAYS} days")
    return start, start + dt.timedelta(days=days - 1)


def _covers(line: Any, day: dt.date) -> bool:
    return line["service_from"] <= day <= (line["service_to"] or line["service_from"])


def where_on(
    trip: Any, day: dt.date, lines: list[Any], plan: dict[int, Any]
) -> tuple[str, str | None, int | None, list[Any]]:
    """``(region, place, day_no, lines running that day)`` of one trip on one day (see the module docstring)."""
    running = [ln for ln in lines if _covers(ln, day)]
    day_no = (day - trip["start_date"]).days + 1 if trip["start_date"] <= day else None
    if trip["end_date"] is not None and day > trip["end_date"]:
        day_no = None  # without an end date the trip is open-ended: still away, its day count goes on
    planned = plan.get(day_no) if day_no is not None else None
    place = planned["place"] if planned is not None else None
    region = next((ln["country_code"] for ln in running if ln["country_code"]), None)
    if region is None:
        region = next(
            (DESTINATION_COUNTRY[d] for d in (trip["destinations"] or []) if d in DESTINATION_COUNTRY), UNKNOWN_REGION
        )
    return region, place, day_no, running


def _line(ln: Any) -> dict[str, Any]:
    return {
        "booking_line_id": str(ln["booking_line_id"]),
        "title": ln["title"],
        "supplier_kind": ln["supplier_kind"],
        "departure_id": ln["departure_id"],
        "service_from": _iso(ln["service_from"]),
        "service_to": _iso(ln["service_to"]),
    }


async def _open_flags(ctx: TenantContext, local_today: dt.date) -> list[dict[str, Any]]:
    flags = await ctx.conn.fetch(OPEN_FLAGS_SQL)
    if not flags:
        return []
    affected: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in await ctx.conn.fetch(FLAG_TRIPS_SQL, [f["departure_id"] for f in flags], local_today):
        affected[r["departure_id"]].append(
            {
                "trip_id": str(r["trip_id"]),
                "title": r["title"],
                "stage": r["stage"],
                "start_date": _iso(r["start_date"]),
                "end_date": _iso(r["end_date"]),
            }
        )
    out = []
    for f in flags:
        trips = affected.get(f["departure_id"], [])
        if not trips:
            continue  # Booking told this agency, but none of its live trips is on the departure any more
        out.append(
            {
                "incident_flag_id": str(f["incident_flag_id"]),
                "departure_id": f["departure_id"],
                "incident_code": f["incident_code"],
                "raised_at": _iso(f["raised_at"]),
                "trips": trips,
            }
        )
    return out


async def travelling(ctx: TenantContext, *, start: dt.date | None = None, days: int = 7) -> dict[str, Any]:
    """Travelling now / this week (A-02), grouped by region; travellers once each, without names."""
    local_today, tz = await today(ctx)
    first, last = _window(start or local_today, days)
    trips = await ctx.conn.fetch(TRIPS_SQL, list(SHOWN_STAGES), first, last, MAX_TRIPS, local_today)
    ids = [t["trip_id"] for t in trips]
    lines: dict[uuid.UUID, list[Any]] = defaultdict(list)
    plans: dict[uuid.UUID, dict[int, Any]] = defaultdict(dict)
    people: dict[uuid.UUID, list[Any]] = defaultdict(list)
    if ids:
        for ln in await ctx.conn.fetch(LINES_SQL, ids):
            lines[ln["trip_id"]].append(ln)
        for d in await ctx.conn.fetch(DAYS_SQL, ids):
            plans[d["trip_id"]][d["day_no"]] = d
        for p in await ctx.conn.fetch(TRAVELLERS_SQL, ids):
            people[p["trip_id"]].append(p)
    flags = await _open_flags(ctx, local_today)
    flagged = {t["trip_id"]: f["incident_flag_id"] for f in flags for t in f["trips"]}

    regions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    travellers: dict[uuid.UUID, dict[str, Any]] = {}
    for t in trips:
        # Where the trip is on the first day of the window it overlaps (today for a trip already under way).
        day = max(first, t["start_date"])
        region, place, day_no, running = where_on(t, day, lines[t["trip_id"]], plans[t["trip_id"]])
        party = people[t["trip_id"]]
        regions[region].append(
            {
                "trip_id": str(t["trip_id"]),
                "title": t["title"],
                "stage": t["stage"],
                "start_date": _iso(t["start_date"]),
                "end_date": _iso(t["end_date"]),
                "owner_member_id": str(t["owner_member_id"]) if t["owner_member_id"] else None,
                "is_mine": t["owner_member_id"] == ctx.member_id,
                "on_date": day.isoformat(),
                "day_no": day_no,
                "place": place,
                "lines": [_line(ln) for ln in running],
                "traveler_ids": [str(p["traveler_id"]) for p in party],
                "incident_flag_id": flagged.get(str(t["trip_id"])),
            }
        )
        for p in party:
            entry = travellers.setdefault(
                p["traveler_id"],
                {
                    "traveler_id": str(p["traveler_id"]),
                    "trip_ids": [],
                    "regions": [],
                    "has_phone": p["has_phone"],
                    "has_email": p["has_email"],
                    "has_emergency_contact": p["has_emergency_contact"],
                    "has_health_info": p["has_health_info"],
                },
            )
            entry["trip_ids"].append(str(t["trip_id"]))
            if region not in entry["regions"]:
                entry["regions"].append(region)
    groups = [
        {
            "region": region,
            "trips": items,
            "traveller_count": len({tid for item in items for tid in item["traveler_ids"]}),
        }
        for region, items in sorted(regions.items(), key=lambda kv: (kv[0] == UNKNOWN_REGION, kv[0]))
    ]
    return {
        "today": local_today.isoformat(),
        "timezone": tz,
        "from": first.isoformat(),
        "to": last.isoformat(),
        "regions": groups,
        "travellers": list(travellers.values()),
        "incidents": flags,
        "truncated": len(trips) == MAX_TRIPS,
    }


async def reach(ctx: TenantContext, *, region: str, on: dt.date | None = None) -> dict[str, Any]:
    """The travellers in ``region`` on one day (default today): ids, their trips and which contact fields exist."""
    view = await travelling(ctx, start=on, days=1)
    trips = next((g["trips"] for g in view["regions"] if g["region"] == region), [])
    wanted = {tid for t in trips for tid in t["traveler_ids"]}
    return {
        "region": region,
        "on_date": view["from"],
        "trips": trips,
        "travellers": [p for p in view["travellers"] if p["traveler_id"] in wanted],
        "incidents": [
            f for f in view["incidents"] if any(t["trip_id"] in {x["trip_id"] for x in trips} for t in f["trips"])
        ],
    }


async def trip_incidents(ctx: TenantContext, trip_id: uuid.UUID) -> dict[str, Any]:
    """The AA incident flags on the trip's departures: open ones, and those cleared in the last 14 days."""
    if await ctx.conn.fetchval(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    rows = await ctx.conn.fetch(TRIP_FLAGS_SQL, trip_id)
    return {
        "trip_id": str(trip_id),
        "incidents": [
            {
                "incident_flag_id": str(r["incident_flag_id"]),
                "departure_id": r["departure_id"],
                "incident_code": r["incident_code"],
                "status": r["status"],
                "raised_at": _iso(r["raised_at"]),
                "cleared_at": _iso(r["cleared_at"]),
            }
            for r in rows
        ],
    }


def bulk_fields(fields: list[str]) -> tuple[str, ...]:
    picked = tuple(dict.fromkeys(fields))
    if not picked or any(f not in BULK_COLUMNS for f in picked):
        raise Invalid("invalid_field", f"some of {', '.join(BULK_FIELDS)}")
    return picked


def needs_sensitive(fields: list[str]) -> bool:
    return any(f in SENSITIVE_FIELDS for f in fields)


async def bulk_reveal(
    ctx: TenantContext, vault: Vault, traveler_ids: list[uuid.UUID], fields: list[str], reason: str | None
) -> dict[str, Any]:
    """Open ``fields`` of up to :data:`MAX_BULK` travellers (card R: a reason above :data:`REASON_THRESHOLD`, the
    hourly limit). An id that is not a live traveller of the agency refuses the whole request (404)."""
    picked = bulk_fields(fields)
    ids = list(dict.fromkeys(traveler_ids))
    if not ids:
        raise Invalid("travelers_required")
    if len(ids) > MAX_BULK:
        raise Invalid("too_many_travelers", f"at most {MAX_BULK} at once", limit=MAX_BULK)
    if reason is None and len(ids) <= REASON_THRESHOLD:
        reason = DEFAULT_REASON
    if reason not in BULK_REASONS:
        raise Invalid(
            "reason_required",
            f"opening more than {REASON_THRESHOLD} travellers needs a reason",
            reasons=list(BULK_REASONS),
            threshold=REASON_THRESHOLD,
        )
    used = reveal_meter.METER.count(ctx.tenant_id, ctx.member_id)
    if used + len(ids) > HOURLY_LIMIT:
        ctx.audit("reveal", "denied", "traveler", fields=picked, reason="rate_limited", object_ids=tuple(map(str, ids)))
        raise RateLimited(
            "reveal_rate_limited",
            int(reveal_meter.WINDOW_S),
            f"{HOURLY_LIMIT} travellers an hour; {max(HOURLY_LIMIT - used, 0)} left",
        )
    rows = {r["traveler_id"]: r for r in await ctx.conn.fetch(BULK_SQL, ids)}
    if len(rows) != len(ids):
        raise NotFound("traveler_not_found")
    out = []
    for tid in ids:
        row = rows[tid]
        values: dict[str, Any] = {}
        for f in picked:
            column = BULK_COLUMNS[f]
            blob = row[column]
            values[f] = (
                await vault.decrypt("traveler", column, tid, blob, tenant_id=ctx.tenant_id)
                if blob is not None
                else None
            )
        ctx.audit("reveal", "allowed", "traveler", str(tid), fields=picked, reason=reason)
        out.append({"traveler_id": str(tid), **values})
    return {"reason": reason, "fields": list(picked), "travellers": out}
