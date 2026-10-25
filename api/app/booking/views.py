"""What the agency sees of an AA line (AA-947): the ``aa`` block of a line view and the AA-aware Lost hint.

Pure functions over the columns :mod:`app.ledger.lines` selects (crm-017), so the ledger and the trip's Bookings tab
show the same thing. Nothing here is margin-bearing: AA's public "from" price is public, and the money of an AA line
stays in the line's keyed ``money`` block (cost / sell), which the margin serializer handles.

The ``actions`` list is what a member may ask for now (the route checks again):

- ``hold``: a quoted line (or one whose hold expired) of an open trip;
- ``confirm``: a held line once the trip is booked or later (after the deposit, design F6);
- ``release``: any line Booking has not booked (a held one releases its seats; a quote is simply dropped);
- ``requote``: a line without an active hold (a new price, an expired quote, a sold-out answer to try again).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

OPEN_STAGES = frozenset({"inquiry", "qualifying", "proposal", "booked", "pre_departure", "travelling"})
CONFIRM_STAGES = frozenset({"booked", "pre_departure", "travelling"})
HOLDABLE = frozenset({"quoted", "expired"})
REQUOTABLE = frozenset({"quoted", "sold_out", "expired", "failed", "not_served"})
DECLINE_TO_LOST = {"sold_out": "aa_sold_out"}


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, dt.date | dt.datetime) else None


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def actions(row: Any, trip_stage: str | None) -> list[str]:
    if row["supplier_kind"] != "aa" or row["status"] != "planning" or row.get("aa_pending"):
        return []
    sub = row["aa_sub_status"]
    open_trip = trip_stage in OPEN_STAGES
    out: list[str] = []
    if open_trip and sub in HOLDABLE:
        out.append("hold")
    if sub == "held" and trip_stage in CONFIRM_STAGES:
        out.append("confirm")
    if row["bk_booking_id"] is None:
        out.append("release")
    if open_trip and sub in REQUOTABLE:
        out.append("requote")
    return out


def aa_block(row: Any, trip_stage: str | None) -> dict[str, Any] | None:
    """The ``aa`` block of an AA line (None for an agency line)."""
    if row["supplier_kind"] != "aa":
        return None
    pending = _json(row.get("aa_pending"))
    from_price = row["from_price"]
    return {
        "sub_status": row["aa_sub_status"],
        "tour_source_id": row["aa_tour_source_id"],
        "component_kind": row["aa_component_kind"],
        "currency": row["aa_currency"],
        "pax": _json(row["aa_pax"]),
        "quote_valid_until": _iso(row["quote_valid_until"]),
        "hold_expires_at": _iso(row["hold_expires_at"]),
        "hold_id": row["bk_hold_id"],
        "booking_id": row["bk_booking_id"],
        "from_price": ({"amount": str(from_price), "currency": row["aa_currency"]} if from_price is not None else None),
        "below_from_price": row["below_from_price"],
        "decline_code": row["aa_decline_code"],
        "result_code": row["aa_result_code"],
        "no_longer_offered": row.get("aa_catalog_state") == "retired",
        # "AA pending": a request waits for Booking (kind, attempts, the next try, the last transient error).
        "pending": pending,
        "actions": actions(row, trip_stage),
    }


def lost_reason_hint(rows: list[Any]) -> str | None:
    """The Lost reason the dialog pre-selects from the AA lines (design §2.7 side flows, R23): a declined line ->
    ``aa_sold_out`` when Booking said sold out, else ``aa_on_request_declined``; a line answered sold out ->
    ``aa_sold_out``. None when AA said nothing that explains a loss."""
    hint = None
    for row in rows:
        if row["supplier_kind"] != "aa":
            continue
        if row["aa_sub_status"] == "declined":
            return DECLINE_TO_LOST.get(row["aa_decline_code"] or "", "aa_on_request_declined")
        if row["aa_sub_status"] == "sold_out" or row["aa_result_code"] == "sold_out":
            hint = "aa_sold_out"
    return hint
