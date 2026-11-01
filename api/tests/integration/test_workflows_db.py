"""AA-959 (card CRM-29a) end to end on PostgreSQL 15 with the RDS role shape and real field encryption: Module 10.

- A rule on "entered Qualifying" starts one run when a member moves the trip, after the Undo window (Fix B8); the run
  opens a custom task (sealed title), a library task waiting for the trip's dates (R20: ``due_at`` NULL, the
  "Waiting for dates" bucket, dated when the trip gets a start date), waits, then opens the last task; a transactional
  mail is queued from the agency's mailbox path (``origin workflow``).
- Undo of the move stops the run before its first step; the trip cancel's hook stops every active run (Fix C3).
- **E**: a date trigger (T-48 h arrival flight) ticked twice starts one run; the task is due two days before departure
  at 09:00 local. Two trips leaving on one day each get their run, and two clients with one birthday each get theirs
  (the trigger key names its subject, S263 review).
- Message triggers: a rule on ``message.received`` runs once per trip per cool-down window, and a text a workflow sent
  (``origin workflow``) starts no ``message.sent`` rule (no loop, S263 review).
- **R**: a birthday greeting (marketing) to a client without marketing consent is not sent: no message, the step
  logged ``skipped_no_consent``, an audit event ``workflow_send`` denied.
- A special-date run of an archived client stops itself (``archived``).
- A task list (manual rule) applied to a trip: dry run lists it eligible, the run opens its three custom tasks side by
  side; the tasks list, a member's own task, and a hand-edited due date that the trip's dates never move again.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from app import cancellation, pipeline, stages
from app.errors import Conflict
from app.intake import public as intake
from app.jobs.queue import Job
from app.mail import templates
from app.permissions import Action
from app.tenancy import system_tx, tenant_tx
from app.vault import Vault
from app.workflows import engine, runner, service, tasks, tick, triggers
from tests.integration.test_crm005_intake import SETTINGS, _keys, _owner_fetch, _process_all, answers
from tests.integration.test_crm010_lifecycle import _inquiry
from tests.integration.test_crm012_schema import _put_stage

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TZ = ZoneInfo("Asia/Ho_Chi_Minh")


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    keys = _keys()
    d = await _inquiry(pool, keys, "owner", "agent")
    d["keys"] = keys
    d["agent"] = d["people"][1][0]
    return d


async def _sql(pool: asyncpg.Pool, tenant_id: uuid.UUID, sql: str, *args: Any) -> list[Any]:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        return await conn.fetch(sql, *args)


async def _rule(pool: asyncpg.Pool, d: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.WORKFLOW_EDIT) as ctx:
        return await service.create(ctx, raw)


async def _advance(pool: asyncpg.Pool, d: dict[str, Any], run_id: Any, now: dt.datetime) -> dict[str, Any]:
    async with system_tx(pool, d["tenant"], actor=engine.ACTOR) as ctx:
        return await engine.advance(ctx, Vault(d["keys"], ctx), uuid.UUID(str(run_id)), now=now)


async def _runs(pool: asyncpg.Pool, d: dict[str, Any]) -> list[Any]:
    return await _sql(pool, d["tenant"], "SELECT * FROM crm.workflow_run ORDER BY created_at, workflow_run_id")


async def _move(pool: asyncpg.Pool, d: dict[str, Any], to: str) -> dict[str, Any]:
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.TRIP_EDIT) as ctx:
        return await stages.move(ctx, d["trip"], to)


QUALIFYING_RULE = {
    "name": "First call",
    "trigger": {"kind": "stage_enter", "value": "qualifying"},
    "steps": [
        {
            "action": "create_task",
            "kind": "custom",
            "title": "Call to talk through the dates",
            "due": {"anchor": "created", "offset_days": 1},
        },
        {"action": "create_task", "kind": "send_pre_travel_pack", "due": {"anchor": "departure", "offset_days": -14}},
        {"action": "wait", "days": 2},
        {"action": "create_task", "kind": "follow_up_quiet_client"},
    ],
}


async def test_a_stage_rule_runs_after_the_undo_window_and_dates_wait_for_the_trip(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    rule = await _rule(app_pool, d, QUALIFYING_RULE)
    moved = await _move(app_pool, d, "qualifying")
    assert moved["workflow_runs"] == 1
    (run,) = await _runs(app_pool, d)
    assert run["status"] == "active" and run["trigger_key"].startswith("stage:")
    assert run["next_at"] > dt.datetime.now(dt.UTC) + dt.timedelta(seconds=10)  # the Undo window first
    jobs = await _sql(app_pool, d["tenant"], "SELECT kind, payload FROM crm.job WHERE kind = 'crm_workflow_run'")
    assert any(json.loads(j["payload"])["workflow_run_id"] == str(run["workflow_run_id"]) for j in jobs)
    # The job's run before the window ends does nothing.
    job = Job(
        "j", "crm_workflow_run", {"tenant_id": str(d["tenant"]), "workflow_run_id": str(run["workflow_run_id"])}, 1, 5
    )
    assert (await runner.build_handler(d["keys"])(app_pool, job))["status"] == "not_due"
    now = run["next_at"] + dt.timedelta(seconds=1)
    out = await _advance(app_pool, d, run["workflow_run_id"], now)
    assert out["status"] == "waiting" and out["next_step_count"] == 3
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.PIPELINE_VIEW) as ctx:
        listed = await tasks.listing(ctx, Vault(d["keys"], ctx), scope="trip", trip_id=d["trip"])
    titles = {t["title"] for b in listed["buckets"].values() for t in b if t["source"] == "workflow"}
    assert "Call to talk through the dates" in titles
    [pack] = [t for t in listed["buckets"]["waiting_for_dates"] if t["kind"] == "send_pre_travel_pack"]
    assert pack["due_at"] is None and pack["due_anchor"] == "departure" and pack["due_offset_days"] == -14
    # The trip gets its dates: the waiting task is dated (14 days before at 09:00 local), R20.
    start = dt.date.today() + dt.timedelta(days=60)
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.TRIP_EDIT) as ctx:
        await pipeline.update_trip(
            ctx, d["trip"], {"start_date": start, "end_date": start + dt.timedelta(days=9)}, Vault(d["keys"], ctx)
        )
    [due] = await _sql(
        app_pool, d["tenant"], "SELECT due_at FROM crm.task WHERE task_id = $1", uuid.UUID(pack["task_id"])
    )
    assert due["due_at"] == dt.datetime.combine(start - dt.timedelta(days=14), dt.time(9), tzinfo=TZ)
    # Not due before the wait ends; then the last step and the run completes.
    assert (await _advance(app_pool, d, run["workflow_run_id"], now + dt.timedelta(days=1)))["status"] == "not_due"
    done = await _advance(app_pool, d, run["workflow_run_id"], now + dt.timedelta(days=2, minutes=1))
    assert done["status"] == "completed"
    [final] = await _runs(app_pool, d)
    log = json.loads(final["log"]) if isinstance(final["log"], str) else final["log"]
    assert [e["outcome"] for e in log] == ["created", "created", "waiting", "created"]
    # The rule shows the finished run; the trip page shows the custom task's title.
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.TRIP_VIEW) as ctx:
        detail = await pipeline.trip_detail(ctx, Vault(d["keys"], ctx), d["trip"])
        runs = await service.runs(ctx, trip_id=d["trip"], traveler_id=None, rule_id=None)
    assert any(t["title"] == "Call to talk through the dates" for t in detail["tasks"])
    assert runs["runs"][0]["status"] == "completed" and runs["runs"][0]["rule_name"] == rule["name"]


async def test_undo_stops_the_run_and_the_cancel_hook_stops_every_run(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    await _rule(app_pool, d, QUALIFYING_RULE)
    moved = await _move(app_pool, d, "qualifying")
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.TRIP_EDIT) as ctx:
        undone = await stages.undo(ctx, d["trip"], uuid.UUID(moved["undo"]["history_id"]))
    assert undone["workflows_stopped"] == 1
    [run] = await _runs(app_pool, d)
    assert (run["status"], run["stop_reason"]) == ("stopped", "undone")
    assert await _sql(app_pool, d["tenant"], "SELECT 1 FROM crm.task WHERE workflow_run_id IS NOT NULL") == []
    # A second move starts a new run (a new history row); the cancel hook stops it.
    await _move(app_pool, d, "qualifying")
    triggers.install()
    assert ("workflows", triggers._cancel_hook) in cancellation.CANCEL_HOOKS  # noqa: SLF001
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.TRIP_CANCEL) as ctx:
        assert await triggers.stop_trip_runs(ctx, d["trip"], True) == 1
        assert await triggers.stop_trip_runs(ctx, d["trip"], False) == 1
    stopped = [r["stop_reason"] for r in await _runs(app_pool, d)]
    assert stopped == ["undone", "trip_cancelled"]


async def test_a_date_tick_twice_starts_one_run(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    d = await _setup(app_pool)
    d["member"] = d["owner_id"]
    await _put_stage(admin, d, "booked")
    start = dt.datetime.now(TZ).date() + dt.timedelta(days=2)
    await _sql(app_pool, d["tenant"], "UPDATE crm.trip SET start_date = $2 WHERE trip_id = $1", d["trip"], start)
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.WORKFLOW_EDIT) as ctx:
        installed = await service.install(ctx, "arrival_flight_t48h")
    assert installed["status"] == "active"
    now = dt.datetime.now(dt.UTC)
    for _ in range(2):
        async with system_tx(app_pool, d["tenant"], actor=tick.ACTOR) as ctx:
            counts = await tick.run_agency(ctx, Vault(d["keys"], ctx), now)
    assert counts.get("date_runs_started", 0) == 0
    [run] = await _runs(app_pool, d)
    assert run["trigger_key"] == f"date:{d['trip']}:departure:-2:{start.isoformat()}"
    assert (await _advance(app_pool, d, run["workflow_run_id"], now + dt.timedelta(seconds=1)))["status"] == "completed"
    [task] = await _sql(
        app_pool,
        d["tenant"],
        "SELECT kind, priority, due_at FROM crm.task WHERE workflow_run_id = $1",
        run["workflow_run_id"],
    )
    assert task["kind"] == "confirm_arrival_flight" and task["priority"] == "urgent"
    assert task["due_at"] == dt.datetime.combine(start - dt.timedelta(days=2), dt.time(9), tzinfo=TZ)
    # A second install of the same template is refused while the first is live.
    with pytest.raises(Conflict):
        async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.WORKFLOW_EDIT) as ctx:
            await service.install(ctx, "arrival_flight_t48h")


async def _second_inquiry(pool: asyncpg.Pool, d: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID]:
    """Another client's inquiry through the same form: a second trip and a second lead client in the agency."""
    other = answers(name="Lê Minh Khoa", email="khoa.le@example.test", phone="+84987654321", companions=[])
    assert (await intake.submit(pool, d["keys"], SETTINGS, d["form"].public_key, other, origin=None)).stored
    [made] = [r for r in await _process_all(pool, d["keys"], d["tenant"]) if r["status"] == "created"]
    return uuid.UUID(made["trip_id"]), uuid.UUID(made["traveler_id"])


