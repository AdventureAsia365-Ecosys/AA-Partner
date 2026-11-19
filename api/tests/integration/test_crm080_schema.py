"""crm-080 (AA-1144): the agency's own items are a closed list; the older rows are re-typed and trashed, never deleted.

The data step runs on rows the narrowed CHECKs no longer allow, so the test drops those two CHECKs, seeds one agency
the way Dev looked before crm-080, runs the file again as crm_owner (it is re-runnable) and reads the result. The
file puts the CHECKs back, so the database every other test sees is the migrated one.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "080_crm_agency_lines_closed_list.sql"


async def _as_owner(admin: asyncpg.Connection, tenant: uuid.UUID, sql: str, *args: Any) -> Any:
    """One statement as crm_owner inside the agency (FORCE RLS still applies to the owner)."""
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await admin.execute("SELECT set_config('app.actor', 'seed:test', true)")
        return await admin.fetchval(sql, *args)


async def _run_file(admin: asyncpg.Connection) -> list[str]:
    notices: list[str] = []
    admin.add_log_listener(lambda _c, msg: notices.append(str(msg.message)))
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    return notices


async def _supplier(admin: asyncpg.Connection, tenant: uuid.UUID, name: str, category: str) -> uuid.UUID:
    return await _as_owner(
        admin, tenant,
        "INSERT INTO crm.agency_supplier (kind, name, category) VALUES ('own', $1, $2) RETURNING agency_supplier_id",
        name, category,
    )  # fmt: skip


async def _row(admin: asyncpg.Connection, tenant: uuid.UUID, supplier: uuid.UUID) -> dict[str, Any]:
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        row = await admin.fetchrow(
            "SELECT category, deleted_at IS NOT NULL AS trashed, deleted_by, purge_after > now() AS restorable "
            "FROM crm.agency_supplier WHERE agency_supplier_id = $1",
            supplier,
        )
    return dict(row)


async def test_older_rows_are_retyped_and_the_unused_ones_trashed(admin: asyncpg.Connection) -> None:
    tenant = uuid.uuid4()
    await _as_owner(admin, tenant, "INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant)
    await _as_owner(admin, tenant, "INSERT INTO crm.tenant_settings (tenant_id) VALUES ($1)", tenant)
    await _as_owner(admin, tenant, "INSERT INTO crm.agency_supplier (kind, name, category) VALUES ('aa', 'AA', 'dmc')")
    trip = await _as_owner(
        admin, tenant,
        "INSERT INTO crm.trip (title, stage, waiting_on) VALUES ('Lantern coast 6 days', 'booked', 'none') "
        "RETURNING trip_id",
    )  # fmt: skip
    await admin.execute("ALTER TABLE crm.agency_supplier DROP CONSTRAINT agency_supplier_category_chk")
    await admin.execute("ALTER TABLE crm.agency_service DROP CONSTRAINT agency_service_category_chk")
    try:
        unused = await _supplier(admin, tenant, "Pine Cove Lodge", "accommodation")
        lined = await _supplier(admin, tenant, "Harbour Coaches", "transport")
        listed = await _supplier(admin, tenant, "Reed Valley Guides", "guide")
        flights = await _supplier(admin, tenant, "Gull Air", "flight")
        await _as_owner(
            admin, tenant,
            "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, status, title) "
            "VALUES ($1, 'agency', $2, 'planning', 'Coach to the harbour')",
            trip, lined,
        )  # fmt: skip
        service = await _as_owner(
            admin, tenant,
            "INSERT INTO crm.agency_service (agency_supplier_id, category, name, unit_sell, currency, qty_basis) "
            "VALUES ($1, 'transfer', 'Valley shuttle', 20, 'USD', 'per_booking') RETURNING service_id",
            listed,
        )  # fmt: skip
        notices = await _run_file(admin)
    finally:
        # Never leave the shared database without the CHECKs (the file re-adds them; this only runs on a failure).
        await admin.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agency_supplier_category_chk') "
            "THEN RAISE EXCEPTION 'crm-080 re-run did not finish'; END IF; END $$"
        )
    assert any(n.startswith("crm-080 counts:") for n in notices)
    assert any(n.startswith("crm-080 done:") for n in notices)
    # Nothing names it: re-typed and in the trash, restorable until the lifecycle purge.
    assert await _row(admin, tenant, unused) == {
        "category": "other", "trashed": True, "deleted_by": "migration:crm-080", "restorable": True,
    }  # fmt: skip
    # A booking line or a catalogue service names it: re-typed, kept live so the history reads as before.
    for kept in (lined, listed):
        assert await _row(admin, tenant, kept) == {
            "category": "other", "trashed": False, "deleted_by": None, "restorable": None,
        }  # fmt: skip
    assert (await _row(admin, tenant, flights))["category"] == "flight"
    trashed_service = await _as_owner(
        admin, tenant, "SELECT deleted_by FROM crm.agency_service WHERE service_id = $1", service
    )
    assert trashed_service == "migration:crm-080"
    # The narrowed CHECKs are back: a hotel supplier and a transfer service are refused by the database too.
    with pytest.raises(asyncpg.CheckViolationError):
        await _supplier(admin, tenant, "Marsh Inn", "accommodation")
    with pytest.raises(asyncpg.CheckViolationError):
        await _as_owner(
            admin, tenant,
            "INSERT INTO crm.agency_service (category, name, unit_sell, currency, qty_basis) "
            "VALUES ('transfer', 'Dune shuttle', 20, 'USD', 'per_booking')",
        )  # fmt: skip


async def test_an_item_type_belongs_to_agency_rows_only(admin: asyncpg.Connection) -> None:
    tenant = uuid.uuid4()
    await _as_owner(admin, tenant, "INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant)
    aa = await _as_owner(
        admin, tenant,
        "INSERT INTO crm.agency_supplier (kind, name, category) VALUES ('aa', 'AA', 'dmc') "
        "RETURNING agency_supplier_id",
    )  # fmt: skip
    own = await _supplier(admin, tenant, "Gull Air", "flight")
    trip = await _as_owner(
        admin,
        tenant,
        "INSERT INTO crm.trip (title, stage, waiting_on) VALUES ('Lantern coast', 'booked', 'none') RETURNING trip_id",
    )
    insert = (
        "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, status, title, category) "
        "VALUES ($1, $2, $3, 'planning', 'Item', $4) RETURNING category"
    )
    assert await _as_owner(admin, tenant, insert, trip, "agency", own, "flight") == "flight"
    assert await _as_owner(admin, tenant, insert, trip, "agency", own, None) is None  # an older line
    for kind, supplier, category in (("agency", own, "transfer"), ("agency", own, "other"), ("aa", aa, "flight")):
        with pytest.raises(asyncpg.CheckViolationError):
            await _as_owner(admin, tenant, insert, trip, kind, supplier, category)


async def test_the_extra_night_request_shape(admin: asyncpg.Connection) -> None:
    grants = {
        r["privilege_type"]
        for r in await admin.fetch(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema = 'crm' AND table_name = 'aa_extra_night_request' AND grantee = 'crm_app'"
        )
    }
    assert grants == {"SELECT", "INSERT", "UPDATE"}
    assert await admin.fetchval(
        "SELECT count(*) FROM crm.status_transition WHERE table_name = 'aa_extra_night_request'"
    ) == 1  # fmt: skip
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-080'") == 1
