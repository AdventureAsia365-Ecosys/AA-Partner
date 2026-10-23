"""The ``crm.job`` kinds of the skeleton (AA-933): the liveness heartbeat and the job-table retention.

- ``crm_heartbeat`` (every 60 s, one row per minute slot): proves the worker claims and finishes jobs. A due heartbeat
  that stays queued is the "worker stopped" signal (``OldestQueuedAgeSeconds``). Idle-exempt in DB Migrate.
- ``crm_job_retention`` (daily): data standards §7 row ``crm.job`` — succeeded rows 30 days, cancelled 90 days after
  ``finished_at``; queued / running never. **Failed rows are kept**: §7 says "archive before delete: failed rows
  only", and the CRM has no archive bucket yet (CRM-04 rest), so nothing that must be archived is deleted. A row that
  another job points at (``parent_job_id``) is kept. DM-7.2: the first runs are DRY RUN (counts only) while
  ``JOB_RETENTION_DRY_RUN`` is on, the default; turning it off is an ops decision recorded in the runbook.
  Deletes go in batches of :data:`BATCH_SIZE` (at most :data:`MAX_BATCHES` per run).
"""

from __future__ import annotations

from typing import Any

import asyncpg

from app.jobs.kinds import HEARTBEAT, JOB_RETENTION
from app.jobs.queue import Job
from app.jobs.worker import Handler, Recurring

SUCCEEDED_DAYS = 30
CANCELLED_DAYS = 90
FAILED_DAYS = 90
BATCH_SIZE = 1000
MAX_BATCHES = 50

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


def build_handlers(*, retention_dry_run: bool) -> dict[str, Handler]:
    return {HEARTBEAT: heartbeat, JOB_RETENTION: build_job_retention(retention_dry_run)}


RECURRING: tuple[Recurring, ...] = (
    Recurring(HEARTBEAT, every_seconds=60, keep_days=2),
    Recurring(JOB_RETENTION, every_seconds=86_400, keep_days=30),
)
