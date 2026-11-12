"""AA-960 (CRM-29b) pure tag rules: codes, names, tones, the R29 trip system tags from AA catalogue facets and the R36
"ready to travel again" rule with its reasons. Invented tours and places only."""

from __future__ import annotations

import datetime as dt

import pytest

from app.errors import Invalid
from app.tags import model

TODAY = dt.date(2026, 11, 12)


def test_codes_are_ascii_fixed_and_never_system() -> None:
    assert model.code_for("VIP client") == "vip_client"
    assert model.code_for("  Đà Lạt lovers! ") == "da_lat_lovers"
    assert model.code_for("2027 honeymoon") == "tag_2027_honeymoon"
    assert model.code_for("Sys admin trips") == "my_sys_admin_trips"
    assert model.code_for("sys") == "my_sys"
    assert model.code_for("???") == "tag"
    assert model.code_for("VIP", taken=["vip", "vip_2"]) == "vip_3"
    long = model.code_for("x" * 80, taken=["x" * 40])
    assert len(long) <= 40 and long.endswith("_2")
    assert all(model.is_code(c) for c in ("vip", "sys_aa_trekking", "a"))
    assert not any(model.is_code(c) for c in ("VIP", "1abc", "", None, "a-b"))


def test_names_tones_and_subjects() -> None:
    assert model.clean_name("  Very   important ") == "Very important"
    for bad, code in ((None, "invalid_text"), ("   ", "text_required"), ("x" * 61, "text_too_long"),
                      ("bad\x07name", "invalid_text")):  # fmt: skip
        with pytest.raises(Invalid) as err:
            model.clean_name(bad)
        assert err.value.code == code
    assert model.clean_tone("purple") == "purple"
    with pytest.raises(Invalid):
        model.clean_tone("pink")
    assert model.subject_of("client") == "client"
    with pytest.raises(Invalid):
        model.subject_of("booking")
    # Every system code starts with sys_ and has a subject; the catalogue covers the CIS activity vocabulary.
    assert all(code.startswith(model.SYSTEM_PREFIX) for code in model.SYSTEM_TAGS)
    assert model.SYSTEM_TAGS[model.READY_TAG][0] == "client"
    assert len(model.ACTIVITY_TAGS) == 8


def test_trip_system_tags_come_from_live_aa_tour_facets() -> None:
    tours = [
        {"source_id": "T-100", "duration_days": 1,
         "facets": {"activities": ["Trekking", "culinary", "kite flying"], "intensity": "strenuous"}},
        {"source_id": "T-200", "duration_days": 4,
         "body": {"facets": {"activities": ["trekking"], "intensity": "easy"}}},
        {"source_id": "T-300", "duration_days": None, "facets": '{"activities": ["wildlife nature"]}'},
        {"source_id": "", "duration_days": 2},
        {"source_id": "T-400", "duration_days": 2, "facets": None},
    ]  # fmt: skip
    got = model.trip_system_tags(tours)
    assert got["sys_aa_day_tour"] == [{"code": "aa_tour", "tour": "T-100"}]
    assert got["sys_aa_multi_day"] == [{"code": "aa_tour", "tour": "T-200"}, {"code": "aa_tour", "tour": "T-400"}]
    assert [r["tour"] for r in got["sys_aa_trekking"]] == ["T-100", "T-200"]
    assert "sys_aa_culinary" in got and "sys_aa_wildlife_nature" in got
    assert got["sys_aa_challenging"] == [{"code": "aa_tour", "tour": "T-100"}]
    assert got["sys_aa_easy"] == [{"code": "aa_tour", "tour": "T-200"}]
    assert not any("kite" in code for code in got)  # an activity outside the vocabulary adds no tag
    assert model.trip_system_tags([]) == {}
    many = [{"source_id": f"T-{n}", "duration_days": 3} for n in range(9)]
    assert len(model.trip_system_tags(many)["sys_aa_multi_day"]) == 5  # at most 5 reasons


def _trip(stage: str, start: dt.date | None, end: dt.date | None, **kw: object) -> model.PastTrip:
    return model.PastTrip(stage=stage, start_date=start, end_date=end, **kw)  # type: ignore[arg-type]


def test_ready_to_travel_needs_a_completed_trip_no_open_one_and_time_at_home() -> None:
    done = _trip("completed", dt.date(2025, 12, 1), dt.date(2025, 12, 10), countries=("VN",), aa_tours=("T-1",))
    served = {"VN": frozenset({"T-1", "T-2", "T-3"}), "LA": frozenset({"T-9"})}
    history = model.ClientHistory(archived=False, trips=(done,), served_by_country=served)
    reasons = model.ready_to_travel(history, TODAY)
    assert reasons == [
        {"code": "returned_months_ago", "months": 11},
        {"code": "anniversary_soon", "days": 19},
        {"code": "new_aa_tours", "country": "VN", "count": 2},
    ]
    # Not ready: archived, an open trip, home for less than 9 months, nothing completed.
    assert model.ready_to_travel(model.ClientHistory(True, (done,)), TODAY) is None
    assert model.ready_to_travel(model.ClientHistory(False, (done, _trip("proposal", None, None))), TODAY) is None
    recent = _trip("completed", dt.date(2026, 5, 1), dt.date(2026, 5, 9))
    assert model.ready_to_travel(model.ClientHistory(False, (done, recent)), TODAY) is None
    assert model.ready_to_travel(model.ClientHistory(False, (_trip("lost", None, None),)), TODAY) is None
    assert model.ready_to_travel(model.ClientHistory(False, (_trip("completed", None, None),)), TODAY) is None


def test_ready_reasons_for_a_repeat_traveller() -> None:
    trips = (
        _trip("completed", dt.date(2023, 3, 1), dt.date(2023, 3, 9)),
        _trip("completed", dt.date(2025, 2, 2), dt.date(2025, 2, 20)),
        _trip("cancelled", None, None),
    )
    reasons = model.ready_to_travel(model.ClientHistory(False, trips), TODAY)
    assert reasons == [
        {"code": "returned_months_ago", "months": 20},
        {"code": "repeat_traveller", "trips": 2, "years": 2},
    ]


def test_dates_and_countries() -> None:
    assert model.months_between(dt.date(2026, 1, 31), dt.date(2026, 2, 28)) == 0
    assert model.months_between(dt.date(2026, 1, 15), dt.date(2026, 3, 15)) == 2
    assert model.months_between(dt.date(2026, 5, 1), dt.date(2026, 1, 1)) == 0
    leap = _trip("completed", dt.date(2024, 2, 29), dt.date(2024, 3, 5))
    reasons = model.ready_to_travel(model.ClientHistory(False, (leap,)), dt.date(2027, 1, 20))
    assert {"code": "anniversary_soon", "days": 39} in (reasons or [])
    assert model.countries_of(["vn", "Hoi An", " la "], ["KH", None, "xx"]) == ("KH", "LA", "VN")  # type: ignore[list-item]
    assert model.countries_of(None, []) == ()
