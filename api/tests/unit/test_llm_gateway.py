"""The LLM gateway order (D12, design v3 §4.5): scrub receipt -> budget -> ai_enabled -> call -> crm.llm_call."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.llm.gateway import Completion, LlmGateway, Scrubbed, StubProvider, StubScrubber, stub_gateway
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import system_ctx

pytestmark = pytest.mark.asyncio


def conn(enabled: bool = True, budget: str = "2", spent: str = "0") -> ScriptedConn:
    return (
        ScriptedConn()
        .on("FROM crm.tenant_settings", {"ai_enabled": enabled, "ai_budget_usd": Decimal(budget)})
        .on("sum(cost_usd_est)", Decimal(spent))
        .on("INSERT INTO crm.llm_call", "00000000-0000-4000-8000-000000000001")
    )


class BrokenScrubber:
    version = "f3-1"

    async def scrub(self, text: str) -> Scrubbed:
        raise RuntimeError("boom")


class RealScrubber:
    version = "f3-1"

    async def scrub(self, text: str) -> Scrubbed:
        return Scrubbed(text=text.replace("Hoa", "<PERSON_1>"), receipt={"scrubber": "f3-1", "PERSON": 1})


class Bedrock:
    name = "bedrock"
    model = "us.model-v1"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> Completion:
        if self.fail:
            raise TimeoutError("slow")
        self.prompts.append(prompt)
        return Completion("ok", 10, 5, Decimal("0.0012"))


async def test_refusals_in_order_and_every_one_is_logged() -> None:
    c = conn()
    ctx = system_ctx(c)
    broken = await LlmGateway(BrokenScrubber(), StubProvider()).call(ctx, stage="crm_x", role="writer", prompt="hi")
    assert broken.refusal_code == "scrub_failed"
    unscrubbed = await LlmGateway(StubScrubber(), Bedrock()).call(ctx, stage="crm_x", role="writer", prompt="hi")
    assert unscrubbed.refusal_code == "scrubber_unavailable"
    budget = await stub_gateway().call(system_ctx(conn(spent="2")), stage="crm_x", role="writer", prompt="hi")
    assert budget.refusal_code == "budget_exhausted" and budget.budget_used_pct == 100.0
    zero = await stub_gateway().call(system_ctx(conn(budget="0")), stage="crm_x", role="writer", prompt="hi")
    assert zero.refusal_code == "budget_exhausted"
    off = await stub_gateway().call(system_ctx(conn(enabled=False)), stage="crm_x", role="judge", prompt="hi")
    assert off.refusal_code == "ai_disabled"
    no_row = ScriptedConn().on("sum(cost_usd_est)", Decimal(0)).on("INSERT INTO crm.llm_call", "id")
    assert (await stub_gateway().call(system_ctx(no_row), stage="crm_x", role="writer", prompt="hi")).refusal_code == (
        "ai_disabled"
    )
    assert len(c.sql("INSERT INTO crm.llm_call")) == 2 and len(c.sql("INSERT INTO crm.llm_usage_daily")) == 2


async def test_ok_call_scrubs_logs_tokens_and_cost_never_text() -> None:
    c = conn(spent="1")
    provider = Bedrock()
    result = await LlmGateway(RealScrubber(), provider).call(
        system_ctx(c), stage="crm_reply", role="writer", prompt="Hi Hoa"
    )
    assert result.outcome == "ok" and result.text == "ok" and result.budget_used_pct == 50.0
    assert provider.prompts == ["Hi <PERSON_1>"]
    [args] = c.sql("INSERT INTO crm.llm_call")
    assert args[:9] == ("crm_reply", "writer", "bedrock", "us.model-v1", 10, 5, Decimal("0.0012"), args[7], "ok")
    assert "Hoa" not in repr(args) and '"PERSON": 1' in args[-1]
    [rollup] = c.sql("INSERT INTO crm.llm_usage_daily")
    assert rollup == ("crm_reply", "us.model-v1", 0, 10, 5, Decimal("0.0012"))


async def test_provider_error_is_logged_by_class() -> None:
    c = conn()
    result = await LlmGateway(RealScrubber(), Bedrock(fail=True)).call(
        system_ctx(c), stage="crm_x", role="validate", prompt="x"
    )
    assert (result.outcome, result.error_class) == ("error", "TimeoutError")
    assert c.sql("INSERT INTO crm.llm_call")[0][10] == "TimeoutError"


async def test_bad_stage_or_role_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        await stub_gateway().call(system_ctx(conn()), stage="cis_x", role="writer", prompt="x")
    with pytest.raises(ValueError):
        await stub_gateway().call(system_ctx(conn()), stage="crm_x", role="poet", prompt="x")


async def test_stub_pair_is_deterministic() -> None:
    scrubbed = await StubScrubber().scrub("a b c")
    assert scrubbed.receipt == {"scrubber": "stub-0", "entities": 0}
    done = await StubProvider().complete("a b c")
    assert (done.text, done.input_tokens, done.cost_usd) == ("[stub reply to 3 words]", 3, Decimal(0))
