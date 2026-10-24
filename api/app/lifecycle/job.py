"""``crm_lifecycle``: the daily data lifecycle run of every agency (AA-942; design §3.5, §3.6, R21, R23).

Queued **due now** once per UTC day from ``JOB_RETENTION_UTC_HOUR`` by :class:`DailyLifecycleSchedule` (never queued
ahead: the deploy gate and the DB Migrate idle check count a queued row as busy). For every agency of the registry
(active or locked; an offboarding / offboarded one is the offboarding job's), in one transaction per agency:

1. **Form submissions** past ``redact_after`` (90 days): payload redacted (``payload_enc`` NULL, row kept), unless the
   row is held (crm-010 hold rule 4d, ``crm.hold_row``: its trip or its traveller is held). A hold placed between the
   check and the update makes the hold guard trigger refuse it (rule 4h, CRM10): the agency counts as failed and the
   next run retries. (No table lock here: the SHARE lock is only taken by ``crm.erase_subject``.)
2. **LLM calls** older than 180 days: deleted (``crm.lifecycle_purge_expired``; the daily rollup stays), with the
   crm-009 placeholder maps they still have, not under an agency hold.
3. **Trash** past ``purge_after``: relationships, tasks, notes, documents, trips, then clients, each through
   ``crm.erase_subject`` in its own savepoint (a hold, a delete block or a client still on a live trip keeps that one
   item and is counted).
4. **Auto-archive**: a client with no trip activity for ``auto_archive_months`` and no open trip is archived with
   reason ``auto_inactive``; the owner then decides on Settings > Data and privacy whether to delete it.
5. One ``crm.lifecycle_run`` row and one ``lifecycle_run`` audit event per agency (counts only).

**DRY RUN** while ``LIFECYCLE_DRY_RUN`` is on (the default): every step counts and nothing changes. Logs carry counts
and codes only; the job result is counts (ids-only payload guard).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import asyncpg

from app.audit_export import registry
from app.documents.store import DocumentStore
from app.errors import Forbidden, NotFound, ServiceError
from app.jobs import queue
from app.jobs.kinds import LIFECYCLE
from app.jobs.queue import Job
from app.jobs.worker import Handler
from app.lifecycle import erase
from app.lifecycle.rules import DEFAULT_AUTO_ARCHIVE_MONTHS
from app.tenancy import LifecycleContext, lifecycle_tx
from app.vault import Vault

if TYPE_CHECKING:  # pragma: no cover
    from app.vault import KeyProvider

log = logging.getLogger(__name__)

ACTOR = "job:crm_lifecycle"
SCHEDULER = "scheduler:crm-lifecycle"
MAX_ATTEMPTS = 3
HISTORY_DAYS = 30
BATCH = 500
OPEN_STAGES = ("inquiry", "qualifying", "proposal", "booked", "pre_departure", "travelling")
# AA-944: planning lines before their trips; suppliers last (a purge refuses one that any line still names).
TRASH_ORDER = ("relation", "task", "note", "document", "booking_line", "trip", "traveler", "agency_supplier")
TRASH_TABLE = {
    "relation": ("traveler_relation", "relation_id"),
    "task": ("task", "task_id"),
    "note": ("note", "note_id"),
    "document": ("document", "document_id"),
    "trip": ("trip", "trip_id"),
    "traveler": ("traveler", "traveler_id"),
    "booking_line": ("booking_line", "booking_line_id"),
    "agency_supplier": ("agency_supplier", "agency_supplier_id"),
}

REDACT_DUE_SQL = """
SELECT s.submission_id,
       crm.hold_row(s.trip_id, s.traveler_id) AS held
  FROM crm.form_submission s
 WHERE s.payload_redacted_at IS NULL AND s.redact_after <= now()
 ORDER BY s.redact_after, s.submission_id
 LIMIT $1
"""
REDACT_SQL = """
UPDATE crm.form_submission SET payload_enc = NULL, payload_redacted_at = now()
 WHERE submission_id = ANY($1::uuid[]) AND payload_redacted_at IS NULL
"""
TRASH_DUE_SQL = {
    kind: f"SELECT {key} AS id FROM crm.{table} "  # noqa: S608 — fixed names
    f"WHERE deleted_at IS NOT NULL AND purge_after <= now() ORDER BY purge_after, {key} LIMIT $1"
    for kind, (table, key) in TRASH_TABLE.items()
}
ARCHIVE_MONTHS_SQL = "SELECT auto_archive_months FROM crm.tenant_settings WHERE tenant_id = $1"
INACTIVE_SQL = """
SELECT t.traveler_id FROM crm.traveler t
 WHERE t.deleted_at IS NULL AND t.archived_at IS NULL
   AND greatest(t.updated_at, coalesce((
         SELECT max(greatest(tr.updated_at, coalesce(tr.end_date::timestamptz, tr.updated_at)))
           FROM crm.trip_traveler tt JOIN crm.trip tr ON tr.trip_id = tt.trip_id
          WHERE tt.traveler_id = t.traveler_id), t.updated_at)) < now() - make_interval(months => $1)
   AND NOT EXISTS (
         SELECT 1 FROM crm.trip_traveler tt JOIN crm.trip tr ON tr.trip_id = tt.trip_id
          WHERE tt.traveler_id = t.traveler_id AND tr.deleted_at IS NULL AND tr.stage = ANY($2::text[]))
 ORDER BY t.traveler_id
 LIMIT $3
