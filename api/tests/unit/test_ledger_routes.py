"""AA-944 ``/crm/v1`` ledger routes: each route reaches its helper with the parsed body, the permission matrix refuses
what a role may not do (money and payment settings), and money keys never reach the assistant through a real route
(the services run on PostgreSQL in tests/integration/test_ledger_api.py)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.ledger import fx, lines, suppliers
from app.lifecycle import trash
from app.main import create_app
from app.routers import ledger as router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx

ID = "00000000-0000-4000-8000-0000000000e1"
MONEY = {"cost": {"amount": "100", "currency": "USD"}, "commission_expected": None, "margin_base": "30", "sell": {}}


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
        margin.current_member_role.set(role)  # what the real tenant_tx() records for the serializer
        return fake_tx(ctx)(*a, **kw)

    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", tx)
    monkeypatch.setattr(router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers


def _stub(calls: list[tuple[str, Any]], name: str, result: Any = None) -> Any:
    async def fn(*args: Any, **kwargs: Any) -> Any:
        calls.append((name, kwargs or args[2:] if len(args) > 2 else kwargs))
        return result if result is not None else {"ok": name}

    return fn


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for name in (
        "list_suppliers",
        "get_supplier",
        "create_supplier",
        "update_supplier",
        "reveal_contact",
        "trash_check",
    ):
        monkeypatch.setattr(suppliers, name, _stub(out, f"suppliers.{name}"))
    for name in ("trip_lines", "create_line", "update_line", "confirm", "confirm_check", "cancel", "trash_check"):
        monkeypatch.setattr(lines, name, _stub(out, f"lines.{name}"))
    monkeypatch.setattr(lines, "put_money", _stub(out, "lines.put_money", {"money": MONEY}))
    monkeypatch.setattr(lines, "ledger", _stub(out, "lines.ledger", {"lines": [{"money": MONEY}], "totals": {}}))
    for name in ("payment_settings", "update_payment_settings", "list_overrides", "add_override"):
        monkeypatch.setattr(fx, name, _stub(out, f"fx.{name}"))
    monkeypatch.setattr(trash, "move_to_trash", _stub(out, "trash.move_to_trash"))
    return out


def test_every_route_reaches_its_helper(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "owner")
    line = {"agency_supplier_id": ID, "title": "Hanoi 2 nights"}
    requests = [
        ("GET", "/crm/v1/suppliers", None, 200),
        ("POST", "/crm/v1/suppliers", {"name": "Pearl", "default_commission_pct": 10}, 201),
        ("GET", f"/crm/v1/suppliers/{ID}", None, 200),
        ("PATCH", f"/crm/v1/suppliers/{ID}", {"name": "Pearl 2"}, 200),
        ("DELETE", f"/crm/v1/suppliers/{ID}", None, 200),
        ("POST", f"/crm/v1/suppliers/{ID}/reveal", {"field": "contact_name", "reason": "prepare_booking"}, 200),
        ("GET", f"/crm/v1/trips/{ID}/lines", None, 200),
        ("POST", f"/crm/v1/trips/{ID}/lines", line, 201),
        ("PATCH", f"/crm/v1/lines/{ID}", {"confirmation_no": "HP-1"}, 200),
        ("GET", f"/crm/v1/lines/{ID}/confirm-check", None, 200),
        ("POST", f"/crm/v1/lines/{ID}/confirm", None, 200),
        ("POST", f"/crm/v1/lines/{ID}/cancel", None, 200),
        ("DELETE", f"/crm/v1/lines/{ID}", None, 200),
        ("PUT", f"/crm/v1/lines/{ID}/money", {"cost": {"amount": "100", "currency": "USD"}}, 200),
        ("GET", "/crm/v1/bookings?status=confirmed&from=2026-10-01", None, 200),
        ("GET", "/crm/v1/settings/payments", None, 200),
        ("PUT", "/crm/v1/settings/payments", {"booking_validation_level": "strict"}, 200),
        ("GET", "/crm/v1/fx/overrides", None, 200),
        ("POST", "/crm/v1/fx/overrides", {"base": "USD", "quote": "VND", "rate": "25500"}, 201),
    ]
    with client:
        for method, path, body, status in requests:
            res = client.request(method, path, headers=h, json=body)
            assert res.status_code == status, (method, path, res.text)
    names = [c[0] for c in calls]
    assert names.count("trash.move_to_trash") == 2 and "suppliers.trash_check" in names and "lines.trash_check" in names
    assert "fx.add_override" in names and "lines.put_money" in names


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", f"/crm/v1/lines/{ID}/money", {"cost": {"amount": "1", "currency": "USD"}}),
        ("POST", "/crm/v1/suppliers", {"name": "Pearl", "default_commission_pct": 5}),
        ("PATCH", f"/crm/v1/suppliers/{ID}", {"default_commission_pct": 5}),
        ("GET", "/crm/v1/settings/payments", None),
        ("PUT", "/crm/v1/settings/payments", {"fx_mode": "manual"}),
        ("POST", "/crm/v1/fx/overrides", {"base": "USD", "quote": "VND", "rate": "1"}),
    ],
)
def test_the_assistant_may_not_touch_money_or_payment_settings(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list, method: str, path: str, body: Any
) -> None:
    client, h = _client(pools, monkeypatch, "assistant")
    with client:
        res = client.request(method, path, headers=h, json=body)
    assert res.status_code == 403 and res.json()["detail"] == "role_forbidden"


def test_payment_settings_are_owner_only(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "agent")
    with client:
        assert client.get("/crm/v1/settings/payments", headers=h).status_code == 403
        assert client.put(f"/crm/v1/lines/{ID}/money", headers=h, json={"sell": None}).status_code == 200


def test_the_assistant_confirms_and_reads_lines_without_margin(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    client, h = _client(pools, monkeypatch, "assistant")
    with client:
        assert client.post(f"/crm/v1/lines/{ID}/confirm", headers=h).status_code == 200
        ledger = client.get("/crm/v1/bookings", headers=h).json()
    assert ledger["lines"][0]["money"] == {"sell": {}}  # cost / commission / margin stripped by key


@pytest.mark.parametrize(
    "body",
    [
        {"cost": {"amount": "-1", "currency": "USD"}},
        {"cost": {"amount": "1", "currency": "usd"}},
        {"tip": {"amount": "1", "currency": "USD"}},
    ],
)
def test_money_bodies_are_validated(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list, body: Any) -> None:
    client, h = _client(pools, monkeypatch, "owner")
    with client:
        assert client.put(f"/crm/v1/lines/{ID}/money", headers=h, json=body).status_code == 422
