"""crm-054 (AA-1070, an expand file) and crm-055 (its contract file) on PostgreSQL with the RDS role shape: the travel
ops tables.

- ``crm.aa_departure_state``: written by a job / platform / seed actor only (CRM53), one row per departure, the status
  values and their times; cascades from the agency's feed row.
- ``crm.booking_line_voucher``: sealed values only, one live row per line.
- ``crm.room_assignment``: room number / type / bed values, one live row per (trip, traveller), a traveller of the
  trip only.
- ``crm.visa_template``: country / type / fee pair / document list / https URL checks, one live per (country, type).
- ``crm.visa_application`` + ``crm.visa_checklist_item``: status moves (crm-055 transitions), dates, a visa number
  only on an approved application, the checklist done triple.
- crm-055: ``tenant_settings.passport_valid_months`` (default 6, 0..24), the new document kinds and uploader.
- Hold rule: a held trip's rows are neither deleted nor trashed directly (CRM10); the erases take every row through
  the cascades (``crm.erase_subject`` unchanged).
- Tenant isolation: another agency sees nothing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CRM055 = Path(__file__).resolve().parents[2] / "migrations" / "055_crm_travel_ops_settings.sql"


async def purge_system_documents(admin: asyncpg.Connection) -> None:
    """Remove the documents of crm-055's new kinds / uploader (as the erase does: crm_owner with app.erasing). crm-006
    and crm-015 (copied, so immutable) re-add the document kind / uploader CHECKs without them when they run again,
    and test_crm_migrations / test_crm015_schema re-run them on this database: a row of the new values left by a test
    would fail that re-run (a re-run DB Migrate never does on Dev). Every test that writes one calls this at its end."""
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("SELECT set_config('app.erasing', 'on', true)")
        await admin.execute("ALTER TABLE crm.document DISABLE ROW LEVEL SECURITY")
        await admin.execute(
            "DELETE FROM crm.document WHERE uploaded_by_kind = 'system' OR kind IN ('confirmation_pdf', 'proposal_pdf')"
        )
        await admin.execute("ALTER TABLE crm.document ENABLE ROW LEVEL SECURITY")


async def reapply_crm055(admin: asyncpg.Connection) -> None:
    """crm-055 again (re-runnable): an earlier test of the session may have re-run crm-006 / crm-015, whose document
    CHECKs do not have crm-055's values."""
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(CRM055.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")


@pytest_asyncio.fixture(autouse=True)
async def _crm055_values(admin: asyncpg.Connection) -> AsyncIterator[None]:
    await reapply_crm055(admin)
    yield
    await purge_system_documents(admin)


NEW_TABLES = (
    "aa_departure_state",
    "booking_line_voucher",
    "room_assignment",
    "visa_template",
    "visa_application",
    "visa_checklist_item",
)


def _sealed() -> bytes:
    return os.urandom(40)  # shaped like a sealed value; never opened here


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = "member:test") -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        await conn.execute("SELECT set_config('app.member_role', 'agent', true)")
        return await conn.fetchval(sql, *args)


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    ids = await _agency(pool)
    t = ids["tenant"]
    await _run(pool, t, "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING")
    await _run(pool, t, "INSERT INTO crm.booking_feed_state DEFAULT VALUES", actor="job:crm_booking_sync")
    supplier = await _run(
        pool, t,
        "INSERT INTO crm.agency_supplier (kind, name, category) VALUES ('own', 'Riverbend Coaches', 'transport') "
        "RETURNING agency_supplier_id",
    )  # fmt: skip
    ids["line"] = await _run(
        pool, t,
        "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, title) "
        "VALUES ($1, 'agency', $2, 'Airport transfer') RETURNING booking_line_id",
        ids["trip"], supplier,
    )  # fmt: skip
    return ids


async def _state(pool: asyncpg.Pool, tenant: uuid.UUID, departure: str, actor: str = "job:crm_booking_sync") -> Any:
    return await _run(
        pool, tenant,
        "INSERT INTO crm.aa_departure_state (departure_id, departure_status, guaranteed_at, last_event_id, "
        "last_event_at) VALUES ($1, 'guaranteed', now(), 'evt-1', now()) RETURNING aa_departure_state_id",
        departure, actor=actor,
    )  # fmt: skip


async def _application(pool: asyncpg.Pool, ids: dict[str, Any], country: str = "KH") -> Any:
    return await _run(
        pool, ids["tenant"],
        "INSERT INTO crm.visa_application (trip_id, traveler_id, country, visa_type, deadline) "
        "VALUES ($1, $2, $3, 'evisa', current_date - 1) RETURNING visa_application_id",
        ids["trip"], ids["lead"], country,
    )  # fmt: skip


@pytest.mark.parametrize("table", NEW_TABLES)
async def test_registered_and_no_reader_grant(admin: asyncpg.Connection, table: str) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-054'") == 1
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-055'") == 1
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege)


