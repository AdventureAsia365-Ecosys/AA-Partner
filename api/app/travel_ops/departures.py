"""The departure roster (AA-1070 item crm-22; crm-054 ``crm.aa_departure_state``): every traveller the agency has on
one Adventure Asia departure, across its trips.

- **The list** (:func:`roster_list`): one row per AA departure id of the agency's live AA lines
  (``crm.booking_line.departure_id``, not cancelled, on a trip that is not lost or cancelled), with its dates, the
  trips on it, pax by type (adults; children 2-17 and infants under 2 from the trips' party ages), the state the
  Booking feed sent (``scheduled`` until an event comes: ``guaranteed`` / ``cancelled``; :func:`apply_event`) and the
  Stage 3 progress (trips at 100 %).
- **One departure** (:func:`roster`): the trips (stage, pax, Stage 3, passport issues, rooms placed) and their
  travellers (name, role, Stage 3 done, passport check, room). Names are the party's; one ``list_view`` audit event.
- **The merged manifest** (:func:`manifest`) for the agency's own tour leader: the fields the member picks
  (:data:`MANIFEST_FIELDS`) of every traveller on the departure, from the same build as the AA ops release
  (:func:`app.ops_release.build.document`: dietary and mobility codes only with the traveller's health consent), plus
  the room and the passport check; watermarked, never stored. It is a bulk reveal (card R, like
  :func:`app.duty.service.bulk_reveal`): a reason from :data:`app.duty.service.BULK_REASONS` is required above
  :data:`app.duty.service.REASON_THRESHOLD` travellers or when a health field (dietary, mobility) is picked; the
  member's hourly reveal limit (:data:`app.duty.service.HOURLY_LIMIT`, the reveal meter) is checked before anything is
  opened (429 ``reveal_rate_limited`` with a denied ``reveal`` audit event); one ``reveal`` audit event per traveller
  released (the reveal meter counts them) and one ``report_export`` audit event naming the trips.
- **The feed** (:func:`apply_event`, called by :mod:`app.booking.sync`): ``departure.guaranteed`` /
  ``departure.cancelled`` upsert the agency's ``crm.aa_departure_state`` row (written by the job only, CRM53); an
  event older than the stored one changes nothing.

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import secrets
import uuid
from typing import Any

from app.duty import service as duty
from app.errors import Invalid, NotFound, RateLimited
from app.observability import reveal_meter
from app.ops_release import build
from app.tenancy import AnyContext, TenantContext
from app.travel_ops import passport, rooming
from app.vault import Vault

REF = re.compile(r"^[A-Za-z0-9:_.-]{1,80}$")
SCOPES = ("upcoming", "past", "all")
STATES = ("scheduled", "guaranteed", "cancelled")
LIVE_STAGES_EXCLUDED = ("lost", "cancelled")
MANIFEST_FIELDS = ("name", "trip_ref", "age_band", "room", "passport_check", "dietary", "mobility", "arrival_flight",
                   "departure_flight")  # fmt: skip
MANIFEST_DEFAULT = ("name", "trip_ref", "age_band", "room")
MAX_DEPARTURES = 200
MAX_TRIPS = 100
WATERMARK_PREFIX = "AADEP"
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_AUDIT_ID = re.compile(r"^[A-Za-z0-9:_-]{1,80}$")

LIST_SQL = """
SELECT l.departure_id, min(l.title) AS title, min(l.service_from) AS service_from,
       max(coalesce(l.service_to, l.service_from)) AS service_to,
       array_agg(DISTINCT l.trip_id) AS trip_ids,
       s.departure_status, s.guaranteed_at, s.cancelled_at, s.min_to_operate
  FROM crm.booking_line l
  JOIN crm.trip t ON t.trip_id = l.trip_id AND t.deleted_at IS NULL AND NOT (t.stage = ANY($2::text[]))
  LEFT JOIN crm.aa_departure_state s ON s.departure_id = l.departure_id AND s.deleted_at IS NULL
 WHERE l.supplier_kind = 'aa' AND l.departure_id IS NOT NULL AND l.status <> 'cancelled' AND l.deleted_at IS NULL
 GROUP BY l.departure_id, s.departure_status, s.guaranteed_at, s.cancelled_at, s.min_to_operate
