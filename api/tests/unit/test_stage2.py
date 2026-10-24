"""The Stage 2 planning form's server rules (AA-943, CRM-15; R31): definition, visibility, validation, completeness."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.errors import Invalid
from app.intake import completeness, slots, stage1, stage2

PLAN = {"accommodation_tier": "comfort", "interests": ["food"], "competing_quotes": False, "occasion": "none"}


def test_the_definition_names_every_field_once_and_only_known_codes() -> None:
    names = [f["name"] for p in stage2.definition()["pages"] for f in p["fields"]]
    assert len(names) == len(set(names)) and set(stage2.PROFILE_FIELDS) <= set(names)
    assert stage2.definition()["schema_key"] == "stage2.v1"


@pytest.mark.parametrize(
    ("style", "consent", "shown", "hidden"),
    [
        ("relaxed", False, {"accommodation_tier"}, {"altitude_ok", "trek_experience", "dietary_flags"}),
        ("adventure", False, {"altitude_ok", "trek_experience"}, {"dietary_flags", "mobility_flags"}),
        (None, True, {"dietary_flags", "mobility_flags"}, {"altitude_ok"}),
    ],
)
def test_r31_branches(style: str | None, consent: bool, shown: set[str], hidden: set[str]) -> None:
    visible = stage2.visible_fields(style, {"health_consent": consent})
    assert shown <= visible and not hidden & visible


def test_hidden_and_unknown_answers_are_dropped_and_counted() -> None:
    out = stage2.validate(
        {**PLAN, "altitude_ok": True, "note": "x", "client_submission_id": "abcdefgh"}, trip_style="relaxed"
    )
    assert out.values == PLAN and out.hidden_answers_dropped == 2 and out.client_submission_id == "abcdefgh"


def test_health_needs_the_consent() -> None:
    with pytest.raises(stage2.HealthConsentMissing):
        stage2.validate({"dietary_flags": ["vegan"]}, trip_style=None)
    with pytest.raises(Invalid) as refused:
        stage2.checked({"mobility_flags": ["wheelchair"], "health_consent": False}, None)
    assert refused.value.code == "health_consent_required"
    ok = stage2.validate({"dietary_flags": ["vegan"], "health_consent": True}, trip_style=None)
    assert ok.health_consent and ok.values == {"dietary_flags": ["vegan"]}


@pytest.mark.parametrize(
    "bad",
    [
        {"accommodation_tier": "palace"},
        {"interests": ["food", "skydiving"]},
        {"interests": "food"},
        {"competing_quotes": "no"},
        {"must_do_detail": "x" * 1001},
        {"must_do_detail": "bell\x07"},
        {"client_submission_id": "bad id!"},
    ],
)
def test_invalid_answers_name_the_field(bad: dict[str, Any]) -> None:
    with pytest.raises(Invalid) as refused:
        stage2.checked(bad, None)
    assert refused.value.code == "invalid_submission" and refused.value.extra["fields"]
    with pytest.raises(stage2.Stage2Invalid):
        stage2.validate([], trip_style=None)


def test_free_text_is_normalised_and_kept_out_of_the_values() -> None:
    out = stage2.validate({"must_do_detail": "  Ha Long Bay  ", "interests": ["food", "food"]}, trip_style=None)
    assert out.must_do_detail == "Ha Long Bay" and out.values == {"interests": ["food"]}
    assert json.loads(json.dumps(out.as_json()))["must_do_detail"] == "Ha Long Bay"
    assert stage2.validate({"must_do_detail": "   "}, trip_style=None).must_do_detail is None


def trip(**over: Any) -> dict[str, Any]:
    base = {
        "destinations": ["vietnam"],
        "month": date(2027, 3, 1),
        "start_date": None,
        "nights": 10,
        "party_type": "couple",
        "budget_band": "2000_3500",
        "trip_style": "balanced",
    }
    base.update(over)
    return base


def test_stage1_and_stage2_missing_fields() -> None:
    assert completeness.s1_missing(trip(), True) == ()
    assert completeness.s1_missing(trip(destinations=["not_sure"], budget_band="not_sure", month=None), False) == (
        "destinations",
        "when",
        "budget_band",
        "contact",
    )
    profile = dict.fromkeys(completeness.S2_FIELDS + completeness.S2_ADVENTURE_FIELDS)
    assert completeness.s2_missing(None, "relaxed") == completeness.S2_FIELDS
    profile.update(fitness_band="2_4h", interests=[], competing_quotes=False)
    missing = completeness.s2_missing(profile, "adventure")
    assert "fitness_band" not in missing and "interests" in missing and "competing_quotes" not in missing
    assert "altitude_ok" in missing and "altitude_ok" not in completeness.s2_missing(profile, "relaxed")
    assert completeness.percent(4, 5) == 80 and completeness.percent(0, 0) == 100


def test_stage1_call_slot_and_pages() -> None:
    pages = stage1.definition()["pages"]
    assert [p["key"] for p in pages] == ["trip", "party", "contact"] and "call_slot" in pages[2]["fields"]
    base = {
        "destinations": ["vietnam"],
        "adults": 2,
        "budget_band": "not_sure",
        "name": "Hoa",
        "email": "hoa@example.test",
        "consent_contact": True,
        "client_submission_id": "00000000-0000-4000-8000-000000000001",
    }
    assert stage1.validate({**base, "call_slot": "2026-11-02T03:00:00Z"}, honeypot_field="website").call_slot
    for bad in ("2026-11-02T03:00:00+07:00", "2026-13-02T03:00:00Z", 5):
        with pytest.raises(stage1.Stage1Invalid):
            stage1.validate({**base, "call_slot": bad}, honeypot_field="website")


RULES = {"windows": [{"dow": 1, "start": "09:00", "end": "10:30"}], "duration_min": 30, "horizon_days": 9}


def test_free_starts_follow_the_local_windows() -> None:
    now = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # Sunday; Monday 26 Oct 09:00 in Hanoi = 02:00 UTC
    starts = slots.free_starts(RULES, "Asia/Ho_Chi_Minh", set(), now)
    assert [slots.slot_text(s) for s in starts] == [
        "2026-10-26T02:00:00Z",
        "2026-10-26T02:30:00Z",
        "2026-10-26T03:00:00Z",
        "2026-11-02T02:00:00Z",
        "2026-11-02T02:30:00Z",
        "2026-11-02T03:00:00Z",
    ]
    taken = {datetime(2026, 10, 26, 2, 30, tzinfo=UTC)}
    assert len(slots.free_starts(RULES, "Asia/Ho_Chi_Minh", taken, now)) == 5
    # The lead time hides a slot that starts too soon.
    late = datetime(2026, 10, 26, 0, 30, tzinfo=UTC)
    assert slots.slot_text(slots.free_starts(RULES, "Asia/Ho_Chi_Minh", set(), late)[0]) == "2026-10-26T02:30:00Z"
    assert slots.free_starts({}, "Asia/Ho_Chi_Minh", set(), now) == []
    assert slots.free_starts(RULES, "Mars/Olympus", set(), now) == []
    assert len(slots.free_starts(RULES, "UTC", set(), now, limit=2)) == 2


def test_free_starts_cross_a_daylight_saving_change() -> None:
    # Sydney leaves daylight saving on 5 Apr 2026: 09:00 local is 22:00 UTC before, 23:00 UTC after.
    rules = {"windows": [{"dow": 7, "start": "09:00", "end": "09:30"}], "duration_min": 30, "horizon_days": 14}
    now = datetime(2026, 3, 28, 0, 0, tzinfo=UTC)
    got = [slots.slot_text(s) for s in slots.free_starts(rules, "Australia/Sydney", set(), now)]
    assert got == ["2026-03-28T22:00:00Z", "2026-04-04T23:00:00Z"]
