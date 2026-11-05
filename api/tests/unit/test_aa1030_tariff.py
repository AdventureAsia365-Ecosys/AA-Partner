"""AA-1030: the agency's AA tariff in the CRM (booking.b2b.v1 1.4 ``GET /tariff``) without a database: the contract
reader, the client's JSON and CSV calls, the CRM's JSON (``b2b_*`` names the margin serializer strips), the error
mapping and the route (owner / agent 200, assistant 403, no-store, Booking's CSV and file name). The database side
(tenant_tx, the audit row): tests/integration/test_aa1030_tariff_api.py."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.booking import contract
from app.booking import tariff as aa_tariff
from app.booking.client import BookingClient, BookingConfig, BookingRefusal, BookingUnavailable, _attachment_name
from app.cis.http import HttpError, HttpResponse
from app.cis.m2m import M2mCredentials, TokenProvider
from app.errors import Conflict, Invalid, UnavailableRetry
from app.main import create_app
from app.margin import filter_body, strip_margin
from app.permissions import Action
from app.routers import aa as router
from tests.aa_fakes import (
    BOOKING_URL,
    TARIFF_TOUR,
    TOKEN_URL,
    FakeBooking,
    booking_client,
    tariff_body,
    tariff_csv,
    tariff_item,
)
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, fake_tx, tenant_ctx

TODAY = dt.date(2031, 1, 5)


# ── the contract reader ──────────────────────────────────────────────────────────────────────────


def test_tariff_reads_every_row() -> None:
    t = contract.tariff(tariff_body("EUR", tier="gold_2"), "EUR")
    assert (t.currency, t.tier_code, t.basis, t.truncated, t.priced_on) == (
        "EUR",
        "gold_2",
        "per_person_band_min_twin_share",
        False,
        dt.date(2031, 1, 5),
    )
    first, open_band = t.items
    assert (first.source_id, first.title, first.pax_min, first.pax_max, first.priced_for_pax) == (
        TARIFF_TOUR,
        "Invented River Ramble",
        2,
        4,
        2,
    )
    assert (first.price, first.price_per_person, first.agency_discount_pct) == (
        Decimal("776.00"),
        Decimal("388.00"),
        Decimal("0.0300"),
    )
    assert (first.rrp_per_person, first.rrp_currency) == (Decimal("500.00"), "USD")  # AA's RRP keeps its currency
    assert (open_band.pax_max, open_band.rrp_per_person, open_band.rrp_currency) == (None, None, None)
    assert contract.tariff({**tariff_body(), "items": []}).items == ()


def _broken(**over: Any) -> dict[str, Any]:
    return {**tariff_body(), "items": [tariff_item(**over)]}


@pytest.mark.parametrize(
    "data",
    [
        ["not", "an", "object"],
        {**tariff_body(), "currency": "usd"},
        {**tariff_body(), "basis": "per_person_whatever"},
        {**tariff_body(), "tier_code": "Gold Tier"},
        {**tariff_body(), "fx": {"rate_per_usd": "0", "rate_date": "2031-01-05"}},
        {**tariff_body(), "fx": None},
        {**tariff_body(), "truncated": "no"},
        {**tariff_body(), "items": {"a": 1}},
        {**tariff_body(), "priced_on": "05/01/2031"},
        _broken(price={"amount": "776.00", "currency": "EUR"}),
        _broken(price_per_person={"amount": "-1.00", "currency": "USD"}),
        _broken(price={"amount": 776, "currency": "USD"}),
        _broken(rrp_per_person={"amount": "-5.00", "currency": "USD"}),
        _broken(pax_min=0),
        _broken(pax_min=True),
        _broken(pax_min=4, pax_max=2),
        _broken(priced_for_pax=None),
        _broken(valid_to="2030-12-31"),
        _broken(valid_from="2031-1-5"),
        _broken(agency_discount_pct="1.0000"),
        _broken(agency_discount_pct="3%"),
        _broken(title="x" * 501),
        _broken(season=7),
        _broken(duration_days=-1),
        _broken(tour={"source_system": "cis"}),
        _broken(tour=None),
    ],
)
def test_a_tariff_that_breaks_the_contract_is_refused(data: Any) -> None:
    with pytest.raises(contract.ContractError):
        contract.tariff(data)


def test_a_tariff_in_another_currency_than_asked_is_refused() -> None:
    with pytest.raises(contract.ContractError):
        contract.tariff(tariff_body("USD"), "EUR")


# ── the client ───────────────────────────────────────────────────────────────────────────────────


class Raw:
    """The token endpoint, then each Booking call answered with the next scripted (status, body, headers)."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.seen: list[tuple[str, str, dict[str, str]]] = []
        self.token_calls = 0

    async def __call__(self, method: str, url: str, headers: Any, body: bytes | None, timeout_s: float) -> HttpResponse:
        if url == TOKEN_URL:
            self.token_calls += 1
            return HttpResponse(200, json.dumps({"access_token": "t", "expires_in": 3600}).encode())
        self.seen.append((method, url, dict(headers)))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, raw, out_headers = answer
        data = raw if isinstance(raw, bytes) else (json.dumps(raw).encode() if raw is not None else b"")
        return HttpResponse(status, data, out_headers)


