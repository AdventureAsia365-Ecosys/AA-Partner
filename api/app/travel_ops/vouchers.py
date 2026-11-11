"""Traveller vouchers, one per confirmed booking line (AA-1070 item crm-20; crm-054 ``crm.booking_line_voucher``).

**Details** the agency adds to a line for its travellers (:func:`save_details`): the pickup and drop-off places, the
service's local start time, the supplier's PNR / booking reference and a note. The four texts are sealed under the
tenant vault DEK (AAD = ``booking_line_voucher``, the column, the row id); the time is plain.

**A voucher** (:func:`trip_vouchers`) puts together, for one line: the agency's name, the lead traveller's name
(opened server-side), the party size, the service (title, dates, time, places), the confirmation number, the PNR, the
note and the supplier: an own supplier's name and its contact person's name (opened for the voucher only, never the
e-mail), or "Adventure Asia" with a short AA booking reference for an AA line (its departure and dates come from the
line's Booking facts; Booking's confirmation read with pick-up points is a later Booking card).

**Released to the travellers** (:data:`RELEASED_STAGES`) when the trip is booked (or later), the line is confirmed and
shown to travellers (``visible_to_travelers``). The agent sees every confirmed line's voucher with ``released`` and,
when not, why (``trip_not_booked`` / ``line_hidden``), so they can print it before releasing.

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from app import cancellation
from app.errors import Invalid, NotFound
from app.intake import stage1
from app.tenancy import AnyContext
from app.vault import Vault

RELEASED_STAGES = ("booked", "pre_departure", "travelling", "completed")
TEXT_FIELDS = {"pickup": 200, "dropoff": 200, "pnr": 80, "note": 500}
TABLE = "booking_line_voucher"
AA_SUPPLIER_NAME = "Adventure Asia"

LINE_SQL = """
SELECT l.booking_line_id, l.trip_id, l.status FROM crm.booking_line l
 WHERE l.booking_line_id = $1 AND l.deleted_at IS NULL
"""
DETAILS_SQL = """
SELECT booking_line_voucher_id, booking_line_id, pickup_enc, dropoff_enc, service_time, pnr_enc, note_enc
  FROM crm.booking_line_voucher WHERE booking_line_id = $1 AND deleted_at IS NULL
"""
TRIP_DETAILS_SQL = """
SELECT booking_line_voucher_id, booking_line_id, pickup_enc, dropoff_enc, service_time, pnr_enc, note_enc
  FROM crm.booking_line_voucher WHERE trip_id = $1 AND deleted_at IS NULL
"""
INSERT_SQL = """
INSERT INTO crm.booking_line_voucher (booking_line_voucher_id, booking_line_id, trip_id, pickup_enc, dropoff_enc,
                                      service_time, pnr_enc, note_enc)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""
UPDATE_SQL = """
UPDATE crm.booking_line_voucher SET pickup_enc = $2, dropoff_enc = $3, service_time = $4, pnr_enc = $5, note_enc = $6
 WHERE booking_line_voucher_id = $1
"""
TRIP_SQL = """
SELECT trip_id, title, stage, start_date, end_date, party_adults, party_child_ages
  FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL
"""
LEAD_SQL = """
SELECT tt.traveler_id, t.display_name_enc, t.given_name_enc, t.family_name_enc
  FROM crm.trip_traveler tt JOIN crm.traveler t ON t.traveler_id = tt.traveler_id AND t.deleted_at IS NULL
 WHERE tt.trip_id = $1 AND tt.role = 'lead' AND tt.deleted_at IS NULL
"""
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
LINES_SQL = """
SELECT l.booking_line_id, l.supplier_kind, l.title, l.confirmation_no, l.service_from, l.service_to,
       l.visible_to_travelers, l.bk_booking_id, l.departure_id, l.agency_supplier_id, s.name AS supplier_name,
       s.kind AS supplier_type, s.category, s.contact_name_enc
  FROM crm.booking_line l JOIN crm.agency_supplier s ON s.agency_supplier_id = l.agency_supplier_id
 WHERE l.trip_id = $1 AND l.status = 'confirmed' AND l.deleted_at IS NULL
 ORDER BY l.service_from NULLS LAST, l.created_at, l.booking_line_id
"""


def _text(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        clean = stage1.clean_text(value, TEXT_FIELDS[field])
    except ValueError:
        clean = None
    if not clean:
        raise Invalid(f"invalid_{field}", f"1 to {TEXT_FIELDS[field]} characters")
    return clean


def _time(value: Any) -> dt.time | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.time):
        return value.replace(second=0, microsecond=0)
    try:
        return dt.time.fromisoformat(str(value)).replace(second=0, microsecond=0)
    except ValueError:
        raise Invalid("invalid_service_time", "HH:MM") from None


async def _open(vault: Vault, row: Any, column: str) -> str | None:
    blob = row[column]
    if blob is None:
        return None
    return await vault.decrypt(TABLE, column, row["booking_line_voucher_id"], blob)


async def _details(vault: Vault, row: Any | None) -> dict[str, Any]:
    if row is None:
        return {"pickup": None, "dropoff": None, "service_time": None, "pnr": None, "note": None}
    return {
        "pickup": await _open(vault, row, "pickup_enc"),
        "dropoff": await _open(vault, row, "dropoff_enc"),
        "service_time": row["service_time"].strftime("%H:%M") if row["service_time"] else None,
        "pnr": await _open(vault, row, "pnr_enc"),
        "note": await _open(vault, row, "note_enc"),
    }


async def get_details(ctx: AnyContext, vault: Vault, line_id: uuid.UUID) -> dict[str, Any]:
    if await ctx.conn.fetchrow(LINE_SQL, line_id) is None:
        raise NotFound("line_not_found")
    out = await _details(vault, await ctx.conn.fetchrow(DETAILS_SQL, line_id))
    ctx.audit("view", "allowed", "booking_line", str(line_id), fields=("voucher",))
    return {"booking_line_id": str(line_id), **out}


