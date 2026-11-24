"""S291 (AA-838, C4.3 W48 database review findings 4 and 5): the CRM job worker's database load, without a database.

1. ``app.pg_connection``: savepoints are named by depth (``crm_sp_1``, ``crm_sp_2``), so ``pg_stat_statements`` (which
   keys a utility statement by its text on PostgreSQL 15) sees a handful of SAVEPOINT texts, not one per transaction;
2. ``app.tenant_status`` (through ``app.tenancy.system_tx``): inside one Booking sync run of one agency (a
   ``memo`` scope) job contexts reuse an ``active`` status, never any other status (fail-closed); a writer of this
   process (lock, offboarding, erase, trash) reaches an open scope at once; outside a scope and in the platform context
   the status is read every time;
3. ``app.booking.proposal_checks`` (``app.booking.sync``): the minute run re-checks an agency's accepted proposals only
   when its last check left work or ``crm.jobs.booking_proposal_check_every_s`` has passed; a nudge run always checks;
4. ``app.jobs.worker``: the recurring kinds' history DELETE at most once per ``crm.jobs.history_prune_every_s``;
5. ``app.portability.restore_drill``: one fixed temporary table name for every restored table.

The PostgreSQL side (rollback semantics of the depth names, the drill on a real database) is in
``tests/integration/test_s291_crm_perf_db.py``.
"""

from __future__ import annotations

import ast
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from asyncpg.transaction import TransactionState

import app.jobs.worker as worker_module
from app import platform_settings as ps
from app import tenancy, tenant_status
from app.booking import proposal_checks, sync
from app.booking.proposal_checks import ProposalChecks
from app.booking.sync import BookingSync
from app.errors import Conflict, Forbidden
from app.jobs.worker import Recurring, Worker
from app.pg_connection import SAVEPOINT_PREFIX, StableSavepointConnection, StableSavepointTransaction, savepoint_name
from app.portability import restore_drill as rd
from app.tenancy import LifecycleContext
from tests import settings_serve
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import fake_tx, system_ctx

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
OTHER = uuid.UUID("00000000-0000-4000-8000-0000000000a2")
STATUS_SQL = "SELECT status FROM crm.tenant WHERE tenant_id"


# ── 1. depth-named savepoints ───────────────────────────────────────────────────────────────────────────────────────


class _Protocol:
    def is_in_transaction(self) -> bool:
        return False

    def is_connected(self) -> bool:
        return True


class FakeConnection(StableSavepointConnection):
    """The connection class without a server: ``execute`` records the statement text."""

    def __init__(self) -> None:  # noqa: D107 — asyncpg's own __init__ needs a live protocol
        self._top_xact = None
        self._pool_release_ctr = 0
        self._aborted = False
        self._protocol = _Protocol()
        self._crm_savepoints = []
        self.sent: list[str] = []
        self.fail_on: str | None = None

    def __del__(self) -> None:  # asyncpg's warns about an unclosed live connection; this one never opened
        pass

    async def execute(self, query: str, *args: Any, timeout: float | None = None) -> str:  # noqa: ASYNC109
        self.sent.append(query)
        if self.fail_on is not None and query.startswith(self.fail_on):
            raise RuntimeError("refused")
        return "OK"


@pytest.mark.asyncio
async def test_savepoints_are_named_by_their_depth_and_the_names_repeat() -> None:
    con = FakeConnection()
    for _ in range(3):  # three requests on one pooled connection: the same texts every time
        async with con.transaction():
            async with con.transaction():
                async with con.transaction():
                    pass
            async with con.transaction():  # a sibling block reuses depth 1
                pass
    assert isinstance(con.transaction(), StableSavepointTransaction)
    assert set(con.sent) == {
        "BEGIN;",
        "SAVEPOINT crm_sp_1;",
        "SAVEPOINT crm_sp_2;",
        "RELEASE SAVEPOINT crm_sp_2;",
        "RELEASE SAVEPOINT crm_sp_1;",
        "COMMIT;",
    }
    assert con.sent[:5] == ["BEGIN;", "SAVEPOINT crm_sp_1;", "SAVEPOINT crm_sp_2;", "RELEASE SAVEPOINT crm_sp_2;",
                            "RELEASE SAVEPOINT crm_sp_1;"]  # fmt: skip
    assert savepoint_name(3) == f"{SAVEPOINT_PREFIX}3" == "crm_sp_3"


