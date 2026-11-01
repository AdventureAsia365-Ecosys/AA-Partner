"""The shape of a workflow rule (AA-959, card CRM-29a; design v3 M10, A-17): triggers, conditions, steps, and the pure
rules the engine and the API share (crm-038 ``crm.workflow_rule`` stores what :func:`validate` returns).

A rule is ``trigger`` + ``conditions`` + ``steps``:

- **Trigger** (:data:`TRIGGER_KINDS`): ``stage_enter`` (a stage of :data:`app.stages.STAGES`), ``event`` (a trip event:
  :data:`EVENTS`), ``date_anchor`` (a date of the trip, :data:`DATE_ANCHORS`, plus ``offset_days``), ``special_date``
  (a date of a client, :data:`SPECIAL_DATES`, plus ``offset_days``), ``manual`` (a task list a member applies, R9).
- **Conditions** (:data:`CONDITION_KEYS`, all optional, all must hold): ``stages``, ``trip_styles``, ``lead_sources``,
  ``destinations_any``, ``has_children``, ``has_aa_lines``, ``stage3_incomplete``. ``tags_any`` is CRM-29b's (AA-960):
  refused until tags exist (422 ``condition_not_available``) rather than accepted and silently ignored.
- **Steps** (1..:data:`MAX_STEPS`, :data:`ACTIONS`): ``create_task`` (a coded kind of :data:`WORKFLOW_TASK_KINDS` or a
  ``custom`` task with the agency's own title; due = anchor + offset; priority; assignee = the trip / client owner or
  one member), ``send_email`` (an agency message template, transactional or marketing), ``send_text`` (an approved
  SMS / WhatsApp template of the agency's Twilio, its variables mapped to merge fields), ``set_field`` (``waiting_on``,
  or ``owner`` = one member), ``wait`` (days / hours, or until a date of the trip).

Names and step titles are agency text (no client data by design: a rule runs for many trips); the editor says so.
Everything here is pure (no database): :func:`validate`, :func:`matches`, :func:`due_at`, :func:`step_kind`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from app.errors import Invalid
from app.proposals.text_render import MERGE_FIELDS
from app.stages import STAGES

TRIGGER_KINDS = ("stage_enter", "event", "date_anchor", "special_date", "manual")
# Trip events a rule may start on: the crm.metric_event names a client's action or the Booking / payment flow writes
# (app.proposals.events), and the AA-958 messaging seam (app.messaging.events).
EVENTS = (
    "proposal.viewed",
    "proposal.option_chosen",
    "proposal.change_requested",
    "proposal.accepted",
    "invoice.payment_claimed",
    "invoice.overdue",
    "invoice.unfulfillable",
    "message.received",
    "message.sent",
)
DATE_ANCHORS = ("departure", "return", "stage_entered", "last_activity", "created")
SPECIAL_DATES = ("birthday", "passport_expiry", "trip_anniversary")
# A trigger date anchor -> the due anchor of a task (crm-005 task_anchor_chk).
DUE_ANCHORS = ("created", "departure", "return", "stage_entered")
ACTIONS = ("create_task", "send_email", "send_text", "set_field", "wait")
PRIORITIES = ("normal", "urgent")
PURPOSES = ("transactional", "marketing")
TEXT_CHANNELS = ("sms", "whatsapp")
WAITING_ON = ("agency", "client", "aa", "supplier", "none")
TRIP_STYLES = ("relaxed", "balanced", "adventure")
STATUSES = ("active", "paused", "archived")
CONDITION_KEYS = (
    "stages",
    "trip_styles",
    "lead_sources",
    "destinations_any",
    "has_children",
    "has_aa_lines",
    "stage3_incomplete",
)
LATER_CONDITION_KEYS = ("tags_any",)  # CRM-29b (AA-960)
MAX_STEPS = 20
MAX_NAME = 120
MAX_TITLE = 120
MAX_OFFSET_DAYS = 400
MAX_TASK_OFFSET_DAYS = 365
MAX_WAIT_DAYS = 365
DUE_LOCAL_TIME = dt.time(9, 0)  # a task due on a date is due at 09:00 in the agency's timezone
CUSTOM_PREFIX = "custom_"
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_CONTENT_SID = re.compile(r"^HX[0-9a-f]{32}$")
_VARIABLE = re.compile(r"^[A-Za-z0-9_]{1,20}$")
_MEMBER = re.compile(r"^member:([0-9a-f-]{36})$")

# The coded task kinds a workflow step may open (labelled by the web: task.<kind>). Bound to *_TASK names so the
# AA-957 kind scan (tests/unit/test_task_kinds.py) sees them written.
ARRIVAL_FLIGHT_TASK = "confirm_arrival_flight"  # R44 step 1: T-48 h, with the flight status link
PASSPORT_TASK = "passport_expiring"  # R2: the passport expires within 200 days of today
BIRTHDAY_TASK = "client_birthday"  # R2
ANNIVERSARY_TASK = "trip_anniversary"  # R2: a year since a completed trip ended
PRE_TRAVEL_PACK_TASK = "send_pre_travel_pack"
SUPPLIER_CHECK_TASK = "confirm_supplier_bookings"
REVIEW_TASK = "request_review"
REPLY_MESSAGE_TASK = "reply_client_message"
QUIET_CLIENT_TASK = "follow_up_quiet_client"
WORKFLOW_TASK_KINDS = (
    ARRIVAL_FLIGHT_TASK,
    PASSPORT_TASK,
    BIRTHDAY_TASK,
    ANNIVERSARY_TASK,
    PRE_TRAVEL_PACK_TASK,
    SUPPLIER_CHECK_TASK,
    REVIEW_TASK,
    REPLY_MESSAGE_TASK,
    QUIET_CLIENT_TASK,
)


@dataclass(frozen=True)
class Rule:
    """A validated rule, ready for crm.workflow_rule."""

    name: str
    trigger_kind: str
    trigger_value: str | None
    trigger_offset_days: int
    conditions: dict[str, Any]
    steps: list[dict[str, Any]]

    @property
    def subject(self) -> str:
        """Whom a run of the rule is about: a client (special dates) or a trip (everything else)."""
        return "traveler" if self.trigger_kind == "special_date" else "trip"


def _err(code: str, where: str, message: str = "") -> Invalid:
    return Invalid(code, message or where, field=where)


def _name(value: Any, where: str, limit: int) -> str:
    if not isinstance(value, str):
        raise _err("invalid_text", where)
    clean = " ".join(value.split())
    if not clean:
        raise _err("text_required", where)
    if len(clean) > limit:
        raise _err("text_too_long", where, f"at most {limit} characters")
    if any(ord(c) < 32 for c in clean):
        raise _err("invalid_text", where)
    return clean


def clean_text(value: Any, where: str, limit: int) -> str:
    """A one-line agency text (a rule name, a task title): spaces collapsed, 1..limit characters, no control
    characters (422 with the field path)."""
    return _name(value, where, limit)


def _int(value: Any, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _err("invalid_number", where, f"a whole number from {low} to {high}")
    return value


def _codes(value: Any, where: str, allowed: tuple[str, ...] | None = None) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 20:
        raise _err("invalid_list", where, "a list of 1 to 20 codes")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _CODE.match(item) or (allowed is not None and item not in allowed):
            raise _err("invalid_code", where)
        if item not in out:
            out.append(item)
    return out


def _member(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _MEMBER.match(value):
        raise _err("invalid_assignee", where, "trip_owner or member:<id>")
    try:
        uuid.UUID(value.removeprefix("member:"))
    except ValueError:
        raise _err("invalid_assignee", where) from None
    return value


def _only(step: dict[str, Any], where: str, keys: set[str]) -> None:
    extra = set(step) - keys - {"action"}
    if extra:
        raise _err("unknown_field", where, ", ".join(sorted(extra)))


def _trigger(kind: Any, value: Any, offset: Any) -> tuple[str, str | None, int]:
    if kind not in TRIGGER_KINDS:
        raise _err("invalid_trigger", "trigger.kind", ", ".join(TRIGGER_KINDS))
    offset = 0 if offset is None else _int(offset, "trigger.offset_days", -MAX_OFFSET_DAYS, MAX_OFFSET_DAYS)
    allowed: dict[str, tuple[str, ...]] = {
        "stage_enter": STAGES,
        "event": EVENTS,
        "date_anchor": DATE_ANCHORS,
        "special_date": SPECIAL_DATES,
    }
    if kind == "manual":
        if value is not None:
            raise _err("invalid_trigger", "trigger.value", "a manual rule has no trigger value")
        return kind, None, 0
    if value not in allowed[kind]:
        raise _err("invalid_trigger", "trigger.value", ", ".join(allowed[kind]))
    if kind == "stage_enter" and value == "cancelled":
        raise _err("invalid_trigger", "trigger.value", "a cancel stops workflows; it does not start one")
    if kind not in ("date_anchor", "special_date") and offset != 0:
        raise _err("invalid_trigger", "trigger.offset_days", "only date triggers have an offset")
    return kind, value, offset


def _conditions(raw: Any, subject: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _err("invalid_conditions", "conditions")
    for key in raw:
        if key in LATER_CONDITION_KEYS:
            raise _err("condition_not_available", f"conditions.{key}", "tags arrive with a later release")
        if key not in CONDITION_KEYS:
            raise _err("unknown_field", f"conditions.{key}")
    if subject == "traveler" and raw:
        raise _err("invalid_conditions", "conditions", "a client's special date has no trip conditions")
    out: dict[str, Any] = {}
    if "stages" in raw:
        out["stages"] = _codes(raw["stages"], "conditions.stages", STAGES)
    if "trip_styles" in raw:
        out["trip_styles"] = _codes(raw["trip_styles"], "conditions.trip_styles", TRIP_STYLES)
    if "lead_sources" in raw:
        out["lead_sources"] = _codes(raw["lead_sources"], "conditions.lead_sources")
    if "destinations_any" in raw:
        out["destinations_any"] = _codes(raw["destinations_any"], "conditions.destinations_any")
    for key in ("has_children", "has_aa_lines", "stage3_incomplete"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise _err("invalid_condition", f"conditions.{key}", "true or false")
            out[key] = raw[key]
    return out


def _due(raw: Any, where: str, subject: str) -> dict[str, Any]:
    if raw is None:
        return {"anchor": "created", "offset_days": 0, "offset_hours": 0}
    if not isinstance(raw, dict) or set(raw) - {"anchor", "offset_days", "offset_hours"}:
        raise _err("invalid_due", where)
    anchor = raw.get("anchor", "created")
    if anchor not in DUE_ANCHORS or (subject == "traveler" and anchor != "created"):
        raise _err("invalid_due", f"{where}.anchor", ", ".join(DUE_ANCHORS if subject == "trip" else ("created",)))
    days = _int(raw.get("offset_days", 0), f"{where}.offset_days", -MAX_TASK_OFFSET_DAYS, MAX_TASK_OFFSET_DAYS)
    hours = _int(raw.get("offset_hours", 0), f"{where}.offset_hours", 0, 72)
    if hours and anchor != "created":
        raise _err("invalid_due", f"{where}.offset_hours", "hours only after the step runs (anchor created)")
    return {"anchor": anchor, "offset_days": days, "offset_hours": hours}


def _step(raw: Any, n: int, subject: str, *, draft: bool) -> dict[str, Any]:
    where = f"steps.{n}"
    if not isinstance(raw, dict):
        raise _err("invalid_step", where)
    action = raw.get("action")
    if action not in ACTIONS:
        raise _err("invalid_action", f"{where}.action", ", ".join(ACTIONS))
    if action == "create_task":
        _only(raw, where, {"kind", "title", "due", "priority", "assignee"})
        kind = raw.get("kind", "custom")
        if kind != "custom" and kind not in WORKFLOW_TASK_KINDS:
            raise _err("invalid_task_kind", f"{where}.kind", "custom or a library task")
        out: dict[str, Any] = {"action": action, "kind": kind}
        if kind == "custom":
            out["title"] = _name(raw.get("title"), f"{where}.title", MAX_TITLE)
        elif raw.get("title") is not None:
            raise _err("unknown_field", f"{where}.title", "a library task is worded by the app")
        out["due"] = _due(raw.get("due"), f"{where}.due", subject)
        priority = raw.get("priority", "normal")
        if priority not in PRIORITIES:
            raise _err("invalid_priority", f"{where}.priority")
        out["priority"] = priority
        assignee = raw.get("assignee", "owner")
        out["assignee"] = "owner" if assignee in ("owner", "trip_owner") else _member(assignee, f"{where}.assignee")
        return out
    if action == "send_email":
        _only(raw, where, {"message_template_id", "purpose"})
        template = raw.get("message_template_id")
        if template is None and not draft:
            raise _err("template_required", f"{where}.message_template_id", "pick a message template")
        if template is not None:
            try:
                template = str(uuid.UUID(str(template)))
            except ValueError:
                raise _err("invalid_template", f"{where}.message_template_id") from None
        purpose = raw.get("purpose", "transactional")
        if purpose not in PURPOSES:
            raise _err("invalid_purpose", f"{where}.purpose")
        return {"action": action, "message_template_id": template, "purpose": purpose}
    if action == "send_text":
        _only(raw, where, {"channel", "content_sid", "variables"})
        channel = raw.get("channel")
        if channel not in TEXT_CHANNELS:
            raise _err("invalid_channel", f"{where}.channel", "sms or whatsapp")
        sid = raw.get("content_sid")
        if sid is None and not draft:
            raise _err("template_required", f"{where}.content_sid", "pick an approved template")
        if sid is not None and (not isinstance(sid, str) or not _CONTENT_SID.match(sid)):
            raise _err("invalid_template", f"{where}.content_sid")
        variables = raw.get("variables") or {}
        if not isinstance(variables, dict) or len(variables) > 20:
            raise _err("invalid_variables", f"{where}.variables")
        for key, field in variables.items():
            if not _VARIABLE.match(str(key)) or field not in MERGE_FIELDS:
                raise _err("invalid_variables", f"{where}.variables.{key}", ", ".join(MERGE_FIELDS))
        return {"action": action, "channel": channel, "content_sid": sid, "variables": dict(variables)}
    if action == "set_field":
        if subject != "trip":
            raise _err("invalid_action", f"{where}.action", "a client's special date sets no trip field")
        _only(raw, where, {"field", "value"})
        field = raw.get("field")
        if field == "waiting_on":
            if raw.get("value") not in WAITING_ON:
                raise _err("invalid_value", f"{where}.value", ", ".join(WAITING_ON))
            return {"action": action, "field": field, "value": raw["value"]}
        if field == "owner":
            return {"action": action, "field": field, "value": _member(raw.get("value"), f"{where}.value")}
        raise _err("invalid_field", f"{where}.field", "waiting_on or owner")
    _only(raw, where, {"days", "hours", "until"})
    if "until" in raw:
        if subject != "trip" or set(raw) - {"action", "until"}:
            raise _err("invalid_wait", where)
        until = raw["until"]
        if not isinstance(until, dict) or set(until) - {"anchor", "offset_days"}:
            raise _err("invalid_wait", f"{where}.until")
        if until.get("anchor") not in ("departure", "return"):
            raise _err("invalid_wait", f"{where}.until.anchor", "departure or return")
        days = _int(until.get("offset_days", 0), f"{where}.until.offset_days", -MAX_WAIT_DAYS, MAX_WAIT_DAYS)
        return {"action": action, "until": {"anchor": until["anchor"], "offset_days": days}}
    days = _int(raw.get("days", 0), f"{where}.days", 0, MAX_WAIT_DAYS)
    hours = _int(raw.get("hours", 0), f"{where}.hours", 0, 23)
    if days == 0 and hours == 0:
        raise _err("invalid_wait", where, "wait at least one hour")
    return {"action": action, "days": days, "hours": hours}


def validate(raw: dict[str, Any], *, draft: bool = False) -> Rule:
    """A rule from the editor's JSON (``name``, ``trigger {kind, value, offset_days}``, ``conditions``, ``steps``).
    ``draft``: a paused rule may still miss its message templates (an installed library template the owner has not
    finished); an active one may not. Raises :class:`Invalid` with the field path."""
    if not isinstance(raw, dict):
        raise _err("invalid_rule", "rule")
    name = _name(raw.get("name"), "name", MAX_NAME)
    trigger = raw.get("trigger") or {}
    if not isinstance(trigger, dict):
        raise _err("invalid_trigger", "trigger")
    kind, value, offset = _trigger(trigger.get("kind"), trigger.get("value"), trigger.get("offset_days"))
    subject = "traveler" if kind == "special_date" else "trip"
    conditions = _conditions(raw.get("conditions"), subject)
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not 1 <= len(steps_raw) <= MAX_STEPS:
        raise _err("invalid_steps", "steps", f"1 to {MAX_STEPS} steps")
    steps = [_step(s, n, subject, draft=draft) for n, s in enumerate(steps_raw)]
    if steps[-1]["action"] == "wait":
        raise _err("invalid_steps", f"steps.{len(steps) - 1}", "a rule does not end with a wait")
    if kind == "manual" and any(s["action"] in ("send_email", "send_text") for s in steps):
        # A task list is applied by a member to a trip; a message to a client goes through the composer (Undo).
        raise _err("invalid_steps", "steps", "a task list holds tasks, field changes and waits only")
    return Rule(name, kind, value, offset, conditions, steps)


def missing_templates(steps: list[dict[str, Any]]) -> list[int]:
    """The send steps still without their template (a rule cannot be switched on until it has them)."""
    out = []
    for n, s in enumerate(steps):
        if (s["action"] == "send_email" and not s.get("message_template_id")) or (
            s["action"] == "send_text" and not s.get("content_sid")
        ):
            out.append(n)
    return out


# ── Conditions, dates and kinds (shared by the signal, the tick, the preview and the engine) ─────────────────────


@dataclass(frozen=True)
class TripFacts:
    """What a condition may read about a trip (app.workflows.facts loads it in one query)."""

    trip_id: uuid.UUID
    stage: str
    trip_style: str | None
    lead_source: str | None
    destinations: tuple[str, ...]
    has_children: bool
    has_aa_lines: bool
    s3: int
    owner_member_id: uuid.UUID | None
    lead_id: uuid.UUID | None
    start_date: dt.date | None
    end_date: dt.date | None
    stage_entered_at: dt.datetime
    last_activity_at: dt.datetime
    created_at: dt.datetime
    deleted: bool
    cancelling: bool


def matches(conditions: dict[str, Any], facts: TripFacts) -> str | None:
    """None when every condition holds, else the code of the first that does not (for the run log and the preview)."""
    if "stages" in conditions and facts.stage not in conditions["stages"]:
        return "stage"
    if "trip_styles" in conditions and facts.trip_style not in conditions["trip_styles"]:
        return "trip_style"
    if "lead_sources" in conditions and facts.lead_source not in conditions["lead_sources"]:
        return "lead_source"
    if "destinations_any" in conditions and not set(conditions["destinations_any"]) & set(facts.destinations):
        return "destination"
    if "has_children" in conditions and facts.has_children != conditions["has_children"]:
        return "children"
    if "has_aa_lines" in conditions and facts.has_aa_lines != conditions["has_aa_lines"]:
        return "aa_lines"
    if "stage3_incomplete" in conditions and (facts.s3 < 100) != conditions["stage3_incomplete"]:
        return "stage3"
    return None


ENDED_STAGES = ("lost", "cancelled")


def stop_reason(facts: TripFacts | None) -> str | None:
    """Why a trip's run may not continue (None = it may): gone, in the trash, cancelled / cancelling, lost."""
    if facts is None:
        return "trip_gone"
    if facts.deleted:
        return "trip_trashed"
    if facts.stage == "cancelled" or facts.cancelling:
        return "trip_cancelled"
    if facts.stage == "lost":
        return "trip_lost"
    return None


