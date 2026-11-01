"""Automations (A-17) for the routes (AA-959): the agency's rules, the AA library, preview, manual apply, runs.

- **Read** (every role): the rules (trigger, conditions, steps, status, version, how many runs are active / done),
  the library (:mod:`app.workflows.library`) with what is already installed, and the runs of a trip or a client
  (status, the step log as codes).
- **Write** (owner, ``WORKFLOW_EDIT``; design §4.6 "Edit workflows / automations: owner"): create a rule, install a
  library template (paused when a send step still needs its message template), edit (``version`` must match: 409
  ``version_conflict``; +1), switch on / pause / archive (on: every send step has its template, 409
  ``templates_missing``; archived runs stop at their next step). The message templates a send step names must be the
  agency's (422 ``template_not_found``); a named member must be an active member (422 ``member_not_found``).
- **Apply** (owner, agent: ``WORKFLOW_APPLY``, "agent applies"): a manual rule (a task list, R9) or any trip rule to
  chosen trips. The dry run lists eligible and blocked trips with the reason code (U5 "apply a workflow (preview
  count)"); the run starts one run per eligible trip (``manual:<request id>:<trip>``), after the Undo window.
- **Preview** (every role): how many live trips a rule would apply to now (conditions met; for a date rule, the trips
  whose date is reached within the catch-up window), with a sample of trip ids.
- **Stop** (owner, agent): one active run stops (``stopped_by_member``).

Every function takes the open member context and never opens one.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.errors import Conflict, Invalid, NotFound
from app.permissions import Action, require
from app.tenancy import TenantContext
from app.workflows import facts as wf_facts
from app.workflows import library, model, triggers

MAX_RULES = 200
MAX_APPLY = 100
MAX_RUNS = 100
SAMPLE = 20

RULES_SQL = """
SELECT w.workflow_rule_id, w.name, w.template_key, w.template_version, w.trigger_kind, w.trigger_value,
       w.trigger_offset_days, w.conditions, w.steps, w.status, w.version, w.created_at, w.updated_at,
       (SELECT count(*) FROM crm.workflow_run r
         WHERE r.workflow_rule_id = w.workflow_rule_id AND r.status = 'active')::int AS active_runs,
       (SELECT count(*) FROM crm.workflow_run r
         WHERE r.workflow_rule_id = w.workflow_rule_id AND r.status <> 'active')::int AS finished_runs
  FROM crm.workflow_rule w
 WHERE w.deleted_at IS NULL AND ($1 OR w.status <> 'archived')
 ORDER BY w.status = 'archived', w.created_at, w.workflow_rule_id
 LIMIT $2
"""
RULE_SQL = """
SELECT workflow_rule_id, name, template_key, template_version, trigger_kind, trigger_value, trigger_offset_days,
       conditions, steps, status, version
  FROM crm.workflow_rule WHERE workflow_rule_id = $1 AND deleted_at IS NULL
"""
LOCK_SQL = RULE_SQL.strip() + "\n   FOR UPDATE"
INSERT_SQL = """
INSERT INTO crm.workflow_rule (name, template_key, template_version, trigger_kind, trigger_value, trigger_offset_days,
                               conditions, steps, status)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9)
RETURNING workflow_rule_id
"""
UPDATE_SQL = """
UPDATE crm.workflow_rule
   SET name = $2, trigger_kind = $3, trigger_value = $4, trigger_offset_days = $5, conditions = $6::jsonb,
       steps = $7::jsonb, version = version + 1
 WHERE workflow_rule_id = $1
RETURNING version
"""
STATUS_SQL = "UPDATE crm.workflow_rule SET status = $2 WHERE workflow_rule_id = $1"
INSTALLED_SQL = """
SELECT template_key FROM crm.workflow_rule
 WHERE template_key IS NOT NULL AND status <> 'archived' AND deleted_at IS NULL
