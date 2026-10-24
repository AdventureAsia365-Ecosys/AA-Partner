"""Margin protection: the per-role serializer and the contract test over every ``/crm/v1`` route (AA-939, design v3
§4.6 Fix B12). Card R test: any assistant response containing a margin, net or commission key fails the test.

1. Structural: every ``/crm/v1`` route of the live app is a :class:`app.margin.MarginFilteredRoute`, so no route can
   answer without the filter (a new router built with ``class_router(AGENCY)`` gets it automatically).
2. Contract: for every ``/crm/v1`` (path, method) of the live app, a twin route with the same path, method, class
   guard and route class answers a payload full of margin keys after "tenant_tx()" recorded the role: the assistant
   (and an unknown role) gets none of them, at any depth; the owner and the agent get all of them.
3. A real route end to end (the board) with a payload carrying margin keys.

Scope (AA-939 review): no current ``/crm/v1`` route emits a money field, so 2. proves the filter on synthetic bodies.
The filter covers JSON bodies only and strips by key name (it over-strips e.g. ``cost_center`` for the filtered roles);
a streaming or file route must check ``VIEW_MARGIN`` itself (see the ``app.margin`` docstring). The cards that add
money fields (quotes, bookings) re-assert this on their real routes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import margin
from app.auth.identity_config import AGENCY
from app.auth.route_guard import class_router
from app.main import create_app
from app.margin import MarginFilteredRoute, filter_body, is_margin_key, strip_margin
from app.routers import pipeline as pipeline_router
from tests.tokens import LocalPools
from tests.unit.slice_fakes import TENANT, fake_tx, tenant_ctx

MARGIN_PAYLOAD: dict[str, Any] = {
    "trip_id": "t1",
    "title": "Vietnam, 9 nights",
    "net": 1,
    "net_price": 2,
    "price_net": 3,
    "b2b_price": 4,
    "cost": 5,
    "unit_cost": 6,
    "margin": 7,
    "margin_pct": 8,
    "gross_margin_total": 9,
    "commission": 10,
    "commission_expected": 11,
    "option": {"net_delta": 12, "label": "Upgrade", "lines": [{"cost_base": 13, "sell": 20}]},
    "metrics": [{"margin_daily": 14, "enquiries": 3}],
    "sell_price": 99,
}
MARGIN_WORDS = re.compile(r"(^|_)(net|b2b|cost|costs|margin|margins|commission|commissions)(_|$)")


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


def _walk(routes: list[Any]) -> Iterator[Any]:
    for r in routes:
        inner = getattr(r, "original_router", None)  # FastAPI >= 0.140 wraps an included router
        if inner is not None:
            yield from _walk(inner.routes)
        else:
            yield r


def _agency_routes(app: FastAPI) -> list[APIRoute]:
    routes = [r for r in _walk(app.routes) if isinstance(r, APIRoute) and r.path.startswith("/crm/v1")]
    # The walk must see every operation the app serves under /crm/v1 (the OpenAPI view of the same app).
    served = {(m.upper(), p) for p, ops in app.openapi()["paths"].items() if p.startswith("/crm/v1") for m in ops}
    assert {(m, r.path) for r in routes for m in r.methods} == served
    return routes


@pytest.mark.parametrize(
    ("key", "margin_key"),
    [
        ("net", True),
        ("net_price", True),
        ("price_net", True),
        ("b2b_price", True),
        ("cost", True),
        ("unit_cost", True),
        ("cost_base", True),
        ("margin", True),
        ("margin_pct", True),
        ("commission", True),
        ("commission_expected", True),
        ("commissions", True),
        ("NET_DELTA", True),
        ("network", False),
        ("internet_ok", False),
        ("sell_price", False),
        ("ai_budget_usd", False),
        ("title", False),
        ("costume", False),
        ("cabinet", False),
        ("pricing", False),
    ],
)
def test_margin_key_names(key: str, margin_key: bool) -> None:
    assert is_margin_key(key) is margin_key


def test_strip_margin_walks_every_depth() -> None:
    removed: set[str] = set()
    cleaned = strip_margin(MARGIN_PAYLOAD, removed)
    assert not any(MARGIN_WORDS.search(k) for k in _keys(cleaned))
    assert cleaned["option"] == {"label": "Upgrade", "lines": [{"sell": 20}]}
    assert cleaned["metrics"] == [{"enquiries": 3}] and cleaned["sell_price"] == 99
    assert {"net", "b2b_price", "commission_expected", "net_delta", "cost_base", "margin_daily"} <= removed


def test_filter_body_by_role() -> None:
    body = json.dumps(MARGIN_PAYLOAD).encode()
    for role in ("owner", "agent"):
        assert filter_body(body, role) == (body, set())
    for role in ("assistant", None, "viewer"):
        cleaned, removed = filter_body(body, role)
        assert removed and not any(MARGIN_WORDS.search(k) for k in _keys(json.loads(cleaned)))
    plain = json.dumps({"title": "x"}).encode()
    assert filter_body(plain, "assistant") == (plain, set())
    assert filter_body(b"not json net", "assistant") == (b"not json net", set())


def test_every_agency_route_of_the_live_app_is_margin_filtered(pools: LocalPools) -> None:
    app = create_app(pools.settings(), verifier=pools.verifier(pools.settings()), keys=object())
    routes = _agency_routes(app)
    assert len(routes) >= 40  # pipeline, travellers, team, invitations, agency
    offenders = [f"{sorted(r.methods)} {r.path}" for r in routes if not isinstance(r, MarginFilteredRoute)]
    assert offenders == []


def _twin_app(pools: LocalPools, routes: list[APIRoute], role: str | None) -> FastAPI:
    """Twin routes: same paths, methods, AGENCY guard and route class; the endpoint records the role the way
    tenant_tx() does and answers the margin payload."""

    async def endpoint(request: Request) -> dict[str, Any]:
        if role is not None:
            margin.current_member_role.set(role)
        return MARGIN_PAYLOAD

    router = class_router(AGENCY)
    for route in routes:
        router.add_api_route(route.path[len("/crm/v1") :], endpoint, methods=sorted(route.methods))
    app = FastAPI()
    app.state.verifier = pools.verifier(pools.settings())
    from app.auth.identity_config import identity_from_settings

    app.state.identity = identity_from_settings(pools.settings())
    app.include_router(router)
    return app


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "00000000-0000-4000-8000-000000000001", path)


@pytest.fixture(scope="module")
def live_routes(pools: LocalPools) -> list[APIRoute]:
    app = create_app(pools.settings(), verifier=pools.verifier(pools.settings()), keys=object())
    return _agency_routes(app)


@pytest.mark.parametrize("role", ["assistant", None])
def test_r_contract_no_margin_key_reaches_an_assistant_on_any_agency_route(
    pools: LocalPools, live_routes: list[APIRoute], role: str | None
) -> None:
    app = _twin_app(pools, live_routes, role)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    failures = []
    with TestClient(app) as client:
        for route in live_routes:
            for method in sorted(route.methods):
                res = client.request(method, _fill(route.path), headers=headers, json={})
                leaked = sorted(k for k in _keys(res.json()) if MARGIN_WORDS.search(k))
                if res.status_code != 200 or leaked:
                    failures.append(f"{method} {route.path}: {res.status_code} {leaked}")
    assert failures == []


@pytest.mark.parametrize("role", ["owner", "agent"])
def test_owner_and_agent_keep_the_margin_keys(pools: LocalPools, live_routes: list[APIRoute], role: str) -> None:
    app = _twin_app(pools, live_routes, role)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    with TestClient(app) as client:
        for route in live_routes:
            method = sorted(route.methods)[0]
            assert client.request(method, _fill(route.path), headers=headers, json={}).json() == MARGIN_PAYLOAD


@pytest.fixture
def board_client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, list[str]]]:
    roles: list[str] = []

    async def _pool(_request: Any) -> object:
        return object()

    async def board(ctx: Any, vault: Any, flt: Any) -> dict[str, Any]:
        return {"cards": [{"trip_id": "t1", "b2b_price": 100, "commission_expected": 5, "title": "x"}]}

    def tx(*a: Any, **kw: Any) -> Any:
        role = roles[-1]
        margin.current_member_role.set(role)  # what the real tenant_tx() records
        return fake_tx(tenant_ctx(role=role))(*a, **kw)

    monkeypatch.setattr(pipeline_router, "request_pool", _pool)
    monkeypatch.setattr(pipeline_router, "board", board)
    monkeypatch.setattr(pipeline_router, "tenant_tx", tx)
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    with TestClient(app) as client:
        yield client, roles


def test_a_real_route_strips_margin_for_the_assistant_only(
    board_client: tuple[TestClient, list[str]], pools: LocalPools
) -> None:
    client, roles = board_client
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    roles.append("assistant")
    assert client.get("/crm/v1/pipeline", headers=headers).json() == {"cards": [{"trip_id": "t1", "title": "x"}]}
    roles.append("agent")
    assert client.get("/crm/v1/pipeline", headers=headers).json()["cards"][0]["b2b_price"] == 100
