"""The CRM LLM gateway (D12 change; design v3 §4.5 "Gateway order"; card CRM-08 "LLM gateway stub + crm.llm_call").

Every model call of the CRM goes through :meth:`LlmGateway.call`, inside the agency's context (``tenant_tx()`` or a
job's ``system_tx()``), in this order — and any step that fails stops the call (fail closed):

1. **scrub receipt**: the scrubber (F3, CRM-10) replaces personal data by placeholders and returns counts only. A
   scrub error refuses with ``scrub_failed``; the **stub** scrubber (no real scrubbing) is accepted only with the
   **stub** provider, so no real model ever receives unscrubbed text (``scrubber_unavailable``);
2. **budget**: this month's ``crm.llm_call`` cost of the agency against ``crm.tenant_settings.ai_budget_usd``
   (default 2 USD); at 100 % the call is refused with ``budget_exhausted`` (R5; the 75 / 90 % alerts are CRM-33a).
   The check runs under a transaction-scoped advisory lock per agency, held until the call is logged, so two calls
   at 99 % cannot both pass (calls of one agency are serialised; acceptable for the volumes of CRM-33a);
3. **ai_enabled** (default false): refused with ``ai_disabled``;
4. **the call**: an error is recorded with its class (never its message);
5. **the log**: one ``crm.llm_call`` row (no prompt, no output, no traveller token) and the platform rollup
   ``crm.llm_usage_daily`` the CIS cost page reads over ``/crm/ops/v1/llm/usage``. Refusals are logged too (cost 0).

Slice 1 ships the stub pair (:class:`StubScrubber`, :class:`StubProvider`: deterministic, no network, cost 0) so the
order, the refusals and the log are tested end to end before CRM-10 (scrubber) and CRM-33a (Bedrock) plug in.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from app.tenancy import AnyContext

STAGE_PREFIX = "crm_"
ROLES = frozenset({"writer", "judge", "validate"})
DEFAULT_BUDGET_USD = Decimal("2")
STUB_PREFIX = "stub"

BUDGET_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('crm_llm_budget:' || $1::text, 0))"
MONTH_SPEND_SQL = """
SELECT coalesce(sum(cost_usd_est), 0) FROM crm.llm_call
 WHERE at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
"""
SETTINGS_SQL = "SELECT ai_enabled, ai_budget_usd FROM crm.tenant_settings WHERE deleted_at IS NULL"
INSERT_CALL_SQL = """
INSERT INTO crm.llm_call (stage, role, provider, model, input_tokens, output_tokens, cost_usd_est, latency_ms, outcome,
                          refusal_code, error_class, scrub_receipt)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
RETURNING llm_call_id
"""
ROLLUP_SQL = """
INSERT INTO crm.llm_usage_daily AS u (day, stage, model, calls, failed_calls, input_tokens, output_tokens, cost_usd)
VALUES ((now() AT TIME ZONE 'UTC')::date, $1, $2, 1, $3, $4, $5, $6)
ON CONFLICT (day, stage, model) DO UPDATE
   SET calls = u.calls + 1, failed_calls = u.failed_calls + EXCLUDED.failed_calls,
       input_tokens = u.input_tokens + EXCLUDED.input_tokens, output_tokens = u.output_tokens + EXCLUDED.output_tokens,
       cost_usd = u.cost_usd + EXCLUDED.cost_usd, updated_at = now()
