"""AA-952 slice 2: the invoice service's Python branches over a :class:`ScriptedConn` (the SQL, the guards and RLS run
on PostgreSQL in tests/integration/test_invoices_api.py and test_crm024_schema.py), and the printable document."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app import cancellation
from app.errors import Conflict, Invalid, NotFound, ServiceError
from app.invoices import render
from app.invoices import service as inv
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault, enc, tenant_ctx

TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000b1")
SCHEDULE = uuid.UUID("00000000-0000-4000-8000-0000000000b2")
PROPOSAL = uuid.UUID("00000000-0000-4000-8000-0000000000b3")
INVOICE = uuid.UUID("00000000-0000-4000-8000-0000000000b4")
LEAD = uuid.UUID("00000000-0000-4000-8000-0000000000b5")
TODAY = dt.date(2026, 10, 30)
NOW = dt.datetime(2026, 10, 30, 9, 0, tzinfo=dt.UTC)


@contextmanager
def _raises(cls: type[ServiceError], code: str) -> Iterator[None]:
    with pytest.raises(cls) as err:
        yield
    assert err.value.code == code, err.value.code


def _pg(sqlstate: str, message: str = "") -> asyncpg.PostgresError:
    exc = asyncpg.PostgresError(message)
    exc.sqlstate = sqlstate  # type: ignore[misc]
    return exc


def _invoice(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "invoice_id": INVOICE,
        "trip_id": TRIP,
        "kind": "deposit",
        "status": "draft",
        "schedule_id": SCHEDULE,
        "proposal_id": PROPOSAL,
        "number": None,
        "currency": "USD",
        "subtotal_amount": Decimal("272.73"),
        "tax_amount": Decimal("27.27"),
        "total_amount": Decimal("300.00"),
        "tax_rate": Decimal("10.000"),
        "tax_label": "VAT",
        "tax_inclusive": True,
        "memo": None,
        "due_on": None,
        "issue_date": None,
        "issued_at": None,
        "paid_at": None,
        "voided_at": None,
        "void_reason": None,
        "created_at": NOW,
        "updated_at": NOW,
        # AA-952 slice 4 (crm-028) and the proposal's AA hold (the pay window).
        "claimed_at": None,
        "hold_extended_at": None,
        "hold_extend_code": None,
        "rehold_requested_at": None,
        "rehold_code": None,
        "proposal_kind": "text",
        "accepted_hold_id": None,
        "hold_id": None,
        "hold_expires_at": None,
    }
    row.update(over)
    return row


def _schedule(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schedule_id": SCHEDULE,
        "trip_id": TRIP,
        "proposal_id": PROPOSAL,
        "kind": "deposit",
        "amount": Decimal("300"),
        "currency": "USD",
        "due_on": TODAY - dt.timedelta(days=2),
        "status": "due",
        "proposal_status": "accepted",
        "version": 2,
    }
    row.update(over)
    return row


def _line(position: int, kind: str = "charge", amount: str | None = "300", **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "line_id": uuid.uuid4(),
        "position": position,
        "kind": kind,
        "source": "schedule" if kind == "charge" else "aa_line",
        "description": "Deposit" if kind == "charge" else "Halong cruise",
        "quantity": Decimal("1.00") if kind == "charge" else None,
        "unit_amount": Decimal(amount) if amount and kind == "charge" else None,
        "amount": Decimal(amount) if amount and kind == "charge" else None,
    }
    row.update(over)
    return row


def _conn(invoice: dict[str, Any] | None = None, lines: list[dict[str, Any]] | None = None) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM crm.trip WHERE trip_id", {"trip_id": TRIP, "stage": "booked"})
    conn.on("AT TIME ZONE", TODAY)
    if invoice is not None:
        conn.on("FROM crm.invoice WHERE invoice_id = $1", invoice)
    conn.on("FROM crm.invoice_line WHERE invoice_id", lines if lines is not None else [_line(1)])
    return conn


# ── pure helpers ──────────────────────────────────────────────────────────────────────────────────


def test_tax_split_inclusive_exclusive_and_zero() -> None:
    assert inv.tax_split(Decimal("300"), Decimal("10"), True) == (Decimal("272.73"), Decimal("27.27"),
                                                                  Decimal("300.00"))  # fmt: skip
    assert inv.tax_split(Decimal("95.5"), Decimal("10"), False) == (Decimal("95.50"), Decimal("9.55"),
                                                                    Decimal("105.05"))  # fmt: skip
    assert inv.tax_split(Decimal("10"), Decimal("0"), True) == (Decimal("10.00"), Decimal("0.00"), Decimal("10.00"))
    assert inv.number_text("INV", 42) == "INV-000042" and inv.number_text("A", 1234567) == "A-1234567"
    assert inv.plain(Decimal("10.000")) == "10" and inv.plain(Decimal("2.50")) == "2.5"
    assert inv.source_ref(INVOICE) == f"invoice:{INVOICE}"


@pytest.mark.parametrize(
    ("lines", "code"),
    [
        ([], "invalid_lines"),
        ("x", "invalid_lines"),
        ([{"description": "a", "unit_amount": "1"}] * 51, "too_many_lines"),
        (["x"], "invalid_lines"),
        ([{"description": "a\nb", "unit_amount": "1"}], "invalid_description"),
        ([{"description": " ", "unit_amount": "1"}], "invalid_description"),
        ([{"description": "a" * 301, "unit_amount": "1"}], "description_too_long"),
        ([{"description": 5, "unit_amount": "1"}], "invalid_description"),
        ([{"description": "a", "unit_amount": "x"}], "invalid_amount"),
        ([{"description": "a", "unit_amount": "NaN"}], "invalid_amount"),
        ([{"description": "a", "quantity": "0", "unit_amount": "1"}], "invalid_quantity"),
        ([{"description": "a", "quantity": "1.005", "unit_amount": "1"}], "invalid_quantity"),
        ([{"description": "a", "unit_amount": "0"}], "invalid_unit_amount"),
        ([{"description": "a", "unit_amount": "1.001"}], "invalid_unit_amount"),
        ([{"description": "a", "quantity": "9999", "unit_amount": "999999999999"}], "invalid_amount"),
    ],
)
def test_charge_lines_refusals(lines: Any, code: str) -> None:
    with _raises(Invalid, code):
        inv.charge_lines(lines)


def test_charge_lines_amounts() -> None:
    out = inv.charge_lines([{"description": " Fee ", "quantity": "2", "unit_amount": "12.50"},
                            {"description": "Letter", "unit_amount": "3"}])  # fmt: skip
    assert [(line["description"], line["amount"]) for line in out] == [("Fee", Decimal("25.00")),
                                                                     ("Letter", Decimal("3.00"))]  # fmt: skip


def test_actions_per_status() -> None:
    assert inv.actions({"status": "draft"}) == ["edit", "issue", "void"]
    assert inv.actions({"status": "open"}) == ["record_payment", "void"]
    assert inv.actions({"status": "open"}, Decimal("10")) == ["record_payment"]
    assert inv.actions({"status": "expired"}) == ["void"] and inv.actions({"status": "expired"}, Decimal(1)) == []
    assert inv.actions({"status": "paid"}) == [] and inv.actions({"status": "void"}) == []
    assert inv.actions({"status": "open"}, Decimal("0")) == ["record_payment", "void"]  # all refunded
    # AA-952 slice 4: a claim is answered first (record what arrived, or "not received"); a late deposit waiting for
    # its AA hold to be placed again offers nothing.
    assert inv.actions({"status": "payment_claimed"}) == ["record_payment", "reject_claim"]
    assert inv.actions({"status": "payment_claimed"}, Decimal("10")) == ["record_payment", "reject_claim"]
    assert inv.actions({"status": "open", "rehold_requested_at": NOW}, Decimal("300")) == []


def test_a_fully_refunded_invoice_offers_void() -> None:
    first = _pay_row("300")
    refund = _pay_row("300", "refund", refund_of=first["payment_id"])
    view = inv.invoice_view(_invoice(status="open"), None, [first, refund], [])
    assert view["actions"] == ["record_payment", "void"] and view["refunded_amount"] == "300.00"
    part = dict(refund, amount=Decimal("100"))
    assert inv.invoice_view(_invoice(status="open"), None, [first, part], [])["actions"] == ["record_payment"]


def test_translate() -> None:
    assert isinstance(inv._translate(_pg("CRM39")), Conflict)
    exists = inv._translate(_pg("23505", 'duplicate key value violates unique constraint "invoice_schedule_live_uq"'))
    assert isinstance(exists, Conflict) and exists.code == "invoice_exists"
    other = _pg("23514")
    assert inv._translate(other) is other


# ── settings ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_defaults_and_update() -> None:
    assert await inv.settings(tenant_ctx()) == inv.DEFAULT_SETTINGS
    assert (await inv.get_settings(tenant_ctx()))["invoice_tax_rate"] == "0"
    conn = ScriptedConn()
    ctx = tenant_ctx(conn)
    out = await inv.update_settings(ctx, {"invoice_prefix": "SV-26", "invoice_tax_rate": Decimal("8.5"),
                                          "invoice_tax_label": " VAT ", "unknown": 1})  # fmt: skip
    assert out["invoice_prefix"] == "SV-26" and out["invoice_tax_rate"] == "8.5" and out["invoice_tax_label"] == "VAT"
    assert conn.sql("INSERT INTO crm.tenant_settings")[0][:3] == ("SV-26", "VAT", Decimal("8.5"))
    for changes, code in (
        ({"invoice_prefix": "inv"}, "invalid_invoice_prefix"),
        ({"invoice_tax_rate": "101"}, "invalid_tax_rate"),
        ({"invoice_tax_rate": "abc"}, "invalid_tax_rate"),
        ({"invoice_tax_rate": "1.0005"}, "invalid_tax_rate"),
        ({"invoice_due_days": 400}, "invalid_invoice_due_days"),
        ({"invoice_due_days": True}, "invalid_invoice_due_days"),
        ({"service_fee_enabled": "yes"}, "invalid_service_fee_enabled"),
        ({"invoice_tax_label": "a\tb"}, "invalid_invoice_tax_label"),
    ):
        with _raises(Invalid, code):
            await inv.update_settings(tenant_ctx(), changes)


@pytest.mark.asyncio
async def test_settings_read_the_row() -> None:
    row = {**inv.DEFAULT_SETTINGS, "invoice_prefix": "SV", "service_fee_enabled": True}
    conn = ScriptedConn().on("FROM crm.tenant_settings", row)
    assert (await inv.settings(tenant_ctx(conn)))["invoice_prefix"] == "SV"


# ── reads ─────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_and_lists() -> None:
    with _raises(NotFound, "invoice_not_found"):
        await inv.get(tenant_ctx(), INVOICE)
    out = await inv.get(tenant_ctx(_conn(_invoice())), INVOICE)
    assert out["tax_rate"] == "10" and out["lines"][0]["quantity"] == "1" and out["lines"][0]["amount"] == "300.00"
    assert out["schedule_id"] == str(SCHEDULE)
    with _raises(NotFound, "trip_not_found"):
        await inv.list_for_trip(tenant_ctx(), TRIP)
    conn = _conn().on("FROM crm.invoice WHERE trip_id", [_invoice(schedule_id=None, proposal_id=None)])
    listed = await inv.list_for_trip(tenant_ctx(conn), TRIP)
    assert listed["invoices"][0]["schedule_id"] is None and "lines" not in listed["invoices"][0]


@pytest.mark.asyncio
async def test_lists_carry_the_agency_timezone() -> None:
    """S261 live finding F2: the claim note's deadline is printed in the agency timezone (with its label) on the
    trip's Invoices card and on /payments alike; both lists name it."""
    conn = _conn().on("FROM crm.invoice WHERE trip_id", []).on("FROM crm.invoice i JOIN crm.trip", [])
    conn.on("timezone FROM crm.tenant_settings LIMIT 1", "Asia/Bangkok")
    assert (await inv.list_for_trip(tenant_ctx(conn), TRIP))["timezone"] == "Asia/Bangkok"
    assert (await inv.receivables(tenant_ctx(conn), None))["timezone"] == "Asia/Bangkok"
    # No settings row, or a name the zone database does not know: the default.
    for stored in (None, "", "Mars/Olympus", "../etc"):
        conn = _conn().on("FROM crm.invoice WHERE trip_id", []).on("timezone FROM crm.tenant_settings LIMIT 1", stored)
        assert (await inv.list_for_trip(tenant_ctx(conn), TRIP))["timezone"] == inv.DEFAULT_TZ == "Asia/Ho_Chi_Minh"


