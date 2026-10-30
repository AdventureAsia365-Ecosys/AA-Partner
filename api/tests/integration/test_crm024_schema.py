"""crm-024 (AA-952 CRM-23 slices 2-3) on PostgreSQL with the RDS role shape: invoices and payments in the database.

- ``crm.invoice`` / ``crm.invoice_line`` / ``crm.invoice_sequence``: a draft takes lines; the issue needs a charge line
  whose sum is the total (CRM39); an issued invoice is sealed (CRM31) and its lines never change (CRM39); the number
  sequence moves by one (CRM40); status moves follow crm.status_transition.
- ``crm.payment``: a payment names an invoice or a schedule row; a refund names the payment it returns; still
  append-only; payment_schedule_once_uq holds for the payments without an invoice only.
- ``crm.refund_request``: sealed once decided (CRM31), executed only after approval.
- ``crm.payment_provider_account``: one active account per provider; a manual account has no external id.
- FORCE RLS and same-agency keys on every new table; the trip and agency erase take every new row.

The API's use of them is in tests/integration/test_invoices_api.py and test_payments_api.py.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "024_crm_invoices_payments.sql"
NEW_TABLES = ("invoice_sequence", "invoice", "invoice_line", "refund_request", "payment_provider_account")


async def _reapply(admin: asyncpg.Connection) -> None:
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")


async def test_crm024_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    # Earlier test modules re-apply their own erase_subject text: put this key's back first.
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-024'") == 1
    await _reapply(admin)
    src = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    assert src.count("'invoice'") == 2 and src.count("'refund_request'") == 2 and "'dispatch_attempt'" in src
    assert src.count("'payment_provider_account'") == 1 and src.count("'invoice_sequence'") == 1
    order = [src.index(f"['{t}', 'trip_id = $1'") for t in ("refund_request", "payment", "invoice_line", "invoice")]
    assert order == sorted(order) and order[-1] < src.index("['payment_schedule', 'trip_id = $1'")


async def test_privileges(admin: asyncpg.Connection) -> None:
    for table in NEW_TABLES:
        for privilege, want in (("SELECT", True), ("INSERT", True), ("UPDATE", True), ("TRUNCATE", False),
                                ("DELETE", table == "invoice_line")):  # fmt: skip
            got = await admin.fetchval("SELECT has_table_privilege('crm_app', $1, $2)", f"crm.{table}", privilege)
            assert got is want, (table, privilege)
        for reader in READERS:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert not await admin.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege
                )
        forced = await admin.fetchval(
            "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = $1::regclass", f"crm.{table}"
        )
        assert forced, table
    for privilege, want in (("INSERT", True), ("UPDATE", False), ("DELETE", False)):
        assert await admin.fetchval("SELECT has_table_privilege('crm_app', 'crm.payment', $1)", privilege) is want


async def _draft(conn: asyncpg.Connection, trip: uuid.UUID, *, kind: str = "service_fee") -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO crm.invoice (trip_id, kind, currency, tax_rate, tax_inclusive) VALUES ($1, $2, 'USD', 10, true) "
        "RETURNING invoice_id",
        trip,
        kind,
    )


async def _line(conn: asyncpg.Connection, invoice: uuid.UUID, trip: uuid.UUID, pos: int, amount: str) -> None:
    await conn.execute(
        "INSERT INTO crm.invoice_line (invoice_id, trip_id, position, kind, source, description, quantity, "
        "unit_amount, amount) VALUES ($1, $2, $3, 'charge', 'service_fee', 'Planning fee', 1, $4::numeric, "
        "$4::numeric)",
        invoice,
        trip,
        pos,
        amount,
    )


ISSUE_SQL = """
UPDATE crm.invoice SET status = 'open', number_seq = $2, number = $3, issue_date = current_date,
       issued_at = now(), issued_by = 'member:test', due_on = current_date + 7, subtotal_amount = $4::numeric,
       tax_amount = $5::numeric, total_amount = $4::numeric + $5::numeric
 WHERE invoice_id = $1