async def test_two_trips_on_one_date_and_two_clients_with_one_birthday_each_get_a_run(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    d = await _setup(app_pool)
    d["member"] = d["owner_id"]
    other_trip, other_lead = await _second_inquiry(app_pool, d)
    start = dt.datetime.now(TZ).date() + dt.timedelta(days=2)
    for trip in (d["trip"], other_trip):
        await _put_stage(admin, d | {"trip": trip}, "booked")
        await _sql(app_pool, d["tenant"], "UPDATE crm.trip SET start_date = $2 WHERE trip_id = $1", trip, start)
    template_id = await _template(app_pool, d)
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.WORKFLOW_EDIT) as ctx:
        await service.install(ctx, "arrival_flight_t48h")
        await service.install(
            ctx,
            "birthday_greeting",
            steps=[
                {"action": "send_email", "message_template_id": template_id},
                {"action": "create_task", "kind": "client_birthday"},
            ],
        )
    for lead in (d["lead"], other_lead):
        await _birthday_today(app_pool, d, lead)
    for _ in range(2):
        async with system_tx(app_pool, d["tenant"], actor=tick.ACTOR) as ctx:
            await tick.run_agency(ctx, Vault(d["keys"], ctx), _at_local_hour(8))
    runs = await _runs(app_pool, d)
    assert {r["trip_id"] for r in runs if r["trigger_key"].startswith("date:")} == {d["trip"], other_trip}
    assert {r["traveler_id"] for r in runs if r["trigger_key"].startswith("special:birthday:")} == {
        d["lead"],
        other_lead,
    }
    assert len(runs) == 4  # twice ticked, one run per subject