@pytest.mark.asyncio
async def test_receivables() -> None:
    with _raises(Invalid, "invalid_status"):
        await inv.receivables(tenant_ctx(_conn()), "unknown")
    facts = {k: _invoice()[k] for k in ("schedule_id", "claimed_at", "hold_extended_at", "hold_extend_code",
                                        "rehold_requested_at", "rehold_code", "proposal_kind", "accepted_hold_id",
                                        "hold_id", "hold_expires_at")}  # fmt: skip
    rows = [
        {**facts, "invoice_id": INVOICE, "trip_id": TRIP, "trip_title": "Laos", "kind": "deposit", "status": "open",
         "number": "INV-000001", "currency": "USD", "total_amount": Decimal("300"), "due_on": TODAY - dt.timedelta(1),
         "issue_date": TODAY - dt.timedelta(5), "paid_at": None, "paid_amount": Decimal("100")},
        {**facts, "invoice_id": uuid.uuid4(), "trip_id": TRIP, "trip_title": "Laos", "kind": "balance",
         "status": "open", "number": "INV-000002", "currency": "USD", "total_amount": Decimal("700"), "due_on": None,
         "issue_date": TODAY, "paid_at": None, "paid_amount": Decimal(0)},
    ]  # fmt: skip
    conn = _conn().on("FROM crm.invoice i JOIN crm.trip", rows)
    out = await inv.receivables(tenant_ctx(conn), None)
    assert [r["overdue"] for r in out["invoices"]] == [True, False] and out["today"] == "2026-10-30"
    assert [r["balance_amount"] for r in out["invoices"]] == ["200.00", "700.00"]
    assert [r["pay_window"] for r in out["invoices"]] == [{"state": "open", "until": None}] * 2
    assert conn.sql("FROM crm.invoice i JOIN crm.trip")[0][0] == list(inv.RECEIVABLE)
    await inv.receivables(tenant_ctx(conn), "paid")
    assert conn.sql("FROM crm.invoice i JOIN crm.trip")[1][0] == ["paid"]


