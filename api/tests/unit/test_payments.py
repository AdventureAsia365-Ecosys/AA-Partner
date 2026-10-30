"""AA-952 slice 3: the payment service's Python branches over a :class:`ScriptedConn` (the SQL, the ledger and the
guards run on PostgreSQL in tests/integration/test_payments_api.py and test_crm024_schema.py), and the providers."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest

from app.errors import Conflict, Forbidden, Invalid, NotFound, ServiceError
from app.ledger.fx import Rate
from app.payments import providers
from app.payments import service as pay
from app.proposals import service as proposals
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault, tenant_ctx

TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
INVOICE = uuid.UUID("00000000-0000-4000-8000-0000000000c2")
SCHEDULE = uuid.UUID("00000000-0000-4000-8000-0000000000c3")
PAYMENT = uuid.UUID("00000000-0000-4000-8000-0000000000c4")
REFUND = uuid.UUID("00000000-0000-4000-8000-0000000000c5")
ACCOUNT = uuid.UUID("00000000-0000-4000-8000-0000000000c6")
TODAY = dt.date(2026, 10, 30)
NOW = dt.datetime(2026, 10, 30, 9, 0, tzinfo=dt.UTC)


@contextmanager
def _raises(cls: type[ServiceError], code: str) -> Iterator[None]:
    with pytest.raises(cls) as err:
        yield
    assert err.value.code == code, err.value.code


def _invoice(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"invoice_id": INVOICE, "trip_id": TRIP, "kind": "deposit", "status": "open",
                           "schedule_id": SCHEDULE, "currency": "USD", "total_amount": Decimal("300"),
                           "number": "INV-000001"}  # fmt: skip
    row.update(over)
    return row


def _payment(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"payment_id": PAYMENT, "trip_id": TRIP, "invoice_id": INVOICE, "schedule_id": SCHEDULE,
                           "amount": Decimal("300"), "currency": "USD", "provider": "manual", "provider_ref": "TT1",
                           "entry_kind": "payment"}  # fmt: skip
    row.update(over)
    return row


def _refund(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"refund_id": REFUND, "trip_id": TRIP, "invoice_id": INVOICE, "payment_id": PAYMENT,
                           "amount": Decimal("120"), "currency": "USD", "status": "proposed",
                           "requested_by": "member:a", "reason": "Shortened", "decided_by": None, "decided_at": None,
                           "decision_note": None, "executed_at": None, "refund_payment_id": None,
                           "created_at": NOW}  # fmt: skip
    row.update(over)
    return row


def _conn() -> ScriptedConn:
    conn = ScriptedConn().on("AT TIME ZONE", TODAY)
    conn.on("FROM crm.invoice WHERE invoice_id = $1 AND deleted_at IS NULL FOR UPDATE", _invoice())
    conn.on("FROM crm.payment_schedule s", {"schedule_id": SCHEDULE, "status": "due", "kind": "balance"})
    return conn


@pytest.fixture
def full(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    seen: list[uuid.UUID] = []

    async def fake(_ctx: Any, invoice_id: uuid.UUID) -> dict[str, Any]:
        seen.append(invoice_id)
        return {"invoice_id": str(invoice_id)}

    monkeypatch.setattr(pay.invoices, "full", fake)
    return seen


# ── providers ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_providers() -> None:
    manual = providers.provider("manual")
    assert manual.name == "manual" and not manual.takes_cards and not manual.needs_external_account
    result = await manual.refund(amount=Decimal(1), currency="USD", original_ref=None, reference="RF")
    assert result.provider_ref == "RF"
    with _raises(Conflict, "provider_not_available"):
        providers.provider("stripe")
    with _raises(Conflict, "unknown_provider"):
        providers.provider("paypal")
    assert providers.available() == [{"provider": "manual", "available": True},
                                     {"provider": "stripe", "available": False},
                                     {"provider": "vn_gateway", "available": False}]  # fmt: skip


# ── record a payment ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_payment_refusals(full: list) -> None:
    v = FakeVault()
    for amount, code in (("0", "invalid_amount"), ("1.001", "invalid_amount"), ("x", "invalid_amount"),
                         ("NaN", "invalid_amount")):  # fmt: skip
        with _raises(Invalid, code):
            await pay.record_payment(tenant_ctx(), v, INVOICE, amount=amount, method="cash", received_on=None)
    with _raises(Invalid, "invalid_method"):
        await pay.record_payment(tenant_ctx(), v, INVOICE, amount="1", method="crypto", received_on=None)
    with _raises(Invalid, "invalid_reference"):
        await pay.record_payment(tenant_ctx(), v, INVOICE, amount="1", method="cash", received_on=None,
                                 reference="a\nb")  # fmt: skip
    with _raises(Invalid, "reference_too_long"):
        await pay.record_payment(tenant_ctx(), v, INVOICE, amount="1", method="cash", received_on=None,
                                 reference="x" * 121)  # fmt: skip
    with _raises(Invalid, "invalid_reference"):
        await pay.record_payment(tenant_ctx(), v, INVOICE, amount="1", method="cash", received_on=None, reference=5)
    with _raises(NotFound, "invoice_not_found"):
        await pay.record_payment(tenant_ctx(), v, INVOICE, amount="1", method="cash", received_on=None)
    draft = _conn().on("FOR UPDATE", _invoice(status="draft"))
    with _raises(Conflict, "invoice_not_payable"):
        await pay.record_payment(tenant_ctx(draft), v, INVOICE, amount="1", method="cash", received_on=None)
    over = _conn().on("entry_kind = 'payment'", Decimal("250"))
    with _raises(Invalid, "amount_above_balance"):
        await pay.record_payment(tenant_ctx(over), v, INVOICE, amount="60", method="cash", received_on=None)
    future = _conn().on("entry_kind = 'payment'", Decimal(0))
    with _raises(Invalid, "received_in_future"):
        await pay.record_payment(tenant_ctx(future), v, INVOICE, amount="60", method="cash",
                                 received_on=TODAY + dt.timedelta(days=1))  # fmt: skip
    assert full == []


@pytest.mark.asyncio
async def test_a_partial_payment_leaves_the_invoice_open(full: list) -> None:
    conn = _conn().on("entry_kind = 'payment'", Decimal(0))
    out = await pay.record_payment(tenant_ctx(conn), FakeVault(), INVOICE, amount=Decimal("100"),
                                   method="bank_transfer", received_on=TODAY, reference=" TT 9 ")  # fmt: skip
    insert = conn.sql("INSERT INTO crm.payment")[0]
    assert (
        insert[1:4] == (TRIP, SCHEDULE, INVOICE)
        and insert[4] == Decimal("100")
        and insert[8:] == ("manual", "TT 9", "payment", None)
    )
    assert conn.sql("INSERT INTO crm.trip_commitment")[0][1:3] == ("payment", "succeeded")
    assert not conn.sql("SET status = 'paid'") and out["trip_moved"] is None and full == [INVOICE]


@pytest.mark.asyncio
async def test_the_last_payment_pays_the_invoice_and_settles_the_row(
    full: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    settled: list[Any] = []

    async def settle(_ctx: Any, _vault: Any, row: Any) -> dict[str, Any]:
        settled.append(row["schedule_id"])
        return {"trip_moved": {"stage": "booked"}, "portal": {"invited": []}}

    monkeypatch.setattr(proposals, "settle_schedule", settle)
    conn = _conn().on("entry_kind = 'payment'", Decimal("100"))
    out = await pay.record_payment(tenant_ctx(conn), FakeVault(), INVOICE, amount="200", method="cash",
                                   received_on=None)  # fmt: skip
    assert conn.sql("SET status = 'paid', paid_at = now() WHERE invoice_id")
    assert [a[1:3] for a in conn.sql("INSERT INTO crm.trip_commitment")] == [("payment", "succeeded"),
                                                                            ("invoice", "paid")]  # fmt: skip
    assert settled == [SCHEDULE] and out["trip_moved"] == {"stage": "booked"}
    # A typed invoice has no schedule row.
    typed = _conn().on("FOR UPDATE", _invoice(schedule_id=None, kind="service_fee")).on("entry_kind = 'payment'", 0)
    await pay.record_payment(tenant_ctx(typed), FakeVault(), INVOICE, amount="300", method="cash", received_on=None)
    assert settled == [SCHEDULE] and not typed.sql("FROM crm.payment_schedule s")


@pytest.mark.asyncio
async def test_a_schedule_invoice_takes_no_payment_once_its_row_is_not_due(full: list) -> None:
    """One row, one path: a row paid or void elsewhere (or gone) refuses the invoice's payments, nothing recorded."""
    for schedule in ({"schedule_id": SCHEDULE, "status": "void"}, {"schedule_id": SCHEDULE, "status": "paid"}, None):
        conn = _conn().on("entry_kind = 'payment'", 0).on("FROM crm.payment_schedule s", schedule)
        with _raises(Conflict, "schedule_not_due"):
            await pay.record_payment(tenant_ctx(conn), FakeVault(), INVOICE, amount="10", method="cash",
                                     received_on=None)  # fmt: skip
        assert not conn.sql("INSERT INTO crm.payment")
    # The invoice row is locked before the schedule row.
    order = [i for i, (sql, _) in enumerate(conn.calls) if "FOR UPDATE" in sql]
    assert "crm.invoice" in conn.calls[order[0]][0] and "crm.payment_schedule" in conn.calls[order[1]][0]
    assert full == []


