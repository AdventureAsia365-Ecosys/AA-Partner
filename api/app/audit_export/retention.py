"""``crm_audit_retention``: the daily audit retention (data standards §7 row ``crm.audit_event``; design v3 §3.6).

One run, queued due now once a day from ``JOB_RETENTION_UTC_HOUR`` (:class:`DailyAuditRetentionSchedule`, the same
pattern as ``crm_job_retention``):

1. keep the monthly partitions three months ahead (``crm.audit_ensure_partitions()``);
2. **verify** every agency from its ``verified_until`` up to its ``exported_until`` (:mod:`app.audit_export.verify`:
   database rows, chain and both archived copies); when an agency is clean, ``verified_until`` moves to the end of
   the last hour checked; a problem is logged (``audit_verify_failed``, codes only) and blocks that agency's mark;
3. **drop** the partitions that end at or before the start of the UTC month twelve months ago, only when every agency
   is verified past them (``crm.audit_drop_expired_partitions``, which checks both again). While
   ``AUDIT_RETENTION_DRY_RUN`` is on (the default, DM-7.2) it only reports ``would_drop``.

Without the buckets (dev / stg / prod) step 2 checks the database and the chain only and never moves
``verified_until``: nothing can be dropped before the archive is proven.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import asyncpg

from app.audit_export import registry
from app.audit_export.archive import AuditArchive
from app.audit_export.verify import verify_tenant
from app.jobs import queue
from app.jobs.queue import Job
from app.jobs.worker import Handler
from app.tenancy import audit_export_tx

log = logging.getLogger(__name__)

KIND = "crm_audit_retention"
MAX_ATTEMPTS = 3
HISTORY_DAYS = 30
SCHEDULER = "scheduler:crm-audit-retention"
MARK_SQL = """
UPDATE crm.audit_export_tenant SET verified_until = $2
 WHERE tenant_id = $1 AND (verified_until IS NULL OR verified_until < $2) AND $2 <= exported_until
"""


def build_handler(archive: AuditArchive | None, *, dry_run: bool) -> Handler:
    async def audit_retention(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        created = await registry.ensure_partitions(pool)
        verified = problems = 0
        for agency in await registry.agencies(pool):
            report = await verify_tenant(pool, archive, agency.tenant_id, since=agency.verified_until)
            if not report.ok:
                problems += len(report.problems)
                codes = sorted({p["code"] for p in report.problems})
                log.error("audit_verify_failed tenant_id=%s codes=%s", agency.tenant_id, ",".join(codes))
                continue
            if archive is not None and report.checked_until is not None:
                async with audit_export_tx(pool, agency.tenant_id) as ctx:
                    await ctx.conn.execute(MARK_SQL, agency.tenant_id, report.checked_until)
                verified += 1
        actions = await registry.drop_expired_partitions(pool, dry_run=dry_run)
        counts = {a: sum(1 for x in actions if x.action == a) for a in ("dropped", "would_drop", "kept_unverified")}
        for x in actions:
            log.info("audit_partition partition=%s action=%s", x.partition_name, x.action)
        return {
            "dry_run": dry_run,
            "status": "problems" if problems else "ok",
            "partition_count": created,
            "verified_count": verified,
            "problem_count": problems,
            "dropped_count": counts["dropped"],
            "would_drop_count": counts["would_drop"],
            "kept_unverified_count": counts["kept_unverified"],
        }

    return audit_retention


def schedule_key(day: date) -> str:
    return f"{KIND}:schedule:{day.isoformat()}"


class DailyAuditRetentionSchedule:
    """Worker maintenance hook: one ``crm_audit_retention`` run per UTC day, due now, from ``utc_hour``; never
    ahead."""

    def __init__(self, utc_hour: int, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if not 0 <= utc_hour <= 23:
            raise ValueError(f"utc_hour must be 0..23, got {utc_hour}")
        self._utc_hour = utc_hour
        self._now = now
        self._done_day: date | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        now = self._now().astimezone(UTC)
        today = now.date()
        if now.hour < self._utc_hour or self._done_day == today:
            return None
        job_id = await queue.enqueue(
            pool,
            KIND,
            {"purpose": "daily_schedule"},
            idempotency_key=schedule_key(today),
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, KIND, HISTORY_DAYS)
        self._done_day = today
        return job_id