# ── create ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_from_schedule_refusals() -> None:
    with _raises(NotFound, "trip_not_found"):
        await inv.create_from_schedule(tenant_ctx(), TRIP, SCHEDULE)
    with _raises(NotFound, "schedule_not_found"):
        await inv.create_from_schedule(tenant_ctx(_conn()), TRIP, SCHEDULE)
    for row, code in ((_schedule(status="paid"), "schedule_not_due"),
                      (_schedule(proposal_status="superseded"), "proposal_not_accepted")):  # fmt: skip
        with _raises(Conflict, code):
            await inv.create_from_schedule(tenant_ctx(_conn().on("FROM crm.payment_schedule s", row)), TRIP, SCHEDULE)
    exists = _conn().on("FROM crm.payment_schedule s", _schedule()).on("status <> 'void'", INVOICE)
    with _raises(Conflict, "invoice_exists"):
        await inv.create_from_schedule(tenant_ctx(exists), TRIP, SCHEDULE)
    race = _conn().on("FROM crm.payment_schedule s", _schedule())
    race.on("INSERT INTO crm.invoice (", _pg("23505", "invoice_schedule_live_uq"))
    with _raises(Conflict, "invoice_exists"):
        await inv.create_from_schedule(tenant_ctx(race), TRIP, SCHEDULE)