@pytest.mark.asyncio
async def test_a_rolled_back_block_frees_its_depth_and_rolls_back_to_its_own_name() -> None:
    con = FakeConnection()
    async with con.transaction():
        with pytest.raises(ValueError):
            async with con.transaction():
                raise ValueError
        async with con.transaction():  # depth 1 again: the newest crm_sp_1 is this block's
            with pytest.raises(KeyError):
                async with con.transaction():
                    raise KeyError
    assert con.sent == [
        "BEGIN;",
        "SAVEPOINT crm_sp_1;",
        "ROLLBACK TO crm_sp_1;",
        "SAVEPOINT crm_sp_1;",
        "SAVEPOINT crm_sp_2;",
        "ROLLBACK TO crm_sp_2;",
        "RELEASE SAVEPOINT crm_sp_1;",
        "COMMIT;",
    ]


@pytest.mark.asyncio
async def test_a_savepoint_that_failed_to_start_takes_no_depth_and_a_new_transaction_starts_at_one() -> None:
    con = FakeConnection()
    tx = con.transaction()
    await tx.start()
    con.fail_on = "SAVEPOINT"
    with pytest.raises(RuntimeError):
        async with con.transaction():
            pass
    con.fail_on = None
    async with con.transaction():
        pass
    await tx.commit()
    # A savepoint left open by a broken caller does not leak into the next top-level transaction.
    con._crm_savepoints = [type("Stale", (), {"_state": TransactionState.STARTED})()]  # type: ignore[list-item]
    async with con.transaction():
        async with con.transaction():
            pass
    assert con.sent[-4:] == ["BEGIN;", "SAVEPOINT crm_sp_1;", "RELEASE SAVEPOINT crm_sp_1;", "COMMIT;"]
    assert con.sent.count("SAVEPOINT crm_sp_1;") == 3  # the failed start, the retry, the new transaction


def test_other_unique_ids_keep_asyncpg_s_counter() -> None:
    con = FakeConnection()
    first, second = con._get_unique_id("stmt"), con._get_unique_id("stmt")
    assert first != second and first.startswith("__asyncpg_stmt_")


def test_both_crm_pools_use_the_connection_class() -> None:
    import inspect

    from app import db

    assert "connection_class=StableSavepointConnection" in inspect.getsource(db.Database.pool)
    assert "connection_class=StableSavepointConnection" in inspect.getsource(rd._main)


# ── 2. the job contexts' status memo (one Booking sync run of one agency) ──────────────────────────────────────────


async def _open(conn: ScriptedConn, tenant: uuid.UUID = TENANT, actor: str = "job:crm_booking_sync") -> None:
    async with tenancy.system_tx(conn, tenant, actor=actor):  # type: ignore[arg-type]
        pass


@pytest.mark.asyncio
async def test_outside_a_scope_every_job_context_reads_the_status() -> None:
    """Intake, campaigns, imports, dispatch, mail import: no scope, so a lock is seen by the very next context."""
    conn = ScriptedConn().on(STATUS_SQL, "active")
    await _open(conn, actor="job:crm_intake_process")
    await _open(conn, actor="job:crm_intake_process")
    conn.on(STATUS_SQL, "locked")
    with pytest.raises(Forbidden):
        await _open(conn, actor="job:crm_intake_process")
    assert len(conn.sql(STATUS_SQL)) == 3


@pytest.mark.asyncio
async def test_a_scope_reads_each_agency_once_and_a_new_scope_reads_again() -> None:
    conn = ScriptedConn().on(STATUS_SQL, "active")
    with tenant_status.memo():
        for tenant in (TENANT, TENANT, OTHER, TENANT, OTHER):
            await _open(conn, tenant)
    assert len(conn.sql(STATUS_SQL)) == 2  # one read per agency
    conn.on(STATUS_SQL, "locked")  # changed by another process after the scope ended
    with tenant_status.memo(), pytest.raises(Forbidden):
        await _open(conn)
    assert len(conn.sql(STATUS_SQL)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["locked", "offboarding", "offboarded", None])
async def test_no_other_status_is_ever_remembered(status: str | None) -> None:
    conn = ScriptedConn().on(STATUS_SQL, status)
    with tenant_status.memo():
        for _ in range(3):
            with pytest.raises(Forbidden):
                await _open(conn)
    assert len(conn.sql(STATUS_SQL)) == 3


