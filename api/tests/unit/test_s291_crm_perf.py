"""S291 (AA-838, C4.3 W48 database review findings 4 and 5): the CRM job worker's database load, without a database.

1. ``app.pg_connection``: savepoints are named by depth (``crm_sp_1``, ``crm_sp_2``), so ``pg_stat_statements`` (which
   keys a utility statement by its text on PostgreSQL 15) sees a handful of SAVEPOINT texts, not one per transaction;
2. ``app.tenancy``: a job context keeps an ``active`` status for ``crm.jobs.tenant_status_cache_s`` seconds, never any
   other status (fail-closed), and the platform context always reads it;
3. ``app.booking.sync``: the minute run re-checks an agency's accepted proposals only when its last check left work or
   ``crm.jobs.booking_proposal_check_every_s`` has passed; a nudge run always checks;
4. ``app.jobs.worker``: the recurring kinds' history DELETE at most once per ``crm.jobs.history_prune_every_s``;
5. ``app.portability.restore_drill``: one fixed temporary table name for every restored table.

The PostgreSQL side (rollback semantics of the depth names, the drill on a real database) is in
``tests/integration/test_s291_crm_perf_db.py``.
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

import pytest
from asyncpg.transaction import TransactionState

import app.jobs.worker as worker_module
from app import platform_settings as ps
from app import tenancy
from app.booking.sync import BookingSync
from app.errors import Conflict, Forbidden
from app.jobs.worker import Recurring, Worker
from app.pg_connection import SAVEPOINT_PREFIX, StableSavepointConnection, StableSavepointTransaction, savepoint_name
from app.portability import restore_drill as rd
from app.tenancy import LifecycleContext
from tests import settings_serve
from tests.unit.fakes import ScriptedConn

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


# ── 2. the job contexts' status cache ───────────────────────────────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(tenancy, "_status_clock", c)
    return c


async def _open(conn: ScriptedConn, tenant: uuid.UUID = TENANT) -> None:
    async with tenancy.system_tx(conn, tenant, actor="job:crm_booking_sync"):  # type: ignore[arg-type]
        pass


@pytest.mark.asyncio
async def test_a_job_context_keeps_active_for_the_window_then_reads_again(clock: Clock) -> None:
    assert ps.get(ps.JOBS_TENANT_STATUS_CACHE_S) == 10  # the seeded default (crm-088)
    conn = ScriptedConn().on(STATUS_SQL, "active")
    await _open(conn)
    clock.now += 9.9
    await _open(conn)
    assert len(conn.sql(STATUS_SQL)) == 1  # one read for both contexts
    # The agency is locked meanwhile: refused once the window has passed (the documented bound).
    conn.on(STATUS_SQL, "locked")
    clock.now += 0.1
    with pytest.raises(Forbidden):
        await _open(conn)
    # A refusal is never kept: an unlock is seen at the next context, a lock again at the one after.
    conn.on(STATUS_SQL, "active")
    await _open(conn)
    assert len(conn.sql(STATUS_SQL)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["locked", "offboarding", "offboarded", None])
async def test_no_other_status_is_ever_cached(clock: Clock, status: str | None) -> None:
    conn = ScriptedConn().on(STATUS_SQL, status)
    for _ in range(3):
        with pytest.raises(Forbidden):
            await _open(conn)
    assert len(conn.sql(STATUS_SQL)) == 3 and tenancy._JOB_STATUS_SEEN == {}


@pytest.mark.asyncio
async def test_zero_turns_the_cache_off_and_the_cache_is_per_agency(clock: Clock) -> None:
    conn = ScriptedConn().on(STATUS_SQL, "active")
    await _open(conn, TENANT)
    await _open(conn, OTHER)
    assert len(conn.sql(STATUS_SQL)) == 2  # each agency is read once
    settings_serve.serve({ps.JOBS_TENANT_STATUS_CACHE_S: 0})
    try:
        await _open(conn, TENANT)
        await _open(conn, TENANT)
    finally:
        settings_serve.reset()
    assert len(conn.sql(STATUS_SQL)) == 4 and TENANT not in tenancy._JOB_STATUS_SEEN
    tenancy.forget_job_status(OTHER)
    assert tenancy._JOB_STATUS_SEEN == {}


@pytest.mark.asyncio
async def test_the_cache_is_bounded(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tenancy, "_JOB_STATUS_CACHE_MAX", 2)
    conn = ScriptedConn().on(STATUS_SQL, "active")
    for tenant in (TENANT, OTHER, uuid.UUID("00000000-0000-4000-8000-0000000000a3")):
        await _open(conn, tenant)
    assert len(tenancy._JOB_STATUS_SEEN) == 2 and TENANT not in tenancy._JOB_STATUS_SEEN


@pytest.mark.asyncio
async def test_the_platform_context_always_reads_the_status(clock: Clock) -> None:
    conn = ScriptedConn().on(STATUS_SQL, "active")
    await _open(conn)  # cached for job contexts
    conn.on(STATUS_SQL, "locked")
    with pytest.raises(Conflict):
        async with tenancy.platform_tx(conn, TENANT, actor="platform:staff-1"):  # type: ignore[arg-type]
            pass


# ── 3. the Booking sync's accepted-proposal check ───────────────────────────────────────────────────────────────────


class ProposalStep:
    """Stands in for BookingSync.proposal_sets: counts calls, moves the counters it is told to."""

    def __init__(self) -> None:
        self.calls = 0
        self.moves: dict[str, int] = {}
        self.outage: str | None = None

    async def __call__(self, pool: Any, tenant_id: uuid.UUID, counts: Counter[str]) -> str | None:
        self.calls += 1
        for k, v in self.moves.items():
            counts[k] += v
        return self.outage


@pytest.fixture
def step(monkeypatch: pytest.MonkeyPatch) -> ProposalStep:
    s = ProposalStep()

    async def proposal_sets(_self: Any, pool: Any, tenant_id: uuid.UUID, counts: Counter[str]) -> str | None:
        return await s(pool, tenant_id, counts)

    monkeypatch.setattr(BookingSync, "proposal_sets", proposal_sets)
    return s


@pytest.mark.asyncio
async def test_the_minute_run_skips_a_clean_agency_until_the_backstop_interval(step: ProposalStep) -> None:
    assert ps.get(ps.JOBS_PROPOSAL_CHECK_EVERY_S) == 300  # the seeded default (crm-088)
    clock = Clock()
    handler = BookingSync(None, clock=clock)
    counts: Counter[str] = Counter()
    await handler.checked_proposal_sets(object(), TENANT, counts, nudged=False)  # type: ignore[arg-type]
    clock.now += 299
    await handler.checked_proposal_sets(object(), TENANT, counts, nudged=False)  # type: ignore[arg-type]
    assert step.calls == 1 and counts["proposal_check_skipped"] == 1
    await handler.checked_proposal_sets(object(), OTHER, counts, nudged=False)  # type: ignore[arg-type]
    assert step.calls == 2  # another agency has its own memory
    clock.now += 1
    await handler.checked_proposal_sets(object(), TENANT, counts, nudged=False)  # type: ignore[arg-type]
    assert step.calls == 3
    # A nudge (an accept, a paid deposit) always checks.
    await handler.checked_proposal_sets(object(), TENANT, counts, nudged=True)  # type: ignore[arg-type]
    assert step.calls == 4
    settings_serve.serve({ps.JOBS_PROPOSAL_CHECK_EVERY_S: 60})
    try:
        clock.now += 60
        await handler.checked_proposal_sets(object(), TENANT, counts, nudged=False)  # type: ignore[arg-type]
    finally:
        settings_serve.reset()
    assert step.calls == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "moves,outage",
    [
        ({"proposal_sets_due": 1}, None),  # a set was due (written or to retry)
        ({"confirms_queued": 1}, None),
        ({"confirms_error": 1}, None),
        ({}, "booking_timeout"),
    ],
)
async def test_an_agency_with_work_left_is_checked_on_the_next_run(
    step: ProposalStep, moves: dict[str, int], outage: str | None
) -> None:
    step.moves, step.outage = moves, outage
    handler = BookingSync(None, clock=Clock())
    for _ in range(2):
        out = await handler.checked_proposal_sets(object(), TENANT, Counter(), nudged=False)  # type: ignore[arg-type]
    assert step.calls == 2 and out == outage
    step.moves, step.outage = {}, None
    await handler.checked_proposal_sets(object(), TENANT, Counter(), nudged=False)  # type: ignore[arg-type]
    await handler.checked_proposal_sets(object(), TENANT, Counter(), nudged=False)  # type: ignore[arg-type]
    assert step.calls == 3  # clean now: the fourth run skips


@pytest.mark.asyncio
async def test_a_failing_check_is_not_remembered(step: ProposalStep, monkeypatch: pytest.MonkeyPatch) -> None:
    async def locked(*_a: Any) -> None:
        raise Forbidden("tenant_locked")

    handler = BookingSync(None, clock=Clock())
    await handler.checked_proposal_sets(object(), TENANT, Counter(), nudged=False)  # type: ignore[arg-type]
    monkeypatch.setattr(BookingSync, "proposal_sets", locked)
    with pytest.raises(Forbidden):
        await handler.checked_proposal_sets(object(), TENANT, Counter(), nudged=True)  # type: ignore[arg-type]
    assert TENANT not in handler._proposal_checks


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
