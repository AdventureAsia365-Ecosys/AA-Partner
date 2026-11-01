"""AA-957 (card CRM-27) without a database: the ``/crm/v1/commissions`` routes reach their helpers and refuse the
assistant; the cancel route passes the refund choices and the kept commissions and adds Booking's fee to a dry run;
the ``booking.b2b.v1`` cancellation answer is read strictly; the client sends one cancel with its key and reason; the
sync turns a cancel answer into the right outcome. The services run on PostgreSQL in
``tests/integration/test_trip_cancel_api.py`` and ``test_crm035_schema.py``."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import cancellation, margin, stages
from app.booking import contract
from app.booking.client import BookingClient, BookingConfig
from app.booking.sync import Work, perform, trip_closing
from app.commissions import service
from app.errors import Invalid
from app.main import create_app
from app.routers import commissions as router
from app.routers import pipeline as pipeline_router
from tests.aa_fakes import FakeBooking, booking_client
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, fake_tx, tenant_ctx

ID = "00000000-0000-4000-8000-0000000000e7"
CANCELLED = {
    "booking_id": "b-h1",
    "dry_run": False,
    "fee": {"amount": "250.00", "currency": "USD"},
    "refundable_b2b_amount": {"amount": "750.00", "currency": "USD"},
    "fee_basis": {"policy_id": "pol-1", "days_before_departure": 40, "fee_pct": "25"},
    "cancelled_at": "2026-11-01T02:00:00Z",
}


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str, module: Any = router
) -> tuple[TestClient, dict[str, str]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), booking_client=None)
    ctx = tenant_ctx(ScriptedConn(), role=role)

    def tx(*a: Any, **kw: Any) -> Any:
        margin.current_member_role.set(role)
        return fake_tx(ctx)(*a, **kw)

    monkeypatch.setattr(module, "request_pool", _pool)
    monkeypatch.setattr(module, "tenant_tx", tx)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []

    def stub(name: str) -> Any:
        async def fn(*args: Any, **kwargs: Any) -> dict[str, Any]:
            out.append((name, args[1:], kwargs))
            return {"commission_id": ID, "commission_expected": {"amount": "40.00"}}

        return fn

    for name in ("listing", "get", "set_due", "claim", "unclaim", "receive", "write_off"):
        monkeypatch.setattr(service, name, stub(name))
    return out


def test_every_commission_route_reaches_its_helper(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    client, h = _client(pools, monkeypatch, "agent")
    requests = [
        ("GET", "/crm/v1/commissions?status=overdue", None),
        ("GET", f"/crm/v1/commissions/{ID}", None),
        ("PATCH", f"/crm/v1/commissions/{ID}", {"due_on": "2027-01-10"}),
        ("POST", f"/crm/v1/commissions/{ID}/claim", {"reference": "INV-SUP-1"}),
        ("POST", f"/crm/v1/commissions/{ID}/unclaim", None),
        ("POST", f"/crm/v1/commissions/{ID}/receive", {"amount": "35.00", "currency": "EUR"}),
        ("POST", f"/crm/v1/commissions/{ID}/write-off", {"reason": "too_small", "note": "Rounding"}),
    ]
    for method, path, body in requests:
        res = client.request(method, path, json=body, headers=h)
        assert res.status_code == 200, (method, path, res.text)
        assert res.json()["commission_expected"]["amount"] == "40.00"  # owner / agent see the money
    assert [c[0] for c in calls] == ["listing", "get", "set_due", "claim", "unclaim", "receive", "write_off"]
    assert calls[0][2] == {"status": "overdue", "trip_id": None}
    received = calls[5][1][1]
    assert received["amount"] == Decimal("35.00") and received["currency"] == "EUR"
    # Bodies are checked before any helper runs.
    for path, body in (
        (f"/crm/v1/commissions/{ID}/write-off", {"reason": "lost_it"}),
        (f"/crm/v1/commissions/{ID}/receive", {"amount": "-1", "currency": "USD"}),
        (f"/crm/v1/commissions/{ID}/receive", {"amount": "1", "currency": "usd"}),
    ):
        assert client.post(path, json=body, headers=h).status_code == 422


def test_the_assistant_never_reaches_a_commission(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    client, h = _client(pools, monkeypatch, "assistant")
    for method, path, body in (
        ("GET", "/crm/v1/commissions", None),
        ("GET", f"/crm/v1/commissions/{ID}", None),
        ("POST", f"/crm/v1/commissions/{ID}/receive", {"amount": "1.00", "currency": "USD"}),
    ):
        res = client.request(method, path, json=body, headers=h)
        assert res.status_code == 403 and res.json()["detail"] == "role_forbidden"
    assert calls == []


def test_the_cancel_route_passes_the_choices_and_adds_booking_fees(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, h = _client(pools, monkeypatch, "agent", pipeline_router)
    seen: dict[str, Any] = {}

    async def fake_cancel(ctx: Any, trip_id: uuid.UUID, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"cancel_reason_code": kw["reason"], "effects": [], "aa_bookings": [{"booking_id": "b-h1"}]}

    async def fees(client_: Any, tenant: uuid.UUID, out: dict[str, Any], reason: str) -> dict[str, Any]:
        seen["fees_reason"] = reason
        return {**out, "booking_unavailable": client_ is None}

    monkeypatch.setattr(stages, "cancel", fake_cancel)
    monkeypatch.setattr(cancellation, "booking_fees", fees)
    pay = str(uuid.uuid4())
    body = {"cancel_reason_code": "client_illness", "refunds": [{"payment_id": pay, "amount": "20.50"}],
            "keep_commission": [ID]}  # fmt: skip
    res = client.post(f"/crm/v1/trips/{ID}/cancel?dry_run=true", json=body, headers=h)
    assert res.status_code == 200 and res.json()["booking_unavailable"] is True
    assert seen["refunds"] == [{"payment_id": pay, "amount": "20.50"}] and seen["keep_commission"] == [ID]
    assert seen["fees_reason"] == "client_illness" and seen["dry_run"] is True
    seen.clear()
    assert client.post(f"/crm/v1/trips/{ID}/cancel?dry_run=false", json=body, headers=h).status_code == 200
    assert "fees_reason" not in seen  # Booking is asked only for a dry run (the run queues the cancel)
    bad = {**body, "refunds": [{"payment_id": pay, "amount": "-1"}]}
    assert client.post(f"/crm/v1/trips/{ID}/cancel", json=bad, headers=h).status_code == 422


def test_a_cancellation_answer_is_read_strictly() -> None:
    c = contract.cancellation(CANCELLED)
    assert (c.fee, c.currency, c.refundable, c.fee_pct, c.days_before_departure) == (
        Decimal("250.00"), "USD", Decimal("750.00"), "25", 40,
    )  # fmt: skip
    assert c.cancelled_at == dt.datetime(2026, 11, 1, 2, tzinfo=dt.UTC)
    assert contract.cancellation({**CANCELLED, "cancelled_at": None, "x_future": 1}).cancelled_at is None
    for bad in (
        {**CANCELLED, "fee": {"amount": "-1", "currency": "USD"}},
        {**CANCELLED, "refundable_b2b_amount": {"amount": "1", "currency": "EUR"}},
        {**CANCELLED, "fee_basis": {"policy_id": "p", "days_before_departure": "40", "fee_pct": "25"}},
        {**CANCELLED, "booking_id": "bad id"},
    ):
        with pytest.raises(contract.ContractError):
            contract.cancellation(bad)


@pytest.mark.asyncio
async def test_the_client_sends_one_cancel_with_its_key_and_reason() -> None:
    sent: list[tuple[str, str, dict[str, str], Any]] = []
    fake = FakeBooking()

    async def transport(method: str, url: str, headers: Any, body: bytes | None, timeout_s: float) -> Any:
        if "/bookings/" in url:
            sent.append((method, url, dict(headers), json.loads(body or b"{}")))
        return await fake(method, url, headers, body, timeout_s)

    real = booking_client(fake)
    client = BookingClient(BookingConfig(base_url=real._config.base_url, tokens=real._config.tokens,
                                         transport=transport))  # fmt: skip
    with pytest.raises(Exception):  # noqa: B017 — the fake knows no such booking: a contract refusal
        await client.cancel_booking(TENANT, "b-9", "client_cancelled", dry_run=True, idempotency_key="k-1")
    method, url, headers, body = sent[0]
    assert method == "POST" and url.endswith("/booking/b2b/v1/bookings/b-9/cancel?dry_run=true")
    assert headers["Idempotency-Key"] == "k-1" and body == {"reason_code": "client_cancelled"}
    with pytest.raises(ValueError):
        await client.cancel_booking(TENANT, "b 9", "other", dry_run=False, idempotency_key="k")


@pytest.mark.asyncio
async def test_the_sync_reads_a_cancel_answer() -> None:
    fake = FakeBooking()
    fake.bookings["h1"] = {"booking_id": "b-h1", "hold_id": "h1", "lines": []}
    line = {"booking_line_id": uuid.uuid4(), "bk_booking_id": "b-h1", "cancel_reason": "aa_cancelled"}
    work = Work({"aa_request_id": uuid.uuid4(), "kind": "cancel", "attempts": 1}, line)
    out = await perform(booking_client(fake), TENANT, work)
    assert out.code == "cancelled" and out.cancellation is not None and out.cancellation.fee == Decimal("250.00")
    assert fake.cancel_keys == [f"crm-cancel:{work.request['aa_request_id']}"]
    again = await perform(booking_client(fake), TENANT, work)  # a retry gets the stored cancellation
    assert again.cancellation == out.cancellation
    refused = await perform(booking_client(FakeBooking(cancel_refusal="forbidden")), TENANT, work)
    assert refused.failed and refused.code == "forbidden"
    missing = Work(work.request, {**line, "bk_booking_id": None})
    assert (await perform(booking_client(fake), TENANT, missing)).code == "not_found"
    down = await perform(booking_client(FakeBooking(down=True)), TENANT, work)
    assert down.retry == "booking_timeout"
    assert cancellation.BOOKING_REASON["aa_cancelled"] == "departure_cancelled"
    assert set(cancellation.BOOKING_REASON) == set(stages.CANCEL_REASONS)
    assert set(cancellation.BOOKING_REASON.values()) <= contract.CANCEL_REASONS


def test_only_not_found_means_nothing_left_to_cancel_and_a_pending_cancel_closes_the_trip_for_requests() -> None:
    # invalid may come back for a booking still live at AA: it is checked against the feed, never taken as done.
    assert cancellation.CANCEL_DONE_CODES == frozenset({"not_found"})
    assert "invalid" in cancellation.CANCEL_CHECK_CODES
    assert trip_closing({"trip_stage": "lost", "cancel_reason": None})
    assert trip_closing({"trip_stage": "booked", "cancel_reason": "client_request"})
    assert not trip_closing({"trip_stage": "booked", "cancel_reason": None})


def test_refund_choices_and_the_reason_text() -> None:
    pay = uuid.uuid4()
    assert cancellation.refund_choices([{"payment_id": str(pay), "amount": "12.50"}]) == {pay: Decimal("12.50")}
    assert cancellation.refund_choices(None) == {}
    for bad in ([{"payment_id": "x", "amount": "1"}], [{"payment_id": str(pay), "amount": "1.001"}], "x"):
        with pytest.raises(Invalid):
            cancellation.refund_choices(bad)
    assert cancellation.refund_reason("client_request", None).endswith("No booking terms were accepted in the CRM.")
    text = cancellation.refund_reason("force_majeure", {"version_label": "v3", "accepted_at": "2026-10-02T10:00:00"})
    assert text == "Trip cancelled (force majeure). Booking terms v3 accepted on 2026-10-02."
    # S262 round 3: the label is printed as the agency typed it, never prefixed ("rv-rv..." was a synthetic UAT label).
    labelled = cancellation.refund_reason("client_request", {"version_label": "rv-x1", "accepted_at": "2026-10-02"})
    assert "Booking terms rv-x1 accepted" in labelled and "rv-rv-" not in labelled


def test_the_commission_view_flags_overdue_and_names_every_money_key() -> None:
    row = {
        "commission_id": uuid.uuid4(), "trip_id": uuid.uuid4(), "trip_title": "Highlands", "trip_stage": "completed",
        "trip_start_date": None, "trip_end_date": None, "trip_month": dt.date(2026, 10, 1),
        "booking_line_id": uuid.uuid4(), "line_title": "Lodge", "line_status": "confirmed",
        "service_from": dt.date(2026, 10, 1), "service_to": dt.date(2026, 10, 3), "agency_supplier_id": uuid.uuid4(),
        "supplier_name": "Riverside", "status": "claimed", "due_on": dt.date(2026, 10, 20),
        "claimed_at": None, "claim_ref": "S-1", "close_reason": None, "note": None, "keep_on_cancel": False,
        "expected_amount": Decimal("40"), "expected_currency": "USD", "expected_amount_base": Decimal("40"),
        "expected_base_currency": "USD", "received_amount": None, "received_currency": None, "received_on": None,
        "received_ref": None, "received_amount_base": None, "base_currency": None, "fx_rate": None,
        "fx_rate_date": None, "fx_source": None, "stale_days": None, "short_amount": None,
        "fx_difference_base": None, "updated_at": None,
    }  # fmt: skip
    view = service.commission_view(row, dt.date(2026, 10, 25))
    assert view["overdue"] and view["days_overdue"] == 5 and view["commission_expected"]["amount"] == "40.00"
    assert not service.commission_view({**row, "status": "received"}, dt.date(2026, 10, 25))["overdue"]
    # The serializer strips every money key of a commission for anyone without view_margin.
    assert margin.strip_margin({"commissions": [view]}) == {}
    stripped = margin.strip_margin(view)
    assert "commission_expected" not in stripped and "commission_received" not in stripped
