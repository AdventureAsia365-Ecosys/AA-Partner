"""AA-1072 routes: the public "Book now" routes, the VNPay IPN and the stub gateway on ``/crm/public/v1``, the client
page's pay-online button, and the owner's Settings routes on ``/crm/v1`` reach their service functions with the right
arguments, status codes and headers (the services run on PostgreSQL in tests/integration/test_aa1072_book_now_api.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.intake import booking, booking_settings
from app.main import create_app
from app.payments import gateway
from app.proposals import public as proposal_page
from app.routers import client_pages, payments
from app.routers import public as public_router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx

KEY = "k" * 22
REF = "a1b2c3d4e5f6a7b8c9d0e1f2"


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _record(calls: list[tuple[str, tuple, dict]], name: str, result: Any) -> Any:
    async def fn(*args: Any, **kwargs: Any) -> Any:
        calls.append((name, args, kwargs))
        return result

    return fn


@pytest.fixture
def public(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, list]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(public_router, "request_pool", _pool)
    monkeypatch.setattr(public_router, "require_keys", lambda keys: keys)
    monkeypatch.setattr(client_pages, "request_pool", _pool)
    monkeypatch.setattr(client_pages, "require_keys", lambda keys: keys)
    calls: list[tuple[str, tuple, dict]] = []
    monkeypatch.setattr(booking, "widget", _record(calls, "widget", {"tour": "T-1"}))
    monkeypatch.setattr(booking, "price", _record(calls, "price", {"total": "1.00"}))
    monkeypatch.setattr(gateway, "handle_ipn", _record(calls, "ipn", {"RspCode": "00", "Message": "Confirm Success"}))
    monkeypatch.setattr(gateway, "stub_page", _record(calls, "stub_page", "<!doctype html><p>stub</p>"))
    monkeypatch.setattr(gateway, "stub_pay", _record(calls, "stub_pay", "https://go.test/c/p/paid"))
    monkeypatch.setattr(proposal_page, "card_payment", _record(calls, "card", {"redirect_url": "https://pay.test/x"}))
    with TestClient(app, base_url="https://go.test") as c:
        yield c, calls


def test_the_widget_and_the_price_routes(public: tuple[TestClient, list]) -> None:
    client, calls = public
    res = client.get(f"/crm/public/v1/book/{KEY}", params={"tour": "T-1", "page": "P-1"})
    assert res.status_code == 200 and res.json() == {"tour": "T-1"} and res.headers["cache-control"] == "no-store"
    assert calls[-1][2] == {"tour": "T-1", "page_id": "P-1"}
    res = client.post(f"/crm/public/v1/book/{KEY}/price", json={"tour": "T-1"})
    assert res.status_code == 200 and calls[-1][0] == "price" and calls[-1][1][-1] == {"tour": "T-1"}
    assert client.post(f"/crm/public/v1/book/{KEY}/price", content=b"{bad").status_code == 400
    assert client.get("/crm/public/v1/book/short").status_code == 422


def test_price_checks_have_their_own_limit_and_leave_the_writes_theirs(
    public: tuple[TestClient, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S277 live check: a visitor who checks the price a few times and then books, holds, accepts and pays within a
    minute is not stopped on "Pay online" (the price check has its own bucket), and price checks still stop."""
    client, calls = public
    monkeypatch.setattr(booking, "submit", _record(calls, "submit", {"status": "created"}))
    intake = client.app.state.intake_limiter  # type: ignore[attr-defined]
    price_limit = client.app.state.booking_price_limiter  # type: ignore[attr-defined]
    limit = price_limit._limit
    assert limit == client.app.state.settings.rate_limit_booking_price_per_minute > intake._limit  # type: ignore[attr-defined]
    for _ in range(3):
        assert client.post(f"/crm/public/v1/book/{KEY}/price", json={"tour": "T-1"}).status_code == 200
    assert client.post(f"/crm/public/v1/book/{KEY}/bookings", json={"tour": "T-1"}).status_code == 201
    client.cookies.set(client_pages.PROPOSAL_COOKIE, "t" * 43, domain="go.test", path="/")
    card = "/crm/public/v1/proposal/invoices/00000000-0000-4000-8000-0000000000d1/card"
    assert client.post(card).status_code == 200  # 3 price checks + 1 booking + this: under the 5 writes a minute
    for _ in range(limit - 3):
        assert client.post(f"/crm/public/v1/book/{KEY}/price", json={"tour": "T-1"}).status_code == 200
    res = client.post(f"/crm/public/v1/book/{KEY}/price", json={"tour": "T-1"})
    assert res.status_code == 429 and res.json()["detail"] == "rate_limited"
    assert client.post(card).status_code == 200  # the price bucket being full leaves the writes alone


@pytest.mark.parametrize(("status", "code"), [("created", 201), ("received", 202), ("already_created", 200)])
def test_a_booking_answers_by_its_status(
    public: tuple[TestClient, list], monkeypatch: pytest.MonkeyPatch, status: str, code: int
) -> None:
    client, calls = public
    monkeypatch.setattr(booking, "submit", _record(calls, "submit", {"status": status}))
    res = client.post(
        f"/crm/public/v1/book/{KEY}/bookings", json={"tour": "T-1"}, headers={"Origin": "https://go.test"}
    )
    assert res.status_code == code and res.headers["referrer-policy"] == "no-referrer"
    assert calls[-1][2]["origin"] == "https://go.test" and calls[-1][2]["client_ip"]