"""


async def _raises(conn: asyncpg.Connection, sqlstate: str, sql: str, *args: Any) -> None:
    with pytest.raises(asyncpg.PostgresError) as err:
        async with conn.transaction():
            await conn.execute(sql, *args)
    assert err.value.sqlstate == sqlstate, (err.value.sqlstate, str(err.value))


async def test_an_invoice_is_issued_with_matching_lines_then_sealed(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            invoice = await _draft(conn, ids["trip"])
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            # No charge line: CRM39 invoice_empty.
            await _raises(conn, "CRM39", ISSUE_SQL, invoice, 1, "INV-000001", 90.91, 9.09)
            await _line(conn, invoice, ids["trip"], 1, "100.00")
            await _line(conn, invoice, ids["trip"], 2, "50.00")
            # The total (tax-inclusive) is not the line sum: CRM39 invoice_totals.
            await _raises(conn, "CRM39", ISSUE_SQL, invoice, 1, "INV-000001", 90.91, 9.09)
            # A charge amount that is not quantity x unit amount.
            await _raises(
                conn,
                "23514",
                "INSERT INTO crm.invoice_line (invoice_id, trip_id, position, kind, source, description, quantity, "
                "unit_amount, amount) VALUES ($1, $2, 3, 'charge', 'manual', 'x', 2, 10, 25)",
                invoice,
                ids["trip"],
            )
            # A draft has no number.
            await _raises(conn, "23514", "UPDATE crm.invoice SET number = 'INV-000009' WHERE invoice_id = $1", invoice)
            await conn.execute(ISSUE_SQL, invoice, 1, "INV-000001", 136.36, 13.64)
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            # Sealed: the amounts, the number and the memo never change; the lines neither.
            await _raises(conn, "CRM31", "UPDATE crm.invoice SET total_amount = 1, subtotal_amount = 1, tax_amount = 0 "
                          "WHERE invoice_id = $1", invoice)  # fmt: skip
            await _raises(conn, "CRM31", "UPDATE crm.invoice SET memo = 'later' WHERE invoice_id = $1", invoice)
            await _raises(conn, "CRM39", "UPDATE crm.invoice_line SET description = 'x' WHERE invoice_id = $1", invoice)
            await _raises(conn, "CRM39", "DELETE FROM crm.invoice_line WHERE invoice_id = $1", invoice)
            # Back to draft is no allowed move.
            await _raises(conn, "23514", "UPDATE crm.invoice SET status = 'draft' WHERE invoice_id = $1", invoice)
            # The same number twice: unique per agency.
            second = await _draft(conn, ids["trip"])
            await _line(conn, second, ids["trip"], 1, "10.00")
            await _raises(conn, "23505", ISSUE_SQL, second, 1, "INV-000001", 9.09, 0.91)
            await conn.execute(
                "UPDATE crm.invoice SET status = 'void', voided_at = now(), voided_by = 'member:test', "
                "void_reason = 'Raised twice' WHERE invoice_id = $1",
                invoice,
            )
        async with conn.transaction():
            await _ctx(conn, other["tenant"])  # FORCE RLS
            for table in ("invoice", "invoice_line"):
                assert await conn.fetchval(f"SELECT count(*) FROM crm.{table}") == 0  # noqa: S608 — fixed names
            # A line of another agency's invoice is refused: the guard cannot see the invoice (CRM39) before the
            # same-agency key would refuse it.
            with pytest.raises(asyncpg.PostgresError) as err:
                await _line(conn, invoice, ids["trip"], 9, "1.00")
            assert err.value.sqlstate == "CRM39"
    assert await _count(app_pool, ids["tenant"], "invoice") == 2


async def test_the_sequence_moves_by_one(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    upsert = (
        "INSERT INTO crm.invoice_sequence (last_number) VALUES (1) ON CONFLICT (tenant_id) DO UPDATE "
        "SET last_number = crm.invoice_sequence.last_number + 1 RETURNING last_number"
    )
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            assert [await conn.fetchval(upsert) for _ in range(3)] == [1, 2, 3]
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            await _raises(conn, "CRM40", "UPDATE crm.invoice_sequence SET last_number = 1")
            await _raises(conn, "CRM40", "UPDATE crm.invoice_sequence SET last_number = last_number + 5")
            await _raises(conn, "42501", "DELETE FROM crm.invoice_sequence")


async def _service_fee_invoice(conn: asyncpg.Connection, trip: uuid.UUID) -> uuid.UUID:
    invoice = await _draft(conn, trip)
    await _line(conn, invoice, trip, 1, "200.00")
    seq = await conn.fetchval(
        "INSERT INTO crm.invoice_sequence (last_number) VALUES (1) ON CONFLICT (tenant_id) DO UPDATE "
        "SET last_number = crm.invoice_sequence.last_number + 1 RETURNING last_number"
    )
    await conn.execute(ISSUE_SQL, invoice, seq, f"INV-{seq:06d}", 181.82, 18.18)
    return invoice


PAY_SQL = """
INSERT INTO crm.payment (trip_id, invoice_id, amount, currency, method, received_on, entry_kind, refund_of)
VALUES ($1, $2, $3, 'USD', 'bank_transfer', current_date, $4, $5) RETURNING payment_id
"""


async def test_payments_refunds_and_refund_requests(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    trip = ids["trip"]
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            invoice = await _service_fee_invoice(conn, trip)
            paid = await conn.fetchval(PAY_SQL, trip, invoice, 120, "payment", None)
            assert await conn.fetchval(PAY_SQL, trip, invoice, 80, "payment", None)  # partial payments
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            # Neither an invoice nor a schedule row; a refund without the payment it returns.
            await _raises(
                conn,
                "23514",
                "INSERT INTO crm.payment (trip_id, amount, currency, method, received_on) "
                "VALUES ($1, 5, 'USD', 'cash', current_date)",
                trip,
            )
            await _raises(conn, "23514", PAY_SQL, trip, invoice, 5, "refund", None)
            await _raises(conn, "23514", PAY_SQL, trip, invoice, 5, "payment", paid)
            await _raises(conn, "42501", "UPDATE crm.payment SET amount = 1")
            await _raises(
                conn,
                "23514",
                "INSERT INTO crm.payment (trip_id, invoice_id, amount, currency, method, received_on, provider) "
                "VALUES ($1, $2, 5, 'USD', 'cash', current_date, 'paypal')",
                trip,
                invoice,
            )
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "member:agent")
            refund = await conn.fetchval(
                "INSERT INTO crm.refund_request (trip_id, invoice_id, payment_id, amount, currency, reason) "
                "VALUES ($1, $2, $3, 50, 'USD', 'Hotel night cancelled') RETURNING refund_id",
                trip,
                invoice,
                paid,
            )
            assert await conn.fetchval("SELECT requested_by FROM crm.refund_request") == "member:agent"
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            # Executed before an approval: the execution columns are sealed while proposed (CRM31).
            refund_entry = await conn.fetchval(PAY_SQL, trip, invoice, 50, "refund", paid)
            await _raises(
                conn,
                "CRM31",
                "UPDATE crm.refund_request SET status = 'executed', executed_at = now(), refund_payment_id = $2, "
                "decided_at = now(), decided_by = 'member:owner' WHERE refund_id = $1",
                refund,
                refund_entry,
            )
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            await conn.execute(
                "UPDATE crm.refund_request SET status = 'approved', decided_at = now(), decided_by = 'member:owner', "
                "base_currency = 'USD', base_amount = 50 WHERE refund_id = $1",
                refund,
            )
            # The request and the decision are sealed once decided.
            await _raises(conn, "CRM31", "UPDATE crm.refund_request SET amount = 70 WHERE refund_id = $1", refund)
            await _raises(
                conn, "CRM31", "UPDATE crm.refund_request SET decided_by = 'member:x' WHERE refund_id = $1", refund
            )
            refund_entry = await conn.fetchval(PAY_SQL, trip, invoice, 50, "refund", paid)
            await conn.execute(
                "UPDATE crm.refund_request SET status = 'executed', executed_at = now(), refund_payment_id = $2 "
                "WHERE refund_id = $1",
                refund,
                refund_entry,
            )
            await _raises(conn, "CRM31", "UPDATE crm.refund_request SET executed_at = now() - interval '1 day'")
    # Two payments, the refund entry of the refused execution (its savepoint only rolled the request back) and the
    # executed one: a refund entry is tied to its request by the API (refund_payment_id), not by the database.
    assert await _count(app_pool, ids["tenant"], "payment") == 4


async def test_schedule_payments_without_an_invoice_stay_one_per_row(admin: asyncpg.Connection) -> None:
    index = await admin.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'crm' AND indexname = 'payment_schedule_once_uq'"
    )
    assert "UNIQUE" in index and "invoice_id IS NULL" in index and "entry_kind = 'payment'" in index
    assert not await admin.fetchval(
        "SELECT attnotnull FROM pg_attribute WHERE attrelid = 'crm.payment'::regclass AND attname = 'schedule_id'"
    )


async def test_one_active_account_per_provider(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    insert = (
        "INSERT INTO crm.payment_provider_account (provider, display_name, instructions, external_account_id) "
        "VALUES ($1, $2, $3, $4) RETURNING account_id"
    )
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            manual = await conn.fetchval(insert, "manual", "Bank transfer", "Pay to our account 123", None)
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            await _raises(conn, "23505", insert, "manual", "Cash", None, None)
            await _raises(conn, "23514", insert, "stripe", "Card", None, None)  # a provider account names its id
            await _raises(conn, "23514", insert, "manual", "x", None, "acct_123")
        async with conn.transaction():
            await _ctx(conn, ids["tenant"])
            await conn.execute(
                "UPDATE crm.payment_provider_account SET status = 'disconnected', disconnected_at = now() "
                "WHERE account_id = $1",
                manual,
            )
            assert await conn.fetchval(insert, "manual", "Cash", "Pay at our office", None)
            await _raises(conn, "42501", "DELETE FROM crm.payment_provider_account")


async def test_the_trip_and_agency_erase_take_the_invoices_and_payments(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    second_trip = uuid.uuid4()
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("INSERT INTO crm.trip (trip_id, title) VALUES ($1, 'Second trip')", second_trip)
        for trip in (ids["trip"], second_trip):
            invoice = await _service_fee_invoice(conn, trip)
            paid = await conn.fetchval(PAY_SQL, trip, invoice, 200, "payment", None)
            refund = await conn.fetchval(
                "INSERT INTO crm.refund_request (trip_id, invoice_id, payment_id, amount, currency, reason, status, "
                "decided_at, decided_by) VALUES ($1, $2, $3, 20, 'USD', 'Goodwill', 'approved', now(), 'member:o') "
                "RETURNING refund_id",
                trip,
                invoice,
                paid,
            )
            entry = await conn.fetchval(PAY_SQL, trip, invoice, 20, "refund", paid)
            await conn.execute(
                "UPDATE crm.refund_request SET status = 'executed', executed_at = now(), refund_payment_id = $2 "
                "WHERE refund_id = $1",
                refund,
                entry,
            )
        await conn.execute(
            "INSERT INTO crm.payment_provider_account (provider, display_name, instructions) "
            "VALUES ('manual', 'Bank transfer', 'Our bank')"
        )
    dry = await _erase(app_pool, ids["tenant"], "trip", ids["trip"], dry_run=True)
    assert {k: dry["counts"][k] for k in ("refund_request", "payment", "invoice_line", "invoice")} == {
        "refund_request": 1,
        "payment": 2,
        "invoice_line": 1,
        "invoice": 1,
    }
    await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert await _count(app_pool, ids["tenant"], "invoice") == 1
    assert await _count(app_pool, ids["tenant"], "payment") == 2
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    done = await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert done["counts"]["invoice_sequence"] == 1 and done["counts"]["payment_provider_account"] == 1
    for table in (*NEW_TABLES, "payment"):
        assert await _count(app_pool, ids["tenant"], table) == 0, table