def _client(transport: Raw) -> BookingClient:
    tokens = TokenProvider(M2mCredentials("crmclient1", "s", TOKEN_URL), transport=transport)
    return BookingClient(BookingConfig(base_url=BOOKING_URL, tokens=tokens, transport=transport))


CSV_HEADERS = {
    "Content-Type": "text/csv; charset=utf-8",
    "Content-Disposition": 'attachment; filename="aa-tariff-x-usd-2031-01-05.csv"',
}


@pytest.mark.asyncio
async def test_the_json_and_csv_calls_ask_the_read_scope_and_name_the_agency() -> None:
    t = Raw((200, tariff_body(), {}), (200, tariff_csv(), CSV_HEADERS))
    c = _client(t)
    assert (await c.tariff(TENANT, currency="USD", source_id=TARIFF_TOUR))["tier_code"] == "silver"
    body, name = await c.tariff_csv(TENANT, currency="EUR")
    assert body == tariff_csv() and name == "aa-tariff-x-usd-2031-01-05.csv"
    (m1, url1, h1), (m2, url2, h2) = t.seen
    path = "/booking/b2b/v1/tariff"
    assert (m1, url1) == ("GET", f"{BOOKING_URL}{path}?currency=USD&format=json&source_id={TARIFF_TOUR}")
    assert (m2, url2) == ("GET", f"{BOOKING_URL}/booking/b2b/v1/tariff?currency=EUR&format=csv")
    assert h1["Accept"] == "application/json" and h2["Accept"] == "text/csv"
    assert h1["X-AA-Tenant-Id"] == h2["X-AA-Tenant-Id"] == str(TENANT)
    assert h2["Authorization"] == "Bearer t" and t.token_calls == 1  # one cached token for the read scope


@pytest.mark.asyncio
@pytest.mark.parametrize("csv", [False, True])
@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (HttpError("timeout"), "booking_timeout"),
        ((503, {"error": "unavailable", "message": "x", "retry_after_s": 60}, {}), "booking_unavailable"),
        ((502, b"<html>bad gateway</html>", {}), "booking_unavailable"),
        ((408, None, {}), "booking_unavailable"),
        ((429, {"error": "invalid", "message": "slow down"}, {}), "booking_throttled"),
    ],
)
async def test_transient_failures_of_both_calls(csv: bool, answer: Any, error: str) -> None:
    c = _client(Raw(answer))
    with pytest.raises(BookingUnavailable) as err:
        await (c.tariff_csv(TENANT, currency="USD") if csv else c.tariff(TENANT, currency="USD"))
    assert err.value.error_class == error


@pytest.mark.asyncio
@pytest.mark.parametrize("csv", [False, True])
async def test_a_401_drops_the_cached_token_for_both_calls(csv: bool) -> None:
    ok = (200, tariff_csv(), CSV_HEADERS) if csv else (200, tariff_body(), {})
    t = Raw((401, {"error": "unauthorized", "message": "x"}, {}), ok)
    c = _client(t)
    call = (lambda: c.tariff_csv(TENANT, currency="USD")) if csv else (lambda: c.tariff(TENANT, currency="USD"))
    with pytest.raises(BookingUnavailable) as err:
        await call()
    assert err.value.error_class == "booking_unauthorized"
    await call()
    assert t.token_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("csv", [False, True])
