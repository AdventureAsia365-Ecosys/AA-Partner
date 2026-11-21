"""crm-083 (AA-1147): the agency's own lines follow the trip (no supplier needed, a deadline, a bundled flag, files and
tasks about one line, priced included rows on the invoice, the accept's line writer).

Rows are written as crm_owner inside one agency (FORCE row security binds the owner too), the way crm-080's test does.
The accept path itself (crm.agency_lines_from_proposal through the client link) is tested end to end in
tests/integration/test_aa1147_agency_extras_api.py.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import APP_ROLE, OWNER

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "083_crm_agency_extras.sql"


async def _as_owner(admin: asyncpg.Connection, tenant: uuid.UUID, sql: str, *args: Any) -> Any:
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await admin.execute("SELECT set_config('app.actor', 'seed:test', true)")
        return await admin.fetchval(sql, *args)


async def _agency(admin: asyncpg.Connection) -> dict[str, Any]:
    tenant = uuid.uuid4()
    await _as_owner(admin, tenant, "INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant)
    await _as_owner(admin, tenant, "INSERT INTO crm.tenant_settings (tenant_id) VALUES ($1)", tenant)
    aa = await _as_owner(
        admin, tenant,
        "INSERT INTO crm.agency_supplier (kind, name, category) VALUES ('aa', 'AA', 'dmc') "
        "RETURNING agency_supplier_id",
    )  # fmt: skip
    trips = [
        await _as_owner(
            admin,
            tenant,
            "INSERT INTO crm.trip (title, stage, waiting_on) VALUES ($1, 'booked', 'none') RETURNING trip_id",
            title,
        )  # fmt: skip
        for title in ("Lantern coast 6 days", "Pine ridge 4 days")
    ]
    return {"tenant": tenant, "aa": aa, "trip": trips[0], "other_trip": trips[1]}


async def _agency_line(admin: asyncpg.Connection, a: dict[str, Any], trip: uuid.UUID, **cols: Any) -> uuid.UUID:
    names = ["trip_id", "supplier_kind", "title", *cols]
    values = [trip, "agency", "Return flight", *cols.values()]
    marks = ", ".join(f"${i}" for i in range(1, len(values) + 1))
    return await _as_owner(
        admin, a["tenant"],
        f"INSERT INTO crm.booking_line ({', '.join(names)}) VALUES ({marks}) RETURNING booking_line_id",  # noqa: S608
        *values,
    )  # fmt: skip


async def test_an_agency_line_needs_no_supplier_an_aa_line_does(admin: asyncpg.Connection) -> None:
    a = await _agency(admin)
    line = await _agency_line(admin, a, a["trip"], category="flight")
    assert line is not None
    with pytest.raises(asyncpg.CheckViolationError, match="booking_line_supplier_chk"):
        await _as_owner(
            admin, a["tenant"],
            "INSERT INTO crm.booking_line (trip_id, supplier_kind, title) VALUES ($1, 'aa', 'Valley tour')", a["trip"],
        )  # fmt: skip


@pytest.mark.parametrize(
    "category,bundled,ok",
    [("flight", True, True), ("fee", True, True), ("visa", True, False), ("insurance", True, False),
     ("visa", False, True)],
    ids=["flight", "fee", "visa", "insurance", "visa-separate"],
)  # fmt: skip
async def test_only_a_flight_or_fee_is_bundled(
    admin: asyncpg.Connection, category: str, bundled: bool, ok: bool
) -> None:
    a = await _agency(admin)
    if ok:
        assert await _agency_line(admin, a, a["trip"], category=category, bundled=bundled) is not None
        return
    with pytest.raises(asyncpg.CheckViolationError, match="booking_line_extras_chk"):
        await _agency_line(admin, a, a["trip"], category=category, bundled=bundled)


async def test_an_aa_line_takes_no_deadline_or_item_link(admin: asyncpg.Connection) -> None:
    a = await _agency(admin)
    with pytest.raises(asyncpg.CheckViolationError, match="booking_line_extras_chk"):
        await _as_owner(
            admin, a["tenant"],
            "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, title, due_at) "
            "VALUES ($1, 'aa', $2, 'Valley tour', now())",
            a["trip"], a["aa"],
        )  # fmt: skip


async def test_one_item_writes_one_line(admin: asyncpg.Connection) -> None:
    a = await _agency(admin)
    option = uuid.uuid4()
    await _agency_line(admin, a, a["trip"], category="visa", option_id=option)
    with pytest.raises(asyncpg.UniqueViolationError, match="booking_line_option_uq"):
        await _agency_line(admin, a, a["trip"], category="visa", option_id=option)


async def test_a_task_about_a_line_stays_in_its_trip(admin: asyncpg.Connection) -> None:
    a = await _agency(admin)
    line = await _agency_line(admin, a, a["trip"], category="flight")
    task = await _as_owner(
        admin, a["tenant"],
        "INSERT INTO crm.task (trip_id, kind, booking_line_id) VALUES ($1, 'agency_flight_due', $2) RETURNING task_id",
        a["trip"], line,
    )  # fmt: skip
    assert task is not None
    with pytest.raises(asyncpg.ForeignKeyViolationError, match="task_booking_line_fk"):
        await _as_owner(
            admin, a["tenant"],
            "INSERT INTO crm.task (trip_id, kind, booking_line_id) VALUES ($1, 'agency_visa_due', $2)",
            a["other_trip"], line,
        )  # fmt: skip
    # A line link needs the trip (the key is not checked when trip_id is NULL): task_line_trip_chk / document's.
    for table, name in (("task", "task_line_trip_chk"), ("document", "document_line_trip_chk")):
        expr = await _check(admin, table, name)
        no_trip = expr.replace("booking_line_id", "gen_random_uuid()").replace("trip_id", "NULL::uuid")
        assert await admin.fetchval(f"SELECT ({no_trip}) IS NOT FALSE") is False, table


async def test_purging_a_line_keeps_its_task_and_drops_the_link(admin: asyncpg.Connection) -> None:
    a = await _agency(admin)
    line = await _agency_line(admin, a, a["trip"], category="insurance")
    task = await _as_owner(
        admin, a["tenant"],
        "INSERT INTO crm.task (trip_id, kind, booking_line_id) VALUES ($1, 'agency_insurance_due', $2) "
        "RETURNING task_id",
        a["trip"], line,
    )  # fmt: skip
    await _as_owner(admin, a["tenant"], "DELETE FROM crm.booking_line WHERE booking_line_id = $1", line)
    assert await _as_owner(admin, a["tenant"], "SELECT booking_line_id FROM crm.task WHERE task_id = $1", task) is None
    assert await _as_owner(admin, a["tenant"], "SELECT trip_id FROM crm.task WHERE task_id = $1", task) == a["trip"]


async def _check(admin: asyncpg.Connection, table: str, name: str) -> str:
    return await admin.fetchval(
        "SELECT pg_get_expr(conbin, conrelid) FROM pg_constraint WHERE conrelid = ('crm.' || $1)::regclass "
        "AND conname = $2",
        table, name,
    )  # fmt: skip


@pytest.mark.parametrize(
    "source,amount,ok",
    [("agency_line", "50.00", True), ("agency_line", None, True), ("aa_line", "50.00", False),
     ("manual", "1.00", False), ("aa_line", None, True), ("agency_line", "-1.00", False)],
    ids=["agency-priced", "agency-bundled", "aa-priced", "manual-priced", "aa-title", "negative"],
)  # fmt: skip
async def test_only_an_included_agency_row_carries_a_price(admin: asyncpg.Connection, source: str,
                                                           amount: str | None, ok: bool) -> None:  # fmt: skip
    expr = await _check(admin, "invoice_line", "invoice_line_amount_chk")
    for column, cast in (("quantity", "NULL::numeric"), ("unit_amount", "NULL::numeric"), ("amount", "$2::numeric"),
                         ("kind", "'included'::text"), ("source", "$1::text")):  # fmt: skip
        expr = expr.replace(column, cast)
    got = await admin.fetchval(f"SELECT ({expr}) IS NOT FALSE", source, amount)
    assert got is ok


async def test_the_option_bundled_check(admin: asyncpg.Connection) -> None:
    expr = await _check(admin, "option", "option_bundled_chk")
    for source, category, ok in (("agency", "flight", True), ("agency", "insurance", False), ("aa", None, False)):
        sql = expr.replace("bundled", "true").replace("source", "$1::text").replace("category", "$2::text")
        assert await admin.fetchval(f"SELECT ({sql}) IS NOT FALSE", source, category) is ok


async def test_visa_letter_is_a_document_kind(admin: asyncpg.Connection) -> None:
    # Another test of the session may have re-run crm-006 / crm-015 / crm-055 (their document CHECK lacks the newer
    # kinds, see test_crm054_schema.reapply_crm055): crm-083 again (re-runnable) puts it back, as Migration Apply has.
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert "visa_letter" in await _check(admin, "document", "document_kind_chk")


async def test_the_line_writer_refuses_a_version_not_accepted(admin: asyncpg.Connection) -> None:
    a = await _agency(admin)
    assert await admin.fetchval(
        "SELECT has_function_privilege($1, 'crm.agency_lines_from_proposal(uuid,jsonb)', 'EXECUTE')", APP_ROLE
    )
    assert not await admin.fetchval(
        "SELECT has_function_privilege('public', 'crm.agency_lines_from_proposal(uuid,jsonb)', 'EXECUTE')"
    )
    with pytest.raises(asyncpg.PostgresError, match="proposal_not_accepted"):
        await _as_owner(admin, a["tenant"], "SELECT crm.agency_lines_from_proposal($1, '[]'::jsonb)", uuid.uuid4())
    with pytest.raises(asyncpg.PostgresError, match="lines_invalid"):
        await _as_owner(admin, a["tenant"], "SELECT crm.agency_lines_from_proposal($1, '{}'::jsonb)", uuid.uuid4())


async def test_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-083'") == 1


async def test_the_file_is_re_runnable(admin: asyncpg.Connection) -> None:
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    count = await admin.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'crm.booking_line'::regclass "
        "AND conname IN ('booking_line_supplier_chk', 'booking_line_extras_chk')"
    )
    assert count == 2
