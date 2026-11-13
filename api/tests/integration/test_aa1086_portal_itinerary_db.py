"""AA-1086 follow-up (S279 rehearsal REH-10-portal) on PostgreSQL with the RDS role shape, as crm_app: the traveller
portal shows the trip's AA tour (tech lead decision 13/11/2026). Invented tours and places only.

- **Builder**: adding a whole AA tour (``trip_plan``) to a block of a draft copies the tour's day titles and places
  (and the place's zone) from the catalog cache into that day and the following ones; text the agency wrote stays, the
  Before you go day and the days past the tour stay as they were; the agency still edits the copied text.
- **Default shown to travellers**: the Booking sync's AA lines of an accepted version are hidden while held and shown
  once Booking confirms them (``BOOKED_SQL``); the portal then lists them (title and dates, never a price).
- **Portal fallback**: a dated day of the accepted version without a title or place takes the AA tour day of a shown
  AA tour line on that date.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.portal import person
from app.proposals import builder
from app.tenancy import tenant_tx
from tests.aa_fakes import FakeBooking
from tests.integration.conftest import OWNER
from tests.integration.test_builder_api import _aa, _as, _create, _pid, _setup
from tests.integration.test_client_proposal_api import _raw
from tests.integration.test_proposal_confirm_api import _accepted, _pay_deposit, _sync

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TOUR, TOUR_B = "T-1086", "T-1086b"
DAYS = [
    {"day_no": 1, "title": "Arrival at Juniper Pass", "time_zone": "Asia/Thimphu",
     "places": [{"destination_id": "p1", "name": "Juniper Pass", "time_zone": "Asia/Thimphu"}]},
    {"day_no": 2, "title": "Mosswater gorge walk", "time_zone": "Asia/Thimphu",
     "places": [{"destination_id": "p2", "name": "Mosswater", "time_zone": "Asia/Thimphu"},
                {"destination_id": "p3", "name": "Owl Ridge", "time_zone": "Asia/Thimphu"}]},
    {"day_no": 3, "title": "Back to Juniper Pass", "time_zone": "Asia/Thimphu",
     "places": [{"destination_id": "p1", "name": "Juniper Pass", "time_zone": "Asia/Thimphu"}]},
]  # fmt: skip


async def _tour(pool: asyncpg.Pool, source_id: str, days: list[dict[str, Any]]) -> None:
    body = {"source_system": "cis", "source_id": source_id, "title": "Juniper Pass 3 days", "country_code": "BT",
            "duration_days": len(days), "content_hash": "sha256:test", "days": days, "inclusions": [],
            "exclusions": [], "facets": {}}  # fmt: skip
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state, body) "
            "VALUES ($1, 'Juniper Pass 3 days', 'BT', $2, 'served', $3::jsonb) "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served', body = EXCLUDED.body",
            source_id,
            len(days),
            json.dumps(body),
        )


@pytest_asyncio.fixture
async def tidy(admin: asyncpg.Connection) -> AsyncIterator[list[uuid.UUID]]:
    """Later modules count the catalog's tours and the agencies the Booking sync calls: the tours this module wrote
    (T-950 too: the accepted-version helper writes it, and every module that uses it writes it again) and the feed rows
    of its agencies go when a test ends (as the owner: crm_app may not delete either)."""
    tenants: list[uuid.UUID] = []
    yield tenants
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute(
            "DELETE FROM crm.aa_catalog_tour WHERE source_id = ANY($1::text[])", [TOUR, TOUR_B, "T-950"]
        )
        for tenant in tenants:
            await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
            await admin.execute("DELETE FROM crm.booking_feed_state")


def _days(v: dict[str, Any]) -> dict[int, tuple[Any, Any, Any]]:
    return {d["day_no"]: (d["title"], d["place"], d["tz"]) for d in v["days"]}


async def test_a_whole_aa_tour_fills_the_empty_days_of_the_draft(app_pool: asyncpg.Pool, tidy: list[uuid.UUID]) -> None:
    await _tour(app_pool, TOUR, DAYS)
    s = await _setup(app_pool)  # a Nepal trip: five days in Asia/Kathmandu, a lodging block on days 1-4
    tidy.append(s["tenant"])
    v = await _create(app_pool, s)
    day2 = next(d for d in v["days"] if d["day_no"] == 2)
    v = await _as(app_pool, s, "agent", builder.update_day, _pid(v), v["edit_seq"], uuid.UUID(day2["proposal_day_id"]),
                  {"title": "Our own gorge day"})  # fmt: skip
    before = _days(v)
    v = await _aa(app_pool, s, FakeBooking(b2b_price="900.00"), v, tour_source_id=TOUR, sell_amount="1500")
    after = _days(v)
    assert after[1] == ("Arrival at Juniper Pass", "Juniper Pass", "Asia/Thimphu")
    assert after[2] == ("Our own gorge day", "Mosswater, Owl Ridge", "Asia/Thimphu")  # the agency's title stays
    assert after[3] == ("Back to Juniper Pass", "Juniper Pass", "Asia/Thimphu")
    assert after[0] == before[0] and after[4] == before[4] and after[5] == before[5]  # Before you go, past the tour
    # The agency edits the copied text like its own.
    day1 = next(d for d in v["days"] if d["day_no"] == 1)
    v = await _as(app_pool, s, "agent", builder.update_day, _pid(v), v["edit_seq"], uuid.UUID(day1["proposal_day_id"]),
                  {"title": "Fly in to Juniper Pass", "place": None})  # fmt: skip
    assert _days(v)[1] == ("Fly in to Juniper Pass", None, "Asia/Thimphu")


async def test_confirmed_aa_lines_and_their_tour_days_reach_the_portal(
    app_pool: asyncpg.Pool, tidy: list[uuid.UUID]
) -> None:
    s = await _accepted(app_pool)  # two AA day tours (days 2 and 3) of an accepted builder version, held
    tidy.append(s["tenant"])
    s["fake"].confirm_line_status = "confirmed"
    await _sync(app_pool, s)
    held = await _raw(app_pool, s, "SELECT status, visible_to_travelers AS shown FROM crm.booking_line "
                      "WHERE trip_id = $1 AND supplier_kind = 'aa'", s["trip"])  # fmt: skip
    assert {(r["status"], r["shown"]) for r in held} == {("planning", False)}  # held, not confirmed: not shown yet
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        assert await person.services(ctx, s["trip"]) == []
    await _pay_deposit(app_pool, s)
    await _sync(app_pool, s)
    lines = await _raw(app_pool, s, "SELECT booking_line_id, title, status, visible_to_travelers AS shown, "
                       "service_from FROM crm.booking_line WHERE trip_id = $1 AND supplier_kind = 'aa' "
                       "ORDER BY service_from, booking_line_id", s["trip"])  # fmt: skip
    assert len(lines) == 2 and {(r["status"], r["shown"]) for r in lines} == {("confirmed", True)}
    # The day-2 line's tour has days in the catalog cache (the job writes AA lines: members never do, CRM24).
    await _tour(app_pool, TOUR_B, DAYS[:2])
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        await conn.execute("UPDATE crm.booking_line SET aa_tour_source_id = $2 WHERE booking_line_id = $1",
                           lines[0]["booking_line_id"], TOUR_B)  # fmt: skip
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        services = await person.services(ctx, s["trip"])
        itinerary = await person.itinerary(ctx, s["trip"])
    assert sorted(x["title"] for x in services) == sorted(r["title"] for r in lines)
    assert all("price" not in str(x) and x["status"] == "confirmed" for x in services)
    by_date = {d["date"]: d for d in itinerary}
    first = lines[0]["service_from"]
    day_a, day_b = by_date[first.isoformat()], by_date[(first + dt.timedelta(days=1)).isoformat()]
    assert (day_a["title"], day_a["place"]) == ("Arrival at Juniper Pass", "Juniper Pass")
    assert (day_b["title"], day_b["place"]) == ("Mosswater gorge walk", "Mosswater, Owl Ridge")
    assert all(d["title"] is None for d in itinerary if d["date"] not in (day_a["date"], day_b["date"]))