@pytest.mark.parametrize(
    ("status", "problem", "code"),
    [
        (403, {"error": "unknown_partner", "message": "no active B2B partner for this tenant"}, "unknown_partner"),
        (403, {"error": "forbidden", "message": "x"}, "forbidden"),
        (422, {"error": "invalid", "message": "XYZ is not a quote currency"}, "invalid"),
        (418, "not a problem", "http_418"),
    ],
)
async def test_refusals_of_both_calls_carry_the_contract_code(csv: bool, status: int, problem: Any, code: str) -> None:
    c = _client(Raw((status, problem, {})))
    with pytest.raises(BookingRefusal) as err:
        await (c.tariff_csv(TENANT, currency="USD") if csv else c.tariff(TENANT, currency="USD"))
    assert (err.value.status, err.value.code) == (status, code)


@pytest.mark.asyncio
async def test_a_csv_call_answered_with_something_else_breaks_the_contract() -> None:
    c = _client(Raw((200, tariff_body(), {"Content-Type": "application/json"}), (200, b"x", {})))
    for _ in range(2):
        with pytest.raises(BookingUnavailable) as err:
            await c.tariff_csv(TENANT, currency="USD")
        assert err.value.error_class == "booking_contract"


@pytest.mark.asyncio
async def test_a_json_call_answered_with_no_object_breaks_the_contract() -> None:
    c = _client(Raw((200, b"tour_key,title\r\n", {"content-type": "text/csv"})))
    with pytest.raises(BookingUnavailable) as err:
        await c.tariff(TENANT, currency="USD")
    assert err.value.error_class == "booking_contract"


@pytest.mark.parametrize(
    ("disposition", "name"),
    [
        ('attachment; filename="aa-tariff-lotus-usd-2031-01-05.csv"', "aa-tariff-lotus-usd-2031-01-05.csv"),
        ("attachment; filename=aa-tariff.csv", "aa-tariff.csv"),
        ('attachment; filename="aa-tariff.csv"; size=10', "aa-tariff.csv"),
        ('attachment; filename="../etc/passwd.csv"', None),
        ('attachment; filename="tariff.exe"', None),
        ('attachment; filename="a b.csv"', None),
        ('attachment; filename="x.csv"\r\nSet-Cookie: a=b', None),
        ("", None),
    ],
)
def test_only_a_plain_csv_file_name_is_passed_on(disposition: str, name: str | None) -> None:
    assert _attachment_name(disposition) == name


# ── the CRM's JSON and errors ────────────────────────────────────────────────────────────────────


def test_the_view_renames_the_b2b_prices_and_the_serializer_strips_them_without_view_margin() -> None:
    view = aa_tariff.view(contract.tariff(tariff_body("EUR"), "EUR"))
    first = view["items"][0]
    assert first["b2b_price"] == {"amount": "776.00", "currency": "EUR"}
    assert first["b2b_price_per_person"] == {"amount": "388.00", "currency": "EUR"}
    assert first["rrp_per_person"] == {"amount": "500.00", "currency": "USD"}
    assert first["agency_discount_pct"] == "0.0300" and view["items"][1]["rrp_per_person"] is None
    assert not {"price", "price_per_person"} & set(first)  # Booking's names, which MARGIN_KEY does not match
    body = json.dumps(view).encode()
    for role in ("owner", "agent"):
        assert filter_body(body, role) == (body, set())
    for role in ("assistant", None):
        cleaned, removed = filter_body(body, role)
        assert removed == {"b2b_price", "b2b_price_per_person"}
        assert all(not {"b2b_price", "b2b_price_per_person"} & set(i) for i in json.loads(cleaned)["items"])
    assert strip_margin(view)["items"][0]["title"] == "Invented River Ramble"


@pytest.mark.parametrize(
    ("exc", "kind", "status", "code"),
    [
        (BookingRefusal(403, "unknown_partner"), Conflict, 409, "aa_partner_not_active"),
        (BookingRefusal(403, "forbidden"), Conflict, 409, "aa_partner_not_active"),
        (BookingRefusal(422, "invalid"), Invalid, 422, "aa_tariff_invalid"),
        (BookingRefusal(404, "not_found"), Invalid, 422, "aa_tariff_refused"),
        (BookingUnavailable("booking_timeout"), UnavailableRetry, 503, "booking_unavailable"),
        (contract.ContractError("items is not a list"), UnavailableRetry, 503, "booking_unavailable"),
    ],
)
def test_booking_errors_become_clear_problems(exc: Exception, kind: type, status: int, code: str) -> None:
    err = aa_tariff.booking_error(exc)
    assert isinstance(err, kind) and (err.status_code, err.code) == (status, code)
    assert "message" not in json.dumps(err.extra)  # never Booking's message


