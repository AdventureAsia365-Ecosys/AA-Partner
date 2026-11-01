"""``crm_workflow_tick``: the hourly workflow pass of every active agency (AA-959; checklist "idempotent tick").

Queued **due now** once per UTC hour by :class:`HourlyWorkflowTick` (never queued ahead: the DB Migrate idle check
counts a queued row as busy; not deploy-gate exempt). Per agency, in one :func:`app.tenancy.system_tx` (actor
``job:crm_workflow_tick``), with "today" in the agency's timezone:

1. **Date triggers**: for each active ``date_anchor`` rule, the live trips whose anchor date (departure, return,
   stage entered, last activity, created) + the rule's offset falls within the last :data:`CATCHUP_DAYS` days, matching
   the rule's conditions, start one run each, keyed ``date:<anchor>:<offset>:<anchor date>``. A tick that runs twice
   starts nothing twice (the key is unique per rule, card E); a trip whose departure moves gets a run for the new date;
   a new rule never fires for dates older than the catch-up window.
2. **Special dates** (R2), at :data:`SPECIAL_HOURS` local only (the client data is opened once or twice a day): a
   client's birthday, passport expiry and the anniversary of a completed trip's end (its lead client), + the rule's
   offset, within the catch-up window: one run per client and date (``special:<code>:<date>``). Archived or trashed
   clients are left out. The consent of a marketing send is checked by the engine (card R).
3. **Due runs**: every active run of an active (or archived: it stops) rule whose ``next_at`` has passed gets one
   ``crm_workflow_run`` job, due now (a wait never queues a row ahead).
4. **Task dates** (R20): every open task anchored on the departure or the return, not edited by hand
   (``due_overridden``), gets its due time from the trip's dates (date + offset at 09:00 local); a trip without the
   date leaves it NULL ("Waiting for dates"), a trip that gets one dates it.
5. **Digest**: an agency with ``task_digest`` on gets one ``crm_task_digest`` job per local day at
   :data:`DIGEST_HOUR` (idempotency key per agency and date).

Payload and result are ids and counts only. A locked or offboarding agency is skipped.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from app.audit_export import registry
from app.errors import Forbidden, NotFound, ServiceError
from app.jobs import queue
from app.jobs.kinds import TASK_DIGEST, WORKFLOW_RUN, WORKFLOW_TICK
from app.jobs.queue import Job
from app.tenancy import SystemContext, system_tx
from app.vault import KeyProvider, Vault
from app.vault.errors import DecryptionDenied
from app.workflows import facts as wf_facts
from app.workflows import model, tasks, triggers

log = logging.getLogger(__name__)

ACTOR = "job:crm_workflow_tick"
SCHEDULER = "scheduler:crm-workflow-tick"
MAX_ATTEMPTS = 3
HISTORY_DAYS = 2
CATCHUP_DAYS = 3
SPECIAL_HOURS = (8, 9)
DIGEST_HOUR = 7
MAX_TRIPS_PER_RULE = 500
MAX_CLIENTS = 5000
MAX_DUE_RUNS = 500

DATE_EXPRESSIONS = {
    "departure": "t.start_date",
    "return": "t.end_date",
    "stage_entered": "(t.stage_entered_at AT TIME ZONE $4)::date",
    "last_activity": "(t.last_activity_at AT TIME ZONE $4)::date",
    "created": "(t.created_at AT TIME ZONE $4)::date",
}
DATE_RULES_SQL = """
SELECT workflow_rule_id, version, trigger_kind, trigger_value, trigger_offset_days, conditions
  FROM crm.workflow_rule
 WHERE status = 'active' AND deleted_at IS NULL AND trigger_kind = $1
 ORDER BY created_at, workflow_rule_id
 LIMIT 100
"""
CANDIDATES_SQL = """
SELECT t.trip_id, {expr} AS anchor_date FROM crm.trip t
 WHERE t.deleted_at IS NULL AND t.stage NOT IN ('lost', 'cancelled')
   AND {expr} + $1::int BETWEEN $2::date - $3::int AND $2::date AND $4::text IS NOT NULL
 ORDER BY 2, 1
 LIMIT $5
"""
BIRTHDAY_SQL = """
SELECT traveler_id, dob_enc AS blob FROM crm.traveler
 WHERE dob_enc IS NOT NULL AND deleted_at IS NULL AND archived_at IS NULL
 ORDER BY traveler_id LIMIT $1
"""
PASSPORT_SQL = """
SELECT traveler_id, passport_expiry_enc AS blob FROM crm.traveler
 WHERE passport_expiry_enc IS NOT NULL AND deleted_at IS NULL AND archived_at IS NULL
 ORDER BY traveler_id LIMIT $1