@pytest.mark.asyncio
async def test_a_writer_of_this_process_reaches_an_open_scope_at_once() -> None:
    """The reviewer's case: a run remembered "active", then a lock / offboarding / erase / trash in this process (the
    writer calls tenant_status.leaving): the run's next context reads the status again and is refused."""
    conn = ScriptedConn().on(STATUS_SQL, "active")
    with tenant_status.memo():
        await _open(conn)
        await _open(conn)
        assert tenant_status.leaving(TENANT) == TENANT  # inside the writer's transaction, not yet committed
        await _open(conn)  # reads again: still the committed "active" (as before S291)
        conn.on(STATUS_SQL, "offboarded")  # the writer commits
        with pytest.raises(Forbidden):
            await _open(conn)
    assert len(conn.sql(STATUS_SQL)) == 3
    # This process never remembers the agency again, even when it is active again (an unlock).
    conn.on(STATUS_SQL, "active")
    with tenant_status.memo():
        await _open(conn)
        await _open(conn)
    assert len(conn.sql(STATUS_SQL)) == 5


@pytest.mark.asyncio
async def test_a_writer_during_an_in_flight_read_wins() -> None:
    """leaving() runs while a context's status read is in flight (it returns the old "active"): not remembered."""

    def answer(*_: Any) -> str:
        tenant_status.leaving(TENANT)
        return "active"

    conn = ScriptedConn().on(STATUS_SQL, answer)
    with tenant_status.memo():
        await _open(conn)
        await _open(conn)
    assert len(conn.sql(STATUS_SQL)) == 2


@pytest.mark.asyncio
async def test_the_platform_context_always_reads_the_status() -> None:
    conn = ScriptedConn().on(STATUS_SQL, "active")
    with tenant_status.memo():
        await _open(conn)  # remembered for job contexts of this scope
        conn.on(STATUS_SQL, "locked")
        with pytest.raises(Conflict):
            async with tenancy.platform_tx(conn, TENANT, actor="platform:staff-1"):  # type: ignore[arg-type]
                pass


@pytest.mark.asyncio
async def test_the_booking_sync_runs_each_agency_in_its_own_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """BookingSync.__call__: every system_tx of one agency's run shares one status read; the next agency, the next
    run and code outside the run read again; a lock between two runs refuses the second (counted ``skipped``)."""
    conn = ScriptedConn().on(STATUS_SQL, "active")

    async def run_agency(_self: Any, pool: Any, tenant: uuid.UUID, _counts: Any, *, feed: bool) -> None:
        for _ in range(4):
            await _open(pool, tenant)

    async def agencies(_pool: Any) -> list[Any]:
        return [type("Agency", (), {"tenant_id": t})() for t in (TENANT, OTHER)]

    monkeypatch.setattr(BookingSync, "run_agency", run_agency)
    monkeypatch.setattr(sync.registry, "agencies", agencies)
    handler = BookingSync(object())  # type: ignore[arg-type]
    job = type("Job", (), {"payload": {}})()
    assert (await handler(conn, job))["status"] == "ok"  # type: ignore[arg-type]
    assert len(conn.sql(STATUS_SQL)) == 2  # one per agency run, not eight
    await _open(conn)  # outside the run: reads
    assert len(conn.sql(STATUS_SQL)) == 3
    conn.on(STATUS_SQL, lambda t: "locked" if t == TENANT else "active")
    result = await handler(conn, job)  # type: ignore[arg-type]
    assert result["skipped_count"] == 1 and len(conn.sql(STATUS_SQL)) == 5


# Every writer that moves an agency away from "active" (lock, offboarding, erase, trash) calls tenant_status.leaving in
# the function that runs its SQL (the reviewer's finding on the S291 cache). New writers are caught here.
LEAVING_SQL = re.compile(
    r"UPDATE\s+crm\.tenant\s+SET\b(?:(?!\bWHERE\b)[^;])*?(\bstatus\s*=\s*('(locked|offboarding|offboarded)'|\$\d)|\bdeleted_at\s*=)",
    re.I | re.S,
)
KNOWN_WRITERS = {
    ("app/cis/entitlement_feed.py", "SET_LOCK_SQL"),
    ("app/cis/membership_feed.py", "SET_LOCK_SQL"),
    ("app/lifecycle/offboarding.py", "TENANT_OFFBOARDING_SQL"),
    ("app/lifecycle/offboarding.py", "TENANT_OFFBOARDED_SQL"),
    ("app/lifecycle/tenant_erase.py", "OFFBOARDING_SQL"),
    ("app/lifecycle/tenant_erase.py", "ERASED_SQL"),
    ("app/ops/console.py", "SET_STATUS_SQL"),
    ("app/ops/console.py", "TRASH_TENANT_SQL"),
}


