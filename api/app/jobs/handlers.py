"""The ``crm.job`` kinds of the skeleton (AA-933): the liveness heartbeat and the job-table retention.

- ``crm_heartbeat`` (every 60 s, one row per minute slot): proves the worker claims and finishes jobs. A due heartbeat
  that stays queued is the "worker stopped" signal (``OldestQueuedAgeSeconds``). Idle-exempt in DB Migrate.
- ``crm_intake_process`` (AA-936, queued by the public intake route with each stored submission): see
  :mod:`app.intake.process`.
- ``crm_job_retention`` (daily, :class:`DailyRetentionSchedule`): data standards §7 row ``crm.job`` — succeeded rows
  30 days, cancelled 90 days after ``finished_at``; queued / running never. **Failed rows are kept**: §7 says "archive
  before delete: failed rows only", and the CRM has no archive bucket yet (CRM-04 rest), so nothing that must be
  archived is deleted. A row that another job points at (``parent_job_id``) is kept. DM-7.2: the first runs are DRY RUN
  (counts only) while ``JOB_RETENTION_DRY_RUN`` is on, the default; turning it off is an ops decision recorded in the
  runbook. Deletes go in batches of :data:`BATCH_SIZE` (at most :data:`MAX_BATCHES` per run).

Why the retention kind is not a :class:`~app.jobs.worker.Recurring` kind: a Recurring kind always holds its NEXT
slot's row, queued with a future ``run_after``. The deploy gate (``queue.active_count``) and AA-CIS-Infra DB Migrate
(``db/run_migration.py`` ``JOB_BUSY_SQL``: "any queued (any run_after)") count such a row as busy, and only the
heartbeat is exempt there, so a queued-ahead daily row would block every deploy and every DB Migrate apply of every
owner. The daily run is instead queued by a maintenance hook (AA-Booking ``retention/jobs.py`` ``DailySchedule``,
AA-825) only once it is due, so the row exists only between "due" and "finished".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import asyncpg

from app.jobs import queue
from app.jobs.kinds import AUDIT_EXPORT, AUDIT_RETENTION, HEARTBEAT, INTAKE_PROCESS, JOB_RETENTION
from app.jobs.queue import Job
from app.jobs.worker import Handler, MaintenanceHook, Recurring

if TYPE_CHECKING:  # pragma: no cover
    from app.audit_export.archive import AuditArchive
    from app.vault import KeyProvider

log = logging.getLogger(__name__)

SUCCEEDED_DAYS = 30
CANCELLED_DAYS = 90
FAILED_DAYS = 90
BATCH_SIZE = 1000
MAX_BATCHES = 50
# The daily run's history (its own rows) is pruned after this many days, as the Recurring kind's keep_days was.
RETENTION_HISTORY_DAYS = 30
RETENTION_MAX_ATTEMPTS = 3
SCHEDULER = "scheduler:crm-retention"

ELIGIBLE_SQL = """
SELECT j.id FROM crm.job j
 WHERE ((j.status = 'succeeded' AND j.finished_at < now() - make_interval(days => $1))
     OR (j.status = 'cancelled' AND j.finished_at < now() - make_interval(days => $2)))
   AND NOT EXISTS (SELECT 1 FROM crm.job c WHERE c.parent_job_id = j.id)
 ORDER BY j.finished_at, j.id
 LIMIT $3
"""

COUNT_SQL = """
SELECT count(*) FILTER (WHERE status = 'succeeded' AND finished_at < now() - make_interval(days => $1))::int AS s,
       count(*) FILTER (WHERE status = 'cancelled' AND finished_at < now() - make_interval(days => $2))::int AS c,
       count(*) FILTER (WHERE status = 'failed' AND finished_at < now() - make_interval(days => $3))::int AS f
  FROM crm.job