"""
ANNIVERSARY_SQL = """
SELECT t.trip_id, t.end_date, tt.traveler_id
  FROM crm.trip t
  JOIN crm.trip_traveler tt ON tt.trip_id = t.trip_id AND tt.role = 'lead'
  JOIN crm.traveler c ON c.traveler_id = tt.traveler_id AND c.deleted_at IS NULL AND c.archived_at IS NULL
 WHERE t.stage = 'completed' AND t.deleted_at IS NULL AND t.end_date IS NOT NULL
 ORDER BY t.end_date, t.trip_id
 LIMIT $1
"""
DUE_RUNS_SQL = """
SELECT r.workflow_run_id, r.step_index, r.next_at
  FROM crm.workflow_run r JOIN crm.workflow_rule w ON w.workflow_rule_id = r.workflow_rule_id
 WHERE r.status = 'active' AND r.next_at <= now() AND w.status IN ('active', 'archived')
 ORDER BY r.next_at
 LIMIT $1
"""
DIGEST_SQL = "SELECT task_digest FROM crm.tenant_settings"


def _conditions(raw: Any) -> dict[str, Any]:
    return json.loads(raw) if isinstance(raw, str) else dict(raw or {})


def in_window(target: dt.date, today: dt.date) -> bool:
    return today - dt.timedelta(days=CATCHUP_DAYS) <= target <= today


def yearly(day: dt.date, year: int) -> dt.date:
    """``day`` in ``year`` (29 February -> 28 February in a common year)."""
    try:
        return day.replace(year=year)
    except ValueError:
        return day.replace(year=year, day=28)


def birthday_targets(dob: dt.date, offset: int, today: dt.date) -> list[dt.date]:
    """This year's and last year's birthday + offset that fall in the catch-up window (a window across New Year)."""
    out = []
    for year in (today.year - 1, today.year, today.year + 1):
        if year <= dob.year:
            continue
        target = yearly(dob, year) + dt.timedelta(days=offset)
        if in_window(target, today):
            out.append(target)
    return out


def anniversary_targets(end: dt.date, offset: int, today: dt.date) -> list[tuple[int, dt.date]]:
    out = []
    for years in range(max(1, today.year - end.year - 1), today.year - end.year + 2):
        target = yearly(end, end.year + years) + dt.timedelta(days=offset)
        if years >= 1 and in_window(target, today):
            out.append((years, target))
    return out


async def _date_triggers(ctx: SystemContext, tz: ZoneInfo, today: dt.date) -> Counter[str]:
    counts: Counter[str] = Counter()
    for rule in await ctx.conn.fetch(DATE_RULES_SQL, "date_anchor"):
        anchor, offset = rule["trigger_value"], int(rule["trigger_offset_days"])
        expr = DATE_EXPRESSIONS.get(anchor)
        if expr is None:
            continue
        sql = CANDIDATES_SQL.format(expr=expr)
        rows = await ctx.conn.fetch(sql, offset, today, CATCHUP_DAYS, tz.key, MAX_TRIPS_PER_RULE)
        all_facts = await wf_facts.trips(ctx, [r["trip_id"] for r in rows])
        conditions = _conditions(rule["conditions"])
        for r in rows:
            facts = all_facts.get(r["trip_id"])
            if facts is None or model.stop_reason(facts) or model.matches(conditions, facts) is not None:
                continue
            key = f"date:{anchor}:{offset}:{r['anchor_date'].isoformat()}"
            if await triggers.start(ctx, rule, trip_id=r["trip_id"], traveler_id=None, key=key) is not None:
                counts["date_runs_started"] += 1
    return counts


async def _open_date(vault: Vault, column: str, row: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(await vault.decrypt("traveler", column, row["traveler_id"], row["blob"]))
    except ValueError:
        return None


async def _special_dates(ctx: SystemContext, vault: Vault, today: dt.date) -> Counter[str]:
    counts: Counter[str] = Counter()
    rules = await ctx.conn.fetch(DATE_RULES_SQL, "special_date")
    if not rules:
        return counts
    wanted = {r["trigger_value"] for r in rules}
    dates: dict[str, list[tuple[Any, dt.date]]] = {}
    for code, sql, column in (("birthday", BIRTHDAY_SQL, "dob_enc"), ("passport_expiry", PASSPORT_SQL,
                                                                     "passport_expiry_enc")):  # fmt: skip
        if code in wanted:
            opened = [(r, await _open_date(vault, column, r)) for r in await ctx.conn.fetch(sql, MAX_CLIENTS)]
            dates[code] = [(r, d) for r, d in opened if d is not None]
    trips = await ctx.conn.fetch(ANNIVERSARY_SQL, MAX_CLIENTS) if "trip_anniversary" in wanted else []
    for rule in rules:
        code, offset = rule["trigger_value"], int(rule["trigger_offset_days"])
        starts: list[tuple[Any, str]] = []
        if code == "birthday":
            for row, dob in dates.get("birthday", []):
                starts += [(row["traveler_id"], f"special:birthday:{t.isoformat()}")
                           for t in birthday_targets(dob, offset, today)]  # fmt: skip
        elif code == "passport_expiry":
            for row, expiry in dates.get("passport_expiry", []):
                if in_window(expiry + dt.timedelta(days=offset), today):
                    starts.append((row["traveler_id"], f"special:passport_expiry:{expiry.isoformat()}"))
        elif code == "trip_anniversary":
            for row in trips:
                for _years, target in anniversary_targets(row["end_date"], offset, today):
                    starts.append(
                        (row["traveler_id"], f"special:trip_anniversary:{row['trip_id']}:{target.isoformat()}")
                    )
        for traveler_id, key in starts:
            if await triggers.start(ctx, rule, trip_id=None, traveler_id=traveler_id, key=key) is not None:
                counts["special_runs_started"] += 1
    return counts


async def _due_runs(ctx: SystemContext) -> int:
    queued = 0
    for r in await ctx.conn.fetch(DUE_RUNS_SQL, MAX_DUE_RUNS):
        job = await queue.enqueue(
            ctx.conn,
            WORKFLOW_RUN,
            {"tenant_id": str(ctx.tenant_id), "workflow_run_id": str(r["workflow_run_id"])},
            idempotency_key=f"{WORKFLOW_RUN}:{r['workflow_run_id']}:{r['step_index']}:{int(r['next_at'].timestamp())}",
            max_attempts=triggers.RUN_ATTEMPTS,
            created_by="workflow",
        )
        queued += job is not None
    return queued


async def run_agency(ctx: SystemContext, vault: Vault, now: datetime) -> Counter[str]:
    tz = await wf_facts.timezone(ctx)
    local = now.astimezone(tz)
    today = local.date()
    counts = await _date_triggers(ctx, tz, today)
    if local.hour in SPECIAL_HOURS:
        counts.update(await _special_dates(ctx, vault, today))
    counts["runs_queued"] += await _due_runs(ctx)
    counts["tasks_redated"] += await tasks.redate(ctx, tz)
    if local.hour == DIGEST_HOUR and await ctx.conn.fetchval(DIGEST_SQL):
        job = await queue.enqueue(
            ctx.conn,
            TASK_DIGEST,
            {"tenant_id": str(ctx.tenant_id)},
            idempotency_key=f"{TASK_DIGEST}:{ctx.tenant_id}:{today.isoformat()}",
            max_attempts=3,
            created_by="workflow",
        )
        counts["digest_queued"] += job is not None
    return counts


def build_handler(keys: KeyProvider | None, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> Any:
    async def crm_workflow_tick(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        if keys is None:
            log.error("workflow_tick_not_configured reason=key_provider")
            return {"agency_count": 0, "reason_code": "key_provider_not_configured"}
        totals: Counter[str] = Counter()
        done = skipped = failed = 0
        moment = now()
        for agency in await registry.agencies(pool):
            try:
                async with system_tx(pool, agency.tenant_id, actor=ACTOR) as ctx:
                    counts = await run_agency(ctx, Vault(keys, ctx), moment)
            except (Forbidden, NotFound):
                skipped += 1
                continue
            except (ServiceError, DecryptionDenied, asyncpg.PostgresError) as exc:
                failed += 1
                log.error("workflow_tick_failed tenant_id=%s error_class=%s", agency.tenant_id, type(exc).__name__)
                continue
            done += 1
            totals.update(counts)
        return {
            "agency_count": done,
            "skipped_count": skipped,
            "failed_count": failed,
            **{f"{k}_count" if not k.endswith("_count") else k: int(v) for k, v in sorted(totals.items())},
        }

    return crm_workflow_tick


def schedule_key(moment: datetime) -> str:
    return f"{WORKFLOW_TICK}:schedule:{moment.strftime('%Y-%m-%dT%H')}"


class HourlyWorkflowTick:
    """Worker maintenance hook: one ``crm_workflow_tick`` per UTC hour, due now (the HourlyStageDateRule pattern)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_slot: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        key = schedule_key(self._now())
        if self._done_slot == key:
            return None
        job_id = await queue.enqueue(
            pool,
            WORKFLOW_TICK,
            {"purpose": "hourly_schedule"},
            idempotency_key=key,
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, WORKFLOW_TICK, HISTORY_DAYS)
        self._done_slot = key
        return job_id