"""
TEMPLATE_IDS_SQL = """
SELECT message_template_id FROM crm.message_template WHERE message_template_id = ANY($1::uuid[]) AND deleted_at IS NULL
"""
TEXT_TEMPLATE_SQL = """
SELECT content_sid FROM crm.channel_template WHERE content_sid = ANY($1::text[]) AND deleted_at IS NULL
"""
MEMBERS_SQL = """
SELECT member_id FROM crm.member WHERE member_id = ANY($1::uuid[]) AND status = 'active' AND deleted_at IS NULL
"""
LIVE_TRIPS_SQL = """
SELECT t.trip_id FROM crm.trip t
 WHERE t.deleted_at IS NULL AND t.stage NOT IN ('lost', 'cancelled')
 ORDER BY t.last_activity_at DESC, t.trip_id
 LIMIT 2000
"""
RUNS_SQL = """
SELECT r.workflow_run_id, r.workflow_rule_id, w.name AS rule_name, w.template_key, r.trip_id, r.traveler_id,
       r.trigger_key, r.status, r.step_index, r.next_at, r.stop_reason, r.log, r.created_at, r.finished_at,
       jsonb_array_length(w.steps) AS step_count
  FROM crm.workflow_run r JOIN crm.workflow_rule w ON w.workflow_rule_id = r.workflow_rule_id
 WHERE ($1::uuid IS NULL OR r.trip_id = $1) AND ($2::uuid IS NULL OR r.traveler_id = $2)
   AND ($3::uuid IS NULL OR r.workflow_rule_id = $3)
 ORDER BY r.created_at DESC, r.workflow_run_id
 LIMIT $4
"""
RUN_SQL = "SELECT workflow_run_id, trip_id, status FROM crm.workflow_run WHERE workflow_run_id = $1 FOR UPDATE"
STOP_SQL = """
UPDATE crm.workflow_run SET status = 'stopped', stop_reason = 'stopped_by_member', next_at = NULL, finished_at = now()
 WHERE workflow_run_id = $1 AND status = 'active'
