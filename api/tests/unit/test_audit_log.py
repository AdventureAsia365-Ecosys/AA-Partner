"""AA-940 access log (A-18 Data & privacy) without a database: owner only with a committed denial, filters, paging,
the download, the sign-in event, the routes; plus the new audit events of F1 / F2 / Slice 1 (refused owner actions,
key operations, AI context builds) and the audit metrics."""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import audit_log
from app.errors import Forbidden, Invalid
from app.jobs import metrics
from app.llm.gateway import stub_gateway
from app.main import create_app
from app.observability import ops_alerts
from app.routers import audit as audit_router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, OTHER_MEMBER, TENANT, fake_tx, system_ctx, tenant_ctx

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def _row(i: int, *, actor_ref: str = f"member:{MEMBER}", at: datetime = NOW) -> dict[str, Any]:
    return {
        "event_id": uuid.UUID(int=i),
        "at": at - timedelta(minutes=i),
        "actor_kind": "user",
        "actor_ref": actor_ref,
        "action": "reveal",
        "outcome": "allowed",
        "object_kind": "traveler",
        "object_id": f"t-{i}",
        "fields": ["passport_no_enc"],
        "reason": "prepare_booking",
        "request_id": "req-1",
        "object_ids": [],
    }


def test_filters_are_validated() -> None:
    for bad in (
        {"actor": "everyone"},
        {"action": "Drop Table"},
        {"object_kind": "x y"},
        {"outcome": "maybe"},
        {"object_id": "a b"},
        {"date_from": date(2026, 2, 1), "date_to": date(2026, 1, 1)},
    ):
        with pytest.raises(Invalid):
            audit_log.AuditFilter(**bad)


def test_cursor_round_trip_and_garbage() -> None:
    event = uuid.uuid4()
    assert audit_log.decode_cursor(audit_log.encode_cursor(NOW, event)) == (NOW, event)
    for bad in ("%%%", "bm90LWEtY3Vyc29y", audit_log.encode_cursor(NOW, event)[:-6]):
        with pytest.raises(Invalid):
            audit_log.decode_cursor(bad)


def test_csv_cells_cannot_start_a_formula() -> None:
    assert audit_log._cell("=cmd") == "'=cmd"
    assert audit_log._cell(["a", "b"]) == "a;b"
    assert audit_log._cell(None) == ""
    assert audit_log.window_start(NOW) == NOW - timedelta(days=365)


@pytest.mark.asyncio
async def test_a_non_owner_gets_403_and_a_denied_event() -> None:
    for role in ("agent", "assistant"):
        ctx = tenant_ctx(ScriptedConn(), role=role)
        with pytest.raises(Forbidden):
            await audit_log.list_events(ctx, audit_log.AuditFilter(), cursor=None, limit=10)
        with pytest.raises(Forbidden):
            await audit_log.export_events(ctx, audit_log.AuditFilter(), "csv")
        assert [(e.action, e.outcome, e.reason) for e in ctx.audit_events] == [
            ("audit_view", "denied", "owner_only"),
            ("audit_export", "denied", "owner_only"),
        ]
        assert ctx.conn.calls == []  # refused before any read


