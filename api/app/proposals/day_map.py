"""The builder map's points: where each proposal day is, from the version's live AA tours (AA-948, card CRM-20).

A CRM day has free text for its place, never coordinates, so the points come from the AA tour itself: the catalog
cache (``crm.aa_catalog_tour.body``, CIS ``catalog.http.v1`` ``Tour.days[].places[]`` with ``name``, ``lat``, ``lng``
and ``precision``) of every whole AA tour of the version (a live AA option of component ``trip_plan``) that AA still
serves (``served_state = 'served'``: the agency's live AA tours only; a paused or retired tour draws nothing).

A whole tour starts on the earliest proposal day of a live block that holds it (the day :mod:`app.proposals.aa_days`
copies its day 1 to). Proposal day *P* takes tour day ``P - start + 1`` **only when the tour runs that day**: unlike
the add-on rule (:mod:`app.proposals.aa_addon_places`, which clamps a day before or after the tour to its first or last
day), the map never places a day the tour does not cover. Two tours on one day give both tours' places, in the order
the tours were added. A day outside every tour, a tour day without places, a place without a valid point and the
"Before you go" day (day 0) get no pin: the builder draws them without one, never a guessed point.

Nothing here asks CIS: it reads the cache the hourly catalog sync keeps (:mod:`app.catalog.sync`).
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from app.tenancy import AnyContext

# The version's whole AA tours: the earliest trip day of a live block that holds each, and the cached catalog days
# when AA still serves the tour (NULL for a tour missing from the cache, paused or retired there: no pin).
TOURS_SQL = """
SELECT min(d.day_no)::int AS start_day, o.aa_tour_source_id AS source_id, min(o.created_at) AS first_added,
       (SELECT c.body -> 'days' FROM crm.aa_catalog_tour c
         WHERE c.source_system = 'cis' AND c.source_id = o.aa_tour_source_id AND c.served_state = 'served') AS days
  FROM crm.option o
  JOIN crm.option_block b ON b.option_block_id = o.option_block_id AND b.deleted_at IS NULL
  JOIN crm.proposal_day d ON d.proposal_day_id = b.proposal_day_id AND d.deleted_at IS NULL
 WHERE o.proposal_id = $1 AND o.source = 'aa' AND o.aa_component_kind = 'trip_plan' AND o.deleted_at IS NULL
   AND o.aa_tour_source_id IS NOT NULL AND d.day_no >= 1
 GROUP BY o.aa_tour_source_id
 ORDER BY first_added, o.aa_tour_source_id
"""


def _coord(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and low <= number <= high else None


def place_point(place: Any) -> dict[str, Any] | None:
    """One cached place as a map point ``{name, lat, lng, approximate}``, or None without a valid point."""
    if not isinstance(place, dict):
        return None
    lat, lng = _coord(place.get("lat"), -90.0, 90.0), _coord(place.get("lng"), -180.0, 180.0)
    if lat is None or lng is None:
        return None
    name = place.get("name")
    name = name.strip() if isinstance(name, str) else ""
    # contract 1.x precision: "point" is an exact spot; anything else ("area", a legacy value) is approximate.
    return {"name": name, "lat": lat, "lng": lng, "approximate": place.get("precision") != "point"}


def tour_days(raw: Any) -> dict[int, list[dict[str, Any]]]:
    """The cached body's ``days`` as {day number: map points}; a malformed day or place is skipped."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    out: dict[int, list[dict[str, Any]]] = {}
    for day in raw if isinstance(raw, list) else []:
        if not isinstance(day, dict):
            continue
        number = day.get("day_no")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1 or number in out:
            continue
        places = day.get("places")
        points = [p for p in (place_point(x) for x in (places if isinstance(places, list) else [])) if p is not None]
        out[number] = points
    return out


def day_points(day_no: int, tours: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The map points of proposal day ``day_no`` (module rule): the places of every tour running that day, in the
    order the tours were added, each point once (same name and position)."""
    if day_no < 1:
        return []
    points: list[dict[str, Any]] = []
    seen: set[tuple[str, float, float]] = set()
    for tour in tours:
        days = tour.get("_days")
        if days is None:
            days = tour_days(tour.get("days"))
        start = tour.get("start_day")
        if not isinstance(start, int) or not days:
            continue
        for p in days.get(day_no - start + 1, []):
            key = (p["name"], p["lat"], p["lng"])
            if key not in seen:
                seen.add(key)
                points.append(p)
    return points


async def tours(ctx: AnyContext, proposal_id: uuid.UUID) -> list[dict[str, Any]]:
    """The version's served whole AA tours, each with its cached days parsed once (``_days``)."""
    rows = [dict(r) for r in await ctx.conn.fetch(TOURS_SQL, proposal_id)]
    for row in rows:
        row["_days"] = tour_days(row.get("days"))
    return rows


async def with_points(ctx: AnyContext, proposal_id: uuid.UUID, days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The builder view's days, each with its ``map_points`` (module rule; an empty list = no pin)."""
    served = await tours(ctx, proposal_id)
    for day in days:
        day["map_points"] = day_points(int(day["day_no"]), served)
    return days
