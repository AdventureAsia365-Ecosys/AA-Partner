"""AA-948 (card CRM-20) on PostgreSQL with the RDS role shape, as crm_app: the builder view carries each day's map
points from the version's whole AA tours that AA still serves. Invented tours and places only.

- A whole AA tour added on day 1 places days 1-3 with its cached places (name, point, approximate for a non-"point"
  precision); the "Before you go" day and the days past the tour have no pin.
- A place without a point is left out; a tour day without places has no pin.
- The tour paused in the catalog cache draws nothing (only the agency's live AA tours are drawn).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.proposals import builder
from tests.aa_fakes import FakeBooking
from tests.integration.conftest import OWNER
from tests.integration.test_builder_api import _aa, _as, _create, _pid, _setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TOUR = "T-948"
DAYS = [
    {"day_no": 1, "title": "Arrival at Juniper Pass", "time_zone": "Asia/Thimphu",
     "places": [{"destination_id": "p1", "name": "Juniper Pass", "time_zone": "Asia/Thimphu",
                 "lat": 27.47, "lng": 89.64, "precision": "point"}]},
    {"day_no": 2, "title": "Mosswater gorge walk", "time_zone": "Asia/Thimphu",
     "places": [{"destination_id": "p2", "name": "Mosswater", "time_zone": "Asia/Thimphu",
                 "lat": 27.52, "lng": 89.71, "precision": "area"},
                {"destination_id": "p3", "name": "Owl Ridge", "time_zone": "Asia/Thimphu"}]},
    {"day_no": 3, "title": "Rest day", "time_zone": "Asia/Thimphu", "places": []},
]  # fmt: skip


async def _tour(pool: asyncpg.Pool, served_state: str = "served") -> None:
    body = {"source_system": "cis", "source_id": TOUR, "title": "Juniper Pass 3 days", "country_code": "BT",
            "duration_days": len(DAYS), "content_hash": "sha256:test", "days": DAYS, "inclusions": [],
            "exclusions": [], "facets": {}}  # fmt: skip
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state, body) "
            "VALUES ($1, 'Juniper Pass 3 days', 'BT', 3, $2, $3::jsonb) "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = EXCLUDED.served_state, "
            "body = EXCLUDED.body",
            TOUR,
            served_state,
            json.dumps(body),
        )


@pytest_asyncio.fixture
async def tidy(admin: asyncpg.Connection) -> AsyncIterator[None]:
    """Later modules count the catalog's tours: the tour this module wrote goes when a test ends (as the owner)."""
    yield
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("DELETE FROM crm.aa_catalog_tour WHERE source_id = $1", TOUR)


def _points(v: dict[str, Any]) -> dict[int, list[tuple[str, float, float, bool]]]:
    return {
        d["day_no"]: [(p["name"], p["lat"], p["lng"], p["approximate"]) for p in d["map_points"]] for d in v["days"]
    }


async def test_the_view_places_the_days_of_a_served_whole_tour(app_pool: asyncpg.Pool, tidy: None) -> None:
    await _tour(app_pool)
    s = await _setup(app_pool)  # five trip days and the Before you go day
    v = await _create(app_pool, s)
    assert all(d["map_points"] == [] for d in v["days"])  # no AA tour yet: no pin anywhere
    v = await _aa(app_pool, s, FakeBooking(b2b_price="900.00"), v, tour_source_id=TOUR, sell_amount="1500")
    got = _points(v)
    assert got[0] == []  # Before you go
    assert got[1] == [("Juniper Pass", 27.47, 89.64, False)]
    assert got[2] == [("Mosswater", 27.52, 89.71, True)]  # Owl Ridge has no point: left out
    assert got[3] == []  # a tour day without places
    assert got[4] == [] and got[5] == []  # past the tour
    # The page read (GET /proposals/{id}/builder) answers the same points.
    again = await _as(app_pool, s, "assistant", builder.view, _pid(v))
    assert _points(again) == got


async def test_a_paused_tour_draws_nothing(app_pool: asyncpg.Pool, tidy: None) -> None:
    await _tour(app_pool)
    s = await _setup(app_pool)
    v = await _create(app_pool, s)
    v = await _aa(app_pool, s, FakeBooking(b2b_price="900.00"), v, tour_source_id=TOUR, sell_amount="1500")
    assert _points(v)[1]
    await _tour(app_pool, "paused")
    v = await _as(app_pool, s, "agent", builder.view, _pid(v))
    assert all(d["map_points"] == [] for d in v["days"])
    assert uuid.UUID(v["proposal_id"])
