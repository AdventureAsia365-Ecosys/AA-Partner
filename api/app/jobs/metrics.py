"""``crm.job`` backlog metrics for CloudWatch. Ported from AA-Booking ``app/jobs/metrics.py`` (AA-751 B5, AA-832).

:class:`JobMetricsSampler` is one asyncio task of the API process, started by the lifespan next to the worker (only
when the worker is enabled and the database is configured) and independent of it: the sample never goes through
``crm.job`` itself, so a stuck or dead worker loop still shows. Every ``every_s`` seconds (default 300) it reads
two numbers and sends them in **one** ``PutMetricData`` call to namespace ``AA/Jobs/Crm`` (no dimensions):

- ``OldestQueuedAgeSeconds`` (Seconds): ``now() - run_after`` of the oldest **due** queued row (``run_after <= now()``,
  no cancel request: the claim never takes one), any kind, so a kind no worker handles shows too. It is **0 while a
  non-overdue job is running**: the worker is serial, so a due row waiting behind a healthy job is normal; an
  unhealthy one shows as overdue instead. 0 when nothing is due.
- ``OverdueRunningJobs`` (Count): running rows whose current attempt
  (``coalesce(progress->>'attempt_started_at', started_at)``, written by every claim since AA-751 B2) has run longer
  than :func:`app.jobs.kinds.overdue_after_seconds` (2 x ``EXPECTED_SECONDS[kind]``, default 300 s).

- ``RevealsPerMemberPeakHour`` (Count, AA-937): the highest number of field reveals / document downloads one member
  made in the last hour in this process (:mod:`app.observability.reveal_meter`, design §4.4 bulk reveal).
- ``AuditExportLagMinutes`` (Count, AA-940): minutes since the end of the oldest agency's last exported audit hour
  (``crm.audit_export_agencies()``; 0 without agencies). Healthy is under about 75 (the hour, the 15-minute lag, the
  hourly schedule); Infra alarms at 180.
- ``AuditPartitionMonthsAhead`` (Count, AA-940): whole months after the current UTC month that already have an audit
  partition (``crm.audit_partition_bounds()``; 3 after the hourly job). Infra alarms below 1: without a partition the
  next month's audit writes, and so the requests that write them, fail.
- ``AuditVerifyLagHours`` (Count, AA-940 review): the largest gap, over agencies, between what is exported
  (``exported_until``) and what the daily verification confirmed (``verified_until``, or the hour the agency was
  registered while it was never verified); 0 when the verification is level with the export. Healthy is under about
  26 (one daily run); Infra alarms at 50. One agency that keeps failing its verification stops partition drops for
  every agency (the drop needs all of them verified), so this is the alarm for that, not the job log line.
- ``MailIngestDropped`` (Count, AA-945): forwarded mails discarded since the last sample because their forward-in
  address is unknown or inactive (card R: nothing is stored; :mod:`app.mail.metrics`).

The three audit values are read in their own statement; before crm-007 (or on any error there) they are left out and the
job values still go.

Infra alarms on them (``aa-crm-<env>-jobs-queue-age`` > 900 s, ``-jobs-overdue-running`` > 0, the same pair as
Booking) and the task role may put metrics into ``AA/Jobs/Crm`` only (IAM condition ``cloudwatch:namespace``;
AA-CIS-Infra ``accounts/aa365/crm_app.tf``). The kind ``crm_heartbeat`` is due every minute, so a stopped worker
also shows as a growing ``OldestQueuedAgeSeconds``.

Never raises: a failed read or put is logged and the next tick tries again. boto3 is blocking, so the put runs in a
thread with short timeouts and at most one retry (botocore's ``retries={"max_attempts": 1}`` counts retries after
the first request, so a failing put is sent at most twice: worst case about (3 s + 5 s) x 2 plus backoff, ~16-17 s, in
the thread, holding no DB connection, well under the 300 s period); the next tick tries again. boto3 is imported
lazily.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import asyncpg

from app.jobs.kinds import overdue_after_seconds
from app.mail import metrics as mail_metrics
from app.observability import reveal_meter

log = logging.getLogger(__name__)

NAMESPACE = "AA/Jobs/Crm"
SAMPLE_EVERY_S = 300.0
# acc2 region: the alarms live in us-west-1.
CLOUDWATCH_REGION = "us-west-1"

PoolProvider = Callable[[], Awaitable[asyncpg.Pool]]

OLDEST_DUE_QUEUED_SQL = """
SELECT extract(epoch FROM now() - min(run_after))::float8
FROM crm.job
WHERE status = 'queued' AND run_after <= now() AND NOT cancel_requested
"""

RUNNING_SQL = """
SELECT kind,
       extract(epoch FROM now() - coalesce((progress->>'attempt_started_at')::timestamptz, started_at))::float8
           AS attempt_age_s