"""


@dataclass(frozen=True)
class Scrubbed:
    text: str
    receipt: dict[str, Any]  # counts and versions only


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class Scrubber(Protocol):
    version: str

    async def scrub(self, text: str) -> Scrubbed: ...


class Provider(Protocol):
    name: str
    model: str

    async def complete(self, prompt: str) -> Completion: ...


class StubScrubber:
    """No scrubbing: accepted only together with :class:`StubProvider` (step 1)."""

    version = "stub-0"

    async def scrub(self, text: str) -> Scrubbed:
        return Scrubbed(text=text, receipt={"scrubber": self.version, "entities": 0})


class StubProvider:
    """A deterministic model: no network, cost 0, token counts from the text length."""

    name = "stub"
    model = "stub-echo-1"

    async def complete(self, prompt: str) -> Completion:
        words = len(prompt.split())
        return Completion(
            text=f"[stub reply to {words} words]", input_tokens=words, output_tokens=6, cost_usd=Decimal(0)
        )


@dataclass(frozen=True)
class GatewayResult:
    outcome: str  # ok | refused | error
    text: str | None = None
    refusal_code: str | None = None
    error_class: str | None = None
    llm_call_id: str | None = None
    budget_used_pct: float | None = None
    receipt: dict[str, Any] = field(default_factory=dict)


class LlmGateway:
    def __init__(self, scrubber: Scrubber, provider: Provider) -> None:
        self.scrubber = scrubber
        self.provider = provider

    async def call(self, ctx: AnyContext, *, stage: str, role: str, prompt: str) -> GatewayResult:
        if not stage.startswith(STAGE_PREFIX) or role not in ROLES:
            raise ValueError("stage must be crm_* and role writer / judge / validate")
        # 1. scrub receipt
        try:
            scrubbed = await self.scrubber.scrub(prompt)
        except Exception:  # noqa: BLE001 — any scrubber failure stops the call
            return await self._log(ctx, stage, role, "refused", refusal="scrub_failed")
        receipt = scrubbed.receipt
        if self.scrubber.version.startswith(STUB_PREFIX) and self.provider.name != STUB_PREFIX:
            return await self._log(ctx, stage, role, "refused", refusal="scrubber_unavailable", receipt=receipt)
        # 2. budget, 3. ai_enabled (the per-agency lock holds until this transaction ends, after the log row)
        await ctx.conn.execute(BUDGET_LOCK_SQL, str(ctx.tenant_id))
        settings = await ctx.conn.fetchrow(SETTINGS_SQL)
        enabled = bool(settings["ai_enabled"]) if settings else False
        budget = Decimal(settings["ai_budget_usd"]) if settings else DEFAULT_BUDGET_USD
        spent = Decimal(await ctx.conn.fetchval(MONTH_SPEND_SQL))
        pct = float(spent / budget * 100) if budget > 0 else 100.0
        if spent >= budget:
            return await self._log(ctx, stage, role, "refused", refusal="budget_exhausted", receipt=receipt, pct=pct)
        if not enabled:
            return await self._log(ctx, stage, role, "refused", refusal="ai_disabled", receipt=receipt, pct=pct)
        # 4. the call
        started = time.monotonic()
        try:
            done = await self.provider.complete(scrubbed.text)
        except Exception as exc:  # noqa: BLE001 — recorded by class only
            latency = int((time.monotonic() - started) * 1000)
            return await self._log(
                ctx, stage, role, "error", error=type(exc).__name__, receipt=receipt, pct=pct, latency=latency
            )
        latency = int((time.monotonic() - started) * 1000)
        result = await self._log(ctx, stage, role, "ok", receipt=receipt, pct=pct, latency=latency, completion=done)
        return GatewayResult(**{**result.__dict__, "text": done.text})

    async def _log(
        self,
        ctx: AnyContext,
        stage: str,
        role: str,
        outcome: str,
        *,
        refusal: str | None = None,
        error: str | None = None,
        receipt: dict[str, Any] | None = None,
        pct: float | None = None,
        latency: int = 0,
        completion: Completion | None = None,
    ) -> GatewayResult:
        receipt = receipt or {}
        tokens_in = completion.input_tokens if completion else 0
        tokens_out = completion.output_tokens if completion else 0
        cost = completion.cost_usd if completion else Decimal(0)
        call_id = await ctx.conn.fetchval(
            INSERT_CALL_SQL,
            stage,
            role,
            self.provider.name,
            self.provider.model,
            tokens_in,
            tokens_out,
            cost,
            latency,
            outcome,
            refusal,
            error,
            json.dumps(receipt),
        )
        await ctx.conn.execute(
            ROLLUP_SQL, stage, self.provider.model, 0 if outcome == "ok" else 1, tokens_in, tokens_out, cost
        )
        return GatewayResult(
            outcome=outcome,
            refusal_code=refusal,
            error_class=error,
            llm_call_id=str(call_id),
            budget_used_pct=round(pct, 1) if pct is not None else None,
            receipt=receipt,
        )


def stub_gateway() -> LlmGateway:
    return LlmGateway(StubScrubber(), StubProvider())
