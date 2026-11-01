"""``crm.job`` backlog sample, the audit export read and the in-app ops checks (AA-751 port; S263 alert diet, AA-1025).

:class:`JobMetricsSampler` is one asyncio task of the API process, started by the lifespan next to the worker (only
when the worker is enabled and the database is configured) and independent of it: the sample never goes through
``crm.job`` itself, so a stuck or dead worker loop still shows. Every ``every_s`` seconds (default 300) it reads:

- ``oldest_queued_age_s``: ``now() - run_after`` of the oldest **due** queued row (``run_after <= now()``, no cancel
  request: the claim never takes one), any kind, so a kind no worker handles shows too. It is **0 while a non-overdue
  job is running**: the worker is serial, so a due row waiting behind a healthy job is normal; an unhealthy one shows
  as overdue instead. 0 when nothing is due. The kind ``crm_heartbeat`` is due every minute, so a stopped worker also
  shows as a growing age.
- ``overdue_running``: running rows whose current attempt (``coalesce(progress->>'attempt_started_at', started_at)``,
  written by every claim since AA-751 B2) has run longer than :func:`app.jobs.kinds.overdue_after_seconds`
  (2 x ``EXPECTED_SECONDS[kind]``, default 300 s).
- the audit export state (AA-940; ``crm.audit_export_agencies()`` / ``crm.audit_partition_bounds()``, own statement,
  left out before crm-007 or on any error): minutes since the oldest agency's last exported hour, whole months ahead
  that already have a partition, and the largest export-to-verification gap in hours.

S263: these were CloudWatch custom metrics (``AA/Jobs/Crm``, also ``RevealsPerMemberPeakHour`` and
``MailIngestDropped``) with Infra alarms. Nothing is published to CloudWatch any more: each tick hands the read and the
events raised since the last tick to :func:`app.observability.ops_alerts.run_checks`, which e-mails AA ops on the
alerts topic once per finding. The reveal peak and the dropped forward-in mails are still logged on the
``ops_metrics_sampled`` line.

Never raises: a failed read or alert is logged and the next tick tries again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

import asyncpg

from app.jobs.kinds import overdue_after_seconds
from app.mail import metrics as mail_metrics
from app.observability import ops_alerts, reveal_meter

log = logging.getLogger(__name__)

SAMPLE_EVERY_S = 300.0

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
        publisher: ops_alerts.AlertPublisher | None = None,
    ) -> None:
        self._pool = pool
        self._every_s = every_s
        self._publisher = publisher if publisher is not None else ops_alerts.AlertPublisher()

    async def sample_once(self) -> Sample | None:
        """Read one sample and run the ops checks on it. Returns the sample, or None when it could not be read.
        Never raises."""
        try:
            pool = await self._pool()
            sample = await read_sample(pool)
        except Exception as exc:  # noqa: BLE001 — DB down or not migrated: nothing to check this tick
            log.warning("ops_metrics_sample_failed stage=read error=%s", type(exc).__name__)
            return None
        audit = await read_audit_sample(pool)
        checks = await ops_alerts.run_checks(pool, self._publisher, sample, audit)
        log.info(
            "ops_metrics_sampled oldest_queued_age_s=%.0f overdue_running_jobs=%d running=%d due_queued_age_s=%.0f"
            " reveal_peak=%d mail_dropped=%d checks=%s",
            sample.oldest_queued_age_s,
            sample.overdue_running,
            sample.running,
            sample.due_queued_age_s,
            reveal_meter.METER.peak(),
            mail_metrics.DROPPED.take(),
            ",".join(f"{k}:{v}" for k, v in sorted(checks.items())) or "ok",
        )
        return sample

    async def flush_events(self) -> dict[str, str]:
        """Send the events raised since the last tick (S263 review: called once by the lifespan on shutdown).
        No database call when nothing is pending. Never raises."""
        if not ops_alerts.PENDING:
            return {}
        try:
            pool = await self._pool()
            results = await ops_alerts.flush_events(pool, self._publisher)
        except Exception as exc:  # noqa: BLE001 — shutdown goes on; the log lines stay
            log.warning("ops_checks_flush_failed error=%s", type(exc).__name__)
            return {}
        if results:
            log.warning("ops_checks_flushed checks=%s", ",".join(f"{k}:{v}" for k, v in sorted(results.items())))
        return results

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Sample at once, then every ``every_s`` seconds until ``stop`` is set (or the task is cancelled)."""
        log.info("job checks sampler started (every %.0fs)", self._every_s)
        while not stop.is_set():
            await self.sample_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._every_s)
        log.info("job checks sampler stopped")
