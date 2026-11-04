"""AA-969 (CRM-37a) on PostgreSQL 15: card R "a noisy tenant cannot push another tenant past the SLO" (the agency
share of the pool on the request path, the fair crm.job claim per agency) and card E "a Spot interruption loses no
jobs".

A Fargate Spot interruption is a SIGTERM (two minutes' notice; ECS stops the task after ``stopTimeout`` 30 s):
uvicorn runs the lifespan shutdown, the worker gets ``WORKER_STOP_TIMEOUT_S`` and is then cancelled, and the
cancelled job is **released** (back to the queue, due now, the attempt not counted). When the task dies without that
(SIGKILL, host loss), the lease runs out and the reaper re-queues the row (``test_job_runner``). Jobs are enqueued in
the same transaction as the data they act on, so a job is never lost between the request and the queue.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest
import pytest_asyncio

from app import tenant_gate
from app.errors import RateLimited
from app.jobs import queue
from app.jobs.queue import Job
from app.jobs.worker import Worker
from app.tenancy import tenant_tx
from tests.integration.test_crm002_tenancy import _seed, caller

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOISY = "00000000-0000-4000-8000-00000000a969"
CALM = "00000000-0000-4000-8000-00000000b969"
OTHER = "00000000-0000-4000-8000-00000000c969"


@pytest_asyncio.fixture(autouse=True)
async def empty_queue(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE crm_owner")
    await admin.execute("DELETE FROM crm.job")
    await admin.execute("RESET ROLE")


async def _pool(pool: asyncpg.Pool) -> asyncpg.Pool:
    return pool


async def _enqueue(pool: asyncpg.Pool, tenant: str | None, n: int, age_s: int) -> list[str]:
    """``n`` due jobs of one agency (or none), queued ``age_s`` seconds ago (older = smaller run_after)."""
    ids = []
    for i in range(n):
        payload = {"tenant_id": tenant} if tenant else {}
        job_id = await queue.enqueue(pool, "crm_probe", payload)
        await pool.execute(
            "UPDATE crm.job SET run_after = now() - make_interval(secs => $2) WHERE id = $1::uuid", job_id, age_s - i
        )
        ids.append(job_id)
    return ids


async def _claim_order(pool: asyncpg.Pool, n: int) -> list[str]:
    order = []
    for _ in range(n):
        job = await queue.claim(pool, "w", ["crm_probe"], 60)
        assert job is not None
        order.append(job.id)
        assert await queue.complete(pool, job.id, "w", None)
    return order


async def test_a_backlog_of_one_agency_does_not_hold_back_the_others(app_pool: asyncpg.Pool) -> None:
    noisy = await _enqueue(app_pool, NOISY, 30, age_s=600)  # 30 jobs, all older than the others
    calm = await _enqueue(app_pool, CALM, 2, age_s=60)
    other = await _enqueue(app_pool, OTHER, 1, age_s=30)
    system = await _enqueue(app_pool, None, 1, age_s=10)
    order = await _claim_order(app_pool, 34)
    # Turn 1 of every group first (oldest first), then turn 2 (an agency just served counts it), ...: the calm agencies
    # and the platform job are done within the first four claims instead of after the noisy agency's 30.
    assert order[:4] == [noisy[0], calm[0], other[0], system[0]]
    assert order[4:6] == [noisy[1], calm[1]]
    assert order[6:] == noisy[2:]  # inside one agency the order stays oldest first


async def test_a_backlog_longer_than_any_window_still_lets_the_calm_agency_through(app_pool: asyncpg.Pool) -> None:
    """Review finding: an oldest-500 window held only the noisy agency's rows once its backlog was longer than 500, so
    the calm agency's newer job waited behind it again. Every due row is ranked inside its agency now."""
    await app_pool.execute(
        """
        INSERT INTO crm.job (kind, payload, run_after)
        SELECT 'crm_probe', jsonb_build_object('tenant_id', $1::text), now() - make_interval(secs => 7200 - g)
          FROM generate_series(1, 600) AS g
        """,
        NOISY,
    )
    (calm,) = await _enqueue(app_pool, CALM, 1, age_s=5)
    claimed = await _claim_order(app_pool, 2)
    assert calm in claimed, claimed


async def test_a_held_head_row_does_not_leave_its_agency_without_a_candidate(app_pool: asyncpg.Pool) -> None:
    noisy = await _enqueue(app_pool, NOISY, 3, age_s=100)
    async with app_pool.acquire() as holder, holder.transaction():
        await holder.execute("SELECT 1 FROM crm.job WHERE id = $1::uuid FOR UPDATE", noisy[0])
        job = await queue.claim(app_pool, "w", ["crm_probe"], 60)
    assert job is not None and job.id == noisy[1]


