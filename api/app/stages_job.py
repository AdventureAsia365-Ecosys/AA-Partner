"""``crm_stage_date_rule``: the date rule of the trip stages (design v3 §3.5, Fix B4; card CRM-15 E), hourly.

Queued **due now** once per UTC hour by :class:`HourlyStageDateRule` (never queued ahead: the deploy gate and the DB
Migrate idle check count a queued row as busy; not deploy-gate exempt). For every active agency, in one
:func:`app.tenancy.system_tx` (actor ``job:crm_stage_date_rule``), with "today" in the agency's own timezone
(``tenant_settings.timezone``, default ``Asia/Ho_Chi_Minh``), so a departure moves near local midnight:

1. ``booked`` with ``start_date <= today`` -> ``travelling``: reason ``date_rule`` when Stage 3 is complete (s3 = 100,
   recomputed first, :mod:`app.portal.progress`), else ``date_rule_gate_unmet`` and one **urgent** task
   ``date_rule_gate_unmet`` (Fix B4: "Stage 3 or the waiver is still missing"; AA-946);
2. ``pre_departure`` with ``start_date <= today`` -> ``travelling`` (reason ``date_rule``);
3. ``travelling`` with ``end_date <= today`` -> ``completed`` (reason ``date_rule``);
4. ``travelling`` without an ``end_date``: one task ``return_date_missing`` per trip, ever (the trip stays; card E of
   CRM-18 too);
5. AA-946 T-21 tick (design F8.5): a ``booked`` trip starting within :data:`T21_DAYS` days whose Stage 3 is not
   complete gets one task ``stage3_due_t21`` per trip, ever;
6. AA-946: a pending e-mail code past its 10 minutes becomes ``expired`` and loses its sealed copy (``code_enc``,
   kept for the mail sender: a code mail held or never sent must not keep it until the purge); then codes
   (``crm.otp_challenge``) older than 24 hours are deleted (DM-7 row; counts only);
7. AA-950 (R4): a sent proposal the client viewed 2 days ago without choosing anything or accepting gives the trip
   owner one ``proposal_follow_up`` task and a bell item, once per version (:func:`app.proposals.events.nudge_due`);
8. AA-952 slice 4 (U8, agent first): an issued invoice past its due date with money due gives the trip owner a bell
   item and an ``invoice_overdue`` task, the day after and then every 3 days, at most 5 times
   (:func:`app.payments.reminders.overdue`); first, the open ``invoice_overdue`` tasks of trips with nothing left to
   chase are closed (:func:`app.payments.reminders.close_settled`).
9. AA-957 (M13, C12): the commission rows of trips whose lines changed without a sync
   (:func:`app.commissions.service.sweep`), one owner task ``commission_overdue`` per trip with a commission past its
   due date (once per row), and the cancel's ``cancel_with_suppliers`` task closed once no confirmed agency line is
   left. A trip whose cancellation waits for Booking is never moved by the date rule.
10. S262 (C12 item 3): the due balance rows of cancelled (or cancelling) trips that no kept invoice bills are voided
    with their draft invoices (:func:`app.cancellation.void_cancelled_schedule`; the cancel run does it at once since
    S262, this catches the trips cancelled before), audit ``payment_schedule_void`` reason ``trip_cancelled``.

Payload and result are ids and counts only. An agency that is locked or offboarding is skipped.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app import cancellation, stages
from app.audit_export import registry
from app.commissions import service as commissions
from app.errors import Forbidden, NotFound, ServiceError
from app.jobs import queue
from app.jobs.kinds import STAGE_DATE_RULE
from app.jobs.queue import Job
from app.payments import reminders
from app.portal import progress
from app.proposals import events
from app.tenancy import SystemContext, system_tx
from app.workflows import triggers

log = logging.getLogger(__name__)

ACTOR = "job:crm_stage_date_rule"
SCHEDULER = "scheduler:crm-stage-date-rule"
HISTORY_DAYS = 2
MAX_ATTEMPTS = 3
BATCH = 500
DEFAULT_TZ = "Asia/Ho_Chi_Minh"
T21_DAYS = 21

TODAY_SQL = """
SELECT (now() AT TIME ZONE coalesce((SELECT timezone FROM crm.tenant_settings LIMIT 1), $1))::date
"""
DUE_SQL = """
SELECT trip_id, stage FROM crm.trip
 WHERE deleted_at IS NULL
   AND ((stage IN ('booked', 'pre_departure') AND start_date <= $1)
        OR (stage = 'travelling' AND end_date <= $1))
   AND NOT EXISTS (SELECT 1 FROM crm.trip_cancellation x WHERE x.trip_id = trip.trip_id AND x.status = 'pending')
 ORDER BY start_date, trip_id
 LIMIT $2
   FOR UPDATE SKIP LOCKED