@pytest.mark.asyncio
async def test_read_and_csv_file_map_their_failures() -> None:
    fake = FakeBooking()
    client = booking_client(fake)
    out = await aa_tariff.read(client, TENANT, currency="USD")
    assert out["tier_code"] == "silver" and len(out["items"]) == 2
    body, name = await aa_tariff.csv_file(client, TENANT, currency="USD", today=TODAY)
    assert body == tariff_csv("USD") and name == "aa-tariff-invented-agency-usd-2031-01-05.csv"
    fake.tariff_refusal = "unknown_partner"
    for call in (
        aa_tariff.read(client, TENANT, currency="USD"),
        aa_tariff.csv_file(client, TENANT, currency="USD", today=TODAY),
    ):
        with pytest.raises(Conflict):
            await call
    fake.tariff_refusal, fake.down = None, True
    with pytest.raises(UnavailableRetry) as err:
        await aa_tariff.csv_file(client, TENANT, currency="USD", today=TODAY)
    assert err.value.retry_after_s == aa_tariff.RETRY_AFTER_S


@pytest.mark.asyncio
async def test_a_csv_without_a_usable_file_name_gets_the_crm_s() -> None:
    client = _client(Raw((200, tariff_csv(), {"content-type": "text/csv"})))
    _, name = await aa_tariff.csv_file(client, TENANT, currency="VND", today=TODAY)
    assert name == "aa-tariff-vnd-2031-01-05.csv"


@pytest.mark.asyncio
async def test_a_tariff_row_that_breaks_the_contract_fails_the_whole_read() -> None:
    client = _client(Raw((200, {**tariff_body(), "items": [tariff_item(), {"tour": None}]}, {})))
    with pytest.raises(UnavailableRetry):
        await aa_tariff.read(client, TENANT, currency="USD")


# ── the route ────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _route_client(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str, *, booking: Any = None
) -> tuple[TestClient, dict[str, str], Any, list[Any]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), booking_client=booking)
    ctx = tenant_ctx(ScriptedConn(), role=role)
    seen: list[Any] = []

    def tx(*a: Any, **kw: Any) -> Any:
        seen.append(kw.get("action"))
        margin.current_member_role.set(role)
        return fake_tx(ctx)(*a, **kw)

    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", tx)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers, ctx, seen


@pytest.mark.parametrize(("role", "status"), [("owner", 200), ("agent", 200), ("assistant", 403)])
def test_the_permission_matrix_of_the_route(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str, status: int
) -> None:
    fake = FakeBooking()
    client, headers, _, seen = _route_client(pools, monkeypatch, role, booking=booking_client(fake))
    for fmt in ("json", "csv"):
        r = client.get(f"/crm/v1/aa/tariff?format={fmt}", headers=headers)
        assert r.status_code == status, (fmt, r.text)
    assert seen == [Action.AA_TARIFF_VIEW, Action.AA_TARIFF_VIEW]
    if status == 403:
        assert r.json()["detail"] == "role_forbidden"
        assert fake.calls["GET tariff"] == 0  # Booking is never asked for a refused role
    else:
        assert fake.calls["GET tariff"] == 2


