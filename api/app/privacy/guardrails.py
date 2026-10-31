"""Pass 3 of the F3 filter: Amazon Bedrock Guardrails ``ApplyGuardrail`` in detect mode (design v3 §4.5 step 3).

The guardrail lives in acc2, us-west-1 (AA-930 readiness §2: Guardrails is a first-party AWS charge there, so the raw
text never leaves the account before it is scrubbed); AA-CIS-Infra ``crm_scrub.tf`` creates it with every PII entity
on action ``NONE`` (detect only: the guardrail never blocks or rewrites, our code numbers the placeholders) and gives
the task role ``bedrock:ApplyGuardrail`` on that guardrail only. Its id and version reach the task as
``SCRUB_GUARDRAIL_ID`` / ``SCRUB_GUARDRAIL_VERSION``.

- ``outputScope=FULL`` so detect-mode findings are returned; only ``type``, ``detected`` and ``match`` are read, and
  ``match`` (the raw value) is never logged or raised (lane §5.4, S6 / K12). With ``FULL`` the live API lists **every
  configured entity type** on each call: a type that was not found comes back as ``{"type": ..., "match": "",
  "action": "NONE", "detected": false}`` (probed live, AA-938). Those rows are not findings and are skipped; a row
  that is ``detected`` (or carries no ``detected`` flag) but has no ``match`` is still returned with an empty value
  so the scrubber refuses it (fail closed).
- botocore retries are off (one attempt) and the read timeout follows the scrubber's timeout; the scrubber's own
  ``asyncio.wait_for`` bounds the whole call. An error is raised by class only.
- Without a configured guardrail, :class:`UnavailableDetector` refuses every scrub: no silent "two passes only" mode.
- **Every call is a ledger row** (AA-964, root ADR 0009 §8-9 seam ``guard``): inside :func:`collect_guard_calls` (the
  gateway opens it around the scrub) each ``ApplyGuardrail`` attempt appends one :class:`GuardCall` with its billed
  text units (the response ``usage``), latency, outcome and the AWS request id; the gateway writes it into
  ``crm.llm_call`` as stage ``crm_scrub``, role ``guard`` with its cost (:data:`USD_PER_TEXT_UNIT`). An attempt that
  times out or fails keeps an **estimated** unit count (1 unit per started 1000 characters, ``pricing_mode
  estimate``): AWS may still bill it. Outside a collector (the corpus workflow) nothing is recorded.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import time
from collections.abc import Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.privacy.scrubber import Detection, PiiScrubber, ScrubError

# Bedrock Guardrails sensitive-information (PII) filter: USD 0.10 per 1000 text units (AWS Bedrock pricing page;
# re-check it before Guardrails is switched on, a paid feature); one text unit = up to 1000 characters. A code table,
# so rows say pricing_mode code_table.
USD_PER_TEXT_UNIT = Decimal("0.0001")
TEXT_UNIT_CHARS = 1000
GUARD_STAGE = "crm_scrub"
GUARD_OPERATION = "apply_guardrail"


@dataclass
class GuardCall:
    """One ``ApplyGuardrail`` attempt (ids and numbers only, never the text or a match)."""

    units: int
    estimated: bool = True
    latency_ms: int = 0
    outcome: str = "error"  # ok | error
    error_class: str | None = "GuardrailNoAnswer"
    request_id: str | None = None

    @property
    def cost_usd(self) -> Decimal:
        return USD_PER_TEXT_UNIT * self.units


_GUARD_CALLS: ContextVar[list[GuardCall] | None] = ContextVar("crm_guard_calls", default=None)


@contextlib.contextmanager
def collect_guard_calls() -> Iterator[list[GuardCall]]:
    """Collect the guardrail attempts made inside the block (the gateway writes them to the ledger)."""
    calls: list[GuardCall] = []
    token = _GUARD_CALLS.set(calls)
    try:
        yield calls
    finally:
        _GUARD_CALLS.reset(token)


def estimated_units(text: str) -> int:
    return max(1, math.ceil(len(text) / TEXT_UNIT_CHARS))


def billed_units(response: Any) -> int | None:
    """The text units AWS billed (``usage``: every ``*Units`` count but the free ones), or None when absent."""
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = [v for k, v in usage.items() if k.endswith("Units") and "Free" not in k and isinstance(v, int) and v >= 0]
    return sum(counts) if counts else None


def _aws_code(exc: BaseException) -> str:
    """The AWS error code (``ThrottlingException`` ...) for the ledger, else the class name; never the message."""
    response = getattr(exc, "response", None)
    code = (response.get("Error") or {}).get("Code") if isinstance(response, dict) else None
    if isinstance(code, str) and re.match(r"^[A-Za-z][A-Za-z0-9_]{0,63}$", code):
        return code
    return type(exc).__name__


def _request_id(response: Any) -> str | None:
    meta = response.get("ResponseMetadata") if isinstance(response, dict) else None
    value = meta.get("RequestId") if isinstance(meta, dict) else None
    return value if isinstance(value, str) and 0 < len(value) <= 128 else None


class UnavailableDetector:
    """No guardrail configured: every scrub fails closed (refusal ``scrub_failed``)."""

    name = "unavailable"

    async def detect(self, text: str) -> Sequence[Detection]:
        raise ScrubError("detector not configured")


class BedrockGuardrailsDetector:
    name = "bedrock_guardrails"

    def __init__(
        self,
        guardrail_id: str,
        guardrail_version: str,
        *,
        region: str,
        timeout_s: float,
        client: Any | None = None,
    ) -> None:
        if not guardrail_id or not guardrail_version:
            raise ValueError("guardrail id and version are required")
        self.guardrail_id = guardrail_id
        self.guardrail_version = guardrail_version
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "bedrock-runtime",
                region_name=region,
                config=Config(
                    connect_timeout=max(1, int(timeout_s)),
                    read_timeout=max(1, int(timeout_s) + 1),
                    retries={"max_attempts": 1, "mode": "standard"},
                ),
            )
        self._client = client

    async def detect(self, text: str) -> Sequence[Detection]:
        if not text.strip():
            return []
        calls = _GUARD_CALLS.get()
        record = GuardCall(units=estimated_units(text))
        if calls is not None:
            calls.append(record)  # kept (estimated) when the attempt times out or fails
        started = time.monotonic()
        try:
            response = await asyncio.to_thread(
                self._client.apply_guardrail,
                guardrailIdentifier=self.guardrail_id,
                guardrailVersion=self.guardrail_version,
                source="INPUT",
                content=[{"text": {"text": text}}],
                outputScope="FULL",
            )
        except Exception as exc:  # noqa: BLE001 — class only; botocore messages can echo request fields
            record.latency_ms = int((time.monotonic() - started) * 1000)
            record.error_class = _aws_code(exc)
            raise ScrubError(type(exc).__name__) from None
        record.latency_ms = int((time.monotonic() - started) * 1000)
        record.request_id = _request_id(response)
        units = billed_units(response)
        if units is not None:
            record.units, record.estimated = units, False
        record.outcome, record.error_class = "ok", None
        return parse_assessments(response)


def parse_assessments(response: dict[str, Any]) -> list[Detection]:
    """``ApplyGuardrail`` response -> PII findings. The guardrail has no custom regexes (pass 2 runs them locally, for
    free). Anything malformed fails closed."""
    if not isinstance(response, dict) or not isinstance(response.get("assessments", []), list):
        raise ScrubError("malformed guardrail response")
    found: list[Detection] = []
    for assessment in response.get("assessments", []):
        if not isinstance(assessment, dict):
            raise ScrubError("malformed guardrail response")
        policy = assessment.get("sensitiveInformationPolicy") or {}
        entities = policy.get("piiEntities", []) or []
        if not isinstance(entities, list):
            raise ScrubError("malformed guardrail response")
        for entity in entities:
            if not isinstance(entity, dict) or not entity.get("type"):
                raise ScrubError("malformed guardrail response")
            detected = entity.get("detected")
            if detected is not None and not isinstance(detected, bool):
                raise ScrubError("malformed guardrail response")
            match = entity.get("match")
            if match is not None and not isinstance(match, str):
                raise ScrubError("malformed guardrail response")
            if detected is False and not match:
                continue  # outputScope=FULL lists every configured type; this one was not found in the text
            # A finding without its match is kept with an empty value: the scrubber refuses it (fail closed). A row
            # flagged not detected but carrying a value is kept too: removing a value costs less than leaking it.
            found.append(Detection(str(entity["type"]), match or ""))
    return found


def build_scrubber(settings: Any) -> PiiScrubber:
    """The production scrubber: Guardrails when configured, else one that refuses every scrub (fail closed)."""
    timeout_s = settings.scrub_detector_timeout_ms / 1000
    detector: Any = UnavailableDetector()
    if settings.scrub_guardrail_id:
        detector = BedrockGuardrailsDetector(
            settings.scrub_guardrail_id,
            settings.scrub_guardrail_version,
            region=settings.scrub_guardrail_region,
            timeout_s=timeout_s,
        )
    return PiiScrubber(detector, timeout_s=timeout_s)