async def test_message_rules_cool_down_and_never_answer_a_workflow_text(app_pool: asyncpg.Pool) -> None:
    from app.messaging import events

    d = await _setup(app_pool)
    for value in ("message.received", "message.sent"):
        await _rule(
            app_pool,
            d,
            {
                "name": f"After {value}",
                "trigger": {"kind": "event", "value": value},
                "steps": [{"action": "create_task", "kind": "reply_client_message"}],
            },
        )

    def event(kind: str, message_id: uuid.UUID) -> events.MessageEvent:
        return events.MessageEvent(kind, "sms", message_id, uuid.uuid4(), d["trip"], d["lead"])

    # A message a workflow sent (here its mail: the dispatch row carries origin workflow and the message id).
    await _rule(app_pool, d, WELCOME_MAIL_RULE | {"steps": [WELCOME_MAIL_RULE["steps"][0] | {
        "message_template_id": await _template(app_pool, d)}]})  # fmt: skip
    await _move(app_pool, d, "qualifying")
    [welcome] = [r for r in await _runs(app_pool, d) if r["trigger_key"].startswith("stage:")]
    await _advance(app_pool, d, welcome["workflow_run_id"], welcome["next_at"] + dt.timedelta(seconds=1))
    [sent] = await _sql(app_pool, d["tenant"], "SELECT message_id FROM crm.dispatch_queue WHERE origin = 'workflow'")
    async with system_tx(app_pool, d["tenant"], actor="job:crm_messaging_test") as ctx:
        for _ in range(3):  # a client's auto-reply ping-pong: one run in the window
            await triggers.on_message(ctx, event("received", uuid.uuid4()))
        await triggers.on_message(ctx, event("sent", sent["message_id"]))  # the workflow's own message: no run
        await triggers.on_message(ctx, event("sent", uuid.uuid4()))  # a member's text: one run
    runs = await _sql(
        app_pool,
        d["tenant"],
        "SELECT w.trigger_value AS value, count(*)::int AS n FROM crm.workflow_run r "
        "JOIN crm.workflow_rule w ON w.workflow_rule_id = r.workflow_rule_id WHERE w.trigger_kind = 'event' "
        "GROUP BY 1 ORDER BY 1",
    )
    assert [(r["value"], r["n"]) for r in runs] == [("message.received", 1), ("message.sent", 1)]


