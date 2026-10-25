"""AA-946 routes: the ``/crm/v1`` proposal / schedule / portal-access / agreement routes reach their helpers with the
parsed body and the permission matrix refuses what a role may not do; the ``/crm/public/v1`` client pages (proposal
accept, portal) reach theirs, set and clear the session cookie, and answer a wrong code with 422 (the services run on
PostgreSQL in tests/integration/test_lifecycle_api.py)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.main import create_app
from app.portal import access
from app.portal import public as portal
from app.proposals import agreements, service
from app.proposals import public as proposal_page
from app.routers import client_pages
from app.routers import proposals as router
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


def _stub(calls: list[tuple[str, Any]], name: str, result: Any = None) -> Any:
    async def fn(*args: Any, **kwargs: Any) -> Any:
        calls.append((name, kwargs))
        return result if result is not None else {"ok": name}

    return fn


def _agency_client(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str, owner: Any = None
) -> tuple[TestClient, dict[str, str]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    ctx = tenant_ctx(ScriptedConn().on("owner_member_id FROM crm.trip", {"owner_member_id": owner}), role=role)

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
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for name in ("trip_overview", "create_draft", "update_draft", "send", "withdraw", "mark_paid", "void"):
        monkeypatch.setattr(service, name, _stub(out, f"service.{name}"))
    for name in ("invite", "revoke"):
        monkeypatch.setattr(access, name, _stub(out, f"access.{name}"))
    monkeypatch.setattr(access, "trip_of", _stub(out, "access.trip_of", ID))
    for name in ("list_docs", "create", "update", "publish"):
        monkeypatch.setattr(agreements, name, _stub(out, f"agreements.{name}"))
    return out


PROPOSAL = {"body": "Day 1", "currency": "USD", "total_amount": "1000", "deposit_amount": "300",
            "balance_due_on": "2026-12-01"}  # fmt: skip


def test_every_agency_route_reaches_its_helper(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _agency_client(pools, monkeypatch, "owner")
    requests = [
        ("GET", f"/crm/v1/trips/{ID}/lifecycle", None, 200),
        ("POST", f"/crm/v1/trips/{ID}/proposals", PROPOSAL, 201),
        ("PATCH", f"/crm/v1/proposals/{ID}", {"body": "Day 1 and 2"}, 200),
        ("POST", f"/crm/v1/proposals/{ID}/send", None, 200),
        ("POST", f"/crm/v1/proposals/{ID}/withdraw", None, 200),
        ("POST", f"/crm/v1/schedule/{ID}/mark-paid", {"method": "bank_transfer"}, 200),
        ("POST", f"/crm/v1/schedule/{ID}/void", None, 200),
        ("POST", f"/crm/v1/trips/{ID}/portal-access", {"traveler_id": ID}, 201),
        ("POST", f"/crm/v1/portal-access/{ID}/revoke", None, 200),
        ("GET", "/crm/v1/agreements", None, 200),
        ("POST", "/crm/v1/agreements", {"kind": "waiver", "version_label": "v1", "body_text": "Risk."}, 201),
        ("PATCH", f"/crm/v1/agreements/{ID}", {"body_text": "Risk 2."}, 200),
        ("POST", f"/crm/v1/agreements/{ID}/publish", None, 200),
    ]
    with client:
        for method, path, body, status in requests:
            res = client.request(method, path, headers=h, json=body)
            assert res.status_code == status, (method, path, res.text)
    names = [c[0] for c in calls]
    assert names.count("service.mark_paid") == 1 and "access.trip_of" in names and "agreements.publish" in names
    draft = next(kw for n, kw in calls if n == "service.create_draft")
    assert str(draft["total"]) == "1000" and draft["currency"] == "USD"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", f"/crm/v1/proposals/{ID}/send", None),
        ("POST", f"/crm/v1/schedule/{ID}/mark-paid", {"method": "cash"}),
        ("POST", f"/crm/v1/schedule/{ID}/void", None),
        ("POST", "/crm/v1/agreements", {"kind": "waiver", "version_label": "v1", "body_text": "x"}),
        ("POST", f"/crm/v1/trips/{ID}/portal-access", {"traveler_id": ID}),  # not assigned to them
    ],
)
def test_the_assistant_drafts_but_does_not_send_or_take_money(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list, method: str, path: str, body: Any
) -> None:
    client, h = _agency_client(pools, monkeypatch, "assistant")
    with client:
        res = client.request(method, path, headers=h, json=body)
        assert res.status_code == 403, (path, res.text)
        assert client.post(f"/crm/v1/trips/{ID}/proposals", headers=h, json=PROPOSAL).status_code == 201


def test_agreements_are_owner_only(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _agency_client(pools, monkeypatch, "agent")
    with client:
        assert client.post(f"/crm/v1/agreements/{ID}/publish", headers=h).status_code == 403
        assert client.get("/crm/v1/agreements", headers=h).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {**PROPOSAL, "currency": "usd"},
        {**PROPOSAL, "total_amount": "0"},
        {**PROPOSAL, "net_price": "1"},
        {k: v for k, v in PROPOSAL.items() if k != "body"},
    ],
)
def test_proposal_bodies_are_validated(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list, body: Any
) -> None:
    client, h = _agency_client(pools, monkeypatch, "owner")
    with client:
        assert client.post(f"/crm/v1/trips/{ID}/proposals", headers=h, json=body).status_code == 422


# ── /crm/public/v1 client pages ─────────────────────────────────────────────────────────────────


@pytest.fixture
def public(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, list]]:
    settings = pools.settings(rate_limit_intake_per_minute=50)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(client_pages, "request_pool", _pool)
    monkeypatch.setattr(client_pages, "require_keys", lambda keys: keys)
    out: list[tuple[str, Any]] = []
    monkeypatch.setattr(proposal_page, "read", _stub(out, "proposal.read", {"status": "sent"}))
    monkeypatch.setattr(proposal_page, "accept", _stub(out, "proposal.accept", {"status": "accepted"}))
    monkeypatch.setattr(portal, "home", _stub(out, "portal.home", {"signed_in": True}))
    monkeypatch.setattr(portal, "request_code", _stub(out, "portal.request_code", {"sent": True}))
    monkeypatch.setattr(portal, "submit_stage3", _stub(out, "portal.submit_stage3", {"status": "received"}))
    monkeypatch.setattr(portal, "sign_out", _stub(out, "portal.sign_out", {"signed_in": False}))

    async def verify(_pool: Any, _settings: Any, _token: str, code: Any, **_: Any) -> tuple[Any, dict]:
        out.append(("portal.verify_code", {"code": code}))
        if code == "123456":
            return "session-token", {"signed_in": True}
        return None, {"detail": "code_invalid", "attempts_left": 4}

    async def sign(*args: Any, **kw: Any) -> tuple[bool, dict]:
        out.append(("portal.sign", args[5]))
        return (args[5].get("code") == "654321"), {"status": "signed"}

    monkeypatch.setattr(portal, "verify_code", verify)
    monkeypatch.setattr(portal, "sign", sign)
    with TestClient(app, base_url="https://go.test") as c:
        yield c, out


def test_the_proposal_page_routes(public: tuple[TestClient, list]) -> None:
    client, out = public
    res = client.get(f"/crm/public/v1/p/{TOKEN}")
    assert res.status_code == 200 and res.headers["referrer-policy"] == "no-referrer"
    assert res.headers["cache-control"] == "no-store"
    assert client.post(f"/crm/public/v1/p/{TOKEN}/accept", json={"full_name": "G"}).json() == {"status": "accepted"}
    assert client.get("/crm/public/v1/p/short").status_code == 422  # the token shape
    assert [n for n, _ in out] == ["proposal.read", "proposal.accept"]


def test_the_portal_routes_set_and_clear_the_session(public: tuple[TestClient, list]) -> None:
    client, out = public
    assert client.post(f"/crm/public/v1/portal/{TOKEN}/code", json={"purpose": "sign_in"}).status_code == 202
    wrong = client.post(f"/crm/public/v1/portal/{TOKEN}/session", json={"code": "000000"})
    assert wrong.status_code == 422 and wrong.json()["attempts_left"] == 4 and "set-cookie" not in wrong.headers
    ok = client.post(f"/crm/public/v1/portal/{TOKEN}/session", json={"code": "123456"})
    cookie = ok.headers["set-cookie"]
    assert ok.status_code == 200 and cookie.startswith(f"{portal.SESSION_COOKIE}=session-token")
    for part in ("HttpOnly", "Secure", "SameSite=strict", f"Path=/crm/public/v1/portal/{TOKEN}"):
        assert part.lower() in cookie.lower(), part
    assert client.get(f"/crm/public/v1/portal/{TOKEN}").json() == {"signed_in": True}
    home_call = [kw for n, kw in out if n == "portal.home"]
    assert home_call  # the cookie went with the request (path matches)
    assert client.put(f"/crm/public/v1/portal/{TOKEN}/stage3", json={"dob": "1984-03-02"}).status_code == 200
    assert client.post(f"/crm/public/v1/portal/{TOKEN}/sign", json={"code": "000000"}).status_code == 422
    assert client.post(f"/crm/public/v1/portal/{TOKEN}/sign", json={"code": "654321"}).status_code == 200
    out_res = client.post(f"/crm/public/v1/portal/{TOKEN}/sign-out")
    assert out_res.status_code == 200 and "max-age=0" in out_res.headers["set-cookie"].lower()


def test_the_code_route_passes_the_purpose(public: tuple[TestClient, list]) -> None:
    client, out = public
    client.post(f"/crm/public/v1/portal/{TOKEN}/code", json={"purpose": "sign"})
    client.post(f"/crm/public/v1/portal/{TOKEN}/code", json=["not", "an", "object"])
    purposes = [kw["purpose"] for n, kw in out if n == "portal.request_code"]
    assert purposes == ["sign", ""]
