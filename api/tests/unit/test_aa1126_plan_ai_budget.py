"""AA-1126 L3 / G3 / L4 (crm-076): the agency AI budget comes from its CRM plan (projected from the CIS snapshot's
crm.ai_budget_usd_monthly, the ceiling and the no-row budget; the owner's budget follows the plan while it equals the
previous one), the settings are the fallback, Settings > AI says where the budget comes from, and under the prod spend
rules the CRM app's daily budget only alerts and an unreadable spend lets the draft go on. Invented ids only; no DB."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest

from app import platform_settings as ps
from app.cis import entitlement_feed as feed
from app.llm import budget_limits, spend_environment, spend_rules
from app.llm import gateway as gw
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import tenant_ctx
from tests.unit.test_entitlement_feed import account, pool

A = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
NOW = dt.datetime(2026, 11, 17, 8, 0, tzinfo=dt.UTC)


def _ents(budget: str | None) -> dict[str, str | None]:
    ents: dict[str, str | None] = {"crm.core": None, "crm.ai_assist": None, "crm.seats": "5.00"}
    ents[feed.PLAN_AI_BUDGET_KEY] = budget
    return ents


# ── the projection ───────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("limit", "expected"),
    [("12.50", Decimal("12.50")), ("0.00", Decimal("0.00")), ("25000.00", Decimal("10000.00")), (None, None)],
)
def test_the_projection_reads_the_plan_budget(limit: str | None, expected: Decimal | None) -> None:
    wanted = feed.projection(feed.parse_account(account(ents=_ents(limit)), A))
    assert wanted.ai_budget_usd == expected


def test_no_plan_budget_key_projects_none() -> None:
    assert feed.projection(feed.parse_account(account(), A)).ai_budget_usd is None


def _row(budget: str | None) -> dict[str, Any]:
    return {"plan": "core", "member_limit": 5, "allowed_features": ["ai_assist"], "projected_at": NOW,
            "tp_crm_leads": False, "ai_budget_usd": Decimal(budget) if budget is not None else None}  # fmt: skip


@pytest.mark.asyncio
async def test_a_new_plan_budget_is_written_and_the_owner_budget_follows_it() -> None:
    conn = pool(statuses={A: "active"}, entitlement=_row("2.00"))
    conn.on("UPDATE crm.tenant_settings SET ai_budget_usd", 1)
    wanted = feed.Projection("core", 5, ("ai_assist",), True, True, None, False, Decimal("15.00"))
    assert await feed.apply(conn, A, wanted, feed.RunCounts()) == "applied"  # type: ignore[arg-type]
    assert conn.sql("INSERT INTO crm.tenant_entitlement") == [("core", 5, ["ai_assist"], False, Decimal("15.00"))]
    assert conn.sql("UPDATE crm.tenant_settings SET ai_budget_usd") == [(Decimal("15.00"), Decimal("2.00"))]
    actions = [a[2] for a in conn.sql("INSERT INTO crm.audit_event")]
    assert "entitlements_projected" in actions and "ai_budget_follows_plan" in actions


@pytest.mark.asyncio
async def test_the_first_plan_budget_moves_an_owner_budget_still_at_the_default() -> None:
    conn = pool(statuses={A: "active"}, entitlement=_row(None))
    conn.on("UPDATE crm.tenant_settings SET ai_budget_usd", None)  # the owner chose another budget: nothing follows
    wanted = feed.Projection("core", 5, ("ai_assist",), True, True, None, False, Decimal("8.00"))
    await feed.apply(conn, A, wanted, feed.RunCounts())  # type: ignore[arg-type]
    assert conn.sql("UPDATE crm.tenant_settings SET ai_budget_usd") == [
        (Decimal("8.00"), budget_limits.default_budget_usd())
    ]
    assert "ai_budget_follows_plan" not in [a[2] for a in conn.sql("INSERT INTO crm.audit_event")]


@pytest.mark.asyncio
async def test_an_unchanged_or_removed_plan_budget_leaves_the_owner_budget() -> None:
    conn = pool(statuses={A: "active"}, entitlement=_row("15.00"))
    same = feed.Projection("core", 5, ("ai_assist",), True, True, None, False, Decimal("15.00"))
    assert await feed.apply(conn, A, same, feed.RunCounts()) == "unchanged"  # type: ignore[arg-type]
    gone = feed.Projection("core", 5, ("ai_assist",), True, True, None, False, None)
    assert await feed.apply(conn, A, gone, feed.RunCounts()) == "applied"  # type: ignore[arg-type]
    assert conn.sql("UPDATE crm.tenant_settings SET ai_budget_usd") == []


# ── the limits ───────────────────────────────────────────────────────────────────────────────────────────────────


def _ctx(plan: Any = None, *, error: bool = False, own: str | None = None) -> Any:
    """An agency context: its plan budget (or a read error) and, ``own``, an administrator's own ceiling for it."""
    ps.forget_tenant()
    conn = ScriptedConn()
    conn.on(budget_limits.PLAN_BUDGET_SQL, RuntimeError("no grant") if error else plan)
    rows = [{"key": ps.AI_BUDGET_MAX, "value_text": own, "version": 3}] if own is not None else []
    conn.on("crm.setting_value", rows)
    return tenant_ctx(conn)


