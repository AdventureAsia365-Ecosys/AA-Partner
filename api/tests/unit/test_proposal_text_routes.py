"""AA-951 routes: every ``/crm/v1`` text proposal, Before you go and template route reaches its helper with the parsed
request; the permission matrix refuses what a role may not do (the assistant previews but never sends; only the owner
writes the template library); a bad format, locale, hash or group key is 422 before any helper runs; the client link
origin comes from INTAKE_PAGE_ORIGINS. The services run on PostgreSQL in tests/integration/test_text_proposal_api.py."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.mail import templates
from app.main import create_app
from app.proposals import builder, prep_packs, text_proposal
from app.routers import proposal_text as router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx

P = "00000000-0000-4000-8000-0000000000c1"
X = "00000000-0000-4000-8000-0000000000c2"
HASH = "a" * 64


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str, **settings_over: Any) -> tuple:
    settings = dataclasses.replace(pools.settings(), **settings_over)
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
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    out: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def stub(name: str) -> Any:
        async def fn(*args: Any, **kwargs: Any) -> Any:
            out.append((name, args, kwargs))
            return {"ok": name}

        return fn

    monkeypatch.setattr(text_proposal, "preview", stub("preview"))
    monkeypatch.setattr(text_proposal, "send", stub("send"))
    monkeypatch.setattr(prep_packs, "for_proposal", stub("for_proposal"))
    monkeypatch.setattr(builder, "set_before_you_go", stub("set_before_you_go"))
    for name in ("library", "create", "update", "delete", "import_starters"):
        monkeypatch.setattr(templates, name, stub(name))
    return out


def test_every_route_reaches_its_helper(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "owner", intake_page_origins=("https://go.example.test",))
    base = f"/crm/v1/proposals/{P}"
    requests = [
        ("get", f"{base}/text?format=whatsapp&locale=vi&template_id={X}", None, "preview", 200),
        ("post", f"{base}/text/send", {"format": "email", "price_hash": HASH, "message_id": X, "intro": "Hi"},
         "send", 200),
        ("get", f"{base}/before-you-go", None, "for_proposal", 200),
        ("put", f"{base}/before-you-go", {"seq": 4, "group_keys": ["prep-pack-nepal"]}, "set_before_you_go", 200),
        ("get", "/crm/v1/message-templates", None, "library", 200),
        ("post", "/crm/v1/message-templates", {"locale": "vi", "title": "T", "body": "B"}, "create", 201),
        ("patch", f"/crm/v1/message-templates/{X}", {"title": "T2"}, "update", 200),
        ("delete", f"/crm/v1/message-templates/{X}", None, "delete", 200),
        ("post", "/crm/v1/message-templates/import-aa", None, "import_starters", 200),
    ]  # fmt: skip
    for method, path, body, helper, status in requests:
        r = getattr(client, method)(path, headers=h, **({"json": body} if body is not None else {}))
        assert r.status_code == status, (path, r.text)
        assert calls[-1][0] == helper, path
    preview = next(c for c in calls if c[0] == "preview")
    assert preview[2] == {"fmt": "whatsapp", "locale": "vi", "template_id": uuid.UUID(X)}
    send = next(c for c in calls if c[0] == "send")[2]
    assert send["fmt"] == "email" and send["price_hash"] == HASH and send["intro"] == "Hi" and send["locale"] is None
    assert send["client_origin"] == "https://go.example.test"
    packs = next(c for c in calls if c[0] == "set_before_you_go")[1]
    assert packs[3:] == (4, ["prep-pack-nepal"])
    assert next(c for c in calls if c[0] == "update")[2] == {"locale": None, "title": "T2", "body": None}


def test_bad_requests_reach_no_helper(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "owner")
    base = f"/crm/v1/proposals/{P}"
    assert client.get(f"{base}/text?format=sms", headers=h).status_code == 422
    assert client.get(f"{base}/text?locale=fr", headers=h).status_code == 422
    for body in ({"format": "email", "price_hash": "xyz"}, {"format": "fax", "price_hash": HASH},
                 {"format": "email", "price_hash": HASH, "extra": 1}):  # fmt: skip
        assert client.post(f"{base}/text/send", headers=h, json=body).status_code == 422
    assert client.put(f"{base}/before-you-go", headers=h, json={"seq": 1, "group_keys": ["Bad Key"]}).status_code == 422
    assert client.put(f"{base}/before-you-go", headers=h, json={"group_keys": []}).status_code == 422
    assert client.post("/crm/v1/message-templates", headers=h, json={"title": "T", "body": ""}).status_code == 422
    assert calls == []


def test_roles(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "assistant")
    base = f"/crm/v1/proposals/{P}"
    assert client.get(f"{base}/text", headers=h).status_code == 200  # every role previews
    assert client.put(f"{base}/before-you-go", headers=h, json={"seq": 1, "group_keys": []}).status_code == 200
    assert (
        client.post(f"{base}/text/send", headers=h, json={"format": "whatsapp", "price_hash": HASH}).status_code == 403
    )
    assert client.get("/crm/v1/message-templates", headers=h).status_code == 200
    assert client.post("/crm/v1/message-templates/import-aa", headers=h).status_code == 403
    client, h = _client(pools, monkeypatch, "agent")
    assert (
        client.post(f"{base}/text/send", headers=h, json={"format": "whatsapp", "price_hash": HASH}).status_code == 200
    )
    assert client.post("/crm/v1/message-templates", headers=h, json={"title": "T", "body": "B"}).status_code == 403
    assert next(c for c in calls if c[0] == "send")[2]["client_origin"] is None  # no client origin configured
