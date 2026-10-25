"""crm-016 (AA-946, card CRM-18) on PostgreSQL 15 with the RDS role shape: the thin full lifecycle.

- **R** (card) in the database: Stage 3 is recorded (``traveler_access.stage3_submitted_at``) only after the deposit
  of the accepted proposal is paid, else CRM32 ``stage3_locked``; a ``manual_booking`` override into Booked (deposit
  taken outside the CRM, not undone) opens it too (``crm.portal_unlocked``). A deposit row is never void.
- The gates: proposal -> booked opens with a paid deposit of the accepted proposal (else CRM19 ``gate_unmet:booking``,
  unless ``app.gate_override = booking``); booked -> pre_departure opens at ``s3 = 100`` (else CRM19).
- Sealed rows (CRM31): a sent proposal, a paid schedule row, a published agreement, a signed signature never change;
  the agreement hash is set by the database whatever the writer sends. ``crm.payment`` is append-only for crm_app.
- Shapes: amounts and balance date, one accepted / one sent version per trip, the code expiry (<= 10 minutes) and the
  session (<= 12 hours), link purposes, the readers have nothing.
- Erasure: trip, traveller and agency erases take every new table (no CRM16); a traveller erase also clears the name
  they typed to accept a proposal; a trip hold reaches the portal rows.
- The code mail: ``otp_challenge.code_enc`` is kept only while the code is pending.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase, _hold

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "016_crm_thin_lifecycle.sql"

NEW_TABLES = (
    "agreement_doc",
    "proposal",
    "payment_schedule",
    "payment",
    "traveler_access",
    "otp_challenge",
    "signature_request",
)


def _blob() -> bytes:
    return os.urandom(40)


def _hash() -> bytes:
    return os.urandom(32)


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = "member:test") -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        return await conn.fetchval(sql, *args)


async def _lifecycle(pool: asyncpg.Pool) -> dict[str, Any]:
    """An agency (_agency) with published terms + waiver, its trip at Proposal, a sent and accepted proposal and its
    two schedule rows (deposit 300 of 1000, balance 700), and portal access for both travellers."""
    ids = await _agency(pool)
    t = ids["tenant"]
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, t, "member:test")
        ids["terms"] = await conn.fetchval(
            "INSERT INTO crm.agreement_doc (kind, version_label, body_text) VALUES ('booking_terms', 'v1', "
            "'Booking terms of the agency.') RETURNING doc_id"
        )
        ids["waiver"] = await conn.fetchval(
            "INSERT INTO crm.agreement_doc (kind, version_label, body_text) VALUES ('waiver', 'v1', "
            "'I take part at my own risk.') RETURNING doc_id"
        )
        await conn.execute(
            "UPDATE crm.agreement_doc SET status = 'published', published_at = now() WHERE doc_id = ANY($1::uuid[])",
            [ids["terms"], ids["waiver"]],
        )
        await conn.execute(
            'UPDATE crm.trip SET intake_completeness = \'{"s1": 100, "s2": 100, "s3": 0}\' WHERE trip_id = $1',
            ids["trip"],
        )
        await conn.execute("UPDATE crm.trip SET stage = 'qualifying' WHERE trip_id = $1", ids["trip"])
        await conn.execute("UPDATE crm.trip SET stage = 'proposal' WHERE trip_id = $1", ids["trip"])
        ids["link"] = await conn.fetchval(
            "INSERT INTO crm.client_link (trip_id, traveler_id, purpose, token_hash, expires_at) "
            "VALUES ($1, $2, 'proposal', $3, now() + interval '14 days') RETURNING link_id",
            ids["trip"],
            ids["lead"],
            _hash(),
        )
        ids["proposal"] = await conn.fetchval(
            "INSERT INTO crm.proposal (trip_id, version, body_enc, currency, total_amount, deposit_amount, "
            "balance_due_on, terms_doc_id) VALUES ($1, 1, $2, 'USD', 1000, 300, current_date + 60, $3) "
            "RETURNING proposal_id",
            ids["trip"],
            _blob(),
            ids["terms"],
        )
        ids["price_hash"] = _hash()
        await conn.execute(
            "UPDATE crm.proposal SET status = 'sent', price_hash = $2, sent_at = now(), link_id = $3 "
            "WHERE proposal_id = $1",
            ids["proposal"],
            ids["price_hash"],
            ids["link"],
        )
        await conn.execute(
            "UPDATE crm.proposal SET status = 'accepted', accepted_at = now(), accepted_name_enc = $2, "
            "accepted_price_hash = price_hash, accepted_terms_doc_id = terms_doc_id WHERE proposal_id = $1",
            ids["proposal"],
            _blob(),
        )
        ids["deposit"] = await conn.fetchval(
            "INSERT INTO crm.payment_schedule (trip_id, proposal_id, kind, amount, currency, due_on) "
            "VALUES ($1, $2, 'deposit', 300, 'USD', current_date) RETURNING schedule_id",
            ids["trip"],
            ids["proposal"],
        )
        ids["balance"] = await conn.fetchval(
            "INSERT INTO crm.payment_schedule (trip_id, proposal_id, kind, amount, currency, due_on) "
            "VALUES ($1, $2, 'balance', 700, 'USD', current_date + 60) RETURNING schedule_id",
            ids["trip"],
            ids["proposal"],
        )
        for who, role in (("lead", "lead"), ("companion", "companion")):
            link = await conn.fetchval(
                "INSERT INTO crm.client_link (trip_id, traveler_id, purpose, token_hash, expires_at) "
                "VALUES ($1, $2, 'portal', $3, now() + interval '30 days') RETURNING link_id",
                ids["trip"],
                ids[who],
                _hash(),
            )
            ids[f"access_{who}"] = await conn.fetchval(
                "INSERT INTO crm.traveler_access (trip_id, traveler_id, role, link_id) VALUES ($1, $2, $3, $4) "
                "RETURNING access_id",
                ids["trip"],
                ids[who],
                role,
                link,
            )
    return ids


async def _pay_deposit(pool: asyncpg.Pool, ids: dict[str, Any]) -> uuid.UUID:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "member:test")
        await conn.execute(
            "UPDATE crm.payment_schedule SET status = 'paid', paid_at = now() WHERE schedule_id = $1", ids["deposit"]
        )
        return await conn.fetchval(
            "INSERT INTO crm.payment (trip_id, schedule_id, amount, currency, method, received_on) "
            "VALUES ($1, $2, 300, 'USD', 'bank_transfer', current_date) RETURNING payment_id",
            ids["trip"],
            ids["deposit"],
        )


async def _otp(conn: asyncpg.Connection, ids: dict[str, Any], who: str = "lead", **over: Any) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO crm.otp_challenge (access_id, trip_id, traveler_id, purpose, code_hash, expires_at) "
        "VALUES ($1, $2, $3, $4, $5, now() + $6::text::interval) RETURNING otp_id",
        ids[f"access_{who}"],
        ids["trip"],
        ids[who],
        over.get("purpose", "sign_in"),
        _hash(),
        over.get("ttl", "10 minutes"),
    )


async def test_crm016_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    # Runs first in this file: the crm-012 .. crm-015 tests re-apply their own erase_subject / stage-trigger text and
    # the crm-012 link purposes before it, so this puts the crm-016 objects back (and proves the file is idempotent).
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-016'") == 1
    before = await admin.fetchval("SELECT count(*) FROM crm.status_transition")
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM crm.status_transition") == before


@pytest.mark.parametrize("table", NEW_TABLES)
async def test_the_readers_have_nothing_on_the_new_tables(admin: asyncpg.Connection, table: str) -> None:
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege)


async def test_r_stage3_is_locked_until_the_deposit_is_paid(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    set_s3 = "UPDATE crm.traveler_access SET stage3_submitted_at = now() WHERE access_id = $1"
    with pytest.raises(asyncpg.PostgresError, match="stage3_locked") as err:
        await _run(app_pool, ids["tenant"], set_s3, ids["access_lead"])
    assert err.value.sqlstate == "CRM32"
    # An insert that names Stage 3 at once is refused the same way.
    await _run(
        app_pool,
        ids["tenant"],
        "UPDATE crm.traveler_access SET status = 'revoked' WHERE access_id = $1",
        ids["access_companion"],
    )
    with pytest.raises(asyncpg.PostgresError, match="stage3_locked"):
        await _run(
            app_pool,
            ids["tenant"],
            "INSERT INTO crm.traveler_access (trip_id, traveler_id, role, stage3_submitted_at) "
            "VALUES ($1, $2, 'companion', now())",
            ids["trip"],
            ids["companion"],
        )
    await _pay_deposit(app_pool, ids)
    await _run(app_pool, ids["tenant"], set_s3, ids["access_lead"])
    assert await _run(
        app_pool, ids["tenant"], "SELECT stage3_submitted_at IS NOT NULL FROM crm.traveler_access WHERE access_id = $1",
        ids["access_lead"],
    )  # fmt: skip


async def _history(pool: asyncpg.Pool, ids: dict[str, Any], *values: Any) -> uuid.UUID:
    return await _run(
        pool,
        ids["tenant"],
        "INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code, override, "
        "override_reason_code, undo_of) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING history_id",
        ids["trip"],
        *values,
    )


async def test_r_a_manual_booking_override_opens_the_portal(app_pool: asyncpg.Pool) -> None:
    """Review S251: a trip booked with the manual_booking override (deposit taken outside the CRM) is not locked
    for ever; an override that was undone does not count."""
    ids = await _lifecycle(app_pool)
    unlocked = "SELECT crm.portal_unlocked($1)"
    assert await _run(app_pool, ids["tenant"], unlocked, ids["trip"]) is False
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "member:test")
        await conn.execute("SELECT set_config('app.gate_override', 'booking', true)")
        await conn.execute("UPDATE crm.trip SET stage = 'booked' WHERE trip_id = $1", ids["trip"])
    first = await _history(app_pool, ids, "proposal", "booked", "agent_move", True, "manual_booking", None)
    assert await _run(app_pool, ids["tenant"], unlocked, ids["trip"]) is True
    # Undone (the exact reverse within 15 s): locked again until a new override.
    await _history(app_pool, ids, "booked", "proposal", "undo", False, None, first)
    assert await _run(app_pool, ids["tenant"], unlocked, ids["trip"]) is False
    set_s3 = "UPDATE crm.traveler_access SET stage3_submitted_at = now() WHERE access_id = $1"
    with pytest.raises(asyncpg.PostgresError, match="stage3_locked"):
        await _run(app_pool, ids["tenant"], set_s3, ids["access_lead"])
    await _history(app_pool, ids, "proposal", "booked", "agent_move", True, "manual_booking", None)
    await _run(app_pool, ids["tenant"], set_s3, ids["access_lead"])


async def test_a_deposit_row_is_never_void(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    void = "UPDATE crm.payment_schedule SET status = 'void' WHERE schedule_id = $1"
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, ids["tenant"], void, ids["deposit"])
    await _run(app_pool, ids["tenant"], void, ids["balance"])


async def test_the_booking_gate_opens_with_the_paid_deposit(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    to_booked = "UPDATE crm.trip SET stage = 'booked' WHERE trip_id = $1"
    with pytest.raises(asyncpg.PostgresError, match="gate_unmet:booking") as err:
        await _run(app_pool, ids["tenant"], to_booked, ids["trip"])
    assert err.value.sqlstate == "CRM19"
    # A balance paid first does not open it: the deposit does.
    await _run(
        app_pool,
        ids["tenant"],
        "UPDATE crm.payment_schedule SET status = 'paid', paid_at = now() WHERE schedule_id = $1",
        ids["balance"],
    )
    with pytest.raises(asyncpg.PostgresError, match="gate_unmet:booking"):
        await _run(app_pool, ids["tenant"], to_booked, ids["trip"])
    await _pay_deposit(app_pool, ids)
    await _run(app_pool, ids["tenant"], to_booked, ids["trip"])
    assert await _run(app_pool, ids["tenant"], "SELECT stage FROM crm.trip WHERE trip_id = $1", ids["trip"]) == "booked"


async def test_the_booking_override_still_passes_an_unmet_gate(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "member:test")
        await conn.execute("SELECT set_config('app.gate_override', 'booking', true)")
        await conn.execute("UPDATE crm.trip SET stage = 'booked' WHERE trip_id = $1", ids["trip"])


async def test_the_stage3_gate_opens_at_s3_100(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    await _pay_deposit(app_pool, ids)
    await _run(app_pool, ids["tenant"], "UPDATE crm.trip SET stage = 'booked' WHERE trip_id = $1", ids["trip"])
    to_pre = "UPDATE crm.trip SET stage = 'pre_departure' WHERE trip_id = $1"
    await _run(
        app_pool, ids["tenant"],
        "UPDATE crm.trip SET intake_completeness = '{\"s1\": 100, \"s2\": 100, \"s3\": 99}' WHERE trip_id = $1",
        ids["trip"],
    )  # fmt: skip
    with pytest.raises(asyncpg.PostgresError, match="gate_unmet:stage3") as err:
        await _run(app_pool, ids["tenant"], to_pre, ids["trip"])
    assert err.value.sqlstate == "CRM19"
    await _run(
        app_pool, ids["tenant"],
        "UPDATE crm.trip SET intake_completeness = '{\"s1\": 100, \"s2\": 100, \"s3\": 100}' WHERE trip_id = $1",
        ids["trip"],
    )  # fmt: skip
    await _run(app_pool, ids["tenant"], to_pre, ids["trip"])


@pytest.mark.parametrize(
    ("table", "key", "change"),
    [
        ("proposal", "proposal", "total_amount = 1200"),
        ("proposal", "proposal", "body_enc = body_enc || '\\x00'::bytea"),
        ("proposal", "proposal", "accepted_name_enc = accepted_name_enc || '\\x00'::bytea"),
        ("proposal", "proposal", "accepted_name_enc = NULL, accepted_name_erased_at = now()"),
        ("agreement_doc", "terms", "body_text = 'Changed after publishing.'"),
        ("agreement_doc", "terms", "version_label = 'v2'"),
    ],
)
async def test_sealed_rows_do_not_change(app_pool: asyncpg.Pool, table: str, key: str, change: str) -> None:
    ids = await _lifecycle(app_pool)
    id_col = {"proposal": "proposal_id", "agreement_doc": "doc_id"}[table]
    with pytest.raises(asyncpg.PostgresError, match="row_sealed") as err:
        await _run(app_pool, ids["tenant"], f"UPDATE crm.{table} SET {change} WHERE {id_col} = $1", ids[key])
    assert err.value.sqlstate == "CRM31"


async def test_a_paid_row_and_a_signed_signature_are_sealed(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    await _pay_deposit(app_pool, ids)
    with pytest.raises(asyncpg.PostgresError, match="row_sealed"):
        await _run(
            app_pool, ids["tenant"], "UPDATE crm.payment_schedule SET amount = 1 WHERE schedule_id = $1", ids["deposit"]
        )
    # A draft schedule row (due) may still move its date; a paid one keeps it.
    await _run(
        app_pool,
        ids["tenant"],
        "UPDATE crm.payment_schedule SET due_on = due_on + 1 WHERE schedule_id = $1",
        ids["balance"],
    )
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "member:test")
        otp = await _otp(conn, ids, purpose="sign")
        await conn.execute(
            "UPDATE crm.otp_challenge SET status = 'verified', verified_at = now(), consumed_at = now() "
            "WHERE otp_id = $1",
            otp,
        )
        request = await conn.fetchval(
            "INSERT INTO crm.signature_request (trip_id, traveler_id, access_id, doc_id) VALUES ($1, $2, $3, $4) "
            "RETURNING request_id",
            ids["trip"],
            ids["lead"],
            ids["access_lead"],
            ids["waiver"],
        )
        await conn.execute(
            "UPDATE crm.signature_request SET status = 'signed', signed_at = now(), signed_name_enc = $2, "
            "doc_sha256 = $3, evidence_sha256 = $4, otp_id = $5 WHERE request_id = $1",
            request,
            _blob(),
            _hash(),
            _hash(),
            otp,
        )
    with pytest.raises(asyncpg.PostgresError, match="row_sealed"):
        await _run(
            app_pool,
            ids["tenant"],
            "UPDATE crm.signature_request SET doc_sha256 = $2 WHERE request_id = $1",
            request,
            _hash(),
        )
    # The 24-hour purge of the code row keeps the signature (otp_id SET NULL is not a sealed column).
    await _run(app_pool, ids["tenant"], "DELETE FROM crm.otp_challenge WHERE otp_id = $1", otp)
    assert (
        await _run(app_pool, ids["tenant"], "SELECT otp_id FROM crm.signature_request WHERE request_id = $1", request)
        is None
    )


async def test_the_agreement_hash_is_the_databases(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    doc = await _run(
        app_pool,
        ids["tenant"],
        "INSERT INTO crm.agreement_doc (kind, version_label, body_text, body_sha256) "
        "VALUES ('waiver', 'v2', 'Ngày mai đi chơi.', $1) RETURNING doc_id",
        b"\x00" * 32,
    )
    stored = await _run(app_pool, ids["tenant"], "SELECT body_sha256 FROM crm.agreement_doc WHERE doc_id = $1", doc)
    assert bytes(stored) == hashlib.sha256("Ngày mai đi chơi.".encode()).digest()
    # Two published versions of one kind: refused.
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(
            app_pool,
            ids["tenant"],
            "UPDATE crm.agreement_doc SET status = 'published', published_at = now() WHERE doc_id = $1",
            doc,
        )


async def test_payments_are_append_only_for_crm_app(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    payment = await _pay_deposit(app_pool, ids)
    for sql in (
        "UPDATE crm.payment SET amount = 1 WHERE payment_id = $1",
        "DELETE FROM crm.payment WHERE payment_id = $1",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await _run(app_pool, ids["tenant"], sql, payment)
    # One payment per schedule row.
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(
            app_pool,
            ids["tenant"],
            "INSERT INTO crm.payment (trip_id, schedule_id, amount, currency, method, received_on) "
            "VALUES ($1, $2, 300, 'USD', 'cash', current_date)",
            ids["trip"],
            ids["deposit"],
        )


@pytest.mark.parametrize(
    "sql",
    [
        # deposit above the total
        "INSERT INTO crm.proposal (trip_id, version, body_enc, currency, total_amount, deposit_amount, balance_due_on) "
        "VALUES ($1, 9, $2, 'USD', 100, 200, current_date)",
        # a balance left without its date
        "INSERT INTO crm.proposal (trip_id, version, body_enc, currency, total_amount, deposit_amount) "
        "VALUES ($1, 9, $2, 'USD', 100, 50)",
        # paid in full with a balance date
        "INSERT INTO crm.proposal (trip_id, version, body_enc, currency, total_amount, deposit_amount, balance_due_on) "
        "VALUES ($1, 9, $2, 'USD', 100, 100, current_date)",
        # a bad currency
        "INSERT INTO crm.proposal (trip_id, version, body_enc, currency, total_amount, deposit_amount) "
        "VALUES ($1, 9, $2, 'usd', 100, 100)",
    ],
)
async def test_proposal_amounts_keep_their_shape(app_pool: asyncpg.Pool, sql: str) -> None:
    ids = await _lifecycle(app_pool)
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, ids["tenant"], sql, ids["trip"], _blob())


async def test_one_accepted_and_one_sent_version_per_trip(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    second = await _run(
        app_pool,
        ids["tenant"],
        "INSERT INTO crm.proposal (trip_id, version, body_enc, currency, total_amount, deposit_amount) "
        "VALUES ($1, 2, $2, 'USD', 500, 500) RETURNING proposal_id",
        ids["trip"],
        _blob(),
    )
    await _run(
        app_pool,
        ids["tenant"],
        "UPDATE crm.proposal SET status = 'sent', price_hash = $2, sent_at = now() WHERE proposal_id = $1",
        second,
        _hash(),
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(
            app_pool,
            ids["tenant"],
            "UPDATE crm.proposal SET status = 'accepted', accepted_at = now(), accepted_name_enc = $2, "
            "accepted_price_hash = price_hash WHERE proposal_id = $1",
            second,
            _blob(),
        )
    # Acceptance must pin the hash that was sent.
    third = await _run(
        app_pool,
        ids["tenant"],
        "INSERT INTO crm.proposal (trip_id, version, body_enc, currency, total_amount, deposit_amount) "
        "VALUES ($1, 3, $2, 'USD', 500, 500) RETURNING proposal_id",
        ids["trip"],
        _blob(),
    )
    with pytest.raises(asyncpg.UniqueViolationError):  # a second sent version
        await _run(
            app_pool,
            ids["tenant"],
            "UPDATE crm.proposal SET status = 'sent', price_hash = $2, sent_at = now() WHERE proposal_id = $1",
            third,
            _hash(),
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(
            app_pool,
            ids["tenant"],
            "UPDATE crm.proposal SET status = 'withdrawn', price_hash = $2 WHERE proposal_id = $1",
            third,
            _hash(),
        )


async def test_codes_live_ten_minutes_and_sessions_twelve_hours(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "member:test")
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await _otp(conn, ids, ttl="11 minutes")
        otp = await _otp(conn, ids)
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE crm.otp_challenge SET status = 'verified', verified_at = now(), session_hash = $2, "
                    "session_expires_at = now() + interval '13 hours' WHERE otp_id = $1",
                    otp,
                    _hash(),
                )
        with pytest.raises(asyncpg.CheckViolationError):  # six attempts
            async with conn.transaction():
                await conn.execute("UPDATE crm.otp_challenge SET attempts = 6 WHERE otp_id = $1", otp)
        # The code itself waits (sealed) only while it is pending: leaving pending clears it.
        await conn.execute("UPDATE crm.otp_challenge SET code_enc = $2 WHERE otp_id = $1", otp, _blob())
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE crm.otp_challenge SET status = 'verified', verified_at = now(), session_hash = $2, "
                    "session_expires_at = now() + interval '12 hours' WHERE otp_id = $1",
                    otp,
                    _hash(),
                )
        with pytest.raises(asyncpg.CheckViolationError):  # a delivery code outside the list
            async with conn.transaction():
                await conn.execute("UPDATE crm.otp_challenge SET delivery = 'sms' WHERE otp_id = $1", otp)
        await conn.execute(
            "UPDATE crm.otp_challenge SET status = 'verified', verified_at = now(), session_hash = $2, "
            "session_expires_at = now() + interval '12 hours', code_enc = NULL WHERE otp_id = $1",
            otp,
            _hash(),
        )


async def test_link_purposes_name_a_traveller(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(
            app_pool,
            ids["tenant"],
            "INSERT INTO crm.client_link (trip_id, purpose, token_hash, expires_at) "
            "VALUES ($1, 'portal', $2, now() + interval '1 day')",
            ids["trip"],
            _hash(),
        )


async def test_portal_access_needs_a_traveller_of_the_trip(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    other = await _agency(app_pool)
    await _run(
        app_pool,
        ids["tenant"],
        "UPDATE crm.traveler_access SET status = 'revoked' WHERE access_id = $1",
        ids["access_lead"],
    )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _run(
            app_pool,
            ids["tenant"],
            "INSERT INTO crm.traveler_access (trip_id, traveler_id, role) VALUES ($1, $2, 'lead')",
            ids["trip"],
            other["lead"],
        )


async def _all_rows(pool: asyncpg.Pool, ids: dict[str, Any]) -> None:
    """Fill every new table for the agency's trip: deposit paid, codes, a signed and a pending signature."""
    await _pay_deposit(pool, ids)
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "member:test")
        otp = await _otp(conn, ids, purpose="sign")
        await conn.execute(
            "UPDATE crm.otp_challenge SET status = 'verified', verified_at = now(), consumed_at = now() "
            "WHERE otp_id = $1",
            otp,
        )
        await _otp(conn, ids, "companion")
        await conn.execute(
            "INSERT INTO crm.signature_request (trip_id, traveler_id, access_id, doc_id, status, signed_at, "
            "signed_name_enc, doc_sha256, evidence_sha256, otp_id) VALUES ($1, $2, $3, $4, 'signed', now(), $5, $6, "
            "$7, $8)",
            ids["trip"],
            ids["lead"],
            ids["access_lead"],
            ids["waiver"],
            _blob(),
            _hash(),
            _hash(),
            otp,
        )
        await conn.execute(
            "INSERT INTO crm.signature_request (trip_id, traveler_id, access_id, doc_id) VALUES ($1, $2, $3, $4)",
            ids["trip"],
            ids["companion"],
            ids["access_companion"],
            ids["waiver"],
        )


