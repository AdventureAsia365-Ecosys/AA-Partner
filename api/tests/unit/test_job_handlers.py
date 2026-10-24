"""The skeleton's job kinds with a scripted pool (real SQL: tests/integration/test_job_runner.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.jobs import handlers
from app.jobs.kinds import DEPLOY_GATE_EXEMPT, HEARTBEAT, JOB_RETENTION
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job
from tests.unit.fakes import ScriptedConn

pytestmark = pytest.mark.asyncio

JOB = Job("j1", JOB_RETENTION, {}, 1, 3)
COUNTS = {"s": 2, "c": 1, "f": 4}


async def test_heartbeat_returns_nothing() -> None:
    assert await handlers.heartbeat(ScriptedConn(), Job("h", HEARTBEAT, {}, 1, 3)) is None


async def test_retention_dry_run_counts_only() -> None:
    conn = ScriptedConn().on("count(*) FILTER", COUNTS)
    result = await handlers.build_job_retention(dry_run=True)(conn, JOB)
    assert result == {
        "dry_run": True,
        "succeeded_count": 2,
        "cancelled_count": 1,
        "failed_kept_count": 4,
        "deleted_count": 0,
    }
    assert not conn.sql("DELETE")
    assert conn.sql("count(*) FILTER") == [(30, 90, 90)]
    check_payload(result, what="result")  # ids and counts only: the worker would accept it


async def test_retention_deletes_in_batches_and_never_failed_rows() -> None:
    ids = iter([["a", "b"], []])
    conn = (
        ScriptedConn()
        .on("count(*) FILTER", COUNTS)
        .on("SELECT j.id FROM crm.job", lambda *a: [{"id": i} for i in next(ids)])
    )
    conn.on("DELETE FROM crm.job", None)
    deleted = []

    async def execute(sql: str, *args: object) -> str:
        conn._answer(sql, args)
        if "DELETE" in sql:
            deleted.append(args[0])
            return f"DELETE {len(args[0])}"
        return "OK"

    conn.execute = execute  # type: ignore[method-assign]
    result = await handlers.build_job_retention(dry_run=False)(conn, JOB)
    assert result["deleted_count"] == 2 and deleted == [["a", "b"]]
    (eligible,) = {flat for flat, _ in conn.calls if "SELECT j.id FROM crm.job" in flat}
    assert (
        "'failed'" not in eligible and "NOT EXISTS (SELECT 1 FROM crm.job c WHERE c.parent_job_id = j.id)" in eligible
    )


async def test_recurring_and_handlers_match() -> None:
    handled = set(handlers.build_handlers(retention_dry_run=True))
    assert handled == {r.kind for r in handlers.RECURRING} | handlers.SCHEDULED_KINDS | handlers.EVENT_KINDS


async def test_only_deploy_gate_exempt_kinds_queue_ahead() -> None:
    """A Recurring kind always holds a future-dated queued row; the deploy gate and DB Migrate count it as busy unless
    the kind is exempt. So only exempt kinds may be Recurring (review round, AA-933)."""
    assert {r.kind for r in handlers.RECURRING} <= DEPLOY_GATE_EXEMPT


class FakeEnqueuePool:
    def __init__(self, conflict: bool = False) -> None:
        self.inserts: list[tuple[object, ...]] = []
        self.prunes: list[tuple[object, ...]] = []
        self.conflict = conflict

    async def fetchval(self, sql: str, *args: object) -> object:
        if "INSERT INTO crm.job" in sql:
            self.inserts.append(args)
            return None if self.conflict else f"job-{len(self.inserts)}"
        assert "DELETE FROM crm.job" in sql
        self.prunes.append(args)
        return 0


async def test_daily_schedule_queues_one_due_now_run_per_utc_day_after_the_hour() -> None:
    clock = {"now": datetime(2026, 10, 23, 18, 59, tzinfo=UTC)}
    hook = handlers.DailyRetentionSchedule(19, now=lambda: clock["now"])
    pool = FakeEnqueuePool()
    assert await hook(pool) is None and pool.inserts == []  # before the hour: nothing, not even a future row
    clock["now"] = datetime(2026, 10, 23, 19, 0, tzinfo=UTC)
    assert await hook(pool) == "job-1"
    (args,) = pool.inserts
    kind, payload, key, max_attempts, created_by, delay = args
    assert (kind, key, max_attempts, created_by) == (
        JOB_RETENTION,
        "crm_job_retention:schedule:2026-10-23",
        3,
        "scheduler:crm-retention",
    )
    assert delay == 0.0  # due now: never a future-dated row
    check_payload(json.loads(payload))
    assert pool.prunes == [(JOB_RETENTION, 30)]
    assert await hook(pool) is None and len(pool.inserts) == 1  # same day: no further INSERT
    clock["now"] = datetime(2026, 10, 24, 20, tzinfo=UTC)
    await hook(pool)
    assert len(pool.inserts) == 2


async def test_a_second_worker_finds_the_days_run_already_queued() -> None:
    pool = FakeEnqueuePool(conflict=True)
    hook = handlers.DailyRetentionSchedule(19, now=lambda: datetime(2026, 10, 23, 23, tzinfo=UTC))
    assert await hook(pool) is None and len(pool.inserts) == 1


async def test_schedule_hour_is_checked() -> None:
    with pytest.raises(ValueError, match="0..23"):
        handlers.DailyRetentionSchedule(24)
