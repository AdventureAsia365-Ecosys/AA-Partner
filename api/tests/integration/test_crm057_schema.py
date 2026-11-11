"""crm-057 (AA-1072, a contract file) on PostgreSQL with the RDS role shape: "Book now" and online payment.

- ``crm.tenant_settings.public_booking_enabled``: default off.
- ``crm.public_booking_attempt``: the value checks, one attempt per client submission id, the state rule (a created
  attempt names its trip and proposal, a refused one its code), the status moves, cascade from the settings row.
- ``crm.integration_credential`` provider ``vn_gateway``; ``crm.payment`` method ``online`` and one payment per
  gateway reference (a webhook delivered twice writes one row).
- ``crm.payment_session``: the reference is unique across agencies and only the lookup policy finds another agency's
  row by it (SELECT only, while ``app.payment_txn_ref`` is set); the state rule; the status moves; a held trip's
  session is not deleted directly (CRM10); the trip erase takes it through the invoice cascade.
- Tenant isolation: another agency sees nothing.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import OWNER
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CRM057 = Path(__file__).resolve().parents[2] / "migrations" / "057_crm_public_booking_online_payment.sql"


@pytest_asyncio.fixture(autouse=True)
async def _crm057(admin: asyncpg.Connection) -> AsyncIterator[None]:
    """crm-057 again (re-runnable) before each test: an earlier test of the session may have re-run an older file."""
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(CRM057.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    yield


async def _settings(conn: asyncpg.Connection) -> None:
    await conn.execute("INSERT INTO crm.tenant_settings (tenant_id) VALUES (current_setting('app.tenant_id')::uuid)"
                       " ON CONFLICT DO NOTHING")  # fmt: skip


def _attempt_args(**over: Any) -> dict[str, Any]:
    base = {
        "form": uuid.uuid4(),
        "source": "tour-key-1",
        "departure": "dep-2027-03-01",
        "party": '{"adults": 2, "child_ages": [7]}',
        "submission": uuid.uuid4(),
        "ip": secrets.token_bytes(32),
    }
    base.update(over)
    return base


INSERT_ATTEMPT = """
INSERT INTO crm.public_booking_attempt (intake_form_id, tour_source_id, departure_id, party, client_submission_id,
                                        ip_hash)
