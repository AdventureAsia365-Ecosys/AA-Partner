"""The workflow engine (AA-959; design v3 M10): advance one run through its due steps, in the job's context
(:func:`app.tenancy.system_tx`, actor ``job:crm_workflow_run``; :mod:`app.workflows.runner`).

:func:`advance` locks the run and, while it is due, executes its steps in order:

- **create_task**: one ``crm.task`` on the trip (or the client): the coded kind, or a custom task whose title is sealed
  under the vault DEK (kind ``custom_<hex>`` fixed per rule and step, so a step never opens a second open task on the
  trip); due = its anchor + offset (a date the trip has not got yet leaves ``due_at`` NULL: "Waiting for dates", R20;
  the tick dates it when the trip gets one); assignee = the trip's (the client's) owner, or the member the step names
  while that member is active.
- **send_email**: the agency's message template merged for the trip (``{{traveller_first_name}}``, ``{{trip_name}}``
  ..., :mod:`app.proposals.text_render`), stored as a queued ``crm.message`` from the trip owner's mailbox (else the
  agency's) and one dispatch row ``origin workflow`` (no Undo delay, design §3.5). A missing or disconnected mailbox
  holds it (``held_reconnect``, errata S247 §1: never sent another way). A **marketing** send to a client whose latest
  marketing consent is not a yes is **not sent**: the step is logged ``skipped_no_consent`` and audited ``denied``
  (card R: "a birthday mail without consent -> not sent, logged"); the dispatcher's gate checks it again at send time.
- **send_text**: an approved SMS / WhatsApp template of the agency's own Twilio (a workflow never sends free text: the
  WhatsApp 24 h window, AA-958), its variables from merge fields; the same consent rule for a marketing template; a
  client without a number, opted out, or a channel not connected: the step is skipped with that code.
- **set_field**: the trip's ``waiting_on``, or its owner (an active member).
- **wait**: ``next_at`` moves on (days / hours, or a date of the trip + offset at 09:00 local). The run's job is not
  queued ahead (the DB Migrate idle check counts a queued row as busy): the hourly tick queues the run again once
  ``next_at`` has passed. A wait for a date the trip has not got yet re-checks a day later.

Before each step the run stops itself when it may not go on: the rule archived (``rule_archived``; paused = the run
waits an hour and checks again), the trip lost (unless the rule starts on Lost), trashed, cancelled or cancelling, the
client trashed or archived (``archived``: a workflow on an archived contact is suppressed). The log keeps one entry per
step: step, action, outcome code, ids. A step that fails in the database marks the run ``failed`` (``step_failed``)
instead of retrying it for ever; anything else (a key or network error) is raised so the job is retried.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from app.dispatch import queue as dispatch
from app.errors import Conflict, NotFound, ServiceError
from app.mail import threads
from app.messaging import compose as text_compose
from app.messaging import state as text_state
from app.messaging.providers import placeholders, render
from app.proposals import text_render
from app.tenancy import AnyContext
from app.vault import Vault
from app.vault.blind_index import NormalisationError, normalise_phone
from app.workflows import facts as wf_facts
from app.workflows import model

log = logging.getLogger(__name__)

ACTOR = "job:crm_workflow_run"
MAX_LOG = 40
PAUSED_RECHECK = dt.timedelta(hours=1)
NO_DATE_RECHECK = dt.timedelta(days=1)
MAIL_PURPOSE = "mail"

RUN_SQL = """
SELECT r.workflow_run_id, r.workflow_rule_id, r.trip_id, r.traveler_id, r.trigger_key, r.status, r.step_index,
       r.next_at, r.log, w.status AS rule_status, w.steps, w.trigger_kind, w.trigger_value
  FROM crm.workflow_run r JOIN crm.workflow_rule w ON w.workflow_rule_id = r.workflow_rule_id
 WHERE r.workflow_run_id = $1
   FOR UPDATE OF r