async def test_the_departure_state_is_the_feed_s(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    row = await _state(app_pool, t, "DEP-2026-11-20-A")
    with pytest.raises(asyncpg.PostgresError) as err:
        await _state(app_pool, t, "DEP-2026-11-21-B", actor="member:test")
    assert err.value.sqlstate == "CRM53"
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(app_pool, t, "UPDATE crm.aa_departure_state SET min_to_operate = 4 WHERE aa_departure_state_id = $1",
                   row)  # fmt: skip
    assert err.value.sqlstate == "CRM53"
    with pytest.raises(asyncpg.UniqueViolationError):  # one row per departure
        await _state(app_pool, t, "DEP-2026-11-20-A")
    for bad in ("UPDATE crm.aa_departure_state SET departure_status = 'cancelled'",  # needs its time
                "UPDATE crm.aa_departure_state SET departure_status = 'running'",
                "UPDATE crm.aa_departure_state SET min_to_operate = 0"):  # fmt: skip
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, bad, actor="job:crm_booking_sync")
    assert await _run(app_pool, t, "SELECT count(*) FROM crm.aa_departure_state") == 1  # members read it


async def test_voucher_values_are_sealed_and_one_per_line(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    sql = ("INSERT INTO crm.booking_line_voucher (booking_line_id, trip_id, pickup_enc, service_time, pnr_enc) "
           "VALUES ($1, $2, $3, '07:30', $4) RETURNING booking_line_voucher_id")  # fmt: skip
    await _run(app_pool, t, sql, ids["line"], ids["trip"], _sealed(), _sealed())
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(app_pool, t, sql, ids["line"], ids["trip"], _sealed(), _sealed())
    with pytest.raises(asyncpg.CheckViolationError):  # a plain value never lands in a sealed column
        await _run(app_pool, t, "UPDATE crm.booking_line_voucher SET note_enc = 'short'::bytea")


async def test_room_values_and_party(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    sql = (
        "INSERT INTO crm.room_assignment (trip_id, traveler_id, room_no, room_type, bed_pref)"
        " VALUES ($1, $2, $3, $4, $5)"
    )
    await _run(app_pool, t, sql, ids["trip"], ids["lead"], 1, "double", "double_bed")
    await _run(app_pool, t, sql, ids["trip"], ids["companion"], 1, "double", None)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(app_pool, t, sql, ids["trip"], ids["lead"], 2, "single", None)
    for room_no, room_type, bed in ((0, "single", None), (51, "single", None), (2, "suite", None), (2, "twin", "sofa")):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, "UPDATE crm.room_assignment SET room_no = $1, room_type = $2, bed_pref = $3 "
                       "WHERE traveler_id = $4", room_no, room_type, bed, ids["lead"])  # fmt: skip
    other = await _agency(app_pool)  # a traveller who is not on this trip
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _run(app_pool, t, sql, ids["trip"], other["lead"], 3, "single", None)


async def test_visa_template_values(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    sql = ("INSERT INTO crm.visa_template (country, visa_type, fee, fee_currency, documents, official_url) "
           "VALUES ($1, $2, $3, $4, $5::jsonb, $6) RETURNING visa_template_id")  # fmt: skip
    await _run(app_pool, t, sql, "VN", "evisa", 25, "USD", '["Passport copy", "Photo"]', "https://visa.example.test")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(app_pool, t, sql, "VN", "evisa", None, None, "[]", None)
    for bad in (("vn", "evisa", None, None, "[]", None), ("LA", "magic", None, None, "[]", None),
                ("LA", "evisa", 10, None, "[]", None), ("LA", "evisa", None, None, '{"a": 1}', None),
                ("LA", "evisa", None, None, "[]", "http://visa.example.test")):  # fmt: skip
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, sql, *bad)


async def test_visa_status_moves_and_values(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    app = await _application(app_pool, ids)
    with pytest.raises(asyncpg.UniqueViolationError):  # one live application per traveller and country
        await _application(app_pool, ids)
    with pytest.raises(asyncpg.PostgresError):  # to_do -> approved is not a move (and needs the days)
        await _run(app_pool, t, "UPDATE crm.visa_application SET status = 'approved', submitted_on = current_date, "
                   "decided_on = current_date WHERE visa_application_id = $1", app)  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):  # submitted needs its day
        await _run(app_pool, t, "UPDATE crm.visa_application SET status = 'submitted' WHERE visa_application_id = $1",
                   app)  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):  # a visa number only on an approved application
        await _run(app_pool, t, "UPDATE crm.visa_application SET visa_no_enc = $2 WHERE visa_application_id = $1",
                   app, _sealed())  # fmt: skip
    await _run(app_pool, t, "UPDATE crm.visa_application SET status = 'collecting' WHERE visa_application_id = $1", app)
    await _run(app_pool, t, "UPDATE crm.visa_application SET status = 'submitted', submitted_on = current_date "
               "WHERE visa_application_id = $1", app)  # fmt: skip
    await _run(app_pool, t, "UPDATE crm.visa_application SET status = 'approved', decided_on = current_date, "
               "visa_no_enc = $2, valid_until_enc = $2 WHERE visa_application_id = $1", app, _sealed())  # fmt: skip
    with pytest.raises(asyncpg.PostgresError):  # approved -> to_do is not a move
        await _run(app_pool, t, "UPDATE crm.visa_application SET status = 'to_do' WHERE visa_application_id = $1", app)
    item_sql = ("INSERT INTO crm.visa_checklist_item (visa_application_id, label, sort, done, done_at, done_by_kind) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING visa_checklist_item_id")  # fmt: skip
    await _run(app_pool, t, item_sql, app, "Passport copy", 0, False, None, None)
    for bad in ((" ", 0, False, None, None), ("Photo", 1, True, None, None), ("Photo", 1, False, None, "member"),
                ("Photo", 101, False, None, None)):  # fmt: skip
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, item_sql, app, *bad)


