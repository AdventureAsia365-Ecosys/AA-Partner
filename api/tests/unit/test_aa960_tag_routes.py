"""AA-960 (CRM-29b) ``/crm/v1`` tag routes, the ready-to-travel list and the workflow coverage: each reaches its helper
with the validated body, and the role matrix refuses before any read (SQL and RLS: integration tests). Also the tag
trigger of the workflow engine (:func:`app.workflows.triggers.on_tag_added`, card R)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.routers import tags as tags_router
from app.routers import workflows as workflows_router
from app.tags import ready, service
from app.workflows import coverage, triggers
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, system_ctx, tenant_ctx

TAG = "00000000-0000-4000-8000-00000000a960"
TRIP = "00000000-0000-4000-8000-0000000000e1"
CLIENT = "00000000-0000-4000-8000-0000000000f1"


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> TestClient:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    for module in (tags_router, workflows_router):
        monkeypatch.setattr(module, "request_pool", _pool)
        monkeypatch.setattr(module, "tenant_tx", fake_tx(tenant_ctx(ScriptedConn(), role=role)))
    monkeypatch.setattr(tags_router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(tags_router, "require_keys", lambda keys: keys)
    return TestClient(app)


@pytest.fixture
def client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(pools, monkeypatch, "owner") as c:
        yield c


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-AA-Tenant-Id": str(TENANT)}


def _stubs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    calls: list[tuple[str, Any]] = []

    def stub(name: str) -> Any:
        async def fn(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append((name, {"args": args[1:], **kwargs}))
            return {"ok": name}

        return fn

    for name in ("vocabulary", "create", "update", "delete", "bulk", "of_subject", "put", "remove"):
        monkeypatch.setattr(service, name, stub(name))
    monkeypatch.setattr(ready, "listing", stub("ready"))
    monkeypatch.setattr(coverage, "coverage", stub("coverage"))
    return calls


def test_routes_reach_their_helpers(client: TestClient, pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stubs(monkeypatch)
    h = _headers(pools.partner_web())
    responses = [
        client.get("/crm/v1/tags?applies_to=client&archived=true", headers=h),
        client.post("/crm/v1/tags", json={"applies_to": "trip", "name": "VIP", "tone": "purple"}, headers=h),
        client.patch(f"/crm/v1/tags/{TAG}", json={"name": "Gold", "archived": True}, headers=h),
        client.delete(f"/crm/v1/tags/{TAG}", headers=h),
        client.post(
            f"/crm/v1/tags/{TAG}/bulk",
            json={"action": "add", "filter": {"stage": "proposal", "owner": "me"}, "dry_run": False, "expected": 3},
            headers=h,
        ),  # fmt: skip
        client.get(f"/crm/v1/trips/{TRIP}/tags", headers=h),
        client.put(f"/crm/v1/trips/{TRIP}/tags/{TAG}", headers=h),
        client.delete(f"/crm/v1/trips/{TRIP}/tags/{TAG}", headers=h),
        client.get(f"/crm/v1/travelers/{CLIENT}/tags", headers=h),
        client.put(f"/crm/v1/travelers/{CLIENT}/tags/{TAG}", headers=h),
        client.delete(f"/crm/v1/travelers/{CLIENT}/tags/{TAG}", headers=h),
        client.get("/crm/v1/marketing/ready-to-travel?owner=me&page=2", headers=h),
        client.get("/crm/v1/workflows/coverage", headers=h),
    ]
    assert [r.status_code for r in responses] == [200, 201] + [200] * 11
    names = [c[0] for c in calls]
    assert names == ["vocabulary", "create", "update", "delete", "bulk", "of_subject", "put", "remove", "of_subject",
                     "put", "remove", "ready", "coverage"]  # fmt: skip
    assert calls[0][1] == {"args": (), "applies_to": "client", "archived": True}
    assert calls[1][1]["args"] == ("trip", "VIP", "purple")
    assert calls[2][1]["name"] == "Gold" and calls[2][1]["archived"] is True and calls[2][1]["tone"] is None
    bulk = calls[4][1]
    assert bulk["action"] == "add" and bulk["dry_run"] is False and bulk["expected"] == 3
    assert bulk["selection"] == service.Selection(stage="proposal", owner="me")
    assert calls[6][1]["trip_id"] == uuid.UUID(TRIP) and calls[9][1]["traveler_id"] == uuid.UUID(CLIENT)
    assert calls[11][1]["owner"] == "me" and calls[11][1]["page"] == 2
    bad = [
        client.post("/crm/v1/tags", json={"applies_to": "booking", "name": "x"}, headers=h),
        client.post(f"/crm/v1/tags/{TAG}/bulk", json={"action": "flip"}, headers=h),
        client.post(f"/crm/v1/tags/{TAG}/bulk", json={"action": "add", "extra": 1}, headers=h),
        client.get("/crm/v1/marketing/ready-to-travel?owner=them", headers=h),
    ]
    assert [r.status_code for r in bad] == [422] * 4


@pytest.mark.parametrize(
    ("role", "allowed"),
    [("assistant", {"tags", "put", "ready-no"}), ("agent", {"tags", "put", "bulk", "ready"})],
)
def test_roles(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str, allowed: set[str]) -> None:
    _stubs(monkeypatch)
    with _client(pools, monkeypatch, role) as client:
        h = _headers(pools.partner_web())
        got = {
            "tags": client.get("/crm/v1/tags", headers=h).status_code,
            "put": client.put(f"/crm/v1/trips/{TRIP}/tags/{TAG}", headers=h).status_code,
            "bulk": client.post(
                f"/crm/v1/tags/{TAG}/bulk", json={"action": "add", "ids": [TRIP]}, headers=h
            ).status_code,
            "manage": client.patch(f"/crm/v1/tags/{TAG}", json={"name": "x"}, headers=h).status_code,
            "ready": client.get("/crm/v1/marketing/ready-to-travel", headers=h).status_code,
        }
    assert got["manage"] == 403
    assert (got["bulk"] == 200) == ("bulk" in allowed)
    assert (got["ready"] == 200) == ("ready" in allowed)
    assert got["tags"] == 200 and got["put"] == 200


# ── The tag trigger (card R) ─────────────────────────────────────────────────────────────────────────────────────────

RULE_ROW = {"workflow_rule_id": uuid.UUID(int=7), "version": 1, "trigger_kind": "event",
            "trigger_value": "client_tag.added", "conditions": {"tags_any": ["repeat"]}}  # fmt: skip


@pytest.mark.asyncio
async def test_a_client_tag_starts_its_rules_but_never_on_an_archived_client(monkeypatch: pytest.MonkeyPatch) -> None:
    starts: list[dict[str, Any]] = []

    async def start(ctx: Any, rule: Any, **kw: Any) -> uuid.UUID:
        starts.append(kw)
        return uuid.uuid4()

    monkeypatch.setattr(triggers, "start", start)
    other_rule = {**RULE_ROW, "workflow_rule_id": uuid.UUID(int=8), "conditions": '{"tags_any": ["vip"]}'}
    conn = ScriptedConn().on("FROM crm.workflow_rule", [RULE_ROW, other_rule])
    conn.on("FROM crm.traveler WHERE traveler_id", {"traveler_id": uuid.UUID(CLIENT), "owner_member_id": None,
                                                    "archived": False, "deleted": False})  # fmt: skip
    ctx = system_ctx(conn)
    link = uuid.uuid4()
    assert await triggers.on_tag_added(ctx, "client", "repeat", uuid.UUID(CLIENT), link) == 1
    assert starts == [{"trip_id": None, "traveler_id": uuid.UUID(CLIENT), "key": f"tag:{link}"}]
    assert conn.sql("FROM crm.workflow_rule")[0] == ("event", "client_tag.added")
    # Archived: nothing starts, one denied audit per rule.
    assert await triggers.on_tag_added(ctx, "client", "repeat", uuid.UUID(CLIENT), link, client_archived=True) == 0
    assert (ctx.audit_events[-1].action, ctx.audit_events[-1].outcome, ctx.audit_events[-1].reason) == (
        "workflow_start", "denied", "client_archived")  # fmt: skip
    conn.on("FROM crm.traveler WHERE traveler_id", None)
    assert await triggers.on_tag_added(ctx, "client", "repeat", uuid.UUID(CLIENT), link) == 0
    assert await triggers.on_tag_added(ctx, "client", "unknown", uuid.UUID(CLIENT), link) == 0  # no rule names it
    assert len(starts) == 1


@pytest.mark.asyncio
async def test_a_trip_tag_checks_the_trip_and_a_failure_never_fails_the_tagging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    async def start_matching(ctx: Any, kind: str, value: str, trip: Any, key: str, delay: float, **kw: Any) -> list:
        seen.append((kind, value, trip, key, [r["workflow_rule_id"] for r in kw["rules"]]))
        return [uuid.uuid4()]

    monkeypatch.setattr(triggers, "_start_matching", start_matching)
    rule = {**RULE_ROW, "trigger_value": "trip_tag.added", "conditions": {"tags_any": ["vip"]}}
    conn = ScriptedConn().on("FROM crm.workflow_rule", [rule])
    link = uuid.uuid4()
    assert await triggers.on_tag_added(system_ctx(conn), "trip", "vip", uuid.UUID(TRIP), link) == 1
    assert seen == [("event", "trip_tag.added", uuid.UUID(TRIP), f"tag:{link}", [uuid.UUID(int=7)])]
    conn.on("FROM crm.workflow_rule", asyncpg.PostgresError("boom"))
    assert await triggers.on_tag_added(system_ctx(conn), "trip", "vip", uuid.UUID(TRIP), link) == 0
    # A tag event never comes through the trip-event path.
    assert await triggers.on_trip_event(system_ctx(ScriptedConn()), uuid.UUID(TRIP), "trip_tag.added", link) == 0