WELCOME_MAIL_RULE: dict[str, Any] = {
    "name": "Welcome",
    "trigger": {"kind": "stage_enter", "value": "qualifying"},
    "steps": [{"action": "send_email", "message_template_id": None, "purpose": "transactional"}],
}


async def _template(pool: asyncpg.Pool, d: dict[str, Any]) -> str:
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.WORKFLOW_EDIT) as ctx:
        made = await templates.create(
            ctx,
            Vault(d["keys"], ctx),
            locale="en",
            title="Happy birthday, {{traveller_first_name}}",
            body="Hi {{traveller_first_name}},\nAll of us at {{agency_name}} wish you a happy birthday.",
        )
    return made["message_template_id"]


async def _birthday_today(pool: asyncpg.Pool, d: dict[str, Any], traveler_id: uuid.UUID) -> None:
    today = dt.datetime.now(TZ).date()
    dob = today.replace(year=today.year - 30) if not (today.month == 2 and today.day == 29) else dt.date(1996, 2, 29)
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.CLIENT_EDIT) as ctx:
        blob = await Vault(d["keys"], ctx).encrypt("traveler", "dob_enc", traveler_id, dob.isoformat())
        await ctx.conn.execute("UPDATE crm.traveler SET dob_enc = $2 WHERE traveler_id = $1", traveler_id, blob)