# ── refunds ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_refund() -> None:
    with _raises(Invalid, "invalid_reason"):
        await pay.request_refund(tenant_ctx(), PAYMENT, amount="1", reason=" ")
    with _raises(NotFound, "payment_not_found"):
        await pay.request_refund(tenant_ctx(), PAYMENT, amount="1", reason="x")
    refund_entry = _conn().on("FROM crm.payment p WHERE", _payment(entry_kind="refund"))
    with _raises(NotFound, "payment_not_found"):
        await pay.request_refund(tenant_ctx(refund_entry), PAYMENT, amount="1", reason="x")
    bare = _conn().on("FROM crm.payment p WHERE", _payment(invoice_id=None))
    with _raises(Conflict, "payment_without_invoice"):
        await pay.request_refund(tenant_ctx(bare), PAYMENT, amount="1", reason="x")
    gone = ScriptedConn().on("FROM crm.payment p WHERE", _payment())
    with _raises(NotFound, "invoice_not_found"):
        await pay.request_refund(tenant_ctx(gone), PAYMENT, amount="1", reason="x")
    asked = _conn().on("FROM crm.payment p WHERE", _payment()).on("status <> 'rejected'", Decimal("250"))
    with _raises(Invalid, "amount_above_refundable"):
        await pay.request_refund(tenant_ctx(asked), PAYMENT, amount="60", reason="x")
    with _raises(Invalid, "reason_too_long"):
        await pay.request_refund(tenant_ctx(asked), PAYMENT, amount="10", reason="x" * 501)
    with _raises(Invalid, "invalid_reason"):
        await pay.request_refund(tenant_ctx(asked), PAYMENT, amount="10", reason="a\x00b")
    ok = _conn().on("FROM crm.payment p WHERE", _payment()).on("status <> 'rejected'", Decimal(0))
    ok.on("FROM crm.refund_request WHERE refund_id", _refund())
    out = await pay.request_refund(tenant_ctx(ok), PAYMENT, amount="120", reason="Line\\nshortened")
    assert out["status"] == "proposed" and ok.sql("INSERT INTO crm.refund_request")[0][4:] == (
        Decimal("120"),
        "USD",
        "Line\\nshortened",
    )
    missing = _conn().on("FROM crm.payment p WHERE", _payment()).on("status <> 'rejected'", Decimal(0))
    with _raises(NotFound, "refund_not_found"):
        await pay.request_refund(tenant_ctx(missing), PAYMENT, amount="1", reason="x")


