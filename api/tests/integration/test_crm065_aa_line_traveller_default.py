"""crm-065 (AA-1086 follow-up, S279 rehearsal REH-10-portal; a contract file: data only) on PostgreSQL with the RDS role
shape: the confirmed Adventure Asia lines written before the code that shows them to travellers by default get the
default once.

- a confirmed, live AA line that is hidden -> shown; a planning AA line, a cancelled AA line and an agency line keep
  their flag;
- every agency is backfilled (the file enters each one through ``app.tenant_id``: FORCE RLS hides the rows from the
  owner otherwise);
- the backfill runs once: replaying the registered file never shows a line again (a later choice to hide one stays).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER
from tests.integration.test_crm005_intake import _agency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FILE = Path(__file__).resolve().parents[2] / "migrations" / "065_crm_aa_line_traveller_default.sql"


async def _world(pool: asyncpg.Pool) -> dict[str, Any]:
    """An agency with one trip: AA lines confirmed / planning / cancelled (all hidden) and one agency line."""
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
        for name, status in (("confirmed", "confirmed"), ("planning", "planning"), ("cancelled", "cancelled")):
            lines[name] = await conn.fetchval(
                "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, title, service_from, "
                "service_to) VALUES ($1, 'aa', crm.ensure_aa_supplier(), $2, current_date + 30, current_date + 34) "
                "RETURNING booking_line_id",
                trip,
                f"Lantern coast tour ({name})",
            )
            if status != "planning":
                await conn.execute(
                    "UPDATE crm.booking_line SET status = $2 WHERE booking_line_id = $1", lines[name], status
                )
        supplier = await conn.fetchval(
            "INSERT INTO crm.agency_supplier (name, category) VALUES ('Harbour Kayaks', 'activity') "
            "RETURNING agency_supplier_id"
        )
        lines["agency"] = await conn.fetchval(
            "INSERT INTO crm.booking_line (trip_id, agency_supplier_id, title, service_from) "
            "VALUES ($1, $2, 'Kayak morning', current_date + 31) RETURNING booking_line_id",
            trip,
            supplier,
        )
    return {"tenant": tenant_id, "trip": trip, "lines": lines}


async def _flags(pool: asyncpg.Pool, s: dict[str, Any]) -> dict[str, bool]:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        rows = await conn.fetch(
            "SELECT booking_line_id, visible_to_travelers AS shown FROM crm.booking_line WHERE trip_id = $1", s["trip"]
        )
    by_id = {r["booking_line_id"]: r["shown"] for r in rows}
    return {name: by_id[line] for name, line in s["lines"].items()}


async def _run_file(admin: asyncpg.Connection) -> None:
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(FILE.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")


async def test_crm065_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-065'") == 1


async def test_confirmed_aa_lines_of_every_agency_are_shown_once(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    first, second = await _world(app_pool), await _world(app_pool)
    hidden = {"confirmed": False, "planning": False, "cancelled": False, "agency": False}
    assert await _flags(app_pool, first) == hidden and await _flags(app_pool, second) == hidden
    # As on Dev: the file has not run yet (the session's setup applied it before these rows existed).
    await admin.execute("DELETE FROM shared.schema_versions WHERE version = 'crm-065'")
    await _run_file(admin)
    shown = {"confirmed": True, "planning": False, "cancelled": False, "agency": False}
    assert await _flags(app_pool, first) == shown and await _flags(app_pool, second) == shown
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-065'") == 1
    # A replay of the registered file shows nothing again (the Booking feed job hides the line here, standing in for a
    # later choice).
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(first["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        await conn.execute(
            "UPDATE crm.booking_line SET visible_to_travelers = false WHERE booking_line_id = $1",
            first["lines"]["confirmed"],
        )
    await _run_file(admin)
    assert (await _flags(app_pool, first))["confirmed"] is False
