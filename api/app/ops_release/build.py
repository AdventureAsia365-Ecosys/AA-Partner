"""The ops release document of one trip and its completeness (AA-956, design v3 M8, Q1 3.10 "minimal ops fields",
data-privacy-integrations §2.10 / §5.6 / §6.8, R33, R44 data).

**What AA ops get** (per traveller on the trip, lead first): the name (given + family name when on file, else the
display name), the age band on the departure date (from the date of birth; the date itself is never released),
dietary and mobility **codes** (only while the traveller's latest ``health_data`` consent is yes), the arrival and
departure flights (flight number, airport, local time and its IANA zone: an overnight flight lands on its own local
date, R33), and the insurance choice of the signed waiver (``insured`` / ``declined``, R13). Per trip: a short trip
reference, the dates, the party size, the agency's name and time zone (the agency is the emergency channel, R37) and
the confirmed AA lines (title, tour, Booking departure and booking ids, service dates).

**Never released**: phone, e-mail, the emergency contact (X6: excluded by default, a Ms. Thư question), passport and
nationality data, the date of birth, notes, messages, documents, money, the trip title (agencies write client names
into it).

**Completeness** (the S-02 indicator, Fix B2): per traveller the Stage 3 details are on file
(:func:`app.portal.progress.compute`), the waiver is signed (when the agency has one), and both flights are given.
Counts only: ``{"travellers": n, "complete": k, "missing": {"stage3": a, "waiver": b, "arrival_flight": c,
"departure_flight": d}}``; complete = at least one traveller and every item done.

Every function takes the open context and never opens one; every personal value is opened through the
:class:`app.vault.Vault` of that context.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.portal import progress
from app.tenancy import AnyContext
from app.travellers.age_bands import age_band
from app.vault import Vault

DOCUMENT_VERSION = 1
MISSING_ITEMS = ("stage3", "waiver", "arrival_flight", "departure_flight")
# The field codes a document can carry (crm.ops_release.fields_released) and a manifest can print.
FIELD_CODES = ("name", "age_band", "dietary", "mobility", "arrival_flight", "departure_flight", "insurance")
# The F1 fields a build opens (one audit event per run names them, design §5.7 "volume control").
OPENED_FIELDS = ("display_name", "given_name", "family_name", "dob", "health", "flight_no")

AGENCY_SQL = """
SELECT t.display_name, coalesce((SELECT s.timezone FROM crm.tenant_settings s LIMIT 1), $2) AS timezone
  FROM crm.tenant t WHERE t.tenant_id = $1
"""
TRAVELLERS_SQL = """
SELECT tt.traveler_id, tt.role, t.display_name_enc, t.given_name_enc, t.family_name_enc, t.dob_enc,
       h.health_id, h.health_flags_enc,
       (SELECT c.value FROM crm.consent c
         WHERE c.traveler_id = tt.traveler_id AND c.purpose = 'health_data'
         ORDER BY c.at DESC, c.consent_id DESC LIMIT 1) AS health_ok,
       (SELECT s.insurance_choice FROM crm.signature_request s
         WHERE s.trip_id = tt.trip_id AND s.traveler_id = tt.traveler_id AND s.status = 'signed'
           AND s.deleted_at IS NULL
         ORDER BY s.signed_at DESC, s.request_id LIMIT 1) AS insurance_choice
  FROM crm.trip_traveler tt
  JOIN crm.traveler t ON t.traveler_id = tt.traveler_id AND t.deleted_at IS NULL
  LEFT JOIN crm.traveler_health h ON h.traveler_id = tt.traveler_id AND h.deleted_at IS NULL
 WHERE tt.trip_id = $1 AND tt.deleted_at IS NULL
 ORDER BY tt.role = 'lead' DESC, tt.created_at, tt.traveler_id
"""
FLIGHTS_SQL = """
SELECT traveler_flight_id, traveler_id, direction, flight_no_enc, airport_iata, sched_local, tz
  FROM crm.traveler_flight
 WHERE trip_id = $1 AND deleted_at IS NULL
"""
DEFAULT_TZ = "Asia/Ho_Chi_Minh"


@dataclass(frozen=True)
class Built:
    """One build: the canonical document text (what is sealed), the field codes it carries, the completeness."""

    text: str
    fields: tuple[str, ...]
    completeness: dict[str, Any]
    complete: bool
    traveller_count: int
    traveller_ids: tuple[str, ...]


def _iso(value: dt.date | dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def canonical(doc: dict[str, Any]) -> str:
    """The sealed text: sorted keys, no spaces, so the same facts give the same bytes and a refresh can compare."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def _flight(vault: Vault, row: Any) -> dict[str, Any]:
    return {
        "flight_no": await vault.decrypt("traveler_flight", "flight_no_enc", row["traveler_flight_id"],
                                         row["flight_no_enc"]),  # fmt: skip
        "airport": row["airport_iata"],
        "sched_local": row["sched_local"].isoformat(timespec="minutes"),
        "tz": row["tz"],
    }