@pytest.mark.asyncio
async def test_base_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await pay.base_amount(tenant_ctx(_conn()), Decimal("10"), "USD") == ("USD", Decimal("10"))

    async def no_rate(*_: Any) -> None:
        return None

    async def rate(*_: Any) -> Rate:
        return Rate(Decimal("0.00004"), TODAY, "platform_daily", 0)

    monkeypatch.setattr(pay.fx, "resolve", no_rate)
    assert await pay.base_amount(tenant_ctx(_conn()), Decimal("1000000"), "VND") == ("USD", None)
    monkeypatch.setattr(pay.fx, "resolve", rate)
    assert await pay.base_amount(tenant_ctx(_conn()), Decimal("1000000"), "VND") == ("USD", Decimal("40.00"))


@pytest.mark.asyncio
async def test_decide_refund_follows_the_matrix() -> None:
    with _raises(NotFound, "refund_not_found"):
        await pay.decide_refund(tenant_ctx(), REFUND, approve=True, note=None)
    done = _conn().on("r.deleted_at IS NULL FOR UPDATE", _refund(status="approved"))
    with _raises(Conflict, "refund_not_proposed"):
        await pay.decide_refund(tenant_ctx(done), REFUND, approve=True, note=None)

    def conn(threshold: Any) -> ScriptedConn:
        c = _conn().on("r.deleted_at IS NULL FOR UPDATE", _refund()).on("refund_owner_threshold_base", threshold)
        return c.on("FROM crm.refund_request WHERE refund_id", _refund(status="approved"))

    with _raises(Forbidden, "above_threshold"):  # 120 USD above the agent's 100
        await pay.decide_refund(tenant_ctx(conn(Decimal("100")), role="agent"), REFUND, approve=True, note=None)
    agent = conn(None)  # no settings row: the default 1,000
    await pay.decide_refund(tenant_ctx(agent, role="agent"), REFUND, approve=False, note=" Not due ")
    assert agent.sql("UPDATE crm.refund_request SET status = $2")[0][1:] == (
        "rejected",
        f"member:{tenant_ctx().member_id}",
        "Not due",
        "USD",
        Decimal("120.00"),
    )
    owner = conn(Decimal("100"))
    out = await pay.decide_refund(tenant_ctx(owner), REFUND, approve=True, note=None)
    assert out["status"] == "approved"
    gone = ScriptedConn().on("r.deleted_at IS NULL FOR UPDATE", _refund())
    with _raises(NotFound, "invoice_not_found"):
        await pay.decide_refund(tenant_ctx(gone), REFUND, approve=True, note=None)