"""
NO_RETURN_SQL = """
SELECT t.trip_id FROM crm.trip t
 WHERE t.deleted_at IS NULL AND t.stage = 'travelling' AND t.end_date IS NULL
   AND NOT EXISTS (SELECT 1 FROM crm.task k WHERE k.trip_id = t.trip_id AND k.kind = 'return_date_missing')
 LIMIT $1
"""
MOVE_SQL = """
UPDATE crm.trip SET stage = $2, stage_entered_at = now(), waiting_on = 'none', last_activity_at = now()
 WHERE trip_id = $1
"""
HISTORY_SQL = """
INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code) VALUES ($1, $2, $3, $4)
RETURNING history_id
"""
T21_SQL = """
SELECT t.trip_id FROM crm.trip t
 WHERE t.deleted_at IS NULL AND t.stage = 'booked' AND t.start_date > $1 AND t.start_date <= $1 + $2::int
   AND NOT EXISTS (SELECT 1 FROM crm.task k WHERE k.trip_id = t.trip_id AND k.kind = 'stage3_due_t21')
 LIMIT $3
"""
OTP_EXPIRE_SQL = """
UPDATE crm.otp_challenge SET status = 'expired', code_enc = NULL WHERE status = 'pending' AND expires_at <= now()
"""
OTP_PURGE_SQL = "DELETE FROM crm.otp_challenge WHERE created_at < now() - interval '24 hours'"
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
SELECT $1, $2, $3, t.owner_member_id, 'departure', now() FROM crm.trip t WHERE t.trip_id = $1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""


async def run_agency(ctx: SystemContext) -> Counter[str]:
    counts: Counter[str] = Counter()
    today = await ctx.conn.fetchval(TODAY_SQL, DEFAULT_TZ)
    for row in await ctx.conn.fetch(DUE_SQL, today, BATCH):
        trip_id, stage = row["trip_id"], row["stage"]
        if stage == "travelling":
            to, reason = "completed", "date_rule"
        elif stage == "booked":
            done = await progress.refresh(ctx, trip_id)
            met = done.s3 >= stages.STAGE3_GATE
            to, reason = "travelling", "date_rule" if met else "date_rule_gate_unmet"
        else:
            to, reason = "travelling", "date_rule"
        await ctx.conn.execute(MOVE_SQL, trip_id, to)
        history_id = await ctx.conn.fetchval(HISTORY_SQL, trip_id, stage, to, reason)
        ctx.audit("stage_change", "allowed", "trip", str(trip_id), reason=reason)
        if history_id is not None:  # AA-959: workflows that start when the trip enters the stage
            counts["workflow_runs"] += await triggers.on_stage_enter(ctx, trip_id, to, history_id)
        counts[f"{stage}_to_{to}"] += 1
        if reason == "date_rule_gate_unmet":
            await ctx.conn.execute(TASK_SQL, trip_id, "date_rule_gate_unmet", "urgent")
            counts["urgent_tasks"] += 1
    for row in await ctx.conn.fetch(NO_RETURN_SQL, BATCH):
        await ctx.conn.execute(TASK_SQL, row["trip_id"], "return_date_missing", "normal")
        counts["return_date_missing"] += 1
    for row in await ctx.conn.fetch(T21_SQL, today, T21_DAYS, BATCH):
        if (await progress.refresh(ctx, row["trip_id"])).s3 < stages.STAGE3_GATE:
            await ctx.conn.execute(TASK_SQL, row["trip_id"], "stage3_due_t21", "normal")
            counts["stage3_due_t21"] += 1
    expired = stages._rows(await ctx.conn.execute(OTP_EXPIRE_SQL))
    if expired:
        counts["codes_expired"] += expired
    purged = await ctx.conn.execute(OTP_PURGE_SQL)
    if stages._rows(purged):
        counts["codes_purged"] += stages._rows(purged)
    nudged = await events.nudge_due(ctx)
    if nudged:
        counts["proposal_nudges"] += nudged
    closed = await reminders.close_settled(ctx, today)
    if closed:
        counts["invoice_reminder_tasks_closed"] += closed
    reminded = await reminders.overdue(ctx, today)
    if reminded:
        counts["invoice_reminders"] += reminded
    # AA-957 (M13): commission rows of every trip whose lines changed without a sync, the overdue owner task (once per
    # row), and the cancel's supplier task closed once no confirmed agency line is left.
    synced = await commissions.sweep(ctx)
    if synced:
        counts["commission_trips_synced"] += synced
    overdue = await commissions.alert_overdue(ctx, today)
    if overdue:
        counts["commissions_overdue"] += overdue
    closed_supplier = await commissions.close_supplier_tasks(ctx, ACTOR)
    if closed_supplier:
        counts["cancel_supplier_tasks_closed"] += closed_supplier
    # S262: a cancelled trip bills the client no more, also when it was cancelled before the run voided its schedule.
    schedule_voided = await cancellation.void_cancelled_schedule(ctx, ACTOR)
    if schedule_voided:
        counts["cancelled_schedule_rows_voided"] += schedule_voided
    return counts


async def date_rule(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
    totals: Counter[str] = Counter()
    done = skipped = failed = 0
    for agency in await registry.agencies(pool):
        try:
            async with system_tx(pool, agency.tenant_id, actor=ACTOR) as ctx:
                counts = await run_agency(ctx)
        except (Forbidden, NotFound):
            skipped += 1  # locked, offboarding or gone
            continue
        except ServiceError as exc:
            failed += 1
            log.error("stage_date_rule_failed tenant_id=%s code=%s", agency.tenant_id, exc.code)
            continue
        except Exception as exc:  # noqa: BLE001 — one agency's error must not skip every later agency
            failed += 1
            log.error("stage_date_rule_failed tenant_id=%s error_class=%s", agency.tenant_id, type(exc).__name__)
            continue
        done += 1
        totals.update(counts)
    if sum(totals.values()):
        log.info("stage_date_rule %s", " ".join(f"{k}={v}" for k, v in sorted(totals.items())))
    return {
        "agency_count": done,
        "skipped_count": skipped,
        "failed_count": failed,
        **{f"{k}_count": int(v) for k, v in sorted(totals.items())},
    }


def schedule_key(now: datetime) -> str:
    return f"{STAGE_DATE_RULE}:schedule:{now.strftime('%Y-%m-%dT%H')}"


class HourlyStageDateRule:
    """Worker maintenance hook: one ``crm_stage_date_rule`` run per UTC hour, due now (the
    :class:`app.privacy.purge.HourlyPlaceholderPurge` pattern)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_slot: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        key = schedule_key(self._now())
        if self._done_slot == key:
            return None
        job_id = await queue.enqueue(
            pool,
            STAGE_DATE_RULE,
            {"purpose": "hourly_schedule"},
            idempotency_key=key,
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, STAGE_DATE_RULE, HISTORY_DAYS)
        self._done_slot = key
        return job_id
