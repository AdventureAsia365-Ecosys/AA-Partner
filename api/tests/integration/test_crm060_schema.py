"""crm-060 (AA-1070 item crm-13, an expand file) on PostgreSQL with the RDS role shape: ``crm.aa_cancel_case``.

- One case per (trip, departure): a second insert is refused (the feed relies on it: a replayed event opens nothing).
- The values: departure and event id shapes, the resolution set (NULL open, requoted, cancelled) with its time and
  actor, a re-quote names its version, a rebook trip is never the trip itself.
- Hold rule: a held trip's case is neither deleted nor trashed directly (CRM10); other edits stay allowed.
- Tenant isolation: another agency sees nothing; the trip erase and the agency erase take the cases (cascade).
- Registered, no reader grant.

Invented names and places only.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

INSERT = (
    "INSERT INTO crm.aa_cancel_case (trip_id, departure_id, opened_event_id, opened_at) VALUES ($1, $2, $3, now()) "
    "RETURNING aa_cancel_case_id"
)


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = "member:test") -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        return await conn.fetchval(sql, *args)


async def _case(pool: asyncpg.Pool, ids: dict[str, Any], departure: str = "DEP-2027-03-04-K") -> Any:
    return await _run(pool, ids["tenant"], INSERT, ids["trip"], departure, "evt-cx-1", actor="job:crm_booking_sync")


async def test_registered_and_no_reader_grant(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-060'") == 1
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, 'crm.aa_cancel_case', $2)", reader,
                                            privilege)  # fmt: skip


async def test_one_case_per_trip_and_departure_and_its_values(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    case = await _case(app_pool, ids)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _case(app_pool, ids)
    await _case(app_pool, ids, "DEP-2027-03-11-K")  # another departure of the same trip: its own case
    for bad in (
        # a re-quote without its version
        "UPDATE crm.aa_cancel_case SET resolution = 'requoted', resolved_at = now(), resolved_by = 'member:x'",
        "UPDATE crm.aa_cancel_case SET resolution = 'cancelled'",  # no time / actor
        "UPDATE crm.aa_cancel_case SET resolution = 'dismissed', resolved_at = now(), resolved_by = 'member:x'",
        "UPDATE crm.aa_cancel_case SET rebook_trip_id = trip_id, proposal_id = gen_random_uuid()",
        "UPDATE crm.aa_cancel_case SET departure_id = 'bad id'",
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, bad + " WHERE aa_cancel_case_id = $1", case)
    await _run(app_pool, t, "UPDATE crm.aa_cancel_case SET resolution = 'requoted', resolved_at = now(), "
               "resolved_by = 'member:x', proposal_id = gen_random_uuid(), rebook_trip_id = gen_random_uuid() "
               "WHERE aa_cancel_case_id = $1", case)  # fmt: skip
    await _run(app_pool, t, "UPDATE crm.aa_cancel_case SET resolution = 'cancelled' WHERE aa_cancel_case_id = $1", case)
    with pytest.raises(asyncpg.CheckViolationError):  # a bad event id
        await _run(app_pool, t, INSERT, ids["trip"], "DEP-2027-04-01-K", "evt with space")


async def test_a_held_trip_keeps_its_cases(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    case = await _case(app_pool, ids)
    await _run(app_pool, t, "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) VALUES ('trip', $1, "
               "'dispute')", ids["trip"])  # fmt: skip
    trash = "UPDATE crm.aa_cancel_case SET deleted_at = now(), deleted_by = 'm', purge_after = now()"
    for sql in ("DELETE FROM crm.aa_cancel_case", trash):
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, t, sql)
        assert err.value.sqlstate == "CRM10", sql
    await _run(app_pool, t, "UPDATE crm.aa_cancel_case SET resolution = 'cancelled', resolved_at = now(), "
               "resolved_by = 'member:x' WHERE aa_cancel_case_id = $1", case)  # fmt: skip


async def test_another_agency_sees_nothing_and_the_erases_take_the_cases(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    t = ids["tenant"]
    await _case(app_pool, ids)
    assert await _count(app_pool, other["tenant"], "aa_cancel_case") == 0
    assert await _count(app_pool, t, "aa_cancel_case") == 1
    with pytest.raises(asyncpg.PostgresError):  # another agency's trip: the tenant FK / policy refuse it
        await _run(app_pool, other["tenant"], INSERT, ids["trip"], "DEP-X", "evt-x", actor="job:crm_booking_sync")
    await _erase(app_pool, t, "trip", ids["trip"])
    assert await _count(app_pool, t, "aa_cancel_case") == 0
    await _case(app_pool, other)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, other["tenant"])
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    await _erase(app_pool, other["tenant"], "tenant", other["tenant"])
    assert await _count(app_pool, other["tenant"], "aa_cancel_case") == 0
