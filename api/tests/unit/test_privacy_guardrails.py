"""Pass 3: Bedrock Guardrails ApplyGuardrail in detect mode (AA-938; AA-930 §2: acc2, us-west-1)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.privacy.guardrails import BedrockGuardrailsDetector, UnavailableDetector, build_scrubber, parse_assessments
from app.privacy.scrubber import Detection, PiiScrubber, ScrubError, ScrubTimeout
from app.settings import Settings

pytestmark = pytest.mark.asyncio

RESPONSE = {
    "action": "NONE",
    "assessments": [
        {
            "sensitiveInformationPolicy": {
                "piiEntities": [
                    {"type": "NAME", "match": "Peter Lawson", "action": "NONE", "detected": True},
                    {"type": "ADDRESS", "match": "12 Lý Thái Tổ", "action": "NONE", "detected": True},
                    {"type": "NAME", "match": "", "action": "NONE"},
                ],
                "regexes": [{"name": "x", "match": "ignored", "action": "NONE"}],
            }
        },
        {"contentPolicy": {}},
    ],
}


class Client:
    def __init__(self, response: Any = None, fail: Exception | None = None, sleep: float = 0) -> None:
        self.response = response if response is not None else RESPONSE
        self.fail = fail
        self.sleep = sleep
        self.calls: list[dict] = []

    def apply_guardrail(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.sleep:
            time.sleep(self.sleep)
        if self.fail:
            raise self.fail
        return self.response


def detector(client: Client) -> BedrockGuardrailsDetector:
    return BedrockGuardrailsDetector("abc123guard", "1", region="us-west-1", timeout_s=1, client=client)


async def test_detect_mode_call_shape_and_findings() -> None:
    client = Client()
    found = await detector(client).detect("Peter Lawson lives at 12 Lý Thái Tổ")
    assert found == [Detection("NAME", "Peter Lawson"), Detection("ADDRESS", "12 Lý Thái Tổ")]
    [call] = client.calls
    assert call == {
        "guardrailIdentifier": "abc123guard",
        "guardrailVersion": "1",
        "source": "INPUT",
        "content": [{"text": {"text": "Peter Lawson lives at 12 Lý Thái Tổ"}}],
        "outputScope": "FULL",
    }
    assert "Peter" not in repr(found)  # Detection.match is excluded from repr (never in a log line)
    assert await detector(client).detect("   ") == [] and len(client.calls) == 1  # blank text: no paid call


async def test_errors_are_raised_by_class_only() -> None:
    class AccessDeniedException(Exception):
        pass

    with pytest.raises(ScrubError) as info:
        await detector(Client(fail=AccessDeniedException("text was Peter Lawson"))).detect("Peter Lawson")
    assert str(info.value) == "AccessDeniedException"
    for bad in ({"assessments": "x"}, {"assessments": ["x"]}, "nope"):
        with pytest.raises(ScrubError):
            parse_assessments(bad)  # type: ignore[arg-type]


async def test_r_a_slow_guardrail_times_out_through_the_scrubber() -> None:
    scrubber = PiiScrubber(detector(Client(sleep=0.5)), timeout_s=0.05)
    with pytest.raises(ScrubTimeout):
        await scrubber.scrub("Peter Lawson")


async def test_unconfigured_guardrail_refuses_every_scrub() -> None:
    with pytest.raises(ScrubError):
        await UnavailableDetector().detect("x")
    scrubber = build_scrubber(Settings(environment="test"))
    assert scrubber.detector.name == "unavailable" and scrubber.timeout_s == 3.0
    with pytest.raises(ScrubError):
        await scrubber.scrub("hello")
    with pytest.raises(ValueError):
        BedrockGuardrailsDetector("", "1", region="us-west-1", timeout_s=1, client=Client())


async def test_configured_guardrail_builds_a_boto3_client() -> None:
    settings = Settings(
        environment="test",
        scrub_guardrail_id="abc123guard",
        scrub_guardrail_version="2",
        scrub_detector_timeout_ms=1500,
    )
    scrubber = build_scrubber(settings)
    assert isinstance(scrubber.detector, BedrockGuardrailsDetector)
    assert scrubber.detector.guardrail_version == "2" and scrubber.timeout_s == 1.5
    assert scrubber.detector._client.meta.region_name == "us-west-1"
