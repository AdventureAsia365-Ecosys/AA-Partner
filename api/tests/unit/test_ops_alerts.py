"""In-app ops checks that replaced the CRM CloudWatch alarms (S263 alert diet, AA-1025): events raised at the alarm
lines, state checks of the job sampler read, the once-only ledger dedupe and the SNS publish. The ledger SQL runs
against Postgres in tests/integration/test_job_metrics.py."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.jobs import queue
from app.jobs.metrics import AuditSample, Sample
from app.observability import ops_alerts, reveal_meter
from app.observability.ops_alerts import AlertPublisher, Finding, alert_key, render, run_checks, state_findings
from app.ops_release import service as ops_release

APP = Path(__file__).resolve().parents[2] / "app"
NOW = datetime(2026, 11, 1, 23, 59, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _empty_pending() -> Any:
    ops_alerts.PENDING.drain()
    yield
    ops_alerts.PENDING.drain()


# ── registry ───────────────────────────────────────────────────────────────────────────────────


def test_every_event_check_is_raised_somewhere_in_the_app() -> None:
    """An event check nobody emits would be a silent hole (the old metric filter had the same failure mode)."""
    code = "\n".join(p.read_text() for p in APP.rglob("*.py") if p.name != "ops_alerts.py")
    raised = set(re.findall(r'ops_alerts\.emit\(\s*"([a-z_]+)"', code)) | set(ops_release.OPS_ALERT_CHECKS.values())
    assert raised == set(ops_alerts.EVENT_CHECKS)
    assert ops_alerts.EVENT_CHECKS <= set(ops_alerts.CHECKS)


def test_ops_alert_kind_fits_the_job_kind_rule() -> None:
    assert re.match(r"^crm_[a-z0-9_]+$", ops_alerts.OPS_ALERT_KIND)


# ── events ─────────────────────────────────────────────────────────────────────────────────────


def test_emit_queues_ids_only_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    sid = uuid.uuid4()
    ops_alerts.emit("breakglass_opened", sid, session_id=sid, reason_code="incident")
    ops_alerts.emit("not_a_check", None, x=1)  # logged, not raised
    (f,) = ops_alerts.PENDING.drain()
    assert (f.check, f.scope) == ("breakglass_opened", str(sid))
    assert f.details == {"session_id": str(sid), "reason_code": "incident"}
    assert "ops_alert_emit_failed check=not_a_check" in caplog.text


def test_pending_is_bounded_and_restores_in_order() -> None:
    for i in range(ops_alerts.MAX_PENDING + 5):
        ops_alerts.emit("membership_apply_refused", i, event_id=i)
    items = ops_alerts.PENDING.drain()
    assert len(items) == ops_alerts.MAX_PENDING and items[0].scope == "5"
    ops_alerts.PENDING.restore(items[:2])
    ops_alerts.emit("membership_feed_stuck", error="cis_5xx", failures=10)
    assert [f.scope for f in ops_alerts.PENDING.drain()] == ["5", "6", None]


def test_ops_release_notice_raises_the_check() -> None:
    rid = uuid.uuid4()
    notice = ops_release.Notice(
        ops_release.T21_EVENT, logging.WARNING, (("tenant_id", uuid.uuid4()), ("ops_release_id", rid))
    )
    notice.emit()
    (f,) = ops_alerts.PENDING.drain()
    assert (f.check, f.scope) == ("ops_release_incomplete_t21", str(rid))


def test_bulk_reveal_crossing_raises_one_check_per_member_and_hour() -> None:
    meter = reveal_meter.RevealMeter(threshold=3)
    tenant, member = uuid.uuid4(), uuid.uuid4()
    meter.record(tenant, member, 2)
    assert ops_alerts.PENDING.drain() == []
    meter.record(tenant, member, 2)  # crosses
    meter.record(tenant, member, 1)  # already above: no second event
    (f,) = ops_alerts.PENDING.drain()
    assert f.check == "bulk_reveal" and f.scope is not None and f.scope.startswith(f"{member}:")
    assert f.details["reveals_last_hour"] == "4"


# ── state checks ───────────────────────────────────────────────────────────────────────────────


def test_state_thresholds_are_the_old_alarms() -> None:
    assert state_findings(Sample(900.0, 0, 0, 900.0), AuditSample(179.0, 1, 49.0)) == []
    found = state_findings(Sample(901.0, 1, 2, 901.0), AuditSample(180.0, 0, 50.0))
    assert [f.check for f in found] == [
        "jobs_stuck",
        "jobs_overdue",
        "audit_export_lag",
        "audit_partitions",
        "audit_verify_lag",
    ]
    assert all(f.scope is None for f in found)
    assert state_findings(Sample(0.0, 0, 0, 0.0), None) == []


def test_keys_are_per_event_or_per_utc_day() -> None:
    assert alert_key(Finding("jobs_stuck", "x"), NOW) == "crm_ops_alert:jobs_stuck:2026-11-01"
    assert alert_key(Finding("breakglass_opened", "x", "s-1"), NOW) == "crm_ops_alert:breakglass_opened:s-1"


def test_render_subject_short_ascii() -> None:
    subject, text = render(Finding("audit_partitions", "0 month(s) ahead", None, {"a": "1"}), "dev")
    assert subject == "[AA-Partner dev] Ops check: Audit partition missing"
    assert len(subject) <= 100 and subject.isascii()
    assert "a: 1" in text and "per check and UTC day" in text
    assert "per this event" in render(Finding("breakglass_opened", "x", "s-1"), "dev")[1]


def test_sns_client_short_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    seen: dict[str, Any] = {}

    def fake_client(service: str, **kw: Any) -> str:
        seen.update(kw, service=service)
        return "client"

    monkeypatch.setattr(boto3, "client", fake_client)
    assert ops_alerts.sns_client() == "client"
    cfg = seen["config"]
    assert (seen["service"], seen["region_name"]) == ("sns", "us-west-1")
    assert (cfg.connect_timeout, cfg.read_timeout, cfg.retries) == (3, 5, {"max_attempts": 1})


# ── publishing ─────────────────────────────────────────────────────────────────────────────────


class FakeSns:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def publish(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        if self.error:
            raise self.error
        return {"MessageId": "m-1"}


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"keys": {}, "forgotten": [], "error": None}

    async def record(pool: Any, kind: str, key: str, payload: dict[str, Any]) -> str | None:
        if state["error"]:
            raise state["error"]
        if key in state["keys"]:
            return None
        state["keys"][key] = payload
        return f"id-{len(state['keys'])}"

    async def forget(pool: Any, job_id: str) -> None:
        state["forgotten"].append(job_id)

    monkeypatch.setattr(queue, "record_alert", record)
    monkeypatch.setattr(queue, "forget_alert", forget)
    return state


STUCK = Finding("jobs_stuck", "oldest due row waited 1200 s")
OPENED = Finding("breakglass_opened", "session_id=s-1", "s-1")


@pytest.mark.asyncio
async def test_sent_once(ledger: dict[str, Any]) -> None:
    sns = FakeSns()
    pub = AlertPublisher("topic-placeholder", environment="dev", client_factory=lambda: sns)
    assert await pub.alert(object(), [STUCK, OPENED], NOW) == ({"jobs_stuck": "sent", "breakglass_opened": "sent"}, [])  # type: ignore[arg-type]
    assert await pub.alert(object(), [STUCK], NOW) == ({"jobs_stuck": "already_sent"}, [])  # type: ignore[arg-type]
    assert len(sns.calls) == 2 and sns.calls[0]["TopicArn"] == "topic-placeholder"
    assert ledger["keys"]["crm_ops_alert:jobs_stuck:2026-11-01"] == {"kind": "jobs_stuck", "scope": "2026-11-01"}


@pytest.mark.asyncio
async def test_failed_publish_forgets_and_retries(ledger: dict[str, Any]) -> None:
    pub = AlertPublisher("topic-placeholder", client_factory=lambda: FakeSns(PermissionError("AccessDenied")))
    results, retry = await pub.alert(object(), [OPENED], NOW)  # type: ignore[arg-type]
    assert results == {"breakglass_opened": "failed"} and retry == [OPENED]
    assert ledger["forgotten"] == ["id-1"]


@pytest.mark.asyncio
async def test_ledger_or_forget_failure_never_raises(ledger: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    ledger["error"] = ConnectionError("db down")
    pub = AlertPublisher("topic-placeholder", client_factory=FakeSns)
    assert await pub.alert(object(), [STUCK], NOW) == ({"jobs_stuck": "failed"}, [STUCK])  # type: ignore[arg-type]

    ledger["error"] = None

    async def broken_forget(pool: Any, job_id: str) -> None:
        raise ConnectionError("db down")

    monkeypatch.setattr(queue, "forget_alert", broken_forget)
    pub = AlertPublisher("topic-placeholder", client_factory=lambda: FakeSns(RuntimeError("throttled")))
    assert (await pub.alert(object(), [OPENED], NOW))[0] == {"breakglass_opened": "failed"}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_no_topic_logs_only(ledger: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    pub = AlertPublisher("", client_factory=FakeSns)
    assert not pub.configured
    assert await pub.alert(object(), [STUCK], NOW) == ({"jobs_stuck": "not_configured"}, [])  # type: ignore[arg-type]
    assert ledger["keys"] == {} and "ALERTS_TOPIC_ARN unset" in caplog.text


@pytest.mark.asyncio
async def test_run_checks_puts_failed_events_back_but_not_state_checks(ledger: dict[str, Any]) -> None:
    ops_alerts.emit("breakglass_opened", "s-9", session_id="s-9")
    pub = AlertPublisher("topic-placeholder", client_factory=lambda: FakeSns(RuntimeError("down")))
    results = await run_checks(object(), pub, Sample(1200.0, 0, 0, 1200.0), None, NOW)  # type: ignore[arg-type]
    assert results == {"jobs_stuck": "failed", "breakglass_opened": "failed"}
    assert [f.check for f in ops_alerts.PENDING.drain()] == ["breakglass_opened"]


@pytest.mark.asyncio
async def test_flush_events_sends_the_pending_events_only(ledger: dict[str, Any]) -> None:
    """S263 review: the lifespan's shutdown flush sends the events of the last tick, no state check."""
    sns = FakeSns()
    pub = AlertPublisher("topic-placeholder", client_factory=lambda: sns)
    assert await ops_alerts.flush_events(object(), pub, NOW) == {}  # type: ignore[arg-type]
    assert sns.calls == [] and ledger["keys"] == {}  # nothing pending: no ledger write
    ops_alerts.emit("breakglass_opened", "s-7", session_id="s-7")
    assert len(ops_alerts.PENDING) == 1
    assert await ops_alerts.flush_events(object(), pub, NOW) == {"breakglass_opened": "sent"}  # type: ignore[arg-type]
    assert len(sns.calls) == 1 and len(ops_alerts.PENDING) == 0


@pytest.mark.asyncio
async def test_a_tick_cancelled_mid_way_puts_its_events_back(ledger: dict[str, Any]) -> None:
    """The lifespan cancels the sampler: events a cancelled tick had drained must reach the shutdown flush."""
    started = asyncio.Event()

    class SlowPublisher(AlertPublisher):
        async def alert(self, pool: Any, found: list[Finding], now: Any = None) -> Any:
            started.set()
            await asyncio.Event().wait()

    ops_alerts.emit("ops_release_withdrawn", "r-1", release_id="r-1")
    tick = asyncio.create_task(run_checks(object(), SlowPublisher("topic-placeholder"), Sample(0.0, 0, 0, 0.0)))  # type: ignore[arg-type]
    await started.wait()
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick
    assert [f.check for f in ops_alerts.PENDING.drain()] == ["ops_release_withdrawn"]
