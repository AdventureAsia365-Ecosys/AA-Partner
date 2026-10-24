"""The crm.job runner on PostgreSQL 15 as crm_app (card CRM-05 E check: "a stopped worker's lease is re-claimed").

Also: the per-slot recurring heartbeat (two workers, one row), the deploy-gate count (heartbeat exempt), the payload
guard before any SQL, and the retention kind (DRY RUN counts; a real run deletes only what data standards §7 allows).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import asyncpg
import pytest
import pytest_asyncio

from app.jobs import handlers, queue
from app.jobs.kinds import (
    AUDIT_EXPORT,
    AUDIT_RETENTION,
    DEPLOY_GATE_EXEMPT,
    HEARTBEAT,
    JOB_RETENTION,
    LIFECYCLE,
    PLACEHOLDER_PURGE,
)
from app.jobs.payload_guard import PersonalDataInPayload
from app.jobs.queue import Job
from app.jobs.worker import Recurring, Worker

# AA-CIS-Infra db/run_migration.py JOB_BUSY_SQL (DB Migrate idle check, DM-11.4) and its IDLE_EXEMPT_KINDS["crm"],
# copied verbatim: "busy" = any queued (any run_after) or running row of a kind that is not exempt.
INFRA_JOB_BUSY_SQL = """
SELECT kind, status, count(*)::int AS n,
       count(*) FILTER (WHERE status = 'queued' AND run_after <= now())::int AS due
  FROM {table} WHERE status IN ('queued', 'running') AND NOT (kind = ANY($1::text[]))
 GROUP BY kind, status ORDER BY kind, status"""
INFRA_IDLE_EXEMPT_CRM = frozenset({"crm_heartbeat"})

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TENANT = "00000000-0000-4000-8000-0000000000a1"


@pytest_asyncio.fixture(autouse=True)
async def empty_queue(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE crm_owner")
    await admin.execute("DELETE FROM crm.job")
    await admin.execute("RESET ROLE")


async def test_a_stopped_workers_lease_is_reclaimed(app_pool: asyncpg.Pool) -> None:
    done: list[str] = []

    async def probe(pool: asyncpg.Pool, job: Job) -> dict:
        done.append(job.id)
        return {"tenant_id": job.payload["tenant_id"]}

    job_id = await queue.enqueue(app_pool, "crm_probe", {"tenant_id": TENANT})
    # Worker A claims the job and dies without finishing or releasing it.
    claimed = await queue.claim(app_pool, "worker-a", ["crm_probe"], lease_seconds=300)
    assert claimed is not None and claimed.id == job_id
    async with app_pool.acquire() as conn:
        # The lease runs out (instead of waiting 300 s).
        await conn.execute("UPDATE crm.job SET locked_until = now() - interval '1 second' WHERE id = $1", job_id)
    # Worker B: its first tick runs maintenance (the reaper re-queues the job), then claims and runs it.
    b = Worker(lambda: _pool(app_pool), {"crm_probe": probe}, worker_id="worker-b")
    assert await b.run_once() is True
    row = await app_pool.fetchrow("SELECT status, attempt, locked_by, error, result FROM crm.job WHERE id = $1", job_id)
    assert done == [job_id]
    assert (row["status"], row["attempt"], row["locked_by"]) == ("succeeded", 2, None)
    assert json.loads(row["result"]) == {"tenant_id": TENANT}
    # Worker A comes back: it can no longer complete or fail what it lost.
    assert await queue.complete(app_pool, job_id, "worker-a", None) is False
    assert await queue.fail(app_pool, claimed, "worker-a", "RuntimeError", retryable=True) == ""


async def _pool(pool: asyncpg.Pool) -> asyncpg.Pool:
    return pool


async def test_two_workers_schedule_one_heartbeat_per_slot(app_pool: asyncpg.Pool) -> None:
    rec = (Recurring(HEARTBEAT, every_seconds=60, keep_days=2),)
    hb = {HEARTBEAT: handlers.heartbeat}
    clock = 1_000_000.0
    for wid in ("a", "b"):
        await Worker(lambda: _pool(app_pool), hb, recurring=rec, worker_id=wid, clock=lambda: clock).maintenance(
            app_pool
        )
    rows = await app_pool.fetch("SELECT idempotency_key, status FROM crm.job WHERE kind = $1", HEARTBEAT)
    assert [(r["idempotency_key"], r["status"]) for r in rows] == [(f"{HEARTBEAT}:{int(clock // 60) + 1}", "queued")]


async def test_deploy_gate_count_skips_the_heartbeat(app_pool: asyncpg.Pool) -> None:
    await queue.enqueue(app_pool, HEARTBEAT, {}, idempotency_key="hb:1")
    assert (await queue.active_count(app_pool, DEPLOY_GATE_EXEMPT))["active"] == 0
    await queue.enqueue(app_pool, "crm_probe", {"tenant_id": TENANT})
    counts = await queue.active_count(app_pool, DEPLOY_GATE_EXEMPT)
    assert counts == {"active": 1, "by_status": {"queued": 1}, "by_kind": {"crm_probe": 1}}


async def test_personal_data_never_reaches_the_table(app_pool: asyncpg.Pool) -> None:
    with pytest.raises(PersonalDataInPayload):
        await queue.enqueue(app_pool, "crm_probe", {"tenant_id": TENANT, "email": "someone@example.com"})
    assert await app_pool.fetchval("SELECT count(*) FROM crm.job") == 0


async def test_kind_outside_the_crm_prefix_is_refused_by_the_table(app_pool: asyncpg.Pool) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await queue.enqueue(app_pool, "bk_probe", {})


async def test_retention_dry_run_then_real_run(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE crm_owner")
    await admin.execute(
        """
        INSERT INTO crm.job (kind, status, finished_at, created_at) VALUES
          ('crm_probe', 'succeeded', now() - interval '31 days', now() - interval '31 days'),
          ('crm_probe', 'succeeded', now() - interval '29 days', now() - interval '29 days'),
          ('crm_probe', 'cancelled', now() - interval '91 days', now() - interval '91 days'),
          ('crm_probe', 'failed',    now() - interval '200 days', now() - interval '200 days')
        """
    )
    # A parent row past its window is kept while a child points at it.
    await admin.execute(
        """
        WITH p AS (INSERT INTO crm.job (kind, status, finished_at)
                   VALUES ('crm_probe', 'succeeded', now() - interval '40 days') RETURNING id)
        INSERT INTO crm.job (kind, parent_job_id) SELECT 'crm_probe', id FROM p
        """
    )
    await admin.execute("RESET ROLE")
    job = Job("r1", JOB_RETENTION, {}, attempt=1, max_attempts=3)
    dry = await handlers.build_job_retention(dry_run=True)(app_pool, job)
    assert dry == {
        "dry_run": True,
        "succeeded_count": 2,
        "cancelled_count": 1,
        "failed_kept_count": 1,
        "deleted_count": 0,
    }
    assert await app_pool.fetchval("SELECT count(*) FROM crm.job") == 6
    real = await handlers.build_job_retention(dry_run=False)(app_pool, job)
    assert real["deleted_count"] == 2  # the 31-day succeeded row and the 91-day cancelled row; not the parent
    left = await app_pool.fetch("SELECT status, parent_job_id IS NOT NULL AS child FROM crm.job ORDER BY created_at")
    assert sorted((r["status"], r["child"]) for r in left) == [
        ("failed", False),
        ("queued", True),
        ("succeeded", False),
        ("succeeded", False),
    ]


def CLOCK() -> datetime:  # noqa: N802 — a fixed minute of the current hour: the hourly audit export is due
    return datetime.now(UTC).replace(minute=30, second=0, microsecond=0)


async def test_production_schedule_leaves_the_queue_idle_for_deploys_and_db_migrate(app_pool: asyncpg.Pool) -> None:
    """Review round (AA-933): with the production RECURRING and maintenance hooks, after the worker has run what is due
    the only queued rows are exempt ones. A queued-ahead daily retention row would keep the deploy gate false and make
    every DB Migrate apply refuse (exit 7) for every owner."""
    assert DEPLOY_GATE_EXEMPT == INFRA_IDLE_EXEMPT_CRM
    # Hour 0: the day's retention run is always due, so the hook queues it now (the worst case for the gate).
    worker = Worker(
        lambda: _pool(app_pool),
        handlers.build_handlers(retention_dry_run=True),
        recurring=handlers.RECURRING,
        hooks=handlers.build_hooks(retention_utc_hour=0, now=CLOCK),
        worker_id="worker-a",
    )
    await worker.maintenance(app_pool)
    rows = await app_pool.fetch("SELECT kind, run_after <= now() AS due FROM crm.job WHERE status = 'queued'")
    # AA-940: at minute 30 the hourly audit export is due too, and the daily audit retention at hour 0; AA-942: the
    # daily lifecycle run too.
    assert sorted((r["kind"], r["due"]) for r in rows) == [
        (AUDIT_EXPORT, True),
        (AUDIT_RETENTION, True),
        (HEARTBEAT, False),
        (JOB_RETENTION, True),
        (PLACEHOLDER_PURGE, True),  # AA-938: the hourly purge, queued due now like the daily retention
        (LIFECYCLE, True),
    ]
    # The due runs count while they wait (they would be interrupted by a deploy) ...
    assert (await queue.active_count(app_pool, DEPLOY_GATE_EXEMPT))["by_kind"] == {
        JOB_RETENTION: 1,
        AUDIT_EXPORT: 1,
        AUDIT_RETENTION: 1,
        PLACEHOLDER_PURGE: 1,
        LIFECYCLE: 1,
    }
    while await worker.run_once():
        pass
    # ... and once it has run, nothing but the exempt heartbeat slot is left, for the gate and for DB Migrate.
    assert (await queue.active_count(app_pool, DEPLOY_GATE_EXEMPT))["active"] == 0
    busy = await app_pool.fetch(INFRA_JOB_BUSY_SQL.format(table="crm.job"), sorted(INFRA_IDLE_EXEMPT_CRM))
    assert busy == []
    # Another worker (or a restart) the same day queues nothing new: one run per day, never a future-dated row.
    other = Worker(
        lambda: _pool(app_pool),
        handlers.build_handlers(retention_dry_run=True),
        recurring=handlers.RECURRING,
        hooks=handlers.build_hooks(retention_utc_hour=0, now=CLOCK),
        worker_id="worker-b",
    )
    await other.maintenance(app_pool)
    assert await app_pool.fetch(INFRA_JOB_BUSY_SQL.format(table="crm.job"), sorted(INFRA_IDLE_EXEMPT_CRM)) == []
    done = await app_pool.fetch("SELECT status, result FROM crm.job WHERE kind = $1", JOB_RETENTION)
    assert [r["status"] for r in done] == ["succeeded"] and json.loads(done[0]["result"])["dry_run"] is True