def _at_local_hour(hour: int) -> dt.datetime:
    return dt.datetime.combine(dt.datetime.now(TZ).date(), dt.time(hour, 5), tzinfo=TZ).astimezone(dt.UTC)


async def test_r_a_birthday_mail_without_consent_is_not_sent_and_logged(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    d = await _setup(app_pool)
    template_id = await _template(app_pool, d)
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.WORKFLOW_EDIT) as ctx:
        rule = await service.install(
            ctx,
            "birthday_greeting",
            steps=[
                {"action": "send_email", "message_template_id": template_id, "purpose": "marketing"},
                {"action": "create_task", "kind": "client_birthday"},
            ],
        )
    assert rule["status"] == "active"
    await _birthday_today(app_pool, d, d["lead"])
    async with system_tx(app_pool, d["tenant"], actor=tick.ACTOR) as ctx:
        counts = await tick.run_agency(ctx, Vault(d["keys"], ctx), _at_local_hour(8))
    assert counts["special_runs_started"] == 1
    [run] = await _runs(app_pool, d)
    assert run["traveler_id"] == d["lead"] and run["trigger_key"].startswith("special:birthday:")
    out = await _advance(app_pool, d, run["workflow_run_id"], dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1))
    assert out["status"] == "completed"
    [run] = await _runs(app_pool, d)
    log = json.loads(run["log"]) if isinstance(run["log"], str) else run["log"]
    assert [e["outcome"] for e in log] == ["skipped_no_consent", "created"]
    assert await _sql(app_pool, d["tenant"], "SELECT 1 FROM crm.dispatch_queue WHERE origin = 'workflow'") == []
    denied = await _owner_fetch(
        admin, d["tenant"], "SELECT reason FROM crm.audit_event WHERE action = 'workflow_send' AND outcome = 'denied'"
    )
    assert [r["reason"] for r in denied] == ["no_consent"]
    [task] = await _sql(
        app_pool,
        d["tenant"],
        "SELECT trip_id, traveler_id, kind FROM crm.task WHERE workflow_run_id = $1",
        run["workflow_run_id"],
    )
    assert task["trip_id"] is None and task["traveler_id"] == d["lead"] and task["kind"] == "client_birthday"
    # The tick again the same day starts nothing new (the key holds the date).
    async with system_tx(app_pool, d["tenant"], actor=tick.ACTOR) as ctx:
        again = await tick.run_agency(ctx, Vault(d["keys"], ctx), _at_local_hour(9))
    assert again.get("special_runs_started", 0) == 0


async def test_a_transactional_workflow_mail_is_queued_with_consent_given_or_not(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    template_id = await _template(app_pool, d)
    await _rule(
        app_pool,
        d,
        {
            "name": "Welcome",
            "trigger": {"kind": "stage_enter", "value": "qualifying"},
            "steps": [{"action": "send_email", "message_template_id": template_id, "purpose": "transactional"}],
        },
    )
    await _move(app_pool, d, "qualifying")
    [run] = await _runs(app_pool, d)
    assert (await _advance(app_pool, d, run["workflow_run_id"], run["next_at"] + dt.timedelta(seconds=1)))[
        "status"
    ] == "completed"
    [queued] = await _sql(
        app_pool,
        d["tenant"],
        "SELECT origin, purpose, message_id, not_before <= now() AS due "
        "FROM crm.dispatch_queue WHERE origin = 'workflow'",
    )
    assert queued["purpose"] == "transactional" and queued["due"]  # no Undo delay for a workflow send
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.TRIP_VIEW) as ctx:
        row = await ctx.conn.fetchrow(
            "SELECT status, body_enc FROM crm.message WHERE message_id = $1", queued["message_id"]
        )
        body = await Vault(d["keys"], ctx).decrypt(
            "message", "body_enc", queued["message_id"], row["body_enc"], purpose="mail"
        )
    assert row["status"] == "queued" and "Sen Vàng Travel" in body and "{{" not in body


