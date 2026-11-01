"""The ``crm.job`` kinds of the skeleton (AA-933): the liveness heartbeat and the job-table retention.

- ``crm_heartbeat`` (every 60 s, one row per minute slot): proves the worker claims and finishes jobs. A due heartbeat
  that stays queued is the "worker stopped" signal (``OldestQueuedAgeSeconds``). Idle-exempt in DB Migrate.
- ``crm_intake_process`` (AA-936, queued by the public intake route with each stored submission): see
  :mod:`app.intake.process`.
- ``crm_placeholder_purge`` (AA-938, hourly, :class:`app.privacy.purge.HourlyPlaceholderPurge`): expired F3
  placeholder maps (24 h), every agency in one DELETE through the crm-009 purge policy.
- ``crm_stage_date_rule`` (AA-943, hourly, :class:`app.stages_job.HourlyStageDateRule`): the trip date rule.
- ``crm_fx_fetch`` (AA-944, daily, :class:`app.fx.fetch.DailyFxSchedule`): the platform FX rates (``FX_PROVIDER``).
- ``crm_membership_pull`` (AA-978, every 20 s, Recurring): the CIS membership feed, :mod:`app.cis.membership_feed`.
  Deploy-gate and DB Migrate idle exempt, like the heartbeat.
- ``crm_booking_sync`` (AA-947, every 60 s, Recurring, plus one-agency nudges): the AA outage queue and the Booking
  event feed, :mod:`app.booking.sync`. Exempt like the membership pull. ``crm_catalog_sync`` (AA-947, hourly,
  :class:`app.catalog.sync.HourlyCatalogSync`): the AA tour cache.
- ``crm_ops_release_sync`` (AA-956, hourly, :class:`app.ops_release.job.HourlyOpsReleaseSync`, plus one-trip nudges):
  the Module 8 ops release (create, refresh, T-21 alert, withdraw, purge with a receipt).
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
from app.jobs.kinds import (
    AUDIT_EXPORT,
    AUDIT_RETENTION,
    BOOKING_SYNC,
    CATALOG_SYNC,
    CLIENT_IMPORT,
    DATA_EXPORT,
    DISPATCH,
    FX_FETCH,
    HEARTBEAT,
    INTAKE_PROCESS,
    JOB_RETENTION,
    LIFECYCLE,
    MAIL_HEALTH,
    MAIL_IMPORT,
    MAIL_INGEST,
    MEMBERSHIP_PULL,
    MESSAGING_HEALTH,
    MESSAGING_TEMPLATES,
    OFFBOARD,
    OPS_RELEASE_SYNC,
    PLACEHOLDER_PURGE,
    STAGE_DATE_RULE,
    STUDIO_ACTIVATION,
)
from app.jobs.queue import Job
from app.jobs.worker import Handler, MaintenanceHook, Recurring

if TYPE_CHECKING:  # pragma: no cover
    from app.audit_export.archive import AuditArchive
    from app.booking.client import BookingClient
    from app.catalog.client import CatalogConfig
    from app.cis.activation import ActivationConfig
    from app.cis.membership_feed import FeedConfig
    from app.dispatch.worker import Sender
    from app.documents.store import DocumentStore
    from app.mail.state import MailRuntime
    from app.mail.store import IngestStore
    from app.members.directory import PartnerDirectory
    from app.messaging.state import MessagingRuntime
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
    documents: DocumentStore | None = None,
    lifecycle_dry_run: bool = True,
    dek_cache_ttl_s: int = 300,
    sender: Sender | None = None,
    fx_provider: str = "synthetic",
    membership_feed: FeedConfig | None = None,
    directory: PartnerDirectory | None = None,
    mail_runtime: MailRuntime | None = None,
    ingest_store: IngestStore | None = None,
    ingest_domain: str = "",
    booking_client: BookingClient | None = None,
    catalog: CatalogConfig | None = None,
    studio_activation: ActivationConfig | None = None,
    messaging_runtime: MessagingRuntime | None = None,
) -> dict[str, Handler]:
    """Every kind the worker runs. ``keys``: the field-encryption provider the intake kind needs (AA-936); without
    it ``crm_intake_process`` jobs are retried until the provider is configured. ``audit_archive``: the two audit
    buckets (AA-940); without them the export keeps the partitions only and the retention never drops anything.
    AA-942: ``documents`` (the bucket the purges and the offboarding export use), ``lifecycle_dry_run``
    (``LIFECYCLE_DRY_RUN``), ``dek_cache_ttl_s`` (reported on the offboarding receipt), ``sender`` (none until CRM-17:
    messages that pass the dispatcher gate are held). AA-944: ``fx_provider`` (``FX_PROVIDER``, synthetic or live).
    AA-978: ``membership_feed`` (CIS_API_URL + the M2M token; None = the pull logs not_configured) and ``directory``
    (members the feed disables are signed out of the partner pool).
    AA-945: ``mail_runtime`` (providers, system mailer, directory: the mailbox health check), ``ingest_store`` +
    ``ingest_domain`` (``CRM_INGEST_BUCKET`` / ``CRM_INGEST_DOMAIN``: the forward-in ingest and the history import;
    without them the ingest answers not_configured and the import is retried).
    AA-947: ``booking_client`` (``BOOKING_API_URL`` + the M2M token: the AA outage queue and the Booking feed; None =
    the sync logs not_configured) and ``catalog`` (``CIS_API_URL`` + the token: the AA tour cache).
    AA-979: ``studio_activation`` (``CIS_API_URL`` + the token: CIS activateInvitation; None = the job is retried).
    AA-958: ``messaging_runtime`` (the SMS / WhatsApp providers: the template sync and the daily connection check)."""
    from app.audit_export import export as audit_export
    from app.audit_export import retention as audit_retention
    from app.booking.sync import BookingSync
    from app.catalog.sync import CatalogSync
    from app.cis.membership_feed import MembershipFeed
    from app.dispatch import worker as dispatch_worker
    from app.fx import fetch as fx_fetch
    from app.imports import service as client_imports
    from app.intake.process import build_handler as build_intake_handler
    from app.lifecycle import job as lifecycle_job
    from app.lifecycle import offboarding
    from app.mail import health as mail_health
    from app.mail import history_import
    from app.mail import ingest as mail_ingest
    from app.mail.state import MailRuntime as _MailRuntime
    from app.members import studio
    from app.messaging import health as messaging_health
    from app.messaging.state import MessagingRuntime as _MessagingRuntime
    from app.ops_release import job as ops_release_job
    from app.portability import exports as portability_exports
    from app.privacy.purge import purge_expired_maps
    from app.stages_job import date_rule

    return {
        HEARTBEAT: heartbeat,
        JOB_RETENTION: build_job_retention(retention_dry_run),
        INTAKE_PROCESS: build_intake_handler(keys),
        AUDIT_EXPORT: audit_export.build_handler(audit_archive),
        AUDIT_RETENTION: audit_retention.build_handler(audit_archive, dry_run=audit_retention_dry_run),
        PLACEHOLDER_PURGE: purge_expired_maps,
        LIFECYCLE: lifecycle_job.build_handler(dry_run=lifecycle_dry_run, keys=keys, store=documents),
        OFFBOARD: offboarding.build_handler(
            keys=keys, store=documents, dry_run=lifecycle_dry_run, dek_cache_ttl_s=dek_cache_ttl_s
        ),
        DISPATCH: dispatch_worker.build_handler(sender),
        STAGE_DATE_RULE: date_rule,
        FX_FETCH: fx_fetch.build_handler(fx_provider),
        MEMBERSHIP_PULL: MembershipFeed(membership_feed, directory),
        MAIL_INGEST: mail_ingest.build_handler(ingest_store, documents, keys, ingest_domain),
        MAIL_HEALTH: mail_health.build_handler(mail_runtime or _MailRuntime(keys=keys)),
        MAIL_IMPORT: history_import.build_handler(ingest_store, documents, keys, ingest_domain),
        BOOKING_SYNC: BookingSync(booking_client),
        CATALOG_SYNC: CatalogSync(catalog),
        STUDIO_ACTIVATION: studio.build_handler(studio_activation),
        DATA_EXPORT: portability_exports.build_handler(keys=keys, store=documents),
        CLIENT_IMPORT: client_imports.build_handler(ingest_store, keys),
        OPS_RELEASE_SYNC: ops_release_job.build_handler(keys),
        MESSAGING_TEMPLATES: messaging_health.build_templates_handler(messaging_runtime or _MessagingRuntime(keys=keys)),
        MESSAGING_HEALTH: messaging_health.build_health_handler(messaging_runtime or _MessagingRuntime(keys=keys)),
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


def build_hooks(
    *, retention_utc_hour: int, now: Callable[[], datetime] | None = None, ingest_store: IngestStore | None = None
) -> tuple[MaintenanceHook, ...]:
    """The due-now schedules. ``now``: a fixed clock for tests (default: the real UTC clock). AA-945:
    ``ingest_store`` (the forward-in scan; none = no scan)."""
    from app.audit_export.export import HourlyAuditExportSchedule
    from app.audit_export.retention import DailyAuditRetentionSchedule
    from app.catalog.sync import HourlyCatalogSync
    from app.fx.fetch import DailyFxSchedule
    from app.lifecycle.job import DailyLifecycleSchedule
    from app.mail.health import DailyMailHealthSchedule
    from app.mail.ingest import MailIngestScan
    from app.messaging.health import DailyMessagingHealthSchedule
    from app.ops_release.job import HourlyOpsReleaseSync
    from app.privacy.purge import HourlyPlaceholderPurge
    from app.stages_job import HourlyStageDateRule

    clock = now or (lambda: datetime.now(UTC))
    return (
        DailyRetentionSchedule(retention_utc_hour, clock),
        HourlyAuditExportSchedule(clock),
        DailyAuditRetentionSchedule(retention_utc_hour, clock),
        HourlyPlaceholderPurge(clock),
        DailyLifecycleSchedule(retention_utc_hour, clock),
        HourlyStageDateRule(clock),
        DailyFxSchedule(now=clock),
        DailyMailHealthSchedule(retention_utc_hour, clock),
        MailIngestScan(ingest_store, clock),
        HourlyCatalogSync(clock),
        HourlyOpsReleaseSync(clock),
        DailyMessagingHealthSchedule(retention_utc_hour, clock),
    )


# Only deploy-gate and DB Migrate idle exempt kinds queue their next slot ahead: the heartbeat and (AA-978) the CIS
# membership pull. A daily or slower kind goes through a due-now maintenance hook instead (DailyRetentionSchedule):
# never add one here. The worker schedules the next slot at each maintenance pass (about every 30 s when idle), so a
# CIS change reaches the CRM within about a minute.
RECURRING: tuple[Recurring, ...] = (
    Recurring(HEARTBEAT, every_seconds=60, keep_days=2),
    Recurring(MEMBERSHIP_PULL, every_seconds=20, keep_days=1),
    Recurring(BOOKING_SYNC, every_seconds=60, keep_days=1),
)
SCHEDULED_KINDS: frozenset[str] = frozenset(
    {
        JOB_RETENTION,
        AUDIT_EXPORT,
        AUDIT_RETENTION,
        PLACEHOLDER_PURGE,
        LIFECYCLE,
        STAGE_DATE_RULE,
        FX_FETCH,
        MAIL_INGEST,
        MAIL_HEALTH,
        CATALOG_SYNC,
        OPS_RELEASE_SYNC,
        MESSAGING_HEALTH,
    }
)
# Kinds queued by request code, one row per event (AA-936: each stored inquiry), never scheduled.
# AA-979: one crm_studio_activation row per accepted Content studio invitation.
EVENT_KINDS: frozenset[str] = frozenset(
    # AA-967: one per export; AA-962: one per import slice.
    # AA-958: one template sync per connect / Refresh.
    {INTAKE_PROCESS, OFFBOARD, DISPATCH, MAIL_IMPORT, STUDIO_ACTIVATION, DATA_EXPORT, CLIENT_IMPORT, MESSAGING_TEMPLATES}
)
