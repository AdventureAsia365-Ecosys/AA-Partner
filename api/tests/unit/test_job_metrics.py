"""Job sampler (AA-751 B5; S263 ops checks instead of CloudWatch): the pure sample rule, the hand-off to the in-app
checks, and the never-raise loop. The SQL runs against Postgres in tests/integration/test_job_metrics.py."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from app.jobs import metrics
from app.jobs.kinds import DEFAULT_EXPECTED_SECONDS, EXPECTED_SECONDS, expected_seconds, overdue_after_seconds
from app.jobs.metrics import JobMetricsSampler, Sample, backlog_sample
from app.observability import ops_alerts, reveal_meter
from tests.unit.fakes import ScriptedConn


@pytest.fixture(autouse=True)
def _no_pending_events() -> Any:
    """S263: events other tests raised (a bulk reveal crossing, ...) would join this module's findings."""
    ops_alerts.PENDING.drain()
    yield
    ops_alerts.PENDING.drain()


def test_expected_seconds_and_default() -> None:
    assert expected_seconds("crm_heartbeat") == 30
    assert expected_seconds("crm_job_retention") == 900
    assert expected_seconds("crm_unknown") == DEFAULT_EXPECTED_SECONDS == 300
    assert overdue_after_seconds("crm_heartbeat") == 60
    assert overdue_after_seconds("crm_unknown") == 600
    assert all(kind.startswith("crm_") for kind in EXPECTED_SECONDS)


def test_nothing_due_nothing_running_is_zero() -> None:
    assert backlog_sample(None, []) == Sample(0.0, 0, 0, 0.0)


def test_due_rows_age_when_nothing_runs() -> None:
    assert backlog_sample(1200.4, []).oldest_queued_age_s == 1200.4


def test_a_healthy_running_job_zeroes_the_queue_age() -> None:
    s = backlog_sample(1200.0, [("crm_job_retention", 800.0)])  # 800 < 2 x 900
    assert (s.oldest_queued_age_s, s.overdue_running, s.running, s.due_queued_age_s) == (0.0, 0, 1, 1200.0)


def test_an_overdue_running_job_counts_and_does_not_hide_the_queue() -> None:
    s = backlog_sample(1000.0, [("crm_heartbeat", 61.0)])  # > 2 x 30
    assert (s.oldest_queued_age_s, s.overdue_running) == (1000.0, 1)


def test_overdue_threshold_is_strict_and_unknown_kinds_use_the_default() -> None:
    assert backlog_sample(None, [("crm_heartbeat", 60.0)]).overdue_running == 0
    assert backlog_sample(None, [("crm_x6_probe", 600.0), ("crm_x6_probe", 601.0)]).overdue_running == 1


def test_one_healthy_job_among_overdue_ones_still_zeroes_the_queue_age() -> None:
    # Rolling deploy: two workers, one stuck, one healthy. The stuck one shows as overdue.
    s = backlog_sample(950.0, [("crm_heartbeat", 500.0), ("crm_heartbeat", 5.0)])
    assert (s.oldest_queued_age_s, s.overdue_running) == (0.0, 1)


def test_unknown_attempt_age_is_healthy_and_negative_ages_clamp() -> None:
    s = backlog_sample(-3.0, [("crm_heartbeat", None)])
    assert (s.oldest_queued_age_s, s.overdue_running, s.due_queued_age_s) == (0.0, 0, 0.0)


def test_no_cloudwatch_left() -> None:
    """S263: the AA/Jobs/Crm metrics and their alarms are gone (app.observability.ops_alerts instead)."""
    for name in ("metric_data", "cloudwatch_client", "NAMESPACE", "CLOUDWATCH_REGION"):
        assert not hasattr(metrics, name)


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    async def alert(self, pool: Any, found: list[Any], now: Any = None) -> tuple[dict[str, str], list[Any]]:
        self.calls.append(found)
        return {f.check: "sent" for f in found}, []


def _pool(conn: ScriptedConn):
    async def provide() -> ScriptedConn:
        return conn

    return provide


def _conn(oldest: Any = 1000.0, running: Any = ()) -> ScriptedConn:
    return ScriptedConn(
        [
            ("min(run_after)", oldest),
            ("WHERE status = 'running'", [{"kind": k, "attempt_age_s": a} for k, a in running]),
        ]
    )