async def document(ctx: AnyContext, vault: Vault, trip: Any, lines: list[Any]) -> Built:
    """The release document of ``trip`` (a row with ``trip_id``, ``start_date``, ``end_date``, ``party_adults``,
    ``party_child_ages``) and its confirmed AA ``lines``."""
    trip_id: uuid.UUID = trip["trip_id"]
    agency = await ctx.conn.fetchrow(AGENCY_SQL, ctx.tenant_id, DEFAULT_TZ)
    flights: dict[tuple[uuid.UUID, str], Any] = {
        (r["traveler_id"], r["direction"]): r for r in await ctx.conn.fetch(FLIGHTS_SQL, trip_id)
    }
    done = {p.traveler_id: p for p in (await progress.compute(ctx, trip_id)).travellers}
    waiver_required = any(p.waiver_signed is not None for p in done.values())
    start: dt.date | None = trip["start_date"]
    travellers: list[dict[str, Any]] = []
    fields: set[str] = set()
    missing = dict.fromkeys(MISSING_ITEMS, 0)
    complete_count = 0
    for r in await ctx.conn.fetch(TRAVELLERS_SQL, trip_id):
        tid = r["traveler_id"]

        async def opened(column: str, blob: bytes | None, *, row_id: uuid.UUID = tid) -> str | None:
            if blob is None:
                return None
            return await vault.decrypt("traveler", column, row_id, blob)

        person: dict[str, Any] = {
            "traveler_id": str(tid),
            "role": r["role"],
            "name": await opened("display_name_enc", r["display_name_enc"]),
            "given_name": await opened("given_name_enc", r["given_name_enc"]),
            "family_name": await opened("family_name_enc", r["family_name_enc"]),
        }
        fields.add("name")
        dob_text = await opened("dob_enc", r["dob_enc"])
        person["age_band"] = age_band(dt.date.fromisoformat(dob_text), start) if dob_text else None
        if person["age_band"]:
            fields.add("age_band")
        person["dietary"], person["mobility"] = [], []
        if r["health_flags_enc"] is not None and r["health_ok"] is True:
            codes = json.loads(
                await vault.decrypt("traveler_health", "health_flags_enc", r["health_id"], r["health_flags_enc"])
            )
            person["dietary"] = sorted(codes.get("dietary") or [])
            person["mobility"] = sorted(codes.get("mobility") or [])
            fields.update(f for f in ("dietary", "mobility") if person[f])
        for direction in ("arrival", "departure"):
            row = flights.get((tid, direction))
            person[direction] = await _flight(vault, row) if row is not None else None
            if row is not None:
                fields.add(f"{direction}_flight")
        person["insurance"] = r["insurance_choice"]
        if person["insurance"]:
            fields.add("insurance")
        travellers.append(person)
        # Completeness of this traveller.
        p = done.get(tid)
        gaps = {
            "stage3": not (p is not None and p.details_done),
            "waiver": waiver_required and not (p is not None and p.waiver_signed),
            "arrival_flight": person["arrival"] is None,
            "departure_flight": person["departure"] is None,
        }
        for item, gap in gaps.items():
            missing[item] += int(gap)
        complete_count += int(not any(gaps.values()))
    doc = {
        "v": DOCUMENT_VERSION,
        "trip": {
            "trip_ref": str(trip_id)[:8],
            "start_date": _iso(start),
            "end_date": _iso(trip["end_date"]),
            "adults": trip["party_adults"],
            "children": len(trip["party_child_ages"] or []),
            "agency_name": agency["display_name"] if agency else None,
            "agency_timezone": agency["timezone"] if agency else DEFAULT_TZ,
        },
        "lines": [
            {
                "booking_line_id": str(ln["booking_line_id"]),
                "title": ln["title"],
                "tour_id": ln["aa_tour_source_id"],
                "departure_id": ln["departure_id"],
                "bk_booking_id": ln["bk_booking_id"],
                "service_from": _iso(ln["service_from"]),
                "service_to": _iso(ln["service_to"]),
            }
            for ln in lines
        ],
        "travellers": travellers,
    }
    n = len(travellers)
    completeness = {"travellers": n, "complete": complete_count, "missing": missing}
    return Built(
        text=canonical(doc),
        fields=tuple(f for f in FIELD_CODES if f in fields),
        completeness=completeness,
        complete=n > 0 and complete_count == n,
        traveller_count=n,
        traveller_ids=tuple(t["traveler_id"] for t in travellers),
    )