@pytest.mark.asyncio
async def test_an_agent_never_approves_their_own_refund() -> None:
    def conn(requested_by: str) -> ScriptedConn:
        c = _conn().on("r.deleted_at IS NULL FOR UPDATE", _refund(requested_by=requested_by))
        c.on("refund_owner_threshold_base", Decimal("1000")).on("status IN ('approved', 'executed')", Decimal(0))
        return c.on("FROM crm.refund_request WHERE refund_id", _refund(status="approved"))

    me = f"member:{tenant_ctx().member_id}"
    mine = conn(me)
    with _raises(Forbidden, "refund_self_approval"):
        await pay.decide_refund(tenant_ctx(mine, role="agent"), REFUND, approve=True, note=None)
    assert not mine.sql("UPDATE crm.refund_request SET status = $2")
    # Their own request: an agent may still reject it (no money moves); the owner may approve it; another agent too.
    await pay.decide_refund(tenant_ctx(conn(me), role="agent"), REFUND, approve=False, note="Asked by mistake")
    assert (await pay.decide_refund(tenant_ctx(conn(me)), REFUND, approve=True, note=None))["status"] == "approved"
    other = conn("member:someone-else")
    await pay.decide_refund(tenant_ctx(other, role="agent"), REFUND, approve=True, note=None)
    assert other.sql("UPDATE crm.refund_request SET status = $2")[0][1] == "approved"


@pytest.mark.asyncio
async def test_a_split_refund_still_reaches_the_owner() -> None:
    """§4.6: an agent's approval counts what is approved on the invoice already (900 + 900 > 1,000)."""

    def conn(before: Any) -> ScriptedConn:
        c = _conn().on("r.deleted_at IS NULL FOR UPDATE", _refund(amount=Decimal("900"), requested_by="member:b"))
        c.on("refund_owner_threshold_base", Decimal("1000")).on("status IN ('approved', 'executed')", before)
        return c.on("FROM crm.refund_request WHERE refund_id", _refund(status="approved"))

    first = conn(Decimal(0))
    await pay.decide_refund(tenant_ctx(first, role="agent"), REFUND, approve=True, note=None)
    assert first.sql("status IN ('approved', 'executed')")[0] == (INVOICE, REFUND, "USD")
    assert first.sql("UPDATE crm.refund_request SET status = $2")[0][5] == Decimal("900.00")  # its own base amount
    with _raises(Forbidden, "above_threshold"):
        await pay.decide_refund(tenant_ctx(conn(Decimal("900")), role="agent"), REFUND, approve=True, note=None)
    with _raises(Forbidden, "above_threshold"):  # an earlier approval without a base amount: the owner decides
        await pay.decide_refund(tenant_ctx(conn(None), role="agent"), REFUND, approve=True, note=None)
    # A rejection is judged on the request alone; the owner approves any total.
    await pay.decide_refund(tenant_ctx(conn(Decimal("900")), role="agent"), REFUND, approve=False, note=None)
    await pay.decide_refund(tenant_ctx(conn(Decimal("900"))), REFUND, approve=True, note=None)