@pytest.mark.asyncio
async def test_create_from_schedule_bills_the_row_with_included_lines() -> None:
    conn = _conn(_invoice()).on("FROM crm.payment_schedule s", _schedule())
    conn.on("FROM crm.tenant_settings", {**inv.DEFAULT_SETTINGS, "invoice_tax_rate": Decimal("10"),
                                         "invoice_tax_inclusive": False})  # fmt: skip
    conn.on("FROM crm.booking_line", [{"title": "Halong cruise", "supplier_kind": "aa"},
                                      {"title": " \n ", "supplier_kind": "agency"},
                                      {"title": "Hanoi hotel\t3 nights", "supplier_kind": "agency"}])  # fmt: skip
    out = await inv.create_from_schedule(tenant_ctx(conn), TRIP, SCHEDULE)
    assert out["status"] == "draft"
    insert = conn.sql("INSERT INTO crm.invoice (")[0]
    assert insert[2:6] == ("deposit", SCHEDULE, PROPOSAL, "USD") and insert[8] is True  # always inclusive
    lines = conn.sql("INSERT INTO crm.invoice_line")
    assert [(a[2], a[3], a[4], a[5]) for a in lines] == [
        (1, "charge", "schedule", "Deposit (proposal version 2)"),
        (2, "included", "aa_line", "Halong cruise"),
        (3, "included", "agency_line", "Hanoi hotel 3 nights"),
    ]
    assert conn.sql("UPDATE crm.invoice SET subtotal_amount")[0][1:] == (Decimal("272.73"), Decimal("27.27"),
                                                                       Decimal("300.00"))  # fmt: skip
    assert conn.sql("last_activity_at")


