"""AA-1067 without a database: the payment schedule plans (money math per currency, instalments add up to the total),
the traveller cancellation policy (tiers, charge, decision, refunds never negative, the fee invoice amount), the
payment reminder triggers and library templates, and the services on a scripted connection. The SQL itself runs on
PostgreSQL in ``tests/integration/test_aa1067_retail_payments_api.py`` and ``test_crm049_schema.py`` /
``test_crm050_schema.py``."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest

from app import cancellation
from app.cancel_policy import rules
from app.cancel_policy import service as policy
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.payments import schedule_plan as sp
from app.payments import schedule_settings
from app.proposals import builder, text_render
from app.workflows import engine, library, model, tick
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import tenant_ctx

D = Decimal
TODAY = dt.date(2026, 11, 10)
START = dt.date(2027, 3, 1)
POLICY = uuid.UUID("00000000-0000-4000-8000-0000000c0001")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000c0002")
PROPOSAL = uuid.UUID("00000000-0000-4000-8000-0000000c0003")


def flat(sql: str) -> str:
    return " ".join(sql.split())


# ── Schedule plans: shapes ───────────────────────────────────────────────────────────────────────


def test_the_aa_default_is_30_now_40_at_t60_rest_at_t30() -> None:
    lines = sp.from_template(None, START)
    assert [(x.kind, x.seq, x.percent, x.due_on, x.days_before) for x in lines] == [
        ("deposit", 1, D("30"), None, None),
        ("instalment", 1, D("40"), START - dt.timedelta(days=60), 60),
        ("balance", 1, None, START - dt.timedelta(days=30), 30),
    ]
    assert sp.deposit_pct(lines) == D("30") and sp.balance_due_on(lines) == START - dt.timedelta(days=30)
    assert [x.due_on for x in sp.from_template(None, None)] == [None, None, None]  # no day 1 yet: dates follow


@pytest.mark.parametrize(
    ("template", "field"),
    [
        ([], "schedule"),
        ({"kind": "deposit"}, "schedule"),
        ([{"kind": "instalment", "percent": "50", "due": {"anchor": "before_start", "days": 9}}], "schedule.0.kind"),
        (
            [
                {"kind": "deposit", "percent": "30", "due": {"anchor": "accept"}},
                {"kind": "deposit", "percent": "10", "due": {"anchor": "accept"}},
            ],
            "schedule.0.kind",
        ),  # fmt: skip
        ([{"kind": "deposit", "percent": "0", "due": {"anchor": "accept"}}], "schedule.0.percent"),
        ([{"kind": "deposit", "percent": "30.123", "due": {"anchor": "accept"}}], "schedule.0.percent"),
        ([{"kind": "deposit", "percent": "x", "due": {"anchor": "accept"}}], "schedule.0.percent"),
        ([{"kind": "deposit", "percent": "30", "due": {"anchor": "before_start", "days": 3}}], "schedule.0.due"),
        ([{"kind": "deposit", "percent": "30", "due": {"anchor": "accept"}, "extra": 1}], "schedule.0"),
        (
            [
                {"kind": "deposit", "percent": "30", "due": {"anchor": "accept"}},
                {"kind": "balance", "percent": "70", "due": {"anchor": "before_start", "days": 30}},
            ],
            "schedule.1.percent",
        ),
        (
            [
                {"kind": "deposit", "percent": "30", "due": {"anchor": "accept"}},
                {"kind": "balance", "due": {"anchor": "accept"}},
            ],
            "schedule.1.due.anchor",
        ),
        (
            [
                {"kind": "deposit", "percent": "30", "due": {"anchor": "accept"}},
                {"kind": "instalment", "percent": "20", "due": {"anchor": "before_start", "days": 30}},
                {"kind": "balance", "due": {"anchor": "before_start", "days": 60}},
            ],
            "schedule.2.due.days",
        ),
        (
            [
                {"kind": "deposit", "percent": "30", "due": {"anchor": "accept"}},
                {"kind": "instalment", "percent": "70", "due": {"anchor": "before_start", "days": 30}},
                {"kind": "balance", "due": {"anchor": "before_start", "days": 10}},
            ],
            "schedule",
        ),  # nothing for the balance
        (
            [
                {"kind": "deposit", "percent": "30", "due": {"anchor": "accept"}},
                {"kind": "instalment", "percent": "60", "due": {"anchor": "before_start", "days": 30}},
            ],
            "schedule",
        ),
        (
            [
                {"kind": "deposit", "percent": "30", "due": {"anchor": "accept"}},
                {"kind": "balance", "due": {"anchor": "before_start", "days": 30}},
                {"kind": "instalment", "percent": "10", "due": {"anchor": "before_start", "days": 5}},
            ],
            "schedule.kind",
        ),
        (
            [
                {"kind": "deposit", "percent": "30", "due": {"anchor": "accept"}},
                {"kind": "instalment", "percent": "10", "due": {"anchor": "before_start", "days": 0}},
                {"kind": "balance", "due": {"anchor": "before_start", "days": 0}},
            ],
            "schedule.1.due.days",
        ),
    ],
)
def test_a_bad_template_is_refused_with_its_field(template: Any, field: str) -> None:
    with pytest.raises(Invalid) as err:
        sp.check_template(template)
    assert err.value.code == "invalid_schedule" and err.value.extra["field"] == field


def test_a_template_without_balance_adds_up_to_100_and_eleven_instalments_are_too_many() -> None:
    ok = sp.check_template([
        {"kind": "deposit", "percent": "50", "due": {"anchor": "accept"}},
        {"kind": "instalment", "percent": "50", "due": {"anchor": "before_start", "days": 14}},
    ])  # fmt: skip
    assert ok[1] == {"kind": "instalment", "percent": "50", "due": {"anchor": "before_start", "days": 14}}
    many = [{"kind": "deposit", "percent": "1", "due": {"anchor": "accept"}}] + [
        {"kind": "instalment", "percent": "1", "due": {"anchor": "before_start", "days": 300 - n}} for n in range(11)
    ]
    with pytest.raises(Invalid):
        sp.check_template(many)


def test_the_agent_plan_takes_dates_or_days_and_problems_block_publish() -> None:
    lines = sp.check_plan([
        {"kind": "deposit", "percent": "25"},
        {"kind": "instalment", "percent": "25", "due_on": "2027-01-15"},
        {"kind": "instalment", "percent": "25", "days_before": 40},
        {"kind": "balance", "due_on": None, "days_before": 20},
    ], START)  # fmt: skip
    assert [x.seq for x in lines] == [1, 1, 2, 1]
    assert lines[2].due_on == START - dt.timedelta(days=40) and lines[3].due_on == START - dt.timedelta(days=20)
    assert sp.problems(lines, D("1000"), "USD", START) == []
    undated = sp.check_plan([{"kind": "deposit", "percent": "30"}, {"kind": "balance"}], None)
    assert sp.problems(undated, D("1000"), "USD", None) == ["schedule_dates_required"]
    backwards = sp.check_plan([{"kind": "deposit", "percent": "30"},
                               {"kind": "instalment", "percent": "30", "due_on": "2027-02-20"},
                               {"kind": "balance", "due_on": "2027-02-01"}], START)  # fmt: skip
    assert sp.problems(backwards, D("1000"), "USD", START) == ["schedule_dates_order"]
    late = sp.check_plan([{"kind": "deposit", "percent": "30"}, {"kind": "balance", "due_on": "2027-03-05"}], START)
    assert sp.problems(late, D("1000"), "USD", START) == ["schedule_after_start"]
    tiny = sp.check_plan([{"kind": "deposit", "percent": "99.99"}, {"kind": "balance", "due_on": "2027-02-01"}],
                         START)  # fmt: skip
    assert sp.problems(tiny, D("100"), "VND", START) == ["schedule_amount_zero"]  # 99.99 % of 100 VND rounds to 100
    for bad in ([{"kind": "deposit", "percent": "30", "due_on": "2027-01-01"}], "x",
                [{"kind": "deposit", "percent": "30"}, {"kind": "balance", "percent": "5"}],
                [{"kind": "deposit", "percent": "30"}, {"kind": "balance", "due_on": "not a date"}],
                [{"kind": "deposit", "percent": "30"}, {"kind": "balance", "days_before": -1}],
                [{"kind": "deposit", "percent": "30", "colour": "red"}]):  # fmt: skip
        with pytest.raises(Invalid):
            sp.check_plan(bad, START)


def test_redate_moves_only_the_lines_that_follow_the_template_and_json_round_trips() -> None:
    lines = sp.check_plan([{"kind": "deposit", "percent": "30"},
                           {"kind": "instalment", "percent": "40", "due_on": "2027-01-02"},
                           {"kind": "balance", "days_before": 30}], START)  # fmt: skip
    moved = sp.redate(lines, START + dt.timedelta(days=10))
    assert moved[1].due_on == dt.date(2027, 1, 2) and moved[2].due_on == START - dt.timedelta(days=20)
    stored = sp.to_json(moved)
    assert sp.from_json(json.dumps(stored)) == moved and sp.from_json(stored) == moved and sp.from_json(None) is None
    assert sp.hash_record(moved)[2] == ["balance", 1, None, (START - dt.timedelta(days=20)).isoformat()]


# ── Schedule plans: money ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("total", "currency", "expected"),
    [
        ("1000.00", "USD", ["300.00", "400.00", "300.00"]),
        ("999.99", "USD", ["300.00", "400.00", "299.99"]),  # 299.997 -> 300.00, 399.996 -> 400.00, the rest
        ("1234.57", "EUR", ["370.37", "493.83", "370.37"]),
        ("10000001.00", "VND", ["3000000.00", "4000000.00", "3000001.00"]),  # whole dong, the balance the rest
        ("12345.00", "JPY", ["3704.00", "4938.00", "3703.00"]),
        ("0.01", "USD", ["0.01", "0.00", "0.00"]),  # the deposit is at least one unit, never above the total
    ],
)
def test_split_rounds_per_currency_and_always_adds_up_to_the_total(
    total: str, currency: str, expected: list[str]
) -> None:
    lines = sp.from_template(None, START)
    amounts = sp.split(D(total), lines, currency)
    assert [str(a) for a in amounts] == expected
    assert sum(amounts, D(0)) == D(total)


def test_split_sum_holds_for_many_totals_and_plans() -> None:
    plans = [
        sp.from_template(None, START),
        sp.check_plan(
            [
                {"kind": "deposit", "percent": "33.33"},
                {"kind": "instalment", "percent": "33.33", "due_on": "2027-01-01"},
                {"kind": "instalment", "percent": "33.34", "due_on": "2027-02-01"},
            ],
            START,
        ),  # fmt: skip
        sp.check_plan([{"kind": "deposit", "percent": "12.5"}, {"kind": "balance", "due_on": "2027-01-01"}], START),
    ]
    for plan in plans:
        for currency in ("USD", "VND", "KRW", "GBP"):
            for cents in (1, 99, 101, 333_33, 1_000_000_07, 123_456_789_01):
                total = D(cents) / 100
                assert sum(sp.split(total, plan, currency), D(0)) == total.quantize(D("0.01"))
    assert sp.split(D("0"), plans[0], "USD") == [D("0.00")] * 3 and sp.split(D("5"), [], "USD") == []


def test_accept_rows_date_a_past_line_today_and_skip_nothing_with_money() -> None:
    lines = sp.from_template(None, dt.date(2026, 12, 1))  # T-60 is already past on TODAY
    rows = sp.accept_rows(lines, D("2000"), "USD", TODAY)
    assert [(r["kind"], r["seq"], r["amount"], r["due_on"]) for r in rows] == [
        ("deposit", 1, D("600.00"), TODAY),
        ("instalment", 1, D("800.00"), TODAY),
        ("balance", 1, D("600.00"), dt.date(2026, 11, 1) if dt.date(2026, 11, 1) > TODAY else TODAY),
    ]
    assert sum(r["amount"] for r in rows) == D("2000")
    want = {"kind": "instalment", "seq": 1, "percent": "40", "due_on": "2026-10-02", "days_before": 60,
            "amount": "800.00"}  # fmt: skip
    assert sp.view(lines, D("2000"), "USD")[1] == want


def test_text_render_lists_the_instalments() -> None:
    snap = text_render.Snapshot(
        kind="builder", version=1, price_hash="ab" * 32, agency_name="Lotus Trails", trip_title="Island loop",
        currency="USD", total=D("1000"), deposit=D("300"), balance_due_on=dt.date(2027, 1, 30),
        schedule=(("deposit", 1, D("300"), None), ("instalment", 1, D("400"), dt.date(2026, 12, 31)),
                  ("balance", 1, D("300"), dt.date(2027, 1, 30))),
    )  # fmt: skip
    text = text_render.proposal_block(snap, "email", "en")
    assert "Deposit to confirm: 300.00 USD" in text and "Instalment 1: 400.00 USD, due 31 Dec 2026" in text
    assert "Balance: 300.00 USD, due 30 Jan 2027" in text
    assert "Đợt thanh toán 1: 400,00 USD" in text_render.proposal_block(snap, "email", "vi")


# ── Cancellation policy: rules ───────────────────────────────────────────────────────────────────


def test_tiers_are_checked_and_sorted_most_days_first() -> None:
    tiers = rules.check_tiers([{"days_before_min": 0, "charge_pct": "100"},
                               {"days_before_min": 30, "charge_pct": "10", "min_charge_amount": "50"}])  # fmt: skip
    assert [t.days_before_min for t in tiers] == [30, 0] and tiers[0].min_charge_amount == D("50")
    for bad in ([], [{"days_before_min": 5, "charge_pct": "10"}],  # no 0-day line
                [{"days_before_min": 0, "charge_pct": "10"}, {"days_before_min": 0, "charge_pct": "20"}],
                [{"days_before_min": 0, "charge_pct": "101"}], [{"days_before_min": -1, "charge_pct": "1"}],
                [{"days_before_min": 0, "charge_pct": "1", "min_charge_amount": "0"}],
                [{"days_before_min": 0, "charge_pct": "1.234"}], [{"days_before_min": True, "charge_pct": "1"}],
                [{"days_before_min": 0, "charge_pct": "1", "x": 1}], "tiers"):  # fmt: skip
        with pytest.raises(Invalid):
            rules.check_tiers(bad)


@pytest.mark.parametrize(
    ("days", "pct", "amount"),
    [(90, "10", "100.00"), (31, "10", "100.00"), (30, "25", "250.00"), (15, "25", "250.00"), (14, "50", "500.00"),
     (8, "50", "500.00"), (7, "100", "1000.00"), (0, "100", "1000.00"), (-3, "100", "1000.00"),
     (None, "100", "1000.00")],
)  # fmt: skip
def test_the_default_table_charges_by_days_before_departure(days: int | None, pct: str, amount: str) -> None:
    tier, charge = rules.policy_charge(D("1000"), days, rules.default_tiers(), "USD", None)
    assert (str(tier.charge_pct), str(charge)) == (pct, amount)


def test_minimum_charge_in_the_policy_currency_and_never_above_the_total() -> None:
    tiers = [rules.Tier(30, D("5"), D("80")), rules.Tier(0, D("100"))]
    assert rules.policy_charge(D("1000"), 40, tiers, "USD", "USD")[1] == D("80.00")  # 5 % = 50 < minimum 80
    assert rules.policy_charge(D("1000"), 40, tiers, "EUR", "USD")[1] == D("50.00")  # another currency: no minimum
    assert rules.policy_charge(D("60"), 40, tiers, "USD", "USD")[1] == D("60.00")  # at most the total
    assert rules.policy_charge(D("1000001"), 40, tiers, "VND", None)[1] == D("50000.00")  # whole dong


def test_decisions_overrides_need_a_reason_and_stay_within_the_total() -> None:
    assert rules.check_decision(None) == rules.Decision("policy")
    assert rules.check_decision({"mode": "policy"}) == rules.Decision("policy")
    waive = rules.check_decision({"mode": "waive", "reason": "  Medical  note "})
    assert waive == rules.Decision("waive", D("0.00"), "Medical note")
    override = rules.check_decision({"mode": "override", "amount": "120.50", "reason": "Rebooked"})
    assert rules.final_charge(D("250"), override, D("1000")) == ("override", D("120.50"))
    assert rules.final_charge(D("250"), waive, D("1000")) == ("waived", D("0.00"))
    assert rules.final_charge(None, rules.Decision("policy"), D("1000")) == ("no_policy", D("0.00"))
    assert rules.final_charge(D("250"), rules.Decision("policy"), D("1000")) == ("policy", D("250"))
    with pytest.raises(Invalid):
        rules.final_charge(D("250"), rules.Decision("override", D("1000.01"), "x"), D("1000"))
    for bad in ({"mode": "refund"}, {"mode": "waive"}, {"mode": "override", "reason": "x"},
                {"mode": "override", "amount": "-1", "reason": "x"}, {"mode": "override", "amount": "1.001",
                                                                      "reason": "x"},
                {"mode": "waive", "reason": "x" * 501}, {"x": 1}, "waive"):  # fmt: skip
        with pytest.raises(Invalid):
            rules.check_decision(bad)


def test_refunds_keep_the_charge_from_the_oldest_payments_and_are_never_negative() -> None:
    payments = [("p1", D("300")), ("p2", D("200")), ("p3", D("-5")), ("p4", D("100"))]
    assert rules.allocate(payments, D("350")) == {"p1": D("0.00"), "p2": D("150.00"), "p3": D("0.00"),
                                                  "p4": D("100.00")}  # fmt: skip
    assert rules.allocate(payments, D("0")) == {"p1": D("300.00"), "p2": D("200.00"), "p3": D("0.00"),
                                                "p4": D("100.00")}  # fmt: skip
    assert all(v == 0 for v in rules.allocate(payments, D("10000")).values())
    assert rules.refund_due(D("600"), D("350")) == D("250.00") and rules.refund_due(D("100"), D("350")) == D("0.00")
    assert rules.invoice_due(D("350"), D("100")) == D("250.00") and rules.invoice_due(D("350"), D("600")) == 0
    for charge in (D("0"), D("1"), D("299.99"), D("600"), D("601")):
        refunds = rules.allocate(payments, charge)
        assert all(v >= 0 for v in refunds.values())
        assert sum(refunds.values(), D(0)) == max(D("600") - charge, D(0))


def test_aa_fee_warnings() -> None:
    fees = [{"amount": "120.00", "currency": "USD"}, {"amount": "30.00", "currency": "USD"}, None]
    assert rules.aa_fee_warnings(D("100"), "USD", fees) == ["charge_below_aa_fee"]
    assert rules.aa_fee_warnings(D("150"), "USD", fees) == []
    assert rules.aa_fee_warnings(D("100"), "USD", [{"amount": "120", "currency": "EUR"}]) == ["aa_fee_other_currency"]
    preview: dict[str, Any] = {"charge": {"currency": "USD", "charge_amount": "100.00", "warnings": ["x"]},
                               "aa_bookings": [{"fee": f} for f in fees]}  # fmt: skip
    out = cancellation.charge_vs_aa_fees(preview)["charge"]
    assert out["aa_fee_amount"] == "150.00" and out["warnings"] == ["charge_below_aa_fee", "x"]
    assert cancellation.charge_vs_aa_fees({"charge": {"currency": None}}) == {"charge": {"currency": None}}


def test_policy_text_in_both_languages() -> None:
    text = rules.policy_text("Standard", 2, rules.default_tiers(), None, "en")
    assert text.splitlines() == [
        "Cancellation policy: Standard (version 2)",
        "- 31 days or more before departure: 10 % of the trip price",
        "- 15 to 30 days before departure: 25 % of the trip price",
        "- 8 to 14 days before departure: 50 % of the trip price",
        "- Fewer than 8 days before departure, or after the trip starts: 100 % of the trip price",
    ]
    vi = rules.policy_text("Chuẩn", 1, [rules.Tier(0, D("20"), D("50"))], "USD", "vi")
    assert vi.splitlines()[1] == "- Mọi trường hợp huỷ: 20 % giá chuyến đi (tối thiểu 50.00 USD)"
    assert rules.tiers_view([rules.Tier(0, D("20"))]) == [{"days_before_min": 0, "charge_pct": "20",
                                                           "min_charge_amount": None}]  # fmt: skip


# ── Cancellation policy: the service on a scripted connection ────────────────────────────────────


def _policy_row(**over: Any) -> dict[str, Any]:
    row = {
        "policy_id": POLICY,
        "name": "Standard",
        "version": 1,
        "replaces_policy_id": None,
        "currency": None,
        "is_default": True,
        "published_at": dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
        "retired_at": None,
        "created_at": dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
        "updated_at": dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
    }
    row.update(over)
    return row


def _tier_rows(policy_id: uuid.UUID = POLICY) -> list[dict[str, Any]]:
    return [{"policy_id": policy_id, "days_before_min": t.days_before_min, "charge_pct": t.charge_pct,
             "min_charge_amount": None} for t in rules.default_tiers()]  # fmt: skip


def _facts(**over: Any) -> dict[str, Any]:
    row = {
        "start_date": TODAY + dt.timedelta(days=20),
        "proposal_id": PROPOSAL,
        "cancellation_policy_id": POLICY,
        "currency": "USD",
        "kind": "builder",
        "total": D("1000"),
    }
    row.update(over)
    return row


def _world(**facts: Any) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on(flat(policy.TRIP_FACTS_SQL), _facts(**facts))
    conn.on("AT TIME ZONE", TODAY)
    conn.on(flat(policy.ONE_SQL), _policy_row())
    conn.on(flat(policy.DEFAULT_SQL), _policy_row())
    conn.on(flat(policy.TIERS_SQL), _tier_rows())
    conn.on("refund_owner_threshold_base", D("100"))
    conn.on("SELECT base_currency", {"base_currency": "USD", "booking_validation_level": "basic",
                                     "fx_mode": "automatic"})  # fmt: skip
    return conn


PAID = [{"payment_id": "p1", "refundable": "300.00", "currency": "USD"},
        {"payment_id": "p2", "refundable": "100.00", "currency": "USD"}]  # fmt: skip


@pytest.mark.asyncio
async def test_assess_charges_the_frozen_policy_and_splits_the_refund() -> None:
    a = await policy.assess(tenant_ctx(_world(), role="agent"), TRIP, PAID, rules.Decision("policy"))
    assert (a.days_before, a.tier.charge_pct, a.policy_charge, a.charge) == (20, D("25"), D("250.00"), D("250.00"))
    assert a.refunds == {"p1": D("50.00"), "p2": D("100.00")} and a.kept == D("250.00")
    assert (a.refund_due, a.invoice_due, a.needs_owner) == (D("150.00"), D("0.00"), False)
    view = a.view()
    assert view["policy"] == {"policy_id": str(POLICY), "name": "Standard", "version": 1, "frozen": True}
    assert view["tier"] == {"days_before_min": 15, "charge_pct": "25"} and view["warnings"] == []
    big = await policy.assess(tenant_ctx(_world(start_date=TODAY + dt.timedelta(days=3)), role="agent"), TRIP, PAID,
                              rules.Decision("policy"))  # fmt: skip
    assert (big.charge, big.refund_due, big.invoice_due) == (D("1000.00"), D("0.00"), D("600.00"))


@pytest.mark.asyncio
async def test_an_agent_lowering_the_charge_above_the_threshold_needs_the_owner() -> None:
    waive = rules.Decision("waive", D("0.00"), "Goodwill")
    agent = await policy.assess(tenant_ctx(_world(), role="agent"), TRIP, PAID, waive)
    assert agent.decision == "waived" and agent.needs_owner  # 250 lowered > 100 threshold
    with pytest.raises(Forbidden) as err:
        policy.require_allowed(tenant_ctx(_world(), role="agent"), agent, TRIP)
    assert err.value.code == "cancel_charge_owner_only"
    owner = await policy.assess(tenant_ctx(_world(), role="owner"), TRIP, PAID, waive)
    assert not owner.needs_owner
    policy.require_allowed(tenant_ctx(_world(), role="owner"), owner, TRIP)
    small = await policy.assess(tenant_ctx(_world(), role="agent"), TRIP, PAID,
                                rules.Decision("override", D("200"), "Partial rebook"))  # fmt: skip
    assert not small.needs_owner  # 50 lowered, under the threshold
    raised = await policy.assess(tenant_ctx(_world(), role="agent"), TRIP, PAID,
                                 rules.Decision("override", D("900"), "Late change"))  # fmt: skip
    assert not raised.needs_owner and raised.invoice_due == D("500.00")


@pytest.mark.asyncio
async def test_assess_without_a_frozen_policy_uses_the_default_and_says_so() -> None:
    conn = _world(cancellation_policy_id=None)
    a = await policy.assess(tenant_ctx(conn), TRIP, PAID, rules.Decision("policy"))
    assert a.charge == D("250.00") and "policy_not_frozen" in a.warnings and not a.frozen
    none = _world(cancellation_policy_id=None, start_date=None)
    none.on(flat(policy.DEFAULT_SQL), None)
    b = await policy.assess(tenant_ctx(none), TRIP, PAID + [{"payment_id": "p9", "refundable": "5", "currency": "EUR"}],
                            rules.Decision("policy"))  # fmt: skip
    assert b.decision == "no_policy" and b.charge == 0 and b.refunds == {"p1": D("300.00"), "p2": D("100.00")}
    assert {"no_start_date", "payments_other_currency"} <= set(b.warnings)
    gone = ScriptedConn()
    with pytest.raises(NotFound):
        await policy.assess(tenant_ctx(gone), TRIP, [], rules.Decision("policy"))


@pytest.mark.asyncio
async def test_record_and_read_the_charge() -> None:
    conn = _world()
    charge_id = uuid.uuid4()
    conn.on("INSERT INTO crm.cancellation_charge", charge_id)
    a = await policy.assess(tenant_ctx(conn), TRIP, PAID, rules.Decision("policy"))
    assert await policy.record(tenant_ctx(conn), TRIP, a, None) == charge_id
    (args,) = conn.sql("INSERT INTO crm.cancellation_charge")
    assert args[9:] == ("policy", None, D("400.00"), D("250.00"), D("150.00"), D("0.00"), None)
    assert cancellation.fee_description(a) == (
        "Cancellation charge (25 % under Standard v1, 20 days before departure): 250.00 USD, less 250.00 USD already "
        "paid"
    )
    row = {"charge_id": charge_id, "trip_id": TRIP, "proposal_id": PROPOSAL, "policy_id": POLICY, "currency": "USD",
           "trip_total": D("1000"), "days_before": 20, "charge_pct": D("25"), "policy_charge": D("250"),
           "charge_amount": D("250"), "decision": "policy", "reason": None, "paid_amount": D("400"),
           "kept_amount": D("250"), "refund_due": D("150"), "invoiced_amount": D("0"), "invoice_id": None,
           "created_at": dt.datetime(2026, 11, 10, tzinfo=dt.UTC), "created_by": "member:x"}  # fmt: skip
    conn.on(flat(policy.CHARGE_SQL), row)
    seen = await policy.charge_of(tenant_ctx(conn), TRIP)
    assert seen is not None and seen["charge_amount"] == "250.00" and seen["refund_due"] == "150.00"
    assert await policy.charge_of(tenant_ctx(ScriptedConn()), TRIP) is None
    empty = policy.Assessment(None, D(0), None, None, None, None, "no_policy", D(0), None, D(0), {}, None, False)
    with pytest.raises(Conflict):
        await policy.record(tenant_ctx(conn), TRIP, empty, None)


@pytest.mark.asyncio
async def test_policy_settings_lifecycle_on_a_scripted_connection() -> None:
    conn = ScriptedConn()
    conn.on("SELECT count(*) FROM crm.cancellation_policy", 0)
    conn.on(flat(policy.TIERS_SQL), lambda ids: [r for r in _tier_rows(ids[0])])
    draft = _policy_row(is_default=False, published_at=None)
    conn.on(flat(policy.LOCK_SQL), draft)
    conn.on(flat(policy.ONE_SQL), draft)
    made = await policy.create(tenant_ctx(conn), {"aa_default": True})
    assert made["status"] == "draft" and made["tiers"][0] == {"days_before_min": 31, "charge_pct": "10",
                                                              "min_charge_amount": None}  # fmt: skip
    assert len(conn.sql("INSERT INTO crm.cancellation_policy_tier")) == 4
    assert made["text"]["en"].startswith("Cancellation policy: Standard (version 1)")
    edited = await policy.update_draft(tenant_ctx(conn), POLICY, {"name": "Flexible", "tiers": [
        {"days_before_min": 0, "charge_pct": "50"}]})  # fmt: skip
    assert edited["policy_id"] == str(POLICY) and conn.sql(flat(policy.UPDATE_DRAFT_SQL))[-1][1] == "Flexible"
    with pytest.raises(Invalid):  # a minimum needs the policy currency
        await policy.update_draft(tenant_ctx(conn), POLICY, {"tiers": [
            {"days_before_min": 0, "charge_pct": "50", "min_charge_amount": "10"}]})  # fmt: skip
    conn.on(flat(policy.DEFAULT_SQL), None)
    await policy.publish(tenant_ctx(conn), POLICY, make_default=False)
    assert conn.sql(flat(policy.PUBLISH_SQL)) and conn.sql(flat(policy.SET_DEFAULT_SQL))  # the first one is default
    assert (await policy.delete_draft(tenant_ctx(conn), POLICY))["deleted"]
    listing = await policy.listing(tenant_ctx(conn.on(flat(policy.LIST_SQL), [draft])))
    assert listing["aa_default"]["tiers"][-1]["charge_pct"] == "100" and listing["policies"][0]["status"] == "draft"
    # Published: frozen, a new version is drafted from it, retired once.
    live = _policy_row()
    pub = ScriptedConn().on(flat(policy.LOCK_SQL), live).on(flat(policy.ONE_SQL), live)
    pub.on(flat(policy.TIERS_SQL), _tier_rows()).on("SELECT count(*) FROM crm.cancellation_policy", 3)
    for call in (
        policy.update_draft(tenant_ctx(pub), POLICY, {"name": "X"}),
        policy.delete_draft(tenant_ctx(pub), POLICY),
        policy.publish(tenant_ctx(pub), POLICY, make_default=True),
    ):
        with pytest.raises(Conflict):
            await call
    await policy.create(tenant_ctx(pub), {"from_policy_id": str(POLICY)})
    (args,) = pub.sql(flat(policy.INSERT_SQL))
    assert args[1:] == ("Standard", 2, POLICY, None)
    await policy.set_default(tenant_ctx(pub), POLICY)
    await policy.retire(tenant_ctx(pub), POLICY)
    assert pub.sql(flat(policy.RETIRE_SQL))
    pub.on(flat(policy.DRAFT_OF_SQL), uuid.uuid4())
    with pytest.raises(Conflict):
        await policy.create(tenant_ctx(pub), {"from_policy_id": str(POLICY)})
    full = ScriptedConn().on("SELECT count(*) FROM crm.cancellation_policy", 50)
    with pytest.raises(Conflict):
        await policy.create(tenant_ctx(full), {"aa_default": True})
    for body in ({"name": "", "tiers": [{"days_before_min": 0, "charge_pct": "1"}]},
                 {"name": "X", "currency": "usd", "tiers": [{"days_before_min": 0, "charge_pct": "1"}]}):  # fmt: skip
        with pytest.raises(Invalid):
            await policy.create(tenant_ctx(ScriptedConn().on("SELECT count(*)", 0)), body)


@pytest.mark.asyncio
async def test_a_new_version_takes_over_the_default_and_retires_the_old_one() -> None:
    old_id = uuid.uuid4()
    draft = _policy_row(is_default=False, published_at=None, version=2, replaces_policy_id=old_id)
    conn = ScriptedConn().on(flat(policy.ONE_SQL), draft)
    conn.on(flat(policy.TIERS_SQL), _tier_rows())
    conn.on(flat(policy.LOCK_SQL), lambda pid: draft if pid == POLICY else _policy_row(policy_id=old_id))
    conn.on(flat(policy.DEFAULT_SQL), _policy_row(policy_id=old_id))
    await policy.publish(tenant_ctx(conn), POLICY, make_default=False)
    assert conn.sql(flat(policy.RETIRE_SQL)) == [(old_id,)] and conn.sql(flat(policy.SET_DEFAULT_SQL)) == [(POLICY,)]


@pytest.mark.asyncio
async def test_pin_choice_summary_and_merge_text() -> None:
    conn = _world()
    pinned = await policy.pin(tenant_ctx(conn), {"proposal_id": PROPOSAL, "cancellation_policy_id": None})
    assert pinned["cancellation_policy_id"] == POLICY and conn.sql(flat(policy.PIN_SQL)) == [(PROPOSAL, POLICY)]
    same = {"proposal_id": PROPOSAL, "cancellation_policy_id": POLICY}
    assert await policy.pin(tenant_ctx(conn), same) is same
    empty = ScriptedConn()
    assert await policy.pin(tenant_ctx(empty), {"proposal_id": PROPOSAL, "cancellation_policy_id": None})
    assert await policy.check_choice(tenant_ctx(conn), POLICY) == POLICY
    assert await policy.check_choice(tenant_ctx(conn), None) is None
    retired = ScriptedConn().on(flat(policy.ONE_SQL), _policy_row(retired_at=dt.datetime.now(dt.UTC),
                                                                  is_default=False))  # fmt: skip
    with pytest.raises(Conflict):
        await policy.check_choice(tenant_ctx(retired), POLICY)
    text = await policy.merge_text(tenant_ctx(conn), POLICY, "vi")
    assert text is not None and text.startswith("Chính sách huỷ: Standard")
    assert await policy.merge_text(tenant_ctx(conn), None) is None
    assert await policy.summary(tenant_ctx(ScriptedConn()), POLICY) is None


# ── Schedule template settings, builder plan changes ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_template_settings() -> None:
    conn = ScriptedConn()
    seen = await schedule_settings.read(tenant_ctx(conn))
    assert seen["is_default"] and seen["template"][1]["percent"] == "40"
    lines = [{"kind": "deposit", "percent": "50", "due": {"anchor": "accept"}},
             {"kind": "balance", "due": {"anchor": "before_start", "days": 45}}]  # fmt: skip
    out = await schedule_settings.update(tenant_ctx(conn), lines)
    assert not out["is_default"] and json.loads(conn.sql(flat(schedule_settings.UPSERT_SQL))[0][0]) == lines
    back = await schedule_settings.update(tenant_ctx(conn), None)
    assert back["is_default"] and conn.sql(flat(schedule_settings.UPSERT_SQL))[-1] == (None,)
    assert (await schedule_settings.read(tenant_ctx(ScriptedConn().on("schedule_template", json.dumps(lines)))))[
        "template"] == lines  # fmt: skip


def test_builder_plan_changes() -> None:
    plan = sp.from_template(None, START)
    assert builder._plan_changes({}, None, START, None) is None
    assert builder._plan_changes({"schedule": None}, plan, START, None) is None
    assert builder._plan_changes({"schedule_reset": True}, None, START, None) == plan
    deposit = builder._plan_changes({"deposit_pct": D("20")}, plan, START, None)
    assert deposit is not None and deposit[0].percent == D("20") and deposit[2] == plan[2]
    dated = builder._plan_changes({"balance_due_on": dt.date(2027, 2, 14)}, plan, START, None)
    assert dated is not None and (dated[2].due_on, dated[2].days_before) == (dt.date(2027, 2, 14), None)
    moved = builder._plan_changes({"starts_on": START + dt.timedelta(days=7)}, plan, START + dt.timedelta(days=7),
                                  None)  # fmt: skip
    assert moved is not None and moved[1].due_on == plan[1].due_on + dt.timedelta(days=7)
    assert builder._plan_changes({"intro": "x"}, plan, START, None) == plan
    full = builder._plan_changes({"deposit_pct": D("100")}, plan, START, None)
    assert full == [sp.Line("deposit", 1, D("100"), None, None)]  # paid in full at accept
    with pytest.raises(Invalid):  # 70 % deposit + 40 % instalment leaves nothing for the balance
        builder._plan_changes({"deposit_pct": D("70")}, plan, START, None)
    s = {"options": [], "blocks": [], "discount_pct": None, "schedule_plan": plan, "currency": "VND"}
    assert builder._charged(s, None, None)["deposit"] == D("0.00")  # nothing chosen, nothing charged


# ── Payment reminders ────────────────────────────────────────────────────────────────────────────


def test_schedule_triggers_validate_their_offsets() -> None:
    def rule(value: str, offset: int) -> dict[str, Any]:
        return {"name": "Remind", "trigger": {"kind": "date_anchor", "value": value, "offset_days": offset},
                "steps": [{"action": "create_task", "title": "Call the client"}]}  # fmt: skip

    assert model.validate(rule("schedule_due_soon", -7)).trigger_offset_days == -7
    assert model.validate(rule("schedule_overdue", 3)).trigger_value == "schedule_overdue"
    for value, offset in (("schedule_due_soon", 3), ("schedule_due_soon", -61), ("schedule_overdue", 0),
                          ("schedule_overdue", 61)):  # fmt: skip
        with pytest.raises(Invalid):
            model.validate(rule(value, offset))
    sid = uuid.uuid4()
    key = tick.reminder_key(sid, "schedule_overdue", 3, dt.date(2026, 11, 1))
    assert key == f"schedule:{sid}:schedule_overdue:3:2026-11-01" and model.schedule_id_of(key) == str(sid)
    assert model.schedule_id_of("date:x") is None and model.schedule_id_of(None) is None
    assert model.schedule_id_of("schedule:") is None


def test_the_reminder_library_templates_are_opt_in_with_a_starter_message() -> None:
    for key in ("payment_due_soon", "payment_overdue"):
        t = library.get(key)
        assert t is not None and t.starts_paused and set(t.starter_mail) == {"en", "vi"}
        rule = model.validate(t.rule(), draft=True)
        assert model.missing_templates(rule.steps) == [0]
        for title, body in t.starter_mail.values():
            fields = set(text_render._FIELD.findall(title + body))
            assert fields <= set(text_render.MERGE_FIELDS) and {"payment_amount", "payment_due_date"} <= fields
    listed = {t["key"]: t for t in library.listing()}
    assert listed["payment_due_soon"]["starts_paused"] and listed["payment_overdue"]["starter_mail"] == ["en", "vi"]


@pytest.mark.asyncio
async def test_reminder_merge_fields_and_paid_meanwhile() -> None:
    sid = uuid.uuid4()
    run = {"trigger_key": f"schedule:{sid}:schedule_due_soon:-7:2026-12-01", "trip_id": TRIP}
    row = {"schedule_id": sid, "kind": "instalment", "seq": 1, "status": "due", "open_amount": D("400"),
           "currency": "USD", "due_on": dt.date(2026, 12, 1)}  # fmt: skip
    conn = ScriptedConn().on(flat(engine.SCHEDULE_ROW_SQL), row)
    assert await engine.schedule_row(tenant_ctx(conn), run) == row
    assert not engine._paid_meanwhile(run, row)
    assert engine._paid_meanwhile(run, {**row, "open_amount": D("0")}) and engine._paid_meanwhile(run, None)
    assert not engine._paid_meanwhile({"trigger_key": "date:x"}, None)
    assert await engine.schedule_row(tenant_ctx(conn), {"trigger_key": "schedule:nope:x", "trip_id": TRIP}) is None
    assert await engine.schedule_row(tenant_ctx(conn), {"trigger_key": "event:x", "trip_id": TRIP}) is None
    subject = engine.Subject(None, None, None)
    values = await engine.merge_values(tenant_ctx(conn.on("display_name", "Lotus Trails")), None, subject, "en", row)  # type: ignore[arg-type]
    assert values["payment_amount"] == "400.00 USD" and values["payment_due_date"] == "1 Dec 2026"