@pytest.mark.asyncio
async def test_execute_refund() -> None:
    with _raises(Invalid, "invalid_method"):
        await pay.execute_refund(tenant_ctx(), REFUND, method="x", refunded_on=None)
    with _raises(NotFound, "refund_not_found"):
        await pay.execute_refund(tenant_ctx(), REFUND, method="cash", refunded_on=None)
    proposed = _conn().on("r.deleted_at IS NULL FOR UPDATE", _refund())
    with _raises(Conflict, "refund_not_approved"):
        await pay.execute_refund(tenant_ctx(proposed), REFUND, method="cash", refunded_on=None)
    no_invoice = ScriptedConn().on("r.deleted_at IS NULL FOR UPDATE", _refund(status="approved"))
    with _raises(NotFound, "invoice_not_found"):
        await pay.execute_refund(tenant_ctx(no_invoice), REFUND, method="cash", refunded_on=None)

    def conn(refunded: Decimal) -> ScriptedConn:
        c = _conn().on("r.deleted_at IS NULL FOR UPDATE", _refund(status="approved", amount=Decimal("300")))
        c.on("FROM crm.payment p WHERE", _payment()).on("refund_of = $1", refunded)
        return c.on("FROM crm.refund_request WHERE refund_id", _refund(status="executed", executed_at=NOW))

    full_back = conn(Decimal("300"))
    out = await pay.execute_refund(tenant_ctx(full_back), REFUND, method="bank_transfer", refunded_on=TODAY,
                                   reference="RF 7")  # fmt: skip
    entry = full_back.sql("INSERT INTO crm.payment")[0]
    assert entry[4] == Decimal("300.00") and entry[8:] == ("manual", "RF 7", "refund", PAYMENT)
    assert full_back.sql("SET status = 'executed'")[0][1] == entry[0]
    assert full_back.sql("INSERT INTO crm.trip_commitment")[0][1:3] == ("payment", "refunded")
    assert out["status"] == "executed"
    part = conn(Decimal("100"))
    await pay.execute_refund(tenant_ctx(part), REFUND, method="cash", refunded_on=None)
    assert not part.sql("INSERT INTO crm.trip_commitment")


@pytest.mark.asyncio
async def test_list_refunds() -> None:
    with _raises(Invalid, "invalid_status"):
        await pay.list_refunds(tenant_ctx(), "maybe")
    row = {**_refund(), "trip_title": "Laos", "number": "INV-000001"}
    conn = ScriptedConn().on("FROM crm.refund_request r JOIN", [row])
    out = await pay.list_refunds(tenant_ctx(conn), None)
    assert out["refund_requests"][0]["invoice_number"] == "INV-000001"
    assert conn.sql("FROM crm.refund_request r JOIN")[0][0] == ["proposed", "approved"]
    await pay.list_refunds(tenant_ctx(conn), "executed")
    assert conn.sql("FROM crm.refund_request r JOIN")[1][0] == ["executed"]


# ── payment methods ───────────────────────────────────────────────────────────────────────────────


def _account(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"account_id": ACCOUNT, "provider": "manual", "status": "active", "display_name": "Bank",
                           "instructions": "Acct", "external_account_id": None, "default_currency": None,
                           "charges_enabled": False, "connected_at": NOW, "disconnected_at": None}  # fmt: skip
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_payment_methods() -> None:
    conn = ScriptedConn().on("FROM crm.payment_provider_account WHERE deleted_at", [_account(), _account(
        status="disconnected", disconnected_at=NOW, connected_at=None)])  # fmt: skip
    out = await pay.payment_methods(tenant_ctx(conn))
    assert out["accounts"][1]["connected_at"] is None and out["providers"][0]["provider"] == "manual"
    refusals = [("", "x", "invalid_display_name"), ("a\nb", "x", "invalid_display_name"),
                ("Bank", None, "invalid_instructions"), ("x" * 81, "x", "display_name_too_long")]  # fmt: skip
    for name, text, code in refusals:
        with _raises(Invalid, code):
            await pay.save_manual(tenant_ctx(), display_name=name, instructions=text)
    new = ScriptedConn()
    await pay.save_manual(tenant_ctx(new), display_name="Bank", instructions="Line 1\nLine 2")
    assert new.sql("INSERT INTO crm.payment_provider_account")[0][1:] == ("Bank", "Line 1\nLine 2")
    existing = ScriptedConn().on("provider = 'manual' AND status = 'active'", ACCOUNT)
    await pay.save_manual(tenant_ctx(existing), display_name="Cash", instructions="Office")
    assert existing.sql("UPDATE crm.payment_provider_account SET display_name")[0] == (ACCOUNT, "Cash", "Office")
    with _raises(Conflict, "use_manual_settings"):
        await pay.connect(tenant_ctx(), "manual")
    with _raises(Conflict, "provider_not_available"):
        await pay.connect(tenant_ctx(), "vn_gateway")
    with _raises(NotFound, "account_not_found"):
        await pay.disconnect(tenant_ctx(), ACCOUNT)
    gone = ScriptedConn().on("SET status = 'disconnected'", ACCOUNT)
    assert (await pay.disconnect(tenant_ctx(gone), ACCOUNT))["accounts"] == []
