"""AA-949: the pure builder rules (:mod:`app.proposals.rules`) and the AA quote body / refusals
(:mod:`app.proposals.aa_options`)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.booking.client import BookingRefusal
from app.errors import Conflict, Invalid, Unavailable
from app.proposals import aa_options, rules
from tests.aa_fakes import FakeBooking, booking_client


def test_card_e_a_nepal_day_shows_plus_05_45() -> None:
    assert rules.utc_offset("Asia/Kathmandu", dt.date(2026, 12, 1)) == "+05:45"
    assert rules.utc_offset("Asia/Yangon", dt.date(2026, 12, 1)) == "+06:30"
    assert rules.utc_offset("Australia/Sydney", dt.date(2026, 12, 1)) == "+11:00"  # summer time
    assert rules.utc_offset("Australia/Sydney", dt.date(2026, 7, 1)) == "+10:00"
    assert rules.utc_offset("America/Los_Angeles", dt.date(2026, 12, 1)) == "-08:00"
    assert rules.utc_offset("UTC", None) == "+00:00"


def test_zones() -> None:
    assert rules.default_tz(["not_sure", "nepal", "vietnam"], None) == "Asia/Kathmandu"
    assert rules.default_tz([], "Australia/Sydney") == "Australia/Sydney"
    assert rules.default_tz(None, "Nowhere/City") == rules.FALLBACK_TZ
    for good in ("Asia/Kathmandu", "UTC", "America/Argentina/Buenos_Aires"):
        assert rules.valid_tz(good), good
    for bad in ("UTC+7", "asia/kathmandu", "Mars/Base", "", "x" * 70, None, "../etc/passwd"):
        assert not rules.valid_tz(bad), bad


@pytest.mark.parametrize(
    ("adults", "ages", "party", "want"),
    [
        (1, [], "solo", [("single", 1, 0)]),
        (2, [], "couple", [("double", 2, 0)]),
        (3, [], "friends", [("twin", 2, 0), ("single", 1, 0)]),
        (2, [8, 1], "family", [("family", 2, 2)]),
        (4, [5], None, [("family", 2, 1), ("double", 2, 0)]),
    ],
)
def test_default_rooms_from_the_party(adults: int, ages: list[int], party: str | None, want: list) -> None:
    got = rules.default_rooms(adults, ages, party)
    assert [(r["type"], r["adults"], r["children"]) for r in got] == want


def test_room_checks_and_pax() -> None:
    rooms = rules.check_rooms([{"type": "family", "adults": 2, "children": 2}, {"type": "single", "adults": 1}])
    assert rooms[1] == {"type": "single", "adults": 1, "children": 0}
    for bad in (
        [],
        [{"type": "suite", "adults": 1}],
        [{"type": "single", "adults": 0}],
        [{"type": "single", "adults": True}],
        [{"type": "single", "adults": 1, "name": "Grace"}],
        "x",
    ):
        with pytest.raises(ValueError):
            rules.check_rooms(bad)  # type: ignore[arg-type]
    pax = rules.room_pax(rooms, 2, [8, 1])
    assert pax == {"adult": 3, "child": 1, "infant": 1,
                   "rooms": [{"occupancy": "family", "count": 1}, {"occupancy": "single", "count": 1}]}  # fmt: skip
    assert rules.room_pax(None, 2, [])["rooms"] is None


def _rule(kind: str, a: str, b: str) -> rules.Rule:
    return rules.Rule(f"{kind}-{a}-{b}", kind, a, b)


def test_card_r_rule_violations() -> None:
    choice = rules.chosen({"B1": "camp", "B2": "jeep", "B3": None}, {"B2": "walk"})
    assert choice == {"B1": "camp", "B2": "walk"}
    assert rules.violations(choice, [_rule("excludes", "camp", "walk")])[0]["kind"] == "excludes"
    assert rules.violations(choice, [_rule("excludes", "camp", "jeep")]) == []
    assert rules.violations(choice, [_rule("requires", "camp", "jeep")])[0]["target_option_id"] == "jeep"
    assert rules.violations(choice, [_rule("requires", "camp", "walk"), _rule("requires", "jeep", "x")]) == []


def test_totals_deposit_and_tier() -> None:
    options = {
        "a": {"sell_amount": Decimal("250"), "net_amount": Decimal("150")},
        "b": {"sell_amount": Decimal("45"), "net_amount": None, "pay_on_arrival": True},
        "c": {"sell_amount": Decimal("100.005"), "net_amount": Decimal("60")},
    }
    sums = rules.totals({"B1": "a", "B2": "b", "B3": "c"}, options)
    assert sums == {"sell_total": Decimal("350.01"), "pay_on_arrival_total": Decimal("45.00"),
                    "net_total": Decimal("210.00"), "margin_total": Decimal("140.01")}  # fmt: skip
    options["c"]["net_amount"] = None
    assert rules.totals({"B3": "c"}, options)["margin_total"] is None
    assert rules.deposit_of(Decimal("1000"), Decimal("30")) == Decimal("300.00")
    assert rules.deposit_of(Decimal("0.01"), Decimal("1")) == Decimal("0.01")
    assert rules.deposit_of(Decimal("0"), Decimal("30")) == Decimal("0.00")
    opts = [{"option_id": "x", "tier": "premium", "availability": "sold_out"},
            {"option_id": "y", "tier": "premium", "availability": "instant"}]  # fmt: skip
    assert rules.tier_default(opts, "premium") == "y"
    assert rules.tier_default(opts, None) is None and rules.tier_default(opts, "budget") is None


PLAN = {"option_id": "00000000-0000-4000-8000-0000000000c1", "proposal_id": "00000000-0000-4000-8000-0000000000c2",
        "version": 3, "currency": "AUD", "tour_source_id": "T-100", "component_id": "tp:1",
        "component_kind": "trip_plan", "departure_id": None, "nights": 2, "service_date": "2026-12-01",
        "pax": {"adult": 2, "child": 1, "infant": 0, "rooms": [{"occupancy": "family", "count": 1}]}}  # fmt: skip


def test_the_quote_body_names_the_proposal_version() -> None:
    body = aa_options.quote_body(PLAN)
    assert body["proposal_ref"] == "crm-proposal:00000000-0000-4000-8000-0000000000c2"
    assert body["proposal_version"] == 3 and body["currency"] == "AUD"
    assert body["lines"] == [{"line_ref": PLAN["option_id"], "component_id": "tp:1", "service_date": "2026-12-01",
                              "pax": {"adult": 2, "child": 1, "infant": 0}, "nights": 2,
                              "rooms": [{"occupancy": "family", "count": 1}]}]  # fmt: skip


@pytest.mark.asyncio
async def test_quotes_and_refusals() -> None:
    fake = FakeBooking(b2b_price="900.00")
    got = await aa_options.quote(booking_client(fake), PLAN_TENANT, PLAN)
    assert got.b2b_price == Decimal("900.00") and got.availability == "available"
    assert got.from_price == Decimal("800.00") and got.quote_id == "q1"
    fake.from_price = None
    assert (await aa_options.quote(booking_client(fake), PLAN_TENANT, PLAN)).from_price is None
    fake.down = True
    with pytest.raises(Unavailable):
        await aa_options.quote(booking_client(fake), PLAN_TENANT, PLAN)
    answers, failed = [], []
    fake.down, fake.availability = False, "sold_out"
    answers, failed = await aa_options.refresh(booking_client(fake), PLAN_TENANT, [PLAN])
    assert answers[0][1].availability == "sold_out" and failed == []


@pytest.mark.parametrize(
    ("code", "kind", "detail"),
    [
        ("unknown_partner", Conflict, "aa_partner_not_active"),
        ("sold_out", Conflict, "aa_sold_out"),
        ("tour_not_served", Conflict, "aa_tour_not_served"),
        ("invalid", Invalid, "aa_quote_refused"),
    ],
)
def test_booking_refusals_map_to_codes(code: str, kind: type, detail: str) -> None:
    exc = aa_options._refusal(BookingRefusal(409, code))
    assert isinstance(exc, kind) and exc.code == detail


PLAN_TENANT = __import__("uuid").UUID("00000000-0000-4000-8000-0000000000c3")