@pytest.mark.asyncio
async def test_create_typed() -> None:
    fee = {"kind": "service_fee", "currency": "USD", "lines": [{"description": "Fee", "unit_amount": "50"}]}
    with _raises(Invalid, "invalid_kind"):
        await inv.create_typed(tenant_ctx(), TRIP, **{**fee, "kind": "deposit"})
    with _raises(Invalid, "invalid_currency"):
        await inv.create_typed(tenant_ctx(), TRIP, **{**fee, "currency": "usd"})
    with _raises(NotFound, "trip_not_found"):
        await inv.create_typed(tenant_ctx(), TRIP, **fee)
    with _raises(Conflict, "service_fee_disabled"):
        await inv.create_typed(tenant_ctx(_conn()), TRIP, **fee)
    conn = _conn(_invoice(kind="adjustment", schedule_id=None, proposal_id=None))
    conn.on("FROM crm.booking_line", [{"title": "Tour", "supplier_kind": "aa"}])
    out = await inv.create_typed(tenant_ctx(conn), TRIP, **{**fee, "kind": "adjustment"}, memo=" Note ",
                                 include_trip_lines=True)  # fmt: skip
    assert out["kind"] == "adjustment"
    insert = conn.sql("INSERT INTO crm.invoice (")[0]
    assert insert[2] == "adjustment" and insert[9] == "Note"
    assert [a[4] for a in conn.sql("INSERT INTO crm.invoice_line")] == ["manual", "aa_line"]
    on = _conn(_invoice(kind="service_fee")).on("FROM crm.tenant_settings",
                                                 {**inv.DEFAULT_SETTINGS, "service_fee_enabled": True})  # fmt: skip
    await inv.create_typed(tenant_ctx(on), TRIP, **fee)
    assert [a[4] for a in on.sql("INSERT INTO crm.invoice_line")] == ["service_fee"]
    with _raises(Invalid, "memo_too_long"):
        await inv.create_typed(tenant_ctx(on), TRIP, **fee, memo="x" * 2001)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "closed", [{"stage": "cancelled", "cancelling": False}, {"stage": "booked", "cancelling": True}]
)
async def test_a_cancelled_trip_takes_no_new_invoice(closed: dict[str, Any]) -> None:
    """S262 (C12 §2.7): a cancelled trip, or one whose cancellation waits for AA, bills the client no more: no invoice
    from its schedule, no typed invoice, no issue of a draft (409 trip_cancelled), and nothing is written."""
    fee = {"kind": "adjustment", "currency": "USD", "lines": [{"description": "Fee", "unit_amount": "50"}]}
    conn = _conn(_invoice()).on("FROM crm.payment_schedule s", _schedule()).on("crm.invoice_sequence", 7)
    conn.on(" ".join(cancellation.CLOSED_SQL.split()), closed)
    with _raises(Conflict, "trip_cancelled"):
        await inv.create_from_schedule(tenant_ctx(conn), TRIP, SCHEDULE)
    with _raises(Conflict, "trip_cancelled"):
        await inv.create_typed(tenant_ctx(conn), TRIP, **fee)
    with _raises(Conflict, "trip_cancelled"):
        await inv.issue(tenant_ctx(conn), INVOICE)
    assert not any(sql.startswith(("INSERT", "UPDATE")) for sql, _ in conn.calls)