"""
SAVE_SQL = """
UPDATE crm.workflow_run SET step_index = $2, next_at = $3, log = $4::jsonb WHERE workflow_run_id = $1
"""
FINISH_SQL = """
UPDATE crm.workflow_run SET status = $2, stop_reason = $3, step_index = $4, next_at = NULL, finished_at = now(),
       log = $5::jsonb
 WHERE workflow_run_id = $1
"""
TRIP_INFO_SQL = "SELECT title FROM crm.trip WHERE trip_id = $1"
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
NAME_SQL = "SELECT given_name_enc, display_name_enc FROM crm.traveler WHERE traveler_id = $1"
TASK_TRIP_SQL = """
INSERT INTO crm.task (task_id, trip_id, kind, priority, assignee_member_id, due_anchor, due_offset_days, due_at,
                      title_enc, workflow_run_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
RETURNING task_id
"""
TASK_CLIENT_SQL = """
INSERT INTO crm.task (task_id, traveler_id, kind, priority, assignee_member_id, due_anchor, due_offset_days, due_at,
                      title_enc, workflow_run_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
RETURNING task_id
"""
TEMPLATE_SQL = """
SELECT message_template_id, locale, title, body_enc FROM crm.message_template
 WHERE message_template_id = $1 AND deleted_at IS NULL
"""
MAILBOX_SQL = """
SELECT channel_account_id FROM crm.channel_account
 WHERE deleted_at IS NULL AND (member_id = $1 OR member_id IS NULL)
 ORDER BY member_id NULLS LAST
 LIMIT 1
"""
CONSENT_SQL = """
SELECT c.value FROM crm.consent c WHERE c.traveler_id = $1 AND c.purpose = 'marketing'
 ORDER BY c.at DESC, c.created_at DESC, c.consent_id DESC
 LIMIT 1
"""
MAIL_SQL = """
INSERT INTO crm.message (message_id, conversation_id, direction, status, sender_kind, channel_account_id, subject_enc,
                         body_enc, match_method, occurred_at)
VALUES ($1, $2, 'out', 'queued', 'agency_mailbox', $3, $4, $5, 'compose', now())
"""
TEXT_SQL = """
INSERT INTO crm.message (message_id, conversation_id, direction, status, channel, sender_kind, body_enc, from_enc,
                         to_enc, match_method, occurred_at)
VALUES ($1, $2, 'out', 'queued', $3, 'agency_mailbox', $4, $5, $6, 'compose', now())
"""
DELIVERY_SQL = """
INSERT INTO crm.message_delivery (message_id, channel, content_sid, variables_enc) VALUES ($1, $2, $3, $4)
"""
TOUCH_CONVERSATION_SQL = "UPDATE crm.conversation SET last_message_at = now() WHERE conversation_id = $1"
CLIENT_STATE_SQL = """
SELECT archived_at IS NOT NULL AS archived, deleted_at IS NOT NULL AS deleted FROM crm.traveler WHERE traveler_id = $1
"""
PHONE_SQL = "SELECT phone_enc FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL"
WAITING_ON_SQL = "UPDATE crm.trip SET waiting_on = $2 WHERE trip_id = $1 AND waiting_on IS DISTINCT FROM $2"
OWNER_SQL = "UPDATE crm.trip SET owner_member_id = $2 WHERE trip_id = $1 AND owner_member_id IS DISTINCT FROM $2"


@dataclass
class Step:
    """What one step did: the log entry, and whether the run waits (until ``wait_until``) or stops (``stop``)."""

    outcome: str
    ids: dict[str, str] = field(default_factory=dict)
    wait_until: dt.datetime | None = None
    advance: bool = True
    stop: str | None = None


@dataclass
class Subject:
    trip: model.TripFacts | None
    traveler_id: uuid.UUID | None
    owner: uuid.UUID | None


def _log(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    return list(raw or [])


def _steps(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    return list(raw or [])


def _entry(n: int, action: str, step: Step, now: dt.datetime) -> dict[str, Any]:
    return {"step": n, "action": action, "outcome": step.outcome, "at": now.isoformat(), **step.ids}


def _dump(log_rows: list[dict[str, Any]]) -> str:
    return json.dumps(log_rows[-MAX_LOG:], separators=(",", ":"))


async def _subject(ctx: AnyContext, run: Any) -> tuple[Subject, str | None]:
    """The run's trip or client, and why the run may not go on (None = it may)."""
    if run["trip_id"] is not None:
        facts = await wf_facts.trip(ctx, run["trip_id"])
        reason = model.stop_reason(facts)
        if reason == "trip_lost" and run["trigger_kind"] == "stage_enter" and run["trigger_value"] == "lost":
            reason = None  # a rule that starts on Lost runs on the lost trip
        return Subject(facts, facts.lead_id if facts else None, facts.owner_member_id if facts else None), reason
    client = await wf_facts.client(ctx, run["traveler_id"])
    if client is None or client["deleted"]:
        return Subject(None, run["traveler_id"], None), "client_trashed"
    if client["archived"]:
        return Subject(None, run["traveler_id"], client["owner_member_id"]), "archived"
    return Subject(None, run["traveler_id"], client["owner_member_id"]), None


async def merge_values(ctx: AnyContext, vault: Vault, subject: Subject, locale: str) -> dict[str, str | None]:
    """The merge fields of a workflow message (text_render.MERGE_FIELDS; agent_name and proposal stay empty)."""
    values: dict[str, str | None] = dict.fromkeys(text_render.MERGE_FIELDS)
    values["agency_name"] = await ctx.conn.fetchval(AGENCY_SQL, ctx.tenant_id)
    if subject.traveler_id is not None:
        row = await ctx.conn.fetchrow(NAME_SQL, subject.traveler_id)
        if row is not None:
            first = None
            if row["given_name_enc"] is not None:
                first = await vault.decrypt("traveler", "given_name_enc", subject.traveler_id, row["given_name_enc"])
            elif row["display_name_enc"] is not None:
                name = await vault.decrypt("traveler", "display_name_enc", subject.traveler_id, row["display_name_enc"])
                first = (name.split() or [None])[0]
            values["traveller_first_name"] = first
    if subject.trip is not None:
        values["trip_name"] = await ctx.conn.fetchval(TRIP_INFO_SQL, subject.trip.trip_id)
        if subject.trip.destinations:
            values["destination"] = ", ".join(d.replace("_", " ").title() for d in subject.trip.destinations)
        if subject.trip.start_date is not None:
            lang = locale if locale in text_render.LOCALES else "en"
            values["departure_date"] = text_render.date(subject.trip.start_date, lang, weekday=False)
    return values


async def _marketing_ok(ctx: AnyContext, traveler_id: uuid.UUID) -> bool:
    return await ctx.conn.fetchval(CONSENT_SQL, traveler_id) is True


async def _client_ok(ctx: AnyContext, traveler_id: uuid.UUID | None) -> str | None:
    if traveler_id is None:
        return "skipped_no_client"
    row = await ctx.conn.fetchrow(CLIENT_STATE_SQL, traveler_id)
    if row is None or row["deleted"]:
        return "skipped_no_client"
    if row["archived"]:
        return "skipped_archived"
    return None


async def _create_task(
    ctx: AnyContext, vault: Vault, run: Any, n: int, step: dict[str, Any], subject: Subject, tz: ZoneInfo,
    now: dt.datetime,
) -> Step:  # fmt: skip
    kind = model.step_kind(step["kind"], run["workflow_rule_id"], n)
    task_id = uuid.uuid4()
    title_enc = await vault.encrypt("task", "title_enc", task_id, step["title"]) if model.is_custom(kind) else None
    assignee = subject.owner
    if step.get("assignee", "owner") != "owner":
        named = uuid.UUID(step["assignee"].removeprefix("member:"))
        if await wf_facts.active_member(ctx, named):
            assignee = named
    due = step.get("due") or {"anchor": "created", "offset_days": 0}
    due_at = model.due_at(due, subject.trip, tz, now)
    args = (task_id, run["trip_id"] or run["traveler_id"], kind, step.get("priority", "normal"), assignee,
            due["anchor"], due.get("offset_days", 0), due_at, title_enc, run["workflow_run_id"])  # fmt: skip
    created = await ctx.conn.fetchval(TASK_TRIP_SQL if run["trip_id"] is not None else TASK_CLIENT_SQL, *args)
    if created is None:
        return Step("already_open")
    ctx.audit("task_create", "allowed", "task", str(task_id), reason="workflow")
    return Step("created", {"task_id": str(task_id)})


async def _send_email(ctx: AnyContext, vault: Vault, run: Any, step: dict[str, Any], subject: Subject) -> Step:
    traveler_id = subject.traveler_id
    refused = await _client_ok(ctx, traveler_id)
    if refused or traveler_id is None:
        return Step(refused or "skipped_no_client")
    template = await ctx.conn.fetchrow(TEMPLATE_SQL, uuid.UUID(step["message_template_id"]))
    if template is None:
        return Step("skipped_template_missing")
    purpose = step.get("purpose", "transactional")
    if purpose == "marketing" and not await _marketing_ok(ctx, traveler_id):
        ctx.audit("workflow_send", "denied", "workflow_run", str(run["workflow_run_id"]), reason="no_consent")
        return Step("skipped_no_consent")
    body = await vault.decrypt("message_template", "body_enc", template["message_template_id"], template["body_enc"],
                               purpose=MAIL_PURPOSE)  # fmt: skip
    values = await merge_values(ctx, vault, subject, template["locale"])
    text, _ = text_render.merge(body, values)
    title, _ = text_render.merge(template["title"], values)
    message_id = uuid.uuid4()
    trip_id = subject.trip.trip_id if subject.trip else None
    conversation_id = await threads.conversation_for(ctx, trip_id, traveler_id)
    mailbox = await ctx.conn.fetchval(MAILBOX_SQL, subject.owner)
    subject_enc = await vault.encrypt("message", "subject_enc", message_id, title or template["title"],
                                      purpose=MAIL_PURPOSE)  # fmt: skip
    body_enc = await vault.encrypt("message", "body_enc", message_id, text or body, purpose=MAIL_PURPOSE)
    await ctx.conn.execute(MAIL_SQL, message_id, conversation_id, mailbox, subject_enc, body_enc)
    await ctx.conn.execute(TOUCH_CONVERSATION_SQL, conversation_id)
    try:
        await dispatch.enqueue_message(ctx, traveler_id=traveler_id, channel="email", purpose=purpose,
                                       origin="workflow", trip_id=trip_id, message_id=message_id)  # fmt: skip
    except (Conflict, NotFound) as exc:
        raise _Undo(f"skipped_{exc.code}") from None
    ctx.audit("message_send", "allowed", "message", str(message_id), reason=f"workflow_{purpose}")
    return Step("queued", {"message_id": str(message_id)})


async def _send_text(ctx: AnyContext, vault: Vault, run: Any, step: dict[str, Any], subject: Subject) -> Step:
    traveler_id = subject.traveler_id
    refused = await _client_ok(ctx, traveler_id)
    if refused or traveler_id is None:
        return Step(refused or "skipped_no_client")
    channel = step["channel"]
    connection = await text_state.connection(ctx)
    sender = text_state.sender_for(connection, channel) if connection is not None else None
    if connection is None or connection["credential_status"] == "disconnected" or not sender:
        return Step("skipped_channel_not_connected")
    blob = await ctx.conn.fetchval(PHONE_SQL, traveler_id)
    phone = None
    if blob is not None:
        try:
            phone = normalise_phone(await vault.decrypt("traveler", "phone_enc", traveler_id, blob))
        except NormalisationError:
            phone = None
    if phone is None:
        return Step("skipped_no_client_phone")
    trip_id = subject.trip.trip_id if subject.trip else None
    conversation_id = await text_compose.conversation_for(ctx, channel, trip_id, traveler_id)  # type: ignore[arg-type]
    if await text_state.opted_out(ctx, conversation_id, traveler_id, channel):
        return Step("skipped_client_opted_out")
    template = await ctx.conn.fetchrow(text_compose.TEMPLATE_SQL, step["content_sid"])
    if template is None:
        return Step("skipped_template_not_approved")
    purpose = "marketing" if text_compose.is_marketing(template["category"]) else "transactional"
    if purpose == "marketing" and not await _marketing_ok(ctx, traveler_id):
        ctx.audit("workflow_send", "denied", "workflow_run", str(run["workflow_run_id"]), reason="no_consent")
        return Step("skipped_no_consent")
    merged = await merge_values(ctx, vault, subject, "en")
    names = list(template["variables"] or []) or list(placeholders(template["body"]))
    mapping = step.get("variables") or {}
    values = {name: merged.get(mapping.get(name, "")) or "" for name in names}
    if any(not v.strip() for v in values.values()):
        return Step("skipped_template_variables_missing")
    message_id = uuid.uuid4()
    text = render(template["body"], values)
    body_enc = await vault.encrypt("message", "body_enc", message_id, text, purpose=MAIL_PURPOSE)
    from_enc = await vault.encrypt("message", "from_enc", message_id, sender, purpose=MAIL_PURPOSE)
    to_enc = await vault.encrypt("message", "to_enc", message_id, json.dumps([phone]), purpose=MAIL_PURPOSE)
    variables_enc = (
        await vault.encrypt("message_delivery", "variables_enc", message_id, json.dumps(values), purpose=MAIL_PURPOSE)
        if values
        else None
    )
    await ctx.conn.execute(TEXT_SQL, message_id, conversation_id, channel, body_enc, from_enc, to_enc)
    await ctx.conn.execute(DELIVERY_SQL, message_id, channel, step["content_sid"], variables_enc)
    await ctx.conn.execute(TOUCH_CONVERSATION_SQL, conversation_id)
    try:
        await dispatch.enqueue_message(ctx, traveler_id=traveler_id, channel=channel, purpose=purpose,
                                       origin="workflow", trip_id=trip_id, message_id=message_id)  # fmt: skip
    except (Conflict, NotFound) as exc:
        raise _Undo(f"skipped_{exc.code}") from None
    ctx.audit("message_send", "allowed", "message", str(message_id), reason=f"workflow_{channel}_{purpose}")
    return Step("queued", {"message_id": str(message_id)})


async def _set_field(ctx: AnyContext, step: dict[str, Any], subject: Subject) -> Step:
    if subject.trip is None:
        return Step("skipped_no_trip")
    trip_id = subject.trip.trip_id
    if step["field"] == "waiting_on":
        await ctx.conn.execute(WAITING_ON_SQL, trip_id, step["value"])
        ctx.audit("trip_update", "allowed", "trip", str(trip_id), fields=("waiting_on",), reason="workflow")
        return Step("set")
    member = uuid.UUID(step["value"].removeprefix("member:"))
    if not await wf_facts.active_member(ctx, member):
        return Step("skipped_member_inactive")
    await ctx.conn.execute(OWNER_SQL, trip_id, member)
    ctx.audit("trip_assign", "allowed", "trip", str(trip_id), reason="workflow")
    return Step("set")


def _wait(step: dict[str, Any], subject: Subject, tz: ZoneInfo, now: dt.datetime) -> Step:
    if "until" in step:
        anchor, days = step["until"]["anchor"], step["until"].get("offset_days", 0)
        day = model.anchor_date(anchor, subject.trip, tz) if subject.trip else None
        if day is None:
            return Step("waiting_for_dates", wait_until=now + NO_DATE_RECHECK, advance=False)
        until = model.at_local(day + dt.timedelta(days=days), tz)
    else:
        until = now + dt.timedelta(days=step.get("days", 0), hours=step.get("hours", 0))
    if until <= now:
        return Step("passed")
    return Step("waiting", wait_until=until)


class _Undo(Exception):
    """A step refused after it wrote something: its savepoint is rolled back and the step is logged with the code."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


async def _execute(
    ctx: AnyContext, vault: Vault, run: Any, n: int, step: dict[str, Any], subject: Subject, tz: ZoneInfo,
    now: dt.datetime,
) -> Step:  # fmt: skip
    action = step.get("action")
    if action == "wait":
        return _wait(step, subject, tz, now)
    if action == "create_task":
        return await _create_task(ctx, vault, run, n, step, subject, tz, now)
    if action == "send_email":
        if not step.get("message_template_id"):
            return Step("skipped_template_missing")
        return await _send_email(ctx, vault, run, step, subject)
    if action == "send_text":
        if not step.get("content_sid"):
            return Step("skipped_template_missing")
        return await _send_text(ctx, vault, run, step, subject)
    if action == "set_field":
        return await _set_field(ctx, step, subject)
    return Step("skipped_unknown_action")


async def advance(
    ctx: AnyContext, vault: Vault, run_id: uuid.UUID, *, now: dt.datetime | None = None
) -> dict[str, Any]:
    """Run the due steps of one run. Returns ids and codes (the job result)."""
    now = now or dt.datetime.now(dt.UTC)
    run = await ctx.conn.fetchrow(RUN_SQL, run_id)
    out: dict[str, Any] = {"workflow_run_id": str(run_id)}
    if run is None:
        return {**out, "status": "missing"}
    if run["status"] != "active":
        return {**out, "status": run["status"]}
    if run["next_at"] is not None and run["next_at"] > now + dt.timedelta(seconds=1):
        return {**out, "status": "not_due"}
    log_rows = _log(run["log"])
    index = int(run["step_index"])
    if run["rule_status"] == "archived":
        return await _finish(ctx, run_id, "stopped", "rule_archived", index, log_rows, out)
    if run["rule_status"] == "paused":
        await ctx.conn.execute(SAVE_SQL, run_id, index, now + PAUSED_RECHECK, _dump(log_rows))
        return {**out, "status": "paused"}
    subject, reason = await _subject(ctx, run)
    if reason is not None:
        return await _finish(ctx, run_id, "stopped", reason, index, log_rows, out)
    tz = await wf_facts.timezone(ctx)
    steps = _steps(run["steps"])
    done = 0
    while index < len(steps):
        step = steps[index]
        try:
            async with ctx.conn.transaction():
                result = await _execute(ctx, vault, run, index, step, subject, tz, now)
        except _Undo as exc:
            result = Step(exc.outcome)
        except (asyncpg.PostgresError, ServiceError) as exc:
            code = getattr(exc, "code", None) or type(exc).__name__
            log.error("workflow_step_failed action=%s error=%s", step.get("action"), code)
            log_rows.append(_entry(index, str(step.get("action")), Step("failed"), now))
            return await _finish(ctx, run_id, "failed", "step_failed", index, log_rows, out)
        log_rows.append(_entry(index, str(step.get("action")), result, now))
        done += 1
        if result.stop:
            return await _finish(ctx, run_id, "stopped", result.stop, index, log_rows, out)
        if result.wait_until is not None:
            index = index + 1 if result.advance else index
            await ctx.conn.execute(SAVE_SQL, run_id, index, result.wait_until, _dump(log_rows))
            return {**out, "status": "waiting", "steps_done_count": done, "step_index": index}
        index += 1
    return await _finish(ctx, run_id, "completed", None, index, log_rows, {**out, "steps_done_count": done})


async def _finish(
    ctx: AnyContext, run_id: uuid.UUID, status: str, reason: str | None, index: int, log_rows: list[dict[str, Any]],
    out: dict[str, Any],
) -> dict[str, Any]:  # fmt: skip
    await ctx.conn.execute(FINISH_SQL, run_id, status, reason, min(index, model.MAX_STEPS), _dump(log_rows))
    ctx.audit("workflow_" + ("complete" if status == "completed" else "stop"), "allowed", "workflow_run",
              str(run_id), reason=reason)  # fmt: skip
    result = {**out, "status": status}
    if reason:
        result["reason_code"] = reason
    return result
