"""AA-952 slice 3 routes: every ``/crm/v1`` payment / refund / payment-method route reaches its service function with
the parsed body, and the permission matrix refuses what a role may not do (the services run on PostgreSQL in
tests/integration/test_payments_api.py)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.main import create_app
from app.payments import service
from app.routers import payments as router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx

ID = "00000000-0000-4000-8000-0000000000e1"
NAMES = ("record_payment", "request_refund", "list_refunds", "decide_refund", "execute_refund", "payment_methods",
         "save_manual", "connect", "disconnect")  # fmt: skip


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> tuple[TestClient, dict[str, str]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    ctx = tenant_ctx(ScriptedConn(), role=role)

    def tx(*a: Any, **kw: Any) -> Any:
        margin.current_member_role.set(role)
        return fake_tx(ctx)(*a, **kw)

    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", tx)
    monkeypatch.setattr(router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple, dict]]:
    out: list[tuple[str, tuple, dict]] = []
    for name in NAMES:

        def make(n: str) -> Any:
            async def fn(*args: Any, **kwargs: Any) -> Any:
                out.append((n, args, kwargs))
                return {"ok": n}

            return fn

        monkeypatch.setattr(service, name, make(name))
    return out


def test_every_route_reaches_its_helper(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "owner")
    requests = [
        ("POST", f"/crm/v1/invoices/{ID}/payments", {"amount": "100", "method": "cash", "reference": "TT 1"}, 201),
        ("POST", f"/crm/v1/payments/{ID}/refund-requests", {"amount": "20", "reason": "Shortened"}, 201),
        ("GET", "/crm/v1/refund-requests?status=executed", None, 200),
        ("POST", f"/crm/v1/refund-requests/{ID}/approve", {"note": "OK"}, 200),
        ("POST", f"/crm/v1/refund-requests/{ID}/reject", {}, 200),
        ("POST", f"/crm/v1/refund-requests/{ID}/execute", {"method": "bank_transfer"}, 200),
        ("GET", "/crm/v1/settings/payment-methods", None, 200),
        ("PUT", "/crm/v1/settings/payment-methods/manual", {"display_name": "Bank", "instructions": "Acct"}, 200),
        ("POST", "/crm/v1/settings/payment-methods", {"provider": "stripe"}, 201),
        ("POST", f"/crm/v1/settings/payment-methods/{ID}/disconnect", None, 200),
    ]
    with client:
        for method, path, body, status in requests:
            res = client.request(method, path, headers=h, json=body)
            assert res.status_code == status, (method, path, res.text)
    by_name: dict[str, list] = {}
    for n, a, kw in calls:
        by_name.setdefault(n, []).append((a, kw))
    paid = by_name["record_payment"][0][1]
    assert str(paid["amount"]) == "100" and paid["method"] == "cash" and paid["reference"] == "TT 1"
    assert [kw["approve"] for _, kw in by_name["decide_refund"]] == [True, False]
    assert by_name["list_refunds"][0][0][1] == "executed" and by_name["connect"][0][0][1] == "stripe"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", f"/crm/v1/invoices/{ID}/payments", {"amount": "1", "method": "cash"}),
        ("POST", f"/crm/v1/payments/{ID}/refund-requests", {"amount": "1", "reason": "x"}),
        ("POST", f"/crm/v1/refund-requests/{ID}/approve", {}),
        ("POST", f"/crm/v1/refund-requests/{ID}/execute", {"method": "cash"}),
        ("GET", "/crm/v1/settings/payment-methods", None),
        ("PUT", "/crm/v1/settings/payment-methods/manual", {"display_name": "B", "instructions": "A"}),
    ],
)
def test_the_assistant_reads_refunds_but_moves_no_money(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list, method: str, path: str, body: Any
) -> None:
    client, h = _client(pools, monkeypatch, "assistant")
    with client:
        assert client.request(method, path, headers=h, json=body).status_code == 403
        assert client.get("/crm/v1/refund-requests", headers=h).status_code == 200
    assert [c[0] for c in calls] == ["list_refunds"]


def test_the_agent_reaches_the_refund_decision_but_not_the_settings(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    client, h = _client(pools, monkeypatch, "agent")
    with client:
        # THRESHOLD: the route lets the agent in; the service checks the amount (tests/unit/test_payments.py).
        assert client.post(f"/crm/v1/refund-requests/{ID}/approve", headers=h, json={}).status_code == 200
        res = client.put("/crm/v1/settings/payment-methods/manual", headers=h, json={"display_name": "B",
                                                                                   "instructions": "A"})  # fmt: skip
        assert res.status_code == 403
        bad = client.post(f"/crm/v1/invoices/{ID}/payments", headers=h, json={"amount": "1", "method": "crypto"})
        assert bad.status_code == 422
    assert [c[0] for c in calls] == ["decide_refund"]
