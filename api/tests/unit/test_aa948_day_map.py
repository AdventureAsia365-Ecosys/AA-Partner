"""AA-948 (card CRM-20): the builder map's points per proposal day, from the version's served whole AA tours
(:mod:`app.proposals.day_map`). Pure rules plus the read with a fake connection; the database run is
``tests/integration/test_aa948_builder_map_db.py``. Invented places only."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from app.proposals import day_map

DAYS = [
    {"day_no": 1, "places": [{"name": "Juniper Pass", "lat": 27.47, "lng": 89.64, "precision": "point"}]},
    {"day_no": 2, "places": [
        {"name": "Mosswater", "lat": 27.5, "lng": 89.7, "precision": "area"},
        {"name": "Owl Ridge", "lat": None, "lng": 89.8},  # no point: no pin
    ]},
    {"day_no": 3, "places": []},  # a day without places: no pin
]  # fmt: skip


def _tour(start: int, days: Any = DAYS, source_id: str = "T-1") -> dict[str, Any]:
    return {"start_day": start, "source_id": source_id, "days": days}


def test_a_place_needs_a_finite_point_in_range() -> None:
    assert day_map.place_point({"name": " Juniper Pass ", "lat": 27.47, "lng": 89.64, "precision": "point"}) == {
        "name": "Juniper Pass",
        "lat": 27.47,
        "lng": 89.64,
        "approximate": False,
    }
    assert day_map.place_point({"name": "x", "lat": 10, "lng": 20})["approximate"] is True  # no precision: approximate
    for bad in (
        {"lat": 91, "lng": 0},
        {"lat": 0, "lng": -181},
        {"lat": float("nan"), "lng": 0},
        {"lat": True, "lng": 0},
        {"lat": "10", "lng": 0},
        {"lng": 0},
        "not a place",
    ):
        assert day_map.place_point(bad) is None
    assert day_map.place_point({"name": None, "lat": 1, "lng": 2})["name"] == ""


def test_tour_days_reads_the_cached_body_and_skips_malformed_days() -> None:
    raw = [*DAYS, {"day_no": 1, "places": []}, {"day_no": 0}, {"day_no": True}, "x", {"day_no": 4, "places": "x"}]
    got = day_map.tour_days(raw)
    assert sorted(got) == [1, 2, 3, 4]
    assert [p["name"] for p in got[1]] == ["Juniper Pass"]  # the first day 1 wins
    assert [p["name"] for p in got[2]] == ["Mosswater"]
    assert got[3] == [] and got[4] == []
    assert day_map.tour_days(json.dumps(DAYS))[1][0]["lat"] == 27.47
    assert day_map.tour_days("not json") == {} and day_map.tour_days(None) == {}


@pytest.mark.parametrize(
    ("day_no", "names"),
    [
        (0, []),  # Before you go: never a pin
        (1, []),  # before the tour starts: no pin (the map never guesses)
        (2, ["Juniper Pass"]),  # tour day 1
        (3, ["Mosswater"]),  # tour day 2
        (4, []),  # tour day 3 has no places
        (5, []),  # past the tour
    ],
)
def test_a_day_takes_the_tour_day_only_while_the_tour_runs(day_no: int, names: list[str]) -> None:
    assert [p["name"] for p in day_map.day_points(day_no, [_tour(2)])] == names


def test_two_tours_on_one_day_give_both_tours_places_once() -> None:
    other = [{"day_no": 1, "places": [{"name": "Cedar Lake", "lat": 27.0, "lng": 90.0, "precision": "point"}]}]
    same = [{"day_no": 1, "places": [DAYS[0]["places"][0]]}]
    got = day_map.day_points(1, [_tour(1), _tour(1, other, "T-2"), _tour(1, same, "T-3")])
    assert [p["name"] for p in got] == ["Juniper Pass", "Cedar Lake"]


def test_a_tour_without_cached_days_or_start_draws_nothing() -> None:
    assert day_map.day_points(1, [_tour(1, None)]) == []
    assert day_map.day_points(1, [{"start_day": None, "days": DAYS}]) == []
    assert day_map.day_points(1, []) == []


class _Conn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return self.rows


class _Ctx:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn


@pytest.mark.asyncio
async def test_tours_reads_served_tours_and_parses_their_days_once() -> None:
    conn = _Conn([{"start_day": 1, "source_id": "T-1", "days": json.dumps(DAYS)}, {"start_day": 3, "days": None}])
    pid = uuid.UUID("00000000-0000-4000-8000-000000000948")
    got = await day_map.tours(_Ctx(conn), pid)  # type: ignore[arg-type]
    assert conn.calls[0][1] == (pid,)
    sql = conn.calls[0][0]
    assert "served_state = 'served'" in sql and "aa_component_kind = 'trip_plan'" in sql and "d.day_no >= 1" in sql
    assert [p["name"] for p in got[0]["_days"][1]] == ["Juniper Pass"]
    assert got[1]["_days"] == {}  # paused, retired or not cached: no pin
    assert [p["name"] for p in day_map.day_points(2, got)] == ["Mosswater"]


@pytest.mark.asyncio
async def test_with_points_adds_the_points_to_every_day_of_the_view() -> None:
    conn = _Conn([{"start_day": 1, "source_id": "T-1", "days": DAYS}])
    days: list[dict[str, Any]] = [{"day_no": 0}, {"day_no": 1}, {"day_no": 2}, {"day_no": 4}]
    pid = uuid.UUID("00000000-0000-4000-8000-000000000948")
    got = await day_map.with_points(_Ctx(conn), pid, days)  # type: ignore[arg-type]
    assert got is days
    assert [[p["name"] for p in d["map_points"]] for d in days] == [[], ["Juniper Pass"], ["Mosswater"], []]
    assert len(conn.calls) == 1  # one read per view, whatever the number of days
