"""AA-1068 end to end on PostgreSQL with the RDS role shape, as crm_app through ``tenant_tx()`` (crm-051).

- Supplier bills: record a bill of the agency's own supplier for two lines; the trip's lines show the actual cost and
  the variance, the trip P&L counts the billed cost; voiding the bill puts the planned cost back.
- The assistant is refused every money read (``view_margin``) and sees no money anywhere.
- "Bookings with AA": Booking's invoices (a fake booking.b2b.v1 1.5) are copied, linked to the AA line of the booking,
  flagged as a mismatch when billed in full at another amount, the past-due banner follows Booking's credit status;
  Booking down -> the copy with ``source = cache``; the AA invoice is then the AA cost in the trip P&L.
- Group departures: the group code, a per-departure cost split by pax over two trips, the departure P&L.
- The profit cube by lead source and by product, its CSV, and the Home "today" tiles.
- Tenant isolation: another agency sees none of it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal as D
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.errors import Conflict, Forbidden, Invalid
from app.finance import aa_invoices, agenda, bills, departures, pnl
from app.ledger import lines, suppliers
from app.permissions import Action
from app.reports import export
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.aa_fakes import FakeBooking, agency_invoice, agency_invoice_detail, booking_client, invoice_line
from tests.integration.test_crm005_intake import _agency, _keys
from tests.integration.test_crm051_schema import purge_actual_cost

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _no_actual_cost_left(admin: asyncpg.Connection) -> AsyncIterator[None]:
    yield
    await purge_actual_cost(admin)  # crm-035 re-run safety, see tests/integration/test_crm051_schema.py


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    tenant_id, people = await _agency(pool, "owner", "agent", "assistant")
    who = {role: people[i][0] for i, role in enumerate(("owner", "agent", "assistant"))}
    s: dict[str, Any] = {"tenant": tenant_id, "who": who, "keys": _keys()}
    async with tenant_tx(pool, who["agent"], tenant_id) as ctx:
        own = await suppliers.create_supplier(
            ctx, Vault(s["keys"], ctx), {"name": "Pine Hill Lodge", "category": "accommodation"}
        )
        s["supplier"] = uuid.UUID(own["agency_supplier_id"])
        s["aa"] = await ctx.conn.fetchval("SELECT crm.ensure_aa_supplier()")
    return s


async def _trip(
    pool: asyncpg.Pool, s: dict[str, Any], adults: int = 2, source: str = "website_form", start_in: int = 10
) -> uuid.UUID:
    trip = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        await conn.execute(
            "INSERT INTO crm.trip (trip_id, title, stage, waiting_on, party_adults, start_date, lead_source) "
            "VALUES ($1, 'Lakes 6 days', 'booked', 'none', $2, current_date + $4::int, $3)",
            trip,
            adults,
            source,
            start_in,
        )
    return trip


async def _confirmed_line(pool: asyncpg.Pool, s: dict[str, Any], trip: uuid.UUID, cost: str, sell: str) -> uuid.UUID:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        made = await lines.create_line(
            ctx, trip, {"agency_supplier_id": s["supplier"], "title": "Lodge 2 nights", "confirmation_no": None}
        )
        line = uuid.UUID(made["booking_line_id"])
        await lines.update_line(ctx, line, {"confirmation_no": f"PH-{uuid.uuid4().hex[:6]}"})
        await lines.put_money(
            ctx, line, {"cost": {"amount": cost, "currency": "USD"}, "sell": {"amount": sell, "currency": "USD"}}
        )
        await lines.confirm(ctx, line)
    return line


async def _aa_line(admin: asyncpg.Connection, s: dict[str, Any], trip: uuid.UUID, booking: str, cost: str) -> uuid.UUID:
    """An AA line confirmed by the Booking feed with its quote-locked cost (as crm_owner, the feed's definer path)."""
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        line = await admin.fetchval(
            "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, status, title, confirmed_at, "
            "bk_booking_id, departure_id) VALUES ($1, 'aa', $2, 'confirmed', 'Lakes tour', now(), $3, 'DEP-77') "
            "RETURNING booking_line_id",
            trip,
            s["aa"],
            booking,
        )
        await admin.execute(
            "INSERT INTO crm.booking_line_money (booking_line_id, kind, amount, currency, base_currency, amount_base, "
            "fx_rate, fx_rate_date, fx_source, fx_basis, stale_days) "
            "VALUES ($1, 'cost', $2::numeric, 'USD', 'USD', $2::numeric, 1, current_date, 'aa_quote_lock', "
            "'quote_lock', 0)",
            line,
            cost,
        )
    return line


def _tx(pool: asyncpg.Pool, who: Any, tenant: uuid.UUID) -> Any:
    return lambda: tenant_tx(pool, who, tenant, action=Action.VIEW_MARGIN)


async def test_supplier_bills_and_the_trip_profit(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    trip = await _trip(app_pool, s)
    first = await _confirmed_line(app_pool, s, trip, "300", "420")
    second = await _confirmed_line(app_pool, s, trip, "100", "130")
    agent, assistant = s["who"]["agent"], s["who"]["assistant"]
    today = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)

    with pytest.raises(Invalid) as err:  # the lines add up to more than the bill
        async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
            await bills.create_bill(
                ctx, {"agency_supplier_id": s["supplier"], "number": "PH-1", "bill_date": today, "currency": "USD",
                      "amount": "100", "lines": [{"booking_line_id": first, "amount": "330"}]},
            )  # fmt: skip
    assert err.value.code == "bill_lines_over"
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        bill = await bills.create_bill(
            ctx, {"agency_supplier_id": s["supplier"], "number": "PH-1", "bill_date": today, "currency": "USD",
                  "amount": "450", "note": "October stay",
                  "lines": [{"booking_line_id": first, "amount": "330"}, {"booking_line_id": second, "amount": "95"}]},
        )  # fmt: skip
    assert bill["status"] == "recorded" and bill["bill_cost"]["unallocated"] == "25.00"
    assert {x["line_cost"] for x in bill["lines"]} == {"330.00", "95.00"}
    with pytest.raises(Conflict) as dup:
        async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
            await bills.create_bill(
                ctx, {"agency_supplier_id": s["supplier"], "number": "PH-1", "bill_date": today, "currency": "USD",
                      "amount": "5", "lines": [{"booking_line_id": first, "amount": "5"}]},
            )  # fmt: skip
    assert dup.value.code == "duplicate_bill_number"

    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        view = await lines.trip_lines(ctx, trip)
        p = await pnl.trip_pnl(ctx, trip)
    money = {x["booking_line_id"]: x["money"] for x in view["lines"]}
    assert money[str(first)]["actual_cost"]["amount"] == "330.00"
    assert money[str(first)]["cost_variance_base"] == "30.00" and money[str(first)]["actual_margin_base"] == "90.00"
    assert money[str(second)]["cost_variance_base"] == "-5.00"
    assert p["costs"]["agency_planned_base"] == "400.00" and p["costs"]["agency_base"] == "425.00"
    assert p["costs"]["variance_base"] == "25.00" and p["counts"]["lines_billed"] == 2
    assert p["revenue"]["total_base"] == "0.00" and "no_revenue" in p["notes"]

    # The assistant: refused before any read, and its line view has no money.
    with pytest.raises(Forbidden):
        async with tenant_tx(app_pool, assistant, s["tenant"], action=Action.VIEW_MARGIN):
            pass
    async with tenant_tx(app_pool, assistant, s["tenant"]) as ctx:
        assert all("money" not in x for x in (await lines.trip_lines(ctx, trip))["lines"])

    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        paid = await bills.update_bill(ctx, uuid.UUID(bill["supplier_bill_id"]), {"status": "paid"})
        assert paid["status"] == "paid" and paid["paid_on"] is not None
        void = await bills.update_bill(ctx, uuid.UUID(bill["supplier_bill_id"]), {"status": "void"})
        assert void["status"] == "void"
        listed = await bills.list_bills(ctx, trip_id=trip)
        p = await pnl.trip_pnl(ctx, trip)
        view = await lines.trip_lines(ctx, trip)
    assert [b["number"] for b in listed["bills"]] == ["PH-1"]
    assert p["costs"]["agency_base"] == "400.00" and p["costs"]["variance_base"] is None
    assert all(x["money"].get("actual_cost") is None for x in view["lines"])


async def test_bookings_with_aa_and_the_aa_cost(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    s = await _setup(app_pool)
    trip = await _trip(app_pool, s)
    line = await _aa_line(admin, s, trip, "BK-501", "900.00")
    fake = FakeBooking()
    fake.agency_invoices = [
        agency_invoice("AAT-2031-000002", total="950.00", balance="950.00", overdue_days=12, due_date="2031-02-15"),
        agency_invoice("AAS-2031-000001", kind="saas", total="49.00", balance="0.00", status="paid"),
    ]
    fake.invoice_details["AAT-2031-000002"] = agency_invoice_detail(
        "AAT-2031-000002",
        [invoice_line(1, "deposit", "300.00", "BK-501"), invoice_line(2, "balance", "650.00", "BK-501"),
         invoice_line(3, "cancellation_fee", "40.00", "BK-999")],
        total="950.00", balance="950.00", overdue_days=12,
    )  # fmt: skip
    fake.billing = {**fake.billing, "status": "past_due", "holds_paused": True, "overdue_invoices": 1,
                    "oldest_overdue_days": 12, "overdue_amount": [{"amount": "950.00", "currency": "USD"}]}  # fmt: skip
    client = booking_client(fake)
    owner = s["who"]["owner"]
    today = dt.date(2031, 3, 1)

    page = await aa_invoices.page(_tx(app_pool, owner, s["tenant"]), client, s["tenant"], today)
    assert page["source"] == "live" and page["booking_error"] is None
    assert page["banner"]["level"] == "past_due" and page["banner"]["holds_paused"] is True
    assert [i["number"] for i in page["invoices"]] == ["AAT-2031-000002", "AAS-2031-000001"]
    settlement = page["invoices"][0]
    assert settlement["overdue"] and settlement["bookings"][0]["booking_id"] == "BK-501"
    assert settlement["bookings"][0]["lines"][0]["booking_line_id"] == str(line)
    row = page["bookings"][0]
    assert row["quoted_b2b"] == {"amount": "900.00", "currency": "USD"}
    assert row["invoiced_b2b"]["amount"] == "950.00" and row["invoiced_b2b"]["complete"]
    assert row["mismatch"] and page["mismatches"] == 1
    assert fake.calls["GET invoices"] == 2  # the page and one settlement detail; the software fee needs none

    # The AA invoice is now the line's AA cost in the trip P&L (billed in full).
    async with tenant_tx(app_pool, owner, s["tenant"]) as ctx:
        p = await pnl.trip_pnl(ctx, trip)
    assert p["costs"]["aa_quoted_base"] == "900.00" and p["costs"]["aa_base"] == "950.00"
    assert p["counts"]["aa_lines_mismatch"] == 1 and "aa_invoice_mismatch" in p["notes"]

    # A second open reads no detail again; Booking down -> the copy, the banner from it.
    await aa_invoices.page(_tx(app_pool, owner, s["tenant"]), client, s["tenant"], today)
    assert fake.calls["GET invoices"] == 3
    fake.down = True
    cached = await aa_invoices.page(_tx(app_pool, owner, s["tenant"]), client, s["tenant"], today)
    assert cached["source"] == "cache" and cached["booking_error"] == "booking_unavailable"
    assert cached["banner"]["level"] == "overdue" and cached["bookings"][0]["mismatch"]
    fake.down = False

    detail = await aa_invoices.invoice_detail(_tx(app_pool, owner, s["tenant"]), client, s["tenant"], "AAT-2031-000002")
    assert [x["crm_lines"][0]["booking_line_id"] if x["crm_lines"] else None for x in detail["lines"]] == [
        str(line), str(line), None,
    ]  # fmt: skip
    assert detail["payment_instructions"] == "Bank transfer to the invented account"

    # Another agency sees no copy.
    other = await _setup(app_pool)
    empty = await aa_invoices.page(_tx(app_pool, other["who"]["owner"], other["tenant"]), None, other["tenant"], today)
    assert empty["invoices"] == [] and empty["booking_error"] == "booking_not_configured"


async def test_group_departure_costs_profit_cube_and_agenda(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    s = await _setup(app_pool)
    first = await _trip(app_pool, s, adults=3, source="referral", start_in=-5)  # left 5 days ago
    second = await _trip(app_pool, s, adults=1)
    await _aa_line(admin, s, first, "BK-1", "600.00")  # first is on AA departure DEP-77
    owner, agent = s["who"]["owner"], s["who"]["agent"]
    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        coded = await departures.set_group_code(ctx, second, "DEP-77")  # second joins it by its group code
        assert coded["departure_refs"] == ["DEP-77"]
        cost = await departures.create_cost(
            ctx, {"departure_ref": "DEP-77", "agency_supplier_id": s["supplier"], "title": "Tour leader",
                  "amount": "200", "currency": "USD", "basis": "per_departure"},
        )  # fmt: skip
        await departures.create_cost(
            ctx, {"departure_ref": "DEP-77", "agency_supplier_id": s["supplier"], "title": "Welcome dinner",
                  "amount": "15", "currency": "USD", "basis": "per_pax"},
        )  # fmt: skip
        dep = await departures.departure_pnl(ctx, "DEP-77")
        p1, p2 = await pnl.trip_pnl(ctx, first), await pnl.trip_pnl(ctx, second)
    assert dep["totals"]["pax"] == 4 and dep["totals"]["departure_cost_base"] == "260.00"  # 200 + 15 x 4
    assert (p1["costs"]["departure_base"], p2["costs"]["departure_base"]) == ("195.00", "65.00")  # 150+45, 50+15
    with pytest.raises(Conflict):  # the supplier is named by a departure cost
        async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
            await suppliers.trash_check(ctx, s["supplier"])

    async with tenant_tx(app_pool, agent, s["tenant"]) as ctx:
        changed = await departures.update_cost(ctx, uuid.UUID(cost["departure_cost_id"]), {"amount": D("100")})
        assert changed["group_cost"]["amount"] == "100.00"
        await departures.delete_cost(ctx, uuid.UUID(cost["departure_cost_id"]))
        assert (await pnl.trip_pnl(ctx, first))["costs"]["departure_base"] == "45.00"

    async with tenant_tx(app_pool, owner, s["tenant"]) as ctx:
        by_source = await pnl.profit_cube(ctx, "12m", None, "lead_source")
        by_product = await pnl.profit_cube(ctx, "12m", None, "product")
        body, count = await export.export(ctx, "12m", None, "profit", dimension="product")
        tiles = await agenda.agenda(ctx)
    # The window (12 months to today) holds the first trip only (the second leaves in 10 days): AA 600 + its
    # departure share 45 (15 x 3), no invoice yet.
    assert [(r["key"], r["trips"], r["pax"], r["margin_base"]) for r in by_source["rows"]] == [
        ("referral", 1, 3, "-645.00"),
    ]  # fmt: skip
    assert by_source["totals"]["cost_base"] == "645.00" and by_source["totals"]["margin_pct"] is None
    assert [(r["key"], r["cost_base"]) for r in by_product["rows"]] == [("aa_tour", "600.00")]
    assert count == 2 and body.decode("utf-8-sig").splitlines()[0].startswith("product,currency,trips")
    assert tiles["next_departures"] == [
        {"departure_ref": "DEP-77", "start_date": tiles["next_departures"][0]["start_date"], "trips": 1, "pax": 1,
         "guaranteed": None},
    ]  # fmt: skip
    assert tiles["aa_overdue_invoices"] == 0 and tiles["passport_missing"] == 0  # no traveller on these trips
    async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"]) as ctx:
        assert (await agenda.agenda(ctx))["aa_overdue_invoices"] is None
        with pytest.raises(Forbidden):
            await export.export(ctx, "12m", None, "profit")