"""
AUTO_ARCHIVE_SQL = """
UPDATE crm.traveler SET archived_at = now(), archive_reason = 'auto_inactive', archived_by = $2
 WHERE traveler_id = ANY($1::uuid[]) AND archived_at IS NULL AND deleted_at IS NULL
"""
RUN_SQL = "INSERT INTO crm.lifecycle_run (kind, dry_run, counts) VALUES ($1, $2, $3::jsonb)"


async def run_agency(
    ctx: LifecycleContext, *, dry_run: bool, keys: KeyProvider | None, store: DocumentStore | None
) -> dict[str, int]:
    """The five steps for one agency (inside its :func:`app.tenancy.lifecycle_tx`). Returns the counts."""
    counts: Counter[str] = Counter()

    due = await ctx.conn.fetch(REDACT_DUE_SQL, BATCH)
    free = [r["submission_id"] for r in due if not r["held"]]
    counts["submission_held"] = len(due) - len(free)
    if free and not dry_run:
        await ctx.conn.execute(REDACT_SQL, free)
    counts["submission_redacted"] = len(free)

    purged = await erase.purge_expired(ctx, dry_run=dry_run)
    counts["llm_call_purged"] = purged.get("llm_call", 0)
    counts["llm_call_held"] = purged.get("held", 0)
    counts["placeholder_map_purged"] = purged.get("placeholder_map", 0)

    vault = Vault(keys, ctx) if keys is not None else None
    for kind in TRASH_ORDER:
        for r in await ctx.conn.fetch(TRASH_DUE_SQL[kind], BATCH):
            if kind == "traveler" and vault is None:
                counts["trash_kept_no_keys"] += 1
                continue
            try:
                async with ctx.conn.transaction():  # one item per savepoint: a refusal keeps that item only
                    await erase.erase(ctx, kind, r["id"], dry_run=dry_run, vault=vault, store=store)
            except ServiceError as exc:
                counts[f"trash_kept_{exc.code}"] += 1
                continue
            counts[f"trash_{kind}_purged"] += 1

    months = await ctx.conn.fetchval(ARCHIVE_MONTHS_SQL, ctx.tenant_id) or DEFAULT_AUTO_ARCHIVE_MONTHS
    inactive = [r["traveler_id"] for r in await ctx.conn.fetch(INACTIVE_SQL, int(months), list(OPEN_STAGES), BATCH)]
    if inactive and not dry_run:
        await ctx.conn.execute(AUTO_ARCHIVE_SQL, inactive, ACTOR)
    counts["traveler_auto_archived"] = len(inactive)

    result = {k: int(v) for k, v in sorted(counts.items())}
    await ctx.conn.execute(RUN_SQL, LIFECYCLE, dry_run, json.dumps(result))
    ctx.audit("lifecycle_run", "allowed", "tenant", str(ctx.tenant_id), reason="dry_run" if dry_run else "applied")
    return result


def build_handler(*, dry_run: bool, keys: KeyProvider | None = None, store: DocumentStore | None = None) -> Handler:
    async def lifecycle(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        totals: Counter[str] = Counter()
        done = skipped = failed = 0
        for agency in await registry.agencies(pool):
            try:
                async with lifecycle_tx(pool, agency.tenant_id, actor=ACTOR) as ctx:
                    counts = await run_agency(ctx, dry_run=dry_run, keys=keys, store=store)
            except (Forbidden, NotFound):
                skipped += 1  # offboarding / offboarded: the offboarding job owns it
                continue
            except ServiceError as exc:
                failed += 1
                log.error("lifecycle_failed tenant_id=%s code=%s", agency.tenant_id, exc.code)
                continue
            except Exception as exc:  # noqa: BLE001 — one agency's unexpected error must not skip every later agency
                failed += 1
                log.error("lifecycle_failed tenant_id=%s error_class=%s", agency.tenant_id, type(exc).__name__)
                continue
            done += 1
            totals.update(counts)
            log.info(
                "lifecycle_run tenant_id=%s dry_run=%s %s",
                agency.tenant_id,
                dry_run,
                " ".join(f"{k}={v}" for k, v in counts.items() if v),
            )
        return {
            "dry_run": dry_run,
            "agency_count": done,
            "skipped_count": skipped,
            "failed_count": failed,
            **{f"{k}_count": int(v) for k, v in sorted(totals.items())},
        }

    return lifecycle


def schedule_key(day: date) -> str:
    return f"{LIFECYCLE}:schedule:{day.isoformat()}"


class DailyLifecycleSchedule:
    """Worker maintenance hook: one ``crm_lifecycle`` run per UTC day, due now, from ``utc_hour``; never ahead."""

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
            LIFECYCLE,
            {"purpose": "daily_schedule"},
            idempotency_key=schedule_key(today),
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, LIFECYCLE, HISTORY_DAYS)
        self._done_day = today
        return job_id
