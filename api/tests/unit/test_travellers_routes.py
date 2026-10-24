"""AA-937 ``/crm/v1`` traveller, party and document routes: each reaches its helper with the validated body, refuses
foreign tokens (route class AGENCY), and needs a reason for a reveal or a download (SQL and RLS: integration tests)."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.documents import service as documents
from app.main import create_app
from app.routers import pipeline as pipeline_router
from app.routers import travellers as travellers_router
from app.travellers import service
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx

T1 = "00000000-0000-4000-8000-0000000000f1"
T2 = "00000000-0000-4000-8000-0000000000f2"
TRIP = "00000000-0000-4000-8000-0000000000e1"


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


@pytest.fixture
def client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    for module in (travellers_router, pipeline_router):
        monkeypatch.setattr(module, "request_pool", _pool)
        monkeypatch.setattr(module, "tenant_tx", fake_tx(tenant_ctx(ScriptedConn())))
        monkeypatch.setattr(module, "Vault", lambda keys, ctx: FakeVault())
        monkeypatch.setattr(module, "require_keys", lambda keys: keys)
    with TestClient(app) as c:
        yield c


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-AA-Tenant-Id": str(TENANT)}


def test_routes_reach_their_helpers(client: TestClient, pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []

    def stub(name: str) -> Any:
        async def fn(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append((name, kwargs or args[-1]))
            return {"ok": name}

        return fn

    for name in (
        "directory",
        "create_traveler",
        "profile",
        "update_traveler",
        "update_profile",
        "set_health",
        "add_relation",
        "remove_relation",
        "add_to_trip",
        "remove_from_trip",
    ):
        monkeypatch.setattr(service, name, stub(name))
    for name in ("trip_documents", "upload", "download", "set_visibility", "trash"):
        monkeypatch.setattr(documents, name, stub(name))
    monkeypatch.setattr(travellers_router, "reveal", stub("reveal"))
    monkeypatch.setattr(pipeline_router, "update_trip", stub("update_trip"))
    h = _headers(pools.partner_web())
    pdf = base64.b64encode(b"%PDF-1.7 synthetic").decode()
    ok = [
        client.get("/crm/v1/travelers?owner=me&page=2", headers=h),
        client.post("/crm/v1/travelers/search", json={"q": "anna", "owner": "me", "page": 2}, headers=h),
        client.post("/crm/v1/travelers", json={"display_name": "Lily Martin"}, headers=h),
        client.get(f"/crm/v1/travelers/{T1}", headers=h),
        client.patch(f"/crm/v1/travelers/{T1}", json={"dob": "1990-04-03"}, headers=h),
        client.patch(f"/crm/v1/travelers/{T1}/profile", json={"fitness_band": "4_6h"}, headers=h),
        client.put(f"/crm/v1/travelers/{T1}/health", json={"dietary": ["vegan"], "consent_recorded": True}, headers=h),
        client.post(
            f"/crm/v1/travelers/{T1}/reveal", json={"field": "passport_no", "reason": "prepare_booking"}, headers=h
        ),
        client.post(f"/crm/v1/travelers/{T1}/relations", json={"to_traveler_id": T2, "kind": "spouse"}, headers=h),
        client.delete(f"/crm/v1/travelers/{T1}/relations/{T2}", headers=h),
        client.post(f"/crm/v1/trips/{TRIP}/travelers", json={"new": {"display_name": "Lily"}}, headers=h),
        client.delete(f"/crm/v1/trips/{TRIP}/travelers/{T1}", headers=h),
        client.get(f"/crm/v1/trips/{TRIP}/documents", headers=h),
        client.post(
            "/crm/v1/documents",
            json={
                "trip_id": TRIP,
                "kind": "ticket",
                "filename": "t.pdf",
                "content_type": "application/pdf",
                "content_b64": pdf,
            },
            headers=h,
        ),
        client.post(f"/crm/v1/documents/{T1}/download", json={"reason": "other"}, headers=h),
        client.patch(f"/crm/v1/documents/{T1}", json={"visible_to_travelers": True}, headers=h),
        client.delete(f"/crm/v1/documents/{T1}", headers=h),
        client.patch(f"/crm/v1/trips/{TRIP}", json={"start_date": "2027-03-12"}, headers=h),
    ]
    assert [r.status_code for r in ok] == [
        200,
        200,
        201,
        200,
        200,
        200,
        200,
        200,
        201,
        200,
        201,
        200,
        200,
        201,
        200,
        200,
        200,
        200,
    ]
    assert [c[0] for c in calls] == [
        "directory",
        "directory",
        "create_traveler",
        "profile",
        "update_traveler",
        "update_profile",
        "set_health",
        "reveal",
        "add_relation",
        "remove_relation",
        "add_to_trip",
        "remove_from_trip",
        "trip_documents",
        "upload",
        "download",
        "set_visibility",
        "trash",
        "update_trip",
    ]
    assert calls[0][1] == {"archived": False, "owner": "me", "page": 2}
    assert calls[1][1] == {"q": "anna", "archived": False, "owner": "me", "page": 2}
    assert calls[4][1] == {"dob": "1990-04-03"}  # only the fields sent (exclude_unset)
    assert calls[13][1]["content"] == b"%PDF-1.7 synthetic"


def test_routes_validate_bodies(client: TestClient, pools: LocalPools) -> None:
    h = _headers(pools.partner_web())
    for method, path, body in (
        ("post", f"/crm/v1/travelers/{T1}/reveal", {"field": "email"}),  # no reason
        ("post", f"/crm/v1/travelers/{T1}/reveal", {"field": "email", "reason": "curious"}),
        ("post", f"/crm/v1/travelers/{T1}/reveal", {"field": "display_name_enc", "reason": "other"}),
        ("post", f"/crm/v1/travelers/{T1}/relations", {"to_traveler_id": T2, "kind": "cousin"}),
        ("post", "/crm/v1/travelers", {"display_name": "A", "margin": 1}),
        ("patch", f"/crm/v1/travelers/{T1}", {"profile_token": "x"}),
        ("post", f"/crm/v1/documents/{T1}/download", {}),
        (
            "post",
            "/crm/v1/documents",
            {"kind": "selfie", "filename": "a", "content_type": "image/png", "content_b64": "xxxx"},
        ),
    ):
        assert getattr(client, method)(path, json=body, headers=h).status_code == 422, (path, body)
    assert client.get("/crm/v1/travelers?page=0", headers=h).status_code == 422
    # The search term never travels in a URL: GET ?q= is refused, the POST body carries it.
    refused = client.get("/crm/v1/travelers?q=anna%40example.test", headers=h)
    assert refused.status_code == 422 and refused.json()["detail"] == "search_by_post"
    for body in ({}, {"q": ""}, {"q": "a", "page": 0}, {"q": "a", "extra": 1}):
        assert client.post("/crm/v1/travelers/search", json=body, headers=h).status_code == 422, body


def test_foreign_tokens_are_refused(client: TestClient, pools: LocalPools) -> None:
    assert client.get("/crm/v1/travelers", headers={"X-AA-Tenant-Id": str(TENANT)}).status_code == 401
    for token in (pools.cis_admin(), pools.breakglass()):
        for path in ("/crm/v1/travelers", f"/crm/v1/travelers/{T1}", f"/crm/v1/trips/{TRIP}/documents"):
            assert client.get(path, headers=_headers(token)).status_code == 403
    res = client.post(
        f"/crm/v1/travelers/{T1}/reveal",
        json={"field": "email", "reason": "other"},
        headers=_headers(pools.cis_admin()),
    )
    assert res.status_code == 403
    assert uuid.UUID(T1)