FROM crm.job
WHERE status = 'running'
"""


class CloudWatchClient(Protocol):
    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]: ...


ClientFactory = Callable[[], CloudWatchClient]


def cloudwatch_client() -> CloudWatchClient:
    import boto3  # lazy: only the sampler needs it at run time
    from botocore.config import Config

    # botocore "max_attempts" = retries after the first request: 1 means at most 2 sends (see module docstring).
    config = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
    return boto3.client("cloudwatch", region_name=CLOUDWATCH_REGION, config=config)


@dataclass(frozen=True)
class Sample:
    oldest_queued_age_s: float
    overdue_running: int
    running: int
    due_queued_age_s: float  # raw age of the oldest due queued row, before the "healthy job running" rule


def backlog_sample(oldest_due_age_s: float | None, running: Iterable[tuple[str, float | None]]) -> Sample:
    """The two metric values from the raw reads (pure).

    ``running`` is ``(kind, attempt age in seconds)`` per running row; an unknown age (no start time at all) counts as
    healthy, never as overdue."""
    rows = list(running)
    overdue = sum(1 for kind, age in rows if age is not None and age > overdue_after_seconds(kind))
    healthy_running = len(rows) - overdue > 0
    due_age = max(float(oldest_due_age_s or 0.0), 0.0)
    return Sample(
        oldest_queued_age_s=0.0 if healthy_running else due_age,
        overdue_running=overdue,
        running=len(rows),
        due_queued_age_s=due_age,
    )


AUDIT_SQL = """
SELECT coalesce(extract(epoch FROM now() - (SELECT min(exported_until) FROM crm.audit_export_agencies())) / 60,
                0)::float8 AS export_lag_min,
       coalesce((SELECT max(extract(epoch FROM a.exported_until
                                    - coalesce(a.verified_until, date_trunc('hour', a.registered_at))))
                   FROM crm.audit_export_agencies() a) / 3600, 0)::float8 AS verify_lag_h,
       (SELECT count(*) FROM crm.audit_partition_bounds()
         WHERE range_start >= (date_trunc('month', now() AT TIME ZONE 'UTC') + interval '1 month') AT TIME ZONE 'UTC'
       )::int AS months_ahead
