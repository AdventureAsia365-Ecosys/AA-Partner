"""AA-1066 (crm-048): retail pricing of the proposal builder against scripted connections — the per-traveller grid
and rows (:mod:`app.proposals.pricing`), the agency's price rules (matching, stacking, rounding, the settings
service), the discount and its owner approval (:mod:`app.proposals.discount`), the builder seams (totals with a
discount, the price hash, the rows on a write, the publish gate) and the routes (owner-only settings, money fields,
the owner's decision). Real SQL, RLS and the end-to-end flows: tests/integration/test_crm048_schema.py and
test_aa1066_retail_pricing_api.py.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal as D
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import margin
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.main import create_app
from app.proposals import builder, client_page, discount, pricing
from app.proposals.pricing import PriceRule, Settings
from app.routers import builder as builder_router
from app.routers import ledger as ledger_router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, TENANT, FakeVault, fake_tx, tenant_ctx
from tests.unit.test_builder_service import O1, O2, TRIP, P, _conn, _options, _proposal, _run

TODAY = dt.date(2026, 11, 10)
TRIP_DAY = dt.date(2027, 1, 20)


def _rule(kind: str, pct: str, *, priority: int = 100, stacking: str = "add", **kw: Any) -> PriceRule:
    base: dict[str, Any] = {
        "rule_id": str(uuid.uuid4()), "name": f"{kind} {pct}", "kind": kind, "product_scope": "all",
        "aa_tour_source_id": None, "date_from": None, "date_to": None, "min_pax": None, "days_before": None,
        "adjust_pct": D(pct), "priority": priority, "stacking": stacking, "active": True,
    }  # fmt: skip
    base.update(kw)
    return PriceRule(**base)


FACTS = {"kind": "trip_plan", "tour_source_id": "T-1", "service_date": TRIP_DAY, "pax": 4, "today": TODAY}


# ── Party counts, the grid, the rows ─────────────────────────────────────────────────────────────


def test_party_counts_follow_the_agency_bands() -> None:
    rooms = [{"type": "double", "adults": 2, "children": 0}, {"type": "single", "adults": 1, "children": 0},
             {"type": "family", "adults": 0, "children": 3}]  # fmt: skip
    counts = pricing.party_counts(rooms, 2, [1, 6, 14], Settings())
    assert counts == {"adult": 4, "child": 1, "infant": 1, "single_supp": 1}  # the 14-year-old is priced as an adult
    narrow = pricing.party_counts(None, 2, [1, 2, 6], Settings(child_age_max=5, infant_age_max=2))
    assert narrow == {"adult": 3, "child": 0, "infant": 2, "single_supp": 0}
    assert pricing.party_counts(None, 0, [], Settings())["adult"] == 1  # never nobody


def test_the_grid_shares_a_total_per_head() -> None:
    rows = pricing.grid(D("1000"), {"adult": 2, "child": 1, "infant": 1, "single_supp": 1})
    assert [(r["pax_type"], r["unit_sell"], r["qty"]) for r in rows] == [
        ("adult", D("333.34"), 2), ("child", D("333.34"), 1), ("infant", D("0.00"), 1), ("single_supp", D("0.00"), 1),
    ]  # fmt: skip
    assert [r["unit_sell"] for r in pricing.grid(D("0"), {"adult": 2})] == [D("0.00")]


def test_rows_are_checked_and_summed() -> None:
    rows = pricing.check_rows(
        [{"pax_type": "child", "unit_sell": "50", "unit_net": "20", "qty": 1},
         {"pax_type": "adult", "unit_sell": "100.50", "unit_net": "60", "qty": 2}],
        aa=False,
    )  # fmt: skip
    assert [r["pax_type"] for r in rows] == ["adult", "child"]  # type order
    assert pricing.sums(rows) == (D("251.00"), D("140.00"))
    assert pricing.sums([{"unit_sell": D("5"), "unit_net": None, "qty": 2}]) == (D("10.00"), None)
    for bad, code in (
        ([], "invalid_prices"),
        ("adult", "invalid_prices"),
        ([{"pax_type": "senior", "unit_sell": "1", "qty": 1}], "invalid_prices"),
        ([{"pax_type": "adult", "unit_sell": "1.234", "qty": 1}], "invalid_unit_sell"),
        ([{"pax_type": "adult", "unit_sell": "x", "qty": 1}], "invalid_unit_sell"),
        ([{"pax_type": "adult", "unit_sell": None, "qty": 1}], "invalid_unit_sell"),
        ([{"pax_type": "adult", "unit_sell": "1", "qty": 100}], "invalid_qty"),
        ([{"pax_type": "adult", "unit_sell": "1", "qty": True}], "invalid_qty"),
        ([{"pax_type": "adult", "unit_sell": "0", "qty": 3}], "prices_nothing_charged"),
    ):
        with pytest.raises(Invalid) as err:
            pricing.check_rows(bad, aa=False)
        assert err.value.code == code, bad
    with pytest.raises(Forbidden) as aa:
        pricing.check_rows([{"pax_type": "adult", "unit_sell": "1", "unit_net": "1", "qty": 1}], aa=True)
    assert aa.value.code == "aa_net_from_quote"


def test_rows_out_hides_the_unit_net_without_margin() -> None:
    rows = [{"pax_type": "adult", "unit_sell": D("10"), "unit_net": D("4"), "qty": 2},
            {"pax_type": "child", "unit_sell": D("5"), "unit_net": None, "qty": 1}]  # fmt: skip
    assert pricing.rows_out(rows, sees_margin=True)[0] == {"pax_type": "adult", "unit_sell": "10.00", "qty": 2,
                                                           "unit_net": "4.00"}  # fmt: skip
    assert pricing.rows_out(rows, sees_margin=True)[1]["unit_net"] is None
    assert all("unit_net" not in r for r in pricing.rows_out(rows, sees_margin=False))


# ── Rules ────────────────────────────────────────────────────────────────────────────────────────


def test_each_rule_kind_matches_its_condition() -> None:
    season = _rule("season", "20", date_from=dt.date(2027, 1, 1), date_to=dt.date(2027, 1, 31))
    assert pricing.matches(season, **FACTS)
    assert not pricing.matches(season, **{**FACTS, "service_date": dt.date(2027, 2, 1)})
    assert not pricing.matches(season, **{**FACTS, "service_date": None})  # no day 1 date: not matched
    group = _rule("group_size", "-5", min_pax=4)
    assert pricing.matches(group, **FACTS) and not pricing.matches(group, **{**FACTS, "pax": 3})
    assert pricing.matches(group, **{**FACTS, "service_date": None})  # a group needs no date
    early = _rule("early_booking", "-10", days_before=60)  # 71 days ahead
    assert pricing.matches(early, **FACTS) and not pricing.matches(early, **{**FACTS, "today": dt.date(2026, 12, 1)})
    late = _rule("last_minute", "-30", days_before=14)
    assert not pricing.matches(late, **FACTS)
    assert pricing.matches(late, **{**FACTS, "today": dt.date(2027, 1, 10)})
    assert not pricing.matches(late, **{**FACTS, "today": dt.date(2027, 1, 25)})  # the date has passed
    # Scope, tour and the active switch.
    assert not pricing.matches(_rule("group_size", "5", min_pax=2, product_scope="hotel_room"), **FACTS)
    assert pricing.matches(_rule("group_size", "5", min_pax=2, product_scope="trip_plan"), **FACTS)
    assert not pricing.matches(_rule("group_size", "5", min_pax=2, aa_tour_source_id="T-2"), **FACTS)
    assert not pricing.matches(_rule("group_size", "5", min_pax=2, active=False), **FACTS)
    assert not pricing.matches(_rule("group_size", "5", min_pax=2), **{**FACTS, "kind": None})


def test_add_rules_sum_and_only_the_first_best_only_rule_applies() -> None:
    rules = [
        _rule("season", "20", priority=50, date_from=dt.date(2027, 1, 1), date_to=dt.date(2027, 1, 31)),
        _rule("group_size", "-5", min_pax=2),
        _rule("early_booking", "-15", priority=20, stacking="best_only", days_before=30),
        _rule("early_booking", "-10", priority=10, stacking="best_only", days_before=60),
        _rule("group_size", "40", min_pax=99),  # not matched
    ]
    pct, used = pricing.adjustment(rules, **FACTS)
    assert pct == D("5") and [u["adjust_pct"] for u in used] == ["-10", "20", "-5"]
    # The total never goes below -90 %.
    floor, _ = pricing.adjustment([_rule("group_size", "-90", min_pax=2), _rule("group_size", "-50", min_pax=2)],
                                  **FACTS)  # fmt: skip
    assert floor == D("-90")
    assert pricing.adjusted(D("100"), D("5")) == D("105.00")
    assert pricing.adjusted(D("33.33"), D("-10")) == D("30.00")  # 29.997 rounded up
    assert pricing.adjusted(D("100"), D("0")) == D("100")


def test_check_rule_reads_only_the_kind_s_condition() -> None:
    ok = pricing.check_rule({"name": "  Peak   weeks ", "kind": "season", "date_from": "2027-01-01",
                             "date_to": "2027-01-31", "min_pax": 4, "adjust_pct": "20"})  # fmt: skip
    assert ok["name"] == "Peak weeks" and ok["min_pax"] is None and ok["date_to"] == dt.date(2027, 1, 31)
    assert pricing.check_rule({"name": "G", "kind": "group_size", "min_pax": 6, "adjust_pct": "-5"})["min_pax"] == 6
    assert pricing.check_rule({"name": "L", "kind": "last_minute", "days_before": 0, "adjust_pct": "-5"})[
        "days_before"] == 0  # fmt: skip
    for body, code in (
        ({"name": "", "kind": "season"}, "invalid_rule_name"),
        ({"name": "X", "kind": "birthday"}, "invalid_rule_kind"),
        ({"name": "X", "kind": "group_size", "product_scope": "spa"}, "invalid_product_scope"),
        ({"name": "X", "kind": "group_size", "aa_tour_source_id": "a\nb"}, "invalid_tour_source_id"),
        ({"name": "X", "kind": "group_size", "min_pax": 3, "adjust_pct": "0"}, "invalid_adjust_pct"),
        ({"name": "X", "kind": "group_size", "min_pax": 3, "adjust_pct": "301"}, "invalid_adjust_pct"),
        ({"name": "X", "kind": "group_size", "min_pax": 3, "adjust_pct": "ten"}, "invalid_adjust_pct"),
        ({"name": "X", "kind": "group_size", "min_pax": 3, "adjust_pct": "5", "stacking": "max"}, "invalid_stacking"),
        ({"name": "X", "kind": "group_size", "min_pax": 1, "adjust_pct": "5"}, "invalid_min_pax"),
        ({"name": "X", "kind": "early_booking", "adjust_pct": "5"}, "invalid_days_before"),
        ({"name": "X", "kind": "season", "date_from": "2027-02-01", "date_to": "2027-01-01", "adjust_pct": "5"},
         "invalid_season"),
        ({"name": "X", "kind": "season", "date_from": "2027-01-01", "date_to": "2028-06-01", "adjust_pct": "5"},
         "invalid_season"),
        ({"name": "X", "kind": "season", "date_from": "soon", "date_to": "2028-06-01", "adjust_pct": "5"},
         "invalid_date_from"),
        ({"name": "X", "kind": "group_size", "min_pax": 3, "adjust_pct": "5", "priority": 0}, "invalid_priority"),
    ):  # fmt: skip
        with pytest.raises(Invalid) as err:
            pricing.check_rule(body)
        assert err.value.code == code, body


def test_applied_rules_parse_and_today_falls_back_to_utc() -> None:
    assert pricing.parse_applied(None) is None
    assert pricing.parse_applied('[{"rule_id": "r", "name": "Peak", "adjust_pct": "20"}, 3]') == [
        {"rule_id": "r", "name": "Peak", "adjust_pct": "20"}]  # fmt: skip
    assert pricing.parse_applied({"name": "x"}) is None
    assert isinstance(pricing.today_in("Not/AZone"), dt.date) and isinstance(pricing.today_in(None), dt.date)


# ── The settings service ─────────────────────────────────────────────────────────────────────────


RULE_ROW = {
    "retail_price_rule_id": uuid.uuid4(), "name": "Group", "kind": "group_size", "product_scope": "all",
    "aa_tour_source_id": None, "date_from": None, "date_to": None, "min_pax": 4, "days_before": None,
    "adjust_pct": D("-5.00"), "priority": 100, "stacking": "add", "active": True,
}  # fmt: skip


def _settings_conn(**extra: Any) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("SELECT child_age_max", {"child_age_max": 11, "infant_age_max": 1, "discount_owner_limit_pct": D("10")})
    conn.on("FROM crm.retail_price_rule WHERE deleted_at IS NULL ORDER BY", [RULE_ROW])
    for fragment, answer in extra.items():
        conn.on(fragment.replace("_", " "), answer)
    return conn


@pytest.mark.asyncio
async def test_settings_read_and_defaults() -> None:
    out = await pricing.read(tenant_ctx(_settings_conn()))
    assert out["discount_owner_limit_pct"] == "10" and out["rules"][0]["adjust_pct"] == "-5"
    assert out["kinds"] == list(pricing.RULE_KINDS) and out["max_rules"] == pricing.MAX_RULES
    assert await pricing.settings(tenant_ctx(ScriptedConn())) == Settings()  # no row yet
    assert await pricing.settings(tenant_ctx(ScriptedConn().on("SELECT child_age_max", "Asia/Bangkok"))) == Settings()


@pytest.mark.asyncio
async def test_update_settings_checks_the_bands() -> None:
    conn = _settings_conn()
    ctx = tenant_ctx(conn)
    await pricing.update_settings(ctx, {"child_age_max": 9, "discount_owner_limit_pct": D("12.5")})
    assert conn.sql("UPDATE crm.tenant_settings SET child_age_max")[0] == (9, 1, D("12.5"))
    assert ctx.audit_events[-1].fields == ("child_age_max", "discount_owner_limit_pct")
    for change, code in (({"infant_age_max": 5, "child_age_max": 4}, "invalid_age_bands"),
                         ({"child_age_max": 30}, "invalid_child_age_max"),
                         ({"discount_owner_limit_pct": D("100.5")}, "invalid_discount_limit")):  # fmt: skip
        with pytest.raises(Invalid) as err:
            await pricing.update_settings(tenant_ctx(_settings_conn()), change)
        assert err.value.code == code


@pytest.mark.asyncio
async def test_rule_create_update_delete() -> None:
    conn = _settings_conn()
    conn.on("SELECT count(*) FROM crm.retail_price_rule", 0)
    conn.on("INSERT INTO crm.retail_price_rule", uuid.uuid4())
    ctx = tenant_ctx(conn)
    await pricing.create_rule(ctx, {"name": "Peak", "kind": "group_size", "min_pax": 3, "adjust_pct": "10"})
    assert conn.sql("INSERT INTO crm.tenant_settings DEFAULT VALUES")  # the rule hangs on the settings row
    full = _settings_conn().on("SELECT count(*) FROM crm.retail_price_rule", pricing.MAX_RULES)
    with pytest.raises(Conflict) as err:
        await pricing.create_rule(tenant_ctx(full), {"name": "P", "kind": "group_size", "min_pax": 3,
                                                     "adjust_pct": "10"})  # fmt: skip
    assert err.value.code == "too_many_price_rules"
    refused = _settings_conn().on("INSERT INTO crm.retail_price_rule", asyncpg.CheckViolationError("check"))
    with pytest.raises(Invalid):
        await pricing.create_rule(tenant_ctx(refused), {"name": "P", "kind": "group_size", "min_pax": 3,
                                                        "adjust_pct": "10"})  # fmt: skip
    # Update merges onto the stored rule; a kind change keeps only the new kind's condition.
    conn = _settings_conn().on("WHERE retail_price_rule_id = $1 AND deleted_at IS NULL", RULE_ROW)
    await pricing.update_rule(tenant_ctx(conn), RULE_ROW["retail_price_rule_id"],
                              {"kind": "early_booking", "days_before": 45})  # fmt: skip
    args = conn.sql("UPDATE crm.retail_price_rule SET")[0]
    assert args[2] == "early_booking" and args[7] is None and args[8] == 45 and len(args) == 13
    with pytest.raises(NotFound):
        await pricing.update_rule(tenant_ctx(_settings_conn()), uuid.uuid4(), {"active": False})
    bad = conn.on("UPDATE crm.retail_price_rule SET", asyncpg.CheckViolationError("check"))
    with pytest.raises(Invalid):
        await pricing.update_rule(tenant_ctx(bad), RULE_ROW["retail_price_rule_id"], {"active": False})

    class Deleted(ScriptedConn):
        async def execute(self, sql: str, *args: Any) -> str:
            self._answer(sql, args)
            return "DELETE 0" if "DELETE" in sql else "OK"

    with pytest.raises(NotFound):
        await pricing.delete_rule(tenant_ctx(Deleted()), uuid.uuid4())
    await pricing.delete_rule(tenant_ctx(_settings_conn()), uuid.uuid4())


# ── The discount ─────────────────────────────────────────────────────────────────────────────────


def _approval(status: str, pct: str, *, below: bool = False, proposal: uuid.UUID = P) -> dict[str, Any]:
    return {"discount_approval_id": uuid.uuid4(), "proposal_id": proposal, "trip_id": TRIP, "discount_pct": D(pct),
            "below_cost": below, "limit_pct": D("10"), "status": status, "decided_by": None, "decided_at": None,
            "created_by": "member:a", "created_at": dt.datetime(2026, 11, 10, tzinfo=dt.UTC)}  # fmt: skip


def test_need_and_cover() -> None:
    assert discount.need(D("15"), D("100"), D("10")) == (True, False)
    assert discount.need(None, D("-1"), D("10")) == (False, True)
    assert discount.need(D("10"), None, D("10")) == (False, False)  # at the limit: fine; margin unknown: fine
    approved = [_approval("approved", "15")]
    assert discount.covered(approved, D("12"), False) and not discount.covered(approved, D("16"), False)
    assert not discount.covered(approved, D("12"), True)  # a sale below cost needs its own approval
    assert discount.covered([_approval("approved", "15", below=True)], D("12"), True)
    assert not discount.covered([_approval("pending", "20")], D("12"), False)


@pytest.mark.asyncio
async def test_state_hides_below_cost_without_margin() -> None:
    conn = ScriptedConn().on("FROM crm.discount_approval WHERE trip_id", [_approval("pending", "15")])
    st = await discount.state(tenant_ctx(conn), trip_id=TRIP, proposal_id=P, discount_pct=D("15"),
                              margin_total=D("10"), bands=Settings(), sees_margin=False)  # fmt: skip
    assert st["needs_approval"] and st["pending"]["discount_pct"] == "15.00"
    assert "below_cost" not in st and "below_cost" not in st["pending"]
    none = await discount.state(tenant_ctx(ScriptedConn()), trip_id=TRIP, proposal_id=P, discount_pct=None,
                                margin_total=None, bands=Settings())  # fmt: skip
    assert none["discount_pct"] is None and not none["needs_approval"] and none["history"] == []


@pytest.mark.asyncio
async def test_request_decide_and_owner_record() -> None:
    new_id = uuid.uuid4()
    conn = ScriptedConn().on("INSERT INTO crm.discount_approval", new_id)
    ctx = tenant_ctx(conn, role="agent")
    got = await discount.request(ctx, trip_id=TRIP, proposal_id=P, discount_pct=D("15"), margin_total=D("1"),
                                 bands=Settings())  # fmt: skip
    assert got == new_id and conn.sql("SET status = 'superseded'") and conn.sql("INSERT INTO crm.task")
    assert ctx.audit_events[-1].reason == "above_limit"
    # The same request again: the pending one, nothing written.
    same = _approval("pending", "15")
    again = ScriptedConn().on("FROM crm.discount_approval WHERE trip_id", [same])
    assert await discount.request(tenant_ctx(again), trip_id=TRIP, proposal_id=P, discount_pct=D("15"),
                                  margin_total=D("1"), bands=Settings()) == same["discount_approval_id"]  # fmt: skip
    assert not again.sql("INSERT INTO crm.discount_approval")
    with pytest.raises(Conflict) as err:
        await discount.request(tenant_ctx(ScriptedConn()), trip_id=TRIP, proposal_id=P, discount_pct=D("5"),
                               margin_total=D("1"), bands=Settings())  # fmt: skip
    assert err.value.code == "discount_approval_not_needed"
    # Decide.
    with pytest.raises(NotFound):
        await discount.decide(tenant_ctx(ScriptedConn()), uuid.uuid4(), "approved", proposal_id=P)
    done = ScriptedConn().on("FOR UPDATE", _approval("approved", "15"))
    with pytest.raises(Conflict) as err:
        await discount.decide(tenant_ctx(done), uuid.uuid4(), "approved", proposal_id=P)
    assert err.value.code == "discount_approval_not_pending"
    open_one = ScriptedConn().on("FOR UPDATE", _approval("pending", "15"))
    out = await discount.decide(tenant_ctx(open_one), uuid.uuid4(), "rejected", proposal_id=P)
    assert out["status"] == "rejected" and open_one.sql("UPDATE crm.task SET status = 'done'")
    assert open_one.sql("UPDATE crm.discount_approval SET status = $2")[0][2] == f"member:{MEMBER}"
    # The owner's own discount.
    own = ScriptedConn().on("VALUES ($1, $2, $3, $4, $5, 'approved'", uuid.uuid4())
    await discount.owner_record(tenant_ctx(own), trip_id=TRIP, proposal_id=P, discount_pct=D("30"), below_cost=False,
                                bands=Settings())  # fmt: skip
    assert own.sql("'approved', now(), $6")[0][5] == f"member:{MEMBER}"


# ── Builder seams ────────────────────────────────────────────────────────────────────────────────


def _priced_conn(*, pct: Any = None, rows: list[dict[str, Any]] | None = None, **kw: Any) -> ScriptedConn:
    conn = _conn(**kw)
    conn.on("SELECT p.discount_pct FROM crm.proposal p", pct)
    conn.on("FROM crm.option_price WHERE proposal_id", rows or [])
    return conn


def test_totals_with_a_discount() -> None:
    s = {"blocks": [{"option_block_id": "b", "default_option_id": "o"}],
         "options": [{"option_id": "o", "option_block_id": "b", "sell_amount": D("1000"), "net_amount": D("900"),
                      "pay_on_arrival": False}], "discount_pct": D("15")}  # fmt: skip
    sums = builder._charged(s, None, D("30"))
    assert (sums["subtotal"], sums["discount"], sums["sell_total"], sums["deposit"]) == (
        D("1000.00"), D("150.00"), D("850.00"), D("255.00"))  # fmt: skip
    assert sums["margin_total"] == D("-50.00")
    row = {"currency": "USD", "deposit_pct": D("30"), "balance_due_on": None}
    t = client_page.totals(s, row, {})
    assert (t["subtotal"], t["discount_pct"], t["discount"], t["total"]) == ("1000.00", "15", "150.00", "850.00")
    assert "discount" not in client_page.totals({**s, "discount_pct": None}, row, {})


def test_the_price_hash_covers_rows_and_discount_only_when_present() -> None:
    s = {"days": [], "blocks": [], "options": [], "rules": []}
    sums = {"sell_total": D("1"), "deposit": D("1"), "pay_on_arrival_total": D("0")}
    plain = builder.price_hash(_proposal(), "x", None, s, sums)
    assert builder.price_hash(_proposal(), "x", None, {**s, "prices": {}, "discount_pct": None}, sums) == plain
    with_rows = {**s, "prices": {"o": [{"pax_type": "adult", "unit_sell": D("1"), "qty": 2}]}}
    assert builder.price_hash(_proposal(), "x", None, with_rows, sums) != plain
    assert builder.price_hash(_proposal(), "x", None, {**s, "discount_pct": D("5")}, sums) != plain


@pytest.mark.asyncio
async def test_the_view_shows_rows_suggestions_and_the_discount_box() -> None:
    rows = [{"option_id": O1, "pax_type": "adult", "unit_sell": D("100"), "unit_net": D("50"), "qty": 2}]
    conn = _priced_conn(pct=D("15"), rows=rows)
    conn.on("FROM crm.discount_approval WHERE trip_id", [])
    v = await _run(builder.view, P, conn=conn)
    o1 = v["days"][1]["blocks"][0]["options"][0]
    assert o1["prices"] == [{"pax_type": "adult", "unit_sell": "100.00", "qty": 2, "unit_net": "50.00"}]
    assert [r["pax_type"] for r in o1["suggested_prices"]] == ["adult"]
    assert v["totals"]["discount_pct"] == "15" and v["pricing"]["discount"]["needs_approval"]
    assert "discount_needs_approval" in v["checks"]["blocking"] and not v["checks"]["publishable"]
    assert "prices_party_changed" not in v["checks"]["warnings"]  # 2 rows for a party of 2
    owner = await _run(builder.view, P, conn=_priced_conn(pct=D("15"), rows=rows), role="owner")
    assert "discount_owner_approves" in owner["checks"]["warnings"]
    assert "discount_needs_approval" not in owner["checks"]["blocking"]
    # The AA option (O2) suggests AA's RRP x party when the agency has no markup.
    o2 = v["days"][1]["blocks"][0]["options"][1]
    assert o2["aa"]["suggested_basis"] == "aa_rrp" and o2["aa"]["suggested_rules"] == []


@pytest.mark.asyncio
async def test_amounts_follow_the_rows() -> None:
    rows = [{"option_id": O1, "pax_type": "adult", "unit_sell": D("100"), "unit_net": None, "qty": 2}]
    with pytest.raises(Conflict) as err:
        await _run(builder.update_option, P, 5, O1, {"sell_amount": D("10")}, conn=_priced_conn(rows=rows))
    assert err.value.code == "amount_from_prices"
    with pytest.raises(Conflict) as none:
        await _run(builder.update_option, P, 5, O1, {"use_suggested": True}, conn=_priced_conn())
    assert none.value.code == "no_suggestion"  # an agency item has no AA suggestion
    conn = _priced_conn()
    await _run(builder.set_option_prices, P, 5, O1,
               [{"pax_type": "adult", "unit_sell": "120", "unit_net": "70", "qty": 2},
                {"pax_type": "infant", "unit_sell": "0", "unit_net": "0", "qty": 1}], conn=conn)  # fmt: skip
    assert conn.sql("UPDATE crm.option SET sell_amount = $2")[0][1:3] == (D("240.00"), D("140.00"))
    assert len(conn.sql("INSERT INTO crm.option_price")) == 2
    removed = _priced_conn()
    await _run(builder.set_option_prices, P, 5, O1, [], conn=removed)
    assert removed.sql("DELETE FROM crm.option_price") and not removed.sql("UPDATE crm.option SET sell_amount")
    # The suggested grid of the AA option: AA's RRP (200 x 2) split per adult, the AA net untouched.
    aa = _priced_conn()
    await _run(builder.set_option_prices, P, 5, O2, None, use_suggested=True, conn=aa)
    args = aa.sql("UPDATE crm.option SET sell_amount = $2")[0]
    assert args[1] == D("400.00") and args[2] == _options()[1]["net_amount"]


@pytest.mark.asyncio
async def test_publish_gates_a_discount_above_the_limit() -> None:
    with pytest.raises(Conflict) as err:
        await _run(builder.publish, P, 5, acknowledge_selection_seq=None, conn=_priced_conn(pct=D("15")))
    assert err.value.code == "discount_needs_approval" and err.value.extra["limit_pct"] == "10"
    covered = _priced_conn(pct=D("15")).on("FROM crm.discount_approval WHERE trip_id", [_approval("approved", "20")])
    out = await _run(builder.publish, P, 5, acknowledge_selection_seq=None, conn=covered)
    assert out["publish"]["price_hash"]
    owner = _priced_conn(pct=D("15")).on("VALUES ($1, $2, $3, $4, $5, 'approved'", uuid.uuid4())
    await _run(builder.publish, P, 5, acknowledge_selection_seq=None, conn=owner, role="owner")
    assert owner.sql("'approved', now(), $6")


@pytest.mark.asyncio
async def test_the_draft_discount_setting() -> None:
    conn = _priced_conn()
    await _run(builder.update_settings, P, 5, {"discount_pct": D("12.5")}, conn=conn)
    assert conn.sql("discount_pct = $9")[0][8] == D("12.5")
    for bad in (D("100.5"), D("1.234")):
        with pytest.raises(Invalid) as err:
            await _run(builder.update_settings, P, 5, {"discount_pct": bad}, conn=_priced_conn())
        assert err.value.code == "invalid_discount_pct"
    zero = _priced_conn(pct=D("5"))
    await _run(builder.update_settings, P, 5, {"discount_pct": D("0")}, conn=zero)
    assert zero.sql("discount_pct = $9")[0][8] is None


# ── Routes ───────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str, conn: ScriptedConn, router: Any):
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    ctx = tenant_ctx(conn, role=role)

    def tx(*a: Any, **kw: Any) -> Any:
        margin.current_member_role.set(role)
        return fake_tx(ctx)(*a, **kw)

    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", tx)
    monkeypatch.setattr(router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    return TestClient(app), {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}


def test_the_pricing_settings_routes(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _settings_conn().on("SELECT count(*) FROM crm.retail_price_rule", 0)
    conn.on("WHERE retail_price_rule_id = $1 AND deleted_at IS NULL", RULE_ROW)
    client, h = _client(pools, monkeypatch, "owner", conn, ledger_router)
    rule = f"/crm/v1/settings/price-rules/{RULE_ROW['retail_price_rule_id']}"
    with client:
        assert client.get("/crm/v1/settings/pricing", headers=h).json()["child_age_max"] == 11
        assert client.put("/crm/v1/settings/pricing", headers=h, json={"child_age_max": 9}).status_code == 200
        assert client.put("/crm/v1/settings/pricing", headers=h, json={"child_age_max": 40}).status_code == 422
        body = {"name": "Early bird", "kind": "early_booking", "days_before": 60, "adjust_pct": "-10"}
        assert client.post("/crm/v1/settings/price-rules", headers=h, json=body).status_code == 201
        assert client.patch(rule, headers=h, json={"active": False}).status_code == 200
        assert client.delete(rule, headers=h).status_code == 200


@pytest.mark.parametrize("role", ["agent", "assistant"])
def test_only_the_owner_sets_pricing(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    conn = _settings_conn()
    client, h = _client(pools, monkeypatch, role, conn, ledger_router)
    with client:
        assert client.get("/crm/v1/settings/pricing", headers=h).status_code == 403
        assert client.put("/crm/v1/settings/pricing", headers=h, json={"child_age_max": 9}).status_code == 403
    assert not conn.sql("UPDATE crm.tenant_settings")


def test_the_builder_pricing_routes(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _priced_conn().on("FROM crm.discount_approval WHERE discount_approval_id", _approval("pending", "15"))
    client, h = _client(pools, monkeypatch, "owner", conn, builder_router)
    base = f"/crm/v1/proposals/{P}"
    with client:
        res = client.put(f"{base}/options/{O1}/prices", headers=h,
                         json={"seq": 5, "prices": [{"pax_type": "adult", "unit_sell": "10", "qty": 2}]})  # fmt: skip
        assert res.status_code == 200, res.text
        assert client.put(f"{base}/options/{O1}/prices", headers=h, json={"seq": 5}).status_code == 200  # removes
        res = client.post(f"{base}/discount-approval/{uuid.uuid4()}", headers=h, json={"decision": "approved"})
        assert res.status_code == 200, res.text
        bad = client.post(f"{base}/discount-approval/{uuid.uuid4()}", headers=h, json={"decision": "maybe"})
        assert bad.status_code == 422
        not_needed = client.post(f"{base}/discount-approval", headers=h, json={"seq": 5})
        assert not_needed.status_code == 409 and "discount_approval_not_needed" in not_needed.text


@pytest.mark.parametrize("role", ["agent", "assistant"])
def test_the_owner_decides_and_the_assistant_never_prices(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    conn = _priced_conn()
    client, h = _client(pools, monkeypatch, role, conn, builder_router)
    base = f"/crm/v1/proposals/{P}"
    with client:
        res = client.post(f"{base}/discount-approval/{uuid.uuid4()}", headers=h, json={"decision": "approved"})
        assert res.status_code == 403
        if role == "assistant":
            res = client.put(f"{base}/options/{O1}/prices", headers=h, json={"seq": 5, "use_suggested": True})
            assert res.status_code == 403
            res = client.patch(f"{base}/builder", headers=h, json={"seq": 5, "discount_pct": "5"})
            assert res.status_code == 403
    assert not conn.sql("UPDATE crm.discount_approval")
