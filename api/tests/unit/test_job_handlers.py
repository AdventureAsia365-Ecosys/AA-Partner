"""The skeleton's job kinds with a scripted pool (real SQL: tests/integration/test_job_runner.py)."""

from __future__ import annotations

import pytest

from app.jobs import handlers
from app.jobs.kinds import HEARTBEAT, JOB_RETENTION
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
    assert set(handlers.build_handlers(retention_dry_run=True)) == {r.kind for r in handlers.RECURRING}
