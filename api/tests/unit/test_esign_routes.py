"""AA-955 routes: ``/crm/v1/trips/{id}/agreements`` and ``/crm/v1/signatures/{id}/envelope`` reach their helpers with
the right permission; the client pages pass the e-sign evidence (client address, user agent) and the sealer on, and the
terms re-accept routes (by token and by cookie) reach :func:`app.proposals.public.reaccept_terms`."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.esign import service
from app.main import create_app
from app.portal import public as portal
from app.proposals import public as proposal_page
from app.routers import client_pages
from app.routers import esign as router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx

ID = "00000000-0000-4000-8000-0000000000e1"
TOKEN = "t" * 43


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
    return TestClient(app), {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}


@pytest.mark.parametrize("role", ["owner", "agent", "assistant"])
def test_the_agency_routes(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    calls: list[tuple[str, Any]] = []

    async def view(_ctx: Any, trip_id: Any) -> dict[str, Any]:
        calls.append(("view", str(trip_id)))
        return {"trip_id": str(trip_id)}

    async def envelope(_ctx: Any, _vault: Any, sealer: Any, request_id: Any, reason: str) -> dict[str, Any]:
        calls.append(("envelope", (str(request_id), reason, type(sealer).__name__)))
        return {"request_id": str(request_id)}

    monkeypatch.setattr(service, "trip_agreements", view)
    monkeypatch.setattr(service, "envelope", envelope)
    client, h = _client(pools, monkeypatch, role)
    with client:
        assert client.get(f"/crm/v1/trips/{ID}/agreements", headers=h).status_code == 200
        res = client.post(f"/crm/v1/signatures/{ID}/envelope", headers=h, json={"reason": "client_request"})
        assert res.status_code == 200, res.text
        assert client.post(f"/crm/v1/signatures/{ID}/envelope", headers=h, json={}).status_code == 422
        assert (
            client.post(f"/crm/v1/signatures/{ID}/envelope", headers=h, json={"reason": "x", "y": 1}).status_code == 422
        )
    assert calls == [("view", ID), ("envelope", (ID, "client_request", "DevStubSealer"))]


def test_the_client_pages_pass_the_evidence(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = pools.settings(rate_limit_intake_per_minute=50)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(client_pages, "request_pool", _pool)
    monkeypatch.setattr(client_pages, "require_keys", lambda keys: keys)
    seen: list[tuple[str, dict[str, Any]]] = []

    def stub(name: str, result: Any) -> Any:
        async def fn(*args: Any, **kw: Any) -> Any:
            seen.append((name, kw))
            return result

        return fn

    monkeypatch.setattr(proposal_page, "accept", stub("accept", {"status": "accepted"}))
    monkeypatch.setattr(proposal_page, "reaccept_terms", stub("reaccept", {"status": "accepted"}))
    monkeypatch.setattr(portal, "sign", stub("sign", (True, {"status": "signed"})))
    monkeypatch.setattr(portal, "home", stub("home", {"signed_in": False}))
    ua = {"User-Agent": "TestBrowser/1"}
    with TestClient(app, base_url="https://go.test") as c:
        assert c.post(f"/crm/public/v1/p/{TOKEN}/accept", json={}, headers=ua).status_code == 200
        assert c.post(f"/crm/public/v1/p/{TOKEN}/terms", json={}, headers=ua).status_code == 200
        c.cookies.set("aa_proposal_link", TOKEN)
        assert c.post("/crm/public/v1/proposal/terms", json={}, headers=ua).status_code == 200
        assert c.post("/crm/public/v1/proposal/accept", json={}, headers=ua).status_code == 200
        assert c.post(f"/crm/public/v1/portal/{TOKEN}/sign", json={}, headers=ua).json() == {"status": "signed"}
        assert c.get(f"/crm/public/v1/portal/{TOKEN}").status_code == 200
    assert [n for n, _ in seen] == ["accept", "reaccept", "reaccept", "accept", "sign", "home"]
    for name, kw in seen[:5]:
        assert kw["user_agent"] == "TestBrowser/1" and kw["client_ip"], name
    assert type(seen[4][1]["sealer"]).__name__ == "DevStubSealer"