@pytest.mark.asyncio
async def test_sample_once_hands_the_findings_to_the_publisher(caplog: pytest.LogCaptureFixture) -> None:
    pub = FakePublisher()
    sampler = JobMetricsSampler(_pool(_conn(1000.0, [("crm_heartbeat", 700.0)])), publisher=pub)  # type: ignore[arg-type]
    caplog.set_level(logging.INFO, logger="app.jobs.metrics")
    sample = await sampler.sample_once()
    assert sample == Sample(1000.0, 1, 1, 1000.0)
    assert [f.check for f in pub.calls[0]] == ["jobs_stuck", "jobs_overdue"]
    assert "ops_metrics_sampled oldest_queued_age_s=1000 overdue_running_jobs=1 running=1" in caplog.text
    assert "checks=jobs_overdue:sent,jobs_stuck:sent" in caplog.text


@pytest.mark.asyncio
async def test_sample_once_never_raises_on_a_failed_read(caplog: pytest.LogCaptureFixture) -> None:
    pub = FakePublisher()
    broken_read = JobMetricsSampler(_pool(_conn(oldest=ConnectionError("db down"))), publisher=pub)  # type: ignore[arg-type]
    assert await broken_read.sample_once() is None
    assert pub.calls == []

    async def no_pool() -> Any:
        raise OSError("connect refused")

    assert await JobMetricsSampler(no_pool, publisher=pub).sample_once() is None  # type: ignore[arg-type]
    assert "stage=read error=ConnectionError" in caplog.text


def test_default_publisher_reads_the_topic_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERTS_TOPIC_ARN", "topic-placeholder")
    assert JobMetricsSampler(_pool(_conn()))._publisher.configured


@pytest.mark.asyncio
async def test_run_forever_samples_at_once_then_every_period_until_stop() -> None:
    cw = FakePublisher()
    sampler = JobMetricsSampler(_pool(_conn()), every_s=0.01, publisher=cw)  # type: ignore[arg-type]
    stop = asyncio.Event()
    task = asyncio.create_task(sampler.run_forever(stop))
    for _ in range(200):
        if len(cw.calls) >= 3:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert len(cw.calls) >= 3


@pytest.mark.asyncio
async def test_run_forever_keeps_going_after_failures_and_stops_on_cancel() -> None:
    reads: list[int] = []

    async def flaky() -> Any:
        reads.append(1)
        raise OSError("down")

    sampler = JobMetricsSampler(flaky, every_s=0.01, publisher=FakePublisher())  # type: ignore[arg-type]
    task = asyncio.create_task(sampler.run_forever(asyncio.Event()))
    for _ in range(200):
        if len(reads) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(reads) >= 3


@pytest.mark.asyncio
async def test_sample_once_logs_the_reveal_peak_and_dropped_mail(caplog: pytest.LogCaptureFixture) -> None:
    """AA-937 / AA-945: the busiest member's reveals and the dropped forward-in mails stay on the sampled line."""
    import uuid

    from app.mail import metrics as mail_metrics

    sampler = JobMetricsSampler(_pool(_conn(None, [])), publisher=FakePublisher())  # type: ignore[arg-type]
    reveal_meter.METER.reset()
    tenant = uuid.uuid4()
    reveal_meter.METER.record(tenant, uuid.uuid4(), 3)
    reveal_meter.METER.record(tenant, uuid.uuid4(), 7)
    mail_metrics.DROPPED.take()
    mail_metrics.DROPPED.add(2)
    caplog.set_level(logging.INFO, logger="app.jobs.metrics")
    try:
        await sampler.sample_once()
    finally:
        reveal_meter.METER.reset()
    assert "reveal_peak=7 mail_dropped=2 checks=ok" in caplog.text
    assert mail_metrics.DROPPED.take() == 0


@pytest.mark.asyncio
async def test_flush_events_on_shutdown(caplog: pytest.LogCaptureFixture) -> None:
    """S263 review: nothing pending = no database call; pending events go to the publisher; a failure never raises."""
    ops_alerts.PENDING.drain()
    reads: list[int] = []

    async def provide() -> Any:
        reads.append(1)
        return object()

    pub = FakePublisher()
    sampler = JobMetricsSampler(provide, publisher=pub)  # type: ignore[arg-type]
    assert await sampler.flush_events() == {} and reads == []
    ops_alerts.emit("membership_apply_refused", "e-1", event_id="e-1")
    caplog.set_level(logging.INFO, logger="app.jobs.metrics")
    assert await sampler.flush_events() == {"membership_apply_refused": "sent"}
    assert [f.check for f in pub.calls[0]] == ["membership_apply_refused"]
    assert "ops_checks_flushed checks=membership_apply_refused:sent" in caplog.text

    async def no_pool() -> Any:
        raise OSError("connect refused")

    ops_alerts.emit("membership_apply_refused", "e-2", event_id="e-2")
    try:
        assert await JobMetricsSampler(no_pool, publisher=pub).flush_events() == {}  # type: ignore[arg-type]
        assert "ops_checks_flush_failed error=OSError" in caplog.text
    finally:
        ops_alerts.PENDING.drain()