@pytest.mark.asyncio
async def test_the_plan_budget_is_the_ceiling_and_the_no_row_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(Decimal("15.00"))
    assert await budget_limits.plan_budget(ctx) == Decimal("15.00")
    assert await budget_limits.budget_ceiling(ctx) == Decimal("15.00")
    assert await budget_limits.new_agency_budget(ctx) == Decimal("15.00")
    assert await budget_limits.agency_budget(ctx, None) == Decimal("15.00")
    assert await budget_limits.agency_budget(ctx, {"ai_budget_usd": Decimal("40")}) == Decimal("15.00")  # capped
    assert await budget_limits.agency_budget(ctx, {"ai_budget_usd": Decimal("3")}) == Decimal("3")  # owner's lower
    assert await budget_limits.budget_source(ctx) == "plan"
    assert await budget_limits.ceiling(ctx) == (Decimal("15.00"), "plan")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan, own, enforced, source",
    [
        (Decimal("2.00"), "7", Decimal("7"), "agency"),  # an administrator's own ceiling wins over the plan
        (Decimal("2.00"), "1", Decimal("1"), "agency"),  # lower too: the administrator's decision stands
        (Decimal("12.00"), None, Decimal("12.00"), "plan"),  # no own value: the plan
        (None, "5", Decimal("5"), "agency"),  # no plan budget: the own value
    ],
)
async def test_the_admin_page_value_is_the_enforced_ceiling(
    plan: Decimal | None, own: str | None, enforced: Decimal, source: str
) -> None:
    """S283 review fix (keep the admin threshold pages in sync): the per-agency value the CIS Platform › Settings CRM
    tab lists under crm.ai.budget_max_usd (ps.resolve with the agency: source tenant) is the ceiling the gateway
    enforces, with or without a plan budget; without an own value the plan's budget is (Tenant 360 / Plans page)."""
    ctx = _ctx(plan, own=own)
    assert await budget_limits.ceiling(ctx) == (enforced, source)
    listed = ps.resolve(ps.AI_BUDGET_MAX, ctx.tenant_id)
    assert (listed.source == "tenant") is (own is not None)
    if own is not None:
        assert ps.as_decimal(listed.value) == await budget_limits.budget_ceiling(ctx)
    assert await budget_limits.agency_budget(ctx, {"ai_budget_usd": Decimal("100")}) == enforced
    assert await budget_limits.budget_source(ctx) == ("plan" if source == "plan" else "settings")
    ps.forget_tenant()