async def test_rows_without_an_agency_never_queue_behind_each_other_by_agency(app_pool: asyncpg.Pool) -> None:
    system = await _enqueue(app_pool, None, 3, age_s=100)
    noisy = await _enqueue(app_pool, NOISY, 3, age_s=50)
    assert await _claim_order(app_pool, 6) == [*system, noisy[0], noisy[1], noisy[2]]


async def test_fair_claim_keeps_skip_locked_for_two_workers(app_pool: asyncpg.Pool) -> None:
    await _enqueue(app_pool, NOISY, 5, age_s=100)
    await _enqueue(app_pool, CALM, 1, age_s=50)
    first, second = await asyncio.gather(
        queue.claim(app_pool, "w1", ["crm_probe"], 60), queue.claim(app_pool, "w2", ["crm_probe"], 60)
    )
    assert first is not None and second is not None and first.id != second.id
    claimed = {first.payload["tenant_id"], second.payload["tenant_id"]}
    assert claimed == {NOISY, CALM}


async def test_cancel_requests_and_future_rows_are_still_never_claimed(app_pool: asyncpg.Pool) -> None:
    (cancelled,) = await _enqueue(app_pool, CALM, 1, age_s=100)
    await app_pool.execute("UPDATE crm.job SET cancel_requested = true WHERE id = $1::uuid", cancelled)
    later = await queue.enqueue(app_pool, "crm_probe", {"tenant_id": OTHER}, delay_seconds=3600)
    assert later is not None
    assert await queue.claim(app_pool, "w", ["crm_probe"], 60) is None


# ── card E: a Spot interruption loses no jobs ───────────────────────────────────────────────────────────────────