async def test_crm055_settings_and_document_kinds(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    assert await _run(app_pool, t, "SELECT passport_valid_months FROM crm.tenant_settings") == 6
    for bad in (-1, 25):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, "UPDATE crm.tenant_settings SET passport_valid_months = $1", bad)
    doc = uuid.uuid4()
    await _run(
        app_pool, t,
        "INSERT INTO crm.document (document_id, trip_id, kind, s3_key, sha256, size_bytes, content_type, filename_enc, "
        "visible_to_travelers, write_only, uploaded_by_kind) VALUES ($1, $2, 'confirmation_pdf', $3, $4, 10, "
        "'application/pdf', $5, false, false, 'system')",
        doc, ids["trip"], f"tenants/{t}/documents/{doc}", os.urandom(32), _sealed(),
    )  # fmt: skip
    assert await _run(app_pool, t, "SELECT kind FROM crm.document WHERE document_id = $1", doc) == "confirmation_pdf"


async def test_a_held_trip_keeps_its_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    await _run(app_pool, t, "INSERT INTO crm.room_assignment (trip_id, traveler_id, room_no, room_type) "
               "VALUES ($1, $2, 1, 'single')", ids["trip"], ids["lead"])  # fmt: skip
    app = await _application(app_pool, ids)
    await _run(app_pool, t, "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) VALUES ('trip', $1, "
               "'dispute')", ids["trip"])  # fmt: skip
    trash = "UPDATE crm.visa_application SET deleted_at = now(), deleted_by = 'm', purge_after = now()"
    for sql in ("DELETE FROM crm.room_assignment", "DELETE FROM crm.visa_application", trash):
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, t, sql)
        assert err.value.sqlstate == "CRM10", sql
    # Other edits stay allowed (the hold is about deletion).
    await _run(app_pool, t, "UPDATE crm.visa_application SET deadline = current_date WHERE visa_application_id = $1",
               app)  # fmt: skip


async def test_another_agency_sees_nothing_and_the_erases_take_everything(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    other = await _setup(app_pool)
    t = ids["tenant"]
    await _state(app_pool, t, "DEP-ERASE-1")
    await _run(app_pool, t, "INSERT INTO crm.booking_line_voucher (booking_line_id, trip_id, pnr_enc) VALUES ($1, $2, "
               "$3)", ids["line"], ids["trip"], _sealed())  # fmt: skip
    await _run(app_pool, t, "INSERT INTO crm.room_assignment (trip_id, traveler_id, room_no, room_type) "
               "VALUES ($1, $2, 1, 'single')", ids["trip"], ids["lead"])  # fmt: skip
    await _run(app_pool, t, "INSERT INTO crm.visa_template (country, visa_type) VALUES ('TH', 'visa_exempt')")
    app = await _application(app_pool, ids)
    await _run(app_pool, t, "INSERT INTO crm.visa_checklist_item (visa_application_id, label) VALUES ($1, 'Photo')",
               app)  # fmt: skip
    for table in NEW_TABLES:
        assert await _count(app_pool, other["tenant"], table) == 0, table
        assert await _count(app_pool, t, table) == 1, table
    # The traveller erase takes their room and visa (cascade from the party / traveller rows) but not the trip's line.
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, t)
        await conn.execute("DELETE FROM crm.trip_traveler WHERE traveler_id = $1", ids["lead"])
    assert await _count(app_pool, t, "room_assignment") == 0
    assert await _count(app_pool, t, "visa_application") == 0
    assert await _count(app_pool, t, "visa_checklist_item") == 0
    await _erase(app_pool, t, "trip", ids["trip"])
    assert await _count(app_pool, t, "booking_line_voucher") == 0
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, t)
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    await _erase(app_pool, t, "tenant", t)
    for table in NEW_TABLES:
        assert await _count(app_pool, t, table) == 0, table