"""


async def heartbeat(_pool: asyncpg.Pool, _job: Job) -> dict[str, Any] | None:
    return None


def build_job_retention(dry_run: bool) -> Handler:
    async def job_retention(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        counts = await pool.fetchrow(COUNT_SQL, SUCCEEDED_DAYS, CANCELLED_DAYS, FAILED_DAYS)
        result: dict[str, Any] = {
            "dry_run": dry_run,
            "succeeded_count": counts["s"],
            "cancelled_count": counts["c"],
            "failed_kept_count": counts["f"],
            "deleted_count": 0,
        }
        if dry_run:
            return result
        deleted = 0
        for _ in range(MAX_BATCHES):
            async with pool.acquire() as conn, conn.transaction():
                ids = [r["id"] for r in await conn.fetch(ELIGIBLE_SQL, SUCCEEDED_DAYS, CANCELLED_DAYS, BATCH_SIZE)]
                if not ids:
                    break
                status = await conn.execute("DELETE FROM crm.job WHERE id = ANY($1::uuid[])", ids)
                deleted += int(status.split()[-1])
            if len(ids) < BATCH_SIZE:
                break
        result["deleted_count"] = deleted
        return result

    return job_retention


def build_handlers(
    *,
    retention_dry_run: bool,
    keys: KeyProvider | None = None,
    audit_archive: AuditArchive | None = None,
    audit_retention_dry_run: bool = True,
) -> dict[str, Handler]:
    """Every kind the worker runs. ``keys``: the field-encryption provider the intake kind needs (AA-936); without
    it ``crm_intake_process`` jobs are retried until the provider is configured. ``audit_archive``: the two audit
    buckets (AA-940); without them the export keeps the partitions only and the retention never drops anything."""
    from app.audit_export import export as audit_export
    from app.audit_export import retention as audit_retention
    from app.intake.process import build_handler as build_intake_handler

    return {
        HEARTBEAT: heartbeat,
        JOB_RETENTION: build_job_retention(retention_dry_run),
        INTAKE_PROCESS: build_intake_handler(keys),
        AUDIT_EXPORT: audit_export.build_handler(audit_archive),
        AUDIT_RETENTION: audit_retention.build_handler(audit_archive, dry_run=audit_retention_dry_run),
    }


def schedule_key(day: date) -> str:
    return f"{JOB_RETENTION}:schedule:{day.isoformat()}"


class DailyRetentionSchedule:
    """Worker maintenance hook: queues one ``crm_job_retention`` run per UTC day, **due now**, on the first maintenance
    pass at or after ``utc_hour`` (``JOB_RETENTION_UTC_HOUR``). Never queues ahead: no future-dated row ever sits in
    crm.job (see the module docstring). Idempotent across workers and restarts (idempotency key per day); the
    in-memory day only saves the INSERT on later passes. Port of AA-Booking ``DailySchedule`` (AA-825)."""

    def __init__(self, utc_hour: int, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if not 0 <= utc_hour <= 23:
            raise ValueError(f"utc_hour must be 0..23, got {utc_hour}")
        self._utc_hour = utc_hour
        self._now = now
        self._done_day: date | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        now = self._now()
        today = now.date()
        if now.hour < self._utc_hour or self._done_day == today:
            return None
        job_id = await queue.enqueue(
            pool,
            JOB_RETENTION,
            {"purpose": "daily_schedule"},
            idempotency_key=schedule_key(today),
            max_attempts=RETENTION_MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, JOB_RETENTION, RETENTION_HISTORY_DAYS)
        self._done_day = today
        if job_id:
            log.info("job retention: queued the scheduled run for %s", today)
        return job_id


def build_hooks(*, retention_utc_hour: int, now: Callable[[], datetime] | None = None) -> tuple[MaintenanceHook, ...]:
    """The due-now schedules. ``now``: a fixed clock for tests (default: the real UTC clock)."""
    from app.audit_export.export import HourlyAuditExportSchedule
    from app.audit_export.retention import DailyAuditRetentionSchedule

    clock = now or (lambda: datetime.now(UTC))
    return (
        DailyRetentionSchedule(retention_utc_hour, clock),
        HourlyAuditExportSchedule(clock),
        DailyAuditRetentionSchedule(retention_utc_hour, clock),
    )


# Only the heartbeat queues its next slot ahead (it is deploy-gate and DB Migrate idle exempt). A daily or slower kind
# goes through a due-now maintenance hook instead (DailyRetentionSchedule): never add one here.
RECURRING: tuple[Recurring, ...] = (Recurring(HEARTBEAT, every_seconds=60, keep_days=2),)
SCHEDULED_KINDS: frozenset[str] = frozenset({JOB_RETENTION, AUDIT_EXPORT, AUDIT_RETENTION})
# Kinds queued by request code, one row per event (AA-936: each stored inquiry), never scheduled.
EVENT_KINDS: frozenset[str] = frozenset({INTAKE_PROCESS})
