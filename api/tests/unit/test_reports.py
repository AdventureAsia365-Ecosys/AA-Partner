"""AA-966 (card CRM-34) without a database: the report window and comparison (R28, agency time zone), the funnel,
lead-source, money and success-metric arithmetic, the CSV formula guard, the insight suppression rule (property
test: no published cell under 10 travellers or 3 agencies; an opted-out agency never counts) and every reports route
(the SQL runs on PostgreSQL in tests/integration/test_reports_api.py).

Dates are fixed in 2031, far from the real clock (lessons "fixed dates near now are a time bomb"): the scripted
settings row is the only "today" the code sees.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import random
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.errors import Forbidden, Invalid
from app.main import create_app
from app.reports import export, insights, period, service
from app.reports import queries as q
from app.routers import reports as reports_router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, TENANT, fake_tx, tenant_ctx

TODAY = dt.date(2031, 5, 20)
AGENT = uuid.UUID("00000000-0000-4000-8000-0000000000a9")
TRIP_A = uuid.UUID("00000000-0000-4000-8000-00000000aa01")
TRIP_B = uuid.UUID("00000000-0000-4000-8000-00000000aa02")


# ── period ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_windows_end_today_in_the_agency_calendar() -> None:
    assert period.window("30d", TODAY) == period.Window(dt.date(2031, 4, 21), TODAY)
    assert period.window("90d", TODAY) == period.Window(dt.date(2031, 2, 20), TODAY)
    assert period.window("12m", TODAY) == period.Window(dt.date(2030, 6, 1), TODAY)
    assert period.window("ytd", TODAY) == period.Window(dt.date(2031, 1, 1), TODAY)
    assert period.window("12m", dt.date(2031, 1, 3)).start == dt.date(2030, 2, 1)
    with pytest.raises(Invalid):
        period.window("7d", TODAY)
    win = period.Window(dt.date(2031, 1, 1), dt.date(2031, 1, 31))
    assert win.end_excl == dt.date(2031, 2, 1) and win.as_dict() == {"from": "2031-01-01", "to": "2031-01-31"}


def test_shift_years_keeps_29_february_inside_the_month() -> None:
    leap = period.Window(dt.date(2032, 2, 29), dt.date(2032, 3, 1))
    assert leap.shift_years(-1) == period.Window(dt.date(2031, 2, 28), dt.date(2031, 3, 1))


def test_compare_needs_a_year_of_history_else_three_months() -> None:
    win = period.window("30d", TODAY)
    assert period.compare_mode(None, win) == "none"
    assert period.compare_mode(dt.date(2030, 4, 21), win) == "previous_year"  # on the earlier window's first day
    assert period.compare_mode(dt.date(2030, 4, 22), win) == "trend_3m"
    months = period.trend_months(period.window("12m", dt.date(2031, 2, 10)))
    assert [(m.start, m.end) for m in months] == [
        (dt.date(2030, 12, 1), dt.date(2030, 12, 31)),
        (dt.date(2031, 1, 1), dt.date(2031, 1, 31)),
        (dt.date(2031, 2, 1), dt.date(2031, 2, 10)),
    ]
    assert period.months(period.Window(dt.date(2030, 11, 15), dt.date(2031, 1, 2))) == [
        dt.date(2030, 11, 1), dt.date(2030, 12, 1), dt.date(2031, 1, 1),
    ]  # fmt: skip


# ── arithmetic ─────────────────────────────────────────────────────────────────────────────────────────────
def _row(stage: str, rank: int, n: int, source: str = "website_form", owner: uuid.UUID | None = AGENT,
         budget: bool = True) -> service.CohortRow:  # fmt: skip
    return service.CohortRow(stage=stage, owner_member_id=owner, lead_source=source, has_budget=budget,
                             max_rank=rank, n=n)  # fmt: skip


COHORT = [
    _row("inquiry", 1, 3, budget=False),
    _row("proposal", 3, 2, source="referral"),
    _row("booked", 4, 1),
    _row("lost", 3, 2, source=""),
    _row("cancelled", 4, 1, source="referral", owner=None),
]


def test_funnel_counts_every_stage_a_trip_reached() -> None:
    f = service.funnel(COHORT)
    assert f["created"] == 9 and f["won"] == 2 and f["win_rate_pct"] == 22.2
    assert [s["count"] for s in f["stages"]] == [9, 6, 6, 2, 0, 0, 0]
    assert f["lost"] == 2 and f["cancelled"] == 1
    assert service.funnel([])["win_rate_pct"] is None


def test_lead_sources_rank_by_trips_and_name_the_unset_source_none() -> None:
    assert service.lead_sources(COHORT) == [
        {"source": "website_form", "trips": 4, "won": 1, "win_rate_pct": 25.0},
        {"source": "referral", "trips": 3, "won": 1, "win_rate_pct": 33.3},
        {"source": None, "trips": 2, "won": 0, "win_rate_pct": 0.0},
    ]


def _line(kind: str, title: str, sell: str | None, cost: str | None, month: dt.date = dt.date(2031, 5, 1),
          trip: uuid.UUID = TRIP_A, party: str = "family") -> service.LineMoney:  # fmt: skip
    return service.LineMoney(
        trip_id=trip, supplier_kind=kind, title=title, month=month, party_type=party,
        sell=None if sell is None else Decimal(sell), cost=None if cost is None else Decimal(cost),
        commission=Decimal("5") if kind == "agency" else None,
    )  # fmt: skip


LINES = [
    _line("aa", "Mekong 3 days", "300", "240"),
    _line("aa", "Mekong 3 days", "300", "250", trip=TRIP_B, party=""),
    _line("agency", "Lotus Lodge", "100", "70", month=dt.date(2031, 4, 1)),
    _line("agency", "Lotus Lodge", "50", None),  # no cost yet: margin ignores it, sell counts
]


def _scope(money: bool = True, compare: str = "previous_year") -> service.Scope:
    return service.Scope(
        today=TODAY, tz="Asia/Ho_Chi_Minh", base_currency="USD", first_day=dt.date(2029, 1, 5),
        window=period.window("90d", TODAY), compare=compare, owner=None, money=money,  # type: ignore[arg-type]
    )  # fmt: skip


def test_money_report_reconciles_with_its_lines() -> None:
    m = service.money_report(LINES, _scope())
    assert m["sell_base"] == "750.00" and m["cost_base"] == "560.00" and m["margin_base"] == "140.00"
    assert m["margin_pct"] == 20.0 and m["lines"] == 4 and m["lines_missing_amounts"] == 1  # 140 / 700
    assert m["commission_expected_base"] == "10.00" and m["aa_share_pct"] == 80.0
    months = {r["month"]: r for r in m["by_month"]}
    assert set(months) == {"2031-02-01", "2031-03-01", "2031-04-01", "2031-05-01"}  # every month of the window
    assert months["2031-03-01"]["lines"] == 0 and months["2031-05-01"]["sell_base"] == "650.00"
    assert sum(Decimal(r["sell_base"]) for r in m["by_month"]) == Decimal(m["sell_base"])
    assert [p["title"] for p in m["top_products"]] == ["Mekong 3 days", "Lotus Lodge"]
    assert m["top_products"][0]["trips"] == 2 and m["top_products"][0]["lines"] == 2
    assert {r["party_type"] for r in m["by_party_type"]} == {None, "family"}
    assert service.money_report([], _scope())["aa_share_pct"] is None


def test_tile_deltas_are_percent_for_amounts_and_points_for_rates() -> None:
    assert service._delta("inquiries", 15, 10) == 50.0
    assert service._delta("revenue", "90.00", "100.00") == -10.0
    assert service._delta("win_rate", 31.0, 27.5) == 3.5
    assert service._delta("inquiries", 3, 0) is None and service._delta("aa_share", None, 4.0) is None


def test_success_status() -> None:
    assert service._status("budget_band_share", 85) == "met"
    assert service._status("budget_band_share", 79.9) == "below"
    assert service._status("first_reply_median", 30) == "met"
    assert service._status("quiet_leads", 2) == "below"
    assert service._status("messages_sent", 4) == "info"
    assert service._status("upgrade_rate", None) == "no_data"
    assert service._status("commission_unclaimed", 0) == "not_built"


# ── the service over a scripted connection ─────────────────────────────────────────────────────────────────
def _cohort_rows(*_: Any) -> list[dict[str, Any]]:
    return [
        {"stage": r.stage, "owner_member_id": r.owner_member_id, "lead_source": r.lead_source,
         "has_budget": r.has_budget, "max_rank": r.max_rank, "n": r.n}
        for r in COHORT
    ]  # fmt: skip


def _money_rows(*_: Any) -> list[dict[str, Any]]:
    return [
        {"trip_id": x.trip_id, "supplier_kind": x.supplier_kind, "title": x.title, "month": x.month,
         "party_type": x.party_type, "sell": x.sell, "cost": x.cost, "commission": x.commission}
        for x in LINES
    ]  # fmt: skip


def report_conn(first_day: dt.date | None = dt.date(2029, 1, 5)) -> ScriptedConn:
    return ScriptedConn(
        [
            ("AS first_day", {"today": TODAY, "tz": "Asia/Ho_Chi_Minh", "base_currency": "USD",
                              "first_day": first_day}),
            ("WITH cohort AS", _cohort_rows),
            ("AS reason, count", [
                {"stage": "lost", "reason": "price", "n": 1}, {"stage": "lost", "reason": "no_response", "n": 3},
                {"stage": "cancelled", "reason": "client_request", "n": 1}]),
            ("AS median_days", {"n": 2, "median_days": 12.25}),
            ("AS stalled_not_waiting", [
                {"stage": "proposal", "n": 4, "stalled": 2, "stalled_not_waiting": 1, "median_age_days": 9.5}]),
            ("AS block_type", [
                {"block_type": "lodging", "kept_default": False, "upgraded": True, "n": 2},
                {"block_type": "activity", "kept_default": False, "upgraded": False, "n": 1},
                {"block_type": "lodging", "kept_default": True, "upgraded": False, "n": 5}]),
            ("max(m.amount_base) FILTER", _money_rows),
            ("SELECT bl.supplier_kind, count(*)::int AS n", [{"supplier_kind": "aa", "n": 1},
                                                             {"supplier_kind": "agency", "n": 3}]),
            ("count(DISTINCT bl.trip_id)::int AS trips", [
                {"supplier_kind": "aa", "title": "Mekong 3 days", "lines": 2, "trips": 2}]),
            ("AS outstanding", [{"currency": "AUD", "n": 2, "total": Decimal("1000"), "paid": Decimal("250"),
                                 "outstanding": Decimal("750"), "overdue_n": 1, "overdue": Decimal("500")}]),
            ("s.currency, s.kind", [{"currency": "AUD", "kind": "balance", "n": 1, "total": Decimal("800"),
                                     "overdue_n": 0, "overdue": Decimal("0")}]),
            ("AS refunded", [{"currency": "AUD", "received": Decimal("900"), "refunded": Decimal("100"), "n": 3}]),
            ("AS messages_sent", [
                {"member_id": MEMBER, "role": "owner", "status": "active", "open_trips": 1, "overdue_tasks": 0,
                 "tasks_done": 4, "messages_sent": 7},
                {"member_id": AGENT, "role": "agent", "status": "active", "open_trips": 5, "overdue_tasks": 2,
                 "tasks_done": 1, "messages_sent": 12}]),
            ("t.owner_member_id IS NULL", 2),
            ("AS minutes", [{"owner_member_id": AGENT, "minutes": 30.0}, {"owner_member_id": AGENT, "minutes": 90.0},
                            {"owner_member_id": None, "minutes": None}]),
            ("last.direction = 'in'", 1),
            ("last_activity_at <", 3),
            ("insights_opt_out", True),
            ("AS median, count(*)::int AS trips", {"median": 4.0, "trips": 2}),
            ("p.kind = 'text'", {"median": 1.5, "n": 1}),
            ("SELECT g.channel", [{"channel": "email", "n": 20}, {"channel": "sms", "n": 2}]),
            ("AS fast", {"accepted": 4, "fast": 3}),
            ("AND t.stage IN ('booked', 'pre_departure', 'travelling') AND ($1::uuid", 0),
            ("l.purpose = 'portal'", {"trips": 2, "opened": 1}),
            ("AS departed", {"departed": 0, "signed": 0}),
            ("AS mixed", {"trips": 2, "mixed": 1}),
            ("'report_export'", 2),
            ("crm.export_job", 1),
        ]
    )  # fmt: skip


@pytest.mark.asyncio
async def test_summary_for_the_owner_has_every_section_and_compares_with_last_year() -> None:
    conn = report_conn()
    ctx = tenant_ctx(conn, role="owner")
    s = await service.summary(ctx, "90d", None)
    assert s["period"]["compare"] == "previous_year" and s["period"]["previous"] == {
        "from": "2030-02-20",
        "to": "2030-05-20",
    }
    windows = [args[:2] for args in conn.sql("WITH cohort AS")]
    assert (dt.date(2030, 2, 20), dt.date(2030, 5, 21)) in windows  # the same window a year earlier
    assert all(args[2] == "Asia/Ho_Chi_Minh" for args in conn.sql("WITH cohort AS"))
    tiles = {t["key"]: t for t in s["tiles"]}
    assert set(tiles) == {"inquiries", "win_rate", "revenue", "margin", "aa_share"}
    assert tiles["revenue"]["value"] == "750.00" and tiles["revenue"]["previous"] == "750.00"
    assert tiles["revenue"]["delta"] == 0.0 and tiles["aa_share"]["basis"] == "sell"
    assert tiles["inquiries"]["href"] == "/pipeline" and tiles["inquiries"]["trend"] == []
    assert s["money_visible"] and s["money"]["margin_base"] == "140.00"
    assert s["top_products"] == s["money"]["top_products"]
    assert s["outcomes"]["lost"] == [{"reason": "no_response", "count": 3}, {"reason": "price", "count": 1}]
    assert s["cycle"] == {"trips": 2, "median_days": 12.2}
    stages = {x["stage"]: x for x in s["open_trips"]}
    assert len(stages) == 6 and stages["proposal"]["stalled"] == 2 and stages["inquiry"]["count"] == 0
    assert s["picks"] == {"picks": 8, "kept_default": 5, "upgraded": 2, "kept_default_pct": 62.5,
                          "upgrade_rate_pct": 25.0, "most_changed_type": "lodging"}  # fmt: skip
    inv = s["payments"]["open_invoices"][0]
    assert inv["outstanding"] == "750.00" and inv["overdue"] == "500.00"
    assert s["payments"]["collected"][0]["kept"] == "800.00"
    team = {m["member_id"]: m for m in s["team"]["members"]}
    assert team[str(AGENT)]["first_reply_median_min"] == 60.0 and team[str(AGENT)]["new_trips"] == 8
    assert team[str(MEMBER)]["is_me"] and team[str(MEMBER)]["first_reply_median_min"] is None
    assert s["team"]["unassigned_open_trips"] == 2 and s["team"]["unanswered_24h"] == 1
    metrics = {m["key"]: m for m in s["success"]}
    assert metrics["budget_band_share"]["value"] == 66.7 and metrics["budget_band_share"]["status"] == "below"
    assert metrics["first_reply_median"]["value"] == 60.0 and metrics["first_reply_median"]["status"] == "met"
    assert metrics["quiet_leads"]["value"] == 3 and metrics["upgrade_rate"]["status"] == "met"
    assert metrics["messages_sent"]["value"] == 20 and metrics["chat_messages_sent"]["value"] == 2
    assert metrics["margin_known_share"]["value"] == 50.0  # TRIP_A has a line without cost
    assert metrics["deposit_48h_share"]["value"] == 75.0 and metrics["signed_before_departure_share"]["value"] is None
    assert metrics["report_exports"]["value"] == 2 and metrics["data_exports"]["value"] == 1
    assert metrics["ai_drafts_light_edit_share"]["status"] == "not_built"
    assert s["insights_included"] is True


@pytest.mark.asyncio
async def test_summary_for_the_assistant_reads_no_money_and_shows_the_trend_for_a_new_agency() -> None:
    conn = report_conn(first_day=dt.date(2031, 3, 1))
    s = await service.summary(tenant_ctx(conn, role="assistant"), "12m", "me")
    assert not conn.sql("max(m.amount_base) FILTER")  # the money query never runs
    assert s["money"] is None and not s["money_visible"]
    assert s["top_products"] == [{"supplier_kind": "aa", "title": "Mekong 3 days", "lines": 2, "trips": 2}]
    tiles = {t["key"]: t for t in s["tiles"]}
    assert set(tiles) == {"inquiries", "win_rate", "aa_share"}
    assert tiles["aa_share"]["value"] == 25.0 and tiles["aa_share"]["basis"] == "lines"
    assert s["period"]["compare"] == "trend_3m" and len(tiles["inquiries"]["trend"]) == 3
    assert tiles["inquiries"]["trend"][0]["from"] == "2031-03-01"
    assert all(args[3] == MEMBER for args in conn.sql("WITH cohort AS"))  # owner=me
    assert s["team"]["unassigned_open_trips"] == 0  # not asked for a single owner's view
    assert "margin_known_share" not in {m["key"] for m in s["success"]}
    removed: set[str] = set()
    margin.strip_margin(s, removed)
    assert removed == set()  # the service builds no margin-named key for the assistant (nothing left to strip)


@pytest.mark.asyncio
async def test_owner_filter_must_be_me_or_a_member_id() -> None:
    ctx = tenant_ctx(report_conn())
    assert (await service.scope(ctx, "30d", str(AGENT))).owner == AGENT
    with pytest.raises(Invalid):
        await service.scope(ctx, "30d", "someone")


@pytest.mark.asyncio
async def test_home_tiles_are_the_last_12_months_of_everyone() -> None:
    conn = report_conn(first_day=None)
    out = await service.home_tiles(tenant_ctx(conn, role="agent"))
    assert out["period"]["from"] == "2030-06-01" and out["period"]["compare"] == "none"
    assert all(t["previous"] is None and t["trend"] == [] for t in out["tiles"])


# ── CSV ────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["=1+1", "+cmd", "-2+3", "@SUM(A1)", "\tx", "\rx"])
def test_formula_guard_makes_dangerous_text_inert(text: str) -> None:
    assert export.guard(text) == "'" + text


def test_numbers_stay_numbers_and_text_stays_text() -> None:
    assert export.guard(Decimal("-12.50")) == "-12.50" and export.guard(-3) == "-3" and export.guard(None) == ""
    assert export.guard(True) == "true" and export.guard("Lotus - Lodge") == "Lotus - Lodge"
    body = export.to_csv(["a", "b"], [["=x", Decimal("1.00")]])
    assert body.startswith("﻿".encode()) and body.endswith(b"\r\n")
    assert list(csv.reader(io.StringIO(body.decode("utf-8-sig")))) == [["a", "b"], ["'=x", "1.00"]]


@pytest.mark.parametrize("section", export.SECTIONS)
@pytest.mark.asyncio
async def test_every_section_exports_for_the_owner(section: str) -> None:
    ctx = tenant_ctx(report_conn(), role="owner")
    body, count = await export.export(ctx, "90d", None, section)
    rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    assert len(rows) == count + 1 and count > 0
    assert [(e.action, e.object_id, e.reason) for e in ctx.audit_events] == [("report_export", section, "period_90d")]


@pytest.mark.asyncio
async def test_csv_rows_reconcile_with_the_page() -> None:
    ctx = tenant_ctx(report_conn(), role="owner")
    body, _ = await export.export(ctx, "90d", None, "money_by_month")
    rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    assert sum(Decimal(r[3]) for r in rows[1:]) == Decimal("750.00")  # = the page's revenue tile
    body, _ = await export.export(ctx, "90d", None, "funnel")
    assert ["booked", "2"] in list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))


@pytest.mark.asyncio
async def test_the_assistant_cannot_export_money_and_gets_products_without_it() -> None:
    ctx = tenant_ctx(report_conn(), role="assistant")
    with pytest.raises(Forbidden):
        await export.export(ctx, "90d", None, "money_by_month")
    assert [(e.action, e.outcome) for e in ctx.audit_events] == [("view_margin", "denied")]
    body, _ = await export.export(tenant_ctx(report_conn(), role="assistant"), "90d", None, "top_products")
    header = next(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    assert header == ["supplier", "product", "lines", "trips"]


# ── insights (module 17) ───────────────────────────────────────────────────────────────────────────────────
AGENCIES = [uuid.UUID(f"00000000-0000-4000-8000-0000000001{i:02d}") for i in range(6)]


def _fact(tenant: uuid.UUID, destination: str | None, size: int, **over: Any) -> insights.Fact:
    """A per-destination fact (crm.v_insight_fact) unless ``grain`` says otherwise."""
    base: dict[str, Any] = {
        "tenant_id": tenant, "month": dt.date(2031, 3, 1), "destination": destination, "budget_band": "2000_3500",
        "age_band": "35_44", "party_size": size, "stage_reached": "inquiry", "lost_reason_code": None,
        "grain": insights.TRIP_DESTINATION,
    }  # fmt: skip
    base.update(over)
    return insights.Fact(**base)


def _trip(tenant: uuid.UUID, size: int, trips: int = 1, **over: Any) -> insights.Fact:
    """A trip-level fact (crm.v_insight_trip_fact): ``trips`` trips of one band combination, no destination."""
    return _fact(tenant, None, size, trip_count=trips, grain=insights.TRIP, **over)


def test_a_cell_needs_ten_travellers_from_three_agencies() -> None:
    facts = [_fact(AGENCIES[0], "laos", 9), _fact(AGENCIES[1], "laos", 1)]  # 10 travellers, 2 agencies
    facts += [_fact(a, "cambodia", 3) for a in AGENCIES[:3]]  # 9 travellers, 3 agencies
    facts += [_fact(a, "vietnam", 4) for a in AGENCIES[:3]]  # 12 travellers, 3 agencies
    facts += [_fact(AGENCIES[0], None, 50)]  # no destination: not a cell
    out = insights.aggregate(facts, "destination")
    assert out["cells"] == [{"bucket": "vietnam", "traveller_count": 12, "agency_count": 3}]
    assert out["suppressed_cells"] == 2 and out["k_min"] == 10 and out["agencies_min"] == 3
    # The opted-out third agency takes Vietnam under the line.
    assert insights.aggregate(facts, "destination", opted_out=frozenset({AGENCIES[2]}))["cells"] == []


def test_one_agency_above_seventy_percent_suppresses_the_cell() -> None:
    dominated = [_fact(AGENCIES[0], "laos", 8), _fact(AGENCIES[1], "laos", 1), _fact(AGENCIES[2], "laos", 1)]
    shared = [_fact(AGENCIES[0], "bali", 7), _fact(AGENCIES[1], "bali", 2), _fact(AGENCIES[2], "bali", 1)]
    out = insights.aggregate(dominated + shared, "destination")
    assert out["cells"] == [{"bucket": "bali", "traveller_count": 10, "agency_count": 3}]  # 70 % exactly: published
    assert out["suppressed_cells"] == 1  # Laos: 80 % from one agency


def test_party_size_is_banded_and_unknown_dimensions_refused() -> None:
    assert [insights.party_band(n) for n in (0, 1, 2, 3, 4, 5, 9)] == ["1", "1", "2", "3_4", "3_4", "5_plus", "5_plus"]
    out = insights.aggregate([_trip(a, 4) for a in AGENCIES[:3]], "party_size")
    assert out["cells"] == [{"bucket": "3_4", "traveller_count": 12, "agency_count": 3}]
    assert insights.aggregate([_trip(a, 4) for a in AGENCIES[:3]], "lost_reason_code")["cells"] == []
    with pytest.raises(ValueError):
        insights.aggregate([], "tenant_id")


def test_a_trip_counts_once_outside_the_destination_dimension() -> None:
    """Review S261: 3 agencies x one 1-traveller trip to 4 destinations is 3 travellers, not 12. Per-destination facts
    are refused for a non-destination dimension; trip facts weigh by trip_count."""
    per_destination = [_fact(a, d, 1, budget_band="over_5000") for a in AGENCIES[:3] for d in ("w", "x", "y", "z")]
    for dimension in ("budget_band", "age_band", "party_size", "stage_reached", "lost_reason_code"):
        with pytest.raises(ValueError, match="trip facts"):
            insights.aggregate(per_destination, dimension)
    with pytest.raises(ValueError):
        insights.aggregate([_trip(AGENCIES[0], 1)], "destination")
    one_each = [_trip(a, 1, budget_band="over_5000") for a in AGENCIES[:3]]
    out = insights.aggregate(one_each, "budget_band")
    assert out["cells"] == [] and out["suppressed_cells"] == 1  # 3 travellers: under k
    out = insights.aggregate([_trip(a, 2, trips=2, budget_band="over_5000") for a in AGENCIES[:3]], "budget_band")
    assert out["cells"] == [{"bucket": "over_5000", "traveller_count": 12, "agency_count": 3}]


@pytest.mark.parametrize(
    ("quarter", "months"),
    [
        ("2026-Q1", (dt.date(2026, 1, 1), dt.date(2026, 3, 1))),
        ("2031-Q4", (dt.date(2031, 10, 1), dt.date(2031, 12, 1))),
    ],
)
def test_a_quarter_is_three_whole_months(quarter: str, months: tuple[dt.date, dt.date]) -> None:
    assert insights.quarter_months(quarter) == months


@pytest.mark.parametrize("bad", ["2026-Q0", "2026-Q5", "2026-03", "2026", "26-Q1", "2026-q1", " 2026-Q1", ""])
def test_anything_but_one_quarter_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        insights.quarter_months(bad)


@pytest.mark.parametrize("seed", range(40))
def test_property_no_published_cell_breaks_the_rule_and_opt_outs_never_count(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 — a reproducible test sample, not a secret
    buckets = ["laos", "cambodia", "vietnam", "thailand", "myanmar"]
    n_rows = rng.randint(0, 60)
    by_destination = [_fact(rng.choice(AGENCIES), rng.choice(buckets), rng.randint(0, 6)) for _ in range(n_rows)]
    by_trip = [
        _trip(rng.choice(AGENCIES), rng.randint(0, 6), rng.randint(1, 3), budget_band=rng.choice(["a", "b", None]))
        for _ in range(n_rows)
    ]
    opted = frozenset(rng.sample(AGENCIES, rng.randint(0, 3)))
    for dimension, facts in (("destination", by_destination), ("budget_band", by_trip), ("party_size", by_trip)):
        out = insights.aggregate(facts, dimension, opted_out=opted)
        kept = [f for f in facts if f.tenant_id not in opted]
        expected: dict[str, dict[uuid.UUID, int]] = {}
        for f in kept:
            b = insights.bucket_of(f, dimension)
            if b is None:
                continue
            cell = expected.setdefault(b, {})
            cell[f.tenant_id] = cell.get(f.tenant_id, 0) + f.party_size * f.trip_count

        def ok(cell: dict[uuid.UUID, int]) -> bool:
            n = sum(cell.values())
            return n >= 10 and len(cell) >= 3 and max(cell.values()) <= 0.7 * n

        for c in out["cells"]:
            cell = expected[c["bucket"]]
            assert c["traveller_count"] == sum(cell.values()) >= 10 and c["agency_count"] == len(cell) >= 3
            assert max(cell.values()) <= 0.7 * c["traveller_count"]
        published = {c["bucket"] for c in out["cells"]}
        assert published == {b for b, cell in expected.items() if ok(cell)}
        assert out["suppressed_cells"] == len(expected) - len(published)
        assert all(set(c) == {"bucket", "traveller_count", "agency_count"} for c in out["cells"])


class AgencyConn(ScriptedConn):
    """A scripted pool that remembers the agency of the open platform context (``app.tenant_id``)."""

    def __init__(self, statuses: dict[uuid.UUID, str | None], synthetic: set, opted: set, facts: dict) -> None:
        super().__init__()
        self.current: uuid.UUID | None = None
        self.roles: list[str] = []
        self.on("crm.audit_export_agencies()", [{"tenant_id": t, "exported_until": None, "verified_until": None,
                                                 "last_seq": 0, "registered_at": None} for t in statuses])  # fmt: skip
        self.on("SELECT status FROM crm.tenant", lambda t: statuses[t])
        self.on("SELECT is_synthetic FROM crm.tenant", lambda t: t in synthetic)
        self.on("insights_opt_out", lambda: self.current in opted)
        self.on("FROM crm.v_insight_fact", lambda *a: facts.get(self.current, []))
        self.on(
            "FROM crm.v_insight_trip_fact",
            lambda *a: [{**r, "destination": None} for r in facts.get(self.current, [])],
        )

    async def execute(self, sql: str, *args: Any) -> str:
        if "set_config('app.tenant_id'" in sql:
            self.current = uuid.UUID(args[0])
        if "SET LOCAL ROLE" in sql:
            self.roles.append(sql.split()[-1])
        return await super().execute(sql, *args)


@pytest.mark.asyncio
async def test_platform_insights_reads_facts_as_the_insights_role_and_skips_who_must_not_count() -> None:
    from app.ops import console

    a, b, c, opted, real, locked, gone = AGENCIES[:4] + [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    row = {"month": dt.date(2031, 3, 1), "destination": "laos", "budget_band": "over_5000", "age_band": None,
           "party_size": 4, "stage_reached": "inquiry", "lost_reason_code": None, "trip_count": 1}  # fmt: skip
    conn = AgencyConn(
        statuses={a: "active", b: "active", c: "active", opted: "active", real: "active", locked: "locked",
                  gone: None},
        synthetic={a, b, c, opted, locked},
        opted={opted},
        facts={a: [row], b: [row], c: [row], opted: [row] * 5, real: [row] * 5},
    )  # fmt: skip
    out = await console.platform_insights(
        conn, staff_sub="s", dimension="destination", month_from=dt.date(2031, 1, 1), month_to=dt.date(2031, 6, 1),
        include_synthetic=True,
    )  # fmt: skip
    assert out["cells"] == [{"bucket": "laos", "traveller_count": 32, "agency_count": 4}]  # a, b, c and the real one
    assert out["agencies_included"] == 4 and out["agencies_opted_out"] == 1
    assert conn.roles == ["crm_insights", "NONE"] * 4
    assert not conn.sql("INSERT INTO crm.audit_event")  # totals only: no per-agency event
    out = await console.platform_insights(
        conn, staff_sub="s", dimension="destination", month_from=dt.date(2031, 1, 1), month_to=dt.date(2031, 6, 1),
        include_synthetic=False,
    )  # fmt: skip
    assert out["agencies_included"] == 1 and out["cells"] == [] and out["suppressed_cells"] == 1
    # A non-destination dimension reads the trip-level view (each trip once), never the per-destination one.
    before = len(conn.sql("FROM crm.v_insight_fact"))
    out = await console.platform_insights(
        conn, staff_sub="s", dimension="budget_band", month_from=dt.date(2031, 1, 1), month_to=dt.date(2031, 3, 1),
        include_synthetic=True,
    )  # fmt: skip
    assert out["cells"] == [{"bucket": "over_5000", "traveller_count": 32, "agency_count": 4}]
    assert len(conn.sql("FROM crm.v_insight_fact")) == before and len(conn.sql("FROM crm.v_insight_trip_fact")) == 4


# ── routes ─────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> tuple[TestClient, dict[str, str]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    ctx = tenant_ctx(report_conn(), role=role)

    def tx(*a: Any, **kw: Any) -> Any:
        margin.current_member_role.set(role)
        return fake_tx(ctx)(*a, **kw)

    monkeypatch.setattr(reports_router, "request_pool", _pool)
    monkeypatch.setattr(reports_router, "tenant_tx", tx)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers


def test_every_reports_route_answers(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    client, h = _client(pools, monkeypatch, "agent")
    with client:
        summary = client.get("/crm/v1/reports/summary?period=90d&owner=me", headers=h)
        assert summary.status_code == 200 and summary.json()["money"]["margin_base"] == "140.00"
        assert client.get("/crm/v1/reports/tiles", headers=h).json()["period"]["compare"] == "previous_year"
        res = client.get("/crm/v1/reports/export?section=lead_sources&period=ytd", headers=h)
        assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
        assert 'filename="report-lead-sources-ytd-' in res.headers["content-disposition"]
        assert res.headers["x-row-count"] == "3" and res.headers["cache-control"] == "no-store"
        for bad in ("summary?period=7d", "summary?owner=everyone", "export?section=trips", "export"):
            assert client.get(f"/crm/v1/reports/{bad}", headers=h).status_code == 422, bad


def test_the_assistant_route_answers_carry_no_margin(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    client, h = _client(pools, monkeypatch, "assistant")
    with client:
        body = client.get("/crm/v1/reports/summary", headers=h).json()
        assert body["money"] is None and "margin" not in {t["key"] for t in body["tiles"]}
        assert not any(margin.is_margin_key(k) for k in _keys(body))
        assert client.get("/crm/v1/reports/export?section=money_by_month", headers=h).status_code == 403


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_queries_bind_every_parameter_they_use() -> None:
    """asyncpg refuses a call whose argument count differs from the statement's parameters: pin the counts."""
    import re

    def params(sql: str) -> int:
        return max((int(n) for n in re.findall(r"\$(\d+)", sql)), default=0)

    assert params(q.COHORT_SQL) == 5 and params(q.MONEY_SQL) == 5 and params(q.TEAM_SQL) == 5
    assert params(q.TOP_PRODUCTS_SQL) == 5
    for sql in (q.OUTCOME_SQL, q.CYCLE_SQL, q.PICKS_SQL, q.LINE_COUNT_SQL, q.FIRST_REPLY_SQL,
                q.MESSAGES_PER_BOOKED_SQL, q.TEXT_PROPOSAL_SQL, q.SENT_BY_CHANNEL_SQL, q.DEPOSIT_48H_SQL,
                q.PORTAL_SQL, q.SIGNED_SQL, q.MIXED_SQL):  # fmt: skip
        assert params(sql) == 4, sql[:60]
    assert params(q.REPORT_EXPORTS_SQL) == params(q.DATA_EXPORTS_SQL) == params(q.COLLECTED_SQL) == 3
    assert params(q.OPEN_SQL) == params(q.OPEN_INVOICES_SQL) == params(q.SCHEDULE_DUE_SQL) == params(q.QUIET_SQL) == 2
    assert params(q.UNASSIGNED_SQL) == params(q.UNANSWERED_SQL) == params(q.OVERDUE_BEFORE_DEPARTURE_SQL) == 1
