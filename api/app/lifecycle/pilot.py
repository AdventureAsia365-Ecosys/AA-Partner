"""The CRM's pilot-agency protections (AA-969 part 2, root ADR 0019 d16).

ADR 0019 d16 lets one real agency (the pilot) use Dev for at most 8 weeks. The CIS tenant master marks it
(``shared.tenants.is_pilot``, CIS 354, at most one); ``crm_entitlement_pull`` projects the marker onto
``crm.tenant.is_pilot`` (crm-044; only that actor may write it, ``crm.guard_pilot_marker`` CRM04). Two rules use it
here:

1. **Clean-up skips the pilot.** :func:`is_pilot` is read inside the agency's own transaction (FORCE RLS) by every path
   that deletes, redacts, archives or rewrites agency data on a schedule or for tests: the daily ``crm_lifecycle`` run
   (:mod:`app.lifecycle.job`: retention purges, the trash purge, the auto-archive) leaves the pilot agency out (counted
   ``pilot_skipped``), and the L1 console's test-agency delete (:func:`app.ops.console.delete_test_agency`, the E2E
   clean-up) refuses it (403 ``pilot_agency``). What the agency's own people ask for (an owner's erasure, offboarding,
   exports, imports) is theirs and is never skipped.
2. **A nightly export of the pilot alone** (``crm_pilot_backup``): once per UTC day, from ``JOB_RETENTION_UTC_HOUR``
   (:class:`DailyPilotBackupSchedule`, due now, never queued ahead), the job looks for the marked agency and queues one
   portability export of it: a ``crm.export_job`` row (scope ``tenant``, ``requested_by`` = :data:`ACTOR`) and its
   ``crm_data_export`` job, the same builder and seal as an owner's export (:mod:`app.portability.exports`): every
   tenant table as JSON + CSV, sealed under the agency ``export`` DEK at
   ``exports/tenants/<tenant>/portability/<export>.zip.enc`` in the documents bucket. The ``exports/`` bucket rule
   deletes the object after 7 days (root data standards DM-7.1, the ``crm.export_job`` row), so the last 7 nights are
   kept. An export of the agency already being built is not doubled (``busy``, the next night tries again). These rows
   are left out of the owner's export list (they are AA's restore copies, not the owner's downloads); an erasure
   withdraws them like any other export. ``app.portability.restore_drill`` restores one of them into a throwaway schema
   and compares row counts (root ``docs/ops/runbooks/pilot-tenant-restore-drill.md``).

Payloads and results are ids and counts only.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import asyncpg

from app.audit_export import registry
from app.errors import Forbidden, NotFound
from app.jobs import queue
from app.jobs.kinds import DATA_EXPORT, PILOT_BACKUP
from app.jobs.queue import Job
from app.jobs.worker import Handler
from app.portability import builder, exports
from app.tenancy import LifecycleContext, lifecycle_tx

log = logging.getLogger(__name__)

ACTOR = exports.PILOT_BACKUP_ACTOR  # "job:crm_pilot_backup"
SCHEDULER = "scheduler:crm-pilot-backup"
MAX_ATTEMPTS = 3
HISTORY_DAYS = 30
IS_PILOT_SQL = "SELECT is_pilot FROM crm.tenant WHERE tenant_id = $1"


async def is_pilot(ctx: LifecycleContext) -> bool:
    """True when the context's agency is the pilot agency (read in its own transaction: RLS shows its row only)."""
    return bool(await ctx.conn.fetchval(IS_PILOT_SQL, ctx.tenant_id))


async def queue_backup(ctx: LifecycleContext) -> str:
    """Queue one export of the agency for the nightly backup: ``queued`` or ``busy`` (an export of the agency is
    already queued or being built)."""
    try:
        async with ctx.conn.transaction():
            export_id = await ctx.conn.fetchval(
                exports.INSERT_SQL, "tenant", ctx.tenant_id, None, builder.SCHEMA_VERSION
            )
    except asyncpg.UniqueViolationError:
        return "busy"
    await queue.enqueue(
        ctx.conn,
        DATA_EXPORT,
        {"tenant_id": str(ctx.tenant_id), "export_id": str(export_id)},
        idempotency_key=f"{DATA_EXPORT}:{export_id}",
        max_attempts=exports.MAX_ATTEMPTS,
        created_by="pilot_backup",
    )
    ctx.audit("export_request", "allowed", "export_job", str(export_id), reason="pilot_backup")
    return "queued"


async def pilot_agencies(pool: asyncpg.Pool) -> list[uuid.UUID]:
    """The marked agencies among the active / locked agencies of the registry (at most one: crm-044's index)."""
    found: list[uuid.UUID] = []
    for agency in await registry.agencies(pool):
        try:
            async with lifecycle_tx(pool, agency.tenant_id, actor=ACTOR) as ctx:
                if await is_pilot(ctx):
                    found.append(ctx.tenant_id)
        except (Forbidden, NotFound):
            continue  # offboarding / offboarded / gone: not served, nothing to back up
    return found


def build_handler() -> Handler:
    async def pilot_backup(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        counts = {"pilot_count": 0, "queued_count": 0, "busy_count": 0}
        for tenant_id in await pilot_agencies(pool):
            counts["pilot_count"] += 1
            async with lifecycle_tx(pool, tenant_id, actor=ACTOR) as ctx:
                outcome = await queue_backup(ctx)
            counts[f"{outcome}_count"] += 1
            log.info("crm_pilot_backup tenant_id=%s outcome=%s", tenant_id, outcome)
        if not counts["pilot_count"]:
            log.info("crm_pilot_backup pilot_count=0")
        return counts

    return pilot_backup


def schedule_key(day: date) -> str:
    return f"{PILOT_BACKUP}:schedule:{day.isoformat()}"


class DailyPilotBackupSchedule:
    """Worker maintenance hook: one ``crm_pilot_backup`` run per UTC day, due now, from ``utc_hour``; never queued
    ahead (the ``DailyLifecycleSchedule`` pattern)."""

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
            PILOT_BACKUP,
            {"purpose": "daily_schedule"},
            idempotency_key=schedule_key(today),
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, PILOT_BACKUP, HISTORY_DAYS)
        self._done_day = today
        return job_id
