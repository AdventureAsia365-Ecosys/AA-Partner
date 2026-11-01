"""AA-959 (card CRM-29a) without a database: the engine (:mod:`app.workflows.engine`), the triggers, the run job and
the digest on a scripted connection. A run stops itself on an archived rule, a lost / trashed / cancelled trip and an
archived client; a paused rule waits; every step's outcome is logged; a marketing send without consent is skipped and
audited denied (card R); a database error fails the run once instead of retrying for ever. The SQL runs on
PostgreSQL in ``tests/integration/test_workflows_db.py``."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import asyncpg
import pytest

from app.errors import Conflict, Forbidden
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.vault import DecryptionDenied
from app.workflows import digest, engine, runner, tick, triggers
from tests.unit.fakes import ScriptedConn
from tests.unit.messaging_fakes import connection_row
from tests.unit.slice_fakes import TENANT, FakeVault, enc, system_ctx, tenant_ctx

RUN = uuid.UUID("00000000-0000-4000-8000-0000000000a9")
RULE = uuid.UUID("00000000-0000-4000-8000-0000000000b9")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000007e1")
LEAD = uuid.UUID("00000000-0000-4000-8000-00000000c11e")
OWNER = uuid.UUID("00000000-0000-4000-8000-0000000000c0")
TEMPLATE = uuid.UUID("00000000-0000-4000-8000-00000000d0c5")
NOW = dt.datetime(2026, 11, 1, 3, tzinfo=dt.UTC)
SID = "HX" + "b" * 32


def run_row(steps: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    return {
        "workflow_run_id": RUN,
        "workflow_rule_id": RULE,
        "trip_id": TRIP,
        "traveler_id": None,
        "trigger_key": "stage:x",
        "status": "active",
        "step_index": 0,
        "next_at": NOW - dt.timedelta(seconds=5),
        "log": "[]",
        "rule_status": "active",
        "steps": json.dumps(steps),
        "trigger_kind": "stage_enter",
        "trigger_value": "booked",
    } | over


def trip_row(**over: Any) -> dict[str, Any]:
    return {
        "trip_id": TRIP,
        "stage": "booked",
        "trip_style": "balanced",
        "lead_source": None,
        "destinations": ["vietnam"],
        "has_children": False,
        "intake_completeness": json.dumps({"s1": 100, "s2": 80, "s3": 40}),
        "owner_member_id": OWNER,
        "start_date": dt.date(2026, 12, 20),
        "end_date": None,
        "stage_entered_at": NOW,
        "last_activity_at": NOW,
        "created_at": NOW,
        "deleted": False,
        "has_aa_lines": False,
        "cancelling": False,
        "lead_id": LEAD,
    } | over


def conn_for(steps: list[dict[str, Any]], run: dict[str, Any] | None = None, trip: dict[str, Any] | None = None):
    conn = ScriptedConn()
    conn.on("FROM crm.workflow_run r JOIN crm.workflow_rule w", run or run_row(steps))
    conn.on("WHERE t.trip_id = ANY($1::uuid[])", [trip or trip_row()])
    conn.on("SELECT timezone FROM crm.tenant_settings", "Asia/Ho_Chi_Minh")
    return conn


async def advance(conn: ScriptedConn, now: dt.datetime = NOW) -> dict[str, Any]:
    return await engine.advance(system_ctx(conn), FakeVault(), RUN, now=now)  # type: ignore[arg-type]


def finished(conn: ScriptedConn) -> tuple[Any, ...]:
    (args,) = conn.sql("UPDATE crm.workflow_run SET status = $2")
    return args


def logged(args: tuple[Any, ...]) -> list[str]:
    return [e["outcome"] for e in json.loads(args[-1])]


@pytest.mark.asyncio
async def test_missing_inactive_and_not_due_runs_do_nothing() -> None:
    conn = ScriptedConn()
    assert (await advance(conn))["status"] == "missing"
    assert (await advance(conn_for([], run_row([], status="completed"))))["status"] == "completed"
    later = run_row([], next_at=NOW + dt.timedelta(minutes=5))
    assert (await advance(conn_for([], later)))["status"] == "not_due"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "trip", "reason"),
    [
        ({"rule_status": "archived"}, {}, "rule_archived"),
        ({}, {"stage": "lost"}, "trip_lost"),
        ({}, {"deleted": True}, "trip_trashed"),
        ({}, {"cancelling": True}, "trip_cancelled"),
    ],
)
async def test_a_run_stops_itself(run: dict[str, Any], trip: dict[str, Any], reason: str) -> None:
    steps = [{"action": "create_task", "kind": "request_review"}]
    conn = conn_for(steps, run_row(steps, **run), trip_row(**trip))
    out = await advance(conn)
    assert (out["status"], out["reason_code"]) == ("stopped", reason)
    assert finished(conn)[1:3] == ("stopped", reason)
    assert not conn.sql("INSERT INTO crm.task")


@pytest.mark.asyncio
async def test_a_rule_on_lost_runs_on_the_lost_trip_and_a_paused_rule_waits() -> None:
    steps = [{"action": "create_task", "kind": "request_review"}]
    conn = conn_for(steps, run_row(steps, trigger_value="lost"), trip_row(stage="lost")).on("INSERT INTO crm.task", RUN)
    assert (await advance(conn))["status"] == "completed"
    paused = conn_for(steps, run_row(steps, rule_status="paused"))
    assert (await advance(paused))["status"] == "paused"
    (args,) = paused.sql("UPDATE crm.workflow_run SET step_index = $2")
    assert args[2] == NOW + engine.PAUSED_RECHECK


@pytest.mark.asyncio
async def test_a_client_run_stops_for_a_trashed_or_archived_client() -> None:
    steps = [{"action": "create_task", "kind": "passport_expiring"}]
    client_run = run_row(steps, trip_id=None, traveler_id=LEAD, trigger_kind="special_date", trigger_value="birthday")
    conn = conn_for(steps, client_run).on("FROM crm.traveler WHERE traveler_id = $1", None)
    assert (await advance(conn))["reason_code"] == "client_trashed"
    archived = {"traveler_id": LEAD, "owner_member_id": OWNER, "archived": True, "deleted": False}
    conn = conn_for(steps, client_run).on("SELECT traveler_id, owner_member_id, archived_at", archived)
    assert (await advance(conn))["reason_code"] == "archived"
    live = archived | {"archived": False}
    conn = conn_for(steps, client_run).on("SELECT traveler_id, owner_member_id, archived_at", live)
    conn.on("INSERT INTO crm.task", RUN)
    assert (await advance(conn))["status"] == "completed"
    (args,) = conn.sql("INSERT INTO crm.task")
    assert args[1] == LEAD and args[4] == OWNER and args[5] == "created"  # client task, assigned to the client's owner


@pytest.mark.asyncio
async def test_tasks_waits_and_fields() -> None:
    member = uuid.UUID(int=77)
    steps = [
        {"action": "create_task", "kind": "custom", "title": "Call the client", "assignee": f"member:{member}"},
        {"action": "create_task", "kind": "send_pre_travel_pack", "due": {"anchor": "return", "offset_days": 2}},
        {"action": "set_field", "field": "waiting_on", "value": "client"},
        {"action": "set_field", "field": "owner", "value": f"member:{member}"},
        {"action": "wait", "until": {"anchor": "departure", "offset_days": -7}},
        {"action": "create_task", "kind": "request_review"},
    ]
    conn = conn_for(steps).on("INSERT INTO crm.task", RUN).on("FROM crm.member WHERE member_id = $1", 1)
    out = await advance(conn)
    assert out == {"workflow_run_id": str(RUN), "status": "waiting", "steps_done_count": 5, "next_step_count": 5}
    custom, pack = conn.sql("INSERT INTO crm.task")
    assert custom[2].startswith("custom_") and custom[4] == member and custom[8].startswith(b"enc:task.title_enc")
    assert pack[2] == "send_pre_travel_pack" and pack[7] is None and pack[8] is None  # no return date yet: waiting
    assert conn.sql("UPDATE crm.trip SET waiting_on")[0][1] == "client"
    assert conn.sql("UPDATE crm.trip SET owner_member_id")[0][1] == member
    (save,) = conn.sql("UPDATE crm.workflow_run SET step_index = $2")
    assert save[2] == dt.datetime(2026, 12, 13, 2, tzinfo=dt.UTC)  # departure - 7 days at 09:00 local
    assert [e["outcome"] for e in json.loads(save[3])] == ["created", "created", "set", "set", "waiting"]


@pytest.mark.asyncio
async def test_an_open_task_inactive_member_waits_for_dates_and_unknown_actions() -> None:
    steps = [
        {"action": "create_task", "kind": "request_review", "assignee": f"member:{uuid.UUID(int=5)}"},
        {"action": "set_field", "field": "owner", "value": f"member:{uuid.UUID(int=5)}"},
        {"action": "teleport"},
        {"action": "wait", "until": {"anchor": "return", "offset_days": 1}},
        {"action": "create_task", "kind": "request_review"},
    ]
    conn = conn_for(steps)
    out = await advance(conn)
    assert out["status"] == "waiting" and out["next_step_count"] == 3  # the wait for a missing date stays on its step
    (save,) = conn.sql("UPDATE crm.workflow_run SET step_index = $2")
    assert save[2] == NOW + engine.NO_DATE_RECHECK
    assert [e["outcome"] for e in json.loads(save[3])] == [
        "already_open",
        "skipped_member_inactive",
        "skipped_unknown_action",
        "waiting_for_dates",
    ]
    # A wait whose time has passed goes straight on; a wait in days sets next_at.
    steps = [
        {"action": "wait", "until": {"anchor": "departure", "offset_days": -100}},
        {"action": "wait", "days": 2},
        {"action": "create_task", "kind": "request_review"},
    ]
    conn = conn_for(steps)
    assert (await advance(conn))["next_step_count"] == 2
    (save,) = conn.sql("UPDATE crm.workflow_run SET step_index = $2")
    assert save[2] == NOW + dt.timedelta(days=2) and [e["outcome"] for e in json.loads(save[3])] == [
        "passed",
        "waiting",
    ]


def mail_conn(purpose: str = "transactional", consent: bool | None = None) -> ScriptedConn:
    steps = [{"action": "send_email", "message_template_id": str(TEMPLATE), "purpose": purpose}]
    conn = conn_for(steps)
    conn.on("SELECT archived_at IS NOT NULL AS archived", {"archived": False, "deleted": False})
    conn.on(
        "FROM crm.message_template",
        {
            "message_template_id": TEMPLATE,
            "locale": "en",
            "title": "Hello {{traveller_first_name}}",
            "body_enc": enc(
                "message_template",
                "body_enc",
                "Hi {{traveller_first_name}} of {{trip_name}}, from {{agency_name}} ({{departure_date}})",
            ),
        },
    )
    conn.on("FROM crm.consent", consent)
    conn.on(
        "SELECT given_name_enc, display_name_enc",
        {"given_name_enc": enc("traveler", "given_name_enc", "Mai"), "display_name_enc": None},
    )
    conn.on("SELECT display_name FROM crm.tenant", "Lotus Trails")
    conn.on("SELECT title FROM crm.trip", "Highlands loop")
    conn.on("FROM crm.conversation", uuid.UUID(int=9))
    conn.on("INSERT INTO crm.conversation", uuid.UUID(int=9))
    conn.on("SELECT archived_at, deleted_at FROM crm.traveler", {"archived_at": None, "deleted_at": None})
    conn.on("INSERT INTO crm.dispatch_queue", {"dispatch_id": uuid.UUID(int=8), "not_before": NOW})
    return conn


@pytest.mark.asyncio
async def test_a_workflow_mail_is_merged_and_queued_without_undo_delay() -> None:
    conn = mail_conn()
    ctx = system_ctx(conn)
    out = await engine.advance(ctx, FakeVault(), RUN, now=NOW)  # type: ignore[arg-type]
    assert out["status"] == "completed"
    (msg,) = conn.sql("INSERT INTO crm.message (")
    assert msg[3] == b"enc:message.subject_enc:Hello Mai"
    assert msg[4] == b"enc:message.body_enc:Hi Mai of Highlands loop, from Lotus Trails (20 Dec 2026)"
    (queued,) = conn.sql("INSERT INTO crm.dispatch_queue")
    assert queued[2:5] == ("email", "transactional", "workflow") and queued[6] == 0
    assert logged(finished(conn)) == ["queued"]
    assert ("message_send", "allowed") in [(e.action, e.outcome) for e in ctx.audit_events]


@pytest.mark.asyncio
async def test_r_a_marketing_mail_without_consent_is_not_sent_and_audited_denied() -> None:
    conn = mail_conn("marketing", consent=False)
    ctx = system_ctx(conn)
    await engine.advance(ctx, FakeVault(), RUN, now=NOW)  # type: ignore[arg-type]
    assert not conn.sql("INSERT INTO crm.message") and not conn.sql("INSERT INTO crm.dispatch_queue")
    assert logged(finished(conn)) == ["skipped_no_consent"]
    assert [(e.action, e.outcome, e.reason) for e in ctx.audit_events if e.action == "workflow_send"] == [
        ("workflow_send", "denied", "no_consent")
    ]
    conn = mail_conn("marketing", consent=True)
    await advance(conn)
    assert conn.sql("INSERT INTO crm.dispatch_queue")[0][3] == "marketing"


@pytest.mark.asyncio
async def test_mail_steps_skip_with_a_code() -> None:
    conn = mail_conn().on("SELECT archived_at IS NOT NULL AS archived", {"archived": True, "deleted": False})
    await advance(conn)
    assert logged(finished(conn)) == ["skipped_archived"]
    conn = mail_conn().on("FROM crm.message_template", None)
    await advance(conn)
    assert logged(finished(conn)) == ["skipped_template_missing"]
    conn = mail_conn().on(
        "SELECT archived_at, deleted_at FROM crm.traveler", {"archived_at": dt.datetime.now(dt.UTC), "deleted_at": None}
    )
    await advance(conn)
    assert logged(finished(conn)) == ["skipped_traveler_archived"]  # refused at queueing: the savepoint undoes it
    steps = [{"action": "send_email", "message_template_id": None}, {"action": "send_text", "channel": "sms"}]
    conn = conn_for(steps)
    await advance(conn)
    assert logged(finished(conn)) == ["skipped_template_missing", "skipped_template_missing"]
    conn = conn_for([{"action": "send_email", "message_template_id": str(TEMPLATE)}], trip=trip_row(lead_id=None))
    await advance(conn)
    assert logged(finished(conn)) == ["skipped_no_client"]


def text_conn(**over: Any) -> ScriptedConn:
    steps = [
        {
            "action": "send_text",
            "channel": "whatsapp",
            "content_sid": SID,
            "variables": {"1": "traveller_first_name", "2": "trip_name"},
        }
    ]
    conn = conn_for(steps)
    conn.on("SELECT archived_at IS NOT NULL AS archived", {"archived": False, "deleted": False})
    conn.on("FROM crm.messaging_connection", over.get("connection", connection_row()))
    conn.on("SELECT phone_enc FROM crm.traveler", over.get("phone", enc("traveler", "phone_enc", "+61491570110")))
    conn.on("SELECT conversation_id FROM crm.conversation", uuid.UUID(int=9))
    conn.on("opted_out_at IS NOT NULL", over.get("opted_out", False))
    conn.on(
        "FROM crm.channel_template",
        over.get(
            "template",
            {
                "content_sid": SID,
                "name": "trip_update",
                "language": "en",
                "category": "UTILITY",
                "body": "Hi {{1}}, news about {{2}}",
                "variables": ["1", "2"],
            },
        ),
    )
    conn.on("FROM crm.consent", over.get("consent"))
    conn.on(
        "SELECT given_name_enc, display_name_enc",
        {"given_name_enc": None, "display_name_enc": enc("traveler", "display_name_enc", "Mai Tran")},
    )
    conn.on("SELECT title FROM crm.trip", over.get("title", "Highlands loop"))
    conn.on("SELECT archived_at, deleted_at FROM crm.traveler", {"archived_at": None, "deleted_at": None})
    conn.on("INSERT INTO crm.dispatch_queue", {"dispatch_id": uuid.UUID(int=8), "not_before": NOW})
    return conn


@pytest.mark.asyncio
async def test_a_workflow_text_sends_an_approved_template() -> None:
    conn = text_conn()
    await advance(conn)
    assert logged(finished(conn)) == ["queued"]
    (msg,) = conn.sql("INSERT INTO crm.message (")
    assert msg[2] == "whatsapp" and msg[3] == b"enc:message.body_enc:Hi Mai, news about Highlands loop"
    (delivery,) = conn.sql("INSERT INTO crm.message_delivery")
    assert delivery[1:3] == ("whatsapp", SID) and delivery[3].startswith(b"enc:message_delivery.variables_enc")
    assert conn.sql("INSERT INTO crm.dispatch_queue")[0][2:5] == ("whatsapp", "transactional", "workflow")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("over", "outcome"),
    [
        ({"connection": None}, "skipped_channel_not_connected"),
        ({"connection": connection_row(credential_status="disconnected")}, "skipped_channel_not_connected"),
        ({"connection": connection_row(whatsapp_sender=None)}, "skipped_channel_not_connected"),
        ({"phone": None}, "skipped_no_client_phone"),
        ({"phone": enc("traveler", "phone_enc", "not a number")}, "skipped_no_client_phone"),
        ({"opted_out": True}, "skipped_client_opted_out"),
        ({"template": None}, "skipped_template_not_approved"),
        (
            {
                "template": {
                    "content_sid": SID,
                    "name": "promo",
                    "language": "en",
                    "category": "MARKETING",
                    "body": "Hi {{1}}",
                    "variables": ["1"],
                }
            },
            "skipped_no_consent",
        ),
        ({"title": None}, "skipped_template_variables_missing"),
    ],
)
async def test_workflow_text_skips(over: dict[str, Any], outcome: str) -> None:
    conn = text_conn(**over)
    await advance(conn)
    assert logged(finished(conn)) == [outcome]
    assert not conn.sql("INSERT INTO crm.dispatch_queue")


@pytest.mark.asyncio
async def test_a_database_error_fails_the_run_once() -> None:
    steps = [{"action": "create_task", "kind": "request_review"}]
    conn = conn_for(steps).on("INSERT INTO crm.task", asyncpg.CheckViolationError("x"))
    out = await advance(conn)
    assert (out["status"], out["reason_code"]) == ("failed", "step_failed")
    assert logged(finished(conn)) == ["failed"]


# ── Triggers ──────────────────────────────────────────────────────────────────────────────────────


def rule_row(**over: Any) -> dict[str, Any]:
    return {
        "workflow_rule_id": RULE,
        "version": 3,
        "trigger_kind": "stage_enter",
        "trigger_value": "booked",
        "conditions": "{}",
    } | over


@pytest.mark.asyncio
async def test_a_stage_entered_by_a_member_starts_matching_rules_after_the_undo_window() -> None:
    conn = ScriptedConn()
    conn.on(
        "FROM crm.workflow_rule",
        [rule_row(), rule_row(workflow_rule_id=uuid.UUID(int=4), conditions={"stages": ["proposal"]})],
    )
    conn.on("WHERE t.trip_id = ANY($1::uuid[])", [trip_row()])
    conn.on("INSERT INTO crm.workflow_run", {"workflow_run_id": RUN, "next_at": NOW})
    conn.on("INSERT INTO crm.job", "job-1")
    ctx = tenant_ctx(conn)
    assert await triggers.on_stage_enter(ctx, TRIP, "booked", uuid.UUID(int=6), user_action=True) == 1
    (start,) = conn.sql("INSERT INTO crm.workflow_run")
    assert start == (RULE, 3, TRIP, None, f"stage:{uuid.UUID(int=6)}", triggers.UNDO_DELAY)
    (job,) = conn.sql("INSERT INTO crm.job")
    assert job[0] == "crm_workflow_run" and job[2] == f"crm_workflow_run:{RUN}:0"
    assert await triggers.on_stage_enter(ctx, TRIP, "cancelled", uuid.UUID(int=6)) == 0


@pytest.mark.asyncio
async def test_no_rule_no_trip_or_an_ended_trip_start_nothing() -> None:
    conn = ScriptedConn()
    assert await triggers.on_stage_enter(tenant_ctx(conn), TRIP, "booked", uuid.UUID(int=6)) == 0
    conn.on("FROM crm.workflow_rule", [rule_row()])
    assert await triggers.on_stage_enter(tenant_ctx(conn), TRIP, "booked", uuid.UUID(int=6)) == 0  # trip gone
    conn.on("WHERE t.trip_id = ANY($1::uuid[])", [trip_row(stage="lost")])
    assert await triggers.on_stage_enter(tenant_ctx(conn), TRIP, "booked", uuid.UUID(int=6)) == 0
    conn.on("INSERT INTO crm.workflow_run", None)  # the key already started one
    conn.on("WHERE t.trip_id = ANY($1::uuid[])", [trip_row(stage="lost")])
    assert await triggers.on_stage_enter(tenant_ctx(conn), TRIP, "lost", uuid.UUID(int=6)) == 0
    assert conn.sql("INSERT INTO crm.workflow_run")  # a rule on Lost does start on a lost trip


@pytest.mark.asyncio
async def test_events_messages_and_errors() -> None:
    conn = ScriptedConn().on("FROM crm.workflow_rule", [rule_row(trigger_kind="event")])
    conn.on("WHERE t.trip_id = ANY($1::uuid[])", [trip_row()])
    conn.on("INSERT INTO crm.workflow_run", {"workflow_run_id": RUN, "next_at": NOW})
    ctx = system_ctx(conn)
    assert await triggers.on_trip_event(ctx, TRIP, "proposal.accepted", uuid.UUID(int=1)) == 1
    assert await triggers.on_trip_event(ctx, TRIP, "message.received", uuid.UUID(int=1)) == 0  # the seam's
    assert await triggers.on_trip_event(ctx, TRIP, "proposal.hold_placed", uuid.UUID(int=1)) == 0  # not a trigger

    class Event:
        kind, trip_id, message_id = "received", TRIP, uuid.UUID(int=2)

    await triggers.on_message(ctx, Event())
    assert conn.sql("INSERT INTO crm.workflow_run")[-1][4] == f"event:{uuid.UUID(int=2)}"
    Event.trip_id = None
    await triggers.on_message(ctx, Event())
    assert len(conn.sql("INSERT INTO crm.workflow_run")) == 2
    broken = ScriptedConn().on("FROM crm.workflow_rule", asyncpg.UndefinedTableError("x"))
    assert await triggers.on_stage_enter(system_ctx(broken), TRIP, "booked", uuid.UUID(int=6)) == 0  # never fails


@pytest.mark.asyncio
async def test_stops_and_install() -> None:
    conn = ScriptedConn().on("SELECT count(*)::int FROM crm.workflow_run", 2)
    conn.rules.append(("UPDATE crm.workflow_run", None))
    ctx = tenant_ctx(conn)
    assert await triggers.stop_trip_runs(ctx, TRIP, True) == 2
    assert await triggers.stop_trip_runs(ctx, TRIP, False) == 0  # the scripted UPDATE answers "OK"
    assert conn.sql("UPDATE crm.workflow_run SET status = 'stopped', stop_reason = $2")[0] == (TRIP, "trip_cancelled")
    await triggers.stop_for_trigger(ctx, TRIP, "stage:x")
    assert conn.sql("trigger_key = $2")[0] == (TRIP, "stage:x", "undone")
    assert await triggers._cancel_hook(ctx, TRIP, True) == 2  # noqa: SLF001
    triggers.install()
    triggers.install()
    from app import cancellation
    from app.messaging import events

    assert [n for n, _ in cancellation.CANCEL_HOOKS].count("workflows") == 1
    assert events._LISTENERS.count(triggers.on_message) == 1  # noqa: SLF001


# ── Jobs ──────────────────────────────────────────────────────────────────────────────────────────


def job(payload: dict[str, Any]) -> Job:
    return Job("j", "crm_workflow_run", payload, 1, 5)


@pytest.mark.asyncio
async def test_the_run_job(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"tenant_id": str(TENANT), "workflow_run_id": str(RUN)}
    with pytest.raises(PermanentError):
        await runner.build_handler(object())(object(), job({}))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await runner.build_handler(None)(object(), job(payload))  # type: ignore[arg-type]
    from tests.unit.slice_fakes import fake_tx

    monkeypatch.setattr(runner, "system_tx", fake_tx(system_ctx(ScriptedConn())))
    monkeypatch.setattr(runner, "Vault", lambda keys, ctx: FakeVault())
    out = await runner.build_handler(object())(object(), job(payload))  # type: ignore[arg-type]
    assert out["status"] == "missing"

    async def denied(*_: Any, **__: Any) -> Any:
        raise DecryptionDenied("x")

    monkeypatch.setattr(runner.engine, "advance", denied)
    with pytest.raises(PermanentError):
        await runner.build_handler(object())(object(), job(payload))  # type: ignore[arg-type]

    async def locked(*_: Any, **__: Any) -> Any:
        raise Forbidden("tenant_locked")

    monkeypatch.setattr(runner.engine, "advance", locked)
    assert (await runner.build_handler(object())(object(), job(payload)))["status"] == "agency_skipped"  # type: ignore[arg-type]


class Directory:
    issuer = "https://issuer.example"

    async def email_for(self, sub: str) -> str | None:
        if sub == "boom":
            raise RuntimeError("down")
        return None if sub == "none" else f"{sub}@agency.example"


class Mailer:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> str:
        self.sent.append(message)
        return "id"


class Runtime:
    def __init__(self) -> None:
        self.directory, self.mailer, self.web_origin = Directory(), Mailer(), "https://go.crm.example"


@pytest.mark.asyncio
async def test_the_digest_mails_counts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.slice_fakes import fake_tx

    rows = [
        {
            "member_id": uuid.UUID(int=i),
            "cognito_iss": iss,
            "cognito_sub": sub,
            "overdue": 2,
            "today": 1,
            "upcoming": 3,
            "waiting": 1,
        }
        for i, (iss, sub) in enumerate(
            [(Directory.issuer, "minh"), (Directory.issuer, "boom"), (Directory.issuer, "none"), ("https://other", "x")]
        )
    ]
    conn = ScriptedConn().on("SELECT task_digest FROM crm.tenant_settings", True).on("FROM crm.task k JOIN", rows)
    conn.on("SELECT display_name FROM crm.tenant", "Lotus Trails").on("SELECT timezone", "Asia/Ho_Chi_Minh")
    monkeypatch.setattr(digest, "system_tx", fake_tx(system_ctx(conn)))
    runtime = Runtime()
    out = await digest.build_handler(runtime)(object(), Job("j", "crm_task_digest", {"tenant_id": str(TENANT)}, 1, 3))  # type: ignore[arg-type]
    assert out == {"member_count": 4, "sent_count": 1, "failed_count": 1}
    (mail,) = runtime.mailer.sent
    assert mail.kind == "task_digest" and mail.to == "minh@agency.example"
    assert "overdue: 2" in mail.text and "https://go.crm.example/tasks" in mail.text and "Lotus Trails" in mail.text
    off = ScriptedConn().on("SELECT task_digest FROM crm.tenant_settings", False)
    monkeypatch.setattr(digest, "system_tx", fake_tx(system_ctx(off)))
    assert (await digest.build_handler(runtime)(object(), Job("j", "k", {"tenant_id": str(TENANT)}, 1, 3)))[  # type: ignore[arg-type]
        "reason_code"
    ] == "digest_off"
    assert (await digest.build_handler(None)(object(), Job("j", "k", {"tenant_id": str(TENANT)}, 1, 3)))[  # type: ignore[arg-type]
        "reason_code"
    ] == "mail_not_configured"
    with pytest.raises(PermanentError):
        await digest.build_handler(runtime)(object(), Job("j", "k", {}, 1, 3))  # type: ignore[arg-type]
    now, today_end, week_end = digest.windows(NOW, tick.ZoneInfo("Asia/Ho_Chi_Minh"))
    assert today_end == dt.datetime(2026, 11, 1, 17, tzinfo=dt.UTC) and week_end - today_end == dt.timedelta(days=7)


@pytest.mark.asyncio
async def test_the_hourly_tick_schedule_and_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = ScriptedConn().on("INSERT INTO crm.job", "job-1")
    hook = tick.HourlyWorkflowTick(lambda: dt.datetime(2026, 11, 1, 7, 30, tzinfo=dt.UTC))
    assert await hook(pool) == "job-1" and await hook(pool) is None
    assert pool.sql("INSERT INTO crm.job")[0][2] == "crm_workflow_tick:schedule:2026-11-01T07"
    out = await tick.build_handler(None)(pool, Job("j", "crm_workflow_tick", {}, 1, 3))  # type: ignore[arg-type]
    assert out["reason_code"] == "key_provider_not_configured"

    class Agency:
        def __init__(self, n: int) -> None:
            self.tenant_id = uuid.UUID(int=n)

    async def agencies(_pool: Any) -> list[Any]:
        return [Agency(1), Agency(2), Agency(3)]

    calls: list[int] = []

    async def run_agency(ctx: Any, vault: Any, now: Any) -> Any:
        calls.append(1)
        from collections import Counter

        if len(calls) == 2:
            raise Conflict("x")
        return Counter({"date_runs_started": 2})

    def tx(pool: Any, tenant_id: Any, actor: str) -> Any:
        from tests.unit.slice_fakes import fake_tx

        if tenant_id == uuid.UUID(int=3):
            raise Forbidden("tenant_locked")
        return fake_tx(system_ctx(ScriptedConn()))(pool, tenant_id, actor=actor)

    monkeypatch.setattr(tick.registry, "agencies", agencies)
    monkeypatch.setattr(tick, "run_agency", run_agency)
    monkeypatch.setattr(tick, "system_tx", tx)
    monkeypatch.setattr(tick, "Vault", lambda keys, ctx: FakeVault())
    out = await tick.build_handler(object())(pool, Job("j", "crm_workflow_tick", {}, 1, 3))  # type: ignore[arg-type]
    assert out == {"agency_count": 1, "skipped_count": 1, "failed_count": 1, "date_runs_started_count": 2}


@pytest.mark.asyncio
async def test_run_agency_queues_due_runs_redates_and_the_digest() -> None:
    conn = ScriptedConn().on("SELECT timezone FROM crm.tenant_settings", "Asia/Ho_Chi_Minh")
    conn.on(
        "FROM crm.workflow_run r JOIN crm.workflow_rule w", [{"workflow_run_id": RUN, "step_index": 2, "next_at": NOW}]
    )
    conn.on("SELECT task_digest FROM crm.tenant_settings", True)
    conn.on("INSERT INTO crm.job", "job")
    # 07:10 local = the digest hour.
    counts = await tick.run_agency(system_ctx(conn), FakeVault(), dt.datetime(2026, 11, 1, 0, 10, tzinfo=dt.UTC))  # type: ignore[arg-type]
    assert counts["runs_queued"] == 1 and counts["digest_queued"] == 1
    keys = [args[2] for args in conn.sql("INSERT INTO crm.job")]
    assert keys == [f"crm_workflow_run:{RUN}:2:{int(NOW.timestamp())}", f"crm_task_digest:{TENANT}:2026-11-01"]
    assert conn.sql("UPDATE crm.task k SET due_at")[0] == ("Asia/Ho_Chi_Minh",)


@pytest.mark.asyncio
async def test_date_and_special_triggers_on_a_scripted_agency() -> None:
    today = dt.date(2026, 11, 1)
    conn = ScriptedConn()
    date_rule = rule_row(
        trigger_kind="date_anchor", trigger_value="departure", trigger_offset_days=-2, conditions={"stages": ["booked"]}
    )
    conn.on("FROM crm.workflow_rule", lambda kind: [date_rule] if kind == "date_anchor" else [])
    conn.on(
        "SELECT t.trip_id, t.start_date AS anchor_date",
        [{"trip_id": TRIP, "anchor_date": today}, {"trip_id": uuid.UUID(int=99), "anchor_date": today}],
    )
    conn.on("WHERE t.trip_id = ANY($1::uuid[])", [trip_row()])
    conn.on("INSERT INTO crm.workflow_run", {"workflow_run_id": RUN, "next_at": NOW})
    counts = await tick._date_triggers(system_ctx(conn), tick.ZoneInfo("Asia/Ho_Chi_Minh"), today)  # noqa: SLF001
    assert counts["date_runs_started"] == 1  # the second trip is gone
    assert conn.sql("INSERT INTO crm.workflow_run")[0][4] == f"date:departure:-2:{today.isoformat()}"
    special = ScriptedConn()
    rules = [
        rule_row(trigger_kind="special_date", trigger_value=v, trigger_offset_days=o)
        for v, o in (("birthday", 0), ("passport_expiry", -200), ("trip_anniversary", 0))
    ]
    special.on("FROM crm.workflow_rule", lambda kind: rules if kind == "special_date" else [])
    special.on(
        "dob_enc AS blob",
        [
            {"traveler_id": LEAD, "blob": enc("traveler", "dob_enc", "1990-11-01")},
            {"traveler_id": uuid.UUID(int=5), "blob": enc("traveler", "dob_enc", "garbled")},
        ],
    )
    special.on(
        "passport_expiry_enc AS blob",
        [{"traveler_id": LEAD, "blob": enc("traveler", "passport_expiry_enc", "2027-05-20")}],
    )
    special.on("JOIN crm.trip_traveler tt", [{"trip_id": TRIP, "end_date": dt.date(2025, 11, 1), "traveler_id": LEAD}])
    special.on("INSERT INTO crm.workflow_run", {"workflow_run_id": RUN, "next_at": NOW})
    counts = await tick._special_dates(system_ctx(special), FakeVault(), today)  # noqa: SLF001  # type: ignore[arg-type]
    assert counts["special_runs_started"] == 3
    keys = [args[4] for args in special.sql("INSERT INTO crm.workflow_run")]
    assert keys == [
        "special:birthday:2026-11-01",
        "special:passport_expiry:2027-05-20",
        f"special:trip_anniversary:{TRIP}:2026-11-01",
    ]
    assert await tick._special_dates(system_ctx(ScriptedConn()), FakeVault(), today) == {}  # noqa: SLF001  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_every_job_result_passes_the_payload_guard() -> None:
    """crm.job results are ids and codes only (app.jobs.payload_guard): every shape the engine answers."""
    from app.jobs.payload_guard import check_payload

    steps = [
        {"action": "create_task", "kind": "request_review"},
        {"action": "wait", "days": 1},
        {"action": "create_task", "kind": "request_review"},
    ]
    results = [
        await advance(ScriptedConn()),
        await advance(conn_for(steps, run_row(steps, rule_status="paused"))),
        await advance(conn_for(steps, run_row(steps, rule_status="archived"))),
        await advance(conn_for(steps).on("INSERT INTO crm.task", RUN)),
        await advance(conn_for(steps[:1]).on("INSERT INTO crm.task", asyncpg.CheckViolationError("x"))),
    ]
    for result in results:
        check_payload(result, what="result")
    assert {r["status"] for r in results} == {"missing", "paused", "stopped", "waiting", "failed"}
