"""The CRM LLM gateway (D12 change; design v3 §4.5 "Gateway order"; card CRM-08 "LLM gateway stub + crm.llm_call").

Every model call of the CRM goes through :meth:`LlmGateway.call`, inside the agency's context (``tenant_tx()`` or a
job's ``system_tx()``), in this order — and any step that fails stops the call (fail closed):

1. **scrub receipt**: the F3 scrubber (:class:`app.privacy.scrubber.PiiScrubber`, AA-938) replaces personal data
   by numbered placeholders (known values of the trip, regex, Bedrock Guardrails) and returns counts only. Any scrub
   error refuses with ``scrub_failed``, a detector timeout with ``scrub_timeout`` (card CRM-10 R test: no call,
   ``manual_reply_needed``); the **stub** scrubber (no real scrubbing) is accepted only with the **stub** provider,
   so no real model ever receives unscrubbed text (``scrubber_unavailable``);
2. **budget**: this month's ``crm.llm_call`` cost of the agency against ``crm.tenant_settings.ai_budget_usd``
   (default 2 USD); at 100 % the call is refused with ``budget_exhausted`` (R5; the 75 / 90 % alerts are CRM-33a).
   The check runs under a transaction-scoped advisory lock per agency, held until the call is logged, so two calls
   at 99 % cannot both pass (calls of one agency are serialised; acceptable for the volumes of CRM-33a);
3. **ai_enabled** (default false): refused with ``ai_disabled``; then, when the scrub replaced anything, the
   placeholder map is sealed under the tenant ``scrub`` DEK **before** the call (a key failure refuses with
   ``scrub_failed``; without a key provider the call is refused the same way: an unrestorable output is useless);
4. **the call**: an error is recorded with its class (never its message);
5. **the log**: one ``crm.llm_call`` row (no prompt, no output, no traveller token) and the platform rollup
   ``crm.llm_usage_daily`` the CIS cost page reads over ``/crm/ops/v1/llm/usage``. Refusals are logged too (cost 0);
6. **the map**: on success, the sealed map is stored against the call (``crm.placeholder_map``, 24 h) in a savepoint;
   if that fails the paid call stays logged and the result is an ``error`` (``PlaceholderMapStoreError``, no text,
   ``manual_reply_needed``). The output keeps its placeholders (``GatewayResult.text``); only
   :func:`app.privacy.placeholder_store.restore_placeholders` in an agency member's session puts the values back.

Every non-``ok`` result carries ``fallback = "manual_reply_needed"``: the draft UI (CRM-33a) shows "reply by hand".

AA-964 (card CRM-33a; root ADR 0008a §5-6, root ADR 0009 §8):

- **Per-agency rate cap** first (``RATE_LIMIT_AI_PER_MINUTE``, :mod:`app.ratelimit`): over it the call is refused
  ``rate_limited`` before the scrub, so a burst pays no guardrail units. Concurrency is already 1 per agency (the
  budget lock).
- **Ledger v2 row** (crm-032): ``app = 'crm'`` (column default), attempt 1, the provider's request id, connection,
  wire id, family, cache tokens, pricing mode, ``prompt_hash`` (sha256 of the scrubbed prompt as sent, never the
  prompt) and the caller's ``prompt_version`` / ``job_id``.
- **Guardrail rows**: every ``ApplyGuardrail`` attempt of the scrub (:func:`app.privacy.guardrails.collect_guard_calls`)
  is its own row, stage ``crm_scrub``, role ``guard``, ``units`` text units and their cost, written before the call's
  row (also when the scrub refuses), so the budget counts what the guardrail cost.
- **Budget alerts** (R5): after the log, this month's spend against the budget; the first time it reaches 75, 90 or
  100 % in a month, ``crm.tenant_settings.ai_alert_month`` / ``ai_alert_pct`` remember the level, the agency audit
  gets one ``ai_budget_alert`` event and the result carries ``budget_alert`` (the draft and settings UI show it).
- **Acc2 guardrail down**: the scrub fails closed (``scrub_failed`` / ``scrub_timeout``), the draft says "reply by
  hand" and ``crm_scrub_unavailable`` is logged (an alarm can count it). No second guardrail elsewhere: a scrub path
  that was never corpus-tested must not see traveller text (decision in ``docs/implementation-notes/AA-964.md``).

Slice 1 ships the stub pair (:class:`StubScrubber`, :class:`StubProvider`: deterministic, no network, cost 0) so the
order, the refusals and the log are tested end to end before CRM-10 (scrubber) and CRM-33a (Bedrock) plug in.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from app.audit import AuditEvent, current_request_id
from app.privacy.guardrails import GUARD_OPERATION, GUARD_STAGE, GuardCall, collect_guard_calls
from app.privacy.placeholder_store import SealedMap, check_scrubber_version, seal_map, store_map
from app.privacy.scrubber import KnownValues, ScrubError
from app.ratelimit import FixedWindowLimiter
from app.tenancy import AnyContext
from app.vault import Vault

STAGE_PREFIX = "crm_"
ROLES = frozenset({"writer", "judge", "validate"})  # callers' roles; "guard" is the gateway's own guardrail row
GUARD_ROLE = "guard"
GUARD_PROVIDER = "bedrock_guardrails"
GUARD_FAMILY = "guardrails"
ALERT_LEVELS = (75, 90, 100)
ATTEMPT = 1  # one attempt per request: no fallback route in the CRM today
DEFAULT_BUDGET_USD = Decimal("2")
STUB_PREFIX = "stub"
MANUAL_REPLY = "manual_reply_needed"
MAP_STORE_FAILED = "PlaceholderMapStoreError"

log = logging.getLogger(__name__)

BUDGET_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('crm_llm_budget:' || $1::text, 0))"
MONTH_SPEND_SQL = """
SELECT coalesce(sum(cost_usd_est), 0) FROM crm.llm_call
 WHERE at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
