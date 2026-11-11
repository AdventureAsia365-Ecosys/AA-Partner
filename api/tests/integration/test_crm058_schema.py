"""crm-058 (AA-1085, a contract file) on PostgreSQL with the RDS role shape: the legal-hold guards read the holds of
the row's own agency.

A foreign-key action runs its query, and the BEFORE triggers it fires, as the table owner without row security, so a
guard that leans on RLS for the agency reads every agency's holds inside a cascade. After crm-058:

- every hold guard trigger function reads holds through ``crm.hold_row_of`` / ``hold_trip_of`` / ``hold_traveler_of``
  with ``OLD.tenant_id`` (or ``crm.legal_hold`` with ``tenant_id =``), on the replayed catalog (the unit lint
  ``tests/unit/test_aa1085_hold_guard_tenant.py`` checks the migration texts the same way);
- two agencies: a hold in agency A never refuses a cascade in agency B (the trip erase through the flight, rooming,
  visa and voucher guards, the agency erase, a partner delete through the commission guard), and a cascade onto a
  held row in agency A is still refused (CRM10) until the hold is released;
- ``crm.partner_commission`` carries the hold guard crm-056 left off;
- the explicit tenant is no oracle: under RLS, asking about another agency's holds answers false.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import OWNER
from tests.integration.test_crm010_schema import _count, _ctx, _erase, _hold, _release
from tests.integration.test_crm029_schema import _flight
from tests.integration.test_crm054_schema import _application, _run, _sealed, _setup
from tests.integration.test_crm056_schema import _accepted_proposal, _as_app, _partner, _rows, _set_partner, _trip

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CRM058 = Path(__file__).resolve().parents[2] / "migrations" / "058_crm_hold_guard_tenant.sql"

HOLD_GUARDS = {
    "guard_held_change",
    "guard_message",
    "guard_held_flight",
    "guard_ops_release",
    "guard_supplier_manifest_export",
    "guard_held_travel_doc",
}
GUARDS_SQL = r"""
SELECT p.proname AS name, p.prosrc AS src
  FROM pg_proc p
 WHERE p.pronamespace = 'crm'::regnamespace AND p.prorettype = 'trigger'::regtype
   AND p.prosrc ~ 'crm\.legal_hold\M|crm\.hold_(row|trip|traveler)(_of)?\s*\('
"""
RLS_HOLD_CALL = re.compile(r"crm\.hold_(?:row|trip|traveler)\s*\(")
HOLD_READ = re.compile(r"FROM\s+crm\.legal_hold\b")


async def reapply_crm058(admin: asyncpg.Connection) -> None:
    """crm-058 again (re-runnable): an earlier test of the session may have re-run crm-015, whose crm.guard_message
    reads holds through RLS (a re-run DB Migrate never does on Dev)."""
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(CRM058.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")


@pytest_asyncio.fixture(autouse=True)
async def _crm058_guards(admin: asyncpg.Connection) -> AsyncIterator[None]:
    await reapply_crm058(admin)
    yield


async def _offboard_and_erase(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant)
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    await _erase(pool, tenant, "tenant", tenant)


async def _travel_rows(pool: asyncpg.Pool, ids: dict[str, Any]) -> None:
    """A flight, a room, a visa application and a voucher on the agency's trip (every cascade-reached hold guard)."""
    t = ids["tenant"]
    await _flight(pool, ids)
    await _run(pool, t, "INSERT INTO crm.room_assignment (trip_id, traveler_id, room_no, room_type) "
               "VALUES ($1, $2, 1, 'twin')", ids["trip"], ids["lead"])  # fmt: skip
    await _application(pool, ids)
    await _run(pool, t, "INSERT INTO crm.booking_line_voucher (booking_line_id, trip_id, pnr_enc) VALUES ($1, $2, $3)",
               ids["line"], ids["trip"], _sealed())  # fmt: skip


