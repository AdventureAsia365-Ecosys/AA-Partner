"""AA-959 (card CRM-29a) without a database: the automation and task services and their routes on a scripted
connection. Owner edits, agent applies, the assistant reads (design §4.6); a rule naming another agency's template or
an inactive member is refused; switching on needs every send step's template; edits check the version; the task list
buckets by the agency's timezone; a member's own task needs a title and one subject; a hand-edited due date is marked
overridden (R20); the T-48 h task shows the arrival flights with a status link. The SQL runs on PostgreSQL in
``tests/integration/test_workflows_db.py``."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.main import create_app
from app.routers import workflows as router
from app.workflows import model, service, tasks
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, TENANT, FakeVault, enc, fake_tx, tenant_ctx

RULE = uuid.UUID("00000000-0000-4000-8000-0000000000b9")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000007e1")
TASK = uuid.UUID("00000000-0000-4000-8000-0000000000a5")
TEMPLATE = str(uuid.UUID("00000000-0000-4000-8000-00000000d0c5"))
NOW = dt.datetime(2026, 11, 1, 3, tzinfo=dt.UTC)
TZ = ZoneInfo("Asia/Ho_Chi_Minh")
TASK_RULE = {
    "name": "Checklist",
    "trigger": {"kind": "stage_enter", "value": "booked"},
    "steps": [{"action": "create_task", "kind": "custom", "title": "Book the airport pick-up"}],
}


def rule_row(**over: Any) -> dict[str, Any]:
    return {
        "workflow_rule_id": RULE,
        "name": "Checklist",
        "template_key": None,
        "template_version": None,
        "trigger_kind": "stage_enter",
        "trigger_value": "booked",
        "trigger_offset_days": 0,
        "conditions": "{}",
        "steps": json.dumps(TASK_RULE["steps"]),
        "status": "active",
        "version": 2,
    } | over


def trip_row(**over: Any) -> dict[str, Any]:
    return {
        "trip_id": TRIP,
        "stage": "booked",
        "trip_style": None,
        "lead_source": None,
        "destinations": [],
        "has_children": False,
        "intake_completeness": {"s3": 100},
        "owner_member_id": MEMBER,
        "start_date": dt.date(2026, 11, 3),
        "end_date": None,
        "stage_entered_at": NOW,
        "last_activity_at": NOW,
        "created_at": NOW,
        "deleted": False,
        "has_aa_lines": False,
        "cancelling": False,
        "lead_id": None,
    } | over


@pytest.mark.asyncio
async def test_the_overview_lists_rules_the_library_and_the_callers_rights() -> None:
    conn = ScriptedConn()
    conn.on("FROM crm.workflow_rule w", [rule_row(active_runs=1, finished_runs=4, created_at=NOW, updated_at=NOW)])
    conn.on("SELECT template_key FROM crm.workflow_rule", [{"template_key": "arrival_flight_t48h"}])
    out = await service.overview(tenant_ctx(conn, role="agent"))
    assert out["can_edit"] is False and out["can_apply"] is True
    assert out["rules"][0]["active_runs"] == 1 and out["rules"][0]["templates_missing"] == []
    installed = {t["key"]: t["installed"] for t in out["library"]}
    assert installed["arrival_flight_t48h"] is True and installed["birthday_greeting"] is False
    assert "message.received" in out["options"]["events"]
    assert (await service.overview(tenant_ctx(ScriptedConn(), role="assistant")))["can_apply"] is False


@pytest.mark.asyncio
async def test_create_checks_the_role_the_templates_and_the_members() -> None:
    with pytest.raises(Forbidden):
        await service.create(tenant_ctx(ScriptedConn(), role="agent"), TASK_RULE)
    conn = (
        ScriptedConn()
        .on("INSERT INTO crm.workflow_rule", RULE)
        .on("WHERE workflow_rule_id = $1 AND deleted_at IS NULL", rule_row())
    )
    out = await service.create(tenant_ctx(conn), TASK_RULE)
    assert out["workflow_rule_id"] == str(RULE)
    (args,) = conn.sql("INSERT INTO crm.workflow_rule")
    assert args[3:5] == ("stage_enter", "booked") and args[8] == "active"
    sending = {**TASK_RULE, "steps": [{"action": "send_email", "message_template_id": TEMPLATE}]}
    with pytest.raises(Invalid, match="template"):  # not one of this agency's templates (RLS hides others)
        await service.create(tenant_ctx(ScriptedConn()), sending)
    with pytest.raises(Conflict) as err:
        await service.create(tenant_ctx(ScriptedConn()), {**TASK_RULE, "steps": [{"action": "send_email"}]})
    assert err.value.code == "templates_missing"
    texting = {**TASK_RULE, "steps": [{"action": "send_text", "channel": "sms", "content_sid": "HX" + "c" * 32}]}
    with pytest.raises(Invalid, match="SMS"):
        await service.create(tenant_ctx(ScriptedConn()), texting)
    member = f"member:{uuid.uuid4()}"
    named = {**TASK_RULE, "steps": [{"action": "set_field", "field": "owner", "value": member}]}
    with pytest.raises(Invalid) as err:
        await service.create(tenant_ctx(ScriptedConn()), named)
    assert err.value.code == "member_not_found"
    ok = ScriptedConn().on("FROM crm.message_template WHERE", [{"message_template_id": TEMPLATE}])
    ok.on("INSERT INTO crm.workflow_rule", RULE).on("WHERE workflow_rule_id = $1 AND deleted_at IS NULL", rule_row())
    await service.create(tenant_ctx(ok), sending)
    with pytest.raises(Invalid):
        await service.create(tenant_ctx(ScriptedConn()), TASK_RULE, status="archived")


@pytest.mark.asyncio
async def test_install_a_library_template() -> None:
    conn = (
        ScriptedConn()
        .on("INSERT INTO crm.workflow_rule", RULE)
        .on("WHERE workflow_rule_id = $1 AND deleted_at IS NULL", rule_row())
    )
    await service.install(tenant_ctx(conn), "birthday_greeting")
    (args,) = conn.sql("INSERT INTO crm.workflow_rule")
    assert args[1:3] == ("birthday_greeting", 1) and args[8] == "paused"  # its mail template is still to pick
    with pytest.raises(NotFound):
        await service.install(tenant_ctx(ScriptedConn()), "nothing")
    with pytest.raises(Invalid) as err:
        await service.install(tenant_ctx(ScriptedConn()), "birthday_greeting", steps=[{"action": "wait"}])
    assert err.value.code == "steps_mismatch"
    taken = ScriptedConn().on("SELECT 1 FROM crm.workflow_rule WHERE template_key", 1)
    with pytest.raises(Conflict):
        await service.install(tenant_ctx(taken), "arrival_flight_t48h", name="Flights")
    renamed = (
        ScriptedConn()
        .on("INSERT INTO crm.workflow_rule", RULE)
        .on("WHERE workflow_rule_id = $1 AND deleted_at IS NULL", rule_row())
    )
    await service.install(tenant_ctx(renamed), "arrival_flight_t48h", name="Flights")
    assert renamed.sql("INSERT INTO crm.workflow_rule")[0][0] == "Flights"
    assert renamed.sql("INSERT INTO crm.workflow_rule")[0][8] == "active"


@pytest.mark.asyncio
async def test_edit_status_and_get() -> None:
    conn = ScriptedConn().on("WHERE workflow_rule_id = $1 AND deleted_at IS NULL", rule_row(version=3))
    conn.on("FOR UPDATE", rule_row()).on("RETURNING version", 3)
    out = await service.update(tenant_ctx(conn), RULE, TASK_RULE, version=2)
    assert out["version"] == 3
    with pytest.raises(Conflict) as err:
        await service.update(tenant_ctx(conn), RULE, TASK_RULE, version=1)
    assert err.value.code == "version_conflict"
    with pytest.raises(NotFound):
        await service.update(tenant_ctx(ScriptedConn()), RULE, TASK_RULE, version=2)
    archived = ScriptedConn().on("FOR UPDATE", rule_row(status="archived"))
    with pytest.raises(Conflict):
        await service.update(tenant_ctx(archived), RULE, TASK_RULE, version=2)
    with pytest.raises(Conflict):
        await service.set_status(tenant_ctx(archived), RULE, "active")
    paused = ScriptedConn().on("FOR UPDATE", rule_row(status="paused", steps=json.dumps([{"action": "send_email"}])))
    with pytest.raises(Conflict) as err:
        await service.set_status(tenant_ctx(paused), RULE, "active")
    assert err.value.code == "templates_missing"
    await service.set_status(tenant_ctx(conn), RULE, "paused")
    assert conn.sql("UPDATE crm.workflow_rule SET status")[0] == (RULE, "paused")
    assert (await service.set_status(tenant_ctx(conn), RULE, "active"))["status"] == "active"  # already: no write
    with pytest.raises(Invalid):
        await service.set_status(tenant_ctx(conn), RULE, "gone")
    with pytest.raises(NotFound):
        await service.set_status(tenant_ctx(ScriptedConn()), RULE, "paused")
    with pytest.raises(NotFound):
        await service.get(tenant_ctx(ScriptedConn()), RULE)


@pytest.mark.asyncio
async def test_preview_counts_live_trips_meeting_the_rule() -> None:
    other = uuid.UUID(int=8)
    conn = ScriptedConn().on("WHERE workflow_rule_id = $1 AND deleted_at IS NULL", rule_row())
    conn.on("ORDER BY t.last_activity_at DESC", [{"trip_id": TRIP}, {"trip_id": other}])
    conn.on("WHERE t.trip_id = ANY($1::uuid[])", [trip_row(), trip_row(trip_id=other, stage="proposal")])
    conn.on("SELECT now()", NOW).on("SELECT timezone", "Asia/Ho_Chi_Minh")
    out = await service.preview(tenant_ctx(conn), RULE)
    assert out == {"workflow_rule_id": str(RULE), "subject": "trip", "count": 1, "sample": [str(TRIP)]}
    dated = conn.on(
        "FROM crm.workflow_rule WHERE",
        rule_row(trigger_kind="date_anchor", trigger_value="departure", trigger_offset_days=-2),
    )
    assert (await service.preview(tenant_ctx(dated), RULE))["count"] == 2  # both depart on 3 Nov - 2 = today
    later = conn.on(
        "FROM crm.workflow_rule WHERE",
        rule_row(trigger_kind="date_anchor", trigger_value="departure", trigger_offset_days=-1),
    )
    assert (await service.preview(tenant_ctx(later), RULE))["count"] == 0
    client = ScriptedConn().on("FROM crm.workflow_rule WHERE", rule_row(trigger_kind="special_date"))
    assert (await service.preview(tenant_ctx(client), RULE))["subject"] == "client"
    with pytest.raises(NotFound):
        await service.preview(tenant_ctx(ScriptedConn()), RULE)


@pytest.mark.asyncio
async def test_apply_dry_run_then_run() -> None:
    gone = uuid.UUID(int=9)
    conn = ScriptedConn().on("FROM crm.workflow_rule WHERE", rule_row(trigger_kind="manual", trigger_value=None))
    conn.on("WHERE t.trip_id = ANY($1::uuid[])", [trip_row()])
    conn.on("INSERT INTO crm.workflow_run", {"workflow_run_id": uuid.UUID(int=3), "next_at": NOW})
    request = uuid.UUID(int=12)
    dry = await service.apply(
        tenant_ctx(conn, role="agent"), RULE, [TRIP, gone, TRIP], dry_run=True, request_id=request
    )
    assert dry["eligible"] == [str(TRIP)] and dry["blocked"] == [{"trip_id": str(gone), "reason": "trip_not_found"}]
    ran = await service.apply(tenant_ctx(conn, role="agent"), RULE, [TRIP], dry_run=False, request_id=request)
    assert ran["started"] == 1 and conn.sql("INSERT INTO crm.workflow_run")[0][4] == f"manual:{request}:{TRIP}"
    with pytest.raises(Forbidden):
        await service.apply(tenant_ctx(conn, role="assistant"), RULE, [TRIP], dry_run=True, request_id=request)
    with pytest.raises(Invalid):
        await service.apply(tenant_ctx(conn), RULE, [], dry_run=True, request_id=request)
    with pytest.raises(NotFound):
        await service.apply(tenant_ctx(ScriptedConn()), RULE, [TRIP], dry_run=True, request_id=request)
    paused = ScriptedConn().on("FROM crm.workflow_rule WHERE", rule_row(status="paused"))
    with pytest.raises(Conflict):
        await service.apply(tenant_ctx(paused), RULE, [TRIP], dry_run=True, request_id=request)
    client = ScriptedConn().on("FROM crm.workflow_rule WHERE", rule_row(trigger_kind="special_date"))
    with pytest.raises(Conflict):
        await service.apply(tenant_ctx(client), RULE, [TRIP], dry_run=True, request_id=request)


@pytest.mark.asyncio
async def test_runs_and_stop() -> None:
    run = {
        "workflow_run_id": uuid.UUID(int=3),
        "workflow_rule_id": RULE,
        "rule_name": "Checklist",
        "template_key": None,
        "trip_id": TRIP,
        "traveler_id": None,
        "trigger_key": "stage:abc",
        "status": "active",
        "step_index": 1,
        "next_at": NOW,
        "stop_reason": None,
        "log": '[{"step": 0, "outcome": "created"}]',
        "created_at": NOW,
        "finished_at": None,
        "step_count": 2,
    }
    conn = ScriptedConn().on("FROM crm.workflow_run r JOIN crm.workflow_rule w", [run])
    out = await service.runs(tenant_ctx(conn), trip_id=TRIP, traveler_id=None, rule_id=None)
    assert out["runs"][0]["trigger"] == "stage" and out["runs"][0]["log"][0]["outcome"] == "created"
    stop = ScriptedConn().on(
        "FROM crm.workflow_run WHERE workflow_run_id", {"workflow_run_id": 1, "trip_id": TRIP, "status": "active"}
    )
    assert (await service.stop(tenant_ctx(stop, role="agent"), uuid.UUID(int=3)))["status"] == "stopped"
    with pytest.raises(Forbidden):
        await service.stop(tenant_ctx(stop, role="assistant"), uuid.UUID(int=3))
    ended = ScriptedConn().on(
        "FROM crm.workflow_run WHERE workflow_run_id", {"workflow_run_id": 1, "trip_id": TRIP, "status": "completed"}
    )
    with pytest.raises(Conflict):
        await service.stop(tenant_ctx(ended), uuid.UUID(int=3))
    with pytest.raises(NotFound):
        await service.stop(tenant_ctx(ScriptedConn()), uuid.UUID(int=3))


# ── Tasks ─────────────────────────────────────────────────────────────────────────────────────────


def task_row(**over: Any) -> dict[str, Any]:
    return {
        "task_id": TASK,
        "kind": "custom_" + "a" * 24,
        "priority": "normal",
        "assignee_member_id": MEMBER,
        "status": "open",
        "due_at": NOW + dt.timedelta(hours=2),
        "due_anchor": "created",
        "due_offset_days": 0,
        "due_overridden": False,
        "done_at": None,
        "created_at": NOW,
        "created_by": f"member:{MEMBER}",
        "trip_id": TRIP,
        "traveler_id": None,
        "workflow_run_id": None,
        "title_enc": enc("task", "title_enc", "Book the airport pick-up"),
        "trip_title": "Highlands loop",
        "trip_stage": "booked",
    } | over


def test_buckets_and_sources() -> None:
    assert tasks.bucket(None, NOW, TZ) == "waiting_for_dates"
    assert tasks.bucket(NOW - dt.timedelta(minutes=1), NOW, TZ) == "overdue"
    assert tasks.bucket(NOW + dt.timedelta(hours=3), NOW, TZ) == "today"
    assert tasks.bucket(NOW + dt.timedelta(days=3), NOW, TZ) == "upcoming"
    assert tasks.bucket(NOW + dt.timedelta(days=20), NOW, TZ) == "later"
    assert tasks.source(task_row()) == "manual"
    assert tasks.source(task_row(workflow_run_id=uuid.UUID(int=1))) == "workflow"
    assert tasks.source(task_row(kind="aa_incident")) == "aa_event"
    assert tasks.source(task_row(kind="reply_inquiry")) == "system"


@pytest.mark.asyncio
async def test_the_list_opens_titles_and_buckets_them() -> None:
    rows = [task_row(), task_row(task_id=uuid.UUID(int=2), kind="send_pre_travel_pack", title_enc=None, due_at=None)]
    conn = ScriptedConn().on("FROM crm.task k LEFT JOIN crm.trip t", rows).on("SELECT now()", NOW)
    conn.on("SELECT timezone", "Asia/Ho_Chi_Minh").on("task_digest", True)
    ctx = tenant_ctx(conn, role="agent")
    out = await tasks.listing(ctx, FakeVault(), scope="mine")
    assert out["counts"]["today"] == 1 and out["counts"]["waiting_for_dates"] == 1
    assert out["buckets"]["today"][0]["title"] == "Book the airport pick-up" and out["digest"] is True
    assert out["can_set_digest"] is False
    (args,) = conn.sql("FROM crm.task k LEFT JOIN crm.trip t")
    assert args[0] is True and args[2] == MEMBER and args[3] is True
    assert ("list_view", "task") in [(e.action, e.object_kind) for e in ctx.audit_events]
    done = await tasks.listing(tenant_ctx(conn), FakeVault(), scope="team", status="done")
    assert len(done["done"]) == 2 and "buckets" not in done
    for bad in ({"scope": "everyone"}, {"status": "late"}, {"scope": "trip"}, {"scope": "client"}):
        with pytest.raises(Invalid):
            await tasks.listing(tenant_ctx(conn), FakeVault(), **bad)


@pytest.mark.asyncio
async def test_a_members_own_task() -> None:
    conn = ScriptedConn().on("SELECT 1 FROM crm.trip", 1).on("FROM crm.task k LEFT JOIN crm.trip t", task_row())
    conn.on("FROM crm.member WHERE member_id", 1)
    out = await tasks.create(
        tenant_ctx(conn),
        FakeVault(),
        title="  Book the   airport pick-up ",
        trip_id=TRIP,
        due_at=NOW,
        assignee_member_id=uuid.UUID(int=5),
    )
    assert out["title"] == "Book the airport pick-up"
    (args,) = conn.sql("INSERT INTO crm.task")
    assert args[3].startswith("custom_") and args[5] == uuid.UUID(int=5) and args[9] is True
    assert args[10] == b"enc:task.title_enc:Book the airport pick-up"
    rel = ScriptedConn().on("SELECT 1 FROM crm.trip", 1).on("FROM crm.task k LEFT JOIN crm.trip t", task_row())
    rel.on("WHERE t.trip_id = ANY($1::uuid[])", [trip_row(start_date=dt.date(2026, 12, 20))])
    rel.on("SELECT timezone", "Asia/Ho_Chi_Minh")
    await tasks.create(tenant_ctx(rel), FakeVault(), title="Pack", trip_id=TRIP, due_rule=("departure", -7))
    (args,) = rel.sql("INSERT INTO crm.task")
    assert args[6:10] == ("departure", -7, dt.datetime(2026, 12, 13, 2, tzinfo=dt.UTC), False)
    for kwargs, error in (
        ({"trip_id": TRIP, "traveler_id": uuid.UUID(int=1)}, Invalid),
        ({"trip_id": TRIP}, NotFound),
        ({"traveler_id": uuid.UUID(int=1)}, NotFound),
        ({"priority": "high"}, Invalid),
        ({"due_at": NOW, "due_rule": ("departure", 1)}, Invalid),
        ({"due_rule": ("departure", 1)}, Invalid),
        ({"assignee_member_id": uuid.UUID(int=6)}, Invalid),
    ):
        with pytest.raises(error):
            await tasks.create(tenant_ctx(ScriptedConn()), FakeVault(), title="x", **kwargs)
    with pytest.raises(Invalid):
        await tasks._relative_due(tenant_ctx(rel), TRIP, "departure", 999)  # noqa: SLF001
    with pytest.raises(Invalid):
        await tasks.create(tenant_ctx(ScriptedConn()), FakeVault(), title=" ")


@pytest.mark.asyncio
async def test_edits() -> None:
    conn = ScriptedConn().on(
        "FOR UPDATE",
        {"task_id": TASK, "kind": "custom_" + "a" * 24, "status": "open", "trip_id": TRIP, "traveler_id": None},
    )
    conn.on("FROM crm.task k LEFT JOIN crm.trip t", task_row()).on("FROM crm.member WHERE member_id", 1)
    await tasks.update(
        tenant_ctx(conn),
        FakeVault(),
        TASK,
        title="New title",
        due_at=NOW,
        set_due=True,
        assignee_member_id=uuid.UUID(int=4),
        set_assignee=True,
        priority="urgent",
    )
    (args,) = conn.sql("UPDATE crm.task")
    assert args == (TASK, b"enc:task.title_enc:New title", True, NOW, True, uuid.UUID(int=4), "urgent")
    coded = ScriptedConn().on(
        "FOR UPDATE", {"task_id": TASK, "kind": "reply_inquiry", "status": "open", "trip_id": TRIP, "traveler_id": None}
    )
    with pytest.raises(Conflict):
        await tasks.update(tenant_ctx(coded), FakeVault(), TASK, title="x")
    with pytest.raises(Invalid):
        await tasks.update(tenant_ctx(coded), FakeVault(), TASK, priority="high")
    closed = ScriptedConn().on(
        "FOR UPDATE", {"task_id": TASK, "kind": "x", "status": "done", "trip_id": None, "traveler_id": None}
    )
    with pytest.raises(Conflict):
        await tasks.update(tenant_ctx(closed), FakeVault(), TASK, priority="urgent")
    with pytest.raises(NotFound):
        await tasks.update(tenant_ctx(ScriptedConn()), FakeVault(), TASK)
    with pytest.raises(NotFound):
        await tasks.get(tenant_ctx(ScriptedConn()), FakeVault(), TASK)


@pytest.mark.asyncio
async def test_the_arrival_flight_task_carries_the_status_link() -> None:
    conn = ScriptedConn().on(
        "FROM crm.task k LEFT JOIN crm.trip t", task_row(kind="confirm_arrival_flight", title_enc=None)
    )
    conn.on(
        "FROM crm.traveler_flight",
        [
            {
                "traveler_flight_id": uuid.UUID(int=1),
                "flight_no_enc": enc("traveler_flight", "flight_no_enc", "vn 611"),
                "airport_iata": "SGN",
                "sched_local": dt.datetime(2026, 12, 20, 9, 30),
                "tz": "Asia/Ho_Chi_Minh",
            }
        ],
    )
    ctx = tenant_ctx(conn)
    out = await tasks.get(ctx, FakeVault(), TASK)
    assert out["flights"] == [
        {
            "flight_no": "vn 611",
            "airport_iata": "SGN",
            "sched_local": "2026-12-20T09:30:00",
            "tz": "Asia/Ho_Chi_Minh",
            "status_url": "https://www.flightaware.com/live/flight/VN611",
        }
    ]
    assert ("view", "traveler_flight") in [(e.action, e.object_kind) for e in ctx.audit_events]
    digest = ScriptedConn()
    assert await tasks.set_digest(tenant_ctx(digest), True) == {"digest": True}
    assert await tasks.redate(tenant_ctx(digest), TZ) == 0


# ── Routes ────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str = "owner") -> tuple[Any, dict[str, str]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    ctx = tenant_ctx(ScriptedConn(), role=role)
    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", fake_tx(ctx))
    monkeypatch.setattr(router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return app, headers


ID = "00000000-0000-4000-8000-0000000000e2"
RULE_BODY = {"name": "Checklist", "trigger": {"kind": "stage_enter", "value": "booked"}, "steps": TASK_RULE["steps"]}
ROUTES = [
    ("get", "/crm/v1/tasks?scope=team", None, (tasks, "listing")),
    (
        "post",
        "/crm/v1/tasks",
        {"title": "Call", "due_rule": {"anchor": "departure", "offset_days": -3}, "trip_id": ID},
        (tasks, "create"),
    ),
    ("get", f"/crm/v1/tasks/{ID}", None, (tasks, "get")),
    ("patch", f"/crm/v1/tasks/{ID}", {"due_at": None}, (tasks, "update")),
    ("put", "/crm/v1/tasks/digest", {"enabled": True}, (tasks, "set_digest")),
    ("get", "/crm/v1/workflows", None, (service, "overview")),
    ("post", "/crm/v1/workflows", RULE_BODY, (service, "create")),
    ("post", "/crm/v1/workflows/install", {"template_key": "arrival_flight_t48h"}, (service, "install")),
    ("get", f"/crm/v1/workflows/runs?trip_id={ID}", None, (service, "runs")),
    ("post", f"/crm/v1/workflows/runs/{ID}/stop", None, (service, "stop")),
    ("get", f"/crm/v1/workflows/{ID}", None, (service, "get")),
    ("patch", f"/crm/v1/workflows/{ID}", {**RULE_BODY, "version": 2}, (service, "update")),
    ("post", f"/crm/v1/workflows/{ID}/status", {"status": "paused"}, (service, "set_status")),
    ("get", f"/crm/v1/workflows/{ID}/preview", None, (service, "preview")),
    ("post", f"/crm/v1/workflows/{ID}/apply", {"trip_ids": [ID], "dry_run": True}, (service, "apply")),
]


@pytest.mark.parametrize(("method", "path", "body", "target"), ROUTES)
def test_each_route_reaches_its_helper(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any, target: Any
) -> None:
    app, headers = _client(pools, monkeypatch)
    calls: list[tuple[str, Any]] = []
    module, name = target

    async def fn(*args: Any, **kwargs: Any) -> Any:
        calls.append((name, kwargs))
        return {"ok": name}

    monkeypatch.setattr(module, name, fn)
    with TestClient(app) as client:
        response = getattr(client, method)(path, headers=headers, **({"json": body} if body is not None else {}))
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": name} and calls[0][0] == name
    if name == "update" and module is tasks:
        assert calls[0][1]["set_due"] is True and calls[0][1]["set_assignee"] is False


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/crm/v1/workflows", RULE_BODY),
        ("put", "/crm/v1/tasks/digest", {"enabled": False}),
        ("post", f"/crm/v1/workflows/{ID}/status", {"status": "paused"}),
    ],
)
def test_only_the_owner_edits(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any):
    app, headers = _client(pools, monkeypatch, role="agent")
    with TestClient(app) as client:
        response = getattr(client, method)(path, headers=headers, json=body)
    assert response.status_code == 403 and response.json()["detail"] == "role_forbidden"


def test_the_assistant_does_not_apply(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    app, headers = _client(pools, monkeypatch, role="assistant")
    with TestClient(app) as client:
        response = client.post(f"/crm/v1/workflows/{ID}/apply", headers=headers, json={"trip_ids": [ID]})
    assert response.status_code == 403


def test_model_keeps_the_merge_fields_of_the_text_proposal() -> None:
    assert "traveller_first_name" in model.MERGE_FIELDS


@pytest.mark.asyncio
async def test_a_due_date_is_due_at_nine_in_the_agency_zone() -> None:
    conn = (
        ScriptedConn().on("FROM crm.task k LEFT JOIN crm.trip t", task_row()).on("SELECT timezone", "Asia/Ho_Chi_Minh")
    )
    await tasks.create(tenant_ctx(conn), FakeVault(), title="Call", due_on=dt.date(2026, 11, 5))
    (args,) = conn.sql("INSERT INTO crm.task")
    assert args[8] == dt.datetime(2026, 11, 5, 2, tzinfo=dt.UTC) and args[9] is True
    with pytest.raises(Invalid):
        await tasks.create(tenant_ctx(conn), FakeVault(), title="Call", due_on=dt.date(2026, 11, 5), due_at=NOW)
    conn.on(
        "FOR UPDATE", {"task_id": TASK, "kind": "reply_inquiry", "status": "open", "trip_id": None, "traveler_id": None}
    )
    await tasks.update(tenant_ctx(conn), FakeVault(), TASK, due_on=dt.date(2026, 11, 6))
    assert conn.sql("UPDATE crm.task")[0][2:4] == (True, dt.datetime(2026, 11, 6, 2, tzinfo=dt.UTC))