HAVING $1::text = 'all'
    OR ($1::text = 'upcoming' AND coalesce(max(coalesce(l.service_to, l.service_from)), $3::date) >= $3::date)
    OR ($1::text = 'past' AND max(coalesce(l.service_to, l.service_from)) < $3::date)
 ORDER BY min(l.service_from) NULLS LAST, l.departure_id
 LIMIT $4
"""
ONE_SQL = """
SELECT l.departure_id, min(l.title) AS title, min(l.service_from) AS service_from,
       max(coalesce(l.service_to, l.service_from)) AS service_to,
       array_agg(DISTINCT l.trip_id) AS trip_ids,
       s.departure_status, s.guaranteed_at, s.cancelled_at, s.min_to_operate
  FROM crm.booking_line l
  JOIN crm.trip t ON t.trip_id = l.trip_id AND t.deleted_at IS NULL AND NOT (t.stage = ANY($2::text[]))
  LEFT JOIN crm.aa_departure_state s ON s.departure_id = l.departure_id AND s.deleted_at IS NULL
 WHERE l.supplier_kind = 'aa' AND l.departure_id = $1 AND l.status <> 'cancelled' AND l.deleted_at IS NULL
 GROUP BY l.departure_id, s.departure_status, s.guaranteed_at, s.cancelled_at, s.min_to_operate
"""
TRIPS_SQL = """
SELECT trip_id, title, stage, start_date, end_date, party_adults, party_child_ages, owner_member_id,
       coalesce((intake_completeness ->> 's3')::int, 0) AS s3
  FROM crm.trip WHERE trip_id = ANY($1::uuid[]) AND deleted_at IS NULL
 ORDER BY start_date NULLS LAST, trip_id
 LIMIT $2
"""
PARTY_SQL = """
SELECT tt.trip_id, tt.traveler_id, tt.role, t.display_name_enc,
       (t.given_name_enc IS NOT NULL AND t.family_name_enc IS NOT NULL AND t.dob_enc IS NOT NULL
        AND t.nationality_enc IS NOT NULL AND t.passport_no_enc IS NOT NULL AND t.passport_expiry_enc IS NOT NULL
        AND t.passport_country_enc IS NOT NULL AND t.emergency_contact_enc IS NOT NULL) AS details_done
  FROM crm.trip_traveler tt
  JOIN crm.traveler t ON t.traveler_id = tt.traveler_id AND t.deleted_at IS NULL
 WHERE tt.trip_id = ANY($1::uuid[]) AND tt.deleted_at IS NULL
 ORDER BY tt.trip_id, tt.role = 'lead' DESC, tt.created_at, tt.traveler_id
"""
COUNT_SQL = """
SELECT count(*) FROM crm.trip_traveler tt
  JOIN crm.traveler t ON t.traveler_id = tt.traveler_id AND t.deleted_at IS NULL
 WHERE tt.trip_id = ANY($1::uuid[]) AND tt.deleted_at IS NULL
"""
# Manifest columns that carry health codes: picking one always needs a reason.
HEALTH_FIELDS = frozenset({"dietary", "mobility"})
STATE_SQL = """
SELECT departure_status, last_event_at FROM crm.aa_departure_state WHERE departure_id = $1 AND deleted_at IS NULL
"""
FEED_ROW_SQL = "SELECT 1 FROM crm.booking_feed_state"
UPSERT_SQL = """
INSERT INTO crm.aa_departure_state (departure_id, departure_status, guaranteed_at, cancelled_at, last_event_id,
                                    last_event_at)
