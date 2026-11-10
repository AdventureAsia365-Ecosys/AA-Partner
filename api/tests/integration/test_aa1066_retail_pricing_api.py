"""AA-1066 end to end on PostgreSQL with the RDS role shape: retail pricing in the day-list builder, as crm_app
through ``tenant_tx()`` with real field encryption and the in-memory Booking (``tests/aa_fakes.py``).

- **Price per traveller type** (item crm-05): rows replace an option's sell (sum) and an agency item's net; the
  assistant never reads a unit net; a typed sell is refused while rows exist; a working copy copies the rows; a
  published version's rows never change; the agency's age bands move a child into the adult count.
- **Retail price rules** (item crm-06): owner-only settings; a season rule, a group rule and two best-only early
  booking rules on an AA tour: the add rules sum, only the first best-only rule applies; the rules used are stored on
  the option and the grid suggestion shares the suggested sell per head.
- **Discount with owner approval** (item crm-07): an agent's discount above the limit blocks Publish (409
  ``discount_needs_approval``) until the owner approves the request; an agent cannot decide; a raised discount
  re-opens the approval; an owner's own discount is recorded as approved at publish; a discounted total below the
  net needs an approval of that too; another agency sees nothing.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.permissions import Action
from app.proposals import builder, pricing, retail
from app.tenancy import tenant_tx
from tests.aa_fakes import FakeBooking
from tests.integration.conftest import OWNER
from tests.integration.test_builder_api import (
    _aa,
    _as,
    _block,
    _catalog_tour,
    _create,
    _option,
    _pid,
    _publishable,
    _setup,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _no_catalog_row_left(admin: asyncpg.Connection) -> Any:
    """The AA tour row this file adds (``_catalog_tour``, a platform table) must not outlive it: the catalog cache test
    of test_aa_lines_api.py counts every cached tour CIS no longer lists."""
    yield
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute("DELETE FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = 'T-949'")
    finally:
        await admin.execute("RESET ROLE")


async def _plain(pool: asyncpg.Pool, s: dict[str, Any], role: str, fn: Any, *args: Any, **kw: Any) -> Any:
    async with tenant_tx(pool, s["who"][role], s["tenant"]) as ctx:
        return await fn(ctx, *args, **kw)


def _opt(v: dict[str, Any], day_no: int = 1, index: int = 0) -> dict[str, Any]:
    return _block(v, day_no)["options"][index]


async def _prices(pool: asyncpg.Pool, s: dict[str, Any], v: dict[str, Any], option: dict[str, Any], rows: Any,
                  role: str = "agent", **kw: Any) -> dict[str, Any]:  # fmt: skip
    return await _as(pool, s, role, builder.set_option_prices, _pid(v), v["edit_seq"],
                     uuid.UUID(option["option_id"]), rows, **kw)  # fmt: skip


ROWS = [
    {"pax_type": "adult", "unit_sell": Decimal("400"), "unit_net": Decimal("200"), "qty": 2},
    {"pax_type": "child", "unit_sell": Decimal("250"), "unit_net": Decimal("120"), "qty": 1},
    {"pax_type": "single_supp", "unit_sell": Decimal("80"), "unit_net": Decimal("40"), "qty": 1},
]


async def test_price_per_traveller_type_on_an_option(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _create(app_pool, s)
    v = await _option(app_pool, s, v, _block(v, 1), title="Riverside lodge", sell_amount=Decimal("1"))
    # The party by retail type (2 adults and a child of 8, one family room): the grid the page offers.
    assert v["pricing"]["party"] == {"adult": 2, "child": 1, "infant": 0, "single_supp": 0}
    assert [r["pax_type"] for r in _opt(v)["suggested_prices"]] == ["adult", "child"]
    v = await _prices(app_pool, s, v, _opt(v), ROWS)
    o = _opt(v)
    assert o["sell_amount"] == "1130.00" and o["net_amount"] == "560.00" and o["price_rules"] is None
    assert [(r["pax_type"], r["unit_sell"], r["qty"], r["unit_net"]) for r in o["prices"]] == [
        ("adult", "400.00", 2, "200.00"), ("child", "250.00", 1, "120.00"), ("single_supp", "80.00", 1, "40.00"),
    ]  # fmt: skip
    assert v["totals"]["sell_total"] == "1130.00"
    # The rows count 3 people + 1 single supplement: the party has 3, so no warning.
    assert "prices_party_changed" not in v["checks"]["warnings"]
    # The assistant reads the types and unit sells, never a unit net.
    seen = await _as(app_pool, s, "assistant", builder.view, _pid(v))
    assert all("unit_net" not in r for r in _opt(seen)["prices"])
    # A typed sell or net is refused while the rows decide the amounts.
    for change in ({"sell_amount": Decimal("10")}, {"net_amount": Decimal("10")}):
        with pytest.raises(Conflict) as err:
            await _as(app_pool, s, "agent", builder.update_option, _pid(v), v["edit_seq"],
                      uuid.UUID(o["option_id"]), change)  # fmt: skip
        assert err.value.code == "amount_from_prices"
    # Bad rows: a unit net on some rows only, a type twice, nothing charged.
    for bad, code in (
        ([{**ROWS[0]}, {**ROWS[1], "unit_net": None}], "unit_net_partial"),
        ([ROWS[0], ROWS[0]], "invalid_prices"),
        ([{**ROWS[0], "qty": 0}], "prices_nothing_charged"),
    ):
        with pytest.raises(Invalid) as bad_err:
            await _prices(app_pool, s, v, o, bad)
        assert bad_err.value.code == code
    # Removing the rows keeps the sell they summed to.
    v2 = await _prices(app_pool, s, v, o, [])
    assert _opt(v2)["prices"] == [] and _opt(v2)["sell_amount"] == "1130.00"
    v = await _prices(app_pool, s, v2, _opt(v2), ROWS)
    # The agency's bands: with children up to 7 only, the 8-year-old is priced as an adult; the rows now count other
    # travellers than the party (a warning, the agent decides).
    await _plain(app_pool, s, "owner", pricing.update_settings, {"child_age_max": 7})
    v = await _as(app_pool, s, "agent", builder.view, _pid(v))
    assert v["pricing"]["party"] == {"adult": 3, "child": 0, "infant": 0, "single_supp": 0}
    assert v["pricing"]["bands"] == {"child_age_max": 7, "infant_age_max": 1}


async def test_rows_are_published_with_the_version_and_copied_to_a_working_copy(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _publishable(app_pool, s)
    before = v["totals"]["sell_total"]
    o = _opt(v, 2)
    v = await _prices(app_pool, s, v, o, [{"pax_type": "adult", "unit_sell": Decimal("100"), "qty": 2},
                                          {"pax_type": "child", "unit_sell": Decimal("50"), "qty": 1}])  # fmt: skip
    assert _opt(v, 2)["sell_amount"] == "250.00" and v["totals"]["sell_total"] == before
    out = await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    assert out["status"] == "published"
    with pytest.raises(Conflict) as err:  # a published version never changes
        await _prices(app_pool, s, out, _opt(out, 2), [])
    assert err.value.code == "proposal_not_draft"
    copy = await _create(app_pool, s, from_proposal_id=_pid(out))
    assert [r["qty"] for r in _opt(copy, 2)["prices"]] == [2, 1]
    assert _opt(copy, 2)["sell_amount"] == "250.00"


async def test_price_rules_adjust_the_aa_suggestion(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    await _catalog_tour(app_pool)
    start = s["start"]
    for body in (
        {"name": "Peak weeks", "kind": "season", "date_from": start - dt.timedelta(days=3),
         "date_to": start + dt.timedelta(days=3), "adjust_pct": Decimal("20"), "priority": 50},
        {"name": "Family of three", "kind": "group_size", "min_pax": 3, "adjust_pct": Decimal("-5")},
        {"name": "Early bird 30", "kind": "early_booking", "days_before": 30, "adjust_pct": Decimal("-10"),
         "priority": 10, "stacking": "best_only"},
        {"name": "Early bird 60", "kind": "early_booking", "days_before": 60, "adjust_pct": Decimal("-15"),
         "priority": 20, "stacking": "best_only"},
        {"name": "Hotels only", "kind": "group_size", "min_pax": 2, "adjust_pct": Decimal("50"),
         "product_scope": "hotel_room"},
        {"name": "Last minute", "kind": "last_minute", "days_before": 14, "adjust_pct": Decimal("-30")},
    ):  # fmt: skip
        out = await _plain(app_pool, s, "owner", pricing.create_rule, body)
    assert [r["name"] for r in out["rules"]][:2] == ["Early bird 30", "Early bird 60"]  # priority order
    # Settings > Pricing is the owner's (the route's action): the agent and the assistant are refused.
    for role in ("agent", "assistant"):
        with pytest.raises(Forbidden):
            async with tenant_tx(app_pool, s["who"][role], s["tenant"], action=Action.PAYMENT_SETTINGS):
                pass
    # A new AA tour without a sell: AA's RRP (800 x 3) with +20 -5 and only the first best-only rule (-10) = +5 %.
    fake = FakeBooking(b2b_price="900.00")
    v = await _create(app_pool, s)
    v = await _aa(app_pool, s, fake, v)
    o = _opt(v)
    assert o["sell_amount"] == "2520.00"
    assert [r["name"] for r in o["price_rules"]] == ["Early bird 30", "Peak weeks", "Family of three"]
    assert o["aa"]["suggested_sell"] == "2520.00" and o["aa"]["suggested_basis"] == "aa_rrp"
    assert [r["name"] for r in o["aa"]["suggested_rules"]] == ["Early bird 30", "Peak weeks", "Family of three"]
    # The grid shares the suggestion per head (adults and the child), the rules kept on the option.
    v = await _prices(app_pool, s, v, o, None, use_suggested=True)
    o = _opt(v)
    assert [(r["pax_type"], r["unit_sell"], r["qty"]) for r in o["prices"]] == [("adult", "840.00", 2),
                                                                               ("child", "840.00", 1)]  # fmt: skip
    assert o["sell_amount"] == "2520.00" and len(o["price_rules"]) == 3
    # The agency markup on the B2B price (900 x 1.2 = 1080), then the same rules (+5 %).
    await _plain(app_pool, s, "owner", retail.update, {"trip_plan": 20})
    v = await _prices(app_pool, s, v, o, [])
    v = await _as(app_pool, s, "agent", builder.update_option, _pid(v), v["edit_seq"], uuid.UUID(o["option_id"]),
                  {"use_suggested": True})  # fmt: skip
    assert _opt(v)["sell_amount"] == "1134.00" and _opt(v)["aa"]["suggested_basis"] == "agency_markup"
    # A typed sell clears the rules: priced by hand.
    v = await _as(app_pool, s, "agent", builder.update_option, _pid(v), v["edit_seq"], uuid.UUID(o["option_id"]),
                  {"sell_amount": Decimal("2000")})  # fmt: skip
    assert _opt(v)["price_rules"] is None
    # A rule edit and delete; an unknown rule is not found; a rule without its own condition is refused.
    rule_id = uuid.UUID(out["rules"][0]["retail_price_rule_id"])
    out = await _plain(app_pool, s, "owner", pricing.update_rule, rule_id, {"active": False})
    assert out["rules"][0]["active"] is False
    out = await _plain(app_pool, s, "owner", pricing.delete_rule, rule_id)
    assert all(r["retail_price_rule_id"] != str(rule_id) for r in out["rules"])
    with pytest.raises(NotFound):
        await _plain(app_pool, s, "owner", pricing.delete_rule, rule_id)
    with pytest.raises(Invalid) as err:
        await _plain(app_pool, s, "owner", pricing.create_rule, {"name": "No dates", "kind": "season",
                                                                 "adjust_pct": Decimal("10")})  # fmt: skip
    assert err.value.code == "invalid_season"


async def _discounted(pool: asyncpg.Pool, s: dict[str, Any], pct: str, role: str = "agent") -> dict[str, Any]:
    v = await _publishable(pool, s)
    return await _as(pool, s, role, builder.update_settings, _pid(v), v["edit_seq"], {"discount_pct": Decimal(pct)})


async def test_a_discount_above_the_limit_needs_the_owner(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _discounted(app_pool, s, "15")
    # 1000 charged, 15 % off: 850; the deposit follows; pay on arrival is never discounted.
    assert v["totals"]["subtotal"] == "1000.00" and v["totals"]["discount"] == "150.00"
    assert v["totals"]["sell_total"] == "850.00" and v["totals"]["deposit"] == "255.00"
    assert v["totals"]["pay_on_arrival_total"] == "45.00" and v["totals"]["margin_total"] == "250.00"
    d = v["pricing"]["discount"]
    assert d["above_limit"] and d["needs_approval"] and d["limit_pct"] == "10.00" and d["pending"] is None
    assert "discount_needs_approval" in v["checks"]["blocking"] and not v["checks"]["publishable"]
    # The owner sees the same draft as publishable (their publish records their own approval).
    as_owner = await _as(app_pool, s, "owner", builder.view, _pid(v))
    assert as_owner["checks"]["publishable"] and "discount_owner_approves" in as_owner["checks"]["warnings"]
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    assert err.value.code == "discount_needs_approval" and err.value.extra["reason"] == "above_limit"
    # The agent asks: one pending request and an urgent task for the owner; asking again is the same request.
    v = await _as(app_pool, s, "agent", builder.request_discount_approval, _pid(v), v["edit_seq"])
    pending = v["pricing"]["discount"]["pending"]
    assert pending["status"] == "pending" and pending["discount_pct"] == "15.00"
    again = await _as(app_pool, s, "agent", builder.request_discount_approval, _pid(v), v["edit_seq"])
    assert again["pricing"]["discount"]["pending"]["discount_approval_id"] == pending["discount_approval_id"]
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        task = await ctx.conn.fetchrow(
            "SELECT assignee_member_id, priority FROM crm.task WHERE trip_id = $1 AND kind = 'discount_approval' "
            "AND status = 'open'", s["trip"],
        )  # fmt: skip
    assert task["assignee_member_id"] == s["members"]["owner"] and task["priority"] == "urgent"
    # The agent cannot decide: the route's action refuses, and the database guard too.
    with pytest.raises(Forbidden):
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"], action=Action.DISCOUNT_APPROVE):
            pass
    with pytest.raises(asyncpg.PostgresError) as guard:
        await _as(app_pool, s, "agent", builder.decide_discount_approval, _pid(v),
                  uuid.UUID(pending["discount_approval_id"]), "approved")  # fmt: skip
    assert guard.value.sqlstate == "CRM48"
    # Another agency cannot see or decide it.
    other = await _setup(app_pool)
    with pytest.raises(NotFound):
        await _as(app_pool, other, "owner", builder.decide_discount_approval, _pid(v),
                  uuid.UUID(pending["discount_approval_id"]), "approved")  # fmt: skip
    # The owner approves: the task closes, the agent publishes.
    v = await _as(app_pool, s, "owner", builder.decide_discount_approval, _pid(v),
                  uuid.UUID(pending["discount_approval_id"]), "approved")  # fmt: skip
    assert not v["pricing"]["discount"]["needs_approval"] and v["pricing"]["discount"]["approved_pct"] == "15.00"
    with pytest.raises(Conflict) as twice:
        await _as(app_pool, s, "owner", builder.decide_discount_approval, _pid(v),
                  uuid.UUID(pending["discount_approval_id"]), "rejected")  # fmt: skip
    assert twice.value.code == "discount_approval_not_pending"
    out = await _as(app_pool, s, "agent", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    assert out["status"] == "published" and out["totals"]["sell_total"] == "850.00"
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        assert await ctx.conn.fetchval(
            "SELECT total_amount FROM crm.proposal WHERE proposal_id = $1", _pid(out)
        ) == Decimal("850.00")
        assert await ctx.conn.fetchval(
            "SELECT count(*) FROM crm.task WHERE trip_id = $1 AND kind = 'discount_approval' AND status = 'open'",
            s["trip"],
        ) == 0  # fmt: skip
    # A working copy at 20 %: the 15 % approval no longer covers it (re-opened).
    copy = await _create(app_pool, s, from_proposal_id=_pid(out))
    assert copy["totals"]["discount_pct"] == "15.00" and not copy["pricing"]["discount"]["needs_approval"]
    copy = await _as(app_pool, s, "agent", builder.update_settings, _pid(copy), copy["edit_seq"],
                     {"discount_pct": Decimal("20")})  # fmt: skip
    assert copy["pricing"]["discount"]["needs_approval"]
    # The owner raises the limit to 25 %: 20 % needs nobody.
    await _plain(app_pool, s, "owner", pricing.update_settings, {"discount_owner_limit_pct": Decimal("25")})
    copy = await _as(app_pool, s, "agent", builder.view, _pid(copy))
    assert not copy["pricing"]["discount"]["above_limit"] and copy["checks"]["publishable"]


async def test_an_owner_discount_is_recorded_and_a_sale_below_cost_needs_approval(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    v = await _discounted(app_pool, s, "30", role="owner")
    out = await _as(app_pool, s, "owner", builder.publish, _pid(v), v["edit_seq"], acknowledge_selection_seq=None)
    assert out["status"] == "published"
    history = out["pricing"]["discount"]["history"]
    assert history[0]["status"] == "approved" and history[0]["decided_by"] == f"member:{s['members']['owner']}"
    # Below cost: 60 % off 1000 = 400 < the net of 600. The owner's 30 % approval does not cover it.
    copy = await _create(app_pool, s, from_proposal_id=_pid(out))
    copy = await _as(app_pool, s, "agent", builder.update_settings, _pid(copy), copy["edit_seq"],
                     {"discount_pct": Decimal("60")})  # fmt: skip
    d = copy["pricing"]["discount"]
    assert d["below_cost"] and d["needs_approval"] and copy["totals"]["margin_total"] == "-200.00"
    seen = await _as(app_pool, s, "assistant", builder.view, _pid(copy))
    assert "below_cost" not in seen["pricing"]["discount"]  # margin data, never built for the assistant
    copy = await _as(app_pool, s, "agent", builder.request_discount_approval, _pid(copy), copy["edit_seq"])
    assert copy["pricing"]["discount"]["pending"]["below_cost"] is True
    # A discount the limit covers needs no request.
    within = await _as(app_pool, s, "agent", builder.update_settings, _pid(copy), copy["edit_seq"],
                       {"discount_pct": Decimal("5")})  # fmt: skip
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", builder.request_discount_approval, _pid(within), within["edit_seq"])
    assert err.value.code == "discount_approval_not_needed"
    cleared = await _as(app_pool, s, "agent", builder.update_settings, _pid(within), within["edit_seq"],
                        {"discount_pct": None})  # fmt: skip
    assert "discount" not in cleared["totals"] and cleared["totals"]["sell_total"] == "1000.00"
