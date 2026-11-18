"""AA-1113 (root ADR 0009 decision 6; S284 redesign, crm-077): the CRM's own monthly cap per model connection
(:mod:`app.llm.connection_cap`).

- The rule: the CRM's month spend (``crm.llm_usage_daily`` since the first day of the UTC month) against the CRM's
  own cap on the connection (``crm.ai.connection_monthly_cap_usd``); at or above it the dev spend rules refuse
  ``connection_budget_exhausted``, the prod rules only log; no entry = no cap and nothing read; an unreadable setting
  or spend refuses ``spend_unreadable`` in dev only; a provider with no CIS connection (the stub, an unmapped role
  connection) reads nothing. The CRM never reads a CIS view.
- Through the real gateway, each of the three CRM AI paths: over the cap = one 0 USD refused row and the provider is
  never called; under it = the answered call and its row, as before.
Fakes only (no database, no AWS, no paid call); invented names and amounts.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from app import platform_settings as ps
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

# The setting and its reader ship with the crm-077 migration PR (MIG-4); this code PR runs after main has them.
pytest.importorskip("app.llm.connection_share", reason="ships with the crm-077 migration PR")
CAP_KEY = "crm.ai.connection_monthly_cap_usd"

pytestmark = pytest.mark.asyncio

SPEND = "FROM crm.llm_usage_daily WHERE day >="


class BedrockLike(FakeProvider):
    """The fake model on the task role's own Bedrock connection (the CRM's live route)."""

    connection_key = "bedrock"


@pytest.fixture
def caps(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The value in force of crm.ai.connection_monthly_cap_usd (empty = the compiled default)."""
    value: dict[str, Any] = {}
    real = ps.get

    def fake(key: str, tenant_id: object = None) -> Any:
        return value if key == CAP_KEY else real(key, tenant_id)

    monkeypatch.setattr(ps, "get", fake)
    return value


@pytest.fixture
def prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spend_environment, "is_prod", lambda: True)


def spend_conn(crm: Any = "0", conn: ScriptedConn | None = None) -> ScriptedConn:
    c = conn or ScriptedConn()
    return c.on(SPEND, crm if isinstance(crm, BaseException) else Decimal(crm))


# ── the rule ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cap", "crm", "expected"),
    [
        (10, "11", ("connection_budget_exhausted", "budget")),  # over
        (10, "10", ("connection_budget_exhausted", "budget")),  # exactly at the cap
        (0, "0", ("connection_budget_exhausted", "budget")),  # a 0 USD cap: the CRM does not use this connection
        (10, "9.99", (None, None)),  # under
    ],
    ids=["over", "at-cap", "zero-cap", "under"],
)
async def test_the_rule_in_dev(caps: dict[str, Any], cap: int, crm: str, expected: tuple[Any, Any]) -> None:
    caps["acc2"] = cap
    c = spend_conn(crm)
    assert await connection_cap.refusal(c, BedrockLike()) == expected
    assert c.sql(SPEND) == [(connection_cap.month_start(),)]


async def test_no_entry_for_the_connection_is_no_cap_and_reads_nothing(caps: dict[str, Any]) -> None:
    caps["zz-other"] = 1
    c = spend_conn("500")
    assert await connection_cap.refusal(c, BedrockLike()) == (None, None)
    assert c.calls == []


async def test_the_compiled_default_caps_nothing() -> None:
    c = spend_conn("500")
    assert await connection_cap.refusal(c, BedrockLike()) == (None, None)
    assert c.calls == []


async def test_prod_never_refuses_for_the_cap(caps: dict[str, Any], prod: None) -> None:
    caps["acc2"] = 10
    assert await connection_cap.refusal(spend_conn("20"), BedrockLike()) == (None, None)


