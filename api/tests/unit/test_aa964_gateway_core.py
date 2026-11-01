"""AA-964, S263 gateway plan F1 + F2: the CRM gateway on the shared core (``app.aa_llm``) with stage profiles.

F1: a failed attempt's class and status come from ``errors.classify`` / ``ledger.failed_outcome``; a reply cut at its
token limit is ``truncated`` (never ``ok``); a billed answer without text is ``billed_unusable``; the spend decision is
``budget.decide`` (an unreadable month refuses ``spend_unreadable``); Bedrock prices come from ``pricing.quote``
(catalog, override, code table, estimate); the family is the maker.
F2: the stage profile in ``crm`` (crm-040): no profile / unreadable / another role / kill switch off refuse with one
0 USD row before any scrub or call; the route runs entry by entry (one row per attempt), at most ``max_attempts``,
no new attempt after the deadline or once a billed failure used the budget up.

Fixtures use invented makers and model names only (no key, no AWS, no paid call).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.llm import gateway as gw
from app.llm.gateway import (
    MANUAL_REPLY,
    STUB_FAIL_FIRST,
    STUB_TRUNCATE,
    Completion,
    LlmGateway,
    StubProvider,
    StubScrubber,
)
from app.llm.profiles import (
    PROFILE_SQL,
    DbProfileSource,
    RouteEntry,
    StageProfile,
    family_of,
    fixed_profile,
    profile_from_rows,
)
from app.llm.providers.bedrock import BedrockConverseProvider, ProviderCallError
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import system_ctx

pytestmark = pytest.mark.asyncio

STAGE = "crm_draft_reply"
SMALL = "us.maker.small-v1:0"
LARGE = "us.maker.large-v1:0"


def rows(
    *,
    active: bool = True,
    attempts: int = 2,
    deadline: int = 30,
    role: str = "writer",
    route: tuple[tuple[int, str], ...] = ((0, SMALL), (1, LARGE)),
    family: str | None = "maker",
) -> list[dict[str, Any]]:
    head = {
        "stage": STAGE,
        "role": role,
        "is_active": active,
        "max_attempts": attempts,
        "route_deadline_s": deadline,
        "degrade_mode": "manual_reply_needed",
    }
    if not route:
        return [{**head, "position": None, "model": None, "family": None, "price_in_per_mtok": None,
                 "price_out_per_mtok": None}]  # fmt: skip
    return [
        {
            **head,
            "position": position,
            "model": model,
            "family": family,
            "price_in_per_mtok": Decimal("1"),
            "price_out_per_mtok": Decimal("5"),
        }
        for position, model in route
    ]


def conn(profile: Any = None, spent: Any = "0", enabled: bool = True) -> ScriptedConn:
    spend = spent if callable(spent) else (lambda *_: Decimal(spent))
    return (
        ScriptedConn()
        .on("FROM crm.tenant_settings", {"ai_enabled": enabled, "ai_budget_usd": Decimal("2")})
        .on("sum(cost_usd_est)", spend)
        .on("INSERT INTO crm.llm_call", "00000000-0000-4000-8000-000000000001")
        .on("FROM crm.llm_stage_profile", rows() if profile is None else profile)
    )


def routed(provider: Any = None, clock: Any = None) -> LlmGateway:
    return LlmGateway(StubScrubber(), provider or StubProvider(), profiles=DbProfileSource(), clock=clock)


def ledger_rows(c: ScriptedConn) -> list[tuple[Any, ...]]:
    return c.sql("INSERT INTO crm.llm_call")


class Scripted:
    """A provider playing route entries from a script per position: a Completion or an exception."""

    name = "stub"
    connection_key = "stub"

    def __init__(self, answers: dict[int, Any], model: str = SMALL, family: str | None = "maker", pos: int = 0):
        self.answers, self.model, self.family, self.position = answers, model, family, pos
        self.calls: list[int] = []

    def for_route(self, entry: RouteEntry) -> Scripted:
        child = Scripted(self.answers, entry.model, entry.family, entry.position)
        child.calls = self.calls
        return child

    async def complete(self, prompt: str, *, system: str | None = None) -> Completion:
        self.calls.append(self.position)
        answer = self.answers[self.position]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def done(cost: str = "0", truncated: bool = False, stop: str | None = None) -> Completion:
    return Completion("draft", 10, 5, Decimal(cost), truncated=truncated, stop_reason=stop)


# ── profiles ──────────────────────────────────────────────────────────────────────────────────────


async def test_family_is_the_maker_of_the_wire_id() -> None:
    assert family_of("us.maker.small-v1:0") == "maker"
    assert family_of("maker.small-v1:0") == "maker"
    assert family_of("arn:aws:bedrock:region::inference-profile/global.maker.small-v1:0") == "maker"
    assert family_of("bare-model") is None and family_of("") is None and family_of(None) is None
    assert family_of("us.Bad Maker!.x") is None


async def test_profile_rows_become_a_route_and_the_family_falls_back_to_the_wire_id() -> None:
    profile = profile_from_rows(rows(family=None))
    assert profile is not None and [e.model for e in profile.route] == [SMALL, LARGE]
    assert {e.family for e in profile.route} == {"maker"} and profile.route[0].catalog_prices == (1, 5)
    empty = profile_from_rows(rows(route=()))
    assert empty is not None and empty.route == () and empty.attempts == ()
    assert profile_from_rows([]) is None
    capped = StageProfile(STAGE, "writer", True, 9, 30, tuple(RouteEntry(i, f"m{i}") for i in range(8)))
    assert len(capped.attempts) == 6
    one = fixed_profile(STAGE, "judge", "stub-echo-1", "stub")
    assert (one.max_attempts, one.route[0].model, one.is_active) == (1, "stub-echo-1", True)
    assert RouteEntry(0, SMALL).catalog_prices is None


async def test_the_source_reads_the_profile_in_the_call_context() -> None:
    c = conn()
    profile = await DbProfileSource().load_route(system_ctx(c), STAGE)
    assert profile is not None and profile.max_attempts == 2
    assert c.sql("FROM crm.llm_stage_profile") == [(STAGE,)]
    assert "r.is_enabled" in PROFILE_SQL  # a switched-off entry never reaches the route


# ── F2: the kill switch and the other profile refusals ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("profile", "code"),
    [
        (rows(active=False), "stage_disabled"),
        ([], "stage_unrouted"),
        (rows(route=()), "stage_unrouted"),
        (rows(role="judge"), "profile_mismatch"),
        (RuntimeError("relation does not exist"), "profile_unavailable"),
    ],
)
async def test_a_stage_without_a_usable_profile_is_refused_at_zero_cost_first(profile: Any, code: str) -> None:
    c = conn(profile)
    provider = Scripted({0: done()})
    result = await routed(provider).call(system_ctx(c), stage=STAGE, role="writer", prompt="hello")
    assert (result.outcome, result.refusal_code, result.fallback) == ("refused", code, MANUAL_REPLY)
    [row] = ledger_rows(c)
    assert (row[6], row[8], row[9]) == (Decimal(0), "refused", code)
    assert provider.calls == [] and c.sql("pg_advisory_xact_lock") == []  # no budget lock, no scrub, no call


async def test_switching_the_stage_back_on_routes_the_stub_as_the_profile_says() -> None:
    c = conn()
    result = await routed().call(system_ctx(c), stage=STAGE, role="writer", prompt="hello there")
    assert result.outcome == "ok" and result.text == "[stub reply to 2 words]"
    [row] = ledger_rows(c)
    # provider, model, cost, outcome, attempt, connection, wire id, family
    assert (row[2], row[3], row[6], row[8], row[12], row[15], row[16], row[17]) == (
        "stub",
        SMALL,
        Decimal(0),
        "ok",
        1,
        "stub",
        SMALL,
        "maker",
    )


# ── F2: the route ─────────────────────────────────────────────────────────────────────────────────


async def test_a_throttled_first_entry_falls_over_to_the_second_one_row_each() -> None:
    c = conn()
    result = await routed().call(system_ctx(c), stage=STAGE, role="writer", prompt=f"hi {STUB_FAIL_FIRST}")
    assert result.outcome == "ok"
    first, second = ledger_rows(c)
    assert (first[3], first[8], first[9], first[10], first[12]) == (SMALL, "refused", "throttled", "throttled", 1)
    assert (second[3], second[8], second[12]) == (LARGE, "ok", 2)
    assert first[6] == Decimal(0)


async def test_every_entry_refused_at_account_level_is_a_refused_result() -> None:
    c = conn()
    denied = ProviderCallError("AccessDeniedException", llm_error_class="access_denied")
    provider = Scripted({0: denied, 1: ProviderCallError("AccessDeniedException")})
    result = await routed(provider).call(system_ctx(c), stage=STAGE, role="writer", prompt="x")
    assert (result.outcome, result.refusal_code, result.error_class) == ("refused", "access_denied", "access_denied")
    assert [r[8] for r in ledger_rows(c)] == ["refused", "refused"] and provider.calls == [0, 1]


async def test_a_mixed_failure_is_an_error_with_the_last_class() -> None:
    c = conn()
    provider = Scripted({0: ProviderCallError("ThrottlingException"), 1: TimeoutError("slow")})
    result = await routed(provider).call(system_ctx(c), stage=STAGE, role="writer", prompt="x")
    assert (result.outcome, result.refusal_code, result.error_class) == ("error", None, "other")
    assert [(r[8], r[10]) for r in ledger_rows(c)] == [("refused", "throttled"), ("error", "other")]


async def test_max_attempts_caps_the_route() -> None:
    c = conn(rows(attempts=1))
    provider = Scripted({0: TimeoutError("slow"), 1: done()})
    result = await routed(provider).call(system_ctx(c), stage=STAGE, role="writer", prompt="x")
    assert result.outcome == "error" and provider.calls == [0] and len(ledger_rows(c)) == 1


async def test_no_new_attempt_starts_after_the_route_deadline() -> None:
    ticks = iter([0.0, 0.0, 31.0, 31.0, 31.0])
    c = conn(rows(deadline=30))
    provider = Scripted({0: TimeoutError("slow"), 1: done()})
    result = await routed(provider, clock=lambda: next(ticks)).call(
        system_ctx(c), stage=STAGE, role="writer", prompt="x"
    )
    assert (result.outcome, result.error_class) == ("error", "deadline") and provider.calls == [0]


# ── F1: truncated, billed_unusable, classify ──────────────────────────────────────────────────────


async def test_a_truncated_reply_is_never_ok_and_the_next_entry_is_tried() -> None:
    c = conn()
    provider = Scripted({0: done("0.01", stop="max_tokens"), 1: done("0.02")})
    result = await routed(provider).call(system_ctx(c), stage=STAGE, role="writer", prompt="x")
    assert result.outcome == "ok" and result.truncated is False
    first, second = ledger_rows(c)
    assert (first[6], first[8], first[9], first[10]) == (Decimal("0.01"), "truncated", None, "truncated")
    assert second[8] == "ok"
    rollups = c.sql("INSERT INTO crm.llm_usage_daily")
    assert [r[2] for r in rollups] == [1, 0]  # the truncated attempt counts as failed


async def test_the_stub_truncates_on_request_and_the_draft_falls_back() -> None:
    c = conn()
    result = await routed().call(system_ctx(c), stage=STAGE, role="writer", prompt=f"x {STUB_TRUNCATE}")
    assert (result.outcome, result.error_class, result.truncated, result.text) == ("error", "truncated", True, None)
    assert result.fallback == MANUAL_REPLY
    assert [r[8] for r in ledger_rows(c)] == ["truncated", "truncated"]


async def test_a_billed_failure_that_uses_the_budget_up_ends_the_route() -> None:
    spends = iter([Decimal("1.99"), Decimal("2")])
    c = conn(spent=lambda *_: next(spends))
    billed = ProviderCallError("EmptyOutput", billed=done("0.02"))
    provider = Scripted({0: billed, 1: done()})
    result = await routed(provider).call(system_ctx(c), stage=STAGE, role="writer", prompt="x")
    assert (result.outcome, result.error_class) == ("error", "budget") and provider.calls == [0]
    [row] = ledger_rows(c)
    assert (row[6], row[8], row[10]) == (Decimal("0.02"), "billed_unusable", "other")
    assert result.budget_alert == 100


async def test_a_truncated_reply_that_uses_the_budget_up_ends_the_route() -> None:
    c = conn(spent="1.99")
    provider = Scripted({0: done("0.02", truncated=True), 1: done()})
    result = await routed(provider).call(system_ctx(c), stage=STAGE, role="writer", prompt="x")
    assert (result.outcome, result.error_class, result.truncated) == ("error", "budget", True)
    assert provider.calls == [0]


# ── F1: budget.decide ─────────────────────────────────────────────────────────────────────────────


async def test_an_unreadable_month_refuses_before_the_scrub() -> None:
    c = conn(spent=RuntimeError("canceling statement due to statement timeout"))
    provider = Scripted({0: done()})
    result = await routed(provider).call(system_ctx(c), stage=STAGE, role="writer", prompt="x")
    assert (result.outcome, result.refusal_code) == ("refused", "spend_unreadable") and provider.calls == []
    [row] = ledger_rows(c)
    assert (row[6], row[9]) == (Decimal(0), "spend_unreadable")


async def test_an_unreadable_month_after_the_call_skips_the_alert_only() -> None:
    spends = iter([Decimal("0.5"), RuntimeError("gone")])
    c = conn(spent=lambda *_: next(spends))
    result = await routed().call(system_ctx(c), stage=STAGE, role="writer", prompt="x")
    assert result.outcome == "ok" and result.budget_alert is None


# ── F1: Bedrock prices through the shared core ────────────────────────────────────────────────────


def bedrock(**kwargs: Any) -> BedrockConverseProvider:
    return BedrockConverseProvider(SMALL, client=object(), **kwargs)


def answer(stop: str = "end_turn", text: str = "Hi") -> dict[str, Any]:
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": stop,
        "usage": {"inputTokens": 1000, "outputTokens": 200},
    }


async def test_bedrock_prices_catalog_override_code_table_and_estimate() -> None:
    catalog = bedrock(catalog=(Decimal("2"), Decimal("10"))).parse(answer())
    assert (catalog.cost_usd, catalog.pricing_mode) == (Decimal("0.004"), "catalog")  # 1000 x 2 + 200 x 10 per M
    override = bedrock(
        catalog=(Decimal("2"), Decimal("10")), usd_per_mtok_in=Decimal("1"), usd_per_mtok_out=Decimal("5")
    ).parse(answer())
    assert (override.cost_usd, override.pricing_mode) == (Decimal("0.002"), "override")
    known = BedrockConverseProvider("us.anthropic.claude-haiku-4-5-20251001-v1:0", client=object()).parse(answer())
    assert known.pricing_mode == "code_table" and known.cost_usd == Decimal("0.002")
    unknown = bedrock().parse(answer())
    assert unknown.pricing_mode == "estimate" and unknown.cost_usd > 0  # never 0


async def test_bedrock_marks_a_cut_reply_and_keeps_the_stop_reason() -> None:
    cut = bedrock().parse(answer(stop="max_tokens"))
    assert cut.truncated and cut.stop_reason == "max_tokens"
    odd = bedrock().parse(answer(stop="weird stop!"))
    assert odd.stop_reason is None and not odd.truncated


async def test_bedrock_route_entries_share_the_client_and_get_their_own_price() -> None:
    parent = BedrockConverseProvider(
        SMALL, usd_per_mtok_in=Decimal("1"), usd_per_mtok_out=Decimal("5"), client="the-client"
    )
    same = parent.for_route(RouteEntry(0, SMALL, "maker", Decimal("2"), Decimal("10")))
    other = parent.for_route(RouteEntry(1, LARGE, "maker", Decimal("3"), Decimal("15")))
    assert same.usd_in == Decimal("1") and other.usd_in is None and other.catalog == (Decimal("3"), Decimal("15"))
    assert other._runtime() == "the-client" and other.family == "maker"
    assert parent.for_route(RouteEntry(1, LARGE, "maker", Decimal("3"), Decimal("15"))) is other  # cached
    assert other.for_route(RouteEntry(2, "us.maker.tiny-v1:0"))._runtime() == "the-client"  # one client for all


async def test_a_bedrock_error_carries_the_shared_class_never_the_message() -> None:
    class Denied(Exception):
        def __init__(self) -> None:
            super().__init__("Access to Bedrock models is not allowed for this account (secret detail)")
            self.response = {"Error": {"Code": "ValidationException"}}

    class Client:
        def converse(self, **_: Any) -> Any:
            raise Denied()

    with pytest.raises(ProviderCallError) as caught:
        await BedrockConverseProvider(SMALL, client=Client()).complete("x")
    assert caught.value.error_class == "ValidationException" and caught.value.llm_error_class == "access_denied"
    assert "secret" not in str(caught.value)


async def test_the_guardrail_row_family_is_its_maker() -> None:
    assert gw.GUARD_FAMILY == "amazon"
