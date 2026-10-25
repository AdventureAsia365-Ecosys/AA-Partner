"""AA-945 ``/crm/v1`` mail routes: each route reaches its helper, agency-level mailbox actions are owner-only, the
SMTP login is checked before anything is stored, the history upload is capped (413). The helpers run on scripted SQL
in the other mail tests and on PostgreSQL in the integration tests."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.errors import Forbidden
from app.mail import conversations, health, history_import, mailbox, oauth
from app.mail.providers import AuthRevoked
from app.mail.state import MailRuntime
from app.mail.store import MemoryIngestStore
from app.main import create_app
from app.routers import mail as router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.mail_fakes import FakeProvider, MailVault
from tests.unit.slice_fakes import TENANT, fake_tx, tenant_ctx

ID = "00000000-0000-4000-8000-0000000000e1"


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str = "owner", provider: Any = None) -> Any:
    settings = pools.settings()
    runtime = MailRuntime(keys=object(), providers={"smtp": provider or FakeProvider()})  # type: ignore[arg-type]
    app = create_app(
        settings,
        verifier=pools.verifier(settings),
        keys=object(),
        mail_runtime=runtime,
        ingest_store=MemoryIngestStore(),
    )
    ctx = tenant_ctx(ScriptedConn(), role=role)
    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", fake_tx(ctx))
    monkeypatch.setattr(router, "Vault", lambda keys, ctx: MailVault())
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return app, headers


def _stub(calls: list[tuple[str, Any]], name: str) -> Any:
    async def fn(*args: Any, **kwargs: Any) -> Any:
        calls.append((name, kwargs))
        if name == "complete":
            return oauth.Completed(provider="google", scope=kwargs.get("scope", "own"), secret={"address": "a@b.test"})
        if name == "mailbox_id":
            return uuid.UUID(ID)
        return {"ok": name}

    return fn


ROUTES = [
    ("get", "/crm/v1/mailbox", None, (mailbox, "view")),
    ("post", "/crm/v1/mailbox/oauth/start", {"provider": "google"}, (oauth, "start")),
    ("post", "/crm/v1/mailbox/test", {"scope": "own"}, (health, "test_mailbox")),
    ("delete", "/crm/v1/mailbox?scope=own", None, (mailbox, "disconnect")),
    ("get", "/crm/v1/mailbox/ingest-address", None, (mailbox, "ingest_address")),
    ("get", f"/crm/v1/conversations?trip_id={ID}", None, (conversations, "list_conversations")),
    ("get", f"/crm/v1/conversations/{ID}/messages", None, (conversations, "conversation_messages")),
    ("post", f"/crm/v1/conversations/{ID}/link", {"traveler_id": ID}, (conversations, "link")),
    ("put", f"/crm/v1/messages/drafts/{ID}", {"trip_id": ID, "body": "Hi"}, (conversations, "save_draft")),
    ("delete", f"/crm/v1/messages/drafts/{ID}", None, (conversations, "discard_draft")),
    ("post", "/crm/v1/messages", {"message_id": ID, "conversation_id": ID, "body": "Hi"}, (conversations, "send")),
    ("post", f"/crm/v1/messages/{ID}/undo", None, (conversations, "undo")),
    ("post", f"/crm/v1/messages/{ID}/edit", None, (conversations, "back_to_draft")),
    ("get", "/crm/v1/saved-replies", None, (conversations, "saved_replies")),
    ("post", "/crm/v1/saved-replies", {"title": "T", "body": "B"}, (conversations, "create_reply")),
    ("patch", f"/crm/v1/saved-replies/{ID}", {"title": "T"}, (conversations, "update_reply")),
    ("delete", f"/crm/v1/saved-replies/{ID}", None, (conversations, "delete_reply")),
    ("get", f"/crm/v1/mail/imports/{ID}", None, (history_import, "import_status")),
]


@pytest.mark.parametrize(("method", "path", "body", "target"), ROUTES)
def test_each_route_reaches_its_helper(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any, target: Any
) -> None:
    app, headers = _client(pools, monkeypatch)
    calls: list[tuple[str, Any]] = []
    module, name = target
    monkeypatch.setattr(module, name, _stub(calls, name))
    monkeypatch.setattr(mailbox, "mailbox_id", _stub(calls, "mailbox_id"))
    with TestClient(app) as client:
        response = client.request(method.upper(), path, json=body, headers=headers)
    assert response.status_code in (200, 201, 202), response.text
    assert name in [c[0] for c in calls]


def test_senders_table_and_agency_scope_is_owner_only(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    app, headers = _client(pools, monkeypatch, role="agent")
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(oauth, "start", _stub(calls, "start"))
    with TestClient(app) as client:
        assert client.get("/crm/v1/mail/senders", headers=headers).json()["senders"][0]["path"] == "client_message"
        refused = client.post(
            "/crm/v1/mailbox/oauth/start", json={"provider": "google", "scope": "agency"}, headers=headers
        )
        assert refused.status_code == 403
        assert client.delete("/crm/v1/mailbox?scope=agency", headers=headers).status_code == 403
        assert (
            client.post("/crm/v1/mailbox/oauth/start", json={"provider": "google"}, headers=headers).status_code == 200
        )


def test_oauth_complete_checks_the_sealed_scope(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    app, headers = _client(pools, monkeypatch, role="agent")
    calls: list[tuple[str, Any]] = []

    async def complete(*_: Any, **__: Any) -> Any:
        return oauth.Completed(provider="google", scope="agency", secret={"address": "a@b.test"})

    monkeypatch.setattr(oauth, "complete", complete)
    monkeypatch.setattr(mailbox, "connect", _stub(calls, "connect"))
    with TestClient(app) as client:
        response = client.post("/crm/v1/mailbox/oauth/complete", json={"code": "c", "state": "s"}, headers=headers)
    assert response.status_code == 403 and calls == []


def test_smtp_login_is_proved_before_storing(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "host": "smtp.x.test",
        "port": 587,
        "security": "starttls",
        "username": "u",
        "password": "p",
        "address": "Hello@X.test",
    }
    calls: list[tuple[str, Any]] = []
    app, headers = _client(pools, monkeypatch, provider=FakeProvider(error=AuthRevoked("auth_revoked")))
    monkeypatch.setattr(mailbox, "connect", _stub(calls, "connect"))
    with TestClient(app) as client:
        assert client.put("/crm/v1/mailbox/smtp", json=body, headers=headers).json()["detail"] == "smtp_auth_revoked"
    assert calls == []
    app, headers = _client(pools, monkeypatch)
    with TestClient(app) as client:
        assert client.put("/crm/v1/mailbox/smtp", json=body, headers=headers).status_code == 200
    assert calls[0][1]["secret"]["address"] == "hello@x.test" and calls[0][1]["provider"] == "smtp"


def test_history_upload_is_capped(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    app, headers = _client(pools, monkeypatch)
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(history_import, "create_import", _stub(calls, "create_import"))
    monkeypatch.setattr(history_import, "MAX_BYTES", 10)
    with TestClient(app) as client:
        ok = client.post("/crm/v1/mail/imports?source_kind=csv", content=b"a,b\n", headers=headers)
        assert ok.status_code == 202 and calls[0][1]["data"] == b"a,b\n"
        big = client.post("/crm/v1/mail/imports?source_kind=mbox", content=b"x" * 11, headers=headers)
        assert big.status_code == 413 and big.json()["detail"] == "import_too_large"


SMTP_BODY = {
    "host": "smtp.x.test",
    "port": 587,
    "security": "starttls",
    "username": "u",
    "password": "p",
    "address": "hello@x.test",
}


def test_smtp_check_never_runs_before_authorisation(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review S250 blocker: the SMTP check dials a host the caller names, so it must not run for an agent asking for
    the agency mailbox or for someone who is not a member of the agency (no port scan / SMTP-login oracle)."""
    provider = FakeProvider()
    app, headers = _client(pools, monkeypatch, role="agent", provider=provider)
    with TestClient(app) as client:
        refused = client.put("/crm/v1/mailbox/smtp", json=SMTP_BODY | {"scope": "agency"}, headers=headers)
    assert refused.status_code == 403 and provider.checked == 0

    @asynccontextmanager
    async def not_a_member(*_: Any, **__: Any):  # noqa: ANN202
        raise Forbidden("not_a_member")
        yield  # pragma: no cover

    app, headers = _client(pools, monkeypatch, provider=provider)
    monkeypatch.setattr(router, "tenant_tx", not_a_member)
    with TestClient(app) as client:
        refused = client.put("/crm/v1/mailbox/smtp", json=SMTP_BODY, headers=headers)
    assert refused.status_code == 403 and provider.checked == 0
