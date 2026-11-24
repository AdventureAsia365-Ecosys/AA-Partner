"""AA-1152 follow-up (S292, catalog.http.v1 1.4) on PostgreSQL as crm_app: a tour body that CIS serves with
``days[].overnight_destination_id`` is stored by the catalog cache refresh as served (the client's ``get_tour`` and
the sync's ``store.upsert``), and the "AA add-on" rule then allows the tour day's overnight place even when it is none
of the day's stops: the drawer lists its add-on and the add route takes it on that tour. Invented tours and places
only."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.catalog import store
from app.catalog.client import CatalogClient, summary
from app.proposals import aa_addon_places, aa_addons
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.aa_fakes import FakeBooking, FakeCis, booking_client, catalog_config
from tests.integration.conftest import OWNER
from tests.integration.test_builder_api import _aa, _block, _create, _pid, _setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TOUR = "T-S292-overnight"
BAY = "c2920000-0000-4000-8000-0000000000b1"
HILL = "c2920000-0000-4000-8000-0000000000b2"
COVE = "c2920000-0000-4000-8000-0000000000b3"
LODGE = "c2920000-0000-4000-8000-0000000000b4"  # the night of day 2: none of the day's stops
FAR = "c2920000-0000-4000-8000-0000000000b9"
SV_LODGE = "sv:c2920000-0000-4000-8000-0000000000e1"
SV_FAR = "sv:c2920000-0000-4000-8000-0000000000e2"
ZONE = "Asia/Thimphu"
DAYS = [
    {"day_no": 1, "title": "Arrive at Lantern Bay", "time_zone": ZONE, "overnight_destination_id": BAY,
     "places": [{"destination_id": BAY, "name": "Lantern Bay", "time_zone": ZONE}]},
    {"day_no": 2, "title": "Harbour Hill walk", "time_zone": ZONE, "overnight_destination_id": LODGE.upper(),
     "places": [{"destination_id": HILL, "name": "Harbour Hill", "time_zone": ZONE}]},
    {"day_no": 3, "title": "Kelp Cove", "time_zone": ZONE, "overnight_destination_id": None,
     "places": [{"destination_id": COVE, "name": "Kelp Cove", "time_zone": ZONE}]},
]  # fmt: skip
BODY = {"source_system": "cis", "source_id": TOUR, "title": "Lantern coast 3 days", "country_code": "BT",
        "duration_days": 3, "content_hash": "sha256:s292", "days": DAYS, "inclusions": [], "exclusions": [],
        "facets": {"activities": []}, "photos": []}  # fmt: skip


@pytest_asyncio.fixture
async def tour(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> AsyncIterator[str]:
    """The tour as the hourly refresh stores it: CIS answers the 1.4 body, the sync's write keeps it as served."""
    answer = await CatalogClient(catalog_config(FakeCis(tours={TOUR: BODY}))).get_tour(TOUR)
    assert answer.state == "served" and answer.body is not None
    await store.upsert(app_pool, summary(answer.body), answer.etag, answer.body)
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
        self.inner = booking_client(fake)

    async def addons(self, tenant_id: uuid.UUID, query: dict[str, str]) -> dict[str, Any]:
        return {"currency": query["currency"], "items": [_addon(SV_LODGE, LODGE, "Lodge night walk"),
                                                         _addon(SV_FAR, FAR, "Faraway kayak")]}  # fmt: skip

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def test_the_refresh_stores_the_overnight_and_the_rule_reads_it(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool, tour: str
) -> None:
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        stored = await admin.fetchval(
            "SELECT body -> 'days' -> 1 ->> 'overnight_destination_id' FROM crm.aa_catalog_tour WHERE source_id = $1",
            tour,
        )
    assert stored == LODGE.upper()  # the cache keeps CIS's body as served
    s = await _setup(app_pool)  # a five-day trip with a block on days 1-4
    v = await _create(app_pool, s)
    v = await _aa(app_pool, s, FakeBooking(b2b_price="900.00"), v, tour_source_id=tour, sell_amount="1500")
    for day_no, expected in ((1, {BAY}), (2, {HILL, LODGE}), (3, {COVE}), (4, {COVE})):
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
            block = uuid.UUID(_block(v, day_no)["option_block_id"])
            got = await aa_addon_places.of_block(ctx, _pid(v), block, aa_addons.TOUR_SQL)
        assert got == aa_addon_places.DayPlaces(tour, frozenset(expected)), day_no


async def test_the_drawer_and_the_add_route_take_the_add_on_at_the_nights_place(
    app_pool: asyncpg.Pool, tour: str
) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking(b2b_price="900.00")
    v = await _create(app_pool, s)
    v = await _aa(app_pool, s, fake, v, tour_source_id=tour, sell_amount="1500")
    client = AddonBooking(fake)
    block = uuid.UUID(_block(v, 2)["option_block_id"])
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        day = await aa_addons.day_of(ctx, _pid(v), block)
    listed = aa_addons.listing(await aa_addons.day_offered(client, s["tenant"], day), day)  # type: ignore[arg-type]
    assert [i["component_id"] for i in listed["items"]] == [SV_LODGE] and listed["no_tour_place"] is False

    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        planned = await aa_addons.plan(ctx, _pid(v), v["edit_seq"], block, SV_LODGE, "activity")
    assert planned["tour_source_id"] == tour and sorted(planned["tour_places"]) == sorted([HILL, LODGE])
    planned, answer = await aa_addons.quote(client, s["tenant"], planned)  # type: ignore[arg-type]
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        v = await aa_addons.add(ctx, Vault(s["keys"], ctx), planned, answer, {})
    added = next(o for o in _block(v, 2)["options"] if SV_LODGE in str(o))
    assert added["source"] == "aa" and added["title"].startswith("Lodge night walk")
