"""crm-035 (AA-957, card CRM-27) on PostgreSQL with the RDS role shape: commission and the trip cancellation record.

- ``crm.commission_sync``: a confirmed agency line with a positive expected commission gets one expected row (also
  when the assistant confirmed it: R18, the function reads the money as its owner); due = the last service day + the
  supplier's ``commission_due_days`` (default 30); the row is mirrored into ``crm.trip_commitment``; an expected
  commission set to 0 cancels it (``no_commission``) and a new positive one makes a new row; a cancelled line cancels
  it (``line_cancelled``) unless ``keep_on_cancel``; nothing new on a trip with a pending cancellation.
- Margins (§4.6 ``commission.*``): the assistant and an AA platform actor read no row; crm_app cannot insert or delete
  one and writes the receipt only through ``crm.commission_receive``.
- **E** (card): a receipt in another currency uses the receipt-date rate (not the confirmation day's), ``short_amount``
  is judged in the original currency only, ``fx_difference_base`` only when the original amounts match.
- R21: a claimed commission blocks the trip delete (``has_commission``).
- ``crm.trip_cancellation``: one per trip, written only through ``crm.trip_cancellation_open`` (owner / agent; the
  database decides pending / done from the live AA bookings) and ``crm.trip_cancellation_finish`` (done only once no
  AA booking is live and the trip is cancelled); crm_app has SELECT only; a done row is closed; ``crm.aa_request``
  takes kind ``cancel``; ``crm.booking_line_aa_cancel_fee`` writes the fee as a ``cancellation_cost`` head at the
  line's quote FX lock, a job actor only.
- The file runs again as a no-op; RLS isolates every new row.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _blockers, _ctx
from tests.integration.test_crm012_schema import _put_stage
from tests.integration.test_crm014_schema import (
    OVERRIDE_SQL,
    _as,
    _confirm,
    _currency,
    _ledger,
    _line,
    _money,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "035_crm_commission_cancel.sql"
SHAPE_SQL = """
SELECT (SELECT count(*) FROM pg_constraint WHERE connamespace = 'crm'::regnamespace) AS constraints,
       (SELECT count(*) FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid
         WHERE c.relnamespace = 'crm'::regnamespace AND NOT g.tgisinternal) AS triggers,
       (SELECT count(*) FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid
         WHERE c.relnamespace = 'crm'::regnamespace) AS policies,
       (SELECT count(*) FROM crm.status_transition) AS transitions,
       (SELECT relacl::text FROM pg_class WHERE oid = 'crm.commission'::regclass) AS acl
