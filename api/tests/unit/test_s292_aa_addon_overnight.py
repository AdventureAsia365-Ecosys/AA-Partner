"""AA-1152 follow-up (S292, CIS catalog.http.v1 1.4): the "AA add-on" rule also allows the tour day's overnight
destination (``days[].overnight_destination_id``), which may be none of the day's stops; a body cached before 1.4 (no
key) keeps its stops only. The catalog client hands the 1.4 body to the cache unchanged. The SQL on PostgreSQL is in
tests/integration/test_s292_aa_addon_overnight_db.py. Invented tours and places only."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.catalog.client import CatalogClient
from app.proposals import aa_addon_places as places
from tests.aa_fakes import FakeCis, catalog_config, tour_body

BAY = "d1530000-0000-4000-8000-0000000000d1"
HILL = "d1530000-0000-4000-8000-0000000000d2"
LODGE = "d1530000-0000-4000-8000-0000000000d4"

DAYS = [
    {"day_no": 1, "places": [{"destination_id": BAY}], "overnight_destination_id": BAY},
    {"day_no": 2, "places": [{"destination_id": HILL}], "overnight_destination_id": LODGE.upper()},
    {"day_no": 3, "places": [], "overnight_destination_id": None},
]  # fmt: skip


def test_the_overnight_place_joins_the_days_places() -> None:
    assert places.days_places(json.dumps(DAYS)) == {1: {BAY}, 2: {HILL, LODGE}, 3: frozenset()}


def test_a_body_cached_before_1_4_keeps_its_stops_only() -> None:
    old = [{k: v for k, v in day.items() if k != "overnight_destination_id"} for day in DAYS]
    assert places.days_places(old) == {1: {BAY}, 2: {HILL}, 3: frozenset()}


@pytest.mark.parametrize("value", [None, 7, True, "", "   ", ["x"], {"id": "x"}])
def test_a_malformed_overnight_is_skipped(value: Any) -> None:
    day = {"day_no": 1, "places": [{"destination_id": BAY}], "overnight_destination_id": value}
    assert places.days_places([day]) == {1: {BAY}}


def test_a_day_with_only_a_night_is_a_known_place() -> None:
    got = places.nearest(2, [{"source_id": "SYN-T1", "start_day": 1, "days": DAYS}])
    assert got == places.DayPlaces("SYN-T1", frozenset({HILL, LODGE}))
    assert got.allows([LODGE]) and not got.allows([BAY])
    only_night = [{"day_no": 1, "places": [], "overnight_destination_id": LODGE}]
    assert places.nearest(1, [{"source_id": "SYN-T2", "start_day": 1, "days": only_night}]) == places.DayPlaces(
        "SYN-T2", frozenset({LODGE})
    )


@pytest.mark.asyncio
async def test_the_client_hands_the_1_4_body_to_the_cache_unchanged() -> None:
    body = {**tour_body("T-1"), "days": DAYS}
    served = await CatalogClient(catalog_config(FakeCis(tours={"T-1": body}))).get_tour("T-1")
    assert served.state == "served" and served.body == body
    assert served.body["days"][1]["overnight_destination_id"] == LODGE.upper()