"""


@dataclass(frozen=True)
class AuditSample:
    export_lag_minutes: float
    partition_months_ahead: int
    verify_lag_hours: float = 0.0


def metric_data(
    sample: Sample, reveal_peak: int | None = None, audit: AuditSample | None = None, mail_dropped: int | None = None
) -> list[dict[str, Any]]:
    data = [
        {"MetricName": "OldestQueuedAgeSeconds", "Value": sample.oldest_queued_age_s, "Unit": "Seconds"},
        {"MetricName": "OverdueRunningJobs", "Value": float(sample.overdue_running), "Unit": "Count"},
    ]
    if mail_dropped is not None:
        data.append({"MetricName": "MailIngestDropped", "Value": float(mail_dropped), "Unit": "Count"})
    if reveal_peak is not None:
        data.append({"MetricName": "RevealsPerMemberPeakHour", "Value": float(reveal_peak), "Unit": "Count"})
    if audit is not None:
        data.append(
            {"MetricName": "AuditExportLagMinutes", "Value": max(audit.export_lag_minutes, 0.0), "Unit": "Count"}
        )
        data.append(
            {"MetricName": "AuditPartitionMonthsAhead", "Value": float(audit.partition_months_ahead), "Unit": "Count"}
        )
        data.append({"MetricName": "AuditVerifyLagHours", "Value": max(audit.verify_lag_hours, 0.0), "Unit": "Count"})
    return data


async def read_audit_sample(pool: asyncpg.Pool) -> AuditSample | None:
    """The two audit values, or None when they cannot be read (crm-007 not applied yet)."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(AUDIT_SQL)
        return AuditSample(
            export_lag_minutes=float(row["export_lag_min"]),
            partition_months_ahead=int(row["months_ahead"]),
            verify_lag_hours=float(row["verify_lag_h"]),
        )
    except Exception as exc:  # noqa: BLE001 — never block the job metrics
        log.warning("ops_metrics_audit_read_failed error=%s", type(exc).__name__)
        return None


async def read_sample(pool: asyncpg.Pool) -> Sample:
    """Two SELECTs on one connection; the app pool's ``command_timeout`` (5 s, ``app.db``) bounds each."""
    async with pool.acquire() as conn:
        oldest = await conn.fetchval(OLDEST_DUE_QUEUED_SQL)
        rows = await conn.fetch(RUNNING_SQL)
    return backlog_sample(oldest, [(r["kind"], r["attempt_age_s"]) for r in rows])


class JobMetricsSampler:
    def __init__(
        self,
        pool: PoolProvider,
        every_s: float = SAMPLE_EVERY_S,
        *,
        client_factory: ClientFactory = cloudwatch_client,
    ) -> None:
        self._pool = pool
        self._every_s = every_s
        self._client_factory = client_factory
        self._client: CloudWatchClient | None = None

    def _put(self, data: list[dict[str, Any]]) -> None:
        # Runs in a worker thread: creating the boto3 client reads credentials, so it is created here, once.
        if self._client is None:
            self._client = self._client_factory()
        self._client.put_metric_data(Namespace=NAMESPACE, MetricData=data)

    async def sample_once(self) -> Sample | None:
        """Read and publish one sample. Returns it when it was published, else None. Never raises."""
        try:
            pool = await self._pool()
            sample = await read_sample(pool)
        except Exception as exc:  # noqa: BLE001 — DB down or not migrated: no datapoint (sampling-stopped alarm)
            log.warning("ops_metrics_sample_failed namespace=%s stage=read error=%s", NAMESPACE, type(exc).__name__)
            return None
        audit = await read_audit_sample(pool)
        try:
            dropped = mail_metrics.DROPPED.take()
            await asyncio.to_thread(self._put, metric_data(sample, reveal_meter.METER.peak(), audit, dropped))
        except Exception as exc:  # noqa: BLE001 — AccessDenied before the Infra apply, throttling, network
            log.warning(
                "ops_metrics_sample_failed namespace=%s stage=put error=%s: %s",
                NAMESPACE,
                type(exc).__name__,
                str(exc)[:300],
            )
            return None
        log.info(
            "ops_metrics_sampled namespace=%s oldest_queued_age_s=%.0f overdue_running_jobs=%d running=%d"
            " due_queued_age_s=%.0f",
            NAMESPACE,
            sample.oldest_queued_age_s,
            sample.overdue_running,
            sample.running,
            sample.due_queued_age_s,
        )
        return sample

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Sample at once, then every ``every_s`` seconds until ``stop`` is set (or the task is cancelled)."""
        log.info("job metrics sampler started (namespace %s, every %.0fs)", NAMESPACE, self._every_s)
        while not stop.is_set():
            await self.sample_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._every_s)
        log.info("job metrics sampler stopped")
