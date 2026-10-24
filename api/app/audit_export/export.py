"""``crm_audit_export``: every agency's audit, hour by hour, to both Object Lock buckets with the hash chain (design v3
§4.9; card CRM-12).

Per agency and per closed UTC hour, in one :func:`app.tenancy.audit_export_tx` transaction:

1. lock the agency's registry row (``crm.audit_export_tenant ... FOR UPDATE``): two workers never export the same
   agency at once; the next hour is ``[exported_until, exported_until + 1 h)`` and is exported only once it ended at
   least :data:`EXPORT_LAG` ago (an event's ``at`` is its insert time, crm-007, and request transactions are short);
2. read the agency's last manifest (seq and hash) and the hour's events, ordered by ``(at, event_id)``;
3. build the object (:mod:`app.audit_export.chain`) and PUT it to the **compliance** then the **governance** bucket;
4. insert the manifest row (the crm-007 trigger refuses a row that does not continue the chain) and move the
   registry forward; commit.

A failure anywhere (an S3 outage after the first PUT, a lost connection) rolls the transaction back: no manifest, the
registry unchanged. The retry rebuilds the same hour from the same rows with the same previous hash, so it writes the
same bytes under the same key (a new identical locked version in a bucket that already had it) and the chain goes on
without a gap or a fork (card E test). Empty hours are exported too (``event_count`` 0): every hour of an agency is
covered by exactly one manifest, which is what lets verification prove no row was added or removed later.

Scheduling: :class:`HourlyAuditExportSchedule` queues one run per UTC hour, due now, once the hour is
:data:`EXPORT_LAG` + 1 minute old (never ahead: see :mod:`app.jobs.handlers`). One run also keeps the monthly
partitions three months ahead (``crm.audit_ensure_partitions()``) and catches up at most :data:`MAX_HOURS_PER_RUN`
hours per agency. Without the buckets (dev / stg / prod) it only keeps the partitions and returns ``not_configured``:
the ``AuditExportLagMinutes`` metric then grows and its alarm fires.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from app.audit_export import registry
from app.audit_export.archive import TIERS, AuditArchive
from app.audit_export.chain import PERIOD, ZERO_HASH, ExportObject, build_object
from app.jobs import queue
from app.jobs.queue import Job
from app.jobs.worker import Handler
from app.tenancy import ExportContext, audit_export_tx

log = logging.getLogger(__name__)

KIND = "crm_audit_export"
EXPORT_LAG = timedelta(minutes=15)
MAX_HOURS_PER_RUN = 48
SCHEDULE_MINUTE = int(EXPORT_LAG.total_seconds() // 60) + 1
MAX_ATTEMPTS = 3
HISTORY_DAYS = 7
SCHEDULER = "scheduler:crm-audit-export"

REGISTRY_LOCK_SQL = "SELECT exported_until, last_seq FROM crm.audit_export_tenant WHERE tenant_id = $1 FOR UPDATE"
LAST_MANIFEST_SQL = """
SELECT seq, manifest_sha256, period_end FROM crm.audit_export_manifest ORDER BY seq DESC LIMIT 1
"""
EVENTS_SQL = """
SELECT event_id, tenant_id, at, actor_kind, actor_ref, action, outcome, object_kind, object_id, fields, reason,
       request_id, object_ids, created_by
  FROM crm.audit_event
 WHERE at >= $1 AND at < $2
 ORDER BY at, event_id
"""
INSERT_MANIFEST_SQL = """
INSERT INTO crm.audit_export_manifest (seq, period_start, period_end, event_count, content_sha256,
                                       prev_manifest_sha256, manifest_sha256, object_key, object_sha256)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""
ADVANCE_SQL = "UPDATE crm.audit_export_tenant SET exported_until = $2, last_seq = $3 WHERE tenant_id = $1"


class ChainStateMismatch(RuntimeError):
    """The registry and the manifest table disagree (seq or period): never export over it."""


async def read_events(ctx: ExportContext, start: datetime, end: datetime) -> list[asyncpg.Record]:
    return await ctx.conn.fetch(EVENTS_SQL, start, end)