VALUES ($1, $2, $3, $4::jsonb, $5, $6) RETURNING attempt_id
"""


async def _attempt(conn: asyncpg.Connection, **over: Any) -> uuid.UUID:
    a = _attempt_args(**over)
    return await conn.fetchval(
        INSERT_ATTEMPT, a["form"], a["source"], a["departure"], a["party"], a["submission"], a["ip"]
    )


async def _invoice(conn: asyncpg.Connection, trip: uuid.UUID) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO crm.invoice (trip_id, kind, currency, tax_rate, tax_inclusive) VALUES ($1, 'adjustment', 'VND', 0,"
        " true) RETURNING invoice_id",
        trip,
    )


async def _session(conn: asyncpg.Connection, invoice: uuid.UUID, trip: uuid.UUID, ref: str | None = None) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO crm.payment_session (invoice_id, trip_id, account_id, provider, txn_ref, amount, currency,"
        " expires_at) VALUES ($1, $2, $3, 'vn_gateway', $4, 1500000, 'VND', now() + interval '15 minutes')"
        " RETURNING session_id",
        invoice,
        trip,
        uuid.uuid4(),
        ref or secrets.token_hex(12),
    )


async def _raises(conn: asyncpg.Connection, sqlstate: str, sql: str, *args: Any) -> None:
    async with conn.transaction():
        with pytest.raises(asyncpg.PostgresError) as err:
            await conn.execute(sql, *args)
    assert err.value.sqlstate == sqlstate, err.value


async def test_crm057_is_registered_and_book_now_is_off_by_default(app_pool: asyncpg.Pool, admin: asyncpg.Connection
                                                                   ) -> None:  # fmt: skip
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-057'") == 1
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "seed:test")
        await _settings(conn)
        assert await conn.fetchval("SELECT public_booking_enabled FROM crm.tenant_settings") is False


async def test_an_attempt_is_checked_and_one_per_submission(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            await _settings(conn)
        await _ctx(conn, ids["tenant"], "seed:test")
        same = uuid.uuid4()
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            await _attempt(conn, submission=same)
        for over in ({"ip": b"short"}, {"party": "[1, 2]"}, {"departure": "bad id!"}, {"source": ""}):
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], "seed:test")
                with pytest.raises(asyncpg.CheckViolationError):
                    await _attempt(conn, **over)
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            with pytest.raises(asyncpg.UniqueViolationError):
                await _attempt(conn, submission=same)


async def test_an_attempt_moves_only_forward_and_names_what_it_made(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            await _settings(conn)
            made = await _attempt(conn)
            refused = await _attempt(conn)
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            # created without its trip / proposal: the state rule refuses it
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE crm.public_booking_attempt SET status = 'created' WHERE attempt_id = $1",
                                   made)  # fmt: skip
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            await conn.execute(
                "UPDATE crm.public_booking_attempt SET status = 'created', trip_id = $2, proposal_id = $3"
                " WHERE attempt_id = $1",
                made,
                ids["trip"],
                uuid.uuid4(),
            )
            await conn.execute(
                "UPDATE crm.public_booking_attempt SET status = 'refused', refusal_code = 'aa_sold_out'"
                " WHERE attempt_id = $1",
                refused,
            )
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute("UPDATE crm.public_booking_attempt SET status = 'received', refusal_code = NULL"
                                   " WHERE attempt_id = $1", refused)  # fmt: skip


async def test_attempts_leave_with_the_agency_settings_row(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "seed:test")
        await _settings(conn)
        await _attempt(conn)
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await _ctx(admin, ids["tenant"], "seed:test")
        await admin.execute("DELETE FROM crm.tenant_settings WHERE tenant_id = $1", ids["tenant"])
        assert await admin.fetchval("SELECT count(*) FROM crm.public_booking_attempt") == 0


async def test_vn_gateway_credentials_and_online_payments_once_per_reference(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            await conn.execute(
                "INSERT INTO crm.integration_credential (provider, account_ref, secret_enc, status)"
                " VALUES ('vn_gateway', 'MERCHANT1', $1, 'connected')",
                secrets.token_bytes(40),
            )
            invoice = await _invoice(conn, ids["trip"])
        args = (ids["trip"], invoice, "online", "vn_gateway", "14012345")
        sql = (
            "INSERT INTO crm.payment (trip_id, invoice_id, amount, currency, method, received_on, provider,"
            " provider_ref, entry_kind) VALUES ($1, $2, 1500000, 'VND', $3, current_date, $4, $5, 'payment')"
        )
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            await conn.execute(sql, *args)
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(sql, *args)
        # The manual provider keeps its own references free (an agency may type the same bank reference twice).
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            await conn.execute(sql, ids["trip"], invoice, "bank_transfer", "manual", "REF-1")
            await conn.execute(sql, ids["trip"], invoice, "bank_transfer", "manual", "REF-1")


async def test_a_session_is_found_by_its_reference_only_through_the_lookup(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    ref = secrets.token_hex(12)
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            invoice = await _invoice(conn, ids["trip"])
            await _session(conn, invoice, ids["trip"], ref)
        # No agency context: nothing without the reference, exactly the one row with it.
        async with conn.transaction():
            assert await conn.fetchval("SELECT count(*) FROM crm.payment_session") == 0
            await conn.execute("SELECT set_config('app.payment_txn_ref', $1, true)", ref)
            row = await conn.fetchrow("SELECT tenant_id, invoice_id FROM crm.payment_session")
            assert row["tenant_id"] == ids["tenant"] and row["invoice_id"] == invoice
            # SELECT only: the lookup never lets an update through.
            assert await conn.execute("UPDATE crm.payment_session SET response_code = 'x'") == "UPDATE 0"
        other = await _agency(app_pool)
        async with conn.transaction():
            await _ctx(conn, other["tenant"], "seed:test")
            assert await conn.fetchval("SELECT count(*) FROM crm.payment_session") == 0
            oi = await _invoice(conn, other["trip"])
            with pytest.raises(asyncpg.UniqueViolationError):
                await _session(conn, oi, other["trip"], ref)


async def test_a_session_moves_and_closes_by_its_state_rule(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            invoice = await _invoice(conn, ids["trip"])
            s1 = await _session(conn, invoice, ids["trip"])
            s2 = await _session(conn, invoice, ids["trip"])
        for sql in (
            "UPDATE crm.payment_session SET status = 'paid', closed_at = now() WHERE session_id = $1",  # no payment
            "UPDATE crm.payment_session SET status = 'failed' WHERE session_id = $1",  # not closed
            "UPDATE crm.payment_session SET txn_ref = 'short' WHERE session_id = $1",
        ):
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], "seed:test")
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(sql, s1)
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            await conn.execute(
                "UPDATE crm.payment_session SET status = 'paid', closed_at = now(), payment_id = $2,"
                " provider_ref = '14012345', response_code = '00' WHERE session_id = $1",
                s1,
                uuid.uuid4(),
            )
            await conn.execute(
                "UPDATE crm.payment_session SET status = 'failed', closed_at = now(), response_code = '24'"
                " WHERE session_id = $1",
                s2,
            )
            await conn.execute("UPDATE crm.payment_session SET status = 'review' WHERE session_id = $1", s2)
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute("UPDATE crm.payment_session SET status = 'open', closed_at = NULL"
                                   " WHERE session_id = $1", s1)  # fmt: skip


async def test_a_held_trip_keeps_its_sessions_and_the_erases_take_them(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, t, "seed:test")
            invoice = await _invoice(conn, ids["trip"])
            session = await _session(conn, invoice, ids["trip"])
            hold = await conn.fetchval(
                "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) VALUES ('trip', $1, 'dispute')"
                " RETURNING hold_id",
                ids["trip"],
            )
        async with conn.transaction():
            await _ctx(conn, t, "seed:test")
            with pytest.raises(asyncpg.PostgresError) as err:
                await conn.execute("DELETE FROM crm.payment_session WHERE session_id = $1", session)
        assert err.value.sqlstate == "CRM10"
        async with conn.transaction():
            await _ctx(conn, t, "seed:test")
            await conn.execute(
                "UPDATE crm.legal_hold SET status = 'released', released_at = now(), released_by = 'seed:test'"
                " WHERE hold_id = $1",
                hold,
            )
    other = await _agency(app_pool)
    assert await _count(app_pool, other["tenant"], "payment_session") == 0
    await _erase(app_pool, t, "trip", ids["trip"])
    assert await _count(app_pool, t, "payment_session") == 0