@pytest.mark.asyncio
async def test_the_owner_pages_newest_first_with_filters() -> None:
    rows = [_row(1), _row(2, actor_ref=f"member:{OTHER_MEMBER}"), _row(3, actor_ref="job:crm_intake_process")]
    conn = ScriptedConn(
        [
            ("FROM crm.audit_event WHERE", rows),
            ("FROM crm.member", [{"member_id": MEMBER, "role": "owner"}, {"member_id": OTHER_MEMBER, "role": "agent"}]),
        ]
    )
    ctx = tenant_ctx(conn)
    f = audit_log.AuditFilter(
        actor="user",
        action="reveal",
        outcome="allowed",
        member_id=OTHER_MEMBER,
        object_kind="traveler",
        object_id="t-2",
        date_from=date(2026, 10, 1),
        date_to=date(2026, 10, 24),
    )
    page = await audit_log.list_events(ctx, f, cursor=audit_log.encode_cursor(NOW, uuid.uuid4()), limit=2, now=NOW)
    assert [e["event_id"] for e in page["events"]] == [str(uuid.UUID(int=1)), str(uuid.UUID(int=2))]
    assert page["events"][1]["member_role"] == "agent" and page["events"][1]["actor_group"] == "user"
    assert page["next_cursor"] == audit_log.encode_cursor(rows[1]["at"], rows[1]["event_id"])
    assert page["retention_months"] == 12
    sql, args = next((s, a) for s, a in conn.calls if "FROM crm.audit_event" in s)
    assert "ORDER BY at DESC, event_id DESC" in sql and "(at, event_id) <" in sql
    assert ["user", "traveler"] in args and f"member:{OTHER_MEMBER}" in args and args[-1] == 3
    assert args[0] == datetime(2026, 10, 1, tzinfo=UTC)  # the later of "from" and 12 months back
    assert [(e.action, e.outcome) for e in ctx.audit_events] == [("audit_view", "allowed")]
    with pytest.raises(Invalid):
        await audit_log.list_events(ctx, audit_log.AuditFilter(), cursor=None, limit=0)
    job = await audit_log.list_events(
        tenant_ctx(ScriptedConn([("FROM crm.audit_event", [rows[2]])])), audit_log.AuditFilter(), cursor=None, limit=5
    )
    assert job["events"][0]["member_id"] is None and job["next_cursor"] is None
    assert audit_log._member_id("member:not-a-uuid") is None


@pytest.mark.asyncio
async def test_the_download_is_csv_or_json_lines_and_audited() -> None:
    conn = ScriptedConn([("FROM crm.audit_event WHERE", [_row(1), _row(2)])])
    ctx = tenant_ctx(conn)
    body, count = await audit_log.export_events(ctx, audit_log.AuditFilter(), "csv", now=NOW)
    rows = list(csv.reader(io.StringIO(body.decode())))
    assert count == 2 and rows[0] == list(audit_log.CSV_COLUMNS) and rows[1][3] == "reveal"
    lines, _ = await audit_log.export_events(ctx, audit_log.AuditFilter(), "jsonl", now=NOW)
    assert lines.count(b"\n") == 2 and b'"action": "reveal"' in lines
    assert [e.reason for e in ctx.audit_events] == ["format_csv", "format_jsonl"]
    with pytest.raises(Invalid):
        await audit_log.export_events(ctx, audit_log.AuditFilter(), "xlsx")
    big = ScriptedConn([("FROM crm.audit_event WHERE", [_row(1)] * (audit_log.EXPORT_MAX_ROWS + 1))])
    with pytest.raises(Invalid) as too_large:
        await audit_log.export_events(tenant_ctx(big), audit_log.AuditFilter(), "csv")
    assert too_large.value.code == "export_too_large"


def test_sign_in_is_one_event_of_the_member() -> None:
    ctx = tenant_ctx(role="assistant")
    assert audit_log.record_sign_in(ctx) == {"recorded": True}
    [event] = ctx.audit_events
    assert (event.action, event.object_kind, event.object_id) == ("sign_in", "member", str(MEMBER))


def test_select_takes_fixed_orders_only() -> None:
    with pytest.raises(ValueError):
        audit_log._select("true", "at; DROP TABLE x", 1)


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


@pytest.fixture
def client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def _pool(_request: Any) -> object:
        return object()

    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(audit_router, "request_pool", _pool)
    monkeypatch.setattr(audit_router, "tenant_tx", fake_tx(tenant_ctx(ScriptedConn())))
    with TestClient(app) as c:
        yield c


def test_routes_reach_the_access_log(client: TestClient, pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Any]] = []

    async def list_events(ctx: Any, f: audit_log.AuditFilter, *, cursor: Any, limit: int) -> dict[str, Any]:
        seen.append(("list", (f, cursor, limit)))
        return {"events": []}

    async def export_events(ctx: Any, f: audit_log.AuditFilter, fmt: str) -> tuple[bytes, int]:
        seen.append(("export", (f, fmt)))
        return b"at\r\n", 0

    monkeypatch.setattr(audit_log, "list_events", list_events)
    monkeypatch.setattr(audit_log, "export_events", export_events)
    h = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    r = client.get(f"/crm/v1/audit/events?actor=ai&from=2026-10-01&to=2026-10-24&member_id={MEMBER}&limit=5", headers=h)
    assert r.status_code == 200
    f, cursor, limit = seen[0][1]
    assert (f.actor, f.date_from, f.member_id, cursor, limit) == ("ai", date(2026, 10, 1), MEMBER, None, 5)
    r = client.get("/crm/v1/audit/events/export?format=csv&outcome=denied", headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].startswith('attachment; filename="access-log-')
    assert r.headers["x-row-count"] == "0" and seen[1][1][1] == "csv"
    assert client.post("/crm/v1/audit/sign-in", headers=h).json() == {"recorded": True}
    assert client.get("/crm/v1/audit/events?actor=everyone", headers=h).status_code == 422
    assert client.get("/crm/v1/audit/events/export?format=xlsx", headers=h).status_code == 422
    assert client.get("/crm/v1/audit/events", headers={"X-AA-Tenant-Id": str(TENANT)}).status_code == 401
    staff = {"Authorization": f"Bearer {pools.cis_admin()}", "X-AA-Tenant-Id": str(TENANT)}
    assert client.get("/crm/v1/audit/events", headers=staff).status_code == 403