async def test_the_trip_erase_takes_the_lifecycle_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    await _all_rows(app_pool, ids)
    done = await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    for table in ("signature_request", "otp_challenge", "traveler_access", "payment", "payment_schedule", "proposal"):
        assert done["counts"][table] >= 1, table
        assert await _count(app_pool, ids["tenant"], table) == 0, table
    assert await _count(app_pool, ids["tenant"], "agreement_doc") == 2  # the agency's terms stay


async def test_the_traveller_erase_takes_their_portal_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    await _all_rows(app_pool, ids)
    # The companion leaves the trip first (traveler_on_trip otherwise): the trip goes to the trash.
    await _run(
        app_pool,
        ids["tenant"],
        "UPDATE crm.trip SET deleted_at = now(), deleted_by = 'member:test', purge_after = now() WHERE trip_id = $1",
        ids["trip"],
    )
    done = await _erase(app_pool, ids["tenant"], "traveler", ids["lead"], ids["token"])
    assert done["counts"]["signature_request"] == 1
    assert done["counts"]["traveler_access"] == 1
    # The proposal's link to the lead is gone; the proposal (the trip's) stays until the trip is erased, but the name
    # the lead typed to accept it is erased with them (review S251); the acceptance time and hash stay.
    assert done["counts"]["proposal_acceptance_name_erased"] == 1
    row = await _run(
        app_pool, ids["tenant"],
        "SELECT row(link_id IS NULL, accepted_name_enc IS NULL, accepted_name_erased_at IS NOT NULL, "
        "accepted_at IS NOT NULL, status) FROM crm.proposal WHERE proposal_id = $1",
        ids["proposal"],
    )  # fmt: skip
    assert tuple(row) == (True, True, True, True, "accepted")
    assert await _count(app_pool, ids["tenant"], "traveler_access") == 1  # the companion's


