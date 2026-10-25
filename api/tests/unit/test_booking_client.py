"""AA-947: the booking.b2b.v1 client (scopes, headers, transient vs. final failures) and the tolerant contract reader.
The DB side runs in tests/integration/test_aa_lines_api.py."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.booking import contract
from app.booking.client import BookingClient, BookingConfig, BookingRefusal, BookingUnavailable
from app.cis.http import HttpError, HttpResponse
from app.cis.m2m import M2mCredentials, TokenProvider
from tests.aa_fakes import BOOKING_URL, TOKEN_URL, FakeBooking, booking_client

pytestmark = pytest.mark.asyncio
TENANT = uuid.UUID("6c950969-ae80-4258-ab68-6ed80f2bbe71")


class Scripted:
    """Answers the token endpoint, then each Booking call with the next scripted response (or raises it)."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.seen: list[tuple[str, str, dict[str, str], Any]] = []
        self.token_scopes: list[str] = []

    async def __call__(self, method: str, url: str, headers: Any, body: bytes | None, timeout_s: float) -> HttpResponse:
        if url == TOKEN_URL:
            self.token_scopes.append(body.decode().split("scope=")[1])
            return HttpResponse(200, json.dumps({"access_token": "t", "expires_in": 3600}).encode())
        self.seen.append((method, url, dict(headers), json.loads(body) if body else None))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, data = answer
        return HttpResponse(status, json.dumps(data).encode() if data is not None else b"")


def _client(transport: Scripted) -> BookingClient:
    tokens = TokenProvider(M2mCredentials("crmclient1", "s", TOKEN_URL), transport=transport)
    return BookingClient(BookingConfig(base_url=BOOKING_URL, tokens=tokens, transport=transport))


async def test_each_call_asks_its_scope_and_names_the_agency() -> None:
    t = Scripted((201, {"ok": 1}), (201, {"ok": 2}), (201, {"ok": 3}), (204, None), (200, {"items": []}))
    c = _client(t)
    await c.quote(TENANT, {"x": 1})
    await c.hold(TENANT, {"x": 1}, idempotency_key="crm-hold:1:v1")
    await c.confirm(TENANT, "h1", {"price_hash": "a"}, idempotency_key="crm-confirm:1")
    assert await c.release(TENANT, "h1") is None
    await c.events(TENANT, after="bk1.5", limit=200)
    assert t.token_scopes == ["booking.b2b%2Fquote", "booking.b2b%2Fhold", "booking.b2b%2Fbook", "booking.b2b%2Fread"]
    method, url, headers, body = t.seen[1]
    assert (method, url) == ("POST", f"{BOOKING_URL}/booking/b2b/v1/holds")
    assert headers["X-AA-Tenant-Id"] == str(TENANT) and headers["Idempotency-Key"] == "crm-hold:1:v1"
    assert t.seen[2][1].endswith("/holds/h1/confirm") and t.seen[3][0] == "DELETE"
    assert t.seen[4][1].endswith("/events?limit=200&after=bk1.5")


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (HttpError("timeout"), "booking_timeout"),
        ((503, {"error": "unavailable", "message": "x"}), "booking_unavailable"),
        ((408, None), "booking_unavailable"),
        ((401, {"error": "unauthorized", "message": "x"}), "booking_unauthorized"),
        ((429, {"error": "invalid", "message": "slow down"}), "booking_throttled"),
        ((200, ["not", "an", "object"]), "booking_contract"),
    ],
)
async def test_transient_failures_are_retried(answer: Any, error: str) -> None:
    t = Scripted(answer)
    with pytest.raises(BookingUnavailable) as err:
        await _client(t).products(TENANT)
    assert err.value.error_class == error


async def test_final_refusals_carry_the_contract_code() -> None:
    t = Scripted(
        (429, {"error": "rehold_limit", "message": "x", "retry_after_s": 30}),
        (409, {"error": "price_changed", "message": "x", "price_hash": "b" * 64}),
        (418, "not a problem"),
    )
    c = _client(t)
    with pytest.raises(BookingRefusal) as err:
        await c.hold(TENANT, {}, idempotency_key="k" * 10)
    assert (err.value.status, err.value.code, err.value.problem["retry_after_s"]) == (429, "rehold_limit", 30)
    with pytest.raises(BookingRefusal) as err:
        await c.get_hold(TENANT, "h1")
    assert err.value.code == "price_changed" and err.value.problem["price_hash"] == "b" * 64
    with pytest.raises(BookingRefusal) as err:
        await c.price_parity(TENANT, "T-1", "USD")
    assert err.value.code == "http_418"


async def test_a_401_drops_the_cached_token() -> None:
    t = Scripted((401, None), (200, {"items": []}))
    c = _client(t)
    with pytest.raises(BookingUnavailable):
        await c.departures(TENANT, "T-1", date_from="2026-12-01", date_to="2027-12-01")
    await c.departures(TENANT, "T-1")
    assert len(t.token_scopes) == 2
    assert "source_system=cis&source_id=T-1&from=2026-12-01&to=2027-12-01" in t.seen[0][1]


async def test_token_failures_and_bad_ids() -> None:
    async def refuse(method: str, url: str, *_: Any) -> HttpResponse:
        return HttpResponse(400, b"{}")

    tokens = TokenProvider(M2mCredentials("crmclient1", "s", TOKEN_URL), transport=refuse)
    c = BookingClient(BookingConfig(base_url=BOOKING_URL, tokens=tokens, transport=refuse))
    with pytest.raises(BookingUnavailable) as err:
        await c.products(TENANT, cursor="c1")
    assert err.value.error_class == "token_unauthorized"
    with pytest.raises(ValueError):
        await _client(Scripted()).get_hold(TENANT, "../admin")