# ── draft edits ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_draft() -> None:
    with _raises(Conflict, "invoice_not_draft"):
        await inv.update_draft(tenant_ctx(_conn(_invoice(status="open"))), INVOICE, {"memo": "x"})
    with _raises(Conflict, "schedule_invoice_fixed"):
        await inv.update_draft(tenant_ctx(_conn(_invoice())), INVOICE, {"lines": []})
    conn = _conn(_invoice())
    await inv.update_draft(tenant_ctx(conn), INVOICE, {"memo": "Thanks", "tax_rate": Decimal("5"), "tax_label": "GST",
                                                       "due_on": TODAY})  # fmt: skip
    assert conn.sql("UPDATE crm.invoice SET memo")[0][1:] == ("Thanks", TODAY, Decimal("5"), "GST", True)
    typed = _invoice(kind="service_fee", schedule_id=None, proposal_id=None, tax_inclusive=False)
    conn = _conn(typed, [_line(1), _line(2, "included")])
    conn.on("FROM crm.booking_line", [{"title": "Tour", "supplier_kind": "aa"}])
    await inv.update_draft(tenant_ctx(conn), INVOICE, {"lines": [{"description": "Fee", "unit_amount": "20"}],
                                                       "tax_inclusive": True, "memo": None})  # fmt: skip
    assert conn.sql("DELETE FROM crm.invoice_line") and [a[2] for a in conn.sql("INSERT INTO crm.invoice_line")] == [
        1,
        2,
    ]
    assert conn.sql("UPDATE crm.invoice SET memo")[0][1] is None
    sealed = _conn(typed).on("UPDATE crm.invoice SET memo", _pg("CRM39"))
    with _raises(Conflict, "invoice_not_draft"):
        await inv.update_draft(tenant_ctx(sealed), INVOICE, {"memo": "x"})


# ── issue and void ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue() -> None:
    with _raises(Conflict, "invoice_not_draft"):
        await inv.issue(tenant_ctx(_conn(_invoice(status="void"))), INVOICE)
    with _raises(Conflict, "schedule_not_due"):
        await inv.issue(tenant_ctx(_conn(_invoice())), INVOICE)  # the schedule row is gone
    paid = _conn(_invoice()).on("FROM crm.payment_schedule s", _schedule(status="paid"))
    with _raises(Conflict, "schedule_not_due"):
        await inv.issue(tenant_ctx(paid), INVOICE)
    empty = _conn(_invoice(kind="adjustment", schedule_id=None, total_amount=Decimal(0)))
    with _raises(Conflict, "invoice_empty"):
        await inv.issue(tenant_ctx(empty), INVOICE)
    conn = _conn(_invoice()).on("FROM crm.payment_schedule s", _schedule()).on("crm.invoice_sequence", 7)
    await inv.issue(tenant_ctx(conn), INVOICE)
    args = conn.sql("SET status = 'open'")[0]
    assert args[1:5] == (7, "INV-000007", TODAY, TODAY + dt.timedelta(days=7)) and args[5].startswith("member:")
    assert conn.sql("INSERT INTO crm.trip_commitment")[0] == (TRIP, "open", f"invoice:{INVOICE}")
    late = _conn(_invoice(due_on=TODAY - dt.timedelta(days=3))).on("FROM crm.payment_schedule s", _schedule())
    late.on("crm.invoice_sequence", 8)
    await inv.issue(tenant_ctx(late), INVOICE)
    assert late.sql("SET status = 'open'")[0][4] == TODAY  # never due before the issue date
    broken = _conn(_invoice()).on("FROM crm.payment_schedule s", _schedule()).on("crm.invoice_sequence", 9)
    broken.on("SET status = 'open'", _pg("CRM39"))
    with _raises(Conflict, "invoice_not_draft"):
        await inv.issue(tenant_ctx(broken), INVOICE)


