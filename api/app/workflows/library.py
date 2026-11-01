"""The AA workflow library (AA-959; design v3 R9 "template library", A-17 "Add from library", §5 automation, R2, R44
step 1): the templates an owner installs as the agency's own rules. Code, not rows: each has a key and a version, the
web words its name and description (``workflow.template.<key>``), and installing one writes a ``crm.workflow_rule``
with ``template_key`` / ``template_version`` (one live rule per template, crm-038).

A template whose steps send a message has no message template of its own: the agency picks one of its own templates
(Settings > Templates) or approved WhatsApp templates. Installed without them, the rule is created **paused** and the
editor asks for them before it can be switched on (:func:`app.workflows.model.missing_templates`).

The T-21 Stage 3 alert to the agent is the date rule's ``stage3_due_t21`` task (AA-946); the library adds the client
nudge (``stage3_nudge_t21``). R44 step 1 is ``arrival_flight_t48h`` (the task carries the flight status link).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.workflows import model as m


@dataclass(frozen=True)
class Template:
    key: str
    version: int
    name: str  # the English default name of the installed rule (the owner may rename it)
    trigger: dict[str, Any]
    steps: tuple[dict[str, Any], ...]
    conditions: dict[str, Any] = field(default_factory=dict)

    def rule(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trigger": dict(self.trigger),
            "conditions": dict(self.conditions),
            "steps": [dict(s) for s in self.steps],
        }


def _task(kind: str, *, anchor: str = "created", days: int = 0, hours: int = 0, urgent: bool = False) -> dict[str, Any]:
    return {
        "action": "create_task",
        "kind": kind,
        "due": {"anchor": anchor, "offset_days": days, "offset_hours": hours},
        "priority": "urgent" if urgent else "normal",
        "assignee": "owner",
    }


def _custom(title: str, *, anchor: str = "created", days: int = 0) -> dict[str, Any]:
    return {**_task("custom", anchor=anchor, days=days), "title": title}


TEMPLATES: tuple[Template, ...] = (
    Template(
        key="arrival_flight_t48h",
        version=1,
        name="Confirm the arrival flight (T-48 h)",
        trigger={"kind": "date_anchor", "value": "departure", "offset_days": -2},
        conditions={"stages": ["booked", "pre_departure"]},
        steps=(_task(m.ARRIVAL_FLIGHT_TASK, anchor="departure", days=-2, urgent=True),),
    ),
    Template(
        key="stage3_nudge_t21",
        version=1,
        name="Remind travellers of their booking details (T-21)",
        trigger={"kind": "date_anchor", "value": "departure", "offset_days": -21},
        conditions={"stages": ["booked"], "stage3_incomplete": True},
        steps=({"action": "send_email", "message_template_id": None, "purpose": "transactional"},),
    ),
    Template(
        key="pre_departure_checklist",
        version=1,
        name="Pre-departure checklist",
        trigger={"kind": "stage_enter", "value": "booked"},
        steps=(
            _task(m.SUPPLIER_CHECK_TASK, anchor="departure", days=-30),
            _task(m.PRE_TRAVEL_PACK_TASK, anchor="departure", days=-14),
        ),
    ),
    Template(
        key="quiet_client_follow_up",
        version=1,
        name="Follow up a quiet client (3 days)",
        trigger={"kind": "date_anchor", "value": "last_activity", "offset_days": 3},
        conditions={"stages": ["qualifying", "proposal"]},
        steps=(_task(m.QUIET_CLIENT_TASK),),
    ),
    Template(
        key="client_message_reply",
        version=1,
        name="Reply to a client's text message",
        trigger={"kind": "event", "value": "message.received"},
        steps=(_task(m.REPLY_MESSAGE_TASK, hours=4),),
    ),
    Template(
        key="post_trip_review",
        version=1,
        name="Ask for a review after the trip",
        trigger={"kind": "stage_enter", "value": "completed"},
        steps=({"action": "wait", "days": 3, "hours": 0}, _task(m.REVIEW_TASK)),
    ),
    Template(
        key="birthday_greeting",
        version=1,
        name="Birthday greeting (marketing consent only)",
        trigger={"kind": "special_date", "value": "birthday", "offset_days": 0},
        steps=(
            {"action": "send_email", "message_template_id": None, "purpose": "marketing"},
            _task(m.BIRTHDAY_TASK),
        ),
    ),
    Template(
        key="passport_expiry_200d",
        version=1,
        name="Passport expires within 200 days",
        trigger={"kind": "special_date", "value": "passport_expiry", "offset_days": -200},
        steps=(_task(m.PASSPORT_TASK),),
    ),
    Template(
        key="trip_anniversary",
        version=1,
        name="A year since the trip (marketing consent only)",
        trigger={"kind": "special_date", "value": "trip_anniversary", "offset_days": 0},
        steps=(
            {"action": "send_email", "message_template_id": None, "purpose": "marketing"},
            _task(m.ANNIVERSARY_TASK),
        ),
    ),
    Template(
        key="visa_documents_task_list",
        version=1,
        name="Visa and documents task list",
        trigger={"kind": "manual"},
        steps=(
            _custom("Check the visa rules for every passport on the trip", anchor="departure", days=-60),
            _custom("Collect passport copies", anchor="departure", days=-45),
            _custom("Confirm travel insurance for every traveller", anchor="departure", days=-30),
        ),
    ),
)
BY_KEY: dict[str, Template] = {t.key: t for t in TEMPLATES}


def get(key: str) -> Template | None:
    return BY_KEY.get(key)


def listing() -> list[dict[str, Any]]:
    """The library as the API shows it (the web words name and description by key)."""
    return [
        {
            "key": t.key,
            "version": t.version,
            "name": t.name,
            "trigger": dict(t.trigger),
            "conditions": dict(t.conditions),
            "steps": [dict(s) for s in t.steps],
            "needs_templates": bool(m.missing_templates([dict(s) for s in t.steps])),
        }
        for t in TEMPLATES
    ]
