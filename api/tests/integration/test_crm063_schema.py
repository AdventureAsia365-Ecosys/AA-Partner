"""crm-063 (AA-1093 part 1, an expand file) on PostgreSQL with the RDS role shape: ``crm.tour_retired_flag``.

- One flag set per transition: the same (tour, raised key, item) again is refused, and one item has at most one open
  flag; a later transition after a clear raises a new row.
- The values: the four kinds and the item each one names, the source id and key shapes, open -> cleared with its time.
- Hold rule: a held trip's flag is neither deleted nor trashed directly (CRM10); clearing stays allowed.
- Tenant isolation: another agency sees nothing; the trip erase and the agency erase take the flags (cascade).
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

TOUR = "0f6b1c52-7d3e-4a8e-9c11-5e2f7a9b3d40"
INSERT = (
    "INSERT INTO crm.tour_retired_flag (trip_id, source_id, kind, option_id, booking_line_id, raised_key) "
    "VALUES ($1, $2, $3, $4, $5, $6) RETURNING tour_retired_flag_id"
)
CLEAR = (
    "UPDATE crm.tour_retired_flag SET state = 'cleared', cleared_at = now(), cleared_key = 'reinstated:2' "
    "WHERE tour_retired_flag_id = $1"
)


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = "job:test") -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        return await conn.fetchval(sql, *args)


async def _flag(pool: asyncpg.Pool, ids: dict[str, Any], kind: str = "held_line", item: uuid.UUID | None = None,
                key: str = "retired:1") -> Any:  # fmt: skip
    item = item or uuid.uuid4()
    option, line = (item, None) if kind == "open_option" else (None, item)
    return await _run(pool, ids["tenant"], INSERT, ids["trip"], TOUR, kind, option, line, key,
                      actor="job:crm_tour_retired_fanout")  # fmt: skip


async def test_registered_and_no_reader_grant(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-063'") == 1
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, 'crm.tour_retired_flag', $2)", reader,
                                            privilege)  # fmt: skip


async def test_one_flag_set_per_transition_and_one_open_flag_per_item(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    line = uuid.uuid4()
    first = await _flag(app_pool, ids, item=line)
    with pytest.raises(asyncpg.UniqueViolationError):  # the same transition again
        await _flag(app_pool, ids, item=line)
    with pytest.raises(asyncpg.UniqueViolationError):  # another transition while the item is still open
        await _flag(app_pool, ids, item=line, key="retired:3")
    await _run(app_pool, t, CLEAR, first)
    with pytest.raises(asyncpg.UniqueViolationError):  # the cleared transition never raises again
        await _flag(app_pool, ids, item=line)
    await _flag(app_pool, ids, item=line, key="retired:3")  # a later retirement: a new flag
    for kind in ("open_option", "quoted_line", "confirmed_line"):
        await _flag(app_pool, ids, kind)
    assert await _count(app_pool, t, "tour_retired_flag") == 5


async def test_the_values(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    trip = ids["trip"]
    bad_inserts = (
        (trip, TOUR, "booked_line", None, uuid.uuid4(), "retired:1"),  # unknown kind
        (trip, TOUR, "open_option", None, uuid.uuid4(), "retired:1"),  # an option flag names an option
        (trip, TOUR, "held_line", uuid.uuid4(), None, "retired:1"),  # a line flag names a line
        (trip, TOUR, "held_line", uuid.uuid4(), uuid.uuid4(), "retired:1"),  # never both
        (trip, "", "held_line", None, uuid.uuid4(), "retired:1"),  # empty source id
        (trip, "x" * 129, "held_line", None, uuid.uuid4(), "retired:1"),
        (trip, TOUR, "held_line", None, uuid.uuid4(), "retired 1"),  # key shape
    )
    for args in bad_inserts:
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, INSERT, *args)
    flag = await _flag(app_pool, ids)
    for bad in (
        "UPDATE crm.tour_retired_flag SET state = 'cleared'",  # no time
        "UPDATE crm.tour_retired_flag SET state = 'gone', cleared_at = now()",
        "UPDATE crm.tour_retired_flag SET cleared_key = 'reinstated:2'",  # a key without the clear
        "UPDATE crm.tour_retired_flag SET deleted_at = now()",  # trash columns together
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, bad + " WHERE tour_retired_flag_id = $1", flag)
    await _run(app_pool, t, CLEAR, flag)
    assert await _run(app_pool, t, "SELECT created_by FROM crm.tour_retired_flag WHERE tour_retired_flag_id = $1",
                      flag) == "job:crm_tour_retired_fanout"  # fmt: skip


async def test_a_held_trip_keeps_its_flags(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    flag = await _flag(app_pool, ids)
    await _run(app_pool, t, "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) VALUES ('trip', $1, "
               "'dispute')", ids["trip"], actor="member:test")  # fmt: skip
    trash = "UPDATE crm.tour_retired_flag SET deleted_at = now(), deleted_by = 'm', purge_after = now()"
    for sql in ("DELETE FROM crm.tour_retired_flag", trash):
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, t, sql)
        assert err.value.sqlstate == "CRM10", sql
    await _run(app_pool, t, CLEAR, flag)


async def test_another_agency_sees_nothing_and_the_erases_take_the_flags(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    t = ids["tenant"]
    await _flag(app_pool, ids)
    assert await _count(app_pool, other["tenant"], "tour_retired_flag") == 0
    assert await _count(app_pool, t, "tour_retired_flag") == 1
    with pytest.raises(asyncpg.PostgresError):  # another agency's trip: the tenant FK / policy refuse it
        await _run(app_pool, other["tenant"], INSERT, ids["trip"], TOUR, "held_line", None, uuid.uuid4(),
                   "retired:1")  # fmt: skip
    await _erase(app_pool, t, "trip", ids["trip"])
    assert await _count(app_pool, t, "tour_retired_flag") == 0
    await _flag(app_pool, other)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, other["tenant"])
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    await _erase(app_pool, other["tenant"], "tenant", other["tenant"])
    assert await _count(app_pool, other["tenant"], "tour_retired_flag") == 0
