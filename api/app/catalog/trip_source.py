"""The AA tour a trip came from (AA-1089, Odoo study rank 48 "show the source AA tour and departure on the trip
header"). Computed on each trip read, never stored: the trip already names its AA tour and departure.

Where the trip names them, first match wins:

1. ``web_booking``: the "Book now" press of the public AA tour page that made the trip (``crm.public_booking_attempt``
   ``created``, AA-1072): its tour and departure; the departure date is the start of the proposal the press sent.
2. ``aa_line``: an AA booking line still standing on the trip (not cancelled, not refused by Booking; the same rule as
   :data:`app.catalog.retired.ITEMS_SQL`): the earliest service date first.

The title comes from the CRM's own cache of the CIS catalogue (``crm.aa_catalog_tour``, the hourly
``crm_catalog_sync``); no CIS call. The link is the public tour page on the client host, ``/trip/<catalogue key>``
(CIS answers a catalogue key with a redirect to the tour's current version since AA-1089): only while the tour is
served, the key is a UUID (CIS 404s anything else before a read) and the client origin is configured.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from app.tenancy import AnyContext

SOURCE_SQL = """
SELECT x.origin, x.tour_source_id, x.departure_id, x.departure_date, c.title, c.served_state
  FROM (
    SELECT 'web_booking' AS origin, a.tour_source_id, a.departure_id, p.starts_on AS departure_date, 0 AS rank,
           a.created_at AS at
      FROM crm.public_booking_attempt a
      LEFT JOIN crm.proposal p ON p.proposal_id = a.proposal_id
     WHERE a.trip_id = $1 AND a.status = 'created' AND a.deleted_at IS NULL
    UNION ALL
    SELECT 'aa_line', l.aa_tour_source_id, l.departure_id, l.service_from, 1, l.created_at
      FROM crm.booking_line l
     WHERE l.trip_id = $1 AND l.supplier_kind = 'aa' AND l.aa_tour_source_id IS NOT NULL
       AND l.deleted_at IS NULL AND l.status <> 'cancelled'
       AND (l.status = 'confirmed' OR l.bk_booking_id IS NOT NULL OR l.aa_sub_status IS NULL
            OR l.aa_sub_status IN ('held', 'pending', 'quoted'))
  ) x
  LEFT JOIN crm.aa_catalog_tour c ON c.source_system = 'cis' AND c.source_id = x.tour_source_id
 ORDER BY x.rank, x.departure_date NULLS LAST, x.at
 LIMIT 1
"""


def page_url(client_origin: str | None, source_id: str, served_state: str | None) -> str | None:
    """The public tour page of a served tour, None when it cannot be opened."""
    if not client_origin or served_state != "served":
        return None
    try:
        key = str(uuid.UUID(source_id))
    except (ValueError, TypeError):
        return None
    return f"{client_origin.rstrip('/')}/trip/{key}"


def _iso(value: dt.date | None) -> str | None:
    return value.isoformat() if value is not None else None


async def trip_view(ctx: AnyContext, trip_id: uuid.UUID, client_origin: str | None = None) -> dict[str, Any] | None:
    """The trip header's "From AA tour" block, or None when the trip names no AA tour."""
    row = await ctx.conn.fetchrow(SOURCE_SQL, trip_id)
    if row is None:
        return None
    state = row["served_state"]
    return {
        "origin": row["origin"],
        "tour_source_id": row["tour_source_id"],
        "title": row["title"],  # None: not in the catalogue cache (yet)
        "served_state": state,
        "departure_id": row["departure_id"],
        "departure_date": _iso(row["departure_date"]),
        "page_url": page_url(client_origin, row["tour_source_id"], state),
    }
