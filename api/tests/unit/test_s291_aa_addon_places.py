"""AA-1152 follow-up (S291): the builder's "AA add-on" drawer offers, and its add route accepts, only an add-on sold at
a place of the AA tour on the block's day (the TripPlanner day cards' rule). Pure rule, the listing filter and the
409 refusal with scripted connections and a fake Booking; the SQL on PostgreSQL is in
tests/integration/test_s291_aa_addon_places_db.py. Invented tours and places only."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal as D
from typing import Any

import pytest

from app.booking import addons as booking_addons
from app.errors import Conflict
from app.proposals import aa_addon_places as places
from app.proposals import aa_addons
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, tenant_ctx

SV = "sv:7d9f8b00-0000-4000-8000-0000000000c2"
SV2 = "sv:7d9f8b00-0000-4000-8000-0000000000c3"
BAY = "d1520000-0000-4000-8000-0000000000d1"
HILL = "d1520000-0000-4000-8000-0000000000d2"
COVE = "d1520000-0000-4000-8000-0000000000d3"
DAY = dt.date(2027, 3, 2)

TOUR_DAYS = [
    {"day_no": 1, "title": "Arrive", "places": [{"destination_id": BAY, "name": "Lantern Bay"}]},
    {"day_no": 2, "title": "Ridge", "places": [{"destination_id": HILL.upper(), "name": "Harbour Hill"},
                                               {"destination_id": BAY, "name": "Lantern Bay"}]},
    {"day_no": 3, "title": "Cove", "places": [{"destination_id": COVE, "name": "Kelp Cove"}]},
]  # fmt: skip


def _tour(source_id: str, start: int, days: Any = TOUR_DAYS) -> dict[str, Any]:
    return {"source_id": source_id, "start_day": start, "days": json.dumps(days) if days is not None else None}


def test_destination_ids_have_one_spelling() -> None:
    assert places.norm(HILL.upper()) == HILL and places.norm(" Vn-Dong-Van ") == "vn-dong-van"
    assert places.norm(None) == ""


def test_days_places_skips_what_is_malformed() -> None:
    raw = [*TOUR_DAYS, {"day_no": 2, "places": []}, {"day_no": 0}, {"day_no": True}, "x",
           {"day_no": 4, "places": "x"}, {"day_no": 5, "places": [{"destination_id": None}, 7]}]  # fmt: skip
    got = places.days_places(json.dumps(raw))
    assert got == {1: {BAY}, 2: {HILL, BAY}, 3: {COVE}, 4: frozenset(), 5: frozenset()}
    assert places.days_places("not json") == {} and places.days_places(None) == {}


@pytest.mark.parametrize(
    ("day_no", "expected"),
    [(1, {BAY}), (2, {HILL, BAY}), (3, {COVE}), (5, {COVE}), (0, {BAY})],
)
def test_the_tour_day_on_the_block_day_before_and_after_the_tour(day_no: int, expected: set[str]) -> None:
    got = places.nearest(day_no, [_tour("SYN-T1", start=1)])
    assert got is not None and got.tour_source_id == "SYN-T1" and got.places == expected


def test_the_nearest_tour_wins_and_ties_join_their_places() -> None:
    other = [{"day_no": 1, "places": [{"destination_id": COVE}]}, {"day_no": 2, "places": [{"destination_id": COVE}]}]
    tours = [_tour("SYN-T1", start=1), _tour("SYN-T2", start=6, days=other)]
    assert places.nearest(7, tours) == places.DayPlaces("SYN-T2", frozenset({COVE}))
    assert places.nearest(2, tours) == places.DayPlaces("SYN-T1", frozenset({HILL, BAY}))
    # Day 5: two days after SYN-T1, one before SYN-T2: the second tour's first day.
    assert places.nearest(5, tours) == places.DayPlaces("SYN-T2", frozenset({COVE}))
    # Alternatives in one block (both start on day 1): the union, on the first added.
    alt = [_tour("SYN-T1", start=1), _tour("SYN-T2", start=1, days=other)]
    assert places.nearest(1, alt) == places.DayPlaces("SYN-T1", frozenset({BAY, COVE}))


def test_a_tour_without_cached_days_allows_nothing_and_no_tour_is_none() -> None:
    assert places.nearest(1, [_tour("SYN-T1", start=1, days=None)]) == places.DayPlaces("SYN-T1", frozenset())
    assert places.nearest(1, []) is None
    assert not places.DayPlaces("SYN-T1", frozenset()).allows([BAY])
    assert places.DayPlaces("SYN-T1", frozenset({HILL})).allows([BAY, HILL.upper()])


@pytest.mark.asyncio
async def test_of_block_reads_the_whole_tours_then_the_parts_fallback() -> None:
    proposal, block = uuid.uuid4(), uuid.uuid4()
    whole = ScriptedConn().on("SELECT d.day_no FROM", 2).on("AS start_day", [_tour("SYN-T1", start=1)])
    got = await places.of_block(tenant_ctx(whole), proposal, block, aa_addons.TOUR_SQL)
    assert got == places.DayPlaces("SYN-T1", frozenset({HILL, BAY})) and not whole.sql("SELECT o.aa_tour_source_id")
    parts = ScriptedConn().on("SELECT o.aa_tour_source_id", "SYN-T9").on("SELECT body -> 'days'", json.dumps(TOUR_DAYS))
    got = await places.of_block(tenant_ctx(parts), proposal, block, aa_addons.TOUR_SQL)
    assert got == places.DayPlaces("SYN-T9", frozenset({BAY, HILL, COVE}))
    assert await places.of_block(tenant_ctx(ScriptedConn()), proposal, block, aa_addons.TOUR_SQL) == places.DayPlaces(
        None, frozenset()
    )


def _addon(component_id: str, destination_id: str, title: str) -> dict[str, Any]:
    return {
        "component_id": component_id, "kind": "activity", "title": title, "unit": "person", "duration_minutes": 90,
        "min_pax": 1, "operating_weekdays": [1, 2, 3, 4, 5, 6, 7], "start_times": [], "first_date": DAY.isoformat(),
        "places": [{"destination_id": destination_id, "role": "operates", "name": "Somewhere"}],
        "b2b_price_per_person": {"amount": "20.00", "currency": "USD"},
        "rrp_per_person": {"amount": "30.00", "currency": "USD"},
    }  # fmt: skip


class FakeBooking:
    def __init__(self, *items: dict[str, Any]) -> None:
        self.items, self.asked = list(items), 0

    async def addons(self, tenant_id: uuid.UUID, query: dict[str, str]) -> dict[str, Any]:
        self.asked += 1
        return {"currency": "USD", "items": self.items}


def _day(**over: Any) -> dict[str, Any]:
    base = {"date": DAY, "party": 2, "currency": "USD", "place": None, "tour_source_id": "SYN-T1",
            "tour_places": frozenset({BAY}), "sees_margin": True,
            "rctx": {"markups": {}, "rules": [], "today": DAY, "round_step": D("1")}}  # fmt: skip
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_the_drawer_lists_only_the_tour_day_s_add_ons() -> None:
    client = FakeBooking(_addon(SV, BAY, "Lantern walk"), _addon(SV2, COVE, "Kelp kayak"))
    got = await aa_addons.day_offered(client, TENANT, _day())  # type: ignore[arg-type]
    assert [a.component_id for a in got] == [SV]
    out = aa_addons.listing(got, _day())
    assert [i["component_id"] for i in out["items"]] == [SV] and out["no_tour_place"] is False
    # No place known for the tour day (or no tour): Booking is not asked, the drawer says why.
    client = FakeBooking(_addon(SV, BAY, "Lantern walk"))
    assert await aa_addons.day_offered(client, TENANT, _day(tour_places=frozenset())) == []  # type: ignore[arg-type]
    assert client.asked == 0
    assert aa_addons.listing([], _day(tour_places=frozenset()))["no_tour_place"] is True
    assert aa_addons.listing([], _day(tour_source_id=None, tour_places=frozenset()))["no_tour_place"] is False


@pytest.mark.asyncio
async def test_the_add_route_refuses_an_add_on_off_the_tour_day(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = {"currency": "USD", "party": 2, "service_date": DAY.isoformat(), "component_id": SV2,
            "component_kind": "activity", "tour_source_id": "SYN-T1", "tour_places": [BAY]}  # fmt: skip
    quoted: list[Any] = []

    async def fake_quote(client: Any, tenant_id: Any, the_plan: Any) -> Any:
        quoted.append(the_plan)
        return aa_addons.aa_options.AaQuote("q1", D("40"), "on_request", dt.datetime(2027, 1, 1, tzinfo=dt.UTC), None)

    monkeypatch.setattr(aa_addons.aa_options, "quote", fake_quote)
    client = FakeBooking(_addon(SV, BAY, "Lantern walk"), _addon(SV2, COVE, "Kelp kayak"))
    with pytest.raises(Conflict) as off:
        await aa_addons.quote(client, TENANT, plan)  # type: ignore[arg-type]
    assert off.value.code == "aa_addon_not_on_tour" and off.value.status_code == 409 and quoted == []
    with pytest.raises(Conflict) as none_known:
        await aa_addons.quote(client, TENANT, {**plan, "component_id": SV, "tour_places": []})  # type: ignore[arg-type]
    assert none_known.value.code == "aa_addon_not_on_tour"
    named, answer = await aa_addons.quote(client, TENANT, {**plan, "component_id": SV})  # type: ignore[arg-type]
    assert named["title"] == "Lantern walk" and answer.from_price == D("30") and len(quoted) == 1


def test_booking_places_feed_the_check() -> None:
    walk = booking_addons.addon(_addon(SV, BAY.upper(), "Lantern walk"), "USD")
    assert places.DayPlaces("SYN-T1", frozenset({BAY})).allows([p.destination_id for p in walk.places])
