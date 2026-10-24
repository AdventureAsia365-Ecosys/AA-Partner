"""AA-942 ``/crm/v1`` lifecycle routes: each route reaches its helper with the parsed body, owner-only routes refuse
other roles, erasure and the offboarding erase honour DRY RUN (the helpers run on PostgreSQL in the integration
tests)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.lifecycle import erase, offboarding, policy, trash
from app.main import create_app
from app.routers import lifecycle as router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx

ID = "00000000-0000-4000-8000-0000000000e1"


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str = "owner", dry_run: bool = True) -> Any:
    settings = pools.settings(lifecycle_dry_run=dry_run)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    conn = ScriptedConn()
    ctx = tenant_ctx(conn, role=role)
    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", fake_tx(ctx))
    monkeypatch.setattr(router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return app, headers, conn, ctx


@pytest.fixture
def owner(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, dict[str, str], Any]]:
    app, headers, conn, ctx = _client(pools, monkeypatch)
    with TestClient(app) as c:
        yield c, headers, conn


def _stub(calls: list[tuple[str, Any]], name: str, result: Any = None) -> Any:
    async def fn(*args: Any, **kwargs: Any) -> Any:
        calls.append((name, kwargs or args[1:]))
        return result if result is not None else {"ok": name}

    return fn


def test_every_route_reaches_its_helper(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers, conn = owner
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(policy, "settings", _stub(calls, "settings", {"trash_days": 7, "auto_archive_months": 36}))
    for name in ("update_settings", "place_hold", "release_hold", "archive", "unarchive"):
        monkeypatch.setattr(policy, name, _stub(calls, name))
    monkeypatch.setattr(policy, "holds", _stub(calls, "holds", []))
    for name in ("list_trash", "restore", "move_to_trash"):
        monkeypatch.setattr(trash, name, _stub(calls, name))
    for name in ("request", "cancel", "confirm_erase", "accept"):
        monkeypatch.setattr(offboarding, name, _stub(calls, name))
    monkeypatch.setattr(offboarding, "latest", _stub(calls, "latest", {"status": "requested"}))
    monkeypatch.setattr(offboarding, "download", _stub(calls, "download", b'{"format": 1}\n'))

    got = client.get("/crm/v1/lifecycle", headers=headers).json()
    assert got["settings"]["trash_days"] == 7 and got["deletion_dry_run"] is True and "holds" in got
    assert {r["code"] for r in got["rules"]} >= {"trash", "llm_calls"} and "price" in got["lost_reasons"]
    assert client.patch("/crm/v1/lifecycle/settings", json={"trash_days": 3}, headers=headers).status_code == 200
    assert client.patch("/crm/v1/lifecycle/settings", json={"trash_days": 99}, headers=headers).status_code == 422
    body = {"scope": "trip", "subject_id": ID, "reason_code": "dispute"}
    assert client.post("/crm/v1/lifecycle/holds", json=body, headers=headers).status_code == 201
    assert client.post(f"/crm/v1/lifecycle/holds/{ID}/release", headers=headers).status_code == 200
    assert client.get("/crm/v1/trash", headers=headers).json() == {"ok": "list_trash"}
    assert client.post(f"/crm/v1/trash/trip/{ID}/restore", headers=headers).json() == {"ok": "restore"}
    assert client.post(f"/crm/v1/trash/audit_event/{ID}/restore", headers=headers).status_code == 422
    for path, kind in (("trips", "trip"), ("travelers", "traveler"), ("tasks", "task"), ("notes", "note")):
        assert client.delete(f"/crm/v1/{path}/{ID}", headers=headers).json() == {"ok": "move_to_trash"}
        assert calls[-1][1][0] == kind
    assert (
        client.post(f"/crm/v1/travelers/{ID}/archive", json={"reason": "duplicate"}, headers=headers).status_code == 200
    )
    assert (
        client.post(f"/crm/v1/travelers/{ID}/archive", json={"reason": "auto_inactive"}, headers=headers).status_code
        == 422
    )
    assert client.post(f"/crm/v1/travelers/{ID}/unarchive", headers=headers).status_code == 200
    assert client.get("/crm/v1/lifecycle/offboarding", headers=headers).json() == {
        "offboarding": {"status": "requested"}
    }
    assert (
        client.post("/crm/v1/lifecycle/offboarding", json={"export_waived": True}, headers=headers).status_code == 201
    )
    assert client.post(f"/crm/v1/lifecycle/offboarding/{ID}/cancel", headers=headers).status_code == 200
    export = client.get(f"/crm/v1/lifecycle/offboarding/{ID}/export", headers=headers)
    assert (
        export.headers["content-type"] == "application/x-ndjson"
        and "attachment" in export.headers["content-disposition"]
    )
    erase_body = {"confirm_tenant_id": str(TENANT)}
    assert client.post(f"/crm/v1/lifecycle/offboarding/{ID}/erase", json=erase_body, headers=headers).status_code == 200
    assert [c for c in calls if c[0] == "confirm_erase"][0][1]["dry_run"] is True
    # AA-941: the owner confirms a request AA raised.
    accept = client.post(f"/crm/v1/lifecycle/offboarding/{ID}/accept", json={"export_waived": True}, headers=headers)
    assert accept.status_code == 200 and [c for c in calls if c[0] == "accept"][0][1] == {"export_waived": True}
    assert client.post(f"/crm/v1/lifecycle/offboarding/{ID}/accept", json={"x": 1}, headers=headers).status_code == 422


def test_non_owners_see_rules_only_and_cannot_erase(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    app, headers, conn, ctx = _client(pools, monkeypatch, role="agent")
    with TestClient(app) as client:
        got = client.get("/crm/v1/lifecycle", headers=headers).json()
        assert "holds" not in got and got["role"] == "agent"
        # AA-939 matrix: client_delete and agency_export are owner-only, refused by tenant_tx(action=) (role_forbidden).
        for res in (
            client.post(f"/crm/v1/travelers/{ID}/erasure", json={}, headers=headers),
            client.get("/crm/v1/lifecycle/offboarding", headers=headers),
            client.post(f"/crm/v1/lifecycle/offboarding/{ID}/accept", json={}, headers=headers),
            client.patch("/crm/v1/lifecycle/settings", json={"trash_days": 3}, headers=headers),
            client.post("/crm/v1/lifecycle/holds", json={"scope": "tenant", "reason_code": "other"}, headers=headers),
        ):
            assert (res.status_code, res.json()["detail"]) == (403, "role_forbidden")


def test_erasure_dry_run_then_real(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []

    async def fake_erase(ctx: Any, scope: str, subject: uuid.UUID, *, dry_run: bool, **_: Any) -> dict[str, int]:
        calls.append((scope, dry_run))
        return {"traveler": 1}

    monkeypatch.setattr(erase, "erase", fake_erase)
    app, headers, conn, ctx = _client(pools, monkeypatch, dry_run=True)
    with TestClient(app) as client:
        assert client.post(f"/crm/v1/travelers/{ID}/erasure", json={}, headers=headers).json() == {
            "traveler_id": ID,
            "dry_run": True,
            "counts": {"traveler": 1},
        }
        refused = client.post(f"/crm/v1/travelers/{ID}/erasure", json={"dry_run": False}, headers=headers)
        assert refused.status_code == 409 and refused.json()["detail"] == "deletion_dry_run"
    app, headers, conn, ctx = _client(pools, monkeypatch, dry_run=False)
    conn.on("INSERT INTO crm.erasure_request", uuid.UUID(ID))
    with TestClient(app) as client:
        done = client.post(f"/crm/v1/travelers/{ID}/erasure", json={"dry_run": False}, headers=headers).json()
    assert done["dry_run"] is False and done["request_id"] == ID and done["backup_purge_by"]
    assert calls[-2:] == [("traveler", True), ("traveler", False)]
    assert ctx.audit_events[-1].reason == "bypass_trash"


def test_the_owner_view_lists_runs_and_archived_clients(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    import datetime as dt

    from tests.unit.slice_fakes import enc

    now = dt.datetime(2026, 10, 24, tzinfo=dt.UTC)
    app, headers, conn, ctx = _client(pools, monkeypatch)
    conn.on(
        "FROM crm.lifecycle_run",
        [{"kind": "crm_lifecycle", "dry_run": True, "counts": '{"trash_trip_purged": 1}', "created_at": now}],
    )
    conn.on(
        "archive_reason = 'auto_inactive'",
        [
            {
                "traveler_id": uuid.UUID(ID),
                "display_name_enc": enc("traveler", "display_name_enc", "Hoa"),
                "archived_at": now,
            }
        ],
    )
    with TestClient(app) as client:
        got = client.get("/crm/v1/lifecycle", headers=headers).json()
    assert got["runs"][0]["counts"] == {"trash_trip_purged": 1}
    assert got["archived_inactive"] == [{"traveler_id": ID, "display_name": "Hoa", "archived_at": now.isoformat()}]
    assert ctx.audit_events[-1].reason == "archived_inactive"