"""
TRIP_OWNER_SQL = "SELECT owner_member_id FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"


def _json(raw: Any) -> Any:
    return json.loads(raw) if isinstance(raw, str) else raw


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def rule_view(r: Any) -> dict[str, Any]:
    steps = _json(r["steps"]) or []
    out = {
        "workflow_rule_id": str(r["workflow_rule_id"]),
        "name": r["name"],
        "template_key": r["template_key"],
        "template_version": r["template_version"],
        "trigger": {"kind": r["trigger_kind"], "value": r["trigger_value"], "offset_days": r["trigger_offset_days"]},
        "conditions": _json(r["conditions"]) or {},
        "steps": steps,
        "status": r["status"],
        "version": r["version"],
        "templates_missing": model.missing_templates(steps),
    }
    if "active_runs" in r.keys():
        out["active_runs"] = r["active_runs"]
        out["finished_runs"] = r["finished_runs"]
        out["updated_at"] = _iso(r["updated_at"])
    return out


async def overview(ctx: TenantContext, *, include_archived: bool = False) -> dict[str, Any]:
    rules = [rule_view(r) for r in await ctx.conn.fetch(RULES_SQL, include_archived, MAX_RULES)]
    installed = {r["template_key"] for r in await ctx.conn.fetch(INSTALLED_SQL)}
    lib = [{**t, "installed": t["key"] in installed} for t in library.listing()]
    can_edit = ctx.member_role == "owner"
    return {
        "rules": rules,
        "library": lib,
        "can_edit": can_edit,
        "can_apply": ctx.member_role in ("owner", "agent"),
        "options": {
            "trigger_kinds": list(model.TRIGGER_KINDS),
            "events": list(model.EVENTS),
            "date_anchors": list(model.DATE_ANCHORS),
            "special_dates": list(model.SPECIAL_DATES),
            "due_anchors": list(model.DUE_ANCHORS),
            "task_kinds": list(model.WORKFLOW_TASK_KINDS),
            "merge_fields": list(model.MERGE_FIELDS),
            "waiting_on": list(model.WAITING_ON),
            "condition_keys": list(model.CONDITION_KEYS),
        },
    }


async def _check_references(ctx: TenantContext, rule: model.Rule) -> None:
    """Message templates, text templates and members a rule names belong to this agency (RLS) and exist."""
    mail = {
        s["message_template_id"] for s in rule.steps if s["action"] == "send_email" and s.get("message_template_id")
    }
    if mail:
        found = {str(r["message_template_id"]) for r in await ctx.conn.fetch(TEMPLATE_IDS_SQL, list(mail))}
        if mail - found:
            raise Invalid("template_not_found", "a message template of this agency")
    texts = {s["content_sid"] for s in rule.steps if s["action"] == "send_text" and s.get("content_sid")}
    if texts:
        found = {r["content_sid"] for r in await ctx.conn.fetch(TEXT_TEMPLATE_SQL, list(texts))}
        if texts - found:
            raise Invalid("template_not_found", "an SMS / WhatsApp template of this agency")
    named: set[str] = set()
    for s in rule.steps:
        if s["action"] == "create_task" and s.get("assignee", "owner") != "owner":
            named.add(s["assignee"].removeprefix("member:"))
        if s["action"] == "set_field" and s["field"] == "owner":
            named.add(s["value"].removeprefix("member:"))
    if named:
        found = {str(r["member_id"]) for r in await ctx.conn.fetch(MEMBERS_SQL, [uuid.UUID(m) for m in named])}
        if named - found:
            raise Invalid("member_not_found", "an active member of this agency")


def _status_for(rule: model.Rule, wanted: str) -> str:
    if wanted == "active" and model.missing_templates(rule.steps):
        return "paused"
    return wanted


async def create(ctx: TenantContext, raw: dict[str, Any], *, status: str = "active") -> dict[str, Any]:
    require(ctx, Action.WORKFLOW_EDIT, object_kind="workflow_rule")
    if status not in ("active", "paused"):
        raise Invalid("invalid_status", "active or paused")
    rule = model.validate(raw, draft=True)
    if status == "active" and model.missing_templates(rule.steps):
        raise Conflict(
            "templates_missing",
            "pick the message template of every send step",
            steps=model.missing_templates(rule.steps),
        )
    await _check_references(ctx, rule)
    rule_id = await ctx.conn.fetchval(
        INSERT_SQL, rule.name, None, None, rule.trigger_kind, rule.trigger_value, rule.trigger_offset_days,
        json.dumps(rule.conditions), json.dumps(rule.steps), status,
    )  # fmt: skip
    ctx.audit("workflow_rule_create", "allowed", "workflow_rule", str(rule_id))
    return await get(ctx, rule_id)


async def install(
    ctx: TenantContext, template_key: str, *, name: str | None = None, steps: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Install a library template as the agency's rule. ``steps`` may replace the template's (the same count and
    actions: the owner picked the message templates or edited the titles; a send step keeps the template's purpose, so
    a marketing greeting cannot be installed as transactional); without every message template the rule starts
    paused."""
    require(ctx, Action.WORKFLOW_EDIT, object_kind="workflow_rule")
    template = library.get(template_key)
    if template is None:
        raise NotFound("template_not_found")
    raw = template.rule()
    if name is not None:
        raw["name"] = name
    if steps is not None:
        if len(steps) != len(raw["steps"]) or any(
            (s or {}).get("action") != t["action"] for s, t in zip(steps, raw["steps"], strict=True)
        ):
            raise Invalid("steps_mismatch", "the template's steps, in order, with your choices")
        raw["steps"] = [
            {**s, "purpose": t["purpose"]} if "purpose" in t else dict(s)
            for s, t in zip(steps, raw["steps"], strict=True)
        ]
    rule = model.validate(raw, draft=True)
    await _check_references(ctx, rule)
    status = _status_for(rule, "active")
    if await ctx.conn.fetchval(
        "SELECT 1 FROM crm.workflow_rule WHERE template_key = $1 AND status <> 'archived' AND deleted_at IS NULL",
        template.key,
    ):
        raise Conflict("template_installed", "this library automation is already installed")
    rule_id = await ctx.conn.fetchval(
        INSERT_SQL, rule.name, template.key, template.version, rule.trigger_kind, rule.trigger_value,
        rule.trigger_offset_days, json.dumps(rule.conditions), json.dumps(rule.steps), status,
    )  # fmt: skip
    ctx.audit("workflow_rule_create", "allowed", "workflow_rule", str(rule_id), reason=f"library_{template.key}"[:60])
    return await get(ctx, rule_id)


