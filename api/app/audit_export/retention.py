"""``crm_audit_retention``: the daily audit retention (data standards §7 row ``crm.audit_event``; design v3 §3.6).

One run, queued due now once a day from ``JOB_RETENTION_UTC_HOUR`` (:class:`DailyAuditRetentionSchedule`, the same
pattern as ``crm_job_retention``):

1. keep the monthly partitions three months ahead (``crm.audit_ensure_partitions()``);
2. **verify** every agency from its ``verified_until`` up to its ``exported_until`` (:mod:`app.audit_export.verify`:
   database rows, chain and both archived copies); when an agency is clean, ``verified_until`` moves to the end of
   the last hour checked; a problem is logged (``audit_verify_failed``, codes only) and blocks that agency's mark;
3. **drop** the partitions that end at or before the start of the UTC month twelve months ago, only when every agency
   is verified past them (``crm.audit_drop_expired_partitions``, which checks both again). While
   ``AUDIT_RETENTION_DRY_RUN`` is on (the default, DM-7.2) it only reports ``would_drop``;
4. **one agency audit event per run per agency** (design §4.9 "Bulk jobs write one event per run per tenant"): actor
   ``system`` / ``job:crm_audit_retention``, action ``audit_retention``, object ``audit_export``, reason ``verified``,
   ``verify_failed`` or ``database_only`` (no buckets), and the partitions really dropped in ``object_ids``. Only for
   an active agency: a locked or trashed one is on its way out (offboarding path, crm-007), and a daily row would keep
   its last partition, and so its tenant row, forever.

``verified_until`` moves only through ``crm.audit_mark_verified()`` (crm_app has no UPDATE on that column): forward
only, never past ``exported_until``.

Without the buckets (dev / stg / prod) step 2 checks the database and the chain only and never moves
``verified_until``: nothing can be dropped before the archive is proven.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import asyncpg

from app.audit import INSERT_SQL, MAX_OBJECT_IDS, AuditEvent
from app.audit_export import registry
from app.audit_export.archive import AuditArchive
from app.audit_export.verify import verify_tenant
from app.errors import NotFound
from app.jobs import queue
from app.jobs.queue import Job
from app.jobs.worker import Handler
from app.tenancy import RETENTION_ACTOR, audit_export_tx

log = logging.getLogger(__name__)

KIND = "crm_audit_retention"
MAX_ATTEMPTS = 3
HISTORY_DAYS = 30
SCHEDULER = "scheduler:crm-audit-retention"
MARK_SQL = "SELECT crm.audit_mark_verified($1)"
ACTIVE_SQL = "SELECT 1 FROM crm.tenant WHERE tenant_id = $1 AND status = 'active' AND deleted_at IS NULL"
EVENT_ACTION = "audit_retention"
EVENT_OBJECT = "audit_export"


def run_event(reason: str, dropped: tuple[str, ...]) -> AuditEvent:
    """The per-agency event of one run (codes and partition names only)."""
    return AuditEvent(
        actor_kind="system",
        actor_ref=RETENTION_ACTOR,
        action=EVENT_ACTION,
        outcome="allowed",
        object_kind=EVENT_OBJECT,
        reason=reason,
        object_ids=dropped[:MAX_OBJECT_IDS],
    )


async def record_run(pool: asyncpg.Pool, tenant_id: Any, reason: str, dropped: tuple[str, ...]) -> bool:
    """Write the run's event in an active agency. Returns False (nothing written) for a locked, trashed or deleted
    one."""
    try:
        async with audit_export_tx(pool, tenant_id, actor=RETENTION_ACTOR) as ctx:
            if await ctx.conn.fetchval(ACTIVE_SQL, tenant_id) is None:
                return False
            await ctx.conn.execute(INSERT_SQL, *run_event(reason, dropped).row())
    except NotFound:
        return False
    return True


def build_handler(archive: AuditArchive | None, *, dry_run: bool) -> Handler:
    async def audit_retention(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        created = await registry.ensure_partitions(pool)
        verified = problems = 0
        reasons: dict[Any, str] = {}
        for agency in await registry.agencies(pool):
            report = await verify_tenant(pool, archive, agency.tenant_id, since=agency.verified_until)
            if not report.ok:
                problems += len(report.problems)
                codes = sorted({p["code"] for p in report.problems})
                log.error("audit_verify_failed tenant_id=%s codes=%s", agency.tenant_id, ",".join(codes))
                reasons[agency.tenant_id] = "verify_failed"
                continue
            if archive is None:
                reasons[agency.tenant_id] = "database_only"
                continue
            if report.checked_until is not None:
                async with audit_export_tx(pool, agency.tenant_id, actor=RETENTION_ACTOR) as ctx:
                    await ctx.conn.execute(MARK_SQL, report.checked_until)
                verified += 1
            reasons[agency.tenant_id] = "verified"
        actions = await registry.drop_expired_partitions(pool, dry_run=dry_run)
        counts = {a: sum(1 for x in actions if x.action == a) for a in ("dropped", "would_drop", "kept_unverified")}
        for x in actions:
            log.info("audit_partition partition=%s action=%s", x.partition_name, x.action)
        dropped = tuple(x.partition_name for x in actions if x.action == "dropped")
        recorded = 0
        for tenant_id, reason in reasons.items():
            recorded += await record_run(pool, tenant_id, reason, dropped)
        return {
            "dry_run": dry_run,
            "status": "problems" if problems else "ok",
            "partition_count": created,
            "verified_count": verified,
            "problem_count": problems,
            "dropped_count": counts["dropped"],
            "would_drop_count": counts["would_drop"],
            "kept_unverified_count": counts["kept_unverified"],
            "event_count": recorded,
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
