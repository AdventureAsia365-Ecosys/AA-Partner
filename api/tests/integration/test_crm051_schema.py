"""crm-051 (AA-1068, a contract file) on PostgreSQL with the RDS role shape: CRM finance.

- ``crm.v_trip_pnl``: revenue = issued invoices net of tax by kind (a cancellation fee invoice by its kind) minus
  refunds net of tax (a trip cancelled with a charge, crm-049: the charge net of tax in their place), at the FX of
  the issue / receipt day; costs = AA lines (quote, or the AA invoice once billed in full), agency lines (actual once
  billed, else planned), AA cancellation fees, the departure share; commission income; margin, margin %, per pax.
  Every number below is worked out by hand in the test.
- ``crm.supplier_bill`` / ``crm.supplier_bill_line`` and ``crm.booking_line_actual_cost``: the actual cost at the bill
  FX, the variance only while billed (a void bill puts the planned cost back), the line guard (CRM51).
- ``crm.departure_cost``: per_departure split by pax with cumulative rounding (the shares add up exactly), per_pax.
- Margin policy on every new table (the assistant reads and writes nothing), tenant isolation, the readers have
  nothing, the migration runs again as a no-op.

The services that write these rows are tested with the code (test_aa1068_finance_api.py); this file seeds with plain
SQL (money rows as crm_owner, the way the definer functions write them).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import OWNER, READERS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "051_crm_trip_finance.sql"


async def purge_actual_cost(admin: asyncpg.Connection) -> None:
    """Remove every ``actual_cost`` money row (as the erase does: crm_owner with app.erasing). crm-035 (copied, so
    immutable) re-adds the money kind CHECK without ``actual_cost`` when it runs again, and test_crm_migrations re-runs
    every contract file on this database: a row of the new kind left by a test would fail that re-run (a re-run DB
    Migrate never does on Dev). Every test that writes an actual cost calls this when it ends."""
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("SELECT set_config('app.erasing', 'on', true)")
        await admin.execute("ALTER TABLE crm.booking_line_money DISABLE ROW LEVEL SECURITY")
        await admin.execute("DELETE FROM crm.booking_line_money WHERE kind = 'actual_cost'")
        await admin.execute("ALTER TABLE crm.booking_line_money ENABLE ROW LEVEL SECURITY")


@pytest_asyncio.fixture(autouse=True)
async def _no_actual_cost_left(admin: asyncpg.Connection) -> AsyncIterator[None]:
    yield
    await purge_actual_cost(admin)


NEW_TABLES = ("supplier_bill", "supplier_bill_line", "aa_invoice_cache", "departure_cost")
DAY = dt.date(2026, 10, 1)


async def _as_owner(admin: asyncpg.Connection, tenant: uuid.UUID, sql: str, *args: Any) -> Any:
    """One statement as crm_owner inside the agency (FORCE RLS still applies to the owner)."""
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await admin.execute("SELECT set_config('app.actor', 'seed:test', true)")
        return await admin.fetchval(sql, *args)


async def _as_app(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, role: str = "owner") -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', 'member:test', true)")
        await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        return await conn.fetchval(sql, *args)


async def _pnl(pool: asyncpg.Pool, tenant: uuid.UUID, trip: uuid.UUID, role: str = "owner") -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', 'member:test', true)")
        await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        row = await conn.fetchrow("SELECT * FROM crm.v_trip_pnl WHERE trip_id = $1", trip)
        assert row is not None
        return dict(row)


async def _agency(admin: asyncpg.Connection) -> dict[str, Any]:
    tenant = uuid.uuid4()
    ids: dict[str, Any] = {"tenant": tenant}
    await _as_owner(admin, tenant, "INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant)
    ids["aa"] = await _as_owner(
        admin, tenant,
        "INSERT INTO crm.agency_supplier (kind, name, category) VALUES ('aa', 'Adventure Asia', 'dmc') "
        "RETURNING agency_supplier_id",
    )  # fmt: skip
    ids["own"] = await _as_owner(
        admin, tenant,
        "INSERT INTO crm.agency_supplier (kind, name, category) VALUES ('own', 'Lotus Lodge', 'accommodation') "
        "RETURNING agency_supplier_id",
    )  # fmt: skip
    ids["other_own"] = await _as_owner(
        admin, tenant,
        "INSERT INTO crm.agency_supplier (kind, name, category) VALUES ('own', 'River Boats', 'transport') "
        "RETURNING agency_supplier_id",
    )  # fmt: skip
    return ids


async def _trip(admin: asyncpg.Connection, tenant: uuid.UUID, adults: int, children: list[int], **kw: Any) -> uuid.UUID:
    trip = kw.get("trip_id") or uuid.uuid4()
    await _as_owner(
        admin, tenant,
        "INSERT INTO crm.trip (trip_id, title, stage, waiting_on, party_adults, party_child_ages, group_code, "
        "lost_reason_code) VALUES ($1, 'Highlands 8 days', $5, 'none', $2, $3, $4, "
        "CASE WHEN $5 = 'lost' THEN 'price' END)",
        trip, adults, children, kw.get("group_code"), kw.get("stage", "booked"),
    )  # fmt: skip
    return trip


async def _line(
    admin: asyncpg.Connection,
    tenant: uuid.UUID,
    trip: uuid.UUID,
    supplier: uuid.UUID,
    kind: str,
    status: str,
    **kw: Any,
) -> uuid.UUID:
    return await _as_owner(
        admin, tenant,
        "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, status, title, confirmed_at, "
        "bk_booking_id, departure_id) VALUES ($1, $2, $3, $4, 'Service', "
        "CASE WHEN $4 = 'confirmed' THEN now() END, $5, $6) RETURNING booking_line_id",
        trip, kind, supplier, status, kw.get("booking"), kw.get("departure"),
    )  # fmt: skip


async def _money(
    admin: asyncpg.Connection, tenant: uuid.UUID, line: uuid.UUID, kind: str, amount: str, *, frozen: bool = True
) -> None:
    if frozen:
        await _as_owner(
            admin, tenant,
            "INSERT INTO crm.booking_line_money (booking_line_id, kind, amount, currency, base_currency, amount_base, "
            "fx_rate, fx_rate_date, fx_source, fx_basis, stale_days) "
            "VALUES ($1, $2, $3::numeric, 'USD', 'USD', $3::numeric, 1, $4, 'platform_daily', 'booking_date', 0)",
            line, kind, amount, DAY,
        )  # fmt: skip
    else:
        await _as_owner(
            admin, tenant,
            "INSERT INTO crm.booking_line_money (booking_line_id, kind, amount, currency) "
            "VALUES ($1, $2, $3::numeric, 'USD')",
            line, kind, amount,
        )  # fmt: skip


_SEQ = iter(range(1, 10_000))


async def _invoice(
    admin: asyncpg.Connection,
    tenant: uuid.UUID,
    trip: uuid.UUID,
    kind: str,
    subtotal: str,
    tax: str,
    currency: str,
    status: str = "open",
) -> uuid.UUID:
    n = next(_SEQ)
    issued = status != "draft"
    return await _as_owner(
        admin, tenant,
        "INSERT INTO crm.invoice (trip_id, kind, status, currency, subtotal_amount, tax_amount, total_amount, "
        "tax_rate, "
        "tax_label, tax_inclusive, number_seq, number, issue_date, issued_at, issued_by, due_on, voided_at, voided_by, "
        "void_reason, paid_at) "
        "VALUES ($1, $2, $3, $4, $5::numeric, $6::numeric, $5::numeric + $6::numeric, 10, 'VAT', false, "
        "CASE WHEN $7 THEN $8::bigint END, CASE WHEN $7 THEN 'INV-' || lpad($8::bigint::text, 6, '0') END, "
        "CASE WHEN $7 THEN $9::date END, "
        "CASE WHEN $7 THEN now() END, CASE WHEN $7 THEN 'member:test' END, CASE WHEN $7 THEN $9::date + 14 END, "
        "CASE WHEN $3 = 'void' THEN now() END, CASE WHEN $3 = 'void' THEN 'member:test' END, "
        "CASE WHEN $3 = 'void' THEN 'wrong amount' END, CASE WHEN $3 IN ('paid', 'paid_unfulfillable') THEN now() END) "
        "RETURNING invoice_id",
        trip, kind, status, currency, subtotal, tax, issued, n, DAY,
    )  # fmt: skip


async def _rates(admin: asyncpg.Connection) -> None:
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute(
            "INSERT INTO crm.fx_rate_platform (rate_date, base, quote, source, rate, fetched_at) "
            "VALUES ($1, 'EUR', 'USD', 'ecb', 1.1, now()), ($1, 'VND', 'USD', 'ecb', 0.00004, now()) "
            "ON CONFLICT DO NOTHING",
            DAY,
        )


async def _bill(
    pool: asyncpg.Pool,
    tenant: uuid.UUID,
    supplier: uuid.UUID,
    number: str,
    amount: str,
    rate: str,
    lines: dict,
    currency: str = "USD",
    day: dt.date = DAY,
    role: str = "owner",
) -> uuid.UUID:
    """A recorded bill with its lines and the actual cost of each line, as the app writes them (one transaction)."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', 'member:test', true)")
        await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        bill = await conn.fetchval(
            "INSERT INTO crm.supplier_bill (agency_supplier_id, number, bill_date, currency, amount, base_currency, "
            "amount_base, fx_rate, fx_rate_date, fx_source, stale_days) "
            "VALUES ($1, $2, $3, $4, $5::numeric, 'USD', round($5::numeric * $6::numeric, 2), $6::numeric, $3, "
            "'platform_daily', 0) RETURNING supplier_bill_id",
            supplier, number, day, currency, amount, rate,
        )  # fmt: skip
        for line, part in lines.items():
            await conn.execute(
                "INSERT INTO crm.supplier_bill_line (supplier_bill_id, booking_line_id, amount) "
                "VALUES ($1, $2, $3::numeric)",
                bill, line, part,
            )  # fmt: skip
            await conn.fetchval("SELECT crm.booking_line_actual_cost($1)", line)
        return bill