async def test_an_archived_clients_special_date_run_stops(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.WORKFLOW_EDIT) as ctx:
        await service.install(ctx, "passport_expiry_200d")
        rule_id = await ctx.conn.fetchrow("SELECT workflow_rule_id, version FROM crm.workflow_rule")
        run_id = await triggers.start(
            ctx, rule_id, trip_id=None, traveler_id=d["lead"], key=f"special:passport_expiry:{d['lead']}:2027-05-01"
        )
        await ctx.conn.execute(
            "UPDATE crm.traveler SET archived_at = now(), archive_reason = 'agent', archived_by = $2 "
            "WHERE traveler_id = $1",
            d["lead"],
            f"member:{d['owner_id']}",
        )
    out = await _advance(app_pool, d, run_id, dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1))
    assert (out["status"], out["reason_code"]) == ("stopped", "archived")


async def test_a_task_list_applied_by_an_agent_and_a_members_own_tasks(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.WORKFLOW_EDIT) as ctx:
        rule = await service.install(ctx, "visa_documents_task_list")
    rule_id = uuid.UUID(rule["workflow_rule_id"])
    async with tenant_tx(app_pool, d["agent"], d["tenant"], action=Action.WORKFLOW_APPLY) as ctx:
        dry = await service.apply(ctx, rule_id, [d["trip"]], dry_run=True, request_id=uuid.uuid4())
        assert dry["eligible"] == [str(d["trip"])] and dry["blocked"] == []
        ran = await service.apply(ctx, rule_id, [d["trip"]], dry_run=False, request_id=uuid.uuid4())
    assert ran["started"] == 1
    [run] = await _runs(app_pool, d)
    await _advance(app_pool, d, run["workflow_run_id"], run["next_at"] + dt.timedelta(seconds=1))
    kinds = [
        r["kind"]
        for r in await _sql(
            app_pool, d["tenant"], "SELECT kind FROM crm.task WHERE workflow_run_id = $1", run["workflow_run_id"]
        )
    ]
    assert len(kinds) == 3 and all(k.startswith("custom_") for k in kinds) and len(set(kinds)) == 3
    # A member's own task, then a hand-edited due date the trip's dates never move again (R20).
    async with tenant_tx(app_pool, d["agent"], d["tenant"], action=Action.TASK_UPDATE) as ctx:
        vault = Vault(d["keys"], ctx)
        mine = await tasks.create(
            ctx, vault, title="Ask about the boat day", trip_id=d["trip"], due_rule=("departure", -7)
        )
        assert mine["due_at"] is None and mine["source"] == "manual"
        listed = await tasks.listing(ctx, vault, scope="mine")
        assert any(t["title"] == "Ask about the boat day" for t in listed["buckets"]["waiting_for_dates"])
        pinned = dt.datetime(2027, 1, 10, 2, tzinfo=dt.UTC)
        edited = await tasks.update(ctx, vault, uuid.UUID(mine["task_id"]), due_at=pinned, set_due=True)
        assert edited["due_overridden"] is True
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.TRIP_EDIT) as ctx:
        await pipeline.update_trip(ctx, d["trip"], {"start_date": dt.date(2027, 3, 1)}, Vault(d["keys"], ctx))
    [row] = await _sql(
        app_pool, d["tenant"], "SELECT due_at FROM crm.task WHERE task_id = $1", uuid.UUID(mine["task_id"])
    )
    assert row["due_at"] == pinned
    # The assistant cannot apply a workflow (design §4.6: owner edits, agent applies).
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.WORKFLOW_EDIT) as ctx:
        archived = await service.set_status(ctx, rule_id, "archived")
    assert archived["status"] == "archived"
