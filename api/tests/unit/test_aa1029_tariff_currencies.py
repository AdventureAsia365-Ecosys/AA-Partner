"""S270 (AA-1029 / AA-1030 follow-up): the currencies the AA prices page offers, without a database.

``GET /crm/v1/aa/tariff/currencies`` answers USD plus each quote currency Booking has an approved FX rate for today
(``GET /booking/b2b/v1/fx-rates``, contract 1.0), cached per process for 10 minutes; Booking down, refusing or not
connected gives every quote currency with ``checked: false`` and never a 5xx. Booking's own 422 ``invalid`` for a
currency without a rate (the tariff read) stays 422 ``aa_tariff_invalid`` in the CRM.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.booking import contract
from app.booking import tariff as aa_tariff
from app.booking.client import BookingRefusal
from app.errors import Invalid
from app.main import create_app
from app.permissions import Action
from app.routers import aa as router
from tests.aa_fakes import BOOKING_URL, FakeBooking, booking_client
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, fake_tx, tenant_ctx
from tests.unit.test_aa1030_tariff import Raw, _client

TODAY = dt.date(2031, 1, 5)
OTHER_AGENCY = uuid.UUID("c0000000-0000-4000-8000-000000001029")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _rates(*currencies: str, on: str = "2031-01-05") -> dict[str, Any]:
    rates = [{"currency": c, "rate_per_usd": "1.25", "as_of": "2031-01-01", "source": "invented"} for c in currencies]
    return {"date": on, "base_currency": "USD", "rates": rates}


# ── the contract reader and the client call ──────────────────────────────────────────────────────


def test_fx_rates_reads_the_currencies_with_a_rate() -> None:
    rates = contract.fx_rates({**_rates("EUR", "VND", "USD"), "x_future": 1}, TODAY)
    assert rates == contract.FxRates(on=TODAY, currencies=frozenset({"EUR", "VND"}))  # USD is the base, not listed
    assert contract.fx_rates(_rates(), TODAY).currencies == frozenset()


@pytest.mark.parametrize(
    "data",
    [
        ["not", "an", "object"],
        _rates("EUR", on="2031-01-04"),  # not the asked day
        {**_rates("EUR"), "base_currency": "EUR"},
        {**_rates("EUR"), "rates": {"EUR": "1"}},
        {**_rates(), "rates": [{"currency": "eur", "rate_per_usd": "1"}]},
        {**_rates(), "rates": [{"currency": "EUR", "rate_per_usd": "0"}]},
        {**_rates(), "rates": [{"currency": "EUR", "rate_per_usd": 1.2}]},
        {**_rates(), "rates": ["EUR"]},
        {**_rates(), "date": "05/01/2031"},
    ],
)
def test_fx_rates_that_break_the_contract_are_refused(data: Any) -> None:
    with pytest.raises(contract.ContractError):
        contract.fx_rates(data, TODAY)


@pytest.mark.asyncio
async def test_the_client_asks_the_read_scope_for_the_day() -> None:
    t = Raw((200, _rates("EUR"), {}))
    assert (await _client(t).fx_rates(TENANT, TODAY))["rates"][0]["currency"] == "EUR"
    [(method, url, headers)] = t.seen
    assert (method, url) == ("GET", f"{BOOKING_URL}/booking/b2b/v1/fx-rates?date=2031-01-05")
    assert headers["X-AA-Tenant-Id"] == str(TENANT) and headers["Accept"] == "application/json"


# ── TariffCurrencies ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_usd_and_the_quote_currencies_with_a_rate_are_offered_in_the_web_order() -> None:
    fake = FakeBooking()  # rates for EUR, GBP, SGD, VND and THB (not a quote currency); none for AUD
    offer = aa_tariff.TariffCurrencies(booking_client(fake), clock=Clock())
    out = await offer.get(TENANT, TODAY)
    assert out == {"currencies": ["USD", "EUR", "GBP", "SGD", "VND"], "rate_date": "2031-01-05", "checked": True}
    assert fake.tenants == [str(TENANT)]


@pytest.mark.asyncio
async def test_one_answer_serves_every_agency_for_ten_minutes_then_booking_is_asked_again() -> None:
    fake = FakeBooking(fx_currencies=("EUR",))
    clock = Clock()
    offer = aa_tariff.TariffCurrencies(booking_client(fake), clock=clock)
    first = await offer.get(TENANT, TODAY)
    first["currencies"].append("XXX")  # a caller changing its copy never changes the cache
    clock.now += aa_tariff.CURRENCIES_TTL_S - 1
    assert (await offer.get(OTHER_AGENCY, TODAY))["currencies"] == ["USD", "EUR"]
    assert fake.calls["GET fx-rates"] == 1  # FX rows are global: the other agency got the cached answer
    fake.fx_currencies = ("EUR", "AUD")  # AA approved an AUD rate meanwhile
    clock.now += 1
    assert (await offer.get(OTHER_AGENCY, TODAY))["currencies"] == ["USD", "EUR", "AUD"]
    assert fake.calls["GET fx-rates"] == 2
    assert (await offer.get(TENANT, TODAY + dt.timedelta(days=1)))["rate_date"] == "2031-01-06"  # a new day asks
    assert fake.calls["GET fx-rates"] == 3


@pytest.mark.asyncio
async def test_asks_waiting_on_one_miss_share_one_booking_read() -> None:
    fake = FakeBooking()
    offer = aa_tariff.TariffCurrencies(booking_client(fake), clock=Clock())
    answers = await asyncio.gather(*(offer.get(TENANT, TODAY) for _ in range(5)))
    assert all(a["checked"] for a in answers) and fake.calls["GET fx-rates"] == 1


@pytest.mark.asyncio
async def test_booking_down_offers_the_static_list_unchecked_and_asks_again_a_minute_later() -> None:
    fake = FakeBooking(down=True)
    clock = Clock()
    offer = aa_tariff.TariffCurrencies(booking_client(fake), clock=clock)
    static = {"currencies": list(aa_tariff.CURRENCIES), "rate_date": None, "checked": False}
    assert await offer.get(TENANT, TODAY) == static
    assert await offer.get(TENANT, TODAY) == static
    assert fake.calls["down"] == 1  # the failure is kept a minute: a page load does not wait on Booking again
    fake.down = False
    clock.now += aa_tariff.CURRENCIES_RETRY_S
    assert (await offer.get(TENANT, TODAY))["checked"] is True


@pytest.mark.asyncio
async def test_a_broken_answer_is_unchecked_too() -> None:
    offer = aa_tariff.TariffCurrencies(_client(Raw((200, {**_rates("EUR"), "base_currency": "VND"}, {}))))
    assert (await offer.get(TENANT, TODAY))["checked"] is False


@pytest.mark.asyncio
async def test_an_agency_booking_refuses_is_not_cached_for_the_next_agency() -> None:
    t = Raw((403, {"error": "unknown_partner", "message": "x"}, {}), (200, _rates("EUR"), {}))
    offer = aa_tariff.TariffCurrencies(_client(t), clock=Clock())
    assert (await offer.get(TENANT, TODAY))["checked"] is False
    assert await offer.get(OTHER_AGENCY, TODAY) == {
        "currencies": ["USD", "EUR"],
        "rate_date": "2031-01-05",
        "checked": True,
    }
    assert len(t.seen) == 2


def test_a_tariff_currency_booking_has_no_rate_for_is_the_agency_s_choice_not_an_outage() -> None:
    """Booking answers the tariff in a currency without an approved FX rate with 422 ``invalid`` (S270): the CRM's
    ``aa_tariff_invalid`` (the web's "pick another currency"), never 503 ``booking_unavailable``."""
    problem = {"error": "invalid", "message": "no approved FX rate for AUD today; AA prices this tariff in USD, EUR"}
    err = aa_tariff.booking_error(BookingRefusal(422, "invalid", problem))
    assert isinstance(err, Invalid) and (err.status_code, err.code) == (422, "aa_tariff_invalid")
    assert "AUD" not in err.message  # Booking's message is never passed on


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


@pytest.mark.parametrize("role", ["owner", "agent"])
def test_owner_and_agent_get_the_offered_currencies(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    fake = FakeBooking()
    client, headers, ctx, seen = _route_client(pools, monkeypatch, role, booking=booking_client(fake))
    for _ in range(2):
        r = client.get("/crm/v1/aa/tariff/currencies", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["checked"] is True and body["currencies"] == ["USD", "EUR", "GBP", "SGD", "VND"]
        assert "AUD" not in body["currencies"]
    assert seen == [Action.AA_TARIFF_VIEW] * 2 and fake.calls["GET fx-rates"] == 1
    assert ctx.audit_events == []  # no price: no audit event (the tariff read itself is audited)


def test_the_assistant_gets_403_and_booking_is_not_asked(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBooking()
    client, headers, _, _ = _route_client(pools, monkeypatch, "assistant", booking=booking_client(fake))
    r = client.get("/crm/v1/aa/tariff/currencies", headers=headers)
    assert (r.status_code, r.json()["detail"]) == (403, "role_forbidden")
    assert fake.calls["GET fx-rates"] == 0


def test_booking_down_or_not_connected_is_never_a_5xx(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    static = {"currencies": list(aa_tariff.CURRENCIES), "rate_date": None, "checked": False}
    client, headers, _, _ = _route_client(pools, monkeypatch, "owner", booking=booking_client(FakeBooking(down=True)))
    r = client.get("/crm/v1/aa/tariff/currencies", headers=headers)
    assert (r.status_code, r.json()) == (200, static)
    client, headers, _, _ = _route_client(pools, monkeypatch, "owner")  # no BOOKING_API_URL on this environment
    r = client.get("/crm/v1/aa/tariff/currencies", headers=headers)
    assert (r.status_code, r.json()) == (200, static)