async def test_crm051_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'crm-051'") == 1
    before = await admin.fetchval("SELECT count(*) FROM crm.status_transition")
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM crm.status_transition") == before
    assert await admin.fetchval(
        "SELECT count(*) FROM crm.status_transition WHERE table_name = 'supplier_bill'"
    ) == 3  # fmt: skip


@pytest.mark.parametrize("table", (*NEW_TABLES, "v_trip_pnl", "v_departure_trip"))
async def test_the_readers_have_nothing_on_the_new_objects(admin: asyncpg.Connection, table: str) -> None:
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege)


async def test_the_trip_profit_adds_up(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    await _rates(admin)
    a = await _agency(admin)
    t = a["tenant"]
    trip = await _trip(admin, t, 2, [8])  # 3 travellers
    second = await _trip(admin, t, 1, [])  # 1 traveller on the same AA departure

    # Revenue: an adjustment 1000 + 100 tax (USD), a service fee 50 EUR (x 1.1 = 55.00), a cancellation fee 20 USD;
    # a draft and a void invoice never count. A refund of 220 on the 1100 invoice is 200.00 net of tax.
    sale = await _invoice(admin, t, trip, "adjustment", "1000", "100", "USD")
    await _invoice(admin, t, trip, "service_fee", "50", "0", "EUR")
    await _invoice(admin, t, trip, "adjustment", "999", "0", "USD", status="draft")
    await _invoice(admin, t, trip, "adjustment", "777", "0", "USD", status="void")
    paid = await _as_owner(
        admin, t,
        "INSERT INTO crm.payment (trip_id, invoice_id, amount, currency, method, received_on) "
        "VALUES ($1, $2, 1100, 'USD', 'bank_transfer', $3) RETURNING payment_id",
        trip, sale, DAY,
    )  # fmt: skip
    await _as_owner(
        admin, t,
        "INSERT INTO crm.payment (trip_id, invoice_id, amount, currency, method, received_on, entry_kind, refund_of) "
        "VALUES ($1, $2, 220, 'USD', 'bank_transfer', $3, 'refund', $4)",
        trip, sale, DAY, paid,
    )  # fmt: skip

    # Agency lines: A confirmed (cost 300, sell 400, commission 30; billed 330), B confirmed (cost 100, not billed),
    # C planning (pipeline, not counted).
    line_a = await _line(admin, t, trip, a["own"], "agency", "confirmed")
    for kind, amount in (("cost", "300"), ("sell", "400"), ("commission_expected", "30")):
        await _money(admin, t, line_a, kind, amount)
    line_b = await _line(admin, t, trip, a["own"], "agency", "confirmed")
    await _money(admin, t, line_b, "cost", "100")
    line_c = await _line(admin, t, trip, a["own"], "agency", "planning")
    await _money(admin, t, line_c, "cost", "50", frozen=False)
    # AA lines: D confirmed on departure DEP-1 (quote 250, AA billed 260 in full: a mismatch), E cancelled (fee 40).
    line_d = await _line(admin, t, trip, a["aa"], "aa", "confirmed", booking="BK-1", departure="DEP-1")
    await _money(admin, t, line_d, "cost", "250")
    await _money(admin, t, line_d, "sell", "320")
    line_e = await _line(admin, t, trip, a["aa"], "aa", "cancelled")
    await _money(admin, t, line_e, "cancellation_cost", "40")
    await _line(admin, t, second, a["aa"], "aa", "planning", departure="DEP-1")

    before = await _pnl(app_pool, t, trip)
    assert before["agency_cost_base"] == D("400.00") and before["aa_cost_base"] == D("250.00")
    assert before["cost_variance_base"] == D("0.00") and before["lines_billed"] == 0

    await _as_app(
        app_pool, t,
        "INSERT INTO crm.aa_invoice_cache (number, agency_supplier_id, invoice_kind, aa_status, currency, "
        "total_amount, "
        "balance_amount, issued_on, due_on, booking_amounts, detail_read) "
        "VALUES ('AAT-2026-000001', $1, 'settlement', 'issued', 'USD', 260, 260, $2::date, $2::date + 7, "
        "$3::jsonb, true)",
        a["aa"], DAY, '{"BK-1": {"amount": "260.00", "complete": true}}',
    )  # fmt: skip
    await _bill(app_pool, t, a["own"], "LL-77", "400", "1", {line_a: "330"})
    # Departure DEP-1: 100 per departure (3 of 4 travellers: 75.00) and 10 per traveller (30.00).
    for amount, basis in (("100", "per_departure"), ("10", "per_pax")):
        await _as_app(
            app_pool, t,
            "INSERT INTO crm.departure_cost (departure_ref, agency_supplier_id, title, amount, currency, basis, "
            "cost_date, base_currency, amount_base, fx_rate, fx_rate_date, fx_source, stale_days) "
            "VALUES ('DEP-1', $1, 'Tour leader', $2::numeric, 'USD', $3, $4, 'USD', $2::numeric, 1, $4, "
            "'platform_daily', 0)",
            a["own"], amount, basis, DAY,
        )  # fmt: skip

    p = await _pnl(app_pool, t, trip)
    assert p["pax"] == 3 and p["base_currency"] == "USD"
    assert (p["revenue_sales_base"], p["revenue_service_fee_base"], p["refunds_base"]) == (
        D("1000.00"), D("55.00"), D("200.00"),
    )  # fmt: skip
    assert p["revenue_cancellation_fee_base"] == D("0.00") and p["revenue_unconverted"] == 0
    assert p["revenue_base"] == D("855.00")
    assert (p["aa_cost_quoted_base"], p["aa_cost_base"]) == (D("250.00"), D("260.00"))
    assert (p["aa_lines_invoiced"], p["aa_lines_mismatch"]) == (1, 1)
    assert (p["agency_cost_planned_base"], p["agency_cost_base"]) == (D("400.00"), D("430.00"))
    assert (p["cost_variance_base"], p["lines_billed"]) == (D("30.00"), 1)
    assert p["cancellation_cost_base"] == D("40.00")
    assert (p["departure_cost_base"], p["departure_costs"]) == (D("105.00"), 2)
    assert p["partner_commission_base"] == D("0.00") and p["sales_partner_id"] is None
    assert p["commission_income_base"] == D("30.00")
    assert p["total_cost_base"] == D("835.00")  # 260 + 430 + 40 + 105
    assert p["margin_base"] == D("50.00")  # 855 + 30 - 835
    assert p["margin_pct"] == D("5.85") and p["margin_per_pax_base"] == D("16.67")
    assert (p["lines_confirmed"], p["lines_planning"]) == (3, 1)
    assert (p["aa_sell_base"], p["agency_sell_base"]) == (D("320.00"), D("400.00"))
    other = await _pnl(app_pool, t, second)
    assert other["departure_cost_base"] == D("35.00")  # 25.00 + 10 x 1

    # The supplier's actual cost row: kind actual_cost, at the bill FX, basis bill_date.
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(t))
        await conn.execute("SELECT set_config('app.member_role', 'agent', true)")
        head = await conn.fetchrow(
            "SELECT amount, amount_base, fx_basis FROM crm.v_booking_line_money_current "
            "WHERE booking_line_id = $1 AND kind = 'actual_cost'",
            line_a,
        )
    assert dict(head) == {"amount": D("330.00"), "amount_base": D("330.00"), "fx_basis": "bill_date"}

    # A void bill: the line is not billed any more, the planned cost counts again (the old head stays, ignored).
    await _as_app(app_pool, t, "UPDATE crm.supplier_bill SET status = 'void' WHERE number = 'LL-77'")
    assert await _as_app(app_pool, t, "SELECT crm.booking_line_actual_cost($1)", line_a) is None
    voided = await _pnl(app_pool, t, trip)
    assert (voided["agency_cost_base"], voided["cost_variance_base"], voided["lines_billed"]) == (
        D("400.00"),
        D("0.00"),
        0,
    )

    # A cancellation fee invoice (kind of crm-050) counts by its kind, apart from the sales.
    await _invoice(admin, t, trip, "cancellation_fee", "20", "0", "USD")
    fee = await _pnl(app_pool, t, trip)
    assert (fee["revenue_cancellation_fee_base"], fee["revenue_base"]) == (D("20.00"), D("875.00"))
    assert fee["cancellation_charged"] is False