async def save_details(ctx: AnyContext, vault: Vault, line_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
    """Write the line's voucher details (every field; an empty one clears it)."""
    line = await ctx.conn.fetchrow(LINE_SQL, line_id)
    if line is None:
        raise NotFound("line_not_found")
    if line["status"] == "cancelled":
        raise Invalid("line_cancelled", "a cancelled line has no voucher")
    await cancellation.ensure_not_cancelled(ctx, line["trip_id"], "no voucher change")
    values = {f: _text(body.get(f), f) for f in TEXT_FIELDS}
    service_time = _time(body.get("service_time"))
    row = await ctx.conn.fetchrow(DETAILS_SQL, line_id)
    voucher_id = row["booking_line_voucher_id"] if row is not None else uuid.uuid4()

    async def seal(field: str) -> bytes | None:
        value = values[field]
        return None if value is None else await vault.encrypt(TABLE, f"{field}_enc", voucher_id, value)

    sealed = [await seal(f) for f in ("pickup", "dropoff")]
    tail = [await seal(f) for f in ("pnr", "note")]
    if row is None:
        await ctx.conn.execute(INSERT_SQL, voucher_id, line_id, line["trip_id"], *sealed, service_time, *tail)
        ctx.audit("create", "allowed", "booking_line_voucher", str(voucher_id), fields=tuple(sorted(TEXT_FIELDS)))
    else:
        await ctx.conn.execute(UPDATE_SQL, voucher_id, *sealed, service_time, *tail)
        ctx.audit("update", "allowed", "booking_line_voucher", str(voucher_id), fields=tuple(sorted(TEXT_FIELDS)))
    return {
        "booking_line_id": str(line_id),
        **{f: values[f] for f in ("pickup", "dropoff", "pnr", "note")},
        "service_time": service_time.strftime("%H:%M") if service_time else None,
    }


async def lead_name(ctx: AnyContext, vault: Vault, trip_id: uuid.UUID) -> str | None:
    row = await ctx.conn.fetchrow(LEAD_SQL, trip_id)
    if row is None:
        return None
    parts = []
    for column in ("given_name_enc", "family_name_enc"):
        if row[column] is not None:
            parts.append(await vault.decrypt("traveler", column, row["traveler_id"], row[column]))
    if parts:
        return " ".join(parts)
    return await vault.decrypt("traveler", "display_name_enc", row["traveler_id"], row["display_name_enc"])


def release_reason(stage: str, visible: bool) -> str | None:
    """Why a confirmed line's voucher is not released to the travellers, or None when it is."""
    if stage not in RELEASED_STAGES:
        return "trip_not_booked"
    if not visible:
        return "line_hidden"
    return None


def _iso(value: dt.date | None) -> str | None:
    return value.isoformat() if value is not None else None


async def trip_vouchers(
    ctx: AnyContext, vault: Vault, trip_id: uuid.UUID, *, travellers_only: bool = False
) -> dict[str, Any]:
    """The vouchers of a trip: every confirmed line (the agent), or only the released ones (the traveller portal)."""
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    lines = list(await ctx.conn.fetch(LINES_SQL, trip_id))
    if travellers_only:
        lines = [ln for ln in lines if release_reason(trip["stage"], ln["visible_to_travelers"]) is None]
    details = {r["booking_line_id"]: r for r in await ctx.conn.fetch(TRIP_DETAILS_SQL, trip_id)} if lines else {}
    agency = await ctx.conn.fetchval(AGENCY_SQL, ctx.tenant_id)
    lead = await lead_name(ctx, vault, trip_id) if lines else None
    pax = int(trip["party_adults"]) + len(trip["party_child_ages"] or [])
    vouchers = []
    for ln in lines:
        aa = ln["supplier_kind"] == "aa"
        contact = None
        if not aa and ln["supplier_type"] == "own" and ln["contact_name_enc"] is not None:
            contact = await vault.decrypt(
                "agency_supplier", "contact_name_enc", ln["agency_supplier_id"], ln["contact_name_enc"]
            )
        reason = release_reason(trip["stage"], ln["visible_to_travelers"])
        vouchers.append(
            {
                "booking_line_id": str(ln["booking_line_id"]),
                "voucher_no": str(ln["booking_line_id"])[:8].upper(),
                "title": ln["title"],
                "service_from": _iso(ln["service_from"]),
                "service_to": _iso(ln["service_to"]),
                "confirmation_no": ln["confirmation_no"],
                "supplier": {
                    "name": AA_SUPPLIER_NAME if aa else ln["supplier_name"],
                    "category": ln["category"],
                    "contact_name": contact,
                    "aa_booking_ref": ((ln["bk_booking_id"] or "")[:8] or None) if aa else None,
                    "aa_departure_id": ln["departure_id"] if aa else None,
                },
                **await _details(vault, details.get(ln["booking_line_id"])),
                "released": reason is None,
                "not_released_reason": reason,
            }
        )
    if vouchers:
        ctx.audit(
            "list_view",
            "allowed",
            "booking_line_voucher",
            None,
            fields=("voucher",),
            reason="traveller_portal" if travellers_only else "trip_vouchers",
            object_ids=tuple(v["booking_line_id"] for v in vouchers),
        )
    return {
        "trip_id": str(trip_id),
        "agency_name": agency,
        "trip_title": trip["title"],
        "start_date": _iso(trip["start_date"]),
        "end_date": _iso(trip["end_date"]),
        "lead_traveller": lead,
        "pax": pax,
        "vouchers": vouchers,
    }
