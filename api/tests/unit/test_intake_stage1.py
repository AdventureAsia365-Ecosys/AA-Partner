"""The Stage 1 form definition and validator (AA-936, card CRM-08: consent required, names only for companions)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.intake import stage1


def raw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "destinations": ["vietnam", "laos", "vietnam"],
        "month": "2027-03",
        "nights": "10",
        "adults": 2,
        "child_ages": [7, "12"],
        "party_type": "family",
        "budget_band": "2000_3500",
        "trip_style": "balanced",
        "name": "  Nguyễn Thị Hoa ",
        "email": " Hoa@Example.TEST ",
        "phone": "+84 912 345 678",
        "preferred_channel": "whatsapp",
        "heard_from": "social",
        "message": "Hello\u0000 there",
        "companions": ["Trần Văn Nam", "  "],
        "consent_contact": True,
        "consent_marketing": True,
        "client_submission_id": str(uuid.uuid4()),
        "locale": "vi",
        "unknown_extra": "x",
        "website": "",
    }
    base.update(over)
    return base


def test_definition_lists_every_field_once() -> None:
    names = [f["name"] for f in stage1.definition()["fields"]]
    assert len(names) == len(set(names)) and "consent_contact" in names and "companions" in names
    assert stage1.definition()["schema_key"] == stage1.SCHEMA_KEY


def test_valid_answers_are_normalised() -> None:
    a = stage1.validate(raw(), honeypot_field="website")
    assert a.destinations == ("vietnam", "laos")
    assert a.name == "Nguyễn Thị Hoa"  # NFC
    assert a.email == "hoa@example.test" and a.phone == "+84912345678"
    assert a.child_ages == (7, 12) and a.nights == 10 and a.companions == ("Trần Văn Nam",)
    assert a.message == "Hello there" and a.consent_marketing and a.locale == "vi"
    assert a.hidden_answers_dropped == 1 and a.month_date.isoformat() == "2027-03-01"
    again = stage1.Stage1Answers.from_json(a.as_json())
    assert again == a and "hidden_answers_dropped" not in a.as_json()


@pytest.mark.parametrize("value", [False, None, "yes", 1])
def test_no_consent_is_its_own_error(value: Any) -> None:
    with pytest.raises(stage1.ConsentMissing):
        stage1.validate(raw(consent_contact=value), honeypot_field="website")


def test_a_non_object_is_invalid() -> None:
    with pytest.raises(stage1.Stage1Invalid):
        stage1.validate(["x"], honeypot_field="website")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"destinations": []}, "destinations"),
        ({"destinations": ["mars"]}, "destinations"),
        ({"destinations": "vietnam"}, "destinations"),
        ({"month": "2027-13"}, "month"),
        ({"month": 202703}, "month"),
        ({"start_date": "2027-02-30"}, "start_date"),
        ({"start_date": 5}, "start_date"),
        ({"date_flex_days": 99}, "date_flex_days"),
        ({"nights": 0}, "nights"),
        ({"adults": True}, "adults"),
        ({"adults": 0}, "adults"),
        ({"adults": 1.5}, "adults"),
        ({"child_ages": [18]}, "child_ages"),
        ({"child_ages": "7"}, "child_ages"),
        ({"party_type": "crowd"}, "party_type"),
        ({"budget_band": None}, "budget_band"),
        ({"trip_style": "wild"}, "trip_style"),
        ({"name": "   "}, "name"),
        ({"name": "x" * 200}, "name"),
        ({"name": 5}, "name"),
        ({"email": "nope"}, "email"),
        ({"email": None}, "email"),
        ({"phone": "0912 345 678"}, "phone"),
        ({"preferred_channel": "fax"}, "preferred_channel"),
        ({"heard_from": "tv"}, "heard_from"),
        ({"message": "x" * 3000}, "message"),
        ({"companions": ["a"] * 9}, "companions"),
        ({"companions": "Nam"}, "companions"),
        ({"consent_marketing": "yes"}, "consent_marketing"),
        ({"locale": "fr"}, "locale"),
        ({"client_submission_id": "not-a-uuid"}, "client_submission_id"),
        ({"start_date": "2027-03-10", "end_date": "2027-03-01"}, "end_date"),
        ({"phone": None, "preferred_channel": "sms"}, "phone"),
    ],
)
def test_each_invalid_answer_names_its_field(over: dict[str, Any], field: str) -> None:
    with pytest.raises(stage1.Stage1Invalid) as exc:
        stage1.validate(raw(**over), honeypot_field="website")
    assert field in exc.value.fields


def test_optional_answers_may_be_empty() -> None:
    a = stage1.validate(
        raw(
            month="",
            nights="",
            child_ages=None,
            companions=None,
            phone="",
            preferred_channel=None,
            locale=None,
            date_flex_days=None,
            start_date="2027-03-01",
            end_date="2027-03-10",
        ),
        honeypot_field="website",
    )
    assert a.month is None and a.nights is None and a.child_ages == () and a.companions == ()
    assert a.phone is None and a.locale == "en" and a.date_flex_days == 0 and a.end_date == "2027-03-10"


@pytest.mark.parametrize(
    ("value", "filled"), [("", False), (None, False), (False, False), ("http://x", True), (1, True)]
)
def test_honeypot(value: Any, filled: bool) -> None:
    assert stage1.honeypot_filled({"company_url": value}, "company_url") is filled
    assert stage1.honeypot_filled("not a dict", "company_url") is False


def test_titles_follow_the_agency_language() -> None:
    a = stage1.validate(raw(), honeypot_field="website")
    assert stage1.trip_title(a, "en-AU") == "Vietnam + Laos, 10 nights"
    assert stage1.trip_title(a, "vi-VN") == "Việt Nam + Lào, 10 đêm"
    one = stage1.validate(raw(nights=1), honeypot_field="website")
    assert stage1.trip_title(one, "en-US").endswith("1 night")
    unsure = stage1.validate(raw(destinations=["not_sure"], nights=None), honeypot_field="website")
    assert (
        stage1.trip_title(unsure, "en-GB") == "Asia trip" and stage1.trip_title(unsure, "vi-VN") == "Chuyến đi Châu Á"
    )
    many = stage1.validate(raw(destinations=["vietnam", "laos", "cambodia", "thailand"]), honeypot_field="website")
    assert "…" in stage1.trip_title(many, "en-AU")
