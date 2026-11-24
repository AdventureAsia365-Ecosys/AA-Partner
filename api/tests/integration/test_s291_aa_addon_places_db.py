"""AA-1152 follow-up (S291) on PostgreSQL with the RDS role shape, as crm_app: the "AA add-on" drawer's tour day
places (:mod:`app.proposals.aa_addon_places`) come from the real option, block, day and catalog cache rows, and the
add route refuses an add-on sold at none of them (409 ``aa_addon_not_on_tour``) before any quote, while one at the
tour day's place is added on that tour. Invented tours and places only."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.errors import Conflict
from app.proposals import aa_addon_places, aa_addons
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.aa_fakes import FakeBooking, booking_client
from tests.integration.conftest import OWNER
from tests.integration.test_builder_api import _aa, _block, _create, _pid, _setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TOUR = "T-S291-addon"
BAY = "c2910000-0000-4000-8000-0000000000b1"
HILL = "c2910000-0000-4000-8000-0000000000b2"
COVE = "c2910000-0000-4000-8000-0000000000b3"
FAR = "c2910000-0000-4000-8000-0000000000b9"
SV_BAY = "sv:c2910000-0000-4000-8000-0000000000e1"
SV_FAR = "sv:c2910000-0000-4000-8000-0000000000e2"
DAYS = [
    {"day_no": 1, "title": "Arrive at Lantern Bay", "time_zone": "Asia/Thimphu",
     "places": [{"destination_id": BAY, "name": "Lantern Bay", "time_zone": "Asia/Thimphu"}]},
    {"day_no": 2, "title": "Harbour Hill walk", "time_zone": "Asia/Thimphu",
     "places": [{"destination_id": HILL, "name": "Harbour Hill", "time_zone": "Asia/Thimphu"}]},
    {"day_no": 3, "title": "Kelp Cove", "time_zone": "Asia/Thimphu",
     "places": [{"destination_id": COVE, "name": "Kelp Cove", "time_zone": "Asia/Thimphu"}]},
]  # fmt: skip


@pytest_asyncio.fixture
async def tour(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> AsyncIterator[str]:
    body = {"source_system": "cis", "source_id": TOUR, "title": "Lantern coast 3 days", "country_code": "BT",
            "duration_days": 3, "content_hash": "sha256:test", "days": DAYS, "inclusions": [], "exclusions": [],
            "facets": {}}  # fmt: skip
    async with app_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state, body) "
            "VALUES ($1, 'Lantern coast 3 days', 'BT', 3, 'served', $2::jsonb) "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served', body = EXCLUDED.body",
            TOUR,
            json.dumps(body),
        )
    yield TOUR
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("DELETE FROM crm.aa_catalog_tour WHERE source_id = $1", TOUR)


def _addon(component_id: str, destination_id: str, title: str) -> dict[str, Any]:
    return {
        "component_id": component_id, "kind": "activity", "title": title, "unit": "person", "duration_minutes": 120,
        "min_pax": 1, "operating_weekdays": [1, 2, 3, 4, 5, 6, 7], "start_times": [], "first_date": "2027-01-01",
        "places": [{"destination_id": destination_id, "role": "operates", "name": "Somewhere"}],
        "b2b_price_per_person": {"amount": "20.00", "currency": "USD"},
        "rrp_per_person": {"amount": "30.00", "currency": "USD"},
    }  # fmt: skip


class AddonBooking:
    """The fake Booking of the builder tests plus Booking's add-on list (contract 1.11 ``GET /add-ons``)."""

    def __init__(self, fake: FakeBooking) -> None:
        self.inner, self.asked = booking_client(fake), 0

    async def addons(self, tenant_id: uuid.UUID, query: dict[str, str]) -> dict[str, Any]:
        self.asked += 1
        return {"currency": query["currency"], "items": [_addon(SV_BAY, HILL, "Hill lantern walk"),
                                                         _addon(SV_FAR, FAR, "Faraway kayak")]}  # fmt: skip

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def _places(pool: asyncpg.Pool, s: dict[str, Any], v: dict[str, Any], day_no: int) -> Any:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        block = uuid.UUID(_block(v, day_no)["option_block_id"])
        return await aa_addon_places.of_block(ctx, _pid(v), block, aa_addons.TOUR_SQL)


async def test_the_tour_day_places_come_from_the_real_rows(app_pool: asyncpg.Pool, tour: str) -> None:
    s = await _setup(app_pool)  # a five-day trip with a block on days 1-4
    v = await _create(app_pool, s)
    assert await _places(app_pool, s, v, 2) == aa_addon_places.DayPlaces(None, frozenset())
    v = await _aa(app_pool, s, FakeBooking(b2b_price="900.00"), v, tour_source_id=tour, sell_amount="1500")
    assert await _places(app_pool, s, v, 1) == aa_addon_places.DayPlaces(tour, frozenset({BAY}))
    assert await _places(app_pool, s, v, 2) == aa_addon_places.DayPlaces(tour, frozenset({HILL}))
    assert await _places(app_pool, s, v, 4) == aa_addon_places.DayPlaces(tour, frozenset({COVE}))  # after: last day


async def test_the_add_route_takes_only_an_add_on_of_the_tour_day(app_pool: asyncpg.Pool, tour: str) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking(b2b_price="900.00")
    v = await _create(app_pool, s)
    v = await _aa(app_pool, s, fake, v, tour_source_id=tour, sell_amount="1500")
    client = AddonBooking(fake)
    block = uuid.UUID(_block(v, 2)["option_block_id"])
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        day = await aa_addons.day_of(ctx, _pid(v), block)
    listed = aa_addons.listing(await aa_addons.day_offered(client, s["tenant"], day), day)  # type: ignore[arg-type]
    assert [i["component_id"] for i in listed["items"]] == [SV_BAY] and listed["no_tour_place"] is False

    quotes_before = fake.quotes
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        far = await aa_addons.plan(ctx, _pid(v), v["edit_seq"], block, SV_FAR, "activity")
    assert far["tour_source_id"] == tour and far["tour_places"] == [HILL]
    with pytest.raises(Conflict) as refused:
        await aa_addons.quote(client, s["tenant"], far)  # type: ignore[arg-type]
    assert refused.value.code == "aa_addon_not_on_tour" and fake.quotes == quotes_before  # no quote was asked

    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        ok = await aa_addons.plan(ctx, _pid(v), v["edit_seq"], block, SV_BAY, "activity")
    ok, answer = await aa_addons.quote(client, s["tenant"], ok)  # type: ignore[arg-type]
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        v = await aa_addons.add(ctx, Vault(s["keys"], ctx), ok, answer, {})
    added = next(o for o in _block(v, 2)["options"] if o.get("aa_component_id") == SV_BAY or SV_BAY in str(o))
    assert added["source"] == "aa" and added["title"].startswith("Hill lantern walk")
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        row = await ctx.conn.fetchrow(
            "SELECT aa_tour_source_id, aa_component_id, aa_from_price FROM crm.option WHERE option_id = $1",
            uuid.UUID(added["option_id"]),
        )
    assert row["aa_tour_source_id"] == tour and row["aa_component_id"] == SV_BAY
    assert row["aa_from_price"] == Decimal("30.00")
