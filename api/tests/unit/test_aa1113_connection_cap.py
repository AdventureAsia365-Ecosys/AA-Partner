"""AA-1113 (root ADR 0009 decision 6; CIS 450): one connection cap for every caller, the CRM's side
(:mod:`app.llm.connection_cap`).

- The rule: the CIS ledger's month spend on the connection (``catalog.v_llm_connection_cap_v1``) plus the CRM's own
  (``crm.llm_usage_daily`` since the view's month start) against the connection's budget; a hard-stop policy at or
  above it refuses ``connection_budget_exhausted`` under the dev spend rules and only logs under prod; no row = no cap;
  a missing view (CIS 450 not applied) goes on; an unreadable cap refuses ``spend_unreadable`` in dev only; a provider
  with no CIS connection (the stub, an unmapped role connection) reads nothing.
- Through the real gateway, each of the three CRM AI paths: over the cap = one 0 USD refused row and the provider is
  never called; under it = the answered call and its row, as before.
Fakes only (no database, no AWS, no paid call); invented names and amounts.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.llm import connection_cap, spend_environment
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa1113_ai_paths_ledger import (  # noqa: F401 — the autouse isolation fixture
    IDS,
    PATHS,
    FakeProvider,
    _isolated,
    gateway,
    gateway_conn,
    ledger,
)

pytestmark = pytest.mark.asyncio

MONTH = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)


class BedrockLike(FakeProvider):
    """The fake model on the task role's own Bedrock connection (the CRM's live route)."""

    connection_key = "bedrock"


def cap_row(budget: str = "10", spent: str = "8", hard_stop: bool = True) -> dict[str, Any]:
    return {"connection_key": "acc2", "monthly_budget_usd": Decimal(budget), "month_start": MONTH,
            "month_spent_usd": Decimal(spent), "hard_stop": hard_stop}  # fmt: skip


def cap_conn(row: Any = None, crm: str = "0", conn: ScriptedConn | None = None) -> ScriptedConn:
    c = conn or ScriptedConn()
    return c.on("FROM catalog.v_llm_connection_cap_v1", row).on("WHERE day >=", Decimal(crm))


@pytest.fixture(autouse=True)
def _cold_cap_cache() -> Iterator[None]:
    connection_cap.invalidate()
    yield
    connection_cap.invalidate()


@pytest.fixture
def prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spend_environment, "is_prod", lambda: True)


# ── the rule ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("row", "crm", "expected"),
    [
        (cap_row("10", "8"), "3", ("connection_budget_exhausted", "budget")),  # 8 CIS + 3 CRM >= 10
        (cap_row("10", "10"), "0", ("connection_budget_exhausted", "budget")),  # exactly at the cap
        (cap_row("0", "0"), "0", ("connection_budget_exhausted", "budget")),  # a 0 USD budget: no spend allowed
        (cap_row("10", "8"), "1", (None, None)),  # under
        (cap_row("10", "12", hard_stop=False), "0", (None, None)),  # alert-only policy
        (None, "50", (None, None)),  # no budget row: no cap
    ],
    ids=["cis-plus-crm-over", "at-cap", "zero-budget", "under", "alert-only", "no-cap"],
)
async def test_the_rule_in_dev(row: Any, crm: str, expected: tuple[Any, Any]) -> None:
    c = cap_conn(row, crm)
    assert await connection_cap.refusal(c, BedrockLike()) == expected
    [(key,)] = c.sql("FROM catalog.v_llm_connection_cap_v1")
    assert key == "acc2"
    if row is not None and row["hard_stop"]:
        assert c.sql("WHERE day >=") == [(MONTH,)]  # the CRM's month from the view's month start
    else:
        assert c.sql("WHERE day >=") == []  # no budget or alert-only: the CRM's spend cannot make it stop


async def test_prod_never_refuses_for_the_cap(prod: None) -> None:
    assert await connection_cap.refusal(cap_conn(cap_row("10", "20")), BedrockLike()) == (None, None)


async def test_a_missing_view_goes_on_and_a_read_error_fails_closed_in_dev_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_cap._unmapped_logged.clear()
    missing = ScriptedConn().on("FROM catalog.v_llm_connection_cap_v1", asyncpg.UndefinedTableError("no view"))
    assert await connection_cap.refusal(missing, BedrockLike()) == (None, None)
    connection_cap.invalidate()
    denied = ScriptedConn().on("FROM catalog.v_llm_connection_cap_v1", asyncpg.InsufficientPrivilegeError("no"))
    assert await connection_cap.refusal(denied, BedrockLike()) == (None, None)
    connection_cap.invalidate()
    broken = ScriptedConn().on("FROM catalog.v_llm_connection_cap_v1", ConnectionError("db down"))
    assert await connection_cap.refusal(broken, BedrockLike()) == ("spend_unreadable", None)
    monkeypatch.setattr(spend_environment, "is_prod", lambda: True)
    assert await connection_cap.refusal(broken, BedrockLike()) == (None, None)


async def test_an_unreadable_crm_spend_fails_closed_in_dev() -> None:
    c = ScriptedConn().on("FROM catalog.v_llm_connection_cap_v1", cap_row("10", "1"))
    c.on("WHERE day >=", ConnectionError("db down"))
    assert await connection_cap.refusal(c, BedrockLike()) == ("spend_unreadable", None)


# ── the cost of a read (review S284): the CIS half is kept CACHE_TTL_SECONDS ────────────────────────


@pytest.mark.parametrize(
    "answer",
    [cap_row("10", "1"), None, asyncpg.UndefinedTableError("no view"), ConnectionError("db down")],
    ids=["row", "no-budget", "unpublished", "unreadable"],
)
async def test_the_view_is_read_once_per_window_whatever_its_outcome(
    monkeypatch: pytest.MonkeyPatch, answer: Any
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(connection_cap.time, "monotonic", lambda: clock[0])
    c = cap_conn(answer, "0.5")
    first = await connection_cap.refusal(c, BedrockLike())
    clock[0] += connection_cap.CACHE_TTL_SECONDS - 1
    assert await connection_cap.refusal(c, BedrockLike()) == first
    assert len(c.sql("FROM catalog.v_llm_connection_cap_v1")) == 1
    clock[0] += 2  # the window has passed: read again
    await connection_cap.refusal(c, BedrockLike())
    assert len(c.sql("FROM catalog.v_llm_connection_cap_v1")) == 2


async def test_the_crm_spend_is_read_on_every_call_so_a_crm_burst_stops_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connection_cap.time, "monotonic", lambda: 1000.0)
    under = cap_conn(cap_row("10", "8"), "1")
    assert await connection_cap.refusal(under, BedrockLike()) == (None, None)
    over = cap_conn(None, "2.5")  # the view row comes from the cache; only the CRM's month is read
    assert await connection_cap.refusal(over, BedrockLike()) == ("connection_budget_exhausted", "budget")
    assert over.sql("FROM catalog.v_llm_connection_cap_v1") == [] and over.sql("WHERE day >=") == [(MONTH,)]


@pytest.mark.parametrize("key", ["stub", "stub-echo", "bedrock_role", "", None])
async def test_a_provider_with_no_cis_connection_reads_nothing(key: Any) -> None:
    provider = FakeProvider()
    provider.connection_key = key  # type: ignore[assignment]
    c = cap_conn(cap_row("1", "5"))
    assert await connection_cap.refusal(c, provider) == (None, None)
    assert c.calls == []


async def test_the_state_adds_both_ledgers() -> None:
    s = connection_cap.CapState("acc2", Decimal("5"), Decimal("3"), Decimal("2"), hard_stop=True)
    assert s.spent_usd == Decimal("5") and s.stops
    assert not connection_cap.CapState("acc2", Decimal("5"), Decimal("3"), Decimal("1.99"), hard_stop=True).stops


# ── through the real gateway ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("stage", "rules", "run", "answer"), PATHS, ids=IDS)
async def test_over_the_cap_is_one_free_row_and_no_model_call(stage: str, rules: Any, run: Any, answer: str) -> None:
    c = rules(cap_conn(cap_row("10", "9"), "2", conn=gateway_conn()))
    provider = BedrockLike(answer)
    out = await run(c, gateway(provider))
    assert provider.prompts == []
    [row] = ledger(c)
    assert (row[0], row[8], row[9], row[6]) == (stage, "refused", "connection_budget_exhausted", Decimal(0))
    assert (out["outcome"], out["refusal_code"]) == ("refused", "connection_budget_exhausted")


@pytest.mark.parametrize(("stage", "rules", "run", "answer"), PATHS, ids=IDS)
async def test_under_the_cap_the_call_is_answered_and_logged(stage: str, rules: Any, run: Any, answer: str) -> None:
    c = rules(cap_conn(cap_row("10", "1"), "0.5", conn=gateway_conn()))
    provider = BedrockLike(answer)
    out = await run(c, gateway(provider))
    assert len(provider.prompts) == 1
    [row] = ledger(c)
    assert (row[0], row[8], row[6]) == (stage, "ok", Decimal("0.0012"))
    assert out["outcome"] == "ok"


@pytest.mark.parametrize(("stage", "rules", "run", "answer"), PATHS[:1], ids=IDS[:1])
async def test_the_app_profile_refusal_still_comes_first(stage: str, rules: Any, run: Any, answer: str) -> None:
    c = rules(cap_conn(cap_row("10", "20"), conn=gateway_conn(app_cap="1", day_spent="1")))
    out = await run(c, gateway(BedrockLike(answer)))
    assert out["refusal_code"] == "app_budget_exhausted"
    assert c.sql("FROM catalog.v_llm_connection_cap_v1") == []
