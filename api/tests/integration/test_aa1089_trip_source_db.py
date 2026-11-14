"""AA-1089 (Odoo study rank 48) on PostgreSQL with the RDS role shape, as crm_app: the trip page names the AA tour
and departure the trip came from (``aa_source``, app.catalog.trip_source).

- A "Book now" trip: the tour of the press, its departure and the departure date of the proposal it sent; the title
  from the catalogue cache.
- A trip with AA booking lines: the earliest line still standing (a cancelled or released line is left out); the
  public tour page link while the tour is served and the key is a UUID; none once it is retired.
- A trip without an AA tour, and another agency's trip: no block.

Invented names and places only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from app import pipeline
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.conftest import OWNER
from tests.integration.test_aa1072_book_now_api import (  # noqa: F401 — the autouse fixture locks the agencies made
    _leave_no_trace,
    _press,
    _setup,
    _submit,
)
from tests.integration.test_aa1093_tour_retired_db import _seed

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

LINE_TOUR = "3e8b1d60-7c42-4a9f-b5d1-0f6a2c9e7b84"
ORIGIN = "https://go.example.test"


@pytest_asyncio.fixture(autouse=True)
async def _drop_line_tour(admin: asyncpg.Connection) -> AsyncIterator[None]:
    yield
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute("DELETE FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = $1", LINE_TOUR)
    finally:
        await admin.execute("RESET ROLE")


async def _detail(pool: asyncpg.Pool, s: dict, trip: uuid.UUID, origin: str | None = ORIGIN) -> dict:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        return await pipeline.trip_detail(ctx, Vault(s["keys"], ctx), trip, origin)


async def _line(pool: asyncpg.Pool, s: dict, trip: uuid.UUID, *, days: int, sub: str | None, dep: str) -> uuid.UUID:
    return await _seed(
        pool, s["tenant"],
        "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, title, departure_id, service_from, "
        "service_to, aa_tour_source_id, aa_sub_status) VALUES ($1, 'aa', crm.ensure_aa_supplier(), "
        "'Fern ridge trek 4 days', $2, current_date + $3::int, current_date + $3::int + 3, $4, $5) "
        "RETURNING booking_line_id",
        trip, dep, days, LINE_TOUR, sub,
    )  # fmt: skip


async def test_a_book_now_trip_names_its_tour_and_departure(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    assert (await _submit(app_pool, s, _press(s)))["status"] == "created"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        trip = await ctx.conn.fetchval("SELECT trip_id FROM crm.trip")
    out = (await _detail(app_pool, s, trip))["aa_source"]
    start = (dt.date.today() + dt.timedelta(days=60)).isoformat()
    assert out == {
        "origin": "web_booking",
        "tour_source_id": "T-1072",
        "title": "Lantern bay overnight cruise",
        "served_state": "served",
        "departure_id": "d1",
        "departure_date": start,
        "page_url": None,  # the key of this fixture is no UUID: CIS has no page for it
    }


async def test_aa_lines_name_the_tour_and_link_its_page_while_served(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state) "
            "VALUES ($1, 'Fern ridge trek 4 days', 'VN', 4, 'served') "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served'",
            LINE_TOUR,
        )
    trip = await _seed(app_pool, s["tenant"], "INSERT INTO crm.trip (title) VALUES ('Fern ridge') RETURNING trip_id")
    bare = await _seed(app_pool, s["tenant"], "INSERT INTO crm.trip (title) VALUES ('Coast days') RETURNING trip_id")
    assert (await _detail(app_pool, s, trip))["aa_source"] is None

    await _line(app_pool, s, trip, days=10, sub="released", dep="dep-fern-a")  # Booking let it go: not a source
    cancelled = await _line(app_pool, s, trip, days=12, sub="held", dep="dep-fern-b")
    await _seed(app_pool, s["tenant"], "UPDATE crm.booking_line SET status = 'cancelled' WHERE booking_line_id = $1 "
                "RETURNING booking_line_id", cancelled)  # fmt: skip
    await _line(app_pool, s, trip, days=40, sub="quoted", dep="dep-fern-d")
    await _line(app_pool, s, trip, days=30, sub="held", dep="dep-fern-c")

    out = (await _detail(app_pool, s, trip, ORIGIN + "/"))["aa_source"]
    assert out == {
        "origin": "aa_line",
        "tour_source_id": LINE_TOUR,
        "title": "Fern ridge trek 4 days",
        "served_state": "served",
        "departure_id": "dep-fern-c",
        "departure_date": (dt.date.today() + dt.timedelta(days=30)).isoformat(),
        "page_url": f"{ORIGIN}/trip/{LINE_TOUR}",
    }
    assert (await _detail(app_pool, s, trip, None))["aa_source"]["page_url"] is None  # no client host configured
    assert (await _detail(app_pool, s, bare))["aa_source"] is None

    # Retired by AA: the tour is still named, the page link goes.
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE crm.aa_catalog_tour SET served_state = 'retired' WHERE source_id = $1", LINE_TOUR)
    out = (await _detail(app_pool, s, trip))["aa_source"]
    assert out["served_state"] == "retired" and out["page_url"] is None and out["title"] == "Fern ridge trek 4 days"

    # Another agency never sees this trip's source.
    other = await _setup(app_pool)
    async with tenant_tx(app_pool, other["who"]["agent"], other["tenant"]) as ctx:
        assert await ctx.conn.fetchrow(pipeline.trip_source.SOURCE_SQL, trip) is None
