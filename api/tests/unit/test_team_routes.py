"""AA-939 routes: Team & roles, plan, owner recovery (/crm/v1), the invitation claim (/crm/public/v1) and accept, the
owner-recovery operations (/crm/ops/v1). Python branches with fakes; the SQL and triggers run in tests/integration."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import build_directory, build_mailer, create_app
from app.members import invitations as invitation_service
from app.members import recovery as recovery_service
from app.members import team as team_service
from app.members.directory import CognitoDirectory, MemoryDirectory
from app.members.mail import MemoryMailer, SesMailer
from app.routers import invitations as invitations_router
from app.routers import ops as ops_router
from app.routers import team as team_router
from app.settings import Settings
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import OTHER_MEMBER, TENANT, fake_tx, tenant_ctx

ID = "00000000-0000-4000-8000-000000000001"
TOKEN = "t" * 43


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


@pytest.fixture
def app_client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Any]]:
    settings = pools.settings(rate_limit_claim_per_minute=2)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    for module in (team_router, invitations_router, ops_router):
        monkeypatch.setattr(module, "request_pool", _pool)
    with TestClient(app) as client:
        yield client, app


def _agency(pools: LocalPools) -> dict[str, str]:
    return {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}


def _as(monkeypatch: pytest.MonkeyPatch, role: str, conn: ScriptedConn | None = None) -> None:
    monkeypatch.setattr(team_router, "tenant_tx", fake_tx(tenant_ctx(conn or ScriptedConn(), role=role)))


def test_team_routes_reach_their_helpers_as_the_owner(
    app_client: Any, pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = app_client
    _as(monkeypatch, "owner")
    seen: list[str] = []

    def stub(name: str, value: Any) -> Any:
        async def call(*args: Any, **kwargs: Any) -> Any:
            seen.append(name)
            if name == "invite":
                assert kwargs["web_origin"] == "https://partner.local.test" and kwargs["ttl_hours"] == 168
                assert kwargs["email"] == "lan@agency.example" and kwargs["role"] == "agent"
            return value

        return call

    for name in (
        "team",
        "invite",
        "revoke_invitation",
        "change_role",
        "remove_member",
        "reassign_queue",
        "reassign",
        "plan",
    ):
        monkeypatch.setattr(team_service, name, stub(name, {"ok": name}))
    for name in ("banner", "cancel"):
        monkeypatch.setattr(recovery_service, name, stub(name, {"ok": name}))
    h = _agency(pools)
    assert client.get("/crm/v1/team", headers=h).json() == {"ok": "team"}
    assert (
        client.post(
            "/crm/v1/team/invitations", headers=h, json={"email": "lan@agency.example", "role": "agent"}
        ).status_code
        == 201
    )
    assert client.delete(f"/crm/v1/team/invitations/{ID}", headers=h).status_code == 200
    assert client.patch(f"/crm/v1/team/members/{ID}", headers=h, json={"role": "assistant"}).status_code == 200
    assert client.post(f"/crm/v1/team/members/{ID}/remove", headers=h).status_code == 200
    assert client.get("/crm/v1/team/reassign-queue", headers=h).status_code == 200
    assert (
        client.post(
            "/crm/v1/team/reassign", headers=h, json={"from_member_id": ID, "to_member_id": str(OTHER_MEMBER)}
        ).status_code
        == 200
    )
    assert client.get("/crm/v1/plan", headers=h).status_code == 200
    assert client.get("/crm/v1/recovery", headers=h).status_code == 200
    assert client.post(f"/crm/v1/recovery/{ID}/cancel", headers=h).status_code == 200
    assert seen == [
        "team",
        "invite",
        "revoke_invitation",
        "change_role",
        "remove_member",
        "reassign_queue",
        "reassign",
        "plan",
        "banner",
        "cancel",
    ]
    # Bodies are closed: an unknown field or role is refused before any helper runs.
    assert (
        client.post("/crm/v1/team/invitations", headers=h, json={"email": "a@b.io", "role": "admin"}).status_code == 422
    )
    assert client.patch(f"/crm/v1/team/members/{ID}", headers=h, json={"role": "owner", "x": 1}).status_code == 422


@pytest.mark.parametrize("role", ["agent", "assistant"])
def test_r_team_routes_refuse_agents_and_assistants(
    app_client: Any, pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    client, _ = app_client
    _as(monkeypatch, role)
    h = _agency(pools)
    for method, path, body in (
        ("GET", "/crm/v1/team", None),
        ("POST", "/crm/v1/team/invitations", {"email": "a@b.io", "role": "agent"}),
        ("DELETE", f"/crm/v1/team/invitations/{ID}", None),
        ("PATCH", f"/crm/v1/team/members/{ID}", {"role": "owner"}),
        ("POST", f"/crm/v1/team/members/{ID}/remove", None),
        ("GET", "/crm/v1/team/reassign-queue", None),
        ("POST", "/crm/v1/team/reassign", {"from_member_id": ID, "to_member_id": ID}),
        ("GET", "/crm/v1/plan", None),
    ):
        res = client.request(method, path, headers=h, json=body)
        assert (res.status_code, res.json()["detail"]) == (403, "role_forbidden"), path


def test_every_role_sees_and_cancels_the_recovery_banner(
    app_client: Any, pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = app_client
    for role in ("owner", "agent", "assistant"):
        _as(monkeypatch, role, ScriptedConn().on("SET status = 'cancelled'", uuid.UUID(ID)))
        assert client.get("/crm/v1/recovery", headers=_agency(pools)).json() == {"recovery": None}
        assert client.post(f"/crm/v1/recovery/{ID}/cancel", headers=_agency(pools)).json()["status"] == "cancelled"


def test_invite_needs_mail_and_an_origin(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(team_router, "request_pool", _pool)
    _as(monkeypatch, "owner")
    body = {"email": "a@b.io", "role": "agent"}
    for kwargs, detail in (({"mailer": None}, "mail_not_configured"), ({}, "invitations_not_configured")):
        settings = pools.settings(environment="dev", db_ssl="require") if not kwargs else pools.settings()
        app = create_app(settings, verifier=pools.verifier(settings), keys=object(), **kwargs)
        if not kwargs:
            app.state.mailer = MemoryMailer()
        with TestClient(app) as client:
            res = client.post("/crm/v1/team/invitations", headers=_agency(pools), json=body)
        assert (res.status_code, res.json()["detail"]) == (503, detail)


def test_claim_route(app_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, app = app_client
    calls: list[str] = []

    async def claim(pool: Any, keys: Any, directory: Any, token: str) -> Any:
        calls.append(token)
        return invitation_service.Claimed(state="created", email="lan@agency.example", temporary_password="Pw-1")

    monkeypatch.setattr(invitation_service, "claim", claim)
    res = client.post("/crm/public/v1/invitations/claim", json={"token": TOKEN})
    assert res.status_code == 200 and res.json()["temporary_password"] == "Pw-1"
    assert res.headers["cache-control"] == "no-store" and calls == [TOKEN]
    assert client.post("/crm/public/v1/invitations/claim", json={"token": TOKEN, "x": 1}).status_code == 422
    # Two per address per minute in this client (a body refused by validation does not count): the third is refused.
    assert client.post("/crm/public/v1/invitations/claim", json={"token": TOKEN}).status_code == 200
    assert client.post("/crm/public/v1/invitations/claim", json={"token": TOKEN}).status_code == 429
    app.state.claim_limiter.allow = lambda key: True
    app.state.directory = None
    assert (
        client.post("/crm/public/v1/invitations/claim", json={"token": TOKEN}).json()["detail"]
        == "directory_not_configured"
    )


def test_accept_route_needs_a_partner_token_and_no_agency_header(
    app_client: Any, pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = app_client

    async def accept(pool: Any, caller: Any, token: str) -> dict[str, Any]:
        return {"tenant_id": str(TENANT), "member_id": ID, "member_role": "agent", "caller": caller.sub}

    monkeypatch.setattr(invitation_service, "accept", accept)
    res = client.post(
        "/crm/v1/invitations/accept", json={"token": TOKEN}, headers={"Authorization": f"Bearer {pools.partner_web()}"}
    )
    assert res.status_code == 200 and res.json()["member_role"] == "agent"
    assert client.post("/crm/v1/invitations/accept", json={"token": TOKEN}).status_code == 401
    cis = client.post(
        "/crm/v1/invitations/accept", json={"token": TOKEN}, headers={"Authorization": f"Bearer {pools.cis_admin()}"}
    )
    assert cis.status_code == 403


def test_ops_recovery_routes_need_the_platform_admin_group_for_writes(
    app_client: Any, pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = app_client
    seen: list[tuple[str, dict[str, Any]]] = []

    def stub(name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            seen.append((name, kwargs))
            return {"ok": name}

        return call

    for name in ("members", "deactivate_member", "open_request", "complete_request"):
        monkeypatch.setattr(recovery_service, name, stub(name))
    reader = {"Authorization": f"Bearer {pools.cis_admin()}"}
    admin = {"Authorization": f"Bearer {pools.cis_admin(['cis-admin', 'crm-platform-admin'])}"}
    base = f"/crm/ops/v1/tenants/{TENANT}"
    assert client.get(f"{base}/members", headers=reader).json() == {"ok": "members"}
    writes = (
        (f"{base}/members/{ID}/deactivate", None),
        (f"{base}/owner-recovery", {"candidate_member_id": ID, "reason": "owner_left"}),
        (f"{base}/owner-recovery/{ID}/complete", None),
    )
    for path, body in writes:
        assert client.post(path, headers=reader, json=body).json()["detail"] == "missing_group"
        assert client.post(path, headers=admin, json=body).status_code in (200, 201)
    assert [n for n, _ in seen] == ["members", "deactivate_member", "open_request", "complete_request"]
    assert seen[2][1]["reason"] == "owner_left" and seen[2][1]["web_origin"] == "https://partner.local.test"
    assert (
        client.post(
            f"{base}/owner-recovery", headers=admin, json={"candidate_member_id": ID, "reason": "x"}
        ).status_code
        == 422
    )
    # A partner token never reaches the ops class.
    assert client.get(f"{base}/members", headers={"Authorization": f"Bearer {pools.partner_web()}"}).status_code == 403


def test_build_directory_and_mailer(pools: LocalPools) -> None:
    assert isinstance(build_directory(pools.settings()), MemoryDirectory)
    assert build_directory(Settings(environment="dev")) is None
    configured = pools.settings(cognito_partner_pool_id=pools.settings().cognito_partner_issuer.rsplit("/", 1)[1])
    assert isinstance(build_directory(configured), CognitoDirectory)
    assert isinstance(build_mailer(pools.settings()), MemoryMailer)
    assert build_mailer(Settings(environment="dev")) is None
    ses = Settings(
        environment="dev", ses_from_address="no-reply@example.test", ses_configuration_set="aa-email-dev-transactional"
    )
    assert isinstance(build_mailer(ses), SesMailer)