def test_the_ipn_answers_vnpay_and_counts_unknown_references(
    public: tuple[TestClient, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, calls = public
    signed = {"vnp_TxnRef": REF, "vnp_Amount": "100", "vnp_SecureHash": "0f" * 64}  # AA-1092: the hash shape only
    res = client.get("/crm/public/v1/hooks/vnpay/ipn", params={**signed, "other": "x"})
    assert res.status_code == 200 and res.json()["RspCode"] == "00"
    assert calls[-1][1][2] == signed
    monkeypatch.setattr(gateway, "handle_ipn", _record(calls, "ipn", {"RspCode": "01", "Message": "Order not found"}))
    from app.ratelimit import HOOK_MISSES_PER_MINUTE

    unknown = {"vnp_TxnRef": "x", "vnp_SecureHash": "0f" * 64}
    answers = [
        client.get("/crm/public/v1/hooks/vnpay/ipn", params=unknown).status_code
        for _ in range(HOOK_MISSES_PER_MINUTE + 1)
    ]
    assert answers[0] == 200 and answers[-1] == 429  # an address of unknown references is cut off


def test_the_stub_gateway_routes(public: tuple[TestClient, list]) -> None:
    client, calls = public
    res = client.get("/crm/public/v1/pay/stub", params={"vnp_TxnRef": REF})
    assert res.status_code == 200 and "stub" in res.text and res.headers["x-frame-options"] == "DENY"
    res = client.post(f"/crm/public/v1/pay/stub/{REF}", data={"outcome": "paid"}, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "https://go.test/c/p/paid"
    assert calls[-1][1][3:] == (REF, "paid")
    client.post(f"/crm/public/v1/pay/stub/{REF}", data={"outcome": "anything"}, follow_redirects=False)
    assert calls[-1][1][4] == "cancel"
    assert client.post(f"/crm/public/v1/pay/stub/{REF}", content=b"x" * 2000).status_code == 413


def test_the_pay_online_button(public: tuple[TestClient, list]) -> None:
    client, calls = public
    client.cookies.set(client_pages.PROPOSAL_COOKIE, "t" * 43, domain="go.test", path="/")
    res = client.post(
        "/crm/public/v1/proposal/invoices/00000000-0000-4000-8000-0000000000d1/card",
        headers={"Accept-Language": "vi-VN,vi;q=0.9"},
    )
    assert res.status_code == 200 and res.json() == {"redirect_url": "https://pay.test/x"}
    assert calls[-1][2]["locale"] == "vi" and calls[-1][2]["client_ip"]
    assert client_pages._ip_only("203.0.113.0/24") == "127.0.0.1" and client_pages._ip_only("203.0.113.5")


def _owner(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str = "owner") -> tuple[TestClient, dict]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    ctx = tenant_ctx(ScriptedConn(), role=role)

    def tx(*a: Any, **kw: Any) -> Any:
        margin.current_member_role.set(role)
        return fake_tx(ctx)(*a, **kw)

    monkeypatch.setattr(payments, "request_pool", _pool)
    monkeypatch.setattr(payments, "tenant_tx", tx)
    monkeypatch.setattr(payments, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(payments, "require_keys", lambda keys: keys)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers


def test_the_owner_settings_routes(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple, dict]] = []
    monkeypatch.setattr(gateway, "status", _record(calls, "status", {"connected": False}))
    monkeypatch.setattr(gateway, "connect", _record(calls, "connect", {"vn_gateway": {"mode": "sandbox"}}))
    monkeypatch.setattr(booking_settings, "read", _record(calls, "read", {"enabled": False}))
    monkeypatch.setattr(booking_settings, "update", _record(calls, "update", {"enabled": True}))
    client, h = _owner(pools, monkeypatch)
    assert client.get("/crm/v1/settings/payment-methods/vn-gateway", headers=h).json() == {"connected": False}
    body = {"tmn_code": "TESTSTUB1", "hash_secret": "an-invented-test-secret-0003", "mode": "sandbox"}
    assert client.put("/crm/v1/settings/payment-methods/vn-gateway", json=body, headers=h).status_code == 200
    assert calls[-1][1][3]["tmn_code"] == "TESTSTUB1"
    assert (
        client.put("/crm/v1/settings/payment-methods/vn-gateway", json={**body, "mode": "x"}, headers=h).status_code
        == 422
    )
    assert client.get("/crm/v1/settings/online-booking", headers=h).json() == {"enabled": False}
    assert client.put("/crm/v1/settings/online-booking", json={"enabled": True}, headers=h).json() == {"enabled": True}
    agent, ha = _owner(pools, monkeypatch, role="agent")
    assert agent.put("/crm/v1/settings/online-booking", json={"enabled": True}, headers=ha).status_code == 403
    assert agent.get("/crm/v1/settings/payment-methods/vn-gateway", headers=ha).status_code == 403
