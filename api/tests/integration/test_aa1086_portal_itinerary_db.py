"""AA-1086 follow-up (S279 rehearsal REH-10-portal) on PostgreSQL with the RDS role shape, as crm_app: the traveller
portal shows the trip's AA tour (tech lead decision 13/11/2026). Invented tours and places only.

- **Builder**: adding a whole AA tour (``trip_plan``) as a block's only tour copies the tour's day titles and places
  (and the place's zone) from the catalog cache into that day and the following ones; text the agency wrote stays, the
  Before you go day and the days past the tour stay as they were; the agency still edits the copied text. A second,
  different tour in the block (alternatives) clears the copied text; removing it copies the one left again.
- **Default shown to travellers**: the Booking sync's AA lines of an accepted version are hidden while held and shown
  once Booking confirms them (``BOOKED_SQL``); the portal then lists them (title and dates, never a price). The agency
  hides one (crm-065: a member changes only that column of a confirmed AA line) and a later sync keeps it hidden. The
  travellers' voucher of an AA line names the agency, never Adventure Asia.
- **Portal fallback**: a dated day of the accepted version with neither a title nor a place takes the AA tour day of a
  shown AA tour line on that date (the client's pick among alternative tours); a field the agency cleared stays empty.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.errors import Forbidden, Invalid
from app.ledger import lines as ledger
from app.portal import person
from app.proposals import aa_options, builder, service
from app.proposals import public as page
from app.tenancy import tenant_tx
from app.travel_ops import vouchers
from app.vault import Vault
from tests.aa_fakes import FakeBooking, booking_client
from tests.integration.conftest import OWNER
from tests.integration.test_builder_api import _aa, _as, _block, _create, _pid, _publishable, _setup
from tests.integration.test_client_proposal_api import PLAIN, _accept, _blk, _hold, _raw
from tests.integration.test_proposal_confirm_api import _accepted, _pay_deposit, _sync

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TOUR, TOUR_B, TOUR_X, TOUR_Y = "T-1086", "T-1086b", "T-1086x", "T-1086y"
DAYS = [
    {"day_no": 1, "title": "Arrival at Juniper Pass", "time_zone": "Asia/Thimphu",
     "places": [{"destination_id": "p1", "name": "Juniper Pass", "time_zone": "Asia/Thimphu"}]},
    {"day_no": 2, "title": "Mosswater gorge walk", "time_zone": "Asia/Thimphu",
     "places": [{"destination_id": "p2", "name": "Mosswater", "time_zone": "Asia/Thimphu"},
                {"destination_id": "p3", "name": "Owl Ridge", "time_zone": "Asia/Thimphu"}]},
    {"day_no": 3, "title": "Back to Juniper Pass", "time_zone": "Asia/Thimphu",
     "places": [{"destination_id": "p1", "name": "Juniper Pass", "time_zone": "Asia/Thimphu"}]},
]  # fmt: skip
DAYS_Y = [
    {"day_no": 1, "title": "Cedar lake crossing", "time_zone": "Asia/Thimphu",
     "places": [{"destination_id": "p7", "name": "Cedar Lake", "time_zone": "Asia/Thimphu"}]},
    {"day_no": 2, "title": "Hermit cave hike", "time_zone": "Asia/Thimphu",
     "places": [{"destination_id": "p8", "name": "Hermit Cave", "time_zone": "Asia/Thimphu"}]},
]  # fmt: skip


async def _tour(
    pool: asyncpg.Pool, source_id: str, days: list[dict[str, Any]], title: str = "Juniper Pass 3 days"
) -> None:
    body = {"source_system": "cis", "source_id": source_id, "title": title, "country_code": "BT",
            "duration_days": len(days), "content_hash": "sha256:test", "days": days, "inclusions": [],
            "exclusions": [], "facets": {}}  # fmt: skip
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state, body) "
            "VALUES ($1, $2, 'BT', $3, 'served', $4::jsonb) "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served', title = EXCLUDED.title, "
            "body = EXCLUDED.body",
            source_id,
            title,
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
            "DELETE FROM crm.aa_catalog_tour WHERE source_id = ANY($1::text[])",
            [TOUR, TOUR_B, TOUR_X, TOUR_Y, "T-950"],
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


async def test_alternative_tours_in_one_block_copy_no_days(app_pool: asyncpg.Pool, tidy: list[uuid.UUID]) -> None:
    await _tour(app_pool, TOUR, DAYS)
    await _tour(app_pool, TOUR_Y, DAYS_Y, title="Cedar Lake 2 days")
    s = await _setup(app_pool)
    tidy.append(s["tenant"])
    v = await _create(app_pool, s)
    day2 = next(d for d in v["days"] if d["day_no"] == 2)
    v = await _as(app_pool, s, "agent", builder.update_day, _pid(v), v["edit_seq"], uuid.UUID(day2["proposal_day_id"]),
                  {"title": "Our own gorge day"})  # fmt: skip
    fake = FakeBooking(b2b_price="900.00")
    v = await _aa(app_pool, s, fake, v, tour_source_id=TOUR, sell_amount="1500")
    v = await _aa(app_pool, s, fake, v, tour_source_id=TOUR, sell_amount="1700")  # the same tour, another price
    assert _days(v)[1] == ("Arrival at Juniper Pass", "Juniper Pass", "Asia/Thimphu")
    v = await _aa(app_pool, s, fake, v, tour_source_id=TOUR_Y, sell_amount="1600")  # a different tour: alternatives
    after = _days(v)
    assert (after[1][:2], after[2][:2], after[3][:2]) == ((None, None), ("Our own gorge day", None), (None, None))
    # Removing the other tour copies the one left again; removing the last clears its text.
    tour_y = next(o for o in _block(v, 1)["options"] if (o.get("aa") or {}).get("tour_source_id") == TOUR_Y)
    v = await _as(app_pool, s, "agent", builder.delete_option, _pid(v), v["edit_seq"], uuid.UUID(tour_y["option_id"]))
    assert _days(v)[1][:2] == ("Arrival at Juniper Pass", "Juniper Pass")
    assert _days(v)[2][:2] == ("Our own gorge day", "Mosswater, Owl Ridge")
    for o in [o for o in _block(v, 1)["options"] if o["source"] == "aa"]:
        v = await _as(app_pool, s, "agent", builder.delete_option, _pid(v), v["edit_seq"], uuid.UUID(o["option_id"]))
    assert (_days(v)[1][:2], _days(v)[2][:2]) == ((None, None), ("Our own gorge day", None))


async def _aa_in(pool: asyncpg.Pool, s: dict[str, Any], v: dict[str, Any], tour: str, sell: str) -> dict[str, Any]:
    body = {"tour_source_id": tour, "component_id": "tp:1", "component_kind": "trip_plan", "sell_amount": Decimal(sell)}
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        plan = await builder.aa_plan(ctx, _pid(v), v["edit_seq"], uuid.UUID(_block(v, 2, 1)["option_block_id"]), body)
    quote = await aa_options.quote(booking_client(s["fake"]), s["tenant"], plan)
    return await _as(pool, s, "agent", builder.add_aa_option, plan, quote, body)


async def _booked(pool: asyncpg.Pool, tidy: list[uuid.UUID], tours: list[str], *, pick: int = 0,
                  clear_place: bool = False) -> dict[str, Any]:  # fmt: skip
    """A builder version with an AA tour block on day 2 (one tour, or alternatives), sent, the client's pick held,
    accepted, the deposit paid and the AA line confirmed by the Booking sync."""
    s = await _setup(pool)
    tidy.append(s["tenant"])
    s["fake"] = FakeBooking(b2b_price="300.00", availability="available", strict_version_set=True)
    v = await _publishable(pool, s)
    day2 = next(d for d in v["days"] if d["day_no"] == 2)
    v = await _as(pool, s, "agent", builder.add_block, _pid(v), v["edit_seq"], uuid.UUID(day2["proposal_day_id"]),
                  {"title": "Tour", "type": "activity"})  # fmt: skip
    for i, tour in enumerate(tours):
        v = await _aa_in(pool, s, v, tour, str(500 + 100 * i))
    if clear_place:  # the agency clears the copied place of day 2
        v = await _as(pool, s, "agent", builder.update_day, _pid(v), v["edit_seq"],
                      uuid.UUID(day2["proposal_day_id"]), {"place": None})  # fmt: skip
    v = await _as(pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        sent = await service.send(ctx, Vault(s["keys"], ctx), _pid(v))
    s.update(v=v, token=sent["link_path"].rsplit("/", 1)[1])
    if pick:
        p = await page.read(pool, s["keys"], s["token"])
        block = _blk(p, "Cedar Lake 2 days")
        await page.pick(pool, s["keys"], PLAIN, s["token"], {"block_id": block["option_block_id"],
                        "option_id": block["options"][pick]["option_id"]}, origin=None)  # fmt: skip
    status, out = await _hold(pool, s)
    assert status == 200 and out["outcome"] == "held"
    assert (await _accept(pool, s, out["page"]))["status"] == "accepted"
    s["fake"].confirm_line_status = "confirmed"
    await _sync(pool, s)
    await _pay_deposit(pool, s)
    await _sync(pool, s)
    return s


async def _portal(pool: asyncpg.Pool, s: dict[str, Any]) -> dict[str, dict[str, Any]]:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        return {d["date"]: d for d in await person.itinerary(ctx, s["trip"])}


async def test_the_client_s_pick_among_alternative_tours_reaches_the_portal(
    app_pool: asyncpg.Pool, tidy: list[uuid.UUID]
) -> None:
    await _tour(app_pool, TOUR_X, DAYS)
    await _tour(app_pool, TOUR_Y, DAYS_Y, title="Cedar Lake 2 days")
    s = await _booked(app_pool, tidy, [TOUR_X, TOUR_Y], pick=1)  # the client picks the second tour
    lines = await _raw(app_pool, s, "SELECT aa_tour_source_id AS tour, status, visible_to_travelers AS shown "
                       "FROM crm.booking_line WHERE trip_id = $1 AND supplier_kind = 'aa'", s["trip"])  # fmt: skip
    assert [(r["tour"], r["status"], r["shown"]) for r in lines] == [(TOUR_Y, "confirmed", True)]
    by_date = await _portal(app_pool, s)
    day2 = (s["start"] + dt.timedelta(days=1)).isoformat()
    day3 = (s["start"] + dt.timedelta(days=2)).isoformat()
    assert (by_date[day2]["title"], by_date[day2]["place"]) == ("Cedar lake crossing", "Cedar Lake")
    assert (by_date[day3]["title"], by_date[day3]["place"]) == ("Hermit cave hike", "Hermit Cave")


async def test_a_field_the_agency_cleared_stays_empty_in_the_portal(
    app_pool: asyncpg.Pool, tidy: list[uuid.UUID]
) -> None:
    await _tour(app_pool, TOUR_X, DAYS)
    s = await _booked(app_pool, tidy, [TOUR_X], clear_place=True)
    by_date = await _portal(app_pool, s)
    day2 = (s["start"] + dt.timedelta(days=1)).isoformat()
    day3 = (s["start"] + dt.timedelta(days=2)).isoformat()
    assert (by_date[day2]["title"], by_date[day2]["place"]) == ("Arrival at Juniper Pass", None)
    assert (by_date[day3]["title"], by_date[day3]["place"]) == ("Mosswater gorge walk", "Mosswater, Owl Ridge")


async def test_the_agency_hides_a_confirmed_aa_line_and_the_sync_keeps_it_hidden(
    app_pool: asyncpg.Pool, tidy: list[uuid.UUID]
) -> None:
    await _tour(app_pool, TOUR_X, DAYS)
    s = await _booked(app_pool, tidy, [TOUR_X])
    (line,) = await _raw(app_pool, s, "SELECT booking_line_id AS id, title FROM crm.booking_line "
                         "WHERE trip_id = $1 AND supplier_kind = 'aa'", s["trip"])  # fmt: skip
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        vault = Vault(s["keys"], ctx)
        mine = (await vouchers.trip_vouchers(ctx, vault, s["trip"], travellers_only=True))["vouchers"]
        agent = (await vouchers.trip_vouchers(ctx, vault, s["trip"]))["vouchers"]
        agency = await ctx.conn.fetchval(
            "SELECT display_name AS name FROM crm.tenant WHERE tenant_id = $1", s["tenant"]
        )
    # The travellers' voucher names the agency, never Adventure Asia or an AA reference; the agent's copy does.
    (released,) = [x for x in mine if x["booking_line_id"] == str(line["id"])]
    assert released["supplier"] == {"name": agency, "category": released["supplier"]["category"],
                                    "contact_name": None, "aa_booking_ref": None, "aa_departure_id": None}  # fmt: skip
    assert "Adventure Asia" not in json.dumps(mine)
    assert next(x for x in agent if x["booking_line_id"] == str(line["id"]))["supplier"]["name"] == "Adventure Asia"
    # The agent hides the line (crm_app, a member: crm-065), and only that.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        with pytest.raises(Forbidden):
            await ledger.update_line(ctx, line["id"], {"visible_to_travelers": False, "title": "Renamed"})
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        view = await ledger.update_line(ctx, line["id"], {"visible_to_travelers": False})
    assert view["visible_to_travelers"] is False and view["title"] == line["title"]
    await _sync(app_pool, s)  # Booking answers again: the agency's choice stays
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        assert line["title"] not in [x["title"] for x in await person.services(ctx, s["trip"])]
        mine = (await vouchers.trip_vouchers(ctx, Vault(s["keys"], ctx), s["trip"], travellers_only=True))["vouchers"]
        assert str(line["id"]) not in [x["booking_line_id"] for x in mine]
        # Shown again by the agent; a planning AA line is not the agency's to show.
        assert (await ledger.update_line(ctx, line["id"], {"visible_to_travelers": True}))["visible_to_travelers"]
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await ctx.conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        await ctx.conn.execute("UPDATE crm.booking_line SET status = 'cancelled' WHERE booking_line_id = $1",
                               line["id"])  # fmt: skip
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        with pytest.raises(Invalid):
            await ledger.update_line(ctx, line["id"], {"visible_to_travelers": True})