"""
SETTINGS_SQL = """
SELECT ai_enabled, ai_budget_usd, ai_alert_month, ai_alert_pct FROM crm.tenant_settings WHERE deleted_at IS NULL
"""
INSERT_CALL_SQL = """
INSERT INTO crm.llm_call (stage, role, provider, model, input_tokens, output_tokens, cost_usd_est, latency_ms, outcome,
                          refusal_code, error_class, scrub_receipt, attempt_no, request_id, job_id, connection_key,
                          deployment_wire_id, family, cache_read_tokens, cache_write_tokens, units, unit_kind,
                          pricing_mode, prompt_hash, prompt_version)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22,
        $23, $24, $25)
RETURNING llm_call_id
"""
ALERT_SQL = """
INSERT INTO crm.tenant_settings (ai_alert_month, ai_alert_pct) VALUES ($1, $2)
ON CONFLICT (tenant_id) DO UPDATE SET ai_alert_month = EXCLUDED.ai_alert_month, ai_alert_pct = EXCLUDED.ai_alert_pct
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
    placeholders: dict[str, str] = field(default_factory=dict, repr=False)  # placeholder -> value: sealed, never logged


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    request_id: str | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    pricing_mode: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class CallMeta:
    """What the caller says about the request (ledger v2 columns; none of it is text)."""

    prompt_version: str | None = None
    job_id: uuid.UUID | None = None


def prompt_hash(prompt: str, system: str | None = None) -> str:
    """sha256 hex of what the model is sent (the fixed system text and the scrubbed prompt): never the prompt."""
    return hashlib.sha256(f"{system or ''}\x00{prompt}".encode()).hexdigest()


def alert_level(pct: float) -> int | None:
    """The highest R5 alert level ``pct`` reached (75 / 90 / 100), or None below 75 %."""
    reached = [level for level in ALERT_LEVELS if pct >= level]
    return reached[-1] if reached else None


def month_start(today: dt.date | None = None) -> dt.date:
    return (today or dt.datetime.now(dt.UTC).date()).replace(day=1)


class Scrubber(Protocol):
    version: str

    async def scrub(self, text: str, known: KnownValues | None = None) -> Any: ...  # Scrubbed / ScrubResult


class Provider(Protocol):
    name: str
    model: str

    async def complete(self, prompt: str) -> Completion: ...


class StubScrubber:
    """No scrubbing: accepted only together with :class:`StubProvider` (step 1)."""

    version = "stub-0"

    async def scrub(self, text: str, known: KnownValues | None = None) -> Scrubbed:
        return Scrubbed(text=text, receipt={"scrubber": self.version, "entities": 0})


