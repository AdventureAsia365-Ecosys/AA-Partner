"""crm-021 (AA-950, card CRM-21b) on PostgreSQL with the RDS role shape: the client side of the proposal.

- ``crm.proposal``: viewed counters; the hold set of a builder version (``hold_set_ref`` = the Booking proposal_ref,
  the five hold columns together, at most 3 re-checks a day: the database's backstop of card R); the builder
  acceptance (picks hash and amounts) sealed with the acceptance (CRM31); a text version takes none of it.
- ``crm.metric_event`` (the agency bell): crm_app inserts and reads, sets ``seen_at`` once and nothing else, never
  deletes; FORCE RLS: another agency sees nothing and cannot hang an event on a foreign trip.
- Erasure: the trip and agency erase take the events; a trip hold guards them (CRM10).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase, _hold
from tests.integration.test_crm019_schema import _builder, _publish, _run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "021_crm_client_proposal.sql"


async def test_crm021_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    # The crm-019 tests re-apply their own erase_subject text: put this key's back first.
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-021'") == 1
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    src = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    assert "'metric_event'" in src


async def test_crm_app_inserts_reads_and_marks_seen_only(admin: asyncpg.Connection) -> None:
    for privilege, want in (("SELECT", True), ("INSERT", True), ("UPDATE", False), ("DELETE", False),
                            ("TRUNCATE", False)):  # fmt: skip
        assert await admin.fetchval("SELECT has_table_privilege('crm_app', 'crm.metric_event', $1)", privilege) is want
    assert await admin.fetchval("SELECT has_column_privilege('crm_app', 'crm.metric_event', 'seen_at', 'UPDATE')")
    assert not await admin.fetchval("SELECT has_column_privilege('crm_app', 'crm.metric_event', 'name', 'UPDATE')")
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, 'crm.metric_event', $2)", reader, privilege)


async def _sent(pool: asyncpg.Pool, ids: dict[str, Any]) -> dict[str, Any]:
    b = await _builder(pool, ids)
    await _publish(pool, ids, b["proposal"])
    await _run(pool, ids["tenant"], "UPDATE crm.proposal SET status = 'sent', sent_at = now() WHERE proposal_id = $1",
               b["proposal"])  # fmt: skip
    return b


HOLD_SQL = (
    "UPDATE crm.proposal SET hold_set_ref = 'crm-proposal:' || proposal_id::text, hold_id = 'h1', "
    "hold_quote_id = 'q1', hold_expires_at = now() + interval '2 days', hold_selection_sha256 = $2, "
    "hold_placed_at = now(), hold_count = hold_count + 1 WHERE proposal_id = $1"
)


async def test_the_hold_set_of_a_version(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _sent(app_pool, ids)
    p = b["proposal"]
    await _run(app_pool, ids["tenant"], HOLD_SQL, p, os.urandom(32))
    # Re-checks: at most 3 a day (the API refuses the 4th before Booking; this is the backstop).
    await _run(app_pool, ids["tenant"], "UPDATE crm.proposal SET recheck_day = CURRENT_DATE, recheck_count_day = 3 "
               "WHERE proposal_id = $1", p)  # fmt: skip
    for sql in (
        "UPDATE crm.proposal SET recheck_count_day = 4 WHERE proposal_id = $1",
        "UPDATE crm.proposal SET hold_set_ref = 'crm-proposal:other' WHERE proposal_id = $1",
        "UPDATE crm.proposal SET hold_expires_at = NULL WHERE proposal_id = $1",  # the five hold columns go together
        "UPDATE crm.proposal SET hold_id = 'bad id' WHERE proposal_id = $1",
        "UPDATE crm.proposal SET view_count = 1 WHERE proposal_id = $1",  # viewed counters go together
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, ids["tenant"], sql, p)
    await _run(app_pool, ids["tenant"], "UPDATE crm.proposal SET view_count = 2, first_viewed_at = now(), "
               "last_viewed_at = now() WHERE proposal_id = $1", p)  # fmt: skip
    # The hold request in flight: key + start together, then the quote it presents (all three); the hold columns
    # may all go back to NULL (a released hold whose replacement failed).
    key = f"crm-proposal:{p}:v1:a1"
    await _run(app_pool, ids["tenant"], "UPDATE crm.proposal SET hold_attempt = 1, hold_pending_key = $2, "
               "hold_pending_at = now() WHERE proposal_id = $1", p, key)  # fmt: skip
    await _run(app_pool, ids["tenant"], "UPDATE crm.proposal SET hold_pending_quote_id = 'q2', "
               "hold_pending_price_hash = $2, hold_pending_selection_sha256 = $3 WHERE proposal_id = $1", p, "b" * 64,
               os.urandom(32))  # fmt: skip
    for sql in (
        "UPDATE crm.proposal SET hold_pending_at = NULL WHERE proposal_id = $1",
        "UPDATE crm.proposal SET hold_pending_price_hash = NULL WHERE proposal_id = $1",
        "UPDATE crm.proposal SET hold_pending_key = 'crm-proposal:x:v1:a1' WHERE proposal_id = $1",
        "UPDATE crm.proposal SET hold_attempt = 0 WHERE proposal_id = $1",
        "UPDATE crm.proposal SET hold_pending_key = NULL, hold_pending_at = NULL WHERE proposal_id = $1",
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, ids["tenant"], sql, p)
    await _run(app_pool, ids["tenant"], "UPDATE crm.proposal SET hold_pending_key = NULL, hold_pending_at = NULL, "
               "hold_pending_quote_id = NULL, hold_pending_price_hash = NULL, hold_pending_selection_sha256 = NULL, "
               "hold_id = NULL, hold_quote_id = NULL, hold_expires_at = NULL, hold_selection_sha256 = NULL, "
               "hold_placed_at = NULL WHERE proposal_id = $1", p)  # fmt: skip
    # A text version holds nothing.
    text = uuid.uuid4()
    await _run(app_pool, ids["tenant"], "INSERT INTO crm.proposal (proposal_id, trip_id, version, body_enc, currency, "
               "total_amount, deposit_amount) VALUES ($1, $2, 7, $3, 'USD', 100, 100)", text, ids["trip"],
               os.urandom(40))  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, ids["tenant"], HOLD_SQL, text, os.urandom(32))


async def test_the_builder_acceptance_is_sealed(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _sent(app_pool, ids)
    p = b["proposal"]
    accept = (
        "UPDATE crm.proposal SET status = 'accepted', accepted_at = now(), accepted_name_enc = $2, "
        "accepted_price_hash = price_hash, accepted_selection_sha256 = $3, accepted_total_amount = $4, "
        "accepted_deposit_amount = $5 WHERE proposal_id = $1"
    )
    # A builder acceptance carries the picks and their amounts.
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, ids["tenant"], accept, p, os.urandom(40), None, None, None)
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, ids["tenant"], accept, p, os.urandom(40), os.urandom(32), 100, 120)
    await _run(app_pool, ids["tenant"], accept, p, os.urandom(40), os.urandom(32), 320, 96)
    for sql in (
        "UPDATE crm.proposal SET accepted_total_amount = 1 WHERE proposal_id = $1",
        "UPDATE crm.proposal SET accepted_selection_sha256 = NULL WHERE proposal_id = $1",
        "UPDATE crm.proposal SET accepted_hold_id = 'h9' WHERE proposal_id = $1",
    ):
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, ids["tenant"], sql, p)
        assert err.value.sqlstate in ("CRM31", "23514"), sql


async def _event(pool: asyncpg.Pool, ids: dict[str, Any], proposal: uuid.UUID | None, **kw: Any) -> uuid.UUID:
    return await _run(
        pool,
        ids["tenant"],
        "INSERT INTO crm.metric_event (trip_id, proposal_id, name, detail, bell_member_id) "
        "VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING metric_event_id",
        kw.get("trip", ids["trip"]),
        proposal,
        kw.get("name", "proposal.rehold"),
        kw.get("detail", '{"hold_count": 2}'),
        kw.get("member", ids["member"]),
    )


async def test_metric_events_are_append_only_except_seen_at(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _sent(app_pool, ids)
    e = await _event(app_pool, ids, b["proposal"])
    await _run(app_pool, ids["tenant"], "UPDATE crm.metric_event SET seen_at = now() WHERE metric_event_id = $1", e)
    with pytest.raises(asyncpg.PostgresError) as err:  # seen once
        await _run(app_pool, ids["tenant"], "UPDATE crm.metric_event SET seen_at = now() + interval '1 hour' "
                   "WHERE metric_event_id = $1", e)  # fmt: skip
    assert err.value.sqlstate == "CRM31"
    for sql in (
        "UPDATE crm.metric_event SET detail = '{}' WHERE metric_event_id = $1",
        "DELETE FROM crm.metric_event WHERE metric_event_id = $1",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await _run(app_pool, ids["tenant"], sql, e)
    for bad in ({"name": "Proposal viewed"}, {"name": "proposal"}, {"detail": "[1]"},
                {"detail": '{"x": "' + "a" * 4100 + '"}'}):  # fmt: skip
        with pytest.raises(asyncpg.CheckViolationError):
            await _event(app_pool, ids, b["proposal"], **bad)
    # seen_at needs a bell member.
    plain = await _event(app_pool, ids, None, name="proposal.viewed", member=None)
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, ids["tenant"], "UPDATE crm.metric_event SET seen_at = now() WHERE metric_event_id = $1",
                   plain)  # fmt: skip


async def test_another_agency_sees_no_event_and_cannot_attach_one(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    await _event(app_pool, ids, None)
    assert await _count(app_pool, ids["tenant"], "metric_event") == 1
    assert await _count(app_pool, other["tenant"], "metric_event") == 0
    with pytest.raises(asyncpg.ForeignKeyViolationError):  # agency A's trip from agency B
        await _event(app_pool, other, None, trip=ids["trip"], member=other["member"])
    with pytest.raises(asyncpg.ForeignKeyViolationError):  # agency A's member as B's bell
        await _event(app_pool, other, None, member=ids["member"])


async def test_the_trip_and_agency_erase_take_the_events(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _sent(app_pool, ids)
    await _event(app_pool, ids, b["proposal"])
    await _event(app_pool, ids, None, name="proposal.viewed", member=None)
    done = await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert done["counts"]["metric_event"] == 2 and await _count(app_pool, ids["tenant"], "metric_event") == 0
    again = await _agency(app_pool)
    b = await _sent(app_pool, again)
    await _event(app_pool, again, b["proposal"])
    await _run(app_pool, again["tenant"], "UPDATE crm.tenant SET status = 'offboarding'", actor="seed:test")
    done = await _erase(app_pool, again["tenant"], "tenant", again["tenant"])
    assert done["counts"]["metric_event"] == 1


async def test_a_trip_hold_guards_the_events(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    e = await _event(app_pool, ids, None)
    await _hold(app_pool, ids["tenant"], "trip", ids["trip"], "member:m")
    # Marking it seen is no change a hold protects; the erase is refused.
    await _run(app_pool, ids["tenant"], "UPDATE crm.metric_event SET seen_at = now() WHERE metric_event_id = $1", e)
    with pytest.raises(asyncpg.PostgresError) as err:
        await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert err.value.sqlstate == "CRM10"
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        assert await conn.fetchval("SELECT count(*) FROM crm.metric_event") == 1