async def _cancelled_commission(admin: asyncpg.Connection, tenant: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """A partner whose only commission row (cancelled: the partner was taken off the booked trip) a partner delete
    removes by cascade. Returns (partner, trip)."""
    partner = await _partner(admin, tenant, rate="10")
    trip = await _trip(admin, tenant)
    await _accepted_proposal(admin, tenant, trip, "1800.00")
    await _set_partner(admin, tenant, trip, partner)
    await _set_partner(admin, tenant, trip, None)
    assert [r["status"] for r in await _rows(admin, tenant, trip)] == ["cancelled"]
    return partner, trip


DELETE_PARTNER = "DELETE FROM crm.sales_partner WHERE sales_partner_id = $1 RETURNING sales_partner_id"


async def test_crm058_is_registered_and_a_contract_file(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-058'") == 1
    assert "-- migrate:" not in CRM058.read_text(encoding="utf-8")  # contract: no directive line, MIG-4 order


async def test_every_hold_guard_reads_the_holds_of_its_own_agency(admin: asyncpg.Connection) -> None:
    guards = {r["name"]: r["src"] for r in await admin.fetch(GUARDS_SQL)}
    assert set(guards) >= HOLD_GUARDS
    for name, src in guards.items():
        code = re.sub(r"--[^\n]*", "", src)
        assert not RLS_HOLD_CALL.search(code), f"{name} checks holds through RLS only"
        for m in HOLD_READ.finditer(code):
            assert "tenant_id =" in re.sub(r"\s+", " ", code[m.end() : m.end() + 200]), f"{name}: {m.group(0)}"
    for fn in ("crm.hold_trip_of(uuid,uuid)", "crm.hold_traveler_of(uuid,uuid)", "crm.hold_row_of(uuid,uuid,uuid)"):
        row = await admin.fetchrow(
            "SELECT pg_get_functiondef($1::regprocedure) AS def, p.proacl::text AS acl, p.prosecdef AS definer "
            "FROM pg_proc p WHERE p.oid = $1::regprocedure",
            fn,
        )
        assert not row["definer"], fn  # invoker rights: RLS still applies outside a cascade
        acl = row["acl"].strip("{}").split(",")
        assert "crm_app=X/crm_owner" in acl and not any(e.startswith("=") for e in acl), fn  # no PUBLIC
        assert "SET search_path TO 'pg_catalog', 'pg_temp'" in row["def"], fn
        if fn != "crm.hold_row_of(uuid,uuid,uuid)":
            assert row["def"].count("tenant_id = p_tenant") == 2, fn  # crm.legal_hold and crm.trip_traveler


async def test_every_hold_guarded_table_has_a_tenant(admin: asyncpg.Connection) -> None:
    """The guards read OLD.tenant_id: every table one fires on has a NOT NULL tenant_id (a NULL would match no hold)."""
    rows = await admin.fetch(
        GUARDS_SQL.replace("SELECT p.proname AS name, p.prosrc AS src", "SELECT DISTINCT c.relname AS name, "
                           "a.attnotnull AS ok")
        .replace("FROM pg_proc p", "FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid JOIN pg_class c "
                 "ON c.oid = t.tgrelid LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id'")
    )  # fmt: skip
    assert len(rows) > 30
    assert [r["name"] for r in rows if not r["ok"]] == []
    assert "partner_commission" in {r["name"] for r in rows}


async def test_a_hold_in_one_agency_never_refuses_a_cascade_in_another(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool
) -> None:
    held = await _setup(app_pool)
    await _hold(app_pool, held["tenant"], "tenant", None, "member:m")
    await _hold(app_pool, held["tenant"], "trip", held["trip"], "member:m")
    await _hold(app_pool, held["tenant"], "traveler", held["lead"], "member:m")

    ids = await _setup(app_pool)
    t = ids["tenant"]
    await _travel_rows(app_pool, ids)
    # The trip erase: the trip row goes, its flight / room / visa / voucher rows by cascade (owner, no RLS).
    await _erase(app_pool, t, "trip", ids["trip"])
    for table in ("traveler_flight", "room_assignment", "visa_application", "booking_line_voucher"):
        assert await _count(app_pool, t, table) == 0, table
    # A partner delete: its commission row goes by cascade through the commission's hold guard.
    partner, _ = await _cancelled_commission(admin, t)
    assert await _as_app(app_pool, t, DELETE_PARTNER, partner) == partner
    assert await _count(app_pool, t, "partner_commission") == 0
    # The agency erase.
    other = await _setup(app_pool)
    await _travel_rows(app_pool, other)
    await _offboard_and_erase(app_pool, other["tenant"])
    assert await _count(app_pool, other["tenant"], "trip") == 0
    # The held agency is untouched and still held.
    assert await _count(app_pool, held["tenant"], "trip") == 1


async def test_a_cascade_onto_a_held_row_is_still_refused_in_its_own_agency(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool
) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    partner, trip = await _cancelled_commission(admin, t)
    hold = await _hold(app_pool, t, "trip", trip, "member:m")
    # crm.sales_partner has no hold guard of its own: the cascade onto the held trip's commission is refused there.
    with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
        await _as_app(app_pool, t, DELETE_PARTNER, partner)
    assert err.value.sqlstate == "CRM10"
    assert "partner_commission" in str(err.value)
    assert await _count(app_pool, t, "partner_commission") == 1
    # A direct delete (the owner role: crm_app has no DELETE on the table) is refused too.
    with pytest.raises(asyncpg.PostgresError) as err:
        async with admin.transaction():
            await admin.execute(f"SET LOCAL ROLE {OWNER}")
            await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(t))
            await admin.execute("DELETE FROM crm.partner_commission WHERE trip_id = $1", trip)
    assert err.value.sqlstate == "CRM10"
    # Released: the same partner delete goes through.
    await _release(app_pool, t, hold)
    assert await _as_app(app_pool, t, DELETE_PARTNER, partner) == partner
    assert await _count(app_pool, t, "partner_commission") == 0


async def _cascade_from(admin: asyncpg.Connection, tenant: uuid.UUID, parent: str, sql: str, *args: Any) -> None:
    """Delete a parent row as the owner with the parent's own hold guard switched off for this transaction only, so
    the refusal can come only from a child's guard inside the foreign-key cascade (trigger depth 2, no row security).
    The ALTER rolls back with the refused transaction."""
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await admin.execute(f"ALTER TABLE crm.{parent} DISABLE TRIGGER {parent}_hold_guard")
        await admin.execute(sql, *args)


async def test_the_flight_and_travel_doc_guards_check_cascades_too(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool
) -> None:
    """crm-029 / crm-054 let every cascade pass (trigger depth > 1). Now the hold of the row's own agency decides at any
    depth: a cascade onto a held trip's flight, room, visa or voucher is refused."""
    ids = await _setup(app_pool)
    t = ids["tenant"]
    await _travel_rows(app_pool, ids)
    await _hold(app_pool, t, "trip", ids["trip"], "member:m")
    party = "DELETE FROM crm.trip_traveler WHERE trip_id = $1 AND traveler_id = $2"
    with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
        await _cascade_from(admin, t, "trip_traveler", party, ids["trip"], ids["lead"])
    assert err.value.sqlstate == "CRM10"
    assert re.search(r"of a held (traveler_flight|room_assignment|visa_application) row", str(err.value))
    with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
        await _cascade_from(admin, t, "booking_line", "DELETE FROM crm.booking_line WHERE booking_line_id = $1",
                            ids["line"])  # fmt: skip
    assert err.value.sqlstate == "CRM10"
    assert "booking_line_voucher" in str(err.value)
    for table in ("trip_traveler", "traveler_flight", "room_assignment", "visa_application", "booking_line_voucher"):
        assert await _count(app_pool, t, table) >= 1, table
    # The guards are back on (the ALTER rolled back with the refusal).
    assert await admin.fetchval(
        "SELECT count(*) FROM pg_trigger WHERE tgname IN ('trip_traveler_hold_guard', 'booking_line_hold_guard') "
        "AND tgenabled = 'O'"
    ) == 2


async def test_the_explicit_tenant_is_no_oracle_under_rls(app_pool: asyncpg.Pool) -> None:
    held = await _setup(app_pool)
    await _hold(app_pool, held["tenant"], "tenant", None, "member:m")
    ids = await _setup(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        assert await conn.fetchval("SELECT crm.hold_row_of($1, $2, NULL)", held["tenant"], held["trip"]) is False
        assert await conn.fetchval("SELECT crm.hold_trip_of($1, NULL)", held["tenant"]) is False
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, held["tenant"])
        assert await conn.fetchval("SELECT crm.hold_row_of($1, $2, NULL)", held["tenant"], held["trip"]) is True
        assert await conn.fetchval("SELECT crm.hold_trip_of(NULL, $1)", held["trip"]) is False  # no tenant, no hold