class StubProvider:
    """A deterministic model: no network, cost 0, token counts from the text length."""

    name = "stub"
    model = "stub-echo-1"
    family = "stub"
    connection_key = "stub"

    async def complete(self, prompt: str, *, system: str | None = None) -> Completion:
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
    placeholder_map_id: str | None = None
    budget_alert: int | None = None  # 75 / 90 / 100 when this call raised the month's alert to that level
    truncated: bool = False

    @property
    def fallback(self) -> str | None:
        """``manual_reply_needed`` for every result without a model output (refused or error)."""
        return None if self.outcome == "ok" else MANUAL_REPLY


class LlmGateway:
    def __init__(
        self,
        scrubber: Scrubber,
        provider: Provider,
        keys: Any | None = None,
        limiter: FixedWindowLimiter | None = None,
    ) -> None:
        """``keys``: the :class:`app.vault.KeyProvider` that seals placeholder maps (required once the scrub replaces
        anything; without it such a call is refused). ``limiter``: the per-agency calls-per-minute cap."""
        self.scrubber = scrubber
        self.provider = provider
        self.keys = keys
        self.limiter = limiter

    async def call(
        self,
        ctx: AnyContext,
        *,
        stage: str,
        role: str,
        prompt: str,
        known: KnownValues | None = None,
        system: str | None = None,
        meta: CallMeta | None = None,
    ) -> GatewayResult:
        """``known``: the identity values tied to the call (``app.privacy.known.known_values_for_trip``). ``system``:
        a fixed instruction of ours (no personal data; not scrubbed, sent as the system block)."""
        if not stage.startswith(STAGE_PREFIX) or stage == GUARD_STAGE or role not in ROLES:
            raise ValueError("stage must be crm_* (not crm_scrub) and role writer / judge / validate")
        meta = meta or CallMeta()
        # 0. the per-agency cap, before anything that costs money
        if self.limiter is not None and not self.limiter.allow(str(ctx.tenant_id)):
            return await self._log(ctx, stage, role, "refused", refusal="rate_limited", meta=meta)
        # 1. scrub receipt (every guardrail attempt it makes is its own ledger row)
        with collect_guard_calls() as guards:
            try:
                scrubbed = await self.scrubber.scrub(prompt, known)
                failure = None
            except ScrubError as exc:
                failure = exc.code
            except Exception:  # noqa: BLE001 — any scrubber failure stops the call
                failure = "scrub_failed"
        for guard in guards:
            await self._log_guard(ctx, guard, meta)
        if failure is not None:
            log.warning("crm_scrub_unavailable refusal=%s guard_calls=%d", failure, len(guards))
            return await self._log(ctx, stage, role, "refused", refusal=failure, meta=meta)
        receipt = scrubbed.receipt
        placeholders: dict[str, str] = dict(getattr(scrubbed, "placeholders", {}) or {})
        common = {"receipt": receipt, "meta": meta}
        if self.scrubber.version.startswith(STUB_PREFIX) and self.provider.name != STUB_PREFIX:
            return await self._log(ctx, stage, role, "refused", refusal="scrubber_unavailable", **common)
        # 2. budget, 3. ai_enabled (the per-agency lock holds until this transaction ends, after the log row)
        await ctx.conn.execute(BUDGET_LOCK_SQL, str(ctx.tenant_id))
        settings = await ctx.conn.fetchrow(SETTINGS_SQL)
        enabled = bool(settings["ai_enabled"]) if settings else False
        budget = Decimal(settings["ai_budget_usd"]) if settings else DEFAULT_BUDGET_USD
        spent = Decimal(await ctx.conn.fetchval(MONTH_SPEND_SQL))
        pct = _pct(spent, budget)
        if spent >= budget:
            result = await self._log(ctx, stage, role, "refused", refusal="budget_exhausted", pct=pct, **common)
            return await self._alert(ctx, result, settings, pct)
        if not enabled:
            return await self._log(ctx, stage, role, "refused", refusal="ai_disabled", pct=pct, **common)
        # 3b. seal the placeholder map before paying for the call
        sealed: SealedMap | None = None
        if placeholders:
            try:
                if self.keys is None:
                    raise ScrubError("no key provider for the placeholder map")
                check_scrubber_version(str(receipt.get("scrubber", "")))
                sealed = await seal_map(Vault(self.keys, ctx), placeholders)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 — a map that cannot be sealed stops the call
                return await self._log(ctx, stage, role, "refused", refusal="scrub_failed", pct=pct, **common)
        # 4. the call
        hashed = prompt_hash(scrubbed.text, system)
        started = time.monotonic()
        try:
            if system:
                done = await self.provider.complete(scrubbed.text, system=system)  # type: ignore[call-arg]
            else:
                done = await self.provider.complete(scrubbed.text)
        except Exception as exc:  # noqa: BLE001 — recorded by class only
            latency = int((time.monotonic() - started) * 1000)
            billed = getattr(exc, "billed", None)
            failed = await self._log(
                ctx,
                stage,
                role,
                "error",
                error=str(getattr(exc, "error_class", "") or type(exc).__name__),
                pct=pct,
                latency=latency,
                completion=billed if isinstance(billed, Completion) else None,
                hashed=hashed,
                **common,
            )
            return await self._alert(ctx, failed, settings, None)
        latency = int((time.monotonic() - started) * 1000)
        result = await self._log(
            ctx, stage, role, "ok", pct=pct, latency=latency, completion=done, hashed=hashed, **common
        )
        result = await self._alert(ctx, result, settings, None)
        map_id = None
        if sealed is not None:
            # 6. the map, against the logged call, in a savepoint: if it cannot be stored the paid call stays logged
            # (budget and rollup keep its cost) and the draft falls back to "reply by hand" (the output could never
            # be restored).
            try:
                async with ctx.conn.transaction():
                    await store_map(ctx, sealed, result.llm_call_id or "", str(receipt.get("scrubber", "")))
            except Exception as exc:  # noqa: BLE001 — class only
                log.warning("llm gateway: placeholder map not stored (%s), output dropped", type(exc).__name__)
                return GatewayResult(**{**result.__dict__, "outcome": "error", "error_class": MAP_STORE_FAILED})
            map_id = str(sealed.placeholder_map_id)
        return GatewayResult(
            **{**result.__dict__, "text": done.text, "placeholder_map_id": map_id, "truncated": done.truncated}
        )

    async def _alert(self, ctx: AnyContext, result: GatewayResult, settings: Any, pct: float | None) -> GatewayResult:
        """R5: raise the month's 75 / 90 / 100 % alert once per level (after the row that crossed it is logged)."""
        if pct is None:
            budget = Decimal(settings["ai_budget_usd"]) if settings else DEFAULT_BUDGET_USD
            pct = _pct(Decimal(await ctx.conn.fetchval(MONTH_SPEND_SQL)), budget)
            result = GatewayResult(**{**result.__dict__, "budget_used_pct": round(pct, 1)})
        level = alert_level(pct)
        if level is None:
            return result
        month = month_start()
        raised = 0
        if settings and settings.get("ai_alert_month") == month and settings.get("ai_alert_pct") is not None:
            raised = int(settings["ai_alert_pct"])
        if level <= raised:
            return result
        await ctx.conn.execute(ALERT_SQL, month, level)
        ctx.audit_events.append(
            AuditEvent(
                actor_kind="ai_agent",
                actor_ref="ai:budget",
                action="ai_budget_alert",
                outcome="allowed",
                object_kind="tenant_settings",
                object_id=str(ctx.tenant_id),
                reason=f"pct_{level}",
                request_id=current_request_id(),
            )
        )
        log.warning("crm_ai_budget_alert tenant=%s pct=%d", ctx.tenant_id, level)
        return GatewayResult(**{**result.__dict__, "budget_alert": level})

    async def _log_guard(self, ctx: AnyContext, guard: GuardCall, meta: CallMeta) -> None:
        """One ``ApplyGuardrail`` attempt as its own ledger row (stage crm_scrub, role guard, text units)."""
        cost = guard.cost_usd
        await ctx.conn.fetchval(
            INSERT_CALL_SQL,
            GUARD_STAGE,
            GUARD_ROLE,
            GUARD_PROVIDER,
            GUARD_OPERATION,
            0,
            0,
            cost,
            max(0, guard.latency_ms),
            "ok" if guard.outcome == "ok" else "error",
            None,
            guard.error_class if guard.outcome != "ok" else None,
            "{}",
            ATTEMPT,
            guard.request_id,
            meta.job_id,
            GUARD_PROVIDER,
            GUARD_OPERATION,
            GUARD_FAMILY,
            None,
            None,
            guard.units,
            "text_unit",
            "estimate" if guard.estimated else "code_table",
            None,
            None,
        )
        await ctx.conn.execute(ROLLUP_SQL, GUARD_STAGE, GUARD_OPERATION, 0 if guard.outcome == "ok" else 1, 0, 0, cost)

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
        meta: CallMeta | None = None,
        hashed: str | None = None,
    ) -> GatewayResult:
        receipt = receipt or {}
        meta = meta or CallMeta()
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
            ATTEMPT,
            completion.request_id if completion else None,
            meta.job_id,
            getattr(self.provider, "connection_key", None) or self.provider.name,
            self.provider.model,
            getattr(self.provider, "family", None),
            completion.cache_read_tokens if completion else None,
            completion.cache_write_tokens if completion else None,
            None,
            None,
            completion.pricing_mode if completion else None,
            hashed,
            meta.prompt_version,
        )
        await ctx.conn.execute(
            ROLLUP_SQL, stage, self.provider.model, 0 if outcome == "ok" else 1, tokens_in, tokens_out, cost
        )
        # Design §4.9 "AI context builds" (AA-940): one agency audit event per gateway call, actor = the AI stage
        # (the member or job that asked is the llm_call row's created_by). A refusal is a denied event.
        ctx.audit_events.append(
            AuditEvent(
                actor_kind="ai_agent",
                actor_ref=f"ai:{stage}",
                action="ai_context_build",
                outcome="denied" if outcome == "refused" else "allowed",
                object_kind="llm_call",
                object_id=str(call_id),
                reason=refusal or ("provider_error" if outcome == "error" else None),
                request_id=current_request_id(),
            )
        )
        return GatewayResult(
            outcome=outcome,
            refusal_code=refusal,
            error_class=error,
            llm_call_id=str(call_id),
            budget_used_pct=round(pct, 1) if pct is not None else None,
            receipt=receipt,
        )