async def _charge(
    admin: asyncpg.Connection, tenant: uuid.UUID, trip: uuid.UUID, charge: str, paid: str, kept: str, **kw: Any
) -> None:
    """The cancel run's record (crm.cancellation_charge, crm-049), seeded as crm_owner (the guard lets it pass)."""
    waived = charge == "0"
    await _as_owner(
        admin, tenant,
        "INSERT INTO crm.cancellation_charge (trip_id, currency, trip_total, policy_charge, charge_amount, decision, "
        "reason, paid_amount, kept_amount, refund_due, invoiced_amount, invoice_id, created_at) "
        "VALUES ($1, $2, 1100, $3::numeric, $3::numeric, $4, $5, $6::numeric, $7::numeric, "
        "$6::numeric - $7::numeric, $8::numeric, $9, $10::timestamptz)",
        trip, kw.get("currency", "USD"), charge, "waived" if waived else "override",
        "Client moved the date" if waived else "Agreed with the client", paid, kept, kw.get("invoiced", "0"),
        kw.get("invoice"), dt.datetime(2026, 10, 1, 10, tzinfo=dt.UTC),
    )  # fmt: skip


async def test_a_cancelled_trip_counts_its_charge_once(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    await _rates(admin)
    a = await _agency(admin)
    t = a["tenant"]
    # Trip 1: a sale of 1000 + 100 tax, 200 paid (the invoice stays open with an unpaid rest), a service fee of 50; the
    # cancel charges 300: 200 kept from the payment, a fee invoice for the other 100 (no tax). Without the charge
    # row the view would count 1000 + 100 + 50 = 1150.00.
    trip = await _trip(admin, t, 2, [])
    sale = await _invoice(admin, t, trip, "adjustment", "1000", "100", "USD")
    await _invoice(admin, t, trip, "service_fee", "50", "0", "USD")
    await _as_owner(
        admin, t,
        "INSERT INTO crm.payment (trip_id, invoice_id, amount, currency, method, received_on) "
        "VALUES ($1, $2, 200, 'USD', 'bank_transfer', $3)",
        trip, sale, DAY,
    )  # fmt: skip
    before = await _pnl(app_pool, t, trip)
    assert (before["revenue_sales_base"], before["revenue_base"]) == (D("1000.00"), D("1050.00"))
    fee_invoice = await _invoice(admin, t, trip, "cancellation_fee", "100", "0", "USD")
    await _charge(admin, t, trip, "300", "200", "200", invoiced="100", invoice=fee_invoice)
    p = await _pnl(app_pool, t, trip)
    # The charge net of tax by the net share of the trip's invoices (1100 / 1200): 300 x 0.9166... = 275.00.
    assert p["cancellation_charged"] is True and p["revenue_cancellation_fee_base"] == D("275.00")
    assert (p["revenue_sales_base"], p["refunds_base"], p["revenue_service_fee_base"]) == (
        D("0.00"), D("0.00"), D("50.00"),
    )  # fmt: skip
    assert p["revenue_base"] == D("325.00") and p["revenue_unconverted"] == 0

    # Trip 2: 1100 paid, refunded in full, the charge waived: revenue 0, not 1000 - 1000.
    waived = await _trip(admin, t, 1, [])
    sale = await _invoice(admin, t, waived, "adjustment", "1000", "100", "USD", status="paid")
    paid = await _as_owner(
        admin, t,
        "INSERT INTO crm.payment (trip_id, invoice_id, amount, currency, method, received_on) "
        "VALUES ($1, $2, 1100, 'USD', 'bank_transfer', $3) RETURNING payment_id",
        waived, sale, DAY,
    )  # fmt: skip
    await _as_owner(
        admin, t,
        "INSERT INTO crm.payment (trip_id, invoice_id, amount, currency, method, received_on, entry_kind, refund_of) "
        "VALUES ($1, $2, 1100, 'USD', 'bank_transfer', $3, 'refund', $4)",
        waived, sale, DAY, paid,
    )  # fmt: skip
    await _charge(admin, t, waived, "0", "1100", "0")
    w = await _pnl(app_pool, t, waived)
    assert (w["revenue_base"], w["refunds_base"], w["cancellation_charged"]) == (D("0.00"), D("0.00"), True)

    # Trip 3: a charge in EUR at the cancel day's rate (1.1), no invoice: 100 EUR = 110.00 USD; no rate = unconverted.
    euro = await _trip(admin, t, 1, [])
    await _charge(admin, t, euro, "100", "0", "0", currency="EUR")
    e = await _pnl(app_pool, t, euro)
    assert (e["revenue_cancellation_fee_base"], e["revenue_unconverted"]) == (D("110.00"), 0)
    yen = await _trip(admin, t, 1, [])
    await _charge(admin, t, yen, "100", "0", "0", currency="JPY")
    j = await _pnl(app_pool, t, yen)
    assert (j["revenue_cancellation_fee_base"], j["revenue_unconverted"]) == (D("0.00"), 1)


async def test_a_per_departure_cost_splits_exactly(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    a = await _agency(admin)
    t = a["tenant"]
    trips = [await _trip(admin, t, 1, [], group_code="GRP-7") for _ in range(3)]
    await _trip(admin, t, 1, [], group_code="GRP-8")  # another group
    lost = await _trip(admin, t, 2, [], group_code="GRP-7", stage="lost")
    await _as_app(
        app_pool, t,
        "INSERT INTO crm.departure_cost (departure_ref, agency_supplier_id, title, amount, currency, basis, cost_date, "
        "base_currency, amount_base, fx_rate, fx_rate_date, fx_source, stale_days) "
        "VALUES ('GRP-7', $1, 'Group flight block', 100, 'USD', 'per_departure', $2, 'USD', 100, 1, $2, 'manual', 0)",
        a["own"], DAY,
    )  # fmt: skip
    shares = [(await _pnl(app_pool, t, trip))["departure_cost_base"] for trip in trips]
    assert sorted(shares) == [D("33.33"), D("33.33"), D("33.34")] and sum(shares) == D("100.00")
    assert (await _pnl(app_pool, t, lost))["departure_cost_base"] == D("0.00")  # a lost trip takes no share


async def test_the_margin_policy_and_tenant_isolation(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    a = await _agency(admin)
    other = await _agency(admin)
    trip = await _trip(admin, a["tenant"], 2, [])
    line = await _line(admin, a["tenant"], trip, a["own"], "agency", "confirmed")
    await _money(admin, a["tenant"], line, "cost", "100")
    bill = await _bill(app_pool, a["tenant"], a["own"], "LL-1", "120", "1", {line: "120"})
    await _as_app(
        app_pool, a["tenant"],
        "INSERT INTO crm.departure_cost (departure_ref, agency_supplier_id, title, amount, currency, basis, cost_date, "
        "base_currency, amount_base, fx_rate, fx_rate_date, fx_source, stale_days) "
        "VALUES ('GRP-1', $1, 'Welcome dinner', 40, 'USD', 'per_pax', $2, 'USD', 40, 1, $2, 'manual', 0)",
        a["own"], DAY,
    )  # fmt: skip
    for table in NEW_TABLES[:2] + ("departure_cost",):
        assert await _as_app(app_pool, a["tenant"], f"SELECT count(*) FROM crm.{table}") >= 1  # noqa: S608
        assert await _as_app(app_pool, a["tenant"], f"SELECT count(*) FROM crm.{table}", role="assistant") == 0  # noqa: S608
        assert await _as_app(app_pool, other["tenant"], f"SELECT count(*) FROM crm.{table}") == 0  # noqa: S608
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # the assistant writes no bill (row security)
        await _bill(app_pool, a["tenant"], a["own"], "LL-2", "10", "1", {}, role="assistant")
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="margin_forbidden"):
        await _as_app(app_pool, a["tenant"], "SELECT crm.booking_line_actual_cost($1)", line, role="assistant")
    # The assistant's view of the P&L has no cost at all (the money rows are hidden): the API never serves it.
    seen = await _pnl(app_pool, a["tenant"], trip, role="assistant")
    assert seen["agency_cost_base"] == D("0.00") and seen["lines_billed"] == 0
    # A recorded bill keeps its amount (column grants: status, paid_on, note only).
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await _as_app(
            app_pool, a["tenant"], "UPDATE crm.supplier_bill SET amount = 1 WHERE supplier_bill_id = $1", bill
        )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await _as_app(app_pool, a["tenant"], "DELETE FROM crm.supplier_bill_line")
    await _as_app(
        app_pool, a["tenant"], "UPDATE crm.supplier_bill SET status = 'paid', paid_on = $2 WHERE supplier_bill_id = $1",
        bill, DAY,
    )  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError, match="not an allowed transition"):
        await _as_app(
            app_pool, a["tenant"],
            "UPDATE crm.supplier_bill SET status = 'recorded', paid_on = NULL WHERE supplier_bill_id = $1", bill,
        )  # fmt: skip


async def test_the_bill_line_guard(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    a = await _agency(admin)
    t = a["tenant"]
    trip = await _trip(admin, t, 2, [])
    aa_line = await _line(admin, t, trip, a["aa"], "aa", "confirmed", booking="BK-9")
    boat = await _line(admin, t, trip, a["other_own"], "agency", "confirmed")
    lodge = await _line(admin, t, trip, a["own"], "agency", "confirmed")
    for line in (aa_line, boat):  # an AA line, a line of another supplier
        with pytest.raises(asyncpg.PostgresError, match="bill_line_guard") as err:
            await _bill(app_pool, t, a["own"], f"X-{uuid.uuid4().hex[:6]}", "10", "1", {line: "10"})
        assert err.value.sqlstate == "CRM51"
    with pytest.raises(asyncpg.PostgresError, match="aa_line_read_only"):
        await _as_app(app_pool, t, "SELECT crm.booking_line_actual_cost($1)", aa_line)
    await _bill(app_pool, t, a["own"], "L-1", "10", "1", {lodge: "10"})
    with pytest.raises(asyncpg.PostgresError, match="bill_currency_mixed"):  # one currency per line
        await _bill(app_pool, t, a["own"], "L-2", "200000", "0.00004", {lodge: "200000"}, currency="VND")
    with pytest.raises(asyncpg.UniqueViolationError):  # one live bill per supplier number
        await _bill(app_pool, t, a["own"], "L-1", "5", "1", {lodge: "5"})


async def test_the_erase_takes_the_finance_rows(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    from tests.integration.test_crm010_schema import _erase

    a = await _agency(admin)
    t = a["tenant"]
    trip = await _trip(admin, t, 2, [])
    line = await _line(admin, t, trip, a["own"], "agency", "confirmed")
    await _money(admin, t, line, "cost", "100")
    await _bill(app_pool, t, a["own"], "B-1", "100", "1", {line: "100"})
    await _erase(app_pool, t, "trip", trip)
    assert await _as_app(app_pool, t, "SELECT count(*) FROM crm.supplier_bill_line") == 0
    assert await _as_app(app_pool, t, "SELECT count(*) FROM crm.supplier_bill") == 1  # the bill is the agency's