# ── the contract reader ──────────────────────────────────────────────────────────────────────────


async def _quote(**over: Any) -> dict[str, Any]:
    fake = FakeBooking(**over)
    body = {"proposal_ref": "crm-line:L1", "proposal_version": 1, "currency": "VND",
            "lines": [{"line_ref": "L1", "component_id": "tp:1", "service_date": "2026-12-01",
                       "pax": {"adult": 2}}]}  # fmt: skip
    return await booking_client(fake).quote(TENANT, body)


async def test_quote_line_reads_the_line_and_the_lock() -> None:
    q = contract.quote_line(await _quote(b2b_price="2500000", currency_rate="25000.5"), "L1")
    assert (q.quote_id, q.currency, q.b2b_price, q.rate_per_usd) == (
        "q1",
        "VND",
        Decimal("2500000"),
        Decimal("25000.5"),
    )
    assert q.rate_date == dt.date(2026, 10, 25) and q.availability == "available"
    assert q.valid_until.tzinfo is not None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda q: q.update(lines=[]),
        lambda q: q.update(lines="x"),
        lambda q: q["lines"][0].update(availability="maybe"),
        lambda q: q["lines"][0].update(b2b_price={"amount": "1", "currency": "EUR"}),
        lambda q: q["fx"].update(rate_per_usd="0"),
        lambda q: q["fx"].update(rate_per_usd=1),
        lambda q: q.update(price_hash="nope"),
        lambda q: q.update(valid_until="2026-10-27T00:00:00"),
        lambda q: q.update(valid_until="soon"),
        lambda q: q["fx"].update(rate_date=5),
        lambda q: q["fx"].update(rate_date="2026-13-01"),
        lambda q: q.update(fx=None),
    ],
)
async def test_quote_line_refuses_what_it_cannot_read(mutate: Any) -> None:
    q = await _quote()
    mutate(q)
    with pytest.raises(contract.ContractError):
        contract.quote_line(q, "L1")


def test_hold_booking_events_parity_products() -> None:
    hold = contract.hold({"hold_id": "h1", "status": "active", "expires_at": "2026-10-27T00:00:00Z",
                          "lines": [{"line_ref": "L1", "line_status": "on_request"}, "x"]}, "L1")  # fmt: skip
    assert (hold.hold_id, hold.status, hold.line_status) == ("h1", "active", "on_request")
    with pytest.raises(contract.ContractError):
        contract.hold({"hold_id": "h1", "status": "lost", "expires_at": "2026-10-27T00:00:00Z"})
    booked = contract.booking({"booking_id": "b1", "hold_id": "h1", "trip_case_code": "TC-0001",
                               "lines": [{"line_ref": "L1", "status": "confirmed"}]}, "L1")  # fmt: skip
    assert booked.line_status == "confirmed" and booked.trip_case_code == "TC-0001"
    for bad in ({"lines": []}, {"booking_id": "b1", "hold_id": "h1", "trip_case_code": "\x00",
                                "lines": [{"line_ref": "L1", "status": "confirmed"}]}):  # fmt: skip
        with pytest.raises(contract.ContractError):
            contract.booking(bad, "L1")
    ev = contract.event({"event_id": "e1", "type": "line.declined", "decline_code": "made_up", "extra": 1,
                         "line_ref": "L1", "cursor": "1"})  # fmt: skip
    assert ev.known and ev.decline_code is None and ev.line_ref == "L1"
    assert not contract.event({"event_id": "e2", "type": "hold.renamed"}).known
    with pytest.raises(contract.ContractError):
        contract.event({"event_id": "e3", "type": "BAD TYPE"})
    assert contract.event_id({"event_id": "e 1"}) is None and contract.event_id("x") is None
    assert contract.event_page({"items": [], "next_cursor": "bk1.0"}) == ([], "bk1.0")
    for bad in ({"items": {}, "next_cursor": "c"}, {"items": []}):
        with pytest.raises(contract.ContractError):
            contract.event_page(bad)
    assert contract.from_price({"from_price": None}, "USD") is None
    assert contract.from_price({"from_price": {"amount": "800.00", "currency": "USD"}}, "USD") == Decimal("800.00")
    with pytest.raises(contract.ContractError):
        contract.from_price({"from_price": {"amount": "800.00", "currency": "EUR"}}, "USD")
    items, cursor = contract.products_page({"items": [{
        "tour": {"source_system": "cis", "source_id": "T-1"}, "trip_plan_id": "p", "sellable": True,
        "withdrawn": True, "has_fixed_departures": False,
        "components": [{"component_id": "tp:1", "kind": "trip_plan"}, {"component_id": "x", "kind": "boat"}],
    }], "next_cursor": ""})  # fmt: skip
    assert cursor is None and not items[0].sellable and [c.kind for c in items[0].components] == ["trip_plan"]
    with pytest.raises(contract.ContractError):
        contract.products_page({"items": "x"})
    deps = contract.departures({"items": [{"departure_id": "d1", "start_date": "2026-12-01", "available": -1,
                                           "status": "scheduled", "auto_confirm": True}]})  # fmt: skip
    assert deps[0].available == 0 and deps[0].auto_confirm
    assert contract.problem_code({"error": "sold_out"}) == "sold_out" and contract.problem_code({}) is None
    with pytest.raises(contract.ContractError):
        contract.money("x", "m")