def _pct(spent: Decimal, budget: Decimal) -> float:
    return float(spent / budget * 100) if budget > 0 else 100.0


def stub_gateway() -> LlmGateway:
    return LlmGateway(StubScrubber(), StubProvider())


def build_gateway(settings: Any, keys: Any | None = None) -> LlmGateway:
    """The process's gateway (AA-964), from the environment (``app.settings``):

    - **scrubber**: the F3 scrubber (:func:`app.privacy.guardrails.build_scrubber`) whenever ``SCRUB_GUARDRAIL_*`` is
      set or a real provider is configured; without a guardrail that scrubber refuses every scrub (fail closed), so a
      real model never runs unscrubbed. Only the stub provider without a guardrail gets the stub scrubber (Dev, tests).
    - **provider**: :class:`app.llm.providers.bedrock.BedrockConverseProvider` only when ``CRM_LLM_PROVIDER=bedrock``
      (``app.settings`` refuses it without a model id and both prices); the stub otherwise (the default).
    - **limiter**: ``RATE_LIMIT_AI_PER_MINUTE`` calls per agency per minute (0 = off).

    No AWS call here: the Bedrock and guardrail clients are made on first use.
    """
    from app.privacy.guardrails import build_scrubber

    provider: Any = StubProvider()
    if settings.crm_llm_provider == "bedrock":
        from app.llm.providers.bedrock import BedrockConverseProvider

        provider = BedrockConverseProvider(
            settings.crm_llm_model_id,
            usd_per_mtok_in=settings.crm_llm_usd_per_mtok_in,
            usd_per_mtok_out=settings.crm_llm_usd_per_mtok_out,
            region=settings.crm_llm_region,
            max_tokens=settings.crm_llm_max_tokens,
            role_arn=settings.crm_llm_role_arn,
        )
    real_scrub = bool(settings.scrub_guardrail_id) or provider.name != STUB_PREFIX
    scrubber: Any = build_scrubber(settings) if real_scrub else StubScrubber()
    return LlmGateway(scrubber, provider, keys=keys, limiter=FixedWindowLimiter(settings.rate_limit_ai_per_minute))
