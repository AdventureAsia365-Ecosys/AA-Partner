"""AA-959 (card CRM-29a) without a database: the rule shape (:mod:`app.workflows.model`), the AA library and the date
helpers of the tick. Every refusal names its field; the library validates; due dates follow the trip's dates at
09:00 local and wait for dates the trip has not got (R20); a custom step's kind is fixed per (rule, step)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.errors import Invalid
from app.workflows import library, model, tick

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
NOW = dt.datetime(2026, 11, 1, 3, 0, tzinfo=dt.UTC)
RULE = uuid.UUID("00000000-0000-4000-8000-0000000000f1")


def raw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "  Pre-departure   checklist ",
        "trigger": {"kind": "stage_enter", "value": "booked"},
        "conditions": {},
        "steps": [{"action": "create_task", "kind": "custom", "title": "Check the visa rules"}],
    }
    base.update(over)
    return base


def facts(**over: Any) -> model.TripFacts:
    base: dict[str, Any] = {
        "trip_id": uuid.UUID(int=1),
        "stage": "booked",
        "trip_style": "adventure",
        "lead_source": "website_form",
        "destinations": ("vietnam", "laos"),
        "has_children": True,
        "has_aa_lines": False,
        "s3": 60,
        "owner_member_id": uuid.UUID(int=2),
        "lead_id": uuid.UUID(int=3),
        "start_date": dt.date(2026, 12, 20),
        "end_date": dt.date(2026, 12, 30),
        "stage_entered_at": dt.datetime(2026, 10, 30, 1, tzinfo=dt.UTC),
        "last_activity_at": dt.datetime(2026, 10, 29, 20, tzinfo=dt.UTC),
        "created_at": dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
        "deleted": False,
        "cancelling": False,
    }
    base.update(over)
    return model.TripFacts(**base)


def test_a_valid_rule_is_normalised() -> None:
    rule = model.validate(raw())
    assert rule.name == "Pre-departure checklist" and rule.subject == "trip"
    assert rule.steps == [
        {
            "action": "create_task",
            "kind": "custom",
            "title": "Check the visa rules",
            "due": {"anchor": "created", "offset_days": 0, "offset_hours": 0},
            "priority": "normal",
            "assignee": "owner",
        }
    ]


def test_every_action_and_trigger_kind_is_accepted() -> None:
    member = f"member:{uuid.uuid4()}"
    rule = model.validate(
        raw(
            trigger={"kind": "date_anchor", "value": "departure", "offset_days": -21},
            conditions={
                "stages": ["booked", "booked"],
                "trip_styles": ["adventure"],
                "lead_sources": ["website_form"],
                "destinations_any": ["vietnam"],
                "has_children": True,
                "has_aa_lines": False,
                "stage3_incomplete": True,
            },
            steps=[
                {
                    "action": "create_task",
                    "kind": "confirm_arrival_flight",
                    "priority": "urgent",
                    "assignee": member,
                    "due": {"anchor": "departure", "offset_days": -2},
                },
                {"action": "send_email", "message_template_id": str(uuid.uuid4()), "purpose": "marketing"},
                {
                    "action": "send_text",
                    "channel": "whatsapp",
                    "content_sid": "HX" + "a" * 32,
                    "variables": {"1": "traveller_first_name"},
                },
                {"action": "set_field", "field": "waiting_on", "value": "client"},
                {"action": "set_field", "field": "owner", "value": member},
                {"action": "wait", "days": 1},
                {"action": "wait", "until": {"anchor": "departure", "offset_days": -7}},
                {"action": "create_task", "kind": "custom", "title": "Call", "due": {"offset_hours": 4}},
            ],
        )
    )
    assert rule.conditions["stages"] == ["booked"] and rule.trigger_offset_days == -21
    assert [s["action"] for s in rule.steps][-1] == "create_task"
    assert (
        model.validate(raw(trigger={"kind": "event", "value": "message.received"})).trigger_value == "message.received"
    )
    special = model.validate(raw(trigger={"kind": "special_date", "value": "birthday", "offset_days": 0}))
    assert special.subject == "traveler"
    manual = model.validate(raw(trigger={"kind": "manual"}))
    assert manual.trigger_value is None and manual.trigger_offset_days == 0


@pytest.mark.parametrize(
    ("over", "code", "field"),
    [
        ({"name": ""}, "text_required", "name"),
        ({"name": 3}, "invalid_text", "name"),
        ({"name": "x" * 121}, "text_too_long", "name"),
        ({"trigger": {"kind": "often"}}, "invalid_trigger", "trigger.kind"),
        ({"trigger": "booked"}, "invalid_trigger", "trigger"),
        ({"trigger": {"kind": "stage_enter", "value": "cancelled"}}, "invalid_trigger", "trigger.value"),
        ({"trigger": {"kind": "stage_enter", "value": "nowhere"}}, "invalid_trigger", "trigger.value"),
        (
            {"trigger": {"kind": "stage_enter", "value": "booked", "offset_days": 2}},
            "invalid_trigger",
            "trigger.offset_days",
        ),
        ({"trigger": {"kind": "manual", "value": "booked"}}, "invalid_trigger", "trigger.value"),
        (
            {"trigger": {"kind": "date_anchor", "value": "departure", "offset_days": 401}},
            "invalid_number",
            "trigger.offset_days",
        ),
        ({"conditions": {"tags_any": ["vip"]}}, "condition_not_available", "conditions.tags_any"),
        ({"conditions": {"colour": "blue"}}, "unknown_field", "conditions.colour"),
        ({"conditions": []}, "invalid_conditions", "conditions"),
        ({"conditions": {"stages": []}}, "invalid_list", "conditions.stages"),
        ({"conditions": {"stages": ["nowhere"]}}, "invalid_code", "conditions.stages"),
        ({"conditions": {"has_children": "yes"}}, "invalid_condition", "conditions.has_children"),
        ({"steps": []}, "invalid_steps", "steps"),
        ({"steps": ["task"]}, "invalid_step", "steps.0"),
        ({"steps": [{"action": "dance"}]}, "invalid_action", "steps.0.action"),
        ({"steps": [{"action": "create_task", "kind": "custom"}]}, "invalid_text", "steps.0.title"),
        ({"steps": [{"action": "create_task", "kind": "free_lunch"}]}, "invalid_task_kind", "steps.0.kind"),
        (
            {"steps": [{"action": "create_task", "kind": "request_review", "title": "x"}]},
            "unknown_field",
            "steps.0.title",
        ),
        ({"steps": [{"action": "create_task", "kind": "request_review", "colour": 1}]}, "unknown_field", "steps.0"),
        (
            {"steps": [{"action": "create_task", "kind": "request_review", "priority": "high"}]},
            "invalid_priority",
            "steps.0.priority",
        ),
        (
            {"steps": [{"action": "create_task", "kind": "request_review", "assignee": "bob"}]},
            "invalid_assignee",
            "steps.0.assignee",
        ),
        (
            {"steps": [{"action": "create_task", "kind": "request_review", "assignee": "member:" + "z" * 36}]},
            "invalid_assignee",
            "steps.0.assignee",
        ),
        (
            {"steps": [{"action": "create_task", "kind": "request_review", "due": {"anchor": "whenever"}}]},
            "invalid_due",
            "steps.0.due.anchor",
        ),
        ({"steps": [{"action": "create_task", "kind": "request_review", "due": "soon"}]}, "invalid_due", "steps.0.due"),
        (
            {
                "steps": [
                    {
                        "action": "create_task",
                        "kind": "request_review",
                        "due": {"anchor": "departure", "offset_hours": 2},
                    }
                ]
            },
            "invalid_due",
            "steps.0.due.offset_hours",
        ),
        ({"steps": [{"action": "send_email"}]}, "template_required", "steps.0.message_template_id"),
        (
            {"steps": [{"action": "send_email", "message_template_id": "nope"}]},
            "invalid_template",
            "steps.0.message_template_id",
        ),
        (
            {"steps": [{"action": "send_email", "message_template_id": str(uuid.uuid4()), "purpose": "spam"}]},
            "invalid_purpose",
            "steps.0.purpose",
        ),
        ({"steps": [{"action": "send_text", "channel": "fax"}]}, "invalid_channel", "steps.0.channel"),
        ({"steps": [{"action": "send_text", "channel": "sms"}]}, "template_required", "steps.0.content_sid"),
        (
            {"steps": [{"action": "send_text", "channel": "sms", "content_sid": "HX1"}]},
            "invalid_template",
            "steps.0.content_sid",
        ),
        (
            {
                "steps": [
                    {
                        "action": "send_text",
                        "channel": "sms",
                        "content_sid": "HX" + "a" * 32,
                        "variables": {"1": "password"},
                    }
                ]
            },
            "invalid_variables",
            "steps.0.variables.1",
        ),
        (
            {"steps": [{"action": "send_text", "channel": "sms", "content_sid": "HX" + "a" * 32, "variables": []}]},
            "invalid_variables",
            "steps.0.variables",
        ),
        ({"steps": [{"action": "set_field", "field": "stage", "value": "booked"}]}, "invalid_field", "steps.0.field"),
        (
            {"steps": [{"action": "set_field", "field": "waiting_on", "value": "nobody"}]},
            "invalid_value",
            "steps.0.value",
        ),
        (
            {"steps": [{"action": "wait", "days": 0}, {"action": "create_task", "kind": "request_review"}]},
            "invalid_wait",
            "steps.0",
        ),
        (
            {
                "steps": [
                    {"action": "wait", "until": {"anchor": "someday"}},
                    {"action": "create_task", "kind": "request_review"},
                ]
            },
            "invalid_wait",
            "steps.0.until.anchor",
        ),
        (
            {"steps": [{"action": "wait", "until": "later"}, {"action": "create_task", "kind": "request_review"}]},
            "invalid_wait",
            "steps.0.until",
        ),
        (
            {
                "steps": [
                    {"action": "wait", "days": 2, "until": {"anchor": "departure"}},
                    {"action": "create_task", "kind": "request_review"},
                ]
            },
            "invalid_wait",
            "steps.0",
        ),
        ({"steps": [{"action": "wait", "days": True}]}, "invalid_number", "steps.0.days"),
        ({"steps": [{"action": "wait", "days": 1}]}, "invalid_steps", "steps.0"),
        ({"steps": [{"action": "create_task", "kind": "request_review"}] * 21}, "invalid_steps", "steps"),
    ],
)
def test_refusals_name_the_field(over: dict[str, Any], code: str, field: str) -> None:
    with pytest.raises(Invalid) as err:
        model.validate(raw(**over))
    assert err.value.code == code and err.value.extra["field"] == field


def test_client_rules_and_task_lists_have_their_own_limits() -> None:
    special = {"kind": "special_date", "value": "birthday"}
    with pytest.raises(Invalid, match="special date"):
        model.validate(raw(trigger=special, conditions={"stages": ["booked"]}))
    with pytest.raises(Invalid) as err:
        model.validate(raw(trigger=special, steps=[{"action": "set_field", "field": "waiting_on", "value": "client"}]))
    assert err.value.code == "invalid_action"
    with pytest.raises(Invalid) as err:
        model.validate(
            raw(
                trigger=special,
                steps=[{"action": "create_task", "kind": "client_birthday", "due": {"anchor": "departure"}}],
            )
        )
    assert err.value.code == "invalid_due"
    with pytest.raises(Invalid) as err:
        model.validate(
            raw(
                trigger=special,
                steps=[
                    {"action": "wait", "until": {"anchor": "departure"}},
                    {"action": "create_task", "kind": "client_birthday"},
                ],
            )
        )
    assert err.value.code == "invalid_wait"
    with pytest.raises(Invalid) as err:
        model.validate(
            raw(trigger={"kind": "manual"}, steps=[{"action": "send_email", "message_template_id": str(uuid.uuid4())}])
        )
    assert err.value.code == "invalid_steps"
    with pytest.raises(Invalid):
        model.validate("rule")  # type: ignore[arg-type]


def test_a_greeting_is_always_marketing() -> None:
    """S263 review: a birthday or anniversary send is marketing (consent checked, card R); transactional is refused."""
    template, sid = str(uuid.uuid4()), "HX" + "a" * 32
    for value in ("birthday", "trip_anniversary"):
        trigger = {"kind": "special_date", "value": value}
        rule = model.validate(
            raw(
                trigger=trigger,
                steps=[
                    {"action": "send_email", "message_template_id": template},
                    {"action": "send_text", "channel": "sms", "content_sid": sid},
                ],
            )
        )
        assert [s["purpose"] for s in rule.steps] == ["marketing", "marketing"]
        assert model.marketing_only("special_date", value)
        for n, step in enumerate(
            ({"action": "send_email", "message_template_id": template, "purpose": "transactional"},
             {"action": "send_text", "channel": "sms", "content_sid": sid, "purpose": "transactional"})
        ):  # fmt: skip
            with pytest.raises(Invalid) as err:
                model.validate(raw(trigger=trigger, steps=[{"action": "create_task", "kind": "client_birthday"}] * n
                                   + [step]))  # fmt: skip
            assert err.value.code == "marketing_only" and err.value.extra["field"] == f"steps.{n}.purpose"
    # A passport-expiry notice stays the owner's choice; a stage rule keeps its transactional mail and text.
    passport = {"kind": "special_date", "value": "passport_expiry"}
    assert not model.marketing_only("special_date", "passport_expiry")
    mail = {"action": "send_email", "message_template_id": template, "purpose": "transactional"}
    assert model.validate(raw(trigger=passport, steps=[mail])).steps[0]["purpose"] == "transactional"
    text = model.validate(raw(steps=[{"action": "send_text", "channel": "sms", "content_sid": sid}])).steps[0]
    assert "purpose" not in text
    with pytest.raises(Invalid) as err:
        model.validate(raw(steps=[{"action": "send_text", "channel": "sms", "content_sid": sid, "purpose": "x"}]))
    assert err.value.code == "invalid_purpose"


def test_a_rule_started_by_a_sent_text_sends_no_text() -> None:
    """S263 review: send_text on message.sent would start the rule again with its own message, for ever."""
    sid = "HX" + "a" * 32
    text = {"action": "send_text", "channel": "whatsapp", "content_sid": sid}
    with pytest.raises(Invalid) as err:
        model.validate(raw(trigger={"kind": "event", "value": "message.sent"}, steps=[text]))
    assert err.value.code == "message_loop" and err.value.extra["field"] == "steps.0.action"
    task = {"action": "create_task", "kind": "reply_client_message"}
    assert model.validate(raw(trigger={"kind": "event", "value": "message.sent"}, steps=[task])).steps
    # message.received may answer with a text: the trigger caps it (one run per trip per cool-down window).
    assert model.validate(raw(trigger={"kind": "event", "value": "message.received"}, steps=[text])).steps


def test_drafts_may_miss_their_templates_until_switched_on() -> None:
    steps = [{"action": "send_email"}, {"action": "send_text", "channel": "sms"}]
    rule = model.validate(raw(steps=steps), draft=True)
    assert model.missing_templates(rule.steps) == [0, 1]
    assert model.missing_templates(model.validate(raw()).steps) == []


def test_the_library_validates_and_needs_templates_where_it_sends() -> None:
    listed = {t["key"]: t for t in library.listing()}
    assert set(listed) == {t.key for t in library.TEMPLATES} and library.get("nope") is None
    for template in library.TEMPLATES:
        rule = model.validate(template.rule(), draft=True)
        sends = any(s["action"] in ("send_email", "send_text") for s in rule.steps)
        assert listed[template.key]["needs_templates"] is sends
        if not sends:
            model.validate(template.rule())
    assert library.get("arrival_flight_t48h").steps[0]["kind"] == model.ARRIVAL_FLIGHT_TASK  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("conditions", "over", "result"),
    [
        ({}, {}, None),
        ({"stages": ["proposal"]}, {}, "stage"),
        ({"trip_styles": ["relaxed"]}, {}, "trip_style"),
        ({"lead_sources": ["referral"]}, {}, "lead_source"),
        ({"destinations_any": ["nepal"]}, {}, "destination"),
        ({"destinations_any": ["laos"]}, {}, None),
        ({"has_children": False}, {}, "children"),
        ({"has_aa_lines": True}, {}, "aa_lines"),
        ({"stage3_incomplete": True}, {}, None),
        ({"stage3_incomplete": True}, {"s3": 100}, "stage3"),
    ],
)
def test_conditions(conditions: dict[str, Any], over: dict[str, Any], result: str | None) -> None:
    assert model.matches(conditions, facts(**over)) == result


def test_why_a_trip_run_stops() -> None:
    assert model.stop_reason(None) == "trip_gone"
    assert model.stop_reason(facts(deleted=True)) == "trip_trashed"
    assert model.stop_reason(facts(stage="cancelled")) == "trip_cancelled"
    assert model.stop_reason(facts(cancelling=True)) == "trip_cancelled"
    assert model.stop_reason(facts(stage="lost")) == "trip_lost"
    assert model.stop_reason(facts()) is None


def test_due_dates_follow_the_trip_and_wait_for_missing_ones() -> None:
    f = facts()
    assert model.due_at({"anchor": "created", "offset_days": 1, "offset_hours": 4}, f, TZ, NOW) == NOW + dt.timedelta(
        days=1, hours=4
    )
    # 14 days before the departure at 09:00 in Ho Chi Minh City (UTC+7) = 02:00 UTC.
    assert model.due_at({"anchor": "departure", "offset_days": -14}, f, TZ, NOW) == dt.datetime(
        2026, 12, 6, 2, tzinfo=dt.UTC
    )
    assert model.due_at({"anchor": "return", "offset_days": 3}, f, TZ, NOW) == dt.datetime(2027, 1, 2, 2, tzinfo=dt.UTC)
    assert model.due_at({"anchor": "stage_entered", "offset_days": 2}, f, TZ, NOW) == f.stage_entered_at + dt.timedelta(
        days=2
    )
    assert model.due_at({"anchor": "departure", "offset_days": -14}, facts(start_date=None), TZ, NOW) is None
    assert model.due_at({"anchor": "departure", "offset_days": 0}, None, TZ, NOW) == NOW
    assert model.anchor_date("last_activity", f, TZ) == dt.date(2026, 10, 30)  # 20:00 UTC = 03:00 next day local
    assert model.anchor_date("created", f, TZ) == dt.date(2026, 10, 1)
    assert model.anchor_date("nothing", f, TZ) is None


def test_task_kinds_of_steps_and_members() -> None:
    first = model.step_kind("custom", RULE, 0)
    assert first == model.step_kind("custom", RULE, 0) != model.step_kind("custom", RULE, 1)
    assert first.startswith("custom_") and len(first) <= 40 and model.is_custom(first)
    assert model.step_kind("request_review", RULE, 0) == "request_review"
    mine = model.manual_kind()
    assert mine != model.manual_kind() and model.is_custom(mine) and len(mine) <= 40
    assert model.clean_text("  a   b ", "title", 10) == "a b"


def test_the_tick_dates() -> None:
    today = dt.date(2026, 11, 1)
    assert tick.in_window(today, today) and tick.in_window(today - dt.timedelta(days=3), today)
    assert not tick.in_window(today - dt.timedelta(days=4), today) and not tick.in_window(
        today + dt.timedelta(1), today
    )
    assert tick.yearly(dt.date(1996, 2, 29), 2026) == dt.date(2026, 2, 28)
    assert tick.birthday_targets(dt.date(1990, 11, 1), 0, today) == [today]
    assert tick.birthday_targets(dt.date(1990, 10, 30), 0, today) == [dt.date(2026, 10, 30)]
    assert tick.birthday_targets(dt.date(1990, 12, 31), 0, dt.date(2027, 1, 1)) == [dt.date(2026, 12, 31)]
    assert tick.birthday_targets(dt.date(2026, 11, 1), 0, today) == []  # born today: no birthday yet
    assert tick.birthday_targets(dt.date(1990, 11, 8), -7, today) == [today]
    assert tick.anniversary_targets(dt.date(2025, 11, 1), 0, today) == [(1, today)]
    assert tick.anniversary_targets(dt.date(2026, 11, 1), 0, today) == []
    assert tick.anniversary_targets(dt.date(2023, 10, 31), 0, today) == [(3, dt.date(2026, 10, 31))]
    assert (
        tick.schedule_key(dt.datetime(2026, 11, 1, 7, 30, tzinfo=dt.UTC)) == "crm_workflow_tick:schedule:2026-11-01T07"
    )
