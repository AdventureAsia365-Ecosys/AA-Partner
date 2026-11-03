"""AA-1025 H1c: the CRM app profile (crm-041, ``app.llm.app_profile``) and its two refusals in the gateway.

- AI off for the whole CRM: one 0 USD refused row ``app_disabled`` (class ``disabled``) before the stage profile's own
  refusals, the scrub, the budget lock or any call; "reply by hand".
- The daily budget of every agency together (``crm.llm_usage_daily``): reached -> ``app_budget_exhausted`` (class
  ``budget``); unreadable -> ``spend_unreadable`` (fail closed); under it the call runs.
- The profile read fails open: no table (before crm-041), a read error or no row = AI on, no daily budget.
- The ops writer: the whole profile, a reason to switch off, the CHECK limits, 503 before crm-041.

Invented model names only; no network, no database.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.llm import app_profile
from app.llm.gateway import MANUAL_REPLY
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import system_ctx
from tests.unit.test_aa964_gateway_core import STAGE, Scripted, conn, done, ledger_rows, routed

NOW = dt.datetime(2026, 11, 3, 9, 0, tzinfo=dt.UTC)


def profile_row(**over: Any) -> dict[str, Any]:
    base = {
        "ai_enabled": True,
        "bulk_paused": False,
        "default_daily_budget_usd": None,
        "degrade_mode": "manual_reply_needed",
        "disabled_reason": None,
        "updated_at": NOW,
        "updated_by": "platform:zz-staff",
    }
    return {**base, **over}


def with_profile(c: ScriptedConn, row: Any, day_spent: Any = Decimal("0")) -> ScriptedConn:
    return c.on("FROM crm.llm_app_profile", row).on("FROM crm.llm_usage_daily", day_spent)


# ── the gateway ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("stage_rows", [None, []])  # a usable stage profile, and none at all
@pytest.mark.asyncio
async def test_ai_off_refuses_first_at_zero_cost(stage_rows: Any) -> None:
    c = with_profile(conn(stage_rows), profile_row(ai_enabled=False, disabled_reason="zz outage"))
    provider = Scripted({0: done()})
    result = await routed(provider).call(system_ctx(c), stage=STAGE, role="writer", prompt="hello")
    assert (result.outcome, result.refusal_code, result.error_class, result.fallback) == (
        "refused",
        "app_disabled",
        "disabled",
        MANUAL_REPLY,
    )
    [row] = ledger_rows(c)
    assert (row[6], row[8], row[9], row[10]) == (Decimal(0), "refused", "app_disabled", "disabled")
    assert provider.calls == [] and c.sql("pg_advisory_xact_lock") == []  # no budget lock, no scrub, no call
    assert c.sql("FROM crm.llm_usage_daily") == []  # AI off needs no spend read


@pytest.mark.asyncio
async def test_the_daily_budget_reached_refuses_with_class_budget() -> None:
    c = with_profile(conn(), profile_row(default_daily_budget_usd=Decimal("1.0000")), Decimal("1.2"))
    provider = Scripted({0: done()})
    result = await routed(provider).call(system_ctx(c), stage=STAGE, role="writer", prompt="hello")
    assert (result.refusal_code, result.error_class) == ("app_budget_exhausted", "budget")
    [row] = ledger_rows(c)
    assert row[6] == Decimal(0) and provider.calls == []


@pytest.mark.asyncio
async def test_a_zero_daily_budget_stops_every_call() -> None:
    c = with_profile(conn(), profile_row(default_daily_budget_usd=Decimal("0")), Decimal("0"))
    result = await routed(Scripted({0: done()})).call(system_ctx(c), stage=STAGE, role="writer", prompt="hi")
    assert result.refusal_code == "app_budget_exhausted"


@pytest.mark.asyncio
async def test_an_unreadable_day_spend_fails_closed() -> None:
    c = with_profile(conn(), profile_row(default_daily_budget_usd=Decimal("5")), RuntimeError("connection reset"))
    result = await routed(Scripted({0: done()})).call(system_ctx(c), stage=STAGE, role="writer", prompt="hi")
    assert (result.refusal_code, result.error_class) == ("spend_unreadable", None)


@pytest.mark.asyncio
async def test_under_the_daily_budget_the_call_runs() -> None:
    c = with_profile(conn(), profile_row(default_daily_budget_usd=Decimal("5")), Decimal("0.5"))
    provider = Scripted({0: done()})
    result = await routed(provider).call(system_ctx(c), stage=STAGE, role="writer", prompt="hello")
    assert result.outcome == "ok" and len(provider.calls) == 1


@pytest.mark.parametrize(
    "row",
    [None, asyncpg.UndefinedTableError("relation does not exist"), RuntimeError("connection reset")],
)
@pytest.mark.asyncio
async def test_no_row_no_table_or_a_read_error_is_ai_on(row: Any) -> None:
    c = with_profile(conn(), row)
    provider = Scripted({0: done()})
    result = await routed(provider).call(system_ctx(c), stage=STAGE, role="writer", prompt="hello")
    assert result.outcome == "ok" and len(provider.calls) == 1
    assert c.sql("FROM crm.llm_usage_daily") == []  # no budget set: no spend read


# ── the module ────────────────────────────────────────────────────────────────────────────────────


def test_profile_of_a_row_and_of_none() -> None:
    p = app_profile.profile_of(profile_row(ai_enabled=False, default_daily_budget_usd=Decimal("2.5")))
    assert (p.ai_enabled, p.default_daily_budget_usd, p.updated_at) == (False, Decimal("2.5"), NOW.isoformat())
    assert app_profile.profile_of(None) == app_profile.DEFAULT
    odd = app_profile.profile_of(profile_row(degrade_mode="other", updated_at="2026-11-03", disabled_reason=""))
    assert odd.degrade_mode == "manual_reply_needed" and odd.updated_at == "2026-11-03" and odd.disabled_reason is None
    assert app_profile.DEFAULT.as_dict() == {
        "app": "crm",
        "ai_enabled": True,
        "bulk_paused": False,
        "default_daily_budget_usd": None,
        "degrade_mode": "manual_reply_needed",
        "disabled_reason": None,
        "updated_at": None,
        "updated_by": None,
    }
    assert (
        app_profile.profile_of(profile_row(default_daily_budget_usd=Decimal("1.5"))).as_dict()[
            "default_daily_budget_usd"
        ]
        == "1.5000"
    )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"ai_enabled": False}, "say why"),
        ({"ai_enabled": False, "disabled_reason": "x"}, "say why"),
        ({"ai_enabled": False, "disabled_reason": "x" * 501}, "at most 500"),
        ({"ai_enabled": True, "default_daily_budget_usd": -1}, "between 0 and 100000"),
        ({"ai_enabled": True, "default_daily_budget_usd": 100001}, "between 0 and 100000"),
        ({"ai_enabled": True, "degrade_mode": "queue_retry"}, "degrade_mode"),
        ({"ai_enabled": True, "app": "cis"}, "not editable"),
    ],
)
def test_merged_refuses_what_the_table_would(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        app_profile.merged(fields)


def test_merged_clears_the_reason_when_ai_is_on_and_rounds_the_budget() -> None:
    on = app_profile.merged({"ai_enabled": True, "disabled_reason": "zz old", "default_daily_budget_usd": 1.23456})
    assert on.disabled_reason is None and on.default_daily_budget_usd == Decimal("1.2346")
    off = app_profile.merged({"ai_enabled": False, "disabled_reason": "  zz outage  ", "bulk_paused": True})
    assert (off.ai_enabled, off.disabled_reason, off.bulk_paused) == (False, "zz outage", True)


@pytest.mark.asyncio
async def test_write_upserts_the_whole_profile_as_the_staff_member() -> None:
    c = ScriptedConn().on("INSERT INTO crm.llm_app_profile", profile_row(ai_enabled=False, disabled_reason="zz"))
    saved = await app_profile.write(
        c,
        {"ai_enabled": False, "disabled_reason": "zz outage"},
        actor="platform:zz-staff",  # type: ignore[arg-type]
    )
    assert saved.ai_enabled is False
    [args] = c.sql("INSERT INTO crm.llm_app_profile")
    assert args == (False, False, None, "manual_reply_needed", "zz outage", "platform:zz-staff")


@pytest.mark.asyncio
async def test_read_and_write_say_unavailable_before_crm_041() -> None:
    missing = asyncpg.UndefinedTableError("relation does not exist")
    c = ScriptedConn().on("crm.llm_app_profile", missing)
    with pytest.raises(app_profile.AppProfileUnavailable):
        await app_profile.read(c)  # type: ignore[arg-type]
    with pytest.raises(app_profile.AppProfileUnavailable):
        await app_profile.write(c, {"ai_enabled": True}, actor="platform:zz-staff")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_read_without_a_row_is_the_defaults() -> None:
    assert await app_profile.read(ScriptedConn()) == app_profile.DEFAULT  # type: ignore[arg-type]
