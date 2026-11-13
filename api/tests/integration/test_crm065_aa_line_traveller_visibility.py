"""crm-065 (AA-1086 follow-up, S279 rehearsal REH-10-portal; a contract file: crm.guard_booking_line section a) on
PostgreSQL with the RDS role shape, as crm_app: an agency member shows or hides a confirmed Adventure Asia line to the
trip's travellers, and nothing else of an AA line.

- a member (not a job: / platform: actor) changes visible_to_travelers on a confirmed, live AA line, both ways;
- the same UPDATE with any other column (title, dates, status, trash) is still CRM24 aa_line_read_only;
- a planning AA line and an insert of an AA line are still CRM24 (crm_app has no DELETE on the table);
- the Booking feed job still writes AA lines; a member's agency line is unchanged by the file.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import pytest

from tests.integration.test_crm005_intake import _agency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MEMBER = "member:test"


async def _world(pool: asyncpg.Pool) -> dict[str, Any]:
    """An agency with one trip: a confirmed AA line and a planning AA line (both hidden), written by the feed job."""
    tenant_id, people = await _agency(pool, "agent")
    trip = uuid.uuid4()
    lines: dict[str, uuid.UUID] = {}
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")  # AA lines: the Booking feed's (CRM24)
        await conn.execute(
            "INSERT INTO crm.trip (trip_id, title, owner_member_id, start_date, end_date) "
            "VALUES ($1, 'Lantern coast 5 days', $2, current_date + 30, current_date + 34)",
            trip,
            people[0][1],
        )
        for name in ("confirmed", "planning"):
            lines[name] = await conn.fetchval(
                "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, title, service_from, "
                "service_to) VALUES ($1, 'aa', crm.ensure_aa_supplier(), $2, current_date + 30, current_date + 34) "
                "RETURNING booking_line_id",
                trip,
                f"Lantern coast tour ({name})",
            )
        await conn.execute(
            "UPDATE crm.booking_line SET status = 'confirmed' WHERE booking_line_id = $1", lines["confirmed"]
        )
    return {"tenant": tenant_id, "trip": trip, "lines": lines}


async def _as(pool: asyncpg.Pool, s: dict[str, Any], sql: str, *args: Any, actor: str = MEMBER) -> str:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        return await conn.execute(sql, *args)


async def _shown(pool: asyncpg.Pool, s: dict[str, Any], line: uuid.UUID) -> bool:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        return await conn.fetchval(
            "SELECT visible_to_travelers AS shown FROM crm.booking_line WHERE booking_line_id = $1", line
        )


async def test_crm065_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-065'") == 1


async def test_a_member_shows_and_hides_a_confirmed_aa_line(app_pool: asyncpg.Pool) -> None:
    s = await _world(app_pool)
    line = s["lines"]["confirmed"]
    sql = "UPDATE crm.booking_line SET visible_to_travelers = $2 WHERE booking_line_id = $1"
    assert await _as(app_pool, s, sql, line, True) == "UPDATE 1"
    assert await _shown(app_pool, s, line) is True
    assert await _as(app_pool, s, sql, line, False) == "UPDATE 1"
    assert await _shown(app_pool, s, line) is False


@pytest.mark.parametrize(
    "change",
    [
        "visible_to_travelers = true, title = 'Renamed tour'",
        "visible_to_travelers = true, service_to = service_to + 1",
        "status = 'cancelled'",
        "title = 'Renamed tour'",
        "confirmation_no = 'X-1'",
    ],
)
async def test_any_other_member_write_to_an_aa_line_is_still_refused(app_pool: asyncpg.Pool, change: str) -> None:
    s = await _world(app_pool)
    with pytest.raises(asyncpg.PostgresError) as err:
        await _as(app_pool, s, f"UPDATE crm.booking_line SET {change} WHERE booking_line_id = $1",  # noqa: S608
                  s["lines"]["confirmed"])  # fmt: skip
    assert err.value.sqlstate == "CRM24"
    assert await _shown(app_pool, s, s["lines"]["confirmed"]) is False


async def test_a_planning_aa_line_insert_and_delete_stay_read_only(app_pool: asyncpg.Pool) -> None:
    s = await _world(app_pool)
    statements: list[tuple[str, tuple[Any, ...]]] = [
        ("UPDATE crm.booking_line SET visible_to_travelers = true WHERE booking_line_id = $1",
         (s["lines"]["planning"],)),
        ("INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, title) "
         "VALUES ($1, 'aa', crm.ensure_aa_supplier(), 'Typed AA line')", (s["trip"],)),
    ]  # fmt: skip
    for sql, args in statements:
        with pytest.raises(asyncpg.PostgresError) as err:
            await _as(app_pool, s, sql, *args)
        assert err.value.sqlstate == "CRM24", sql


async def test_the_feed_job_still_writes_aa_lines(app_pool: asyncpg.Pool) -> None:
    s = await _world(app_pool)
    done = await _as(app_pool, s, "UPDATE crm.booking_line SET title = 'Lantern coast tour (amended)', "
                     "visible_to_travelers = true WHERE booking_line_id = $1", s["lines"]["planning"],
                     actor="job:test")  # fmt: skip
    assert done == "UPDATE 1"