"""
SYNC = "SELECT crm.commission_sync($1)"
RECEIVE = "SELECT crm.commission_receive($1, $2::numeric, $3, $4::date, $5)"
TODAY = "(now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date"
OPEN = "SELECT crm.trip_cancellation_open($1, $2, '{}'::jsonb)"
FINISH = "SELECT crm.trip_cancellation_finish($1)"


async def _sync(pool: asyncpg.Pool, ids: dict[str, Any], role: str = "agent") -> dict[str, int]:
    async with _as(pool, ids["tenant"], role, ids["members"][role]) as conn:
        raw = await conn.fetchval(SYNC, ids["trip"])
    return json.loads(raw)


async def _rows(pool: asyncpg.Pool, ids: dict[str, Any], role: str = "owner") -> list[asyncpg.Record]:
    async with _as(pool, ids["tenant"], role, ids["members"][role]) as conn:
        return await conn.fetch(
            "SELECT commission_id, booking_line_id, status, due_on, close_reason, received_amount_base, short_amount, "
            "fx_difference_base, fx_rate, fx_rate_date, fx_basis FROM crm.commission ORDER BY created_at"
        )


async def _commission_line(
    pool: asyncpg.Pool, ids: dict[str, Any], *, amount: str = "10", currency: str = "USD", role: str = "agent"
) -> Any:
    line = await _line(pool, ids, conf=f"C-{uuid.uuid4().hex[:6]}")
    await _money(pool, ids, line, "cost", "100", currency)
    await _money(pool, ids, line, "sell", "130", currency)
    await _money(pool, ids, line, "commission_expected", amount, currency)
    async with _as(pool, ids["tenant"], "agent", ids["members"]["agent"]) as conn:
        await conn.execute(
            "UPDATE crm.booking_line SET service_from = DATE '2027-03-01', service_to = DATE '2027-03-04' "
            "WHERE booking_line_id = $1",
            line,
        )
    await _confirm(pool, ids, line, role)
    return line


async def test_crm035_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-035'") == 1
    before = dict(await admin.fetchrow(SHAPE_SQL))
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert dict(await admin.fetchrow(SHAPE_SQL)) == before
    assert not MIGRATION.read_text(encoding="utf-8").startswith("-- migrate: expand")  # contract: MIG-4 order


async def test_the_grants_and_the_readers(admin: asyncpg.Connection) -> None:
    app = {
        p: await admin.fetchval("SELECT has_table_privilege('crm_app', 'crm.commission', $1)", p)
        for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
    }
    assert app == {"SELECT": True, "INSERT": False, "UPDATE": False, "DELETE": False}  # UPDATE: named columns only
    for column, allowed in (("status", True), ("received_amount", False), ("fx_rate", False), ("trip_id", False)):
        assert (
            await admin.fetchval("SELECT has_column_privilege('crm_app', 'crm.commission', $1, 'UPDATE')", column)
            is allowed
        ), column
    cancel = {
        p: await admin.fetchval("SELECT has_table_privilege('crm_app', 'crm.trip_cancellation', $1)", p)
        for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
    }
    assert cancel == {"SELECT": True, "INSERT": False, "UPDATE": False, "DELETE": False}  # writes: the two functions
    assert not await admin.fetchval(
        "SELECT bool_or(has_column_privilege('crm_app', 'crm.trip_cancellation', a.attname, 'UPDATE')) "
        "FROM pg_attribute a WHERE a.attrelid = 'crm.trip_cancellation'::regclass AND a.attnum > 0"
    )
    for fn in ("crm.trip_cancellation_open(uuid, text, jsonb)", "crm.trip_cancellation_finish(uuid)"):
        assert await admin.fetchval("SELECT has_function_privilege('crm_app', $1, 'EXECUTE')", fn), fn
        assert await admin.fetchval(
            "SELECT prosecdef AND NOT has_function_privilege('public', p.oid, 'EXECUTE') FROM pg_proc p "
            "WHERE p.oid = $1::regprocedure",
            fn,
        ), fn
    for reader in READERS:
        for table in ("commission", "trip_cancellation"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, 'SELECT')", reader, f"crm.{table}")


async def test_sync_creates_one_expected_row_with_the_supplier_due_days(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        await conn.execute(
            "UPDATE crm.agency_supplier SET commission_due_days = 45 WHERE agency_supplier_id = $1", ids["supplier"]
        )
    line = await _commission_line(app_pool, ids)
    assert await _sync(app_pool, ids) == {"created": 1, "cancelled": 0}
    assert await _sync(app_pool, ids) == {"created": 0, "cancelled": 0}  # idempotent
    rows = await _rows(app_pool, ids)
    assert [(r["booking_line_id"], r["status"], str(r["due_on"])) for r in rows] == [(line, "expected", "2027-04-18")]
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        commit = await conn.fetchval(
            "SELECT line_status FROM crm.trip_commitment WHERE kind = 'commission' AND source_ref = $1",
            str(rows[0]["commission_id"]),
        )
    assert commit == "expected"


async def test_the_assistant_confirm_still_creates_the_row_but_reads_none(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    await _commission_line(app_pool, ids, role="assistant")
    assert await _sync(app_pool, ids, "assistant") == {"created": 1, "cancelled": 0}
    assert await _rows(app_pool, ids, "assistant") == []
    assert len(await _rows(app_pool, ids, "agent")) == 1
    async with _as(app_pool, ids["tenant"], actor="platform:staff-1") as conn:  # AA staff read no commission
        assert await conn.fetchval("SELECT count(*) AS n FROM crm.commission") == 0


async def test_zero_cancels_and_a_new_amount_makes_a_new_row(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    line = await _commission_line(app_pool, ids)
    await _sync(app_pool, ids)
    await _money(app_pool, ids, line, "commission_expected", "0", "USD")
    assert await _sync(app_pool, ids) == {"created": 0, "cancelled": 1}
    await _money(app_pool, ids, line, "commission_expected", "12", "USD")
    assert await _sync(app_pool, ids) == {"created": 1, "cancelled": 0}
    assert [(r["status"], r["close_reason"]) for r in await _rows(app_pool, ids)] == [
        ("cancelled", "no_commission"),
        ("expected", None),
    ]


async def test_a_cancelled_line_cancels_it_unless_kept(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    first = await _commission_line(app_pool, ids)
    second = await _commission_line(app_pool, ids)
    await _sync(app_pool, ids)
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        await conn.execute("UPDATE crm.commission SET keep_on_cancel = true WHERE booking_line_id = $1", second)
        await conn.execute(
            "UPDATE crm.booking_line SET status = 'cancelled' WHERE booking_line_id = ANY($1::uuid[])", [first, second]
        )
    assert await _sync(app_pool, ids) == {"created": 0, "cancelled": 1}
    got = {r["booking_line_id"]: (r["status"], r["close_reason"]) for r in await _rows(app_pool, ids)}
    assert got == {first: ("cancelled", "line_cancelled"), second: ("expected", None)}


async def _aa_booked(admin: asyncpg.Connection, pool: asyncpg.Pool, ids: dict[str, Any]) -> Any:
    """The trip walked to booked with one AA booking (an AA line Booking confirmed, ``bk_booking_id``)."""
    await _put_stage(admin, ids, "booked")
    async with _as(pool, ids["tenant"], actor="job:crm_booking_sync") as conn:
        return await conn.fetchval(
            "INSERT INTO crm.booking_line (trip_id, agency_supplier_id, supplier_kind, title, status, bk_booking_id) "
            "VALUES ($1, $2, 'aa', 'Mekong two days', 'confirmed', $3) RETURNING booking_line_id",
            ids["trip"],
            ids["aa"],
            f"BK-{uuid.uuid4().hex[:6]}",
        )


async def test_nothing_new_while_a_cancellation_is_pending(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    await _aa_booked(admin, app_pool, ids)
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        assert json.loads(await conn.fetchval(OPEN, ids["trip"], "client_request"))["status"] == "pending"
    await _commission_line(app_pool, ids)
    assert await _sync(app_pool, ids) == {"created": 0, "cancelled": 0}


async def test_e_a_receipt_in_another_currency_uses_the_receipt_date_rate(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    cur = _currency()
    owner = ids["members"]["owner"]
    async with _as(app_pool, ids["tenant"], "owner", owner) as conn:
        await conn.execute(OVERRIDE_SQL, cur, "USD", "0.5", owner, 0)  # today: the confirmation day
        await conn.execute(OVERRIDE_SQL, cur, "USD", "0.25", owner, 5)  # five days ago: the receipt day
    line = await _commission_line(app_pool, ids, amount="40", currency="USD")
    other = await _commission_line(app_pool, ids, amount="40", currency=cur)
    await _sync(app_pool, ids)
    rows = {r["booking_line_id"]: r["commission_id"] for r in await _rows(app_pool, ids)}
    async with _as(app_pool, ids["tenant"], "agent", ids["members"]["agent"]) as conn:
        day = await conn.fetchval(f"SELECT {TODAY} - 5")  # noqa: S608 — fixed text
        # 100 of the other currency arrive for a USD commission: the receipt day's rate (0.25), not today's (0.5).
        await conn.fetchval(RECEIVE, rows[line], "100", cur, day, "BANK-77")
        # The other one is paid in its own currency and amount: equal amounts -> the FX difference in base.
        await conn.fetchval(RECEIVE, rows[other], "40", cur, day, None)
    got = {r["booking_line_id"]: r for r in await _rows(app_pool, ids)}
    assert got[line]["received_amount_base"] == Decimal("25.00") and got[line]["fx_rate"] == Decimal("0.25")
    assert got[line]["fx_basis"] == "receipt_date" and got[line]["fx_rate_date"] == day
    assert got[line]["short_amount"] is None and got[line]["fx_difference_base"] is None  # never "underpaid" by FX
    assert got[other]["received_amount_base"] == Decimal("10.00")
    assert got[other]["fx_difference_base"] == Decimal("10.00")  # expected 20.00 at confirm - 10.00 at receipt


async def test_short_is_judged_in_the_original_currency(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    await _commission_line(app_pool, ids, amount="40")
    await _sync(app_pool, ids)
    (row,) = await _rows(app_pool, ids)
    async with _as(app_pool, ids["tenant"], "agent", ids["members"]["agent"]) as conn:
        await conn.fetchval(RECEIVE, row["commission_id"], "30", "USD", await conn.fetchval(f"SELECT {TODAY}"), None)
    (row,) = await _rows(app_pool, ids)
    assert (row["status"], row["short_amount"], row["fx_difference_base"]) == ("received", Decimal("10.00"), None)


async def test_the_receipt_goes_through_the_function_only_and_seals_the_row(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    await _commission_line(app_pool, ids)
    await _sync(app_pool, ids)
    (row,) = await _rows(app_pool, ids)
    cid = row["commission_id"]
    async with app_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError) as err:  # a status move to received without the money
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], f"member:{ids['members']['owner']}")
                await conn.execute("SELECT set_config('app.member_role', 'owner', true)")
                await conn.execute("UPDATE crm.commission SET status = 'received' WHERE commission_id = $1", cid)
        assert err.value.sqlstate in ("CRM46", "23514")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # no INSERT, no receipt column, no DELETE
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], f"member:{ids['members']['owner']}")
                await conn.execute("UPDATE crm.commission SET received_amount = 1 WHERE commission_id = $1", cid)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], f"member:{ids['members']['owner']}")
                await conn.execute("DELETE FROM crm.commission WHERE commission_id = $1", cid)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # the assistant records no money
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], f"member:{ids['members']['assistant']}")
                await conn.execute("SELECT set_config('app.member_role', 'assistant', true)")
                await conn.fetchval(RECEIVE, cid, "10", "USD", await conn.fetchval(f"SELECT {TODAY}"), None)
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        await conn.fetchval(RECEIVE, cid, "10", "USD", await conn.fetchval(f"SELECT {TODAY}"), "REF-1")
    async with app_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError) as err:
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], f"member:{ids['members']['owner']}")
                await conn.execute("SELECT set_config('app.member_role', 'owner', true)")
                await conn.execute("UPDATE crm.commission SET due_on = due_on + 1 WHERE commission_id = $1", cid)
        assert err.value.sqlstate == "CRM46"
        with pytest.raises(asyncpg.PostgresError) as err:  # a second receipt
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], f"member:{ids['members']['owner']}")
                await conn.execute("SELECT set_config('app.member_role', 'owner', true)")
                await conn.fetchval(RECEIVE, cid, "10", "USD", await conn.fetchval(f"SELECT {TODAY}"), None)
        assert err.value.sqlstate == "CRM46"


async def test_r21_a_claimed_commission_blocks_the_trip_delete(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    await _commission_line(app_pool, ids)
    await _sync(app_pool, ids)
    assert "has_commission" not in await _blockers(app_pool, ids["tenant"], ids["trip"])
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        await conn.execute(
            "UPDATE crm.commission SET status = 'claimed', claimed_at = now(), claim_ref = 'INV-SUP-9' "
            "WHERE trip_id = $1",
            ids["trip"],
        )
    await _sync(app_pool, ids)
    assert "has_commission" in await _blockers(app_pool, ids["tenant"], ids["trip"])


async def test_trip_cancellation_one_per_trip_and_closed_once_done(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool
) -> None:
    ids = await _ledger(app_pool)
    line = await _aa_booked(admin, app_pool, ids)
    owner = (app_pool, ids["tenant"], "owner", ids["members"]["owner"])
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # the assistant never cancels a trip (§4.6)
        async with _as(app_pool, ids["tenant"], "assistant", ids["members"]["assistant"]) as conn:
            await conn.fetchval(OPEN, ids["trip"], "client_request")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # no direct write: a "done" row while AA holds a booking
        async with _as(*owner) as conn:
            await conn.execute(
                "INSERT INTO crm.trip_cancellation (trip_id, status, reason_code, from_stage, finished_at) "
                "VALUES ($1, 'done', 'client_request', 'booked', now())",
                ids["trip"],
            )
    async with _as(*owner) as conn:
        opened = json.loads(await conn.fetchval(OPEN, ids["trip"], "client_request"))
        row = await conn.fetchrow("SELECT trip_cancellation_id, status, from_stage, aa_bookings, created_by "
                                  "FROM crm.trip_cancellation")  # fmt: skip
    assert opened == {"status": "pending", "aa_bookings": 1}
    assert (row["status"], row["from_stage"], row["aa_bookings"]) == ("pending", "booked", 1)
    assert row["created_by"] == f"member:{ids['members']['owner']}"
    with pytest.raises(asyncpg.UniqueViolationError):
        async with _as(*owner) as conn:
            await conn.fetchval(OPEN, ids["trip"], "other")
    for sql in (
        "UPDATE crm.trip_cancellation SET status = 'done', finished_at = now() WHERE trip_cancellation_id = $1",
        "UPDATE crm.trip_cancellation SET effects = '{\"x\": 1}' WHERE trip_cancellation_id = $1",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with _as(*owner) as conn:
                await conn.execute(sql, row["trip_cancellation_id"])
    job = (app_pool, ids["tenant"])
    async with _as(*job, actor="job:crm_booking_sync") as conn:
        assert await conn.fetchval(FINISH, ids["trip"]) is False  # card R: the AA booking is still live
    async with _as(*job, actor="job:crm_booking_sync") as conn:
        await conn.execute("UPDATE crm.booking_line SET status = 'cancelled' WHERE booking_line_id = $1", line)
        assert await conn.fetchval(FINISH, ids["trip"]) is False  # the trip has not moved to cancelled yet
    async with admin.transaction():  # the trip moves to cancelled (the sync's move, before it finishes the row)
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await _ctx(admin, ids["tenant"], "job:crm_booking_sync")
        await admin.execute(
            "UPDATE crm.trip SET stage = 'cancelled', cancel_reason_code = 'client_request' WHERE trip_id = $1",
            ids["trip"],
        )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with _as(app_pool, ids["tenant"], "assistant", ids["members"]["assistant"]) as conn:
            await conn.fetchval(FINISH, ids["trip"])
    async with _as(*job, actor="job:crm_booking_sync") as conn:
        assert await conn.fetchval(FINISH, ids["trip"]) is True
        assert await conn.fetchval(FINISH, ids["trip"]) is False  # done already
        done = await conn.fetchrow("SELECT status, finished_at FROM crm.trip_cancellation")
    assert done["status"] == "done" and done["finished_at"] is not None
    async with admin.transaction():  # a done row is closed, even for the owner role
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await _ctx(admin, ids["tenant"], "seed:test")
        with pytest.raises(asyncpg.PostgresError) as err:
            await admin.execute("UPDATE crm.trip_cancellation SET effects = '{\"x\": 1}'")
    assert err.value.sqlstate == "CRM46"
    other = await _ledger(app_pool)
    async with _as(app_pool, other["tenant"], "owner", other["members"]["owner"]) as conn:
        assert await conn.fetchval("SELECT count(*) AS n FROM crm.trip_cancellation") == 0
        with pytest.raises(asyncpg.CheckViolationError):  # an inquiry is not cancelled (it is lost)
            await conn.fetchval(OPEN, other["trip"], "client_request")
    await _put_stage(admin, other, "booked")
    async with _as(app_pool, other["tenant"], "agent", other["members"]["agent"]) as conn:  # no AA booking: done
        assert json.loads(await conn.fetchval(OPEN, other["trip"], "duplicate")) == {"status": "done", "aa_bookings": 0}


async def test_cancel_requests_and_the_cancellation_fee(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    job = "job:crm_booking_sync"
    async with _as(app_pool, ids["tenant"], actor=job) as conn:
        line = await conn.fetchval(
            "INSERT INTO crm.booking_line (trip_id, agency_supplier_id, supplier_kind, title, status, bk_booking_id) "
            "VALUES ($1, $2, 'aa', 'Mekong two days', 'confirmed', 'BK-1') RETURNING booking_line_id",
            ids["trip"],
            ids["aa"],
        )
        await conn.fetchval("SELECT crm.booking_line_aa_money($1, 'cost', 200, 'USD', 1, DATE '2026-10-25')", line)
        await conn.execute(
            "INSERT INTO crm.aa_request (trip_id, booking_line_id, kind) VALUES ($1, $2, 'cancel')", ids["trip"], line
        )
        first = await conn.fetchval("SELECT crm.booking_line_aa_cancel_fee($1, 50, 'USD', DATE '2026-11-01')", line)
        again = await conn.fetchval("SELECT crm.booking_line_aa_cancel_fee($1, 50, 'USD', DATE '2026-11-01')", line)
    assert first == again
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        fee = await conn.fetchrow(
            "SELECT amount, amount_base, fx_source, fx_basis, fx_rate_date FROM crm.v_booking_line_money_current "
            "WHERE booking_line_id = $1 AND kind = 'cancellation_cost'",
            line,
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # a member never writes AA money
            await conn.fetchval("SELECT crm.booking_line_aa_cancel_fee($1, 60, 'USD', DATE '2026-11-01')", line)
    assert dict(fee) == {
        "amount": Decimal("50.00"),
        "amount_base": Decimal("50.00"),
        "fx_source": "aa_quote_lock",
        "fx_basis": "quote_lock",
        "fx_rate_date": dt.date(2026, 10, 25),
    }