async def test_spot_interruption_releases_the_running_job_and_the_next_task_finishes_it(
    app_pool: asyncpg.Pool,
) -> None:
    started = asyncio.Event()
    runs: list[str] = []

    async def slow(pool: asyncpg.Pool, job: Job) -> dict:
        runs.append(job.id)
        started.set()
        await asyncio.sleep(3600)  # still running when the task is told to stop
        return {}

    (job_id,) = await _enqueue(app_pool, CALM, 1, age_s=5)
    interrupted = Worker(lambda: _pool(app_pool), {"crm_probe": slow}, worker_id="spot-task", poll_seconds=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(interrupted.run_forever(stop))
    await asyncio.wait_for(started.wait(), 10)
    # The lifespan on SIGTERM: stop, give the job its grace period, then cancel the worker task.
    stop.set()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await app_pool.fetchrow(
        "SELECT status, attempt, locked_by, run_after <= now() AS due, progress FROM crm.job WHERE id = $1::uuid",
        job_id,
    )
    assert (row["status"], row["attempt"], row["locked_by"], row["due"]) == ("queued", 0, None, True)
    assert json.loads(row["progress"])["releases"] == 1

    async def quick(pool: asyncpg.Pool, job: Job) -> dict:
        runs.append(job.id)
        return {"tenant_id": job.payload["tenant_id"]}

    replacement = Worker(lambda: _pool(app_pool), {"crm_probe": quick}, worker_id="on-demand-task")
    assert await replacement.run_once() is True
    row = await app_pool.fetchrow("SELECT status, attempt, result FROM crm.job WHERE id = $1::uuid", job_id)
    assert (row["status"], row["attempt"]) == ("succeeded", 1)  # the interrupted attempt was not counted
    assert runs == [job_id, job_id]


async def test_repeated_interruptions_end_visibly_failed_never_silently_lost(app_pool: asyncpg.Pool) -> None:
    """A job whose task keeps being replaced mid-run ends ``failed`` after MAX_RELEASES (the L1 console and the ops
    checks show it; the retryable kinds can be re-queued there), instead of looping or disappearing."""
    (job_id,) = await _enqueue(app_pool, CALM, 1, age_s=5)
    statuses = []
    for n in range(queue.MAX_RELEASES):
        job = await queue.claim(app_pool, f"spot-{n}", ["crm_probe"], 60)
        assert job is not None and job.id == job_id
        statuses.append(await queue.release(app_pool, job_id, f"spot-{n}"))
    assert statuses == ["queued"] * (queue.MAX_RELEASES - 1) + ["failed"]
    assert await app_pool.fetchval("SELECT count(*) FROM crm.job WHERE id = $1::uuid", job_id) == 1


# ── card R on the request path: tenant_tx() on the 3-connection pool ────────────────────────────────────────────


async def _flood(pool: asyncpg.Pool, tenant: object, sub: str, n: int, hold_s: float) -> list[asyncio.Task[str]]:
    async def slow() -> str:
        try:
            async with tenant_tx(pool, caller(sub), tenant) as ctx:
                await ctx.conn.execute("SELECT pg_sleep($1)", hold_s)
            return "ok"
        except RateLimited as exc:
            return exc.code

    return [asyncio.create_task(slow()) for _ in range(n)]


async def _calm_latency(pool: asyncpg.Pool, tenant: object, sub: str) -> float:
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with tenant_tx(pool, caller(sub), tenant) as ctx:
        assert await ctx.conn.fetchval("SELECT 1") == 1
    return loop.time() - started


async def _prove(pool: asyncpg.Pool, *agencies: tuple[object, str]) -> None:
    # Each agency's first request proves the membership (the slots are then keyed by agency).
    for tenant, sub in agencies:
        async with tenant_tx(pool, caller(sub), tenant):
            pass


async def test_a_noisy_agency_cannot_hold_the_pool_from_a_calm_one(app_pool: asyncpg.Pool) -> None:
    """Twelve slow requests of one agency at once, then one quick request of another. The request path holds at most
    two of the three connections (the third stays free for the crm.job worker, checked here with a plain acquire),
    and since review round 3 one agency never holds both of them, even into an idle gate: the calm request gets the
    free slot at once, without waiting for any noisy transaction (first come first served would put it behind all
    twelve: about 1.5 s here; round 2 behind one, 0.25 s). The noisy surplus is 429 ``tenant_busy``."""
    noisy_tenant, noisy_sub = await _seed(app_pool)
    calm_tenant, calm_sub = await _seed(app_pool)
    before = tenant_gate.GATE
    tenant_gate.GATE = tenant_gate.TenantGate(2, 0.6)
    try:
        await _prove(app_pool, (noisy_tenant, noisy_sub), (calm_tenant, calm_sub))
        noisy = await _flood(app_pool, noisy_tenant, noisy_sub, 12, 0.25)
        await asyncio.sleep(0.05)  # the noisy burst holds its share: one slot, never the last free one
        assert tenant_gate.GATE.in_use() == 1 and tenant_gate.GATE.in_use(f"tenant:{noisy_tenant}") == 1
        async with app_pool.acquire(timeout=0.2) as worker_conn:  # the worker's connection is still there
            assert await worker_conn.fetchval("SELECT 1") == 1
        calm_latency = await _calm_latency(app_pool, calm_tenant, calm_sub)
        results = await asyncio.gather(*noisy)
    finally:
        tenant_gate.GATE = before
    assert calm_latency < 0.2, calm_latency  # no noisy transaction (0.25 s each) to wait for, never the noisy queue
    assert results.count("ok") >= 2 and "tenant_busy" in results, results


async def test_two_noisy_agencies_cannot_hold_the_pool_from_a_calm_one(app_pool: asyncpg.Pool) -> None:
    """Review round 2: two busy agencies (2 + 2 slots before) used to fill the pool; now they share the request share
    one slot each, the worker connection stays free and a calm agency still gets the next slot that frees up."""
    a_tenant, a_sub = await _seed(app_pool)
    b_tenant, b_sub = await _seed(app_pool)
    calm_tenant, calm_sub = await _seed(app_pool)
    before = tenant_gate.GATE
    tenant_gate.GATE = tenant_gate.TenantGate(2, 3.0)
    try:
        await _prove(app_pool, (a_tenant, a_sub), (b_tenant, b_sub), (calm_tenant, calm_sub))
        noisy = await _flood(app_pool, a_tenant, a_sub, 8, 0.2)
        await asyncio.sleep(0.02)
        noisy += await _flood(app_pool, b_tenant, b_sub, 8, 0.2)
        await asyncio.sleep(0.25)  # one round later: a and b hold one slot each
        gate = tenant_gate.GATE
        assert (gate.in_use(f"tenant:{a_tenant}"), gate.in_use(f"tenant:{b_tenant}")) == (1, 1)
        async with app_pool.acquire(timeout=0.2) as worker_conn:
            assert await worker_conn.fetchval("SELECT 1") == 1
        calm_latency = await _calm_latency(app_pool, calm_tenant, calm_sub)
        results = await asyncio.gather(*noisy)
    finally:
        tenant_gate.GATE = before
    assert calm_latency < 0.4, calm_latency  # behind 14 queued noisy requests it would be about 1.4 s
    assert results.count("ok") == 16  # the busy agencies are slowed down, not refused, within the slot wait
