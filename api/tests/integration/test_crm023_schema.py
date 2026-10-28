"""crm-023 (AA-993 G4b) on PostgreSQL with the RDS role shape: the ``crm_dispatch`` send marker.

- ``crm.dispatch_attempt``: crm_app inserts, reads and closes (decided_at, outcome) only; one open attempt per dispatch
  row; a closed attempt never changes (CRM38); FORCE RLS; the trip and agency erase take it before the dispatch row.
- ``crm.dispatch_queue.gate_result`` takes ``send_uncertain``.

The worker's use of it (two transactions, cancel before / after the provider call) is in
tests/integration/test_dispatch_marker_db.py.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "023_crm_dispatch_attempt.sql"


async def test_crm023_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    # Earlier test modules re-apply their own erase_subject text: put this key's back first.
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-023'") == 1
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    src = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    assert src.count("'dispatch_attempt'") == 3 and "'prep_pack_selection'" in src
    assert src.index("'dispatch_attempt', ''") < src.index("'dispatch_queue', ''")


async def test_privileges(admin: asyncpg.Connection) -> None:
    table = "crm.dispatch_attempt"
    for privilege, want in (("SELECT", True), ("INSERT", True), ("UPDATE", False), ("DELETE", False),
                            ("TRUNCATE", False)):  # fmt: skip
        assert await admin.fetchval("SELECT has_table_privilege('crm_app', $1, $2)", table, privilege) is want
    for column, want in (("decided_at", True), ("outcome", True), ("dispatch_id", False), ("tenant_id", False)):
        assert await admin.fetchval("SELECT has_column_privilege('crm_app', $1, $2, 'UPDATE')", table, column) is want
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, table, privilege)


async def _dispatch(pool: asyncpg.Pool, ids: dict[str, Any]) -> uuid.UUID:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        return await conn.fetchval(
            "INSERT INTO crm.dispatch_queue (traveler_id, trip_id, channel, purpose, origin) "
            "VALUES ($1, $2, 'email', 'transactional', 'system') RETURNING dispatch_id",
            ids["lead"],
            ids["trip"],
        )


async def test_one_open_attempt_per_row_closed_once_and_isolated(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    dispatch = await _dispatch(app_pool, ids)
    insert = "INSERT INTO crm.dispatch_attempt (dispatch_id, job_attempt) VALUES ($1, 1) RETURNING attempt_id"
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            attempt = await conn.fetchval(insert, dispatch)
        with pytest.raises(asyncpg.UniqueViolationError):  # dispatch_attempt_open_uq
            async with conn.transaction():
                await _ctx(conn, ids["tenant"])
                await conn.fetchval(insert, dispatch)
        with pytest.raises(asyncpg.CheckViolationError):  # an outcome needs its decided_at, and a known code
            async with conn.transaction():
                await _ctx(conn, ids["tenant"])
                await conn.execute("UPDATE crm.dispatch_attempt SET outcome = 'maybe', decided_at = now()")
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            await conn.execute(
                "UPDATE crm.dispatch_attempt SET decided_at = now(), outcome = 'sent' WHERE attempt_id = $1", attempt
            )
            assert await conn.fetchval(insert, dispatch) is not None  # closed: a new attempt may open
        with pytest.raises(asyncpg.PostgresError, match="attempt_decided") as err:
            async with conn.transaction():
                await _ctx(conn, ids["tenant"])
                await conn.execute("UPDATE crm.dispatch_attempt SET outcome = 'retry' WHERE attempt_id = $1", attempt)
        assert err.value.sqlstate == "CRM38"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await _ctx(conn, ids["tenant"])
                await conn.execute("DELETE FROM crm.dispatch_attempt")
        async with conn.transaction():  # FORCE RLS: another agency sees none of them
            await _ctx(conn, other["tenant"])
            assert await conn.fetchval("SELECT count(*) FROM crm.dispatch_attempt") == 0
    assert await _count(app_pool, ids["tenant"], "dispatch_attempt") == 2


async def test_an_attempt_names_a_dispatch_row_of_its_own_agency_only(app_pool: asyncpg.Pool) -> None:
    """dispatch_attempt_dispatch_fk is the same-agency key (tenant_id, dispatch_id): a foreign key check ignores RLS,
    so an attempt written under one agency cannot name another agency's dispatch row."""
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    theirs = await _dispatch(app_pool, other)
    async with app_pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with conn.transaction():
                await _ctx(conn, ids["tenant"])
                await conn.execute("INSERT INTO crm.dispatch_attempt (dispatch_id, job_attempt) VALUES ($1, 1)", theirs)
    assert await _count(app_pool, other["tenant"], "dispatch_attempt") == 0


async def test_the_gate_takes_send_uncertain(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    dispatch = await _dispatch(app_pool, ids)
    async with app_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await _ctx(conn, ids["tenant"])
            await conn.execute(
                "UPDATE crm.dispatch_queue SET status = 'held', gate_result = 'send_uncertain', decided_at = now() "
                "WHERE dispatch_id = $1",
                dispatch,
            )
        finally:
            # No send_uncertain row may outlive the test: test_crm_migrations re-applies crm-015, whose gate check
            # predates the code.
            await tx.rollback()


async def test_the_trip_erase_takes_the_attempts_before_the_dispatch_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    dispatch = await _dispatch(app_pool, ids)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("INSERT INTO crm.dispatch_attempt (dispatch_id, job_attempt) VALUES ($1, 1)", dispatch)
    dry = await _erase(app_pool, ids["tenant"], "trip", ids["trip"], dry_run=True)
    assert dry["counts"]["dispatch_attempt"] == 1 and dry["counts"]["dispatch_queue"] == 1
    done = await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert done["counts"]["dispatch_attempt"] == 1
    assert await _count(app_pool, ids["tenant"], "dispatch_attempt") == 0


async def test_the_agency_erase_takes_the_attempts(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    dispatch = await _dispatch(app_pool, ids)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("INSERT INTO crm.dispatch_attempt (dispatch_id, job_attempt) VALUES ($1, 1)", dispatch)
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    done = await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert done["counts"]["dispatch_attempt"] == 1
    assert await _count(app_pool, ids["tenant"], "dispatch_attempt") == 0