async def get(ctx: TenantContext, rule_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(RULE_SQL, rule_id)
    if row is None:
        raise NotFound("workflow_not_found")
    return rule_view(row)


async def update(ctx: TenantContext, rule_id: uuid.UUID, raw: dict[str, Any], *, version: int) -> dict[str, Any]:
    require(ctx, Action.WORKFLOW_EDIT, object_kind="workflow_rule", object_id=str(rule_id))
    row = await ctx.conn.fetchrow(LOCK_SQL, rule_id)
    if row is None:
        raise NotFound("workflow_not_found")
    if row["status"] == "archived":
        raise Conflict("workflow_archived", "an archived automation is not edited")
    if row["version"] != version:
        raise Conflict("version_conflict", "someone changed this automation; reload it", version=row["version"])
    rule = model.validate(raw, draft=row["status"] == "paused")
    await _check_references(ctx, rule)
    new_version = await ctx.conn.fetchval(
        UPDATE_SQL, rule_id, rule.name, rule.trigger_kind, rule.trigger_value, rule.trigger_offset_days,
        json.dumps(rule.conditions), json.dumps(rule.steps),
    )  # fmt: skip
    ctx.audit("workflow_rule_edit", "allowed", "workflow_rule", str(rule_id), reason=f"v{new_version}")
    return await get(ctx, rule_id)


async def set_status(ctx: TenantContext, rule_id: uuid.UUID, status: str) -> dict[str, Any]:
    require(ctx, Action.WORKFLOW_EDIT, object_kind="workflow_rule", object_id=str(rule_id))
    if status not in model.STATUSES:
        raise Invalid("invalid_status", ", ".join(model.STATUSES))
    row = await ctx.conn.fetchrow(LOCK_SQL, rule_id)
    if row is None:
        raise NotFound("workflow_not_found")
    if row["status"] == status:
        return rule_view(row)
    if row["status"] == "archived":
        raise Conflict("workflow_archived", "an archived automation stays archived")
    if status == "active":
        missing = model.missing_templates(_json(row["steps"]) or [])
        if missing:
            raise Conflict("templates_missing", "pick the message template of every send step", steps=missing)
    await ctx.conn.execute(STATUS_SQL, rule_id, status)
    ctx.audit("workflow_rule_status", "allowed", "workflow_rule", str(rule_id), reason=status)
    return await get(ctx, rule_id)


def _rule_of(row: Any) -> model.Rule:
    return model.Rule(
        row["name"], row["trigger_kind"], row["trigger_value"], row["trigger_offset_days"],
        _json(row["conditions"]) or {}, _json(row["steps"]) or [],
    )  # fmt: skip


async def preview(ctx: TenantContext, rule_id: uuid.UUID) -> dict[str, Any]:
    """How many live trips the rule's conditions hold for now (for a date rule: also its date is reached)."""
    row = await ctx.conn.fetchrow(RULE_SQL, rule_id)
    if row is None:
        raise NotFound("workflow_not_found")
    rule = _rule_of(row)
    if rule.subject != "trip":
        return {"workflow_rule_id": str(rule_id), "subject": "client", "count": None, "sample": []}
    ids = [r["trip_id"] for r in await ctx.conn.fetch(LIVE_TRIPS_SQL)]
    all_facts = await wf_facts.trips(ctx, ids)
    tz = await wf_facts.timezone(ctx)
    today = wf_facts.today(tz, await ctx.conn.fetchval("SELECT now()"))
    eligible = []
    for trip_id in ids:
        facts = all_facts.get(trip_id)
        if facts is None or model.stop_reason(facts) or model.matches(rule.conditions, facts) is not None:
            continue
        if rule.trigger_kind == "date_anchor":
            day = model.anchor_date(rule.trigger_value or "", facts, tz)
            if day is None or (day.toordinal() + rule.trigger_offset_days) > today.toordinal():
                continue
        if rule.trigger_kind == "stage_enter" and facts.stage != rule.trigger_value:
            continue
        eligible.append(str(trip_id))
    return {"workflow_rule_id": str(rule_id), "subject": "trip", "count": len(eligible), "sample": eligible[:SAMPLE]}


async def apply(
    ctx: TenantContext, rule_id: uuid.UUID, trip_ids: list[uuid.UUID], *, dry_run: bool, request_id: uuid.UUID
) -> dict[str, Any]:
    """Apply a rule to chosen trips (owner, agent). Eligible = live, not cancelling, the conditions hold."""
    require(ctx, Action.WORKFLOW_APPLY, object_kind="workflow_rule", object_id=str(rule_id))
    if not trip_ids or len(trip_ids) > MAX_APPLY:
        raise Invalid("invalid_trips", f"1 to {MAX_APPLY} trips")
    row = await ctx.conn.fetchrow(RULE_SQL, rule_id)
    if row is None:
        raise NotFound("workflow_not_found")
    if row["status"] != "active":
        raise Conflict("workflow_not_active", "switch the automation on first")
    rule = _rule_of(row)
    if rule.subject != "trip":
        raise Conflict("workflow_not_for_trips", "a client's special-date automation runs on its date")
    all_facts = await wf_facts.trips(ctx, list(dict.fromkeys(trip_ids)))
    eligible: list[str] = []
    blocked: list[dict[str, str]] = []
    for trip_id in dict.fromkeys(trip_ids):
        facts = all_facts.get(trip_id)
        reason = (
            "trip_not_found" if facts is None else model.stop_reason(facts) or model.matches(rule.conditions, facts)
        )
        if reason:
            blocked.append({"trip_id": str(trip_id), "reason": reason})
        else:
            eligible.append(str(trip_id))
    out: dict[str, Any] = {"eligible": eligible, "blocked": blocked, "dry_run": dry_run}
    if dry_run:
        return out
    started = 0
    rule_row = {"workflow_rule_id": rule_id, "version": row["version"]}
    for trip_id in eligible:
        run = await triggers.start(ctx, rule_row, trip_id=uuid.UUID(trip_id), traveler_id=None,
                                   key=f"manual:{request_id}:{trip_id}", delay_s=triggers.UNDO_DELAY)  # fmt: skip
        started += run is not None
    ctx.audit("workflow_apply", "allowed", "workflow_rule", str(rule_id), object_ids=tuple(eligible)[:200])
    return {**out, "started": started, "undo_seconds": triggers.UNDO_DELAY}


def run_view(r: Any) -> dict[str, Any]:
    return {
        "workflow_run_id": str(r["workflow_run_id"]),
        "workflow_rule_id": str(r["workflow_rule_id"]),
        "rule_name": r["rule_name"],
        "template_key": r["template_key"],
        "trip_id": str(r["trip_id"]) if r["trip_id"] else None,
        "traveler_id": str(r["traveler_id"]) if r["traveler_id"] else None,
        "trigger": r["trigger_key"].split(":", 1)[0],
        "status": r["status"],
        "step_index": r["step_index"],
        "step_count": r["step_count"],
        "next_at": _iso(r["next_at"]),
        "stop_reason": r["stop_reason"],
        "log": _json(r["log"]) or [],
        "created_at": _iso(r["created_at"]),
        "finished_at": _iso(r["finished_at"]),
    }


async def runs(
    ctx: TenantContext, *, trip_id: uuid.UUID | None, traveler_id: uuid.UUID | None, rule_id: uuid.UUID | None
) -> dict[str, Any]:
    rows = await ctx.conn.fetch(RUNS_SQL, trip_id, traveler_id, rule_id, MAX_RUNS)
    return {"runs": [run_view(r) for r in rows]}


async def stop(ctx: TenantContext, run_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(RUN_SQL, run_id)
    if row is None:
        raise NotFound("run_not_found")
    require(ctx, Action.WORKFLOW_APPLY, object_kind="workflow_run", object_id=str(run_id))
    if row["status"] != "active":
        raise Conflict("run_not_active", "this run has already ended")
    await ctx.conn.execute(STOP_SQL, run_id)
    ctx.audit("workflow_stop", "allowed", "workflow_run", str(run_id), reason="stopped_by_member")
    return {"workflow_run_id": str(run_id), "status": "stopped"}