def test_json_answer_no_store_and_one_audit_event_without_amounts(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBooking(tier_code="gold")
    client, headers, ctx, _ = _route_client(pools, monkeypatch, "agent", booking=booking_client(fake))
    r = client.get(f"/crm/v1/aa/tariff?currency=EUR&source_id={TARIFF_TOUR}", headers=headers)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    data = r.json()
    assert (data["currency"], data["tier_code"], len(data["items"])) == ("EUR", "gold", 2)
    assert data["items"][0]["b2b_price_per_person"] == {"amount": "388.00", "currency": "EUR"}
    [event] = ctx.audit_events
    assert (event.action, event.outcome, event.object_kind, event.object_id, event.reason) == (
        "aa_tariff_view",
        "allowed",
        "aa_tariff",
        TARIFF_TOUR,
        "json_eur",
    )
    assert event.fields == () and "388" not in repr(event)
    assert fake.tenants[-1] == str(TENANT)


def test_csv_answer_is_booking_s_file_with_its_name(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBooking()
    client, headers, ctx, _ = _route_client(pools, monkeypatch, "owner", booking=booking_client(fake))
    r = client.get("/crm/v1/aa/tariff?format=csv&currency=SGD", headers=headers)
    assert r.status_code == 200 and r.content == tariff_csv("SGD")
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["content-disposition"] == 'attachment; filename="aa-tariff-invented-agency-sgd-2031-01-05.csv"'
    assert r.headers["cache-control"] == "no-store"
    assert [(e.action, e.object_id, e.reason) for e in ctx.audit_events] == [("aa_tariff_view", None, "csv_sgd")]


@pytest.mark.parametrize(
    ("refusal", "status", "detail"),
    [
        ("unknown_partner", 409, "aa_partner_not_active"),  # no partner row, or AA suspended it (Booking: 403)
        ("forbidden", 409, "aa_partner_not_active"),
        ("invalid", 422, "aa_tariff_invalid"),
        ("not_found", 422, "aa_tariff_refused"),
    ],
)
@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_booking_refusals_on_the_route(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, refusal: str, status: int, detail: str, fmt: str
) -> None:
    fake = FakeBooking(tariff_refusal=refusal)
    client, headers, _, _ = _route_client(pools, monkeypatch, "owner", booking=booking_client(fake))
    r = client.get(f"/crm/v1/aa/tariff?format={fmt}", headers=headers)
    assert (r.status_code, r.json()["detail"]) == (status, detail)
    assert "booking-side" not in r.text  # Booking's own message is never passed on


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_booking_down_or_a_broken_answer_is_503_with_retry_after(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    client, headers, _, _ = _route_client(pools, monkeypatch, "agent", booking=booking_client(FakeBooking(down=True)))
    r = client.get(f"/crm/v1/aa/tariff?format={fmt}", headers=headers)
    assert (r.status_code, r.json()["detail"], r.headers["retry-after"]) == (503, "booking_unavailable", "60")
    json_type = {"content-type": "application/json"}
    broken = _client(Raw((200, {**tariff_body(), "basis": "unknown"}, {}), (200, tariff_body(), json_type)))
    client, headers, _, _ = _route_client(pools, monkeypatch, "agent", booking=broken)
    for path in ("/crm/v1/aa/tariff", "/crm/v1/aa/tariff?format=csv"):
        r = client.get(path, headers=headers)
        assert (r.status_code, r.json()["detail"], r.headers["retry-after"]) == (503, "booking_unavailable", "60")


def test_the_route_needs_booking_and_checks_its_query(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers, _, _ = _route_client(pools, monkeypatch, "owner")
    r = client.get("/crm/v1/aa/tariff", headers=headers)
    assert (r.status_code, r.json()["detail"]) == (503, "booking_not_configured")
    client, headers, _, _ = _route_client(pools, monkeypatch, "owner", booking=booking_client(FakeBooking()))
    for bad in ("currency=usd", "currency=USDX", "format=xlsx", "source_id=" + "x" * 129, "source_id="):
        assert client.get(f"/crm/v1/aa/tariff?{bad}", headers=headers).status_code == 422, bad


def test_a_source_id_that_is_not_an_audit_id_is_left_out_of_the_event(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers, ctx, _ = _route_client(pools, monkeypatch, "owner", booking=booking_client(FakeBooking()))
    r = client.get("/crm/v1/aa/tariff?source_id=a%20tour%2Fwith%20spaces", headers=headers)
    assert r.status_code == 200 and r.json()["items"] == []
    assert [(e.object_id, e.reason) for e in ctx.audit_events] == [(None, "json_usd")]


def test_the_currencies_the_web_offers_are_booking_s_quote_currencies() -> None:
    assert aa_tariff.CURRENCIES == ("USD", "EUR", "GBP", "AUD", "SGD", "VND")
    assert uuid.UUID(TARIFF_TOUR)  # the invented tour key is a catalog-shaped id
