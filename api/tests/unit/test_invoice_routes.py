"""AA-952 slice 2 routes: every ``/crm/v1`` invoice route reaches its service function with the parsed body, and the
permission matrix refuses what a role may not do (the services run on PostgreSQL in
tests/integration/test_invoices_api.py)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.invoices import service
from app.main import create_app
from app.routers import invoices as router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx

ID = "00000000-0000-4000-8000-0000000000e1"


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
    for name in ("list_for_trip", "create_from_schedule", "create_typed", "receivables", "get", "update_draft",
                 "issue", "void", "document", "get_settings", "update_settings"):  # fmt: skip

        def make(n: str) -> Any:
            async def fn(*args: Any, **kwargs: Any) -> Any:
                out.append((n, args, kwargs))
                return {"ok": n}

            return fn

        monkeypatch.setattr(service, name, make(name))
    return out


LINES = [{"description": "Planning fee", "unit_amount": "50"}]


def test_every_route_reaches_its_helper(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "owner")
    requests = [
        ("GET", f"/crm/v1/trips/{ID}/invoices", None, 200),
        ("POST", f"/crm/v1/trips/{ID}/invoices", {"schedule_id": ID}, 201),
        ("POST", f"/crm/v1/trips/{ID}/invoices", {"kind": "service_fee", "currency": "USD", "lines": LINES}, 201),
        ("GET", "/crm/v1/invoices?status=paid", None, 200),
        ("GET", f"/crm/v1/invoices/{ID}", None, 200),
        ("PATCH", f"/crm/v1/invoices/{ID}", {"memo": "Thanks", "lines": LINES}, 200),
        ("POST", f"/crm/v1/invoices/{ID}/issue", None, 200),
        ("POST", f"/crm/v1/invoices/{ID}/void", {"reason": "Raised twice"}, 200),
        ("GET", f"/crm/v1/invoices/{ID}/document", None, 200),
        ("GET", "/crm/v1/settings/invoices", None, 200),
        ("PUT", "/crm/v1/settings/invoices", {"invoice_prefix": "SV", "invoice_tax_rate": "10"}, 200),
    ]
    with client:
        for method, path, body, status in requests:
            res = client.request(method, path, headers=h, json=body)
            assert res.status_code == status, (method, path, res.text)
    by_name = {n: (a, kw) for n, a, kw in calls}
    typed = by_name["create_typed"][1]
    assert typed["kind"] == "service_fee" and str(typed["lines"][0]["unit_amount"]) == "50"
    assert str(typed["lines"][0]["quantity"]) == "1"
    assert by_name["receivables"][0][1] == "paid"
    patch = by_name["update_draft"][0][2]
    assert patch["memo"] == "Thanks" and patch["lines"][0]["description"] == "Planning fee"
    assert by_name["void"][0][2] == "Raised twice"
    assert by_name["update_settings"][0][1] == {"invoice_prefix": "SV", "invoice_tax_rate": pytest.approx(10)}


def test_schedule_and_lines_together_are_refused(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    client, h = _client(pools, monkeypatch, "agent")
    with client:
        res = client.post(f"/crm/v1/trips/{ID}/invoices", headers=h, json={"schedule_id": ID, "lines": LINES})
        assert res.status_code == 422 and res.json()["detail"] == "schedule_or_lines"
        res = client.post(f"/crm/v1/trips/{ID}/invoices", headers=h, json={"schedule_id": ID, "extra": 1})
        assert res.status_code == 422
    assert calls == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", f"/crm/v1/trips/{ID}/invoices", {"schedule_id": ID}),
        ("PATCH", f"/crm/v1/invoices/{ID}", {"memo": "x"}),
        ("POST", f"/crm/v1/invoices/{ID}/issue", None),
        ("POST", f"/crm/v1/invoices/{ID}/void", {"reason": "x"}),
        ("GET", "/crm/v1/settings/invoices", None),
        ("PUT", "/crm/v1/settings/invoices", {"service_fee_enabled": True}),
    ],
)
def test_the_assistant_reads_but_does_not_bill(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list, method: str, path: str, body: Any
) -> None:
    client, h = _client(pools, monkeypatch, "assistant")
    with client:
        assert client.request(method, path, headers=h, json=body).status_code == 403
        assert client.get(f"/crm/v1/invoices/{ID}", headers=h).status_code == 200
    assert [c[0] for c in calls] == ["get"]


def test_the_agent_may_not_change_the_invoice_settings(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    client, h = _client(pools, monkeypatch, "agent")
    with client:
        assert client.put("/crm/v1/settings/invoices", headers=h, json={"invoice_due_days": 3}).status_code == 403
    assert calls == []
