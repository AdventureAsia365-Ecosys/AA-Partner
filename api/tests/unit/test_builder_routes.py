"""AA-949 routes: every ``/crm/v1`` builder route reaches its helper with the parsed body and the page's ``seq``; the
permission matrix refuses what a role may not do (the assistant prepares drafts without money fields and never
publishes); the AA routes call Booking between two transactions; the margin serializer strips net keys for the
assistant. The services run on PostgreSQL in tests/integration/test_builder_api.py."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.main import create_app
from app.proposals import aa_options, builder
from app.routers import builder as router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx

P = "00000000-0000-4000-8000-0000000000b1"
X = "00000000-0000-4000-8000-0000000000b2"
VIEW = {"proposal_id": P, "edit_seq": 3, "totals": {"sell_total": "10.00", "net_total": "6.00", "margin_total": "4.00"}}


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str, *, booking: Any = None) -> tuple:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), booking_client=booking)
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
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    out: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def stub(name: str, result: Any = None) -> Any:
        async def fn(*args: Any, **kwargs: Any) -> Any:
            out.append((name, args, kwargs))
            return VIEW if result is None else result

        return fn

    for name in ("create", "view", "update_settings", "add_day", "update_day", "delete_day", "reorder_days",
                 "add_block", "update_block", "delete_block", "add_agency_option", "update_option", "delete_option",
                 "add_rule", "delete_rule", "publish", "add_aa_option", "apply_refresh"):  # fmt: skip
        monkeypatch.setattr(builder, name, stub(name))
    monkeypatch.setattr(builder, "aa_plan", stub("aa_plan", {"option_id": X, "component_kind": "trip_plan"}))
    monkeypatch.setattr(builder, "aa_refresh_plan", stub("aa_refresh_plan", [{"option_id": X}]))
    monkeypatch.setattr(aa_options, "quote", stub("quote", "QUOTE"))
    monkeypatch.setattr(aa_options, "refresh", stub("refresh", ([("plan", "QUOTE")], [])))
    return out


def test_every_route_reaches_its_helper(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "agent", booking=object())
    base = f"/crm/v1/proposals/{P}"
    requests = [
        ("post", f"/crm/v1/trips/{X}/builder", {"currency": "USD"}, "create", 201),
        ("get", f"{base}/builder", None, "view", 200),
        ("patch", f"{base}/builder", {"seq": 3, "price_visibility": "total_only", "deposit_pct": "40",
                                      "room_config": [{"type": "double", "adults": 2}]}, "update_settings", 200),
        ("post", f"{base}/days", {"seq": 3, "title": "Pokhara", "tz": "Asia/Kathmandu"}, "add_day", 201),
        ("patch", f"{base}/days/{X}", {"seq": 3, "notes": "x"}, "update_day", 200),
        ("delete", f"{base}/days/{X}?seq=3", None, "delete_day", 200),
        ("put", f"{base}/days/order", {"seq": 3, "day_ids": [X]}, "reorder_days", 200),
        ("post", f"{base}/days/{X}/blocks", {"seq": 3, "title": "Lodging", "type": "lodging"}, "add_block", 201),
        ("patch", f"{base}/blocks/{X}", {"seq": 3, "default_option_id": None}, "update_block", 200),
        ("delete", f"{base}/blocks/{X}?seq=3", None, "delete_block", 200),
        ("post", f"{base}/blocks/{X}/options", {"seq": 3, "title": "Homestay", "sell_amount": "280",
                                                "net_amount": "140"}, "add_agency_option", 201),
        ("patch", f"{base}/options/{X}", {"seq": 3, "sell_amount": "300"}, "update_option", 200),
        ("delete", f"{base}/options/{X}?seq=3", None, "delete_option", 200),
        ("post", f"{base}/rules", {"seq": 3, "kind": "excludes", "option_id": X, "target_option_id": P},
         "add_rule", 201),
        ("delete", f"{base}/rules/{X}?seq=3", None, "delete_rule", 200),
        ("post", f"{base}/publish", {"seq": 3, "acknowledge_selection_seq": 1}, "publish", 200),
    ]  # fmt: skip
    for method, path, body, helper, status in requests:
        r = getattr(client, method)(path, headers=h, **({"json": body} if body is not None else {}))
        assert r.status_code == status, (path, r.text)
        assert calls[-1][0] == helper, path
    name, args, kw = calls[-1]
    assert kw == {"acknowledge_selection_seq": 1} and args[3] == 3
    settings = next(c for c in calls if c[0] == "update_settings")
    assert settings[1][4] == {"price_visibility": "total_only", "deposit_pct": pytest.approx(40),
                              "room_config": [{"type": "double", "adults": 2, "children": 0}]}  # fmt: skip
    block = next(c for c in calls if c[0] == "update_block")
    assert block[1][5] == {"default_option_id": None}  # an explicit null clears the default


def test_writes_need_seq_and_known_values(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "agent")
    base = f"/crm/v1/proposals/{P}"
    assert client.post(f"{base}/days", headers=h, json={"title": "x"}).status_code == 422
    assert client.delete(f"{base}/days/{X}", headers=h).status_code == 422
    assert (
        client.patch(f"{base}/builder", headers=h, json={"seq": 1, "price_visibility": "per_line"}).status_code == 422
    )
    assert client.post(f"{base}/rules", headers=h, json={"seq": 1, "kind": "maybe", "option_id": X,
                                                        "target_option_id": P}).status_code == 422  # fmt: skip
    assert client.post(f"{base}/blocks/{X}/options", headers=h, json={"seq": 1, "title": "x",
                                                                    "source": "aa"}).status_code == 422  # fmt: skip
    assert calls == []


def test_the_assistant_prepares_drafts_without_money(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    client, h = _client(pools, monkeypatch, "assistant", booking=object())
    base = f"/crm/v1/proposals/{P}"
    refused = [
        ("patch", f"{base}/builder", {"seq": 3, "deposit_pct": "40"}),
        ("post", f"{base}/blocks/{X}/options", {"seq": 3, "title": "Homestay", "sell_amount": "280"}),
        ("post", f"{base}/blocks/{X}/options", {"seq": 3, "title": "Homestay", "net_amount": "140"}),
        ("patch", f"{base}/options/{X}", {"seq": 3, "sell_amount": "1"}),
        ("post", f"{base}/blocks/{X}/aa-options", {"seq": 3, "tour_source_id": "T-1", "component_id": "tp:1",
                                                   "component_kind": "trip_plan", "sell_amount": "1"}),
        ("post", f"{base}/publish", {"seq": 3}),
    ]  # fmt: skip
    for method, path, body in refused:
        r = getattr(client, method)(path, headers=h, json=body)
        assert r.status_code == 403, (path, r.text)
    assert calls == []
    r = client.post(f"{base}/blocks/{X}/options", headers=h, json={"seq": 3, "title": "Homestay"})
    assert r.status_code == 201
    # The serializer strips net and margin for her, whatever a helper built.
    r = client.get(f"{base}/builder", headers=h)
    assert r.json()["totals"] == {"sell_total": "10.00"}


def test_aa_routes_quote_between_two_transactions(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    client, h = _client(pools, monkeypatch, "agent", booking=object())
    base = f"/crm/v1/proposals/{P}"
    r = client.post(
        f"{base}/blocks/{X}/aa-options",
        headers=h,
        json={"seq": 3, "tour_source_id": "T-1", "component_id": "tp:1", "component_kind": "trip_plan"},
    )
    assert r.status_code == 201, r.text
    assert [c[0] for c in calls] == ["aa_plan", "quote", "add_aa_option"]
    calls.clear()
    r = client.post(f"{base}/aa-refresh", headers=h, json={"seq": 3})
    assert r.status_code == 200 and r.json()["refresh"] == {"quoted": 1, "failed": []}
    assert [c[0] for c in calls] == ["aa_refresh_plan", "refresh", "apply_refresh"]


def test_aa_routes_need_booking(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "agent", booking=None)
    r = client.post(f"/crm/v1/proposals/{P}/aa-refresh", headers=h, json={"seq": 3})
    assert r.status_code == 503 and r.json()["detail"] == "booking_not_configured"
    assert calls == []
