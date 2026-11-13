"""The days of an AA tour in the agency's proposal and in the traveller portal (AA-1086 follow-up, S279 rehearsal
REH-10-portal; tech lead decision 13/11/2026).

The AA tour's days come from the catalog cache (``crm.aa_catalog_tour.body``, CIS ``catalog.http.v1`` ``Tour.days``:
``day_no``, ``title``, ``places[].name``, ``time_zone``); nothing here asks CIS or Booking.

- :func:`after_add` / :func:`after_delete`: a block of a builder draft whose only AA option of a whole tour (component
  ``trip_plan``) is tour T gets T's days: tour day *k* goes into the version's day *d + k - 1* (*d* = the block's
  day), its title where that day has none, its places (names joined, at most :data:`MAX_TEXT` characters, the
  column's own limit) where the day has no place, and with the place the tour day's IANA zone. Text the agency wrote
  is never replaced and no day is added (a tour that runs past the version's last day fills the days there are); the
  agency edits the copied text like its own. A block that offers two or more different whole tours as alternatives
  (the client may pick any) copies none: adding the second clears the text the first copied (a title or place still
  equal to its tour day's; the zone stays), so the proposal never shows a tour the client may not take; once the
  client's pick is confirmed, the portal shows that tour's days (:func:`portal_days`). The same tour twice (two
  prices) counts once. Removing a tour (or its block) clears its copied text; when one whole tour is left, its days
  are copied.
- :func:`portal_days`: the traveller portal's days for a day of the accepted version that has neither a title nor a
  place (a version accepted before the copy existed, a block of alternative tours, or a day the agency left empty):
  the day of a live AA tour line of the trip shown to travellers that falls on that date. A day with a title or a
  place is the agency's and is shown as it is (a field the agency cleared stays empty).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from app.proposals import rules
from app.tenancy import AnyContext

# crm.proposal_day title / place: 1..120 characters, no control character (proposal_day_text_chk).
MAX_TEXT = 120
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# crm.proposal_day tz (proposal_day_tz_chk).
_ZONE = re.compile(r"^(UTC|[A-Z][A-Za-z_]+(/[A-Za-z0-9_+-]+){1,2})$")

TOUR_DAYS_SQL = """
SELECT body -> 'days' AS days FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = $1
"""
# The days of the block's version from the block's day on (never the Before you go day 0), with the tour day each takes.
DAYS_SQL = """
SELECT d.proposal_day_id, d.day_no - s.day_no + 1 AS tour_day_no, d.title, d.place, d.tz
  FROM crm.option_block b
  JOIN crm.proposal_day s ON s.proposal_day_id = b.proposal_day_id
  JOIN crm.proposal_day d ON d.proposal_id = b.proposal_id AND d.day_no >= s.day_no AND d.deleted_at IS NULL
 WHERE b.option_block_id = $1 AND s.day_no >= 1
 ORDER BY d.day_no
"""
FILL_SQL = "UPDATE crm.proposal_day SET title = $2, place = $3, tz = $4 WHERE proposal_day_id = $1"
# The block's live AA options of a whole tour, in the block's order.
TOURS_SQL = """
SELECT aa_tour_source_id FROM crm.option
 WHERE option_block_id = $1 AND source = 'aa' AND aa_component_kind = 'trip_plan' AND deleted_at IS NULL
 ORDER BY sort_order, option_id
"""
# The trip's live AA tour lines shown to travellers, with their tour's days (portal fallback).
LINES_SQL = """
SELECT l.service_from, c.body -> 'days' AS days
  FROM crm.booking_line l
  JOIN crm.aa_catalog_tour c ON c.source_system = 'cis' AND c.source_id = l.aa_tour_source_id
 WHERE l.trip_id = $1 AND l.supplier_kind = 'aa' AND l.aa_component_kind = 'trip_plan' AND l.visible_to_travelers
   AND l.status <> 'cancelled' AND l.deleted_at IS NULL AND l.service_from IS NOT NULL
 ORDER BY l.service_from, l.booking_line_id
"""


@dataclass(frozen=True)
class TourDay:
    day_no: int
    title: str | None
    place: str | None
    tz: str | None


def clean_text(value: Any) -> str | None:
    """One line of at most :data:`MAX_TEXT` characters (an ellipsis when cut), or None when empty or not text."""
    if not isinstance(value, str):
        return None
    text = " ".join(_CONTROL.sub(" ", value).split())
    if not text:
        return None
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1].rstrip() + "…"


def place_text(places: Any) -> str | None:
    """The day's place names in order, each once, joined by ", " while they fit :data:`MAX_TEXT` (the first name is
    cut to fit when it alone is longer)."""
    names: list[str] = []
    for place in places if isinstance(places, list) else []:
        name = clean_text(place.get("name")) if isinstance(place, dict) else None
        if name and name not in names:
            names.append(name)
    out: str | None = None
    for name in names:
        joined = name if out is None else f"{out}, {name}"
        if len(joined) > MAX_TEXT:
            break
        out = joined
    return out


def _zone(value: Any) -> str | None:
    return value if isinstance(value, str) and _ZONE.match(value) and rules.valid_tz(value) else None


def tour_days(raw: Any) -> dict[int, TourDay]:
    """The served body's ``days`` by day number (1..); a malformed day is skipped, never an error."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    out: dict[int, TourDay] = {}
    for day in raw if isinstance(raw, list) else []:
        if not isinstance(day, dict):
            continue
        number = day.get("day_no")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1 or number in out:
            continue
        out[number] = TourDay(number, clean_text(day.get("title")), place_text(day.get("places")),
                              _zone(day.get("time_zone")))  # fmt: skip
    return out