@pytest.mark.asyncio
async def test_an_ai_call_is_an_audit_event_of_the_ai_actor() -> None:
    conn = ScriptedConn(
        [
            ("FROM crm.tenant_settings", {"ai_enabled": False, "ai_budget_usd": "2"}),
            ("sum(", "0"),
            ("INSERT INTO crm.llm_call", uuid.UUID(int=7)),
        ]
    )
    ctx = system_ctx(conn)
    result = await stub_gateway().call(ctx, stage="crm_draft", role="writer", prompt="hi")
    assert result.refusal_code == "ai_disabled"
    [event] = ctx.audit_events
    assert (event.actor_kind, event.actor_ref, event.action, event.outcome, event.reason) == (
        "ai_agent",
        "ai:crm_draft",
        "ai_context_build",
        "denied",
        "ai_disabled",
    )


def test_audit_values_feed_the_ops_checks() -> None:
    """S263: the audit export state is checked in-app (was the three AA/Jobs/Crm audit alarms)."""
    sample = metrics.Sample(0.0, 0, 0, 0.0)
    healthy = metrics.AuditSample(export_lag_minutes=-3.0, partition_months_ahead=3, verify_lag_hours=30.0)
    assert ops_alerts.state_findings(sample, healthy) == []
    bad = metrics.AuditSample(export_lag_minutes=180.0, partition_months_ahead=0, verify_lag_hours=50.0)
    assert [f.check for f in ops_alerts.state_findings(sample, bad)] == [
        "audit_export_lag",
        "audit_partitions",
        "audit_verify_lag",
    ]


@pytest.mark.asyncio
async def test_read_audit_sample() -> None:
    row = {"export_lag_min": 42.0, "months_ahead": 3, "verify_lag_h": 5.0}
    ok = ScriptedConn([("crm.audit_export_agencies()", row)])
    assert await metrics.read_audit_sample(ok) == metrics.AuditSample(42.0, 3, 5.0)
    assert await metrics.read_audit_sample(ScriptedConn([("crm.audit_export_agencies()", RuntimeError("x"))])) is None


@pytest.mark.asyncio
async def test_refused_owner_actions_leave_a_denied_event() -> None:
    """AA-940: the F1 / F2 / Slice 1 owner-only refusals are on record (tenant_tx commits denied events); since AA-939
    they go through the permission matrix (permissions.require): the action is the matrix's, the reason its code."""
    from app import pipeline
    from app.documents import service as documents
    from app.routers import pipeline as pipeline_router

    trip, doc = uuid.uuid4(), uuid.uuid4()
    agent = tenant_ctx(
        ScriptedConn([("FOR UPDATE", {"stage": "inquiry", "owner_member_id": OTHER_MEMBER})]), role="agent"
    )
    with pytest.raises(Forbidden):
        await pipeline.assign(agent, trip, MEMBER)
    assistant = tenant_ctx(ScriptedConn(), role="assistant")
    with pytest.raises(Forbidden):
        await documents.trash(assistant, doc)
    with pytest.raises(Forbidden):
        pipeline_router._owner(assistant)
    assert [(e.action, e.outcome, e.object_kind, e.reason) for e in agent.audit_events + assistant.audit_events] == [
        ("reassign", "denied", "trip", "role_forbidden"),
        ("document_delete", "denied", "document", "role_forbidden"),
        ("settings_forms_manage", "denied", "intake_form", "role_forbidden"),
    ]