@pytest.mark.asyncio
async def test_void() -> None:
    with _raises(Invalid, "invalid_reason"):
        await inv.void(tenant_ctx(), INVOICE, " ")
    with _raises(Conflict, "invoice_not_voidable"):
        await inv.void(tenant_ctx(_conn(_invoice(status="paid"))), INVOICE, "x")

    def held(money: str, refund_open: bool = False) -> ScriptedConn:
        block = {"held": Decimal(money), "refund_open": refund_open}
        return _conn(_invoice(status="open", issued_at=NOW, number="INV-000001")).on("AS held", block)

    with _raises(Conflict, "invoice_has_payments"):
        await inv.void(tenant_ctx(held("300")), INVOICE, "x")
    with _raises(Conflict, "refund_pending"):  # every payment refunded but a request still open (defensive)
        await inv.void(tenant_ctx(held("0", True)), INVOICE, "x")
    # Paid in part, then refunded in full (the trip was cancelled): the invoice can be voided, the row billed again.
    refunded = held("0")
    out_void = await inv.void(tenant_ctx(refunded), INVOICE, "Trip cancelled")
    assert refunded.sql("SET status = 'void'")[0][2] == "Trip cancelled" and out_void["invoice_id"] == str(INVOICE)
    draft = _conn(_invoice())
    await inv.void(tenant_ctx(draft), INVOICE, "Not needed")
    assert draft.sql("SET status = 'void'")[0][2] == "Not needed" and not draft.sql("crm.trip_commitment")
    issued = _conn(_invoice(status="open", issued_at=NOW, number="INV-000001"))
    await inv.void(tenant_ctx(issued), INVOICE, "Wrong amount")
    assert issued.sql("INSERT INTO crm.trip_commitment")[0][1] == "void"


# ── the document ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_document_and_bill_to() -> None:
    conn = _conn(_invoice(status="open", number="INV-000001", issue_date=TODAY, due_on=TODAY, issued_at=NOW))
    conn.on("SELECT display_name FROM crm.tenant", "Sen Vang Travel")
    conn.on("tt.role = 'lead'", {"traveler_id": LEAD, "display_name_enc": enc("traveler", "display_name_enc",
                                                                              "Grace <Lee>")})  # fmt: skip
    out = await inv.document(tenant_ctx(conn), FakeVault(), INVOICE)  # type: ignore[arg-type]
    assert out["filename"] == "INV-000001.html"
    assert "Grace &lt;Lee&gt;" in out["html"] and "Sen Vang Travel" in out["html"]
    none = _conn(_invoice())
    assert await inv.bill_to(tenant_ctx(none), FakeVault(), TRIP) is None  # type: ignore[arg-type]
    blank = ScriptedConn().on("tt.role = 'lead'", {"traveler_id": LEAD, "display_name_enc": None})
    assert await inv.bill_to(tenant_ctx(blank), FakeVault(), TRIP) is None  # type: ignore[arg-type]
    draft = await inv.document(tenant_ctx(_conn(_invoice())), FakeVault(), INVOICE)  # type: ignore[arg-type]
    assert draft["filename"] == "draft-invoice.html" and "DRAFT" in draft["html"]


def _view(**over: Any) -> dict[str, Any]:
    view = inv.invoice_view(_invoice(**over), [_line(1, description="<b>Deposit</b>"), _line(2, "included")])
    return view


def test_render_escapes_and_shows_no_prices_for_included_lines() -> None:
    html = render.render(_view(memo="<script>alert(1)</script>"), agency_name="A & B", bill_to=None)
    assert "<script>" not in html and "&lt;script&gt;" in html and "A &amp; B" in html and "&lt;b&gt;Deposit" in html
    assert "This invoice covers:<ul><li>Halong cruise</li></ul>" in html and "Bill to" not in html
    assert "VAT 10% (included)" in html and "USD 300.00" in html
    zero = render.render(_view(tax_amount=Decimal(0), subtotal_amount=Decimal(300)), agency_name=None, bill_to="X")
    assert "(included)" not in html.replace("VAT 10% (included)", "") and ">Total<" in zero
    exclusive = render.render(_view(tax_inclusive=False, kind="service_fee"), agency_name="A", bill_to="B")
    assert "VAT 10%</td>" in exclusive and "Service fee invoice" in exclusive
    void = render.render(_view(status="void", void_reason="<x>", number="INV-000003", issue_date=TODAY,
                               due_on=TODAY), agency_name="A", bill_to="B")  # fmt: skip
    assert "VOID: &lt;x&gt;" in void and "INV-000003" in void and "30 Oct 2026" in void


