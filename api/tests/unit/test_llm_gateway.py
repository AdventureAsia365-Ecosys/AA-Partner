"""The LLM gateway order (D12, design v3 §4.5): scrub receipt -> budget -> ai_enabled -> call -> crm.llm_call."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest

from app.llm import gateway as gateway_module
from app.llm.gateway import MANUAL_REPLY, Completion, LlmGateway, Scrubbed, StubProvider, StubScrubber, stub_gateway
from app.privacy.placeholder_store import SealedMap
from app.privacy.scrubber import VERSION as SCRUBBER_VERSION
from app.privacy.scrubber import Detection, KnownPerson, KnownValues, PiiScrubber
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

    async def scrub(self, text: str, known: KnownValues | None = None) -> Scrubbed:
        raise RuntimeError("boom")


class RealScrubber:
    """Counts only in the receipt, no map (nothing to restore)."""

    version = "f3-1"

    async def scrub(self, text: str, known: KnownValues | None = None) -> Scrubbed:
        return Scrubbed(text=text.replace("Hoa", "[name]"), receipt={"scrubber": "f3-1", "counts": {"PERSON": 1}})


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
    locked = conn(spent="1.99")
    await stub_gateway().call(system_ctx(locked), stage="crm_x", role="writer", prompt="hi")
    flat = [sql for sql, _ in locked.calls]
    lock_at = next(i for i, sql in enumerate(flat) if "pg_advisory_xact_lock" in sql)
    assert lock_at < next(i for i, sql in enumerate(flat) if "sum(cost_usd_est)" in sql)  # serialised per agency
    assert locked.sql("pg_advisory_xact_lock") == [(str(system_ctx().tenant_id),)]
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
    assert provider.prompts == ["Hi [name]"]
    [args] = c.sql("INSERT INTO crm.llm_call")
    assert args[:9] == ("crm_reply", "writer", "bedrock", "us.model-v1", 10, 5, Decimal("0.0012"), args[7], "ok")
    assert "Hoa" not in repr(args) and '"PERSON": 1' in args[11]
    assert result.placeholder_map_id is None and result.fallback is None
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


class SlowGuardrails:
    name = "bedrock_guardrails"

    async def detect(self, text: str) -> list[Detection]:
        await asyncio.sleep(1)
        return []


class NoFindings:
    name = "bedrock_guardrails"

    async def detect(self, text: str) -> list[Detection]:
        return []


async def test_r_guardrails_timeout_no_call_manual_reply_needed() -> None:
    """Card CRM-10 R test: a Guardrails timeout -> no call, manual_reply_needed (and the refusal is logged)."""
    c = conn()
    provider = Bedrock()
    gateway = LlmGateway(PiiScrubber(SlowGuardrails(), timeout_s=0.05), provider, keys=object())
    result = await gateway.call(system_ctx(c), stage="crm_reply", role="writer", prompt="Reply to Peter Lawson")
    assert (result.outcome, result.refusal_code, result.fallback) == ("refused", "scrub_timeout", MANUAL_REPLY)
    assert provider.prompts == []
    [args] = c.sql("INSERT INTO crm.llm_call")
    assert args[8:10] == ("refused", "scrub_timeout") and "Peter" not in repr(args)
    flat = [sql for sql, _ in c.calls]  # the budget step (and its lock) runs before the scrub
    assert next(i for i, q in enumerate(flat) if "pg_advisory_xact_lock" in q) < next(
        i for i, q in enumerate(flat) if "INSERT INTO crm.llm_call" in q
    )


async def test_e_scrubber_failure_means_no_call() -> None:
    provider = Bedrock()
    result = await LlmGateway(BrokenScrubber(), provider).call(
        system_ctx(conn()), stage="crm_x", role="writer", prompt="x"
    )
    assert result.refusal_code == "scrub_failed" and result.fallback == MANUAL_REPLY and provider.prompts == []


async def test_map_is_sealed_before_the_call_and_stored_after_the_log(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    map_id = "00000000-0000-4000-8000-0000000000aa"

    async def fake_seal(vault, placeholders):  # noqa: ANN001, ANN202
        order.append("seal")
        assert placeholders == {"<PERSON_1>": "Nguyễn Thị Hoa"}
        return SealedMap(uuid.UUID(map_id), b"sealed", 1)

    monkeypatch.setattr(gateway_module, "seal_map", fake_seal)
    c = conn()
    provider = Bedrock()
    original_complete = provider.complete

    async def complete(prompt: str) -> Completion:
        order.append("call")
        return await original_complete(prompt)

    provider.complete = complete  # type: ignore[method-assign]
    hoa = KnownValues((KnownPerson("traveller", display_name="Nguyễn Thị Hoa"),))
    gateway = LlmGateway(PiiScrubber(NoFindings()), provider, keys=object())
    result = await gateway.call(system_ctx(c), stage="crm_reply", role="writer", prompt="Hi Nguyen Thi Hoa", known=hoa)
    assert order == ["seal", "call"] and provider.prompts == ["Hi <PERSON_1>"]
    assert result.outcome == "ok" and result.placeholder_map_id == map_id
    [stored] = c.sql("INSERT INTO crm.placeholder_map")
    assert stored[1:] == (uuid.UUID("00000000-0000-4000-8000-000000000001"), b"sealed", 1, SCRUBBER_VERSION)
    flat = [sql for sql, _ in c.calls]
    assert next(i for i, q in enumerate(flat) if "INSERT INTO crm.llm_call" in q) < next(
        i for i, q in enumerate(flat) if "INSERT INTO crm.placeholder_map" in q
    )


async def test_a_map_that_cannot_be_sealed_stops_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    hoa = KnownValues((KnownPerson("traveller", display_name="Nguyễn Thị Hoa"),))
    provider = Bedrock()
    no_keys = await LlmGateway(PiiScrubber(NoFindings()), provider).call(
        system_ctx(conn()), stage="crm_reply", role="writer", prompt="Hi Nguyen Thi Hoa", known=hoa
    )
    assert no_keys.refusal_code == "scrub_failed" and provider.prompts == []

    async def broken_seal(vault, placeholders):  # noqa: ANN001, ANN202
        raise RuntimeError("kms down")

    monkeypatch.setattr(gateway_module, "seal_map", broken_seal)
    c = conn()
    kms_down = await LlmGateway(PiiScrubber(NoFindings()), provider, keys=object()).call(
        system_ctx(c), stage="crm_reply", role="writer", prompt="Hi Nguyen Thi Hoa", known=hoa
    )
    assert kms_down.refusal_code == "scrub_failed" and provider.prompts == []
    assert c.sql("INSERT INTO crm.placeholder_map") == []


async def test_disabled_ai_never_seals_a_map(monkeypatch: pytest.MonkeyPatch) -> None:
    async def must_not_seal(vault, placeholders):  # noqa: ANN001, ANN202
        raise AssertionError("sealed while AI is off")

    monkeypatch.setattr(gateway_module, "seal_map", must_not_seal)
    hoa = KnownValues((KnownPerson("traveller", display_name="Nguyễn Thị Hoa"),))
    off = await LlmGateway(PiiScrubber(NoFindings()), Bedrock(), keys=object()).call(
        system_ctx(conn(enabled=False)), stage="crm_reply", role="writer", prompt="Hi Nguyen Thi Hoa", known=hoa
    )
    assert off.refusal_code == "ai_disabled" and off.fallback == MANUAL_REPLY


async def test_a_map_that_cannot_be_stored_keeps_the_paid_call_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review AA-938: store_map failing after the paid call used to roll back the llm_call row (cost lost).
    async def fake_seal(vault, placeholders):  # noqa: ANN001, ANN202
        return SealedMap(uuid.UUID("00000000-0000-4000-8000-0000000000aa"), b"sealed", 1)

    async def broken_store(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("check violation with Nguyễn Thị Hoa")

    monkeypatch.setattr(gateway_module, "seal_map", fake_seal)
    monkeypatch.setattr(gateway_module, "store_map", broken_store)
    c = conn()
    provider = Bedrock()
    hoa = KnownValues((KnownPerson("traveller", display_name="Nguyễn Thị Hoa"),))
    result = await LlmGateway(PiiScrubber(NoFindings()), provider, keys=object()).call(
        system_ctx(c), stage="crm_reply", role="writer", prompt="Hi Nguyen Thi Hoa", known=hoa
    )
    assert provider.prompts == ["Hi <PERSON_1>"]
    assert (result.outcome, result.error_class, result.text, result.fallback) == (
        "error",
        "PlaceholderMapStoreError",
        None,
        MANUAL_REPLY,
    )
    [logged] = c.sql("INSERT INTO crm.llm_call")
    assert logged[6] == Decimal("0.0012") and logged[8] == "ok"  # the cost stays in the budget and the rollup
    assert len(c.sql("INSERT INTO crm.llm_usage_daily")) == 1


async def test_an_unstorable_map_or_version_is_refused_before_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    class HugeVault:
        def __init__(self, *_a: object) -> None:
            pass

        async def encrypt(self, *_a, **_k):  # noqa: ANN002, ANN003, ANN202
            return b"x" * 70_000

    monkeypatch.setattr(gateway_module, "Vault", HugeVault)
    hoa = KnownValues((KnownPerson("traveller", display_name="Nguyễn Thị Hoa"),))
    provider = Bedrock()
    c = conn()
    huge = await LlmGateway(PiiScrubber(NoFindings()), provider, keys=object()).call(
        system_ctx(c), stage="crm_reply", role="writer", prompt="Hi Nguyen Thi Hoa", known=hoa
    )
    assert huge.refusal_code == "scrub_failed" and provider.prompts == []

    class OddVersion(PiiScrubber):
        version = "F3 Upper"

    odd = await LlmGateway(OddVersion(NoFindings()), provider, keys=object()).call(
        system_ctx(conn()), stage="crm_reply", role="writer", prompt="Hi Nguyen Thi Hoa", known=hoa
    )
    assert odd.refusal_code == "scrub_failed" and provider.prompts == []