VALUES ($1, $2, CASE WHEN $2 = 'guaranteed' THEN $4::timestamptz END,
        CASE WHEN $2 = 'cancelled' THEN $4::timestamptz END, $3, $4)
ON CONFLICT (tenant_id, departure_id) DO UPDATE
   SET departure_status = EXCLUDED.departure_status,
       guaranteed_at = coalesce(EXCLUDED.guaranteed_at, crm.aa_departure_state.guaranteed_at),
       cancelled_at = coalesce(EXCLUDED.cancelled_at, crm.aa_departure_state.cancelled_at),
       last_event_id = EXCLUDED.last_event_id, last_event_at = EXCLUDED.last_event_at
 WHERE crm.aa_departure_state.last_event_at <= EXCLUDED.last_event_at
"""
EVENT_STATES = {"departure.guaranteed": "guaranteed", "departure.cancelled": "cancelled"}


def ref(value: Any) -> str:
    if not isinstance(value, str) or not REF.match(value):
        raise Invalid("invalid_departure_ref", "1 to 80 letters, digits or : _ . -")
    return value


def pax_of(rows: list[Any]) -> dict[str, int]:
    """Adults, children (2-17) and infants (under 2) of the trips' parties."""
    out = {"adults": 0, "children": 0, "infants": 0}
    for r in rows:
        out["adults"] += int(r["party_adults"])
        for age in r["party_child_ages"] or []:
            out["infants" if int(age) < 2 else "children"] += 1
    return out


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _state(row: Any) -> dict[str, Any]:
    return {
        "status": row["departure_status"] or "scheduled",
        "guaranteed_at": _iso(row["guaranteed_at"]),
        "cancelled_at": _iso(row["cancelled_at"]),
        "min_to_operate": row["min_to_operate"],
    }


def _audit_id(value: str) -> str | None:
    return value if _AUDIT_ID.match(value) else None


async def roster_list(ctx: AnyContext, scope: str, today: dt.date) -> dict[str, Any]:
    if scope not in SCOPES:
        raise Invalid("invalid_scope", ", ".join(SCOPES))
    rows = list(await ctx.conn.fetch(LIST_SQL, scope, list(LIVE_STAGES_EXCLUDED), today, MAX_DEPARTURES))
    trip_ids = sorted({t for r in rows for t in r["trip_ids"]})
    trips = {t["trip_id"]: t for t in await ctx.conn.fetch(TRIPS_SQL, trip_ids, MAX_DEPARTURES * 10)} if rows else {}
    items = []
    for r in rows:
        mine = [trips[t] for t in r["trip_ids"] if t in trips]
        items.append(
            {
                "departure_id": r["departure_id"],
                "title": r["title"],
                "service_from": _iso(r["service_from"]),
                "service_to": _iso(r["service_to"]),
                "state": _state(r),
                "trips": len(mine),
                "pax": pax_of(mine),
                "stage3_complete": sum(1 for t in mine if t["s3"] >= 100),
            }
        )
    return {"scope": scope, "departures": items}


async def _one(ctx: AnyContext, departure_id: str) -> Any:
    row = await ctx.conn.fetchrow(ONE_SQL, ref(departure_id), list(LIVE_STAGES_EXCLUDED))
    if row is None:
        raise NotFound("departure_not_found")
    return row