async def export_next_hour(ctx: ExportContext, archive: AuditArchive, now: datetime) -> ExportObject | None:
    """Export the agency's next closed hour, or return None when it is not closed yet (or the agency is not in the
    registry). Raises on any failure; the caller's transaction then rolls back."""
    reg = await ctx.conn.fetchrow(REGISTRY_LOCK_SQL, ctx.tenant_id)
    if reg is None:
        return None
    start = reg["exported_until"]
    end = start + PERIOD
    if end > now - EXPORT_LAG:
        return None
    last = await ctx.conn.fetchrow(LAST_MANIFEST_SQL)
    last_seq = last["seq"] if last else 0
    if reg["last_seq"] != last_seq or (last is not None and last["period_end"] != start):
        raise ChainStateMismatch("registry and manifests disagree")
    obj = build_object(
        ctx.tenant_id,
        last_seq + 1,
        start,
        end,
        await read_events(ctx, start, end),
        last["manifest_sha256"] if last else ZERO_HASH,
    )
    for tier in TIERS:
        await archive.put(tier, obj.object_key, obj.body)
    await ctx.conn.execute(
        INSERT_MANIFEST_SQL,
        obj.seq,
        obj.period_start,
        obj.period_end,
        obj.event_count,
        obj.content_sha256,
        obj.prev_manifest_sha256,
        obj.manifest_sha256,
        obj.object_key,
        obj.object_sha256,
    )
    await ctx.conn.execute(ADVANCE_SQL, ctx.tenant_id, obj.period_end, obj.seq)
    return obj


async def export_tenant(
    pool: asyncpg.Pool,
    archive: AuditArchive,
    tenant_id: uuid.UUID,
    *,
    now: datetime | None = None,
    max_hours: int = MAX_HOURS_PER_RUN,
) -> int:
    """Export every closed hour of one agency (at most ``max_hours``), one transaction per hour. Returns the count."""
    now = now or datetime.now(UTC)
    done = 0
    while done < max_hours:
        async with audit_export_tx(pool, tenant_id) as ctx:
            obj = await export_next_hour(ctx, archive, now)
        if obj is None:
            break
        done += 1
    return done


def build_handler(archive: AuditArchive | None, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> Handler:
    async def audit_export(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        created = await registry.ensure_partitions(pool)
        if archive is None:
            return {"status": "not_configured", "partition_count": created}
        exported = failed = 0
        agencies = await registry.agencies(pool)
        for agency in agencies:
            try:
                exported += await export_tenant(pool, archive, agency.tenant_id, now=now())
            except Exception as exc:  # noqa: BLE001 — one agency's failure must not stop the others
                failed += 1
                log.error("audit_export_failed tenant_id=%s error=%s", agency.tenant_id, type(exc).__name__)
        result = {
            "status": "partial" if failed else "ok",
            "tenant_count": len(agencies),
            "exported_count": exported,
            "failed_count": failed,
            "partition_count": created,
        }
        if failed:
            # The job is retried (backoff); the agencies that succeeded already moved on, the failed ones resume.
            raise RuntimeError(f"audit export failed for {failed} agencies")
        return result

    return audit_export


def schedule_key(hour: datetime) -> str:
    return f"{KIND}:schedule:{hour:%Y-%m-%dT%H}"


class HourlyAuditExportSchedule:
    """Worker maintenance hook: one ``crm_audit_export`` run per UTC hour, queued **due now** from minute
    :data:`SCHEDULE_MINUTE` (the previous hour is then past :data:`EXPORT_LAG`). Idempotent per hour (idempotency key);
    never queues ahead (a future-dated row would block every deploy and DB Migrate, see ``app.jobs.handlers``)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_hour: datetime | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        now = self._now().astimezone(UTC)
        hour = now.replace(minute=0, second=0, microsecond=0)
        if now.minute < SCHEDULE_MINUTE or self._done_hour == hour:
            return None
        job_id = await queue.enqueue(
            pool,
            KIND,
            {"purpose": "hourly_schedule", "slot": f"{hour:%Y-%m-%dT%H}"},
            idempotency_key=schedule_key(hour),
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, KIND, HISTORY_DAYS)
        self._done_hour = hour
        return job_id