def test_render_payments_and_instructions() -> None:
    html = render.render(
        _view(status="open", number="INV-000004"),
        agency_name="A",
        bill_to="B",
        pay_instructions=[{"display_name": "Bank transfer", "instructions": "Account <123>"}],
        payments=[
            {"entry_kind": "payment", "amount": "100.00", "received_on": "2026-10-29"},
            {"entry_kind": "refund", "amount": "20.00", "received_on": "2026-10-30"},
        ],  # fmt: skip
        balance="220.00",
    )
    assert "Paid 29 Oct 2026" in html and "Refunded 30 Oct 2026" in html and "USD 220.00" in html
    assert "Account &lt;123&gt;" in html
    assert render._amount(None, "USD") == "" and render._date(None) == ""


def _pay_row(amount: str, kind: str = "payment", **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"payment_id": uuid.uuid4(), "invoice_id": INVOICE, "entry_kind": kind,
                           "amount": Decimal(amount), "currency": "USD", "method": "cash", "received_on": TODAY,
                           "provider": "manual", "provider_ref": None, "refund_of": None,
                           "created_at": NOW}  # fmt: skip
    row.update(over)
    return row


def test_money_summary() -> None:
    first, second = _pay_row("100"), _pay_row("250")
    refund = _pay_row("40", "refund", refund_of=first["payment_id"])
    asks = [
        {"refund_id": uuid.uuid4(), "invoice_id": INVOICE, "payment_id": first["payment_id"], "amount": Decimal("40"),
         "currency": "USD", "reason": "x", "status": "executed", "requested_by": "member:a", "decided_by": "member:o",
         "decided_at": NOW, "decision_note": None, "executed_at": NOW},
        {"refund_id": uuid.uuid4(), "invoice_id": INVOICE, "payment_id": first["payment_id"], "amount": Decimal("30"),
         "currency": "USD", "reason": "y", "status": "rejected", "requested_by": "member:a", "decided_by": "member:o",
         "decided_at": NOW, "decision_note": "no", "executed_at": None},
    ]  # fmt: skip
    out = inv.money_summary(Decimal("300"), [first, second, refund], asks)
    assert (out["paid_amount"], out["refunded_amount"], out["balance_amount"]) == ("350.00", "40.00", "0.00")
    assert [p.get("refundable_amount") for p in out["payments"]] == ["60.00", "250.00", None]
    assert out["payments"][2]["refund_of"] == str(first["payment_id"]) and len(out["refund_requests"]) == 2
    view = inv.invoice_view(_invoice(status="open"), None, [first], [])
    assert view["actions"] == ["record_payment"] and view["balance_amount"] == "200.00"


@pytest.mark.asyncio
async def test_trip_list_and_document_carry_the_money() -> None:
    payment = _pay_row("100")
    conn = _conn(_invoice(status="open", number="INV-000001", issued_at=NOW, issue_date=TODAY, due_on=TODAY))
    conn.on("FROM crm.invoice WHERE trip_id", [_invoice(status="open"), _invoice(invoice_id=uuid.uuid4())])
    conn.on("FROM crm.payment WHERE trip_id", [payment]).on("FROM crm.payment WHERE invoice_id", [payment])
    listed = await inv.list_for_trip(tenant_ctx(conn), TRIP)
    assert [i["paid_amount"] for i in listed["invoices"]] == ["100.00", "0.00"]
    conn.on("FROM crm.payment_provider_account", [{"display_name": "Bank", "instructions": "Acct 9"}])
    doc = await inv.document(tenant_ctx(conn), FakeVault(), INVOICE)  # type: ignore[arg-type]
    assert "Acct 9" in doc["html"] and "Paid 30 Oct 2026" in doc["html"] and "USD 200.00" in doc["html"]
    paid = _conn(_invoice(status="paid", paid_at=NOW))
    paid.on("FROM crm.payment_provider_account", [{"display_name": "B", "instructions": "Z9"}])
    assert "Z9" not in (await inv.document(tenant_ctx(paid), FakeVault(), INVOICE))["html"]  # type: ignore[arg-type]