@pytest.mark.asyncio
async def test_without_a_plan_budget_the_settings_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    async def tenant_get(conn: Any, key: str, tenant_id: Any) -> Any:
        assert key == ps.AI_BUDGET_MAX
        return Decimal("4")

    monkeypatch.setattr(ps, "tenant_get", tenant_get)
    for ctx in (_ctx(None), _ctx(error=True)):  # none on the plan, or unreadable: the settings, never an error
        assert await budget_limits.plan_budget(ctx) is None
        assert await budget_limits.budget_ceiling(ctx) == Decimal("4")
        assert await budget_limits.new_agency_budget(ctx) == budget_limits.default_budget_usd()


# ── G3 / L4: the spend rules in the gateway ──────────────────────────────────────────────────────────────────────


class _App:
    ai_enabled = True
    default_daily_budget_usd = Decimal("1.00")


def _gateway_app(monkeypatch: pytest.MonkeyPatch, spent: Decimal | None, env: str) -> gw.LlmGateway:
    async def for_call(conn: Any) -> _App:
        return _App()

    async def day_spend(conn: Any) -> Decimal | None:
        return spent

    monkeypatch.setattr(gw.app_profile, "for_call", for_call)
    monkeypatch.setattr(gw.app_profile, "day_spend", day_spend)
    monkeypatch.setattr(spend_environment, "spend_environment", lambda: env)
    return gw.LlmGateway.__new__(gw.LlmGateway)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spent", "env", "expected"),
    [
        (Decimal("2.00"), "dev", (gw.APP_BUDGET_EXHAUSTED, "budget")),
        (None, "dev", (gw.SPEND_UNREADABLE, None)),
        (Decimal("2.00"), "prod", (None, None)),
        (None, "prod", (None, None)),
        (Decimal("0.10"), "dev", (None, None)),
    ],
)
async def test_the_app_daily_budget_stops_on_dev_and_alerts_on_prod(
    monkeypatch: pytest.MonkeyPatch, spent: Decimal | None, env: str, expected: tuple[str | None, str | None]
) -> None:
    g = _gateway_app(monkeypatch, spent, env)
    code, cls = await g._app_refusal(tenant_ctx(ScriptedConn()))
    assert code == expected[0]
    assert (cls is None) == (expected[1] is None)


@pytest.mark.asyncio
async def test_an_unreadable_month_degrades_on_prod_only(monkeypatch: pytest.MonkeyPatch) -> None:
    g = gw.LlmGateway.__new__(gw.LlmGateway)
    conn = ScriptedConn().on(gw.MONTH_SPEND_SQL, RuntimeError("zz db down"))
    ctx = tenant_ctx(conn)
    monkeypatch.setattr(spend_environment, "spend_environment", lambda: "dev")
    assert await g._month_spend(ctx) is None  # dev: the decision then refuses (spend_unreadable)
    monkeypatch.setattr(spend_environment, "spend_environment", lambda: "prod")
    assert await g._month_spend(ctx) == Decimal(0)  # prod: degrade, the call goes on
    assert spend_rules.unreadable_month() == Decimal(0)


@pytest.mark.asyncio
async def test_a_used_up_agency_budget_stays_hard_on_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    g = gw.LlmGateway.__new__(gw.LlmGateway)
    refusals: list[str] = []

    async def log(ctx: Any, stage: str, role: str, status: str, **kw: Any) -> str:
        refusals.append(kw.get("refusal"))
        return "refused"

    async def alert(ctx: Any, result: Any, settings: Any, pct: Any) -> Any:
        return result

    monkeypatch.setattr(g, "_log", log, raising=False)
    monkeypatch.setattr(g, "_alert", alert, raising=False)
    monkeypatch.setattr(spend_environment, "spend_environment", lambda: "prod")
    ctx = tenant_ctx(ScriptedConn())
    out = await g._budget_refusal(ctx, "crm_draft_reply", "writer", None, Decimal("2"), Decimal("2"), None, None)
    assert out == "refused" and refusals == [gw.BUDGET_EXHAUSTED]