def anchor_date(anchor: str, facts: TripFacts, tz: ZoneInfo) -> dt.date | None:
    """The trip's date for a trigger or due anchor (local to the agency), None when the trip has none yet."""
    if anchor == "departure":
        return facts.start_date
    if anchor == "return":
        return facts.end_date
    moment = {
        "stage_entered": facts.stage_entered_at,
        "last_activity": facts.last_activity_at,
        "created": facts.created_at,
    }.get(anchor)
    return moment.astimezone(tz).date() if moment is not None else None


def at_local(day: dt.date, tz: ZoneInfo, time: dt.time = DUE_LOCAL_TIME) -> dt.datetime:
    return dt.datetime.combine(day, time, tzinfo=tz).astimezone(dt.UTC)


def due_at(due: dict[str, Any], facts: TripFacts | None, tz: ZoneInfo, now: dt.datetime) -> dt.datetime | None:
    """When a step's task is due. ``created`` = now + offset days and hours; a date of the trip = that date + offset
    days at 09:00 local; None = the trip has no such date yet (the task waits in "Waiting for dates", R20)."""
    anchor, days = due["anchor"], due.get("offset_days", 0)
    if anchor == "created" or facts is None:
        return now + dt.timedelta(days=days, hours=due.get("offset_hours", 0))
    if anchor == "stage_entered":
        return facts.stage_entered_at + dt.timedelta(days=days)
    day = anchor_date(anchor, facts, tz)
    return at_local(day + dt.timedelta(days=days), tz) if day is not None else None


def step_kind(kind: str, rule_id: uuid.UUID, step_no: int) -> str:
    """The task kind of a create-task step: the coded kind, or the custom family with a suffix fixed per (rule, step),
    so one step never has two open tasks on one trip (crm-005 task_open_kind_uq)."""
    if kind != "custom":
        return kind
    return CUSTOM_PREFIX + hashlib.sha256(f"{rule_id}:{step_no}".encode()).hexdigest()[:24]


def manual_kind() -> str:
    """A member's own task: the custom family with a random suffix (never in conflict with another)."""
    return CUSTOM_PREFIX + uuid.uuid4().hex[:24]


def is_custom(kind: str) -> bool:
    return kind.startswith(CUSTOM_PREFIX)
