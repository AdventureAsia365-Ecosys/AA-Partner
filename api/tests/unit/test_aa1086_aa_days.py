"""AA-1086 follow-up (S279 rehearsal REH-10-portal): the AA tour's days in the agency's proposal and in the traveller
portal (``app.proposals.aa_days``, ``app.portal.person.itinerary``), and the Booking sync's default "shown to
travellers" on a confirmed AA line (``app.booking.sync``). Invented tours and places only."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest

from app.booking import sync
from app.portal import person
from app.proposals import aa_days

BODY_DAYS = [
    {"day_no": 1, "title": "Arrival in Lanterna", "time_zone": "Asia/Bangkok",
     "places": [{"destination_id": "d1", "name": "Lanterna", "time_zone": "Asia/Bangkok"}]},
    {"day_no": 2, "title": "  Reed\nlake   cruise ", "time_zone": "Asia/Vientiane",
     "places": [{"destination_id": "d2", "name": "Reed Lake", "time_zone": "Asia/Vientiane"},
                {"destination_id": "d3", "name": "Copper Hill", "time_zone": "Asia/Vientiane"},
                {"destination_id": "d2", "name": "Reed Lake", "time_zone": "Asia/Vientiane"}]},
    {"day_no": 3, "title": "Free day", "places": [], "time_zone": "Not/A_Zone"},
]  # fmt: skip


class FakeConn:
    def __init__(self, *, value: Any = None, rows: list[dict[str, Any]] | None = None) -> None:
        self.value, self.rows = value, rows or []
        self.executed: list[tuple[Any, ...]] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self.value

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self.rows

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(args)
        return "UPDATE 1"


class Ctx:
    def __init__(self, conn: Any) -> None:
        self.conn = conn


# ── pure parts ───────────────────────────────────────────────────────────────────────────────────


def test_clean_text_is_one_line_within_the_column_limit() -> None:
    assert aa_days.clean_text("  Reed\nlake\t cruise\x00 ") == "Reed lake cruise"
    assert aa_days.clean_text("   ") is None and aa_days.clean_text(None) is None and aa_days.clean_text(7) is None
    long = aa_days.clean_text("x" * 300)
    assert long is not None and len(long) == aa_days.MAX_TEXT and long.endswith("…")


def test_place_text_joins_each_name_once_while_it_fits() -> None:
    places = [{"name": "Reed Lake"}, {"name": "Copper Hill"}, {"name": "Reed Lake"}, "junk", {"name": None}]
    assert aa_days.place_text(places) == "Reed Lake, Copper Hill"
    assert aa_days.place_text([]) is None and aa_days.place_text("Reed Lake") is None
    many = [{"name": f"Village number {i:02d}"} for i in range(20)]
    text = aa_days.place_text(many)
    assert text is not None and len(text) <= aa_days.MAX_TEXT and text.startswith("Village number 00, ")
    assert aa_days.place_text([{"name": "y" * 200}]) == "y" * (aa_days.MAX_TEXT - 1) + "…"


def test_tour_days_reads_the_served_body_and_skips_bad_days() -> None:
    raw = [*BODY_DAYS, {"day_no": 0, "title": "x"}, {"day_no": True, "title": "x"}, {"day_no": 1, "title": "again"},
           "junk"]  # fmt: skip
    days = aa_days.tour_days(json.dumps(raw))  # asyncpg hands jsonb over as text
    assert sorted(days) == [1, 2, 3]
    assert days[1] == aa_days.TourDay(1, "Arrival in Lanterna", "Lanterna", "Asia/Bangkok")
    assert days[2] == aa_days.TourDay(2, "Reed lake cruise", "Reed Lake, Copper Hill", "Asia/Vientiane")
    assert days[3] == aa_days.TourDay(3, "Free day", None, None)  # an unknown zone is never copied
    assert aa_days.tour_days(None) == {} and aa_days.tour_days("{not json") == {} and aa_days.tour_days({}) == {}


def test_filled_keeps_what_the_agency_wrote() -> None:
    tour = aa_days.TourDay(2, "Reed lake cruise", "Reed Lake", "Asia/Vientiane")
    empty = {"title": None, "place": None, "tz": "Asia/Bangkok"}
    assert aa_days.filled(empty, tour) == ("Reed lake cruise", "Reed Lake", "Asia/Vientiane")
    # A title the agency wrote stays; the empty place is filled with its zone.
    assert aa_days.filled({**empty, "title": "Our lake day"}, tour) == ("Our lake day", "Reed Lake", "Asia/Vientiane")
    # A place the agency wrote keeps its zone too.
    own = {"title": None, "place": "Harbour", "tz": "Asia/Bangkok"}
    assert aa_days.filled(own, tour) == ("Reed lake cruise", "Harbour", "Asia/Bangkok")
    assert aa_days.filled({"title": "A", "place": "B", "tz": "UTC"}, tour) is None
    assert aa_days.filled(empty, None) is None
    # A tour day without a place leaves the zone alone.
    assert aa_days.filled(empty, aa_days.TourDay(3, "Free day", None, None)) == ("Free day", None, "Asia/Bangkok")


# ── fill_days / portal_days on a fake connection ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fill_days_writes_only_the_days_it_changes() -> None:
    d1, d2, d3, d4 = (uuid.UUID(int=n) for n in range(1, 5))
    rows = [
        {"proposal_day_id": d1, "tour_day_no": 1, "title": None, "place": None, "tz": "Asia/Bangkok"},
        {"proposal_day_id": d2, "tour_day_no": 2, "title": "Our lake day", "place": "Harbour", "tz": "Asia/Bangkok"},
        {"proposal_day_id": d3, "tour_day_no": 3, "title": None, "place": None, "tz": "Asia/Bangkok"},
        {"proposal_day_id": d4, "tour_day_no": 4, "title": None, "place": None, "tz": "Asia/Bangkok"},  # past the tour
    ]
    conn = FakeConn(value=json.dumps(BODY_DAYS), rows=rows)
    assert await aa_days.fill_days(Ctx(conn), uuid.UUID(int=9), "T-1086", "trip_plan") == 2
    assert conn.executed == [
        (d1, "Arrival in Lanterna", "Lanterna", "Asia/Bangkok"),
        (d3, "Free day", None, "Asia/Bangkok"),
    ]


@pytest.mark.asyncio
async def test_fill_days_skips_a_part_of_a_tour_and_a_tour_without_days() -> None:
    conn = FakeConn(value=json.dumps(BODY_DAYS), rows=[{"proposal_day_id": uuid.UUID(int=1), "tour_day_no": 1,
                                                         "title": None, "place": None, "tz": "UTC"}])  # fmt: skip
    assert await aa_days.fill_days(Ctx(conn), uuid.UUID(int=9), "T-1086", "hotel_room") == 0
    assert await aa_days.fill_days(Ctx(FakeConn(value=None)), uuid.UUID(int=9), "T-1086", "trip_plan") == 0
    assert conn.executed == []


@pytest.mark.asyncio
async def test_portal_days_maps_each_tour_day_to_its_date_earliest_line_first() -> None:
    start = dt.date(2027, 3, 10)
    rows = [
        {"service_from": start, "days": json.dumps(BODY_DAYS)},
        {"service_from": start + dt.timedelta(days=1), "days": json.dumps([{"day_no": 1, "title": "Other tour"}])},
    ]
    days = await aa_days.portal_days(Ctx(FakeConn(rows=rows)), uuid.UUID(int=1))
    assert {d: v.title for d, v in days.items()} == {
        start: "Arrival in Lanterna",
        start + dt.timedelta(days=1): "Reed lake cruise",
        start + dt.timedelta(days=2): "Free day",
    }


class ItineraryConn(FakeConn):
    """ITINERARY_SQL answers the accepted days; LINES_SQL the AA tour lines."""

    def __init__(self, days: list[dict[str, Any]], lines: list[dict[str, Any]]) -> None:
        super().__init__()
        self.days, self.lines = days, lines

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self.lines if sql == aa_days.LINES_SQL else self.days


@pytest.mark.asyncio
async def test_portal_itinerary_falls_back_to_the_aa_tour_day_on_that_date() -> None:
    start = dt.date(2027, 3, 10)
    day = {"tz": "Asia/Bangkok", "starts_on": start}
    days = [
        {**day, "day_no": 0, "title": None, "place": None},
        {**day, "day_no": 1, "title": None, "place": None},
        {**day, "day_no": 2, "title": "Our lake day", "place": None},
        {**day, "day_no": 4, "title": None, "place": None},  # no tour day on that date
    ]
    lines = [{"service_from": start, "days": json.dumps(BODY_DAYS)}]
    out = await person.itinerary(Ctx(ItineraryConn(days, lines)), uuid.UUID(int=1))
    assert [(d["day_no"], d["date"], d["title"], d["place"]) for d in out] == [
        (0, None, None, None),
        (1, "2027-03-10", "Arrival in Lanterna", "Lanterna"),
        (2, "2027-03-11", "Our lake day", "Reed Lake, Copper Hill"),
        (4, "2027-03-13", None, None),
    ]
    assert out[1]["tz"] == "Asia/Bangkok" and out[1]["utc_offset"] == "+07:00"  # the version's zone stays
    assert await person.itinerary(Ctx(ItineraryConn([], lines)), uuid.UUID(int=1)) == []


# ── the Booking sync: a confirmed AA line is shown to travellers by default ─────────────────────────────────


def test_the_sync_shows_an_aa_line_when_it_becomes_confirmed() -> None:
    assert "visible_to_travelers = true" in sync.CONFIRM_LINE_SQL and "status = 'planning'" in sync.CONFIRM_LINE_SQL
    # Booking's booking answer: only the move to confirmed shows the line (a later choice to hide it is kept).
    assert "visible_to_travelers = visible_to_travelers OR ($4 = 'confirmed' AND status <> $4)" in sync.BOOKED_SQL