async def test_an_unreadable_setting_fails_closed_in_dev_only(
    caps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    caps["acc2"] = "lots"
    c = spend_conn("0")
    assert await connection_cap.refusal(c, BedrockLike()) == ("spend_unreadable", None)
    assert c.calls == []
    monkeypatch.setattr(spend_environment, "is_prod", lambda: True)
    assert await connection_cap.refusal(c, BedrockLike()) == (None, None)


async def test_an_unreadable_crm_spend_fails_closed_in_dev_only(
    caps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    caps["acc2"] = 10
    broken = spend_conn(ConnectionError("db down"))
    assert await connection_cap.refusal(broken, BedrockLike()) == ("spend_unreadable", None)
    monkeypatch.setattr(spend_environment, "is_prod", lambda: True)
    assert await connection_cap.refusal(broken, BedrockLike()) == (None, None)


@pytest.mark.parametrize("key", ["stub", "stub-echo", "bedrock_role", "", None])
async def test_a_provider_with_no_cis_connection_reads_nothing(caps: dict[str, Any], key: Any) -> None:
    caps["acc2"] = 0
    provider = FakeProvider()
    provider.connection_key = key  # type: ignore[assignment]
    c = spend_conn("5")
    assert await connection_cap.refusal(c, provider) == (None, None)
    assert c.calls == []


async def test_the_crm_never_reads_a_cis_view(caps: dict[str, Any]) -> None:
    caps["acc2"] = 10
    c = spend_conn("1")
    await connection_cap.refusal(c, BedrockLike())
    assert all("catalog." not in sql and "shared." not in sql for sql, *_ in c.calls)


async def test_the_month_starts_on_the_first_utc_day() -> None:
    late = dt.datetime(2026, 11, 30, 23, 30, tzinfo=dt.timezone(dt.timedelta(hours=-5)))  # 1 Dec 04:30 UTC
    assert connection_cap.month_start(late) == dt.date(2026, 12, 1)
    assert connection_cap.month_start(dt.datetime(2026, 11, 18, 3, 0, tzinfo=dt.UTC)) == dt.date(2026, 11, 1)


async def test_the_state_stops_at_the_cap() -> None:
    assert connection_cap.CapState("acc2", Decimal("5"), Decimal("5")).stops
    assert not connection_cap.CapState("acc2", Decimal("5"), Decimal("4.99")).stops


# ── through the real gateway ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("stage", "rules", "run", "answer"), PATHS, ids=IDS)
async def test_over_the_cap_is_one_free_row_and_no_model_call(
    caps: dict[str, Any], stage: str, rules: Any, run: Any, answer: str
) -> None:
    caps["acc2"] = 10
    c = rules(spend_conn("11", conn=gateway_conn()))
    provider = BedrockLike(answer)
    out = await run(c, gateway(provider))
    assert provider.prompts == []
    [row] = ledger(c)
    assert (row[0], row[8], row[9], row[6]) == (stage, "refused", "connection_budget_exhausted", Decimal(0))
    assert (out["outcome"], out["refusal_code"]) == ("refused", "connection_budget_exhausted")


@pytest.mark.parametrize(("stage", "rules", "run", "answer"), PATHS, ids=IDS)
async def test_under_the_cap_the_call_is_answered_and_logged(
    caps: dict[str, Any], stage: str, rules: Any, run: Any, answer: str
) -> None:
    caps["acc2"] = 10
    c = rules(spend_conn("0.5", conn=gateway_conn()))
    provider = BedrockLike(answer)
    out = await run(c, gateway(provider))
    assert len(provider.prompts) == 1
    [row] = ledger(c)
    assert (row[0], row[8], row[6]) == (stage, "ok", Decimal("0.0012"))
    assert out["outcome"] == "ok"


@pytest.mark.parametrize(("stage", "rules", "run", "answer"), PATHS[:1], ids=IDS[:1])
async def test_the_app_profile_refusal_still_comes_first(
    caps: dict[str, Any], stage: str, rules: Any, run: Any, answer: str
) -> None:
    caps["acc2"] = 0
    c = rules(spend_conn("20", conn=gateway_conn(app_cap="1", day_spent="1")))
    out = await run(c, gateway(BedrockLike(answer)))
    assert out["refusal_code"] == "app_budget_exhausted"
    assert c.sql(SPEND) == []