async def test_a_trip_hold_reaches_the_portal_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    await _hold(app_pool, ids["tenant"], "trip", ids["trip"], "member:m")
    for sql in (
        "UPDATE crm.traveler_access SET trip_id = trip_id, deleted_at = now(), deleted_by = 'm', purge_after = now() "
        "WHERE access_id = $1",
    ):
        with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
            await _run(app_pool, ids["tenant"], sql, ids["access_lead"])
        assert err.value.sqlstate == "CRM10"
    with pytest.raises(asyncpg.PostgresError, match="legal_hold"):
        await _erase(app_pool, ids["tenant"], "traveler", ids["companion"], ids["token2"], dry_run=True)


async def test_the_agency_erase_leaves_nothing(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    await _all_rows(app_pool, ids)
    await _run(app_pool, ids["tenant"], "UPDATE crm.tenant SET status = 'offboarding'")
    done = await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert done["counts"]["agreement_doc"] == 2
    for table in NEW_TABLES:
        assert await _count(app_pool, ids["tenant"], table) == 0, table


async def test_the_erase_text_names_the_new_tables(admin: asyncpg.Connection) -> None:
    """A later key that replaces crm.erase_subject must start from this text."""
    source = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    held = await admin.fetchval("SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_held(text,uuid,uuid)'::regprocedure")
    for table in NEW_TABLES:
        assert f"'{table}'" in source, table
    for table in ("traveler_access", "signature_request", "otp_challenge"):
        assert f"crm.{table}" in held, table
    assert "accepted_name_erased_at" in source
    guards = {
        r["relname"]
        for r in await admin.fetch(
            "SELECT c.relname FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid "
            "WHERE g.tgfoid = 'crm.guard_held_change()'::regprocedure"
        )
    }
    assert {"agreement_doc", "proposal", "payment_schedule", "traveler_access", "signature_request"} <= guards
    assert json.dumps(sorted(guards))  # the set is printable (debug aid on failure)