def filled(day: dict[str, Any], tour_day: TourDay | None) -> tuple[str | None, str | None, str] | None:
    """``(title, place, tz)`` of a proposal day once the tour day fills its empty fields, or None when nothing
    changes."""
    if tour_day is None:
        return None
    title = day["title"] or tour_day.title
    place = day["place"] or tour_day.place
    tz = tour_day.tz if not day["place"] and tour_day.place and tour_day.tz else day["tz"]
    if (title, place, tz) == (day["title"], day["place"], day["tz"]):
        return None
    return title, place, tz


def cleared(day: dict[str, Any], tour_day: TourDay | None) -> tuple[str | None, str | None, str] | None:
    """``(title, place, tz)`` of a proposal day once the text this tour day copied is taken out again (a title or place
    still equal to the tour day's; the zone stays: the column is required), or None when nothing changes."""
    if tour_day is None:
        return None
    title = None if day["title"] is not None and day["title"] == tour_day.title else day["title"]
    place = None if day["place"] is not None and day["place"] == tour_day.place else day["place"]
    if (title, place) == (day["title"], day["place"]):
        return None
    return title, place, day["tz"]


async def _apply(ctx: AnyContext, block_id: uuid.UUID, source_id: str, change: Any) -> int:
    days = tour_days(await ctx.conn.fetchval(TOUR_DAYS_SQL, source_id))
    if not days:
        return 0
    changed = 0
    for row in await ctx.conn.fetch(DAYS_SQL, block_id):
        values = change(dict(row), days.get(row["tour_day_no"]))
        if values is not None:
            await ctx.conn.execute(FILL_SQL, row["proposal_day_id"], *values)
            changed += 1
    return changed


async def fill_days(ctx: AnyContext, block_id: uuid.UUID, source_id: str) -> int:
    """Copy the AA tour's days into the empty fields of the block's days (see the module text); how many days
    changed."""
    return await _apply(ctx, block_id, source_id, filled)


async def clear_days(ctx: AnyContext, block_id: uuid.UUID, source_id: str) -> int:
    """Take the text this AA tour copied out of the block's days again (see :func:`cleared`); how many days changed."""
    return await _apply(ctx, block_id, source_id, cleared)


async def _tours(ctx: AnyContext, block_id: uuid.UUID) -> list[str]:
    return [r["aa_tour_source_id"] for r in await ctx.conn.fetch(TOURS_SQL, block_id)]


async def after_add(ctx: AnyContext, block_id: uuid.UUID, source_id: str, component_kind: str) -> int:
    """After AA option ``source_id`` was added to the block: a whole tour that is the block's only one fills the days;
    a second, different whole tour clears what every whole tour of the block copied; the same tour again (another
    price) and a part of a tour (a room, a transfer) leave the days as they are. Runs in the builder's transaction
    (the draft locked)."""
    if component_kind != "trip_plan":
        return 0
    tours = await _tours(ctx, block_id)
    if tours.count(source_id) > 1:
        return 0
    distinct = list(dict.fromkeys(tours))
    if len(distinct) == 1:
        return await fill_days(ctx, block_id, distinct[0])
    return sum([await clear_days(ctx, block_id, tour) for tour in distinct])


async def after_delete(ctx: AnyContext, block_id: uuid.UUID, option: dict[str, Any]) -> int:
    """After an option was removed from the block: a removed whole tour that the block no longer offers takes its
    copied text with it; when one whole tour is left, its days are copied."""
    if option.get("source") != "aa" or option.get("aa_component_kind") != "trip_plan":
        return 0
    removed, tours = option["aa_tour_source_id"], list(dict.fromkeys(await _tours(ctx, block_id)))
    if removed in tours:
        return 0
    changed = await clear_days(ctx, block_id, removed)
    return changed + (await fill_days(ctx, block_id, tours[0]) if len(tours) == 1 else 0)


async def before_block_delete(ctx: AnyContext, block_id: uuid.UUID) -> int:
    """A block about to be deleted takes the text its whole tours copied with it."""
    return sum([await clear_days(ctx, block_id, tour) for tour in dict.fromkeys(await _tours(ctx, block_id))])


async def portal_days(ctx: AnyContext, trip_id: uuid.UUID) -> dict[dt.date, TourDay]:
    """The AA tour day of each date the trip's live AA tour lines shown to travellers cover (the earliest line first
    when two cover one date)."""
    out: dict[dt.date, TourDay] = {}
    for row in await ctx.conn.fetch(LINES_SQL, trip_id):
        for number, day in tour_days(row["days"]).items():
            out.setdefault(row["service_from"] + dt.timedelta(days=number - 1), day)
    return out