async def roster(ctx: AnyContext, vault: Vault, departure_id: str) -> dict[str, Any]:
    row = await _one(ctx, departure_id)
    trips = list(await ctx.conn.fetch(TRIPS_SQL, list(row["trip_ids"]), MAX_TRIPS))
    ids = [t["trip_id"] for t in trips]
    party = list(await ctx.conn.fetch(PARTY_SQL, ids))
    checks = await passport.checks(ctx, vault, ids)
    status_of = {(t, uuid.UUID(p["traveler_id"])): p["status"] for t, people in checks.items() for p in people}
    out_trips = []
    travellers = []
    for t in trips:
        rooms = await rooming.by_traveller(ctx, t["trip_id"])
        people = [p for p in party if p["trip_id"] == t["trip_id"]]
        for p in people:
            travellers.append(
                {
                    "trip_id": str(t["trip_id"]),
                    "traveler_id": str(p["traveler_id"]),
                    "name": await vault.decrypt(
                        "traveler", "display_name_enc", p["traveler_id"], p["display_name_enc"]
                    ),
                    "role": p["role"],
                    "details_done": bool(p["details_done"]),
                    "passport": status_of.get((t["trip_id"], p["traveler_id"]), "missing"),
                    "room": rooms.get(p["traveler_id"]),
                }
            )
        out_trips.append(
            {
                "trip_id": str(t["trip_id"]),
                "title": t["title"],
                "stage": t["stage"],
                "start_date": _iso(t["start_date"]),
                "end_date": _iso(t["end_date"]),
                "pax": pax_of([t]),
                "s3": t["s3"],
                "travellers": len(people),
                "rooms_placed": sum(1 for p in people if p["traveler_id"] in rooms),
                "passport_issues": sum(
                    1 for p in people if status_of.get((t["trip_id"], p["traveler_id"])) == passport.ISSUE
                ),
            }  # fmt: skip
        )
    if travellers:
        ctx.audit("list_view", "allowed", "departure", _audit_id(row["departure_id"]), fields=("display_name",),
                  reason="departure_roster", object_ids=tuple(str(t) for t in ids)[:200])  # fmt: skip
    return {
        "departure_id": row["departure_id"],
        "title": row["title"],
        "service_from": _iso(row["service_from"]),
        "service_to": _iso(row["service_to"]),
        "state": _state(row),
        "pax": pax_of(trips),
        "stage3_complete": sum(1 for t in trips if t["s3"] >= 100),
        "trips": out_trips,
        "travellers": travellers,
        "manifest_fields": list(MANIFEST_FIELDS),
        "manifest_default": list(MANIFEST_DEFAULT),
    }


def pick_fields(fields: list[str] | None) -> tuple[str, ...]:
    if not fields:
        return MANIFEST_DEFAULT
    unknown = sorted(set(fields) - set(MANIFEST_FIELDS))
    if unknown:
        raise Invalid("manifest_field_unknown", ", ".join(MANIFEST_FIELDS), fields=unknown)
    return tuple(f for f in MANIFEST_FIELDS if f in set(fields))


def watermark() -> str:
    return f"{WATERMARK_PREFIX}-{secrets.token_hex(5).upper()}"


def cell(value: str | None) -> str:
    """CSV formula injection: a cell a spreadsheet would run starts with a quote instead."""
    text = value or ""
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def _flight(f: dict[str, Any] | None) -> str:
    if not f:
        return ""
    return " ".join(str(p) for p in (f.get("flight_no"), f.get("airport"), f.get("sched_local"), f.get("tz")) if p)


def _name(person: dict[str, Any]) -> str:
    full = " ".join(p for p in (person.get("given_name"), person.get("family_name")) if p)
    return full or person.get("name") or ""