def _calls_leaving(fn: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "leaving"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "tenant_status"
        for n in ast.walk(fn)
    )


def test_every_writer_that_leaves_active_tells_the_status_memo() -> None:
    root = Path(__file__).resolve().parents[2]
    found: set[tuple[str, str]] = set()
    missing: list[str] = []
    for path in sorted((root / "app").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text())
        names = {
            t.id
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str) and LEAVING_SQL.search(node.value.value)
            for t in node.targets
            if isinstance(t, ast.Name)
        }  # fmt: skip
        found |= {(rel, n) for n in names}
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id in names}
            if used and not _calls_leaving(fn):
                missing.append(f"{rel}::{fn.name} runs {sorted(used)} without tenant_status.leaving(...)")
    assert found >= KNOWN_WRITERS, sorted(KNOWN_WRITERS - found)  # the pattern still sees every known writer
    assert not missing, missing


# ── 3. the Booking sync's accepted-proposal check ───────────────────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ProposalStep:
    """Stands in for BookingSync: ``proposal_sets`` counts calls and moves the counters it is told to."""

    def __init__(self) -> None:
        self.calls = 0
        self.moves: dict[str, int] = {}
        self.outage: str | None = None

    async def proposal_sets(self, pool: Any, tenant_id: uuid.UUID, counts: Counter[str]) -> str | None:
        self.calls += 1
        for k, v in self.moves.items():
            counts[k] += v
        return self.outage


async def _check(checks: ProposalChecks, step: Any, tenant: uuid.UUID = TENANT, *, nudged: bool = False) -> Any:
    return await checks.run(step, object(), tenant, Counter(), nudged=nudged)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_minute_run_skips_a_clean_agency_until_the_backstop_interval() -> None:
    assert ps.get(ps.JOBS_PROPOSAL_CHECK_EVERY_S) == 300  # the seeded default (crm-088)
    clock, step = Clock(), ProposalStep()
    checks = ProposalChecks(clock=clock)
    counts: Counter[str] = Counter()
    await checks.run(step, object(), TENANT, counts, nudged=False)  # type: ignore[arg-type]
    clock.now += 299
    await checks.run(step, object(), TENANT, counts, nudged=False)  # type: ignore[arg-type]
    assert step.calls == 1 and counts["proposal_check_skipped"] == 1
    await _check(checks, step, OTHER)
    assert step.calls == 2  # another agency has its own memory
    clock.now += 1
    await _check(checks, step)
    assert step.calls == 3
    await _check(checks, step, nudged=True)  # a nudge (an accept, a paid deposit) always checks
    assert step.calls == 4
    settings_serve.serve({ps.JOBS_PROPOSAL_CHECK_EVERY_S: 60})
    try:
        clock.now += 60
        await _check(checks, step)
    finally:
        settings_serve.reset()
    assert step.calls == 5
    checks.forget()
    await _check(checks, step)
    assert step.calls == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "moves,outage",
    [
        ({"proposal_lines_held": 1}, None),  # a set was due and written
        ({"proposal_lines_retry": 1}, None),  # a set was due, Booking to ask again
        ({"confirms_queued": 1}, None),
        ({"confirms_error": 1}, None),
        ({}, "booking_timeout"),
    ],
)
async def test_an_agency_with_work_left_is_checked_on_the_next_run(moves: dict[str, int], outage: str | None) -> None:
    step = ProposalStep()
    step.moves, step.outage = moves, outage
    checks = ProposalChecks(clock=Clock())
    for _ in range(2):
        out = await _check(checks, step)
    assert step.calls == 2 and out == outage
    step.moves, step.outage = {"confirms_queued": 0}, None  # nothing queued is no work
    await _check(checks, step)
    await _check(checks, step)
    assert step.calls == 3  # clean now: the fourth run skips


@pytest.mark.asyncio
async def test_a_failing_check_is_not_remembered() -> None:
    class Locked:
        async def proposal_sets(self, *_a: Any) -> None:
            raise Forbidden("tenant_locked")

    checks = ProposalChecks(clock=Clock())
    await _check(checks, ProposalStep())
    with pytest.raises(Forbidden):
        await _check(checks, Locked(), nudged=True)
    assert TENANT not in checks.clean_at


