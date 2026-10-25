"""crm-017 (AA-947, card CRM-19) on PostgreSQL 15 with the RDS role shape: AA lines, the outage queue, the Booking
feed tables and the catalog cache.

- ``crm.aa_line_request`` is the only member path to an AA line: a pending placeholder whose title comes from the
  catalog cache; a closed trip / bad parameter (CRM33), a tour that is not served (CRM34), a sell amount from the
  assistant (42501) are refused. A member still cannot write the line itself (crm-014 CRM24).
- ``crm.booking_line_aa_money``: a job actor only (members, the assistant and AA platform staff get 42501), AA lines
  only (CRM26), frozen at once with the quote lock; a non-USD base crosses through ``crm.fx_resolve``; the same quote
  twice writes nothing.
- The AA columns stay NULL on agency lines; one live request per (line, kind); ``inbox_events`` is append-only and
  unique per event; the readers have nothing on the new tables.
- Erasure: the trip erase takes the AA requests, the agency erase the feed tables too (no CRM16); a trip hold guards
  the requests.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase, _hold

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "017_crm_aa_lines.sql"
NEW_TENANT_TABLES = ("aa_request", "booking_feed_state", "inbox_events")
REQUEST = (
    "SELECT crm.aa_line_request($1, 'T-9', 'tp:9', 'trip_plan', NULL, DATE '2026-12-01', NULL, 2, 0, 0, NULL, "
    "'USD', $2)"
)
MONEY = "SELECT crm.booking_line_aa_money($1, $2, $3, $4, $5, DATE '2026-10-25')"


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = "job:crm_booking_sync",
               role: str = "") -> Any:  # fmt: skip
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        return await conn.fetchval(sql, *args)


async def _tour(pool: asyncpg.Pool, source_id: str = "T-9", state: str = "served") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state) "
            "VALUES ($1, 'Ha Long overnight', 'VN', 2, $2) "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = EXCLUDED.served_state",
            source_id,
            state,
        )


async def _aa_line(pool: asyncpg.Pool, ids: dict[str, Any], sell: Any = None) -> uuid.UUID:
    await _tour(pool)
    return await _run(pool, ids["tenant"], REQUEST, ids["trip"], sell, actor=f"member:{ids['member']}", role="owner")


async def test_crm017_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    # Runs first in this file: the crm-012 .. crm-016 tests re-apply their own erase_subject text before it.
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-017'") == 1
    before = await admin.fetchval("SELECT count(*) FROM crm.status_transition")
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM crm.status_transition") == before


@pytest.mark.parametrize("table", (*NEW_TENANT_TABLES, "aa_catalog_tour", "aa_catalog_state"))
async def test_the_readers_have_nothing_on_the_new_tables(admin: asyncpg.Connection, table: str) -> None:
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege)


async def test_the_member_path_creates_a_pending_line_from_the_catalog(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    line = await _aa_line(app_pool, ids, 900)
    row = await _run(
        app_pool, ids["tenant"],
        "SELECT row(supplier_kind, status, aa_sub_status, title, service_to::text, aa_pax->>'adult', created_by) "
        "FROM crm.booking_line WHERE booking_line_id = $1", line,
    )  # fmt: skip
    assert tuple(row) == (
        "aa",
        "planning",
        "pending",
        "Ha Long overnight",
        "2026-12-02",
        "2",
        f"member:{ids['member']}",
    )
    req = await _run(app_pool, ids["tenant"], "SELECT row(kind, status, sell_amount::text) FROM crm.aa_request")
    assert tuple(req) == ("quote", "pending", "900.00")
    assert await _count(app_pool, ids["tenant"], "booking_feed_state") == 1
    # The member still cannot write the line itself.
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(app_pool, ids["tenant"],
                   "UPDATE crm.booking_line SET aa_sub_status = 'held' WHERE booking_line_id = $1",
                   line, actor=f"member:{ids['member']}", role="owner")  # fmt: skip
    assert err.value.sqlstate == "CRM24"
    # One live request per (line, kind); a quote request names what Booking prices.
    ask = ("INSERT INTO crm.aa_request (trip_id, booking_line_id, kind) "
           "SELECT trip_id, booking_line_id, $2 FROM crm.booking_line WHERE booking_line_id = $1")  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError, match="aa_request_params_chk"):
        await _run(app_pool, ids["tenant"], ask, line, "quote")
    await _run(app_pool, ids["tenant"], ask, line, "hold")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(app_pool, ids["tenant"], ask, line, "hold")


async def test_the_member_path_refusals(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    member = {"actor": f"member:{ids['member']}", "role": "owner"}
    await _tour(app_pool, "T-gone", "retired")
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(app_pool, ids["tenant"], REQUEST.replace("'T-9'", "'T-gone'"), ids["trip"], None, **member)
    assert err.value.sqlstate == "CRM34"
    await _tour(app_pool)
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(app_pool, ids["tenant"], REQUEST.replace("2, 0, 0", "0, 0, 0"), ids["trip"], None, **member)
    assert err.value.sqlstate == "CRM33" and "params" in str(err.value)
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="margin_forbidden"):
        await _run(app_pool, ids["tenant"], REQUEST, ids["trip"], 5, actor=member["actor"], role="assistant")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await _run(app_pool, ids["tenant"], REQUEST, ids["trip"], None, actor="platform:staff", role="")
    await _run(app_pool, ids["tenant"],
               "UPDATE crm.trip SET stage = 'lost', lost_reason_code = 'other' WHERE trip_id = $1",
               ids["trip"], actor="seed:test")  # fmt: skip
    with pytest.raises(asyncpg.PostgresError, match="trip_closed") as err:
        await _run(app_pool, ids["tenant"], REQUEST, ids["trip"], None, **member)
    assert err.value.sqlstate == "CRM33"


async def test_aa_money_is_the_jobs_and_frozen_at_the_quote_lock(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    line = await _aa_line(app_pool, ids)
    t = ids["tenant"]
    for actor, role in ((f"member:{ids['member']}", "owner"), (f"member:{ids['member']}", "assistant"),
                        ("platform:staff", "")):  # fmt: skip
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await _run(app_pool, t, MONEY, line, "cost", 1000, "VND", 25000, actor=actor, role=role)
    first = await _run(app_pool, t, MONEY, line, "cost", 2500000, "VND", 25000)
    assert await _run(app_pool, t, MONEY, line, "cost", 2500000, "VND", 25000) == first  # the same quote again
    row = await _run(
        app_pool, t,
        "SELECT row(amount_base::text, base_currency, fx_rate::text, fx_source, fx_basis, stale_days) "
        "FROM crm.booking_line_money WHERE booking_line_money_id = $1", first,
    )  # fmt: skip
    assert tuple(row) == ("100.00", "USD", "0.0000400000", "aa_quote_lock", "quote_lock", 0)
    # A non-USD base: the quote lock into USD, then USD into the base at the rate of that day (an override here).
    await _run(app_pool, t, "INSERT INTO crm.tenant_settings (base_currency) VALUES ('EUR') "
               "ON CONFLICT (tenant_id) DO UPDATE SET base_currency = 'EUR'", actor="seed:test")  # fmt: skip
    await _run(app_pool, t, "INSERT INTO crm.fx_rate_override (rate_date, base, quote, rate, set_by) "
               "VALUES (DATE '2026-10-25', 'USD', 'EUR', 0.9, $1)", ids["member"], actor="seed:test")  # fmt: skip
    second = await _run(app_pool, t, MONEY, line, "cost", 2500000, "VND", 25000)
    assert second != first
    eur = await _run(app_pool, t,
                     "SELECT amount_base::text FROM crm.booking_line_money WHERE booking_line_money_id = $1",
                     second)  # fmt: skip
    assert eur == "90.00"
    # Not on an agency line.
    agency_line = await _run(
        app_pool, t,
        "INSERT INTO crm.booking_line (trip_id, agency_supplier_id, title) "
        "SELECT $1, crm.ensure_aa_supplier(), 'x' WHERE false RETURNING booking_line_id", ids["trip"],
    )  # fmt: skip
    assert agency_line is None
    supplier = await _run(app_pool, t, "INSERT INTO crm.agency_supplier (kind, name, category) "
                          "VALUES ('own', 'Hanoi Pearl', 'accommodation') RETURNING agency_supplier_id",
                          actor="seed:test")  # fmt: skip
    own = await _run(app_pool, t, "INSERT INTO crm.booking_line (trip_id, agency_supplier_id, title) "
                     "VALUES ($1, $2, 'Hotel') RETURNING booking_line_id", ids["trip"], supplier,
                     actor="seed:test")  # fmt: skip
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(app_pool, t, MONEY, own, "cost", 10, "USD", 1)
    assert err.value.sqlstate == "CRM26"
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, t, "UPDATE crm.booking_line SET bk_quote_id = 'q1' WHERE booking_line_id = $1", own,
                   actor="seed:test")  # fmt: skip


async def test_the_inbox_is_append_only_and_unique(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    insert = ("INSERT INTO crm.inbox_events (source, contract, idempotency_key, event_type, outcome, reason_code) "
              "VALUES ('booking', 'booking.b2b.v1', 'e1', 'hold.expired', 'applied', 'hold_expired')")  # fmt: skip
    await _run(app_pool, t, insert)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(app_pool, t, insert)
    for sql in ("UPDATE crm.inbox_events SET outcome = 'ignored'", "DELETE FROM crm.inbox_events"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await _run(app_pool, t, sql)


async def test_the_trip_and_agency_erase_take_the_new_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    await _aa_line(app_pool, ids)
    await _run(app_pool, ids["tenant"], "INSERT INTO crm.inbox_events (source, contract, idempotency_key, outcome, "
               "reason_code) VALUES ('booking', 'booking.b2b.v1', 'e9', 'ignored', 'unknown_line')")  # fmt: skip
    done = await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert done["counts"]["aa_request"] == 1 and done["counts"]["booking_line"] == 1
    assert await _count(app_pool, ids["tenant"], "aa_request") == 0
    await _run(app_pool, ids["tenant"], "UPDATE crm.tenant SET status = 'offboarding'", actor="seed:test")
    done = await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert done["counts"]["inbox_events"] == 1 and done["counts"]["booking_feed_state"] == 1
    for table in NEW_TENANT_TABLES:
        assert await _count(app_pool, ids["tenant"], table) == 0, table


async def test_a_trip_hold_guards_its_requests(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    await _aa_line(app_pool, ids)
    await _hold(app_pool, ids["tenant"], "trip", ids["trip"], "member:m")
    with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
        await _run(app_pool, ids["tenant"], "UPDATE crm.aa_request SET deleted_at = now(), deleted_by = 'm', "
                   "purge_after = now()")  # fmt: skip
    assert err.value.sqlstate == "CRM10"
    # A status move is not a change the hold guards (the queue keeps working).
    await _run(app_pool, ids["tenant"], "UPDATE crm.aa_request SET status = 'cancelled', finished_at = now()")