def manifest_csv(people: list[tuple[str, dict[str, Any], str]], fields: tuple[str, ...], mark: str) -> tuple[str, int]:
    """``people`` = ``(trip_ref, release person, passport status)``. Columns: the watermark, the row number, then the
    picked fields."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(["watermark", "no", *fields])
    for i, (trip_ref, person, status) in enumerate(people, start=1):
        values = {
            "name": _name(person),
            "trip_ref": trip_ref,
            "age_band": person.get("age_band") or "",
            "room": rooming.room_text(person.get("room")),
            "passport_check": status,
            "dietary": " ".join(person.get("dietary") or []),
            "mobility": " ".join(person.get("mobility") or []),
            "arrival_flight": _flight(person.get("arrival")),
            "departure_flight": _flight(person.get("departure")),
        }
        w.writerow([mark, str(i), *(cell(values[f]) for f in fields)])
    return out.getvalue(), len(people)


def manifest_reason(reason: str | None, picked: tuple[str, ...], count: int) -> str:
    """The reason of a manifest export: given, or :data:`app.duty.service.DEFAULT_REASON` for a small export without a
    health field; else 422 ``reason_required``."""
    if reason is None and count <= duty.REASON_THRESHOLD and not HEALTH_FIELDS & set(picked):
        reason = duty.DEFAULT_REASON
    if reason not in duty.BULK_REASONS:
        raise Invalid(
            "reason_required",
            f"a manifest of more than {duty.REASON_THRESHOLD} travellers or with health codes needs a reason",
            reasons=list(duty.BULK_REASONS),
            threshold=duty.REASON_THRESHOLD,
        )
    return reason


async def manifest(ctx: TenantContext, vault: Vault, departure_id: str, fields: list[str] | None,
                   reason: str | None = None) -> dict[str, Any]:  # fmt: skip
    picked = pick_fields(fields)
    row = await _one(ctx, departure_id)
    trips = list(await ctx.conn.fetch(TRIPS_SQL, list(row["trip_ids"]), MAX_TRIPS))
    ids = [t["trip_id"] for t in trips]
    count = int(await ctx.conn.fetchval(COUNT_SQL, ids) or 0)
    reason = manifest_reason(reason, picked, count)
    used = reveal_meter.METER.count(ctx.tenant_id, ctx.member_id)
    if used + count > duty.HOURLY_LIMIT:
        ctx.audit("reveal", "denied", "departure", _audit_id(row["departure_id"]), fields=picked,
                  reason="rate_limited", object_ids=tuple(str(t) for t in ids)[:200])  # fmt: skip
        raise RateLimited(
            "reveal_rate_limited",
            int(reveal_meter.WINDOW_S),
            f"{duty.HOURLY_LIMIT} travellers an hour; {max(duty.HOURLY_LIMIT - used, 0)} left",
        )
    checks = await passport.checks(ctx, vault, ids) if "passport_check" in picked else {}
    status_of = {(t, p["traveler_id"]): p["status"] for t, people in checks.items() for p in people}
    people: list[tuple[str, dict[str, Any], str]] = []
    for t in trips:
        built = await build.document(ctx, vault, t, [])
        doc = json.loads(built.text)
        for person in doc["travellers"]:
            people.append((str(t["trip_id"])[:8], person, status_of.get((t["trip_id"], person["traveler_id"]), "")))
    mark = watermark()
    text, count = manifest_csv(people, picked, mark)
    for _trip_ref, person, _status in people:  # one reveal per traveller released: the reveal meter counts them
        ctx.audit("reveal", "allowed", "traveler", str(person["traveler_id"]), fields=picked, reason=reason)
    ctx.audit("report_export", "allowed", "departure", _audit_id(row["departure_id"]), fields=picked,
              reason="departure_manifest", object_ids=tuple(str(t) for t in ids)[:200])  # fmt: skip
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", row["departure_id"])[:40]
    return {
        "filename": f"departure-{safe}-{mark}.csv",
        "watermark": mark,
        "row_count": count,
        "fields": list(picked),
        "reason": reason,
        "csv": text,
    }


async def apply_event(ctx: AnyContext, event_type: str, departure_id: str | None, event_id: str,
                      occurred_at: dt.datetime) -> tuple[str, str]:  # fmt: skip
    """``departure.guaranteed`` / ``departure.cancelled`` -> the agency's departure state. ``(outcome, reason)``."""
    status = EVENT_STATES.get(event_type)
    if status is None or departure_id is None:
        return "ignored", "departure_incomplete"
    if await ctx.conn.fetchval(FEED_ROW_SQL) is None:
        return "ignored", "no_feed_state"
    await ctx.conn.execute(UPSERT_SQL, departure_id, status, event_id, occurred_at)
    ctx.audit("aa_sync", "allowed", "departure", _audit_id(departure_id), reason=f"departure_{status}")
    return "applied", f"departure_{status}"
