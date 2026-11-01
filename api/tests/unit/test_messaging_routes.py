"""AA-958 routes: each ``/crm/v1/messaging`` route reaches its helper; managing the connection is owner-only and the
account is read only after that check and before anything is stored; the public Twilio webhook routes answer TwiML,
403 on a bad signature, 404 on an unknown hook, 413 over 64 KiB, and are not held by the per-address public limit
(only unknown hook ids are counted)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.errors import Forbidden, NotFound
from app.main import create_app
from app.messaging import compose, connection, health, webhook
from app.messaging.providers import AuthRevoked
from app.ratelimit import FixedWindowLimiter
from app.routers import messaging as router
from app.routers import public as public_router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.messaging_fakes import ACCOUNT_SID, AUTH_TOKEN, HOOK_ID, FakeTextProvider, TextVault, runtime
from tests.unit.slice_fakes import TENANT, fake_tx, tenant_ctx

ID = "00000000-0000-4000-8000-0000000000e2"
CONNECT = {
    "provider": "sandbox",
    "account_sid": ACCOUNT_SID,
    "auth_token": AUTH_TOKEN,
    "whatsapp_sender": "+61491570156",
}


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str = "owner", provider: Any = None) -> Any:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), messaging_runtime=runtime(provider))
    ctx = tenant_ctx(ScriptedConn(), role=role)
    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", fake_tx(ctx))
    monkeypatch.setattr(router, "Vault", lambda keys, ctx: TextVault())
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return app, headers


def _stub(calls: list[tuple[str, Any]], name: str) -> Any:
    async def fn(*args: Any, **kwargs: Any) -> Any:
        calls.append((name, kwargs))
        return {"ok": name}

    return fn


ROUTES = [
    ("get", "/crm/v1/messaging", None, (connection, "view")),
    ("post", "/crm/v1/messaging/test", {"channel": "sms", "to": "+61491570110"}, (health, "test_connection")),
    ("post", "/crm/v1/messaging/templates/refresh", None, (connection, "queue_template_sync")),
    ("delete", "/crm/v1/messaging/connection", None, (connection, "disconnect")),
    ("get", f"/crm/v1/messaging/compose?conversation_id={ID}", None, (compose, "compose_state")),
    ("post", "/crm/v1/messaging/messages", {"message_id": ID, "conversation_id": ID, "body": "Hi"}, (compose, "send")),
]


@pytest.mark.parametrize(("method", "path", "body", "target"), ROUTES)
def test_each_route_reaches_its_helper(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any, target: Any
) -> None:
    app, headers = _client(pools, monkeypatch)
    calls: list[tuple[str, Any]] = []
    module, name = target
    monkeypatch.setattr(module, name, _stub(calls, name))
    with TestClient(app) as client:
        response = client.request(method.upper(), path, json=body, headers=headers)
    assert response.status_code in (200, 202), response.text
    assert name in [c[0] for c in calls]


def test_managing_the_connection_is_owner_only(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeTextProvider()
    app, headers = _client(pools, monkeypatch, role="agent", provider=provider)
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(connection, "view", _stub(calls, "view"))
    with TestClient(app) as client:
        assert client.put("/crm/v1/messaging/connection", json=CONNECT, headers=headers).status_code == 403
        assert client.delete("/crm/v1/messaging/connection", headers=headers).status_code == 403
        assert client.post("/crm/v1/messaging/templates/refresh", headers=headers).status_code == 403
        assert client.get("/crm/v1/messaging", headers=headers).status_code == 200
    assert provider.checked == 0


def test_the_account_is_read_before_storing(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(connection, "connect", _stub(calls, "connect"))
    app, headers = _client(pools, monkeypatch, provider=FakeTextProvider(check_error=AuthRevoked("auth_revoked")))
    with TestClient(app) as client:
        refused = client.put("/crm/v1/messaging/connection", json=CONNECT, headers=headers)
    assert refused.status_code == 422 and refused.json()["detail"] == "messaging_auth_revoked" and calls == []
    app, headers = _client(pools, monkeypatch)
    with TestClient(app) as client:
        bad = client.put(
            "/crm/v1/messaging/connection", json=CONNECT | {"account_sid": "AC" + "x" * 32}, headers=headers
        )
        assert bad.status_code == 422 and bad.json()["detail"] == "invalid_account_sid"
        assert client.put("/crm/v1/messaging/connection", json=CONNECT, headers=headers).status_code == 200
    assert calls[0][1]["provider"] == "sandbox" and calls[0][1]["whatsapp_sender"] == "+61491570156"


def test_a_non_member_never_gets_the_account_read(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeTextProvider()

    @asynccontextmanager
    async def not_a_member(*_: Any, **__: Any):  # noqa: ANN202
        raise Forbidden("not_a_member")
        yield  # pragma: no cover

    app, headers = _client(pools, monkeypatch, provider=provider)
    monkeypatch.setattr(router, "tenant_tx", not_a_member)
    with TestClient(app) as client:
        assert client.put("/crm/v1/messaging/connection", json=CONNECT, headers=headers).status_code == 403
    assert provider.checked == 0


def test_no_runtime_answers_503(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), messaging_runtime=None)
    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", fake_tx(tenant_ctx()))
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    with TestClient(app) as client:
        assert client.get("/crm/v1/messaging", headers=headers).status_code == 503
        hook = client.post(f"/crm/public/v1/hooks/twilio/{HOOK_ID}", content=b"a=b")
        assert hook.status_code == 503


def test_public_webhook_routes(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), messaging_runtime=runtime())
    monkeypatch.setattr(public_router, "request_pool", _pool)
    seen: list[Any] = []

    async def handle(_pool: Any, _runtime: Any, request: webhook.HookRequest) -> str:
        seen.append(request)
        if request.signature == "bad":
            raise Forbidden("invalid_signature")
        if request.hook_id == "f" * 32:
            raise NotFound("hook_not_found")
        return "stored"

    monkeypatch.setattr(webhook, "handle", handle)
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    with TestClient(app) as client:
        ok = client.post(
            f"/crm/public/v1/hooks/twilio/{HOOK_ID}",
            content=b"Body=Hi+there&From=whatsapp%3A%2B61491570110&Body=again",
            headers=form | {"X-Twilio-Signature": "sig"},
        )
        assert ok.status_code == 200 and ok.headers["content-type"].startswith("text/xml")
        assert ok.text.endswith("<Response></Response>")
        assert seen[0].params == [("Body", "Hi there"), ("From", "whatsapp:+61491570110"), ("Body", "again")]
        assert (seen[0].kind, seen[0].signature) == ("inbound", "sig")
        status = client.post(f"/crm/public/v1/hooks/twilio/{HOOK_ID}/status", content=b"MessageStatus=read")
        assert status.status_code == 200 and seen[1].kind == "status" and seen[1].signature is None
        bad = client.post(
            f"/crm/public/v1/hooks/twilio/{HOOK_ID}", content=b"a=b", headers={"X-Twilio-Signature": "bad"}
        )
        assert bad.status_code == 403
        assert client.post(f"/crm/public/v1/hooks/twilio/{'f' * 32}", content=b"a=b").status_code == 404
        assert client.post("/crm/public/v1/hooks/twilio/NOT-A-HOOK", content=b"a=b").status_code == 422
        big = client.post(f"/crm/public/v1/hooks/twilio/{HOOK_ID}", content=b"a=" + b"x" * (64 * 1024))
        assert big.status_code == 413
        assert client.post(f"/crm/public/v1/hooks/twilio/{HOOK_ID}", content=b"\xff\xfe").status_code == 400


def test_webhooks_skip_the_per_address_public_limit(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    """Twilio posts every agency's callbacks from one shared pool of addresses: signed callbacks are never counted
    (a 429 loses an inbound message for good); only unknown hook ids are, and an address over that ceiling is 429
    before any work."""
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), messaging_runtime=runtime())
    monkeypatch.setattr(public_router, "request_pool", _pool)
    handled: list[str] = []

    async def handle(_pool: Any, _runtime: Any, request: webhook.HookRequest) -> str:
        handled.append(request.hook_id)
        if request.hook_id == "f" * 32:
            raise NotFound("hook_not_found")
        return "stored"

    monkeypatch.setattr(webhook, "handle", handle)
    with TestClient(app) as client:
        app.state.public_limiter = FixedWindowLimiter(1)
        app.state.hook_miss_limiter = FixedWindowLimiter(2)
        for _ in range(5):  # far over the public limit of 1: every signed callback still goes through
            assert client.post(f"/crm/public/v1/hooks/twilio/{HOOK_ID}/status", content=b"a=b").status_code == 200
        assert client.post(f"/crm/public/v1/hooks/twilio/{'f' * 32}", content=b"a=b").status_code == 404
        assert client.post(f"/crm/public/v1/hooks/twilio/{'f' * 32}", content=b"a=b").status_code == 404
        before = len(handled)
        miss = client.post(f"/crm/public/v1/hooks/twilio/{'f' * 32}", content=b"a=b")
        assert miss.status_code == 429 and miss.headers["Retry-After"] == "60"
        assert client.post(f"/crm/public/v1/hooks/twilio/{HOOK_ID}", content=b"a=b").status_code == 429
        assert len(handled) == before  # refused before any lookup
