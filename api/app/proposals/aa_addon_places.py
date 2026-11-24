"""Which AA add-ons belong to a builder day: the places of the AA tour on that day (AA-1152 follow-up, S291).

The TripPlanner day cards offer only the add-ons sold at one of the tour day's places (its CIS destinations); the CRM
drawer and its add route follow the same rule, so an agency cannot put an add-on of another region into a trip.

The CRM day has free text for its place, never a CIS id, so the places come from the AA tour itself: the catalog cache
(``crm.aa_catalog_tour.body``, CIS ``catalog.http.v1`` ``Tour.days[].places[].destination_id``, the ids Booking's
``GET /add-ons`` answers with). For a block on proposal day *P*:

- every whole AA tour of the version (a live AA option of component ``trip_plan``) starts on the earliest day of a
  block that holds it (the day :mod:`app.proposals.aa_days` copies its day 1 to); its tour day on *P* is
  ``P - start + 1``, and a day before the tour takes its first day, a day after it its last day (the traveller is at
  the start or the end of the tour);
- the tour(s) nearest *P* win (distance 0 when a tour runs that day); tours at the same distance (alternatives in one
  block, two tours on one day) give the union of their places, and the add-on hangs on the first of them;
- a proposal whose AA tour is only there as parts (a room, a transfer; no ``trip_plan``) allows every place of that
  tour's days, and the add-on hangs on it (:data:`app.proposals.aa_addons.TOUR_SQL`);
- no AA tour: nothing is allowed (``aa_addon_needs_tour``); a tour whose days carry no place yet (not resolved in CIS,
  or not cached) allows nothing either: fail closed, the drawer says so.

Nothing here asks CIS or Booking.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.tenancy import AnyContext

# The block's day number (never a deleted day).
DAY_NO_SQL = """
SELECT d.day_no
  FROM crm.option_block b JOIN crm.proposal_day d ON d.proposal_day_id = b.proposal_day_id
 WHERE b.option_block_id = $1
"""
# The version's whole AA tours: the earliest trip day of a live block that holds each, and its cached catalog days.
WHOLE_TOURS_SQL = """
SELECT min(d.day_no)::int AS start_day, o.aa_tour_source_id AS source_id, min(o.created_at) AS first_added,
       (SELECT c.body -> 'days' FROM crm.aa_catalog_tour c
         WHERE c.source_system = 'cis' AND c.source_id = o.aa_tour_source_id) AS days
  FROM crm.option o
  JOIN crm.option_block b ON b.option_block_id = o.option_block_id AND b.deleted_at IS NULL
  JOIN crm.proposal_day d ON d.proposal_day_id = b.proposal_day_id AND d.deleted_at IS NULL
 WHERE o.proposal_id = $1 AND o.source = 'aa' AND o.aa_component_kind = 'trip_plan' AND o.deleted_at IS NULL
   AND o.aa_tour_source_id IS NOT NULL AND d.day_no >= 1
 GROUP BY o.aa_tour_source_id
 ORDER BY first_added, o.aa_tour_source_id
"""
# The cached catalog days of one tour (a tour that is only in the version as parts).
TOUR_DAYS_SQL = """
SELECT body -> 'days' AS days FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = $1
"""


@dataclass(frozen=True)
class DayPlaces:
    """The AA tour an add-on of the day hangs on (None = the version has no AA tour) and the CIS destination ids of the
    places an add-on of that day must be sold at (empty = none is allowed)."""

    tour_source_id: str | None
    places: frozenset[str]

    def allows(self, destination_ids: list[str] | tuple[str, ...]) -> bool:
        return any(norm(d) in self.places for d in destination_ids)


def norm(destination_id: Any) -> str:
    """One spelling of a CIS destination id for both sides (Booking answers lower-case UUIDs)."""
    text = str(destination_id or "").strip()
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return text.casefold()


def days_places(raw: Any) -> dict[int, frozenset[str]]:
    """The served body's ``days`` as {day number: destination ids}; a malformed day or place is skipped."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    out: dict[int, frozenset[str]] = {}
    for day in raw if isinstance(raw, list) else []:
        if not isinstance(day, dict):
            continue
        number = day.get("day_no")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1 or number in out:
            continue
        places = day.get("places")
        ids = {
            norm(p.get("destination_id")) for p in (places if isinstance(places, list) else []) if isinstance(p, dict)
        }
        out[number] = frozenset(i for i in ids if i)
    return out


def _distance(day_no: int, start: int, last: int) -> tuple[int, int]:
    """(how many days *day_no* lies outside the tour, the tour day it takes: clamped to 1..last)."""
    k = day_no - start + 1
    if k < 1:
        return 1 - k, 1
    if k > last:
        return k - last, last
    return 0, k


def nearest(day_no: int, tours: list[dict[str, Any]]) -> DayPlaces | None:
    """The rule of the module text over the whole tours (``source_id``, ``start_day``, ``days`` in the order they
    were added); None when there is no whole tour."""
    scored: list[tuple[int, str, frozenset[str]]] = []
    for tour in tours:
        days = days_places(tour["days"])
        if not days:
            scored.append((10**6, tour["source_id"], frozenset()))  # no cached days: known, but no place
            continue
        gap, k = _distance(day_no, int(tour["start_day"]), max(days))
        scored.append((gap, tour["source_id"], days.get(k, frozenset())))
    if not scored:
        return None
    best = min(s[0] for s in scored)
    chosen = [s for s in scored if s[0] == best]
    return DayPlaces(chosen[0][1], frozenset().union(*(s[2] for s in chosen)))


async def of_block(ctx: AnyContext, proposal_id: uuid.UUID, block_id: uuid.UUID, fallback_sql: str) -> DayPlaces:
    """The AA tour and the allowed places of the block's day (``fallback_sql``: the add-on's tour when the version has
    no whole tour, :data:`app.proposals.aa_addons.TOUR_SQL`)."""
    day_no = await ctx.conn.fetchval(DAY_NO_SQL, block_id)
    tours = [dict(r) for r in await ctx.conn.fetch(WHOLE_TOURS_SQL, proposal_id)]
    found = nearest(int(day_no or 0), tours)
    if found is not None:
        return found
    tour = await ctx.conn.fetchval(fallback_sql, proposal_id, block_id)
    if tour is None:
        return DayPlaces(None, frozenset())
    every = days_places(await ctx.conn.fetchval(TOUR_DAYS_SQL, tour)).values()
    return DayPlaces(tour, frozenset().union(*every))
