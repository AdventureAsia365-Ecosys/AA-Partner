"""Pass 3 of the F3 filter: Amazon Bedrock Guardrails ``ApplyGuardrail`` in detect mode (design v3 §4.5 step 3).

The guardrail lives in acc2, us-west-1 (AA-930 readiness §2: Guardrails is a first-party AWS charge there, so the raw
text never leaves the account before it is scrubbed); AA-CIS-Infra ``crm_scrub.tf`` creates it with every PII entity
on action ``NONE`` (detect only: the guardrail never blocks or rewrites, our code numbers the placeholders) and gives
the task role ``bedrock:ApplyGuardrail`` on that guardrail only. Its id and version reach the task as
``SCRUB_GUARDRAIL_ID`` / ``SCRUB_GUARDRAIL_VERSION``.

- ``outputScope=FULL`` so detect-mode findings are returned; only ``type`` and ``match`` are read, and ``match`` (the
  raw value) is never logged or raised (lane §5.4, S6 / K12).
- botocore retries are off (one attempt) and the read timeout follows the scrubber's timeout; the scrubber's own
  ``asyncio.wait_for`` bounds the whole call. An error is raised by class only.
- Without a configured guardrail, :class:`UnavailableDetector` refuses every scrub: no silent "two passes only" mode.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from app.privacy.scrubber import Detection, PiiScrubber, ScrubError


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
            raise ScrubError(type(exc).__name__) from None
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
        for entity in policy.get("piiEntities", []) or []:
            if entity.get("match") and entity.get("type"):
                found.append(Detection(str(entity["type"]), str(entity["match"])))
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
