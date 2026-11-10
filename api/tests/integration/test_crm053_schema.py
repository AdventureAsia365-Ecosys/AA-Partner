"""crm-053 (AA-1069, a contract file) on PostgreSQL with the RDS role shape: supplier booking requests.

- ``crm.booking_line`` status ``requested``: planning -> requested -> confirmed | planning | cancelled (the
  crm.status_transition rows; a confirmed line is never requested again); a requested line has its ``requested_at``;
  AA lines are never requested (``booking_line_request_chk``); ``request_ref`` is an id-shaped reference.
- ``crm.booking_line_confirm`` confirms a requested line too, and still refuses a confirmed one (CRM26).
- ``crm.conversation.agency_supplier_id``: a supplier thread has a trip, no client, channel email; one live thread per
  trip and supplier.
- The file runs again as a no-op.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER
from tests.integration.test_crm010_schema import _agency, _ctx

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "053_crm_supplier_requests.sql"


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = "member:test",
               role: str | None = "agent") -> Any:  # fmt: skip
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        if role is not None:
            await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        return await conn.fetchval(sql, *args)


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    ids = await _agency(pool)
    t = ids["tenant"]
    await _run(pool, t, "INSERT INTO crm.tenant_settings (booking_validation_level) VALUES ('off') "
               "ON CONFLICT (tenant_id) DO UPDATE SET booking_validation_level = 'off'")  # fmt: skip
    ids["aa"] = await _run(pool, t, "SELECT crm.ensure_aa_supplier()", actor="seed:test", role=None)
    ids["own"] = await _run(
        pool, t,
        "INSERT INTO crm.agency_supplier (kind, name, category) VALUES ('own', 'Bamboo Transfers', 'transport') "
        "RETURNING agency_supplier_id",
    )  # fmt: skip
    return ids


async def _line(pool: asyncpg.Pool, ids: dict[str, Any]) -> uuid.UUID:
    return await _run(
        pool, ids["tenant"],
        "INSERT INTO crm.booking_line (trip_id, agency_supplier_id, title) VALUES ($1, $2, 'Airport pick-up') "
        "RETURNING booking_line_id",
        ids["trip"], ids["own"],
    )  # fmt: skip


REQUEST_SQL = (
    "UPDATE crm.booking_line SET status = 'requested', requested_at = now(), request_ref = $2 "
    "WHERE booking_line_id = $1 RETURNING status"
)


async def test_crm053_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'crm-053'") == 1
    before = await admin.fetchval("SELECT count(*) FROM crm.status_transition")
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM crm.status_transition") == before
    assert await admin.fetchval(
        "SELECT count(*) FROM crm.status_transition WHERE table_name = 'booking_line' "
        "AND 'requested' IN (from_status, to_status)"
    ) == 4  # fmt: skip


async def test_a_requested_line_is_confirmed_by_the_function(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    line = await _line(app_pool, ids)
    with pytest.raises(asyncpg.CheckViolationError):  # a requested line says when it was requested
        await _run(app_pool, t, "UPDATE crm.booking_line SET status = 'requested' WHERE booking_line_id = $1", line)
    with pytest.raises(asyncpg.CheckViolationError):  # the reference is id-shaped
        await _run(app_pool, t, REQUEST_SQL, line, "not a ref!")
    ref = str(uuid.uuid4())
    assert await _run(app_pool, t, REQUEST_SQL, line, ref) == "requested"
    result = await _run(app_pool, t, "SELECT crm.booking_line_confirm($1)", line)
    assert result is not None
    row = await _run(app_pool, t, "SELECT row(status, requested_at IS NOT NULL, request_ref)::text "
                     "FROM crm.booking_line WHERE booking_line_id = $1", line)  # fmt: skip
    assert row == f"(confirmed,t,{ref})"  # the request stays as history
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(app_pool, t, "SELECT crm.booking_line_confirm($1)", line)
    assert err.value.sqlstate == "CRM26"
    with pytest.raises(asyncpg.PostgresError):  # a confirmed line is never requested again
        await _run(app_pool, t, REQUEST_SQL, line, ref)


async def test_a_requested_line_goes_back_to_planning_or_is_cancelled(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    back, cancelled = await _line(app_pool, ids), await _line(app_pool, ids)
    for line in (back, cancelled):
        await _run(app_pool, t, REQUEST_SQL, line, "req-1")
    assert await _run(
        app_pool, t,
        "UPDATE crm.booking_line SET status = 'planning', requested_at = NULL, request_ref = NULL "
        "WHERE booking_line_id = $1 RETURNING status", back,
    ) == "planning"  # fmt: skip
    assert await _run(
        app_pool, t, "UPDATE crm.booking_line SET status = 'cancelled' WHERE booking_line_id = $1 RETURNING status",
        cancelled,
    ) == "cancelled"  # fmt: skip


async def test_an_aa_line_is_never_requested(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    line = await _run(
        app_pool, t,
        "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, title) "
        "VALUES ($1, 'aa', $2, 'AA tour') RETURNING booking_line_id",
        ids["trip"], ids["aa"], actor="job:crm_booking_sync", role=None,
    )  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, t, REQUEST_SQL, line, "req-2", actor="job:crm_booking_sync", role=None)


async def test_one_supplier_thread_per_trip_and_supplier(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    insert = (
        "INSERT INTO crm.conversation (trip_id, traveler_id, channel, agency_supplier_id) "
        "VALUES ($1, $2, $3, $4) RETURNING conversation_id"
    )
    thread = await _run(app_pool, t, insert, ids["trip"], None, "email", ids["own"])
    assert thread is not None
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(app_pool, t, insert, ids["trip"], None, "email", ids["own"])
    for trip, client, channel in ((None, None, "email"), (ids["trip"], ids["lead"], "email"),
                                  (ids["trip"], None, "sms")):  # fmt: skip
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, insert, trip, client, channel, uuid.uuid4())
    # A client conversation of the same trip is unaffected.
    assert await _run(app_pool, t, insert, ids["trip"], ids["lead"], "email", None) is not None
