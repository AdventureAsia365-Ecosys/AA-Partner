"""AA-1068 without a database: the finance routes reach their helpers and refuse the assistant; the P&L view, the
profit cube and its CSV; the bill, departure and agenda services' checks and refusals; the Booking 1.5 invoice reader
and the "Bookings with AA" page builder. The SQL runs on PostgreSQL in ``tests/integration/test_crm051_schema.py`` and
``test_aa1068_finance_api.py``."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal as D
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import margin
from app.booking import contract
from app.booking.client import BookingRefusal
from app.errors import Conflict, Invalid, NotFound, UnavailableRetry
from app.finance import aa_invoices, agenda, bills, departures, pnl
from app.ledger import lines
from app.main import create_app
from app.routers import finance as router
from tests.aa_fakes import FakeBooking, agency_invoice, agency_invoice_detail, booking_client, invoice_line
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, fake_tx, tenant_ctx

ID = "00000000-0000-4000-8000-0000000000a8"
SUPPLIER = uuid.UUID("00000000-0000-4000-8000-0000000000b1")
LINE = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
TODAY = dt.date(2031, 3, 1)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> tuple[TestClient, dict[str, str]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), booking_client=None)
    ctx = tenant_ctx(ScriptedConn().on("SELECT group_code", "GRP-1"), role=role)

    def tx(*a: Any, **kw: Any) -> Any:
        margin.current_member_role.set(role)
        return fake_tx(ctx)(*a, **kw)

    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", tx)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    out: list[str] = []

    def stub(name: str) -> Any:
        async def fn(*args: Any, **kwargs: Any) -> dict[str, Any]:
            if name in ("page", "invoice_detail"):
                async with args[0]():  # the route's transaction factory: the role check
                    pass
            out.append(name)
            return {"name": name, "margin_base": "1.00"}

        return fn

    for module, names in (
        (pnl, ("trip_pnl", "profit_cube")),
        (departures, ("set_group_code", "departure_pnl", "create_cost", "update_cost", "delete_cost")),
        (bills, ("list_bills", "create_bill", "get_bill", "update_bill")),
        (aa_invoices, ("page", "invoice_detail")),
        (agenda, ("agenda",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, stub(name))

    async def refs(*_: Any) -> list[str]:
        return ["GRP-1"]

    monkeypatch.setattr(departures, "trip_refs", refs)
    return out


BILL = {"agency_supplier_id": str(SUPPLIER), "number": "PH-1", "bill_date": "2031-02-01", "currency": "USD",
        "amount": "100.00", "lines": [{"booking_line_id": str(LINE), "amount": "90.00"}]}  # fmt: skip
COST = {"departure_ref": "DEP-1", "agency_supplier_id": str(SUPPLIER), "title": "Tour leader", "amount": "50",
        "currency": "USD"}  # fmt: skip
REQUESTS = [
    ("GET", f"/crm/v1/trips/{ID}/pnl", None, "trip_pnl"),
    ("PUT", f"/crm/v1/trips/{ID}/group-code", {"group_code": "GRP-1"}, "set_group_code"),
    ("GET", "/crm/v1/supplier-bills", None, "list_bills"),
    ("POST", "/crm/v1/supplier-bills", BILL, "create_bill"),
    ("GET", f"/crm/v1/supplier-bills/{ID}", None, "get_bill"),
    ("PATCH", f"/crm/v1/supplier-bills/{ID}", {"status": "void"}, "update_bill"),
    ("GET", "/crm/v1/aa-bookings", None, "page"),
    ("GET", "/crm/v1/aa-bookings/invoices/AAT-2031-000001", None, "invoice_detail"),
    ("GET", "/crm/v1/departures/DEP-1/pnl", None, "departure_pnl"),
    ("POST", "/crm/v1/departure-costs", COST, "create_cost"),
    ("PATCH", f"/crm/v1/departure-costs/{ID}", {"amount": "60"}, "update_cost"),
    ("DELETE", f"/crm/v1/departure-costs/{ID}", None, "delete_cost"),
    ("GET", "/crm/v1/reports/profit?dimension=product", None, "profit_cube"),
    ("GET", "/crm/v1/reports/agenda", None, "agenda"),
]


def test_every_finance_route_reaches_its_helper(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    client, h = _client(pools, monkeypatch, "agent")
    for method, path, body, _name in REQUESTS:
        res = client.request(method, path, json=body, headers=h)
        assert res.status_code in (200, 201), (method, path, res.text)
    assert calls == [r[3] for r in REQUESTS]
    assert client.get(f"/crm/v1/trips/{ID}/pnl", headers=h).json()["departure_refs"] == ["GRP-1"]
    for path, body in (
        ("/crm/v1/supplier-bills", {**BILL, "currency": "usd"}),
        ("/crm/v1/supplier-bills", {**BILL, "lines": []}),
        ("/crm/v1/departure-costs", {**COST, "basis": "per_room"}),
        ("/crm/v1/departure-costs", {**COST, "departure_ref": "a b"}),
    ):
        assert client.post(path, json=body, headers=h).status_code == 422


def test_the_assistant_reaches_only_the_agenda_and_the_group_code(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    client, h = _client(pools, monkeypatch, "assistant")
    for method, path, body, name in REQUESTS:
        res = client.request(method, path, json=body, headers=h)
        if name in ("agenda", "set_group_code"):
            assert res.status_code == 200, (path, res.text)
        else:
            assert res.status_code == 403 and res.json()["detail"] == "role_forbidden", (path, res.text)
    assert calls == ["set_group_code", "agenda"]


# ── the P&L view and the cube ────────────────────────────────────────────────────────────────────────────────────
def pnl_row(**over: Any) -> dict[str, Any]:
    row = dict(
        trip_id=uuid.UUID(ID), base_currency="USD", pax=3, revenue_sales_base=D("1000"), revenue_service_fee_base=D(55),
        revenue_cancellation_fee_base=D(0), refunds_base=D(200), revenue_base=D(855), revenue_unconverted=0,
        cancellation_charged=False,
        aa_cost_quoted_base=D(250), aa_cost_base=D(260), agency_cost_planned_base=D(400), agency_cost_base=D(430),
        cost_variance_base=D(30), lines_billed=1, cancellation_cost_base=D(40), departure_cost_base=D(105),
        partner_commission_base=D(0), total_cost_base=D(835), commission_income_base=D(30), margin_base=D(50),
        margin_pct=D("5.85"), margin_per_pax_base=D("16.67"), lines_confirmed=3, lines_planning=1,
        lines_other_base=0, aa_lines_invoiced=1, aa_lines_mismatch=1, departure_costs=2, lead_source="referral",
        sales_partner_id=None, owner_member_id=None, aa_sell_base=D(320), agency_sell_base=D(400),
        travel_month=dt.date(2031, 2, 1),
    )  # fmt: skip
    row.update(over)
    return row


def test_the_trip_card_view() -> None:
    v = pnl.view(pnl_row())
    assert v["revenue"]["total_base"] == "855.00" and v["costs"]["variance_base"] == "30.00"
    assert v["margin_pct"] == "5.85" and v["notes"] == ["lines_planning", "aa_invoice_mismatch"]
    unbilled = pnl.view(pnl_row(lines_billed=0, margin_pct=None, revenue_base=D(0), lines_planning=0,
                                aa_lines_mismatch=0, revenue_unconverted=2, lines_other_base=1))  # fmt: skip
    assert unbilled["costs"]["variance_base"] is None and unbilled["margin_pct"] is None
    assert unbilled["notes"] == ["revenue_unconverted", "lines_other_base", "no_revenue"]
    charged = pnl.view(pnl_row(cancellation_charged=True, lines_planning=0, aa_lines_mismatch=0))
    assert charged["notes"] == ["cancellation_charge"] and charged["revenue"]["cancellation_charged"] is True
    conn = ScriptedConn()
    with pytest.raises(NotFound):
        run(pnl.trip_pnl(tenant_ctx(conn), uuid.UUID(ID)))
    conn.on("FROM crm.v_trip_pnl WHERE trip_id", pnl_row())
    ctx = tenant_ctx(conn)
    assert run(pnl.trip_pnl(ctx, uuid.UUID(ID)))["margin_base"] == "50.00"
    assert [e.object_kind for e in ctx.audit_events] == ["trip_pnl"]


def test_the_cube_groups_by_each_dimension() -> None:
    other = uuid.uuid4()
    rows = [
        pnl_row(),
        pnl_row(trip_id=other, lead_source=None, revenue_base=D(100), total_cost_base=D(150), margin_base=D(-50),
                commission_income_base=D(0), aa_sell_base=D(0), aa_cost_base=D(0), pax=1,
                travel_month=dt.date(2031, 1, 1), owner_member_id=uuid.UUID(ID)),
    ]  # fmt: skip
    by_source, total = pnl.cube(rows, "lead_source")
    assert [(r["key"], r["margin_base"]) for r in by_source] == [("referral", "50.00"), ("unknown", "-50.00")]
    assert total["trips"] == 2 and total["pax"] == 4 and total["margin_base"] == "0.00"
    assert [r["key"] for r in pnl.cube(rows, "travel_month")[0]] == ["2031-01", "2031-02"]
    assert {r["key"] for r in pnl.cube(rows, "owner_member")[0]} == {"unassigned", ID}
    assert [r["key"] for r in pnl.cube(rows, "sales_partner")[0]] == ["none"]
    product, ptotal = pnl.cube(rows, "product")
    assert {r["key"]: r["margin_base"] for r in product} == {"aa_tour": "60.00", "ancillary": "-60.00"}
    assert product[0]["trips"] == 1 and ptotal["trips"] == 2
    with pytest.raises(Invalid):
        pnl.cube(rows, "colour")
    header, out = pnl.csv_rows(
        {"dimension": "lead_source", "base_currency": "USD", "rows": by_source, "totals": total}
    )  # fmt: skip
    assert header[0] == "lead_source" and out[-1][0] == "total" and out[0][-1] == D("5.85")
    assert pnl.margin_pct(D(1), D(0)) is None


def test_the_profit_cube_reads_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn().on("FROM crm.v_trip_pnl p", [pnl_row()])
    conn.on("AS today", {"today": TODAY, "tz": "Asia/Ho_Chi_Minh", "base_currency": "USD", "first_day": None})
    ctx = tenant_ctx(conn)
    out = run(pnl.profit_cube(ctx, "12m", None, "lead_source"))
    assert out["rows"][0]["key"] == "referral" and out["trips_unconverted"] == 0 and not out["truncated"]
    assert out["window"] == {"from": "2030-04-01", "to": "2031-03-01"}
    assert len(ctx.audit_events) == 1
    run(pnl.profit_cube(ctx, "12m", None, "product", audit=False))
    assert len(ctx.audit_events) == 1
    with pytest.raises(Invalid):
        run(pnl.profit_cube(ctx, "12m", None, "colour"))


# ── bills ────────────────────────────────────────────────────────────────────────────────────────────────────────
def bill_conn(**over: Any) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("now() AT TIME ZONE", TODAY)
    conn.on("SELECT base_currency, booking_validation_level", {"base_currency": "USD", "booking_validation_level":
            "basic", "fx_mode": "automatic"})  # fmt: skip
    conn.on("crm.fx_resolve", {"rate": D(1), "rate_date": TODAY, "fx_source": "platform_daily", "stale_days": 0})
    conn.on("SELECT kind, deleted_at FROM crm.agency_supplier", {"kind": "own", "deleted_at": None})
    conn.on("INSERT INTO crm.supplier_bill (", uuid.UUID(ID))
    conn.on("WHERE b.supplier_bill_id = $1", bill_row())
    for key, value in over.items():
        conn.on(key, value)
    return conn


def bill_row(**over: Any) -> dict[str, Any]:
    row = dict(supplier_bill_id=uuid.UUID(ID), agency_supplier_id=SUPPLIER, supplier_name="Pine Hill", number="PH-1",
               bill_date=dt.date(2031, 2, 1), currency="EUR", amount=D("100.00"), status="recorded", paid_on=None,
               note=None, base_currency="USD", amount_base=D("110.00"), fx_rate=D("1.1"), fx_rate_date=TODAY,
               fx_source="platform_daily", stale_days=0, created_at=dt.datetime(2031, 2, 1, tzinfo=dt.UTC))  # fmt: skip
    row.update(over)
    return row


def bill_body(**over: Any) -> dict[str, Any]:
    body = {"agency_supplier_id": SUPPLIER, "number": " PH-1 ", "bill_date": dt.date(2031, 2, 1), "currency": "USD",
            "amount": D("100"), "lines": [{"booking_line_id": LINE, "amount": D("90")}]}  # fmt: skip
    body.update(over)
    return body


def test_recording_a_bill() -> None:
    conn = bill_conn()
    conn.on("FROM crm.supplier_bill_line x", [{"supplier_bill_id": uuid.UUID(ID), "booking_line_id": LINE,
            "amount": D("90.00"), "line_title": "Lodge", "trip_id": LINE, "trip_title": "Lakes"}])  # fmt: skip
    out = run(bills.create_bill(tenant_ctx(conn), bill_body()))
    assert out["bill_cost"]["unallocated"] == "10.00" and out["bill_cost"]["fx"]["attribution"]
    assert conn.sql("INSERT INTO crm.supplier_bill (")[0][1] == "PH-1"
    assert conn.sql("crm.booking_line_actual_cost") == [(LINE,)]


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"number": " "}, "invalid_number"),
        ({"number": "A\x01"}, "invalid_number"),
        ({"amount": "x"}, "invalid_amount"),
        ({"amount": D(0)}, "invalid_amount"),
        ({"bill_date": dt.date(2031, 3, 2)}, "invalid_bill_date"),
        ({"lines": []}, "invalid_lines"),
        ({"lines": [{"booking_line_id": LINE, "amount": D(1)}] * 2}, "invalid_lines"),
        ({"lines": [{"booking_line_id": LINE, "amount": D(101)}]}, "bill_lines_over"),
    ],
)
def test_a_bill_body_is_checked(over: dict[str, Any], code: str) -> None:
    with pytest.raises(Invalid) as err:
        run(bills.create_bill(tenant_ctx(bill_conn()), bill_body(**over)))
    assert err.value.code == code


def test_the_bill_refusals() -> None:
    with pytest.raises(NotFound):
        run(bills.create_bill(tenant_ctx(bill_conn(**{"SELECT kind, deleted_at FROM crm.agency_supplier": None})),
                              bill_body()))  # fmt: skip
    with pytest.raises(Invalid) as err:
        run(bills.create_bill(tenant_ctx(bill_conn(**{"SELECT kind, deleted_at FROM crm.agency_supplier":
                                                      {"kind": "aa", "deleted_at": None}})), bill_body()))  # fmt: skip
    assert err.value.code == "aa_supplier_bill"
    with pytest.raises(Conflict) as err:
        run(bills.create_bill(tenant_ctx(bill_conn(**{"crm.fx_resolve": None})), bill_body()))
    assert err.value.code == "fx_rate_missing"
    unique = asyncpg.UniqueViolationError("dup")
    unique.constraint_name = "supplier_bill_number_uq"  # type: ignore[attr-defined]
    guard = asyncpg.PostgresError("crm: bill_line_guard")
    guard.sqlstate = "CRM51"  # type: ignore[attr-defined]
    mixed = asyncpg.PostgresError("crm: bill_currency_mixed")
    mixed.sqlstate = "CRM51"  # type: ignore[attr-defined]
    aa = asyncpg.PostgresError("crm: aa_line_read_only")
    aa.sqlstate = "CRM24"  # type: ignore[attr-defined]
    other_unique = asyncpg.UniqueViolationError("dup line")
    for exc, code in ((unique, "duplicate_bill_number"), (guard, "bill_line_invalid"), (mixed, "bill_currency_mixed"),
                      (aa, "bill_line_invalid"), (other_unique, "duplicate_bill_line"),
                      (asyncpg.ForeignKeyViolationError("fk"), "line_not_found")):  # fmt: skip
        with pytest.raises((Conflict, NotFound)) as err:
            run(bills.create_bill(tenant_ctx(bill_conn(**{"INSERT INTO crm.supplier_bill_line": exc})), bill_body()))
        assert err.value.code == code
    plain = asyncpg.PostgresError("other")
    with pytest.raises(asyncpg.PostgresError):
        run(bills.create_bill(tenant_ctx(bill_conn(**{"INSERT INTO crm.supplier_bill_line": plain})), bill_body()))


def test_changing_a_bill() -> None:
    conn = bill_conn(**{"SELECT booking_line_id FROM crm.supplier_bill_line": [{"booking_line_id": LINE}]})
    ctx = tenant_ctx(conn)
    run(bills.update_bill(ctx, uuid.UUID(ID), {"status": "paid", "note": "Paid by transfer"}))
    assert conn.sql("SET status = $2, paid_on = $3")[0][1:] == ("paid", TODAY)
    run(bills.update_bill(ctx, uuid.UUID(ID), {"status": "void"}))
    assert conn.sql("crm.booking_line_actual_cost") == [(LINE,)]
    with pytest.raises(Invalid):
        run(bills.update_bill(ctx, uuid.UUID(ID), {"status": "paid", "paid_on": dt.date(2031, 1, 1)}))
    paid = tenant_ctx(bill_conn(**{"WHERE b.supplier_bill_id = $1": bill_row(status="paid")}))
    with pytest.raises(Invalid):
        run(bills.update_bill(paid, uuid.UUID(ID), {"status": "recorded"}))
    with pytest.raises(Conflict):
        run(bills.update_bill(tenant_ctx(bill_conn(**{"WHERE b.supplier_bill_id = $1": bill_row(status="void")})),
                              uuid.UUID(ID), {"status": "paid"}))  # fmt: skip
    with pytest.raises(NotFound):
        run(bills.update_bill(tenant_ctx(bill_conn(**{"WHERE b.supplier_bill_id = $1": None})), uuid.UUID(ID), {}))
    with pytest.raises(NotFound):
        run(bills.get_bill(tenant_ctx(bill_conn(**{"WHERE b.supplier_bill_id = $1": None})), uuid.UUID(ID)))
    listed = run(bills.list_bills(tenant_ctx(bill_conn(**{"ORDER BY b.bill_date DESC": [bill_row()]}))))
    assert listed["bills"][0]["number"] == "PH-1" and not listed["truncated"]


# ── departures ───────────────────────────────────────────────────────────────────────────────────────────────────
def cost_row(**over: Any) -> dict[str, Any]:
    row = dict(departure_cost_id=uuid.UUID(ID), departure_ref="DEP-1", agency_supplier_id=SUPPLIER,
               supplier_name="Pine Hill", title="Tour leader", amount=D(50), currency="USD", basis="per_pax",
               cost_date=TODAY, base_currency="USD", amount_base=D(50), fx_rate=D(1), fx_rate_date=TODAY,
               fx_source="manual", stale_days=0)  # fmt: skip
    row.update(over)
    return row


def test_departure_costs_and_the_group_code() -> None:
    conn = bill_conn(**{"INSERT INTO crm.departure_cost": uuid.UUID(ID), "WHERE c.departure_cost_id = $1": cost_row(),
                        "SELECT stage FROM crm.trip": "booked",
                        "SELECT departure_ref FROM crm.v_departure_trip": [{"departure_ref": "GRP-1"}]})  # fmt: skip
    ctx = tenant_ctx(conn)
    made = run(departures.create_cost(ctx, {**COST, "agency_supplier_id": SUPPLIER, "amount": D(50)}))
    assert made["group_cost"]["amount_base"] == "50"
    changed = run(departures.update_cost(ctx, uuid.UUID(ID), {"title": "Leader", "amount": None}))
    assert changed["title"] == "Tour leader" and conn.sql("UPDATE crm.departure_cost")[0][2] == "Leader"
    assert run(departures.delete_cost(ctx, uuid.UUID(ID)))["deleted"]
    assert run(departures.set_group_code(ctx, uuid.UUID(ID), "GRP-1"))["departure_refs"] == ["GRP-1"]
    assert run(departures.set_group_code(ctx, uuid.UUID(ID), ""))["group_code"] is None
    for bad in ({"basis": "per_room"}, {"cost_date": dt.date(2025, 1, 1)}, {"departure_ref": "a b"}):
        with pytest.raises(Invalid):
            run(departures.create_cost(ctx, {**COST, "agency_supplier_id": SUPPLIER, **bad}))
    with pytest.raises(Conflict):
        run(departures.create_cost(tenant_ctx(bill_conn(**{"crm.fx_resolve": None})),
                                   {**COST, "agency_supplier_id": SUPPLIER}))  # fmt: skip
    with pytest.raises(Invalid):
        run(departures.create_cost(
            tenant_ctx(bill_conn(**{"INSERT INTO crm.departure_cost": asyncpg.CheckViolationError("chk")})),
            {**COST, "agency_supplier_id": SUPPLIER}))  # fmt: skip
    full = bill_conn(**{"ORDER BY c.cost_date": [cost_row()] * departures.MAX_COSTS})
    with pytest.raises(Conflict):
        run(departures.create_cost(tenant_ctx(full), {**COST, "agency_supplier_id": SUPPLIER}))
    gone = bill_conn(**{"WHERE c.departure_cost_id = $1": None, "SELECT kind, deleted_at FROM crm.agency_supplier":
                        None, "SELECT stage FROM crm.trip": None})  # fmt: skip
    for call in (departures.update_cost(tenant_ctx(gone), uuid.UUID(ID), {}),
                 departures.delete_cost(tenant_ctx(gone), uuid.UUID(ID)),
                 departures.set_group_code(tenant_ctx(gone), uuid.UUID(ID), None),
                 departures.create_cost(tenant_ctx(gone), {**COST, "agency_supplier_id": SUPPLIER})):  # fmt: skip
        with pytest.raises(NotFound):
            run(call)


def test_the_departure_pnl_sums_its_trips() -> None:
    trips = [dict(trip_id=uuid.UUID(ID), pax=3, stage="booked", start_date=TODAY, title="Lakes",
                  departure_cost_base=D(150), revenue_base=D(1000), total_cost_base=D(700), margin_base=D(300)),
             dict(trip_id=LINE, pax=1, stage="booked", start_date=None, title="Lakes 2", departure_cost_base=D(50),
                  revenue_base=None, total_cost_base=None, margin_base=None)]  # fmt: skip
    conn = bill_conn(**{"ORDER BY c.cost_date": [cost_row(), cost_row(basis="per_departure", amount_base=D(100)),
                                                 cost_row(base_currency="EUR")],
                        "FROM crm.v_departure_trip d": trips})  # fmt: skip
    out = run(departures.departure_pnl(tenant_ctx(conn), "DEP-1"))
    assert out["totals"] == {"pax": 4, "trips": 2, "departure_cost_base": "300.00", "revenue_base": "1000.00",
                             "trip_cost_base": "700.00", "margin_base": "300.00"}  # fmt: skip
    assert out["costs_other_base"] == 1 and out["trips"][1]["revenue_base"] == "0.00"


# ── agenda ───────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_agenda_counts() -> None:
    conn = ScriptedConn()
    conn.on("AS open_proposals", {"open_proposals": 2, "deposits_due_week": 1, "overdue_instalments": 3,
                                  "passport_missing": 1})  # fmt: skip
    conn.on("FROM crm.v_departure_trip d", [{"departure_ref": "DEP-1", "start_date": TODAY, "trips": 2, "pax": 5}])
    conn.on("FROM crm.aa_invoice_cache", 4)
    owner = run(agenda.agenda(tenant_ctx(conn)))
    assert owner["aa_overdue_invoices"] == 4 and owner["next_departures"][0]["guaranteed"] is None
    assert run(agenda.agenda(tenant_ctx(conn, role="assistant")))["aa_overdue_invoices"] is None


# ── Booking 1.5 and the "Bookings with AA" page ──────────────────────────────────────────────────────────────────
def test_the_invoice_readers() -> None:
    items, total = contract.agency_invoice_page({"items": [agency_invoice("AAT-2031-000001")], "total": 1})
    assert total == 1 and items[0].total == D("1000.00") and items[0].due_on == dt.date(2031, 2, 15)
    detail = contract.agency_invoice(agency_invoice_detail("AAT-2031-000001", [invoice_line(1, "full", "10.00",
                                                                                            "BK-1")]))  # fmt: skip
    assert detail.lines[0].booking_id == "BK-1" and detail.invoice_inbox == "billing@invented.example"
    for broken in (
        {"items": "x", "total": 1},
        {"items": [agency_invoice("AAT-1")], "total": 1},
        {"items": [agency_invoice("AAT-2031-000001", overdue_days=-1)], "total": 1},
        {"items": [agency_invoice("AAT-2031-000001", invoice_kind="other")], "total": 1},
        {"items": [agency_invoice("AAT-2031-000001", total="10")], "total": 1},
    ):
        with pytest.raises(contract.ContractError):
            contract.agency_invoice_page(broken)
    good = agency_invoice_detail("AAT-2031-000001", [invoice_line(1, "full", "10.00", "BK-1")])
    for broken in (
        {**good, "lines": None},
        {**good, "lines": [{**invoice_line(1, "full", "1.00", None), "line_no": 0}]},
        {**good, "lines": [{**invoice_line(1, "full", "1.00", None), "voided": "no"}]},
        {**good, "lines": [{**invoice_line(1, "spa", "1.00", None)}]},
    ):
        with pytest.raises(contract.ContractError):
            contract.agency_invoice(broken)
    paid = contract.agency_invoice(
        {**good, "issuer": None, "payments": [{"received_on": "2031-02-02", "amount": "5.00", "currency": "USD"}]}
    )
    assert paid.payments[0][1] == D("5.00") and paid.payment_instructions is None  # fmt: skip
    status = contract.billing_status(FakeBooking().billing | {"paid_through": "2031-03-31"})
    assert status.status == "ok" and status.paid_through == dt.date(2031, 3, 31)
    for broken in ({"status": "ok", "holds_paused": "no"}, {"status": "ok", "holds_paused": False,
                                                            "overdue_amount": None}):  # fmt: skip
        with pytest.raises(contract.ContractError):
            contract.billing_status(broken)


def test_booking_amounts_and_the_banner() -> None:
    detail = contract.agency_invoice(agency_invoice_detail("AAT-2031-000001", [
        invoice_line(1, "deposit", "30.00", "BK-1"), invoice_line(2, "balance", "70.00", "BK-1"),
        invoice_line(3, "deposit", "20.00", "BK-2"), invoice_line(4, "deposit", "5.00", "BK-3", voided=True),
        invoice_line(5, "cancellation_fee", "9.00", "BK-4"), invoice_line(6, "adjustment", "-1.00", None),
    ]))  # fmt: skip
    assert aa_invoices.booking_amounts(detail) == {
        "BK-1": {"amount": "100.00", "complete": True}, "BK-2": {"amount": "20.00", "complete": False},
    }  # fmt: skip
    assert aa_invoices.banner(contract.billing_status(FakeBooking().billing), []) is None
    cache = [{"aa_status": "issued", "overdue_days": 4, "balance_amount": D(10), "currency": "USD"},
             {"aa_status": "issued", "overdue_days": 9, "balance_amount": D(5), "currency": "USD"},
             {"aa_status": "paid", "overdue_days": 0, "balance_amount": D(0), "currency": "USD"}]  # fmt: skip
    fallback = aa_invoices.banner(None, cache)
    assert fallback["level"] == "overdue" and fallback["oldest_overdue_days"] == 9
    assert fallback["overdue_b2b"] == [{"amount": "15", "currency": "USD"}]
    assert aa_invoices.banner(None, cache[2:]) is None
    assert aa_invoices.booking_error_code(BookingRefusal(403, "unknown_partner")) == "aa_partner_not_active"
    assert aa_invoices.booking_error_code(contract.ContractError("x")) == "booking_contract"


def _aa_row(line: uuid.UUID, booking: str, amount: str | None, currency: str | None, status: str = "confirmed") -> dict:
    return {"booking_line_id": line, "trip_id": LINE, "trip_title": "Lakes", "start_date": None, "title": "Tour",
            "status": status, "aa_sub_status": None, "bk_booking_id": booking, "bk_hold_id": "H-9",
            "service_from": None, "quoted_amount": None if amount is None else D(amount),
            "quoted_currency": currency}  # fmt: skip


def test_the_mismatch_is_per_aa_booking() -> None:
    """Several CRM lines share one AA booking: the invoiced sum compares with the sum of their quotes."""
    lines = [_aa_row(uuid.uuid4(), "BK-7", "400.00", "USD"), _aa_row(uuid.uuid4(), "BK-7", "500.00", "USD"),
             _aa_row(uuid.uuid4(), "BK-7", "70.00", "USD", status="planning"),
             _aa_row(uuid.uuid4(), "BK-8", "100.00", "USD"), _aa_row(uuid.uuid4(), "BK-8", "50.00", "EUR"),
             _aa_row(uuid.uuid4(), "BK-9", "100.00", "USD"), _aa_row(uuid.uuid4(), "BK-9", None, None)]  # fmt: skip
    quotes = aa_invoices.booking_quotes(lines)
    assert quotes["BK-7"] == {"quoted": {"amount": D("900.00"), "currency": "USD"}, "lines": 2}
    assert quotes["BK-8"]["quoted"] is None and quotes["BK-9"]["quoted"] is None  # mixed currency / unquoted

    def cache(amounts: str) -> list[dict]:
        return [{"number": "AAT-2031-000001", "invoice_kind": "settlement", "aa_status": "issued", "currency": "USD",
                 "total_amount": D(900), "balance_amount": D(900), "issued_on": TODAY, "due_on": TODAY,
                 "overdue_days": 0, "booking_amounts": amounts, "detail_read": True,
                 "refreshed_at": dt.datetime(2031, 3, 1, tzinfo=dt.UTC)}]  # fmt: skip

    full = '{"BK-7": {"amount": "900.00", "complete": true}, "BK-8": {"amount": "150.00", "complete": true}}'
    out = aa_invoices.build(cache(full), lines, aa_invoices.Fetched(), TODAY)
    flags = [(r["booking_id"], r["status"], r["mismatch"]) for r in out["bookings"]]
    assert flags[:3] == [("BK-7", "confirmed", False)] * 2 + [("BK-7", "planning", False)]
    assert flags[3:5] == [("BK-8", "confirmed", True)] * 2  # two currencies quoted: cannot match
    assert out["bookings"][0]["booking_quoted_b2b"] == {"amount": "900.00", "currency": "USD"}
    assert out["bookings"][0]["booking_lines"] == 2 and out["bookings"][3]["booking_quoted_b2b"] is None
    assert out["mismatches"] == 1  # BK-8 (BK-9 is not invoiced)
    over = aa_invoices.build(cache('{"BK-7": {"amount": "950.00", "complete": true}}'), lines, aa_invoices.Fetched(),
                             TODAY)  # fmt: skip
    assert [r["mismatch"] for r in over["bookings"][:3]] == [True, True, False] and over["mismatches"] == 1
    partial = aa_invoices.build(cache('{"BK-7": {"amount": "270.00", "complete": false}}'), lines,
                                aa_invoices.Fetched(), TODAY)  # fmt: skip
    assert partial["mismatches"] == 0  # a deposit only: never a mismatch


def test_the_page_and_the_detail_with_a_fake_booking() -> None:
    fake = FakeBooking()
    fake.agency_invoices = [agency_invoice(f"AAT-2031-{n:06d}") for n in range(1, 4)]
    fake.invoice_details = {
        f"AAT-2031-{n:06d}": agency_invoice_detail(f"AAT-2031-{n:06d}", [invoice_line(1, "full", "1000.00", "BK-1")])
        for n in range(1, 3)
    }
    client = booking_client(fake)
    fetched = run(aa_invoices.fetch(client, TENANT, {}))
    assert fetched.error == "booking_unavailable"  # the third detail is a 404: the whole read is "unavailable"
    fake.invoice_details["AAT-2031-000003"] = fake.invoice_details["AAT-2031-000001"]
    fetched = run(aa_invoices.fetch(client, TENANT, {"AAT-2031-000001": {"detail_read": True, "aa_status": "issued"}}))
    assert fetched.error is None and set(fetched.details) == {"AAT-2031-000002", "AAT-2031-000003"}
    assert run(aa_invoices.fetch(None, TENANT, {})).error == "booking_not_configured"

    conn = ScriptedConn().on("SELECT crm.ensure_aa_supplier()", SUPPLIER)
    assert run(aa_invoices.store(tenant_ctx(conn), fetched)) == 3
    assert run(aa_invoices.store(tenant_ctx(conn), aa_invoices.Fetched())) == 0
    cache = [{"number": "AAT-2031-000001", "invoice_kind": "settlement", "aa_status": "issued", "currency": "USD",
              "total_amount": D(1000), "balance_amount": D(1000), "issued_on": TODAY, "due_on": dt.date(2031, 2, 15),
              "overdue_days": 14, "booking_amounts": '{"BK-1": {"amount": "1000.00", "complete": true}}',
              "detail_read": True, "refreshed_at": dt.datetime(2031, 3, 1, tzinfo=dt.UTC)}]  # fmt: skip
    aa_lines = [{"booking_line_id": LINE, "trip_id": LINE, "trip_title": "Lakes", "start_date": None, "title": "Tour",
                 "status": "confirmed", "aa_sub_status": None, "bk_booking_id": "BK-1", "bk_hold_id": "H-1",
                 "service_from": TODAY, "quoted_amount": D("900.00"), "quoted_currency": "USD"},
                {"booking_line_id": SUPPLIER, "trip_id": LINE, "trip_title": "Lakes", "start_date": TODAY,
                 "title": "Room", "status": "planning", "aa_sub_status": "held", "bk_booking_id": None,
                 "bk_hold_id": "H-2", "service_from": None, "quoted_amount": None,
                 "quoted_currency": None}]  # fmt: skip
    page_conn = ScriptedConn().on("FROM crm.aa_invoice_cache", cache).on("WHERE l.supplier_kind = 'aa'", aa_lines)

    def tx() -> Any:
        return fake_tx(tenant_ctx(page_conn))()

    out = run(aa_invoices.page(tx, None, TENANT, TODAY))
    assert out["source"] == "cache" and out["banner"]["source"] == "cache" and out["mismatches"] == 1
    assert out["invoices"][0]["overdue"] and out["bookings"][1]["invoiced_b2b"] is None

    detail_conn = ScriptedConn().on("SELECT crm.ensure_aa_supplier()", SUPPLIER).on("WHERE l.supplier_kind", aa_lines)

    def dtx() -> Any:
        return fake_tx(tenant_ctx(detail_conn))()

    shown = run(aa_invoices.invoice_detail(dtx, client, TENANT, "AAT-2031-000001"))
    assert shown["lines"][0]["crm_lines"][0]["booking_line_id"] == str(LINE)
    with pytest.raises(NotFound):
        run(aa_invoices.invoice_detail(dtx, client, TENANT, "AAT-2031-000009"))
    with pytest.raises(NotFound):
        run(aa_invoices.invoice_detail(dtx, client, TENANT, "not-a-number"))
    with pytest.raises(UnavailableRetry):
        run(aa_invoices.invoice_detail(dtx, None, TENANT, "AAT-2031-000001"))
    fake.down = True
    with pytest.raises(UnavailableRetry):
        run(aa_invoices.invoice_detail(dtx, client, TENANT, "AAT-2031-000001"))
    fake.down = False
    fake.invoice_details["AAT-2031-000001"] = {"number": "AAT-2031-000001"}
    with pytest.raises(UnavailableRetry):
        run(aa_invoices.invoice_detail(dtx, client, TENANT, "AAT-2031-000001"))
    assert isinstance(aa_invoices._unavailable("aa_partner_not_active"), Conflict)  # noqa: SLF001


# ── the line money view ──────────────────────────────────────────────────────────────────────────────────────────
def money_row(kind: str, amount: str, base: str = "USD") -> dict[str, Any]:
    return {"booking_line_id": LINE, "kind": kind, "amount": D(amount), "currency": "USD", "base_currency": base,
            "amount_base": D(amount), "fx_rate": D(1), "fx_rate_date": TODAY, "fx_source": "manual",
            "stale_days": 0}  # fmt: skip


def test_a_line_shows_its_actual_cost_only_while_billed() -> None:
    rows = [money_row("cost", "100"), money_row("sell", "150"), money_row("actual_cost", "110")]
    billed = lines._money(rows, billed=True)  # noqa: SLF001
    assert billed["cost_variance_base"] == "10" and billed["actual_margin_base"] == "40"
    unbilled = lines._money(rows)  # noqa: SLF001
    assert "actual_cost" not in unbilled and "cost_variance_base" not in unbilled
    mixed = [money_row("cost", "100", "EUR"), money_row("actual_cost", "110")]
    other = lines._money(mixed, billed=True)  # noqa: SLF001
    assert "cost_variance_base" not in other