@pytest.mark.asyncio
async def test_the_scheduled_run_goes_through_the_process_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """BookingSync.run_agency: the minute run (feed on) is gated, a nudge run (feed off) always checks."""
    seen: list[bool] = []

    async def run(_sync: Any, _pool: Any, _tenant: Any, _counts: Any, *, nudged: bool) -> str:
        seen.append(nudged)
        return "booking_timeout"  # an outage: run_agency stops before the request queue

    monkeypatch.setattr(proposal_checks.CHECKS, "run", run)
    monkeypatch.setattr(sync, "system_tx", fake_tx(system_ctx()))  # the request queue: nothing claimed
    handler = BookingSync(None)
    for feed in (True, False):
        await handler.run_agency(object(), TENANT, Counter(), feed=feed)  # type: ignore[arg-type]
    assert seen == [False, True]


# ── 4. the recurring kinds' history prune ───────────────────────────────────────────────────────────────────────────


class PruneQueue:
    def __init__(self) -> None:
        self.pruned: list[str] = []

    async def reap(self, pool: Any) -> dict[str, int]:
        return {}

    async def enqueue(self, *a: Any, **k: Any) -> None:
        return None

    async def prune(self, pool: Any, kind: str, keep_days: int) -> int:
        self.pruned.append(kind)
        return 0


@pytest.mark.asyncio
async def test_the_history_prune_runs_at_most_once_per_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = PruneQueue()
    for name in ("reap", "enqueue", "prune"):
        monkeypatch.setattr(worker_module.queue, name, getattr(fake, name))
    clock = Clock()
    rec = (Recurring("crm_a", 60, keep_days=1), Recurring("crm_b", 20, keep_days=1))

    async def ok(pool: Any, job: Any) -> None:
        return None

    w = Worker(lambda: None, {"crm_a": ok, "crm_b": ok}, recurring=rec, clock=clock,  # type: ignore[arg-type]
               prune_every_s=lambda: float(ps.get(ps.JOBS_HISTORY_PRUNE_EVERY_S)))  # fmt: skip
    assert ps.get(ps.JOBS_HISTORY_PRUNE_EVERY_S) == 60  # the seeded default (crm-088)
    await w.maintenance(object())  # type: ignore[arg-type]
    clock.now += 30
    await w.maintenance(object())  # type: ignore[arg-type]
    assert fake.pruned == ["crm_a", "crm_b"]
    clock.now += 30
    await w.maintenance(object())  # type: ignore[arg-type]
    assert fake.pruned == ["crm_a", "crm_b"] * 2
    # Without the setting (the ported template), every pass prunes.
    plain = Worker(lambda: None, {"crm_a": ok, "crm_b": ok}, recurring=rec, clock=clock)  # type: ignore[arg-type]
    await plain.maintenance(object())  # type: ignore[arg-type]
    await plain.maintenance(object())  # type: ignore[arg-type]
    assert fake.pruned == ["crm_a", "crm_b"] * 4


# ── 5. the restore drill's temporary table ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_restore_drill_reuses_one_fixed_temporary_table_name() -> None:
    conn = ScriptedConn()
    conn.on("FROM pg_catalog.pg_attribute", [{"name": "note_id", "type": "uuid"}, {"name": "body", "type": "text"}])
    conn.on("SELECT count(*)", 1)
    ctx = LifecycleContext(conn=conn, tenant_id=TENANT, actor=rd.ACTOR, status="active")  # type: ignore[arg-type]
    row = {"note_id": "00000000-0000-4000-8000-0000000000b1", "body": "Invented note"}
    for n, table in enumerate(("note", "task")):
        result = await rd.restore_table(ctx, n, {"table": table, "rows": 1}, [row], ("note_id",))
        assert result.restored_rows == 1 and result.ok
    assert rd.TEMP_TABLE == "rd_drill"
    texts = [flat for flat, _ in conn.calls]
    temp_texts = [t for t in texts if "pg_temp" in t or "TEMP TABLE" in t]
    assert temp_texts and all("rd_drill" in t for t in temp_texts)
    assert not any("rd_0" in t or "rd_1" in t for t in texts)
    # Each table drops the table before the next one reuses the name; the per-table texts repeat exactly.
    assert texts.count(rd.DROP_TEMP_SQL) == 2
    insert = [t for t in texts if t.startswith("INSERT INTO pg_temp.rd_drill")]
    assert len(insert) == 2 and insert[0] == insert[1]
