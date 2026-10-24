"""crm-015 (AA-945, card CRM-17) on PostgreSQL 15 with the RDS role shape: Module 5 e-mail.

- The forward-in lookup: ``ingest_address_lookup`` shows crm_app the one active address of ``app.ingest_local_part``
  and nothing else (no tenant set, no other agency's row, no inactive address).
- Mailboxes per member (design §4.6): ``crm.integration_credential`` is unique per (agency, provider, member), NULLS
  NOT DISTINCT; a mailbox uses its own member's credential of its provider (CRM30); disconnect deletes the credential
  and keeps the mailbox row.
- Messages: outbound statuses and moves (draft -> queued -> sent | held_reconnect | failed, Undo back to draft), the
  sender kinds of the BYO errata (no ``platform``), crm_app discards drafts only (CRM28) and never edits a message that
  is not a draft (CRM29), one row per Message-ID per agency.
- ``crm.trip.mail_token`` (card E) and ``crm.dispatch_queue.message_id`` + the mail gate codes (card R
  ``held_reconnect``).
- Erasure and holds: trip, traveller, conversation and agency scopes take the mail rows (no CRM16); a held trip keeps
  its conversations and messages (CRM10).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import ISS, _agency, _count, _ctx, _erase, _hold

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "015_crm_mail.sql"
MAIL_TABLES = ("message", "conversation", "saved_reply", "mail_import", "ingest_address", "channel_account")


def _blob() -> bytes:
    return os.urandom(40)  # shaped like a sealed value (>= 30 bytes); never opened here


def _bidx() -> bytes:
    return os.urandom(16)


def _hash(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


@asynccontextmanager
async def _as(pool: asyncpg.Pool, tenant: uuid.UUID, actor: str = "seed:test") -> AsyncIterator[asyncpg.Connection]:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        yield conn


async def _sqlstate(coro: Any) -> str:
    with pytest.raises(asyncpg.PostgresError) as err:
        await coro
    return str(err.value.sqlstate)


async def _mail(pool: asyncpg.Pool) -> dict[str, Any]:
    """An agency (_agency) with a member mailbox, a forward-in address, a trip conversation with one inbound and one
    outbound message, an Unmatched conversation, a saved reply and an import."""
    ids = await _agency(pool)
    async with _as(pool, ids["tenant"]) as conn:
        ids["credential"] = await conn.fetchval(
            "INSERT INTO crm.integration_credential (provider, secret_enc, member_id, status) "
            "VALUES ('google', $1, $2, 'connected') RETURNING integration_credential_id",
            _blob(),
            ids["member"],
        )
        ids["mailbox"] = await conn.fetchval(
            "INSERT INTO crm.channel_account (member_id, kind, address_enc, address_bidx, credential_id) "
            "VALUES ($1, 'gmail', $2, $3, $4) RETURNING channel_account_id",
            ids["member"],
            _blob(),
            _bidx(),
            ids["credential"],
        )
        ids["local_part"] = f"sunshine-{uuid.uuid4().hex[:10]}"
        ids["ingest"] = await conn.fetchval(
            "INSERT INTO crm.ingest_address (local_part) VALUES ($1) RETURNING ingest_address_id", ids["local_part"]
        )
        ids["conversation"] = await conn.fetchval(
            "INSERT INTO crm.conversation (traveler_id, trip_id) VALUES ($1, $2) RETURNING conversation_id",
            ids["lead"],
            ids["trip"],
        )
        ids["inbound"] = await conn.fetchval(
            "INSERT INTO crm.message (conversation_id, direction, status, subject_enc, body_enc, from_enc, from_bidx, "
            "rfc_message_id_hash, match_method) VALUES ($1, 'in', 'received', $2, $3, $4, $5, $6, 'trip_token') "
            "RETURNING message_id",
            ids["conversation"],
            _blob(),
            _blob(),
            _blob(),
            _bidx(),
            _hash(f"<{uuid.uuid4()}@mail.example>"),
        )
        ids["draft"] = await conn.fetchval(
            "INSERT INTO crm.message (conversation_id, direction, sender_kind, channel_account_id, author_member_id, "
            "subject_enc, body_enc) VALUES ($1, 'out', 'agency_mailbox', $2, $3, $4, $5) RETURNING message_id",
            ids["conversation"],
            ids["mailbox"],
            ids["member"],
            _blob(),
            _blob(),
        )
        ids["unmatched"] = await conn.fetchval("INSERT INTO crm.conversation DEFAULT VALUES RETURNING conversation_id")
        ids["unmatched_msg"] = await conn.fetchval(
            "INSERT INTO crm.message (conversation_id, direction, status, body_enc, match_method) "
            "VALUES ($1, 'in', 'received', $2, 'unmatched') RETURNING message_id",
            ids["unmatched"],
            _blob(),
        )
        await conn.execute(
            "INSERT INTO crm.saved_reply (member_id, title, body_enc) VALUES ($1, 'Visa basics', $2)",
            ids["member"],
            _blob(),
        )
        await conn.execute(
            "INSERT INTO crm.mail_import (member_id, source_kind, size_bytes) VALUES ($1, 'mbox', 2048)", ids["member"]
        )
    return ids


async def test_crm015_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    # Runs first in this file: the crm-012 / crm-013 / crm-014 tests re-apply their own erase_subject text before it.
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-015'") == 1
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    source = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    for table in (*MAIL_TABLES, "booking_line_money", "agency_supplier", "membership_inbox", "client_link"):
        assert f"'{table}'" in source, table


async def test_no_reader_reaches_the_mail_tables(admin: asyncpg.Connection) -> None:
    for table in MAIL_TABLES:
        for reader in READERS:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert not await admin.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege
                ), (reader, table, privilege)
    for table in ("channel_account", "ingest_address", "conversation", "mail_import"):
        assert not await admin.fetchval("SELECT has_table_privilege('crm_app', $1, 'DELETE')", f"crm.{table}")


async def test_ingest_lookup_sees_only_the_named_active_address(app_pool: asyncpg.Pool) -> None:
    ids = await _mail(app_pool)
    other = await _mail(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        # No tenant and no local part: nothing.
        assert await conn.fetchval("SELECT count(*) FROM crm.ingest_address") == 0
        await conn.execute("SELECT set_config('app.ingest_local_part', $1, true)", ids["local_part"])
        rows = await conn.fetch("SELECT tenant_id, ingest_address_id FROM crm.ingest_address")
        assert [(r["tenant_id"], r["ingest_address_id"]) for r in rows] == [(ids["tenant"], ids["ingest"])]
        # The lookup is SELECT only: no write without the tenant context.
        assert await conn.execute("UPDATE crm.ingest_address SET active = false") == "UPDATE 0"
    async with _as(app_pool, ids["tenant"]) as conn:
        await conn.execute("UPDATE crm.ingest_address SET active = false WHERE ingest_address_id = $1", ids["ingest"])
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.ingest_local_part', $1, true)", ids["local_part"])
        assert await conn.fetchval("SELECT count(*) FROM crm.ingest_address") == 0  # inactive: unknown
        await conn.execute("SELECT set_config('app.ingest_local_part', $1, true)", other["local_part"])
        assert await conn.fetchval("SELECT tenant_id FROM crm.ingest_address") == other["tenant"]
    # Local parts are unique across agencies; one active address per agency.
    async with _as(app_pool, ids["tenant"]) as conn:
        with pytest.raises(asyncpg.UniqueViolationError):
            async with conn.transaction():
                await conn.execute("INSERT INTO crm.ingest_address (local_part) VALUES ($1)", other["local_part"])
        await conn.execute("INSERT INTO crm.ingest_address (local_part) VALUES ($1)", f"new-{uuid.uuid4().hex[:8]}")
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute("INSERT INTO crm.ingest_address (local_part) VALUES ($1)", f"two-{uuid.uuid4().hex[:8]}")
    for bad in ("UPPER-case-x", "ab", "-starts-dash", "has_underscore1"):
        async with _as(app_pool, other["tenant"]) as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("INSERT INTO crm.ingest_address (local_part, active) VALUES ($1, false)", bad)


async def test_credentials_and_mailboxes_are_per_member(app_pool: asyncpg.Pool) -> None:
    ids = await _mail(app_pool)
    async with _as(app_pool, ids["tenant"]) as conn:
        agent = await conn.fetchval(
            "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, 'agent', 'active') "
            "RETURNING member_id",
            ISS,
            f"sub-{uuid.uuid4()}",
        )
        insert = "INSERT INTO crm.integration_credential (provider, secret_enc, member_id) VALUES ($1, $2, $3)"
        await conn.execute(insert, "google", _blob(), agent)  # the agent's own Gmail beside the owner's
        await conn.execute(insert, "google", _blob(), None)  # and one agency-level
        await conn.execute(insert, "smtp", _blob(), None)
    for member in ("agent", None):
        async with _as(app_pool, ids["tenant"]) as conn:
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(insert, "google", _blob(), agent if member else None)
    async with _as(app_pool, ids["tenant"]) as conn:
        smtp = await conn.fetchval(
            "SELECT integration_credential_id FROM crm.integration_credential WHERE provider = 'smtp'"
        )
    mailbox = (
        "INSERT INTO crm.channel_account (member_id, kind, address_enc, address_bidx, credential_id) "
        "VALUES ($1, $2, $3, $4, $5)"
    )
    # CRM30: a Gmail mailbox with an SMTP credential; the agent's mailbox with the owner's credential.
    assert await _sqlstate(_run(app_pool, ids["tenant"], mailbox, agent, "gmail", _blob(), _bidx(), smtp)) == "CRM30"
    assert (
        await _sqlstate(_run(app_pool, ids["tenant"], mailbox, agent, "gmail", _blob(), _bidx(), ids["credential"]))
        == "CRM30"
    )
    await _run(app_pool, ids["tenant"], mailbox, None, "smtp", _blob(), _bidx(), smtp)  # agency-level SMTP
    # One mailbox per member (and one agency-level).
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(app_pool, ids["tenant"], mailbox, None, "smtp", _blob(), _bidx(), smtp)
    # Connected needs a credential.
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(
            app_pool,
            ids["tenant"],
            "UPDATE crm.channel_account SET credential_id = NULL WHERE channel_account_id = $1",
            ids["mailbox"],
        )
    # Disconnect: status first, then the credential row goes; the mailbox stays as history.
    async with _as(app_pool, ids["tenant"]) as conn:
        await conn.execute(
            "UPDATE crm.channel_account SET status = 'disconnected' WHERE channel_account_id = $1", ids["mailbox"]
        )
        await conn.execute(
            "DELETE FROM crm.integration_credential WHERE integration_credential_id = $1", ids["credential"]
        )
        row = await conn.fetchrow(
            "SELECT status, credential_id FROM crm.channel_account WHERE channel_account_id = $1", ids["mailbox"]
        )
    assert dict(row) == {"status": "disconnected", "credential_id": None}


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any) -> str:
    async with _as(pool, tenant) as conn:
        return str(await conn.execute(sql, *args))


async def test_message_statuses_sender_kinds_and_seal(app_pool: asyncpg.Pool) -> None:
    ids = await _mail(app_pool)
    tenant = ids["tenant"]
    move = "UPDATE crm.message SET status = $2 WHERE message_id = $1"
    await _run(app_pool, tenant, move, ids["draft"], "queued")
    await _run(app_pool, tenant, move, ids["draft"], "draft_autosaved")  # Undo gives the text back
    await _run(app_pool, tenant, move, ids["draft"], "queued")
    await _run(
        app_pool,
        tenant,
        "UPDATE crm.message SET status = 'held_reconnect', failure_code = 'auth_revoked' WHERE message_id = $1",
        ids["draft"],
    )
    await _run(
        app_pool,
        tenant,
        "UPDATE crm.message SET status = 'queued', failure_code = NULL WHERE message_id = $1",
        ids["draft"],
    )
    await _run(
        app_pool,
        tenant,
        "UPDATE crm.message SET status = 'sent', sent_at = now(), rfc_message_id_hash = $2, external_id = 'gm-1' "
        "WHERE message_id = $1",
        ids["draft"],
        _hash(f"<{uuid.uuid4()}@crm.example>"),
    )
    # A sent message has no way back, keeps its content and is not deleted by crm_app.
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, tenant, move, ids["draft"], "draft_autosaved")
    seal = "UPDATE crm.message SET body_enc = $2 WHERE message_id = $1"
    assert await _sqlstate(_run(app_pool, tenant, seal, ids["draft"], _blob())) == "CRM29"
    assert await _sqlstate(_run(app_pool, tenant, seal, ids["inbound"], _blob())) == "CRM29"
    delete = "DELETE FROM crm.message WHERE message_id = $1"
    assert await _sqlstate(_run(app_pool, tenant, delete, ids["inbound"])) == "CRM28"
    assert await _sqlstate(_run(app_pool, tenant, delete, ids["draft"])) == "CRM28"
    # A draft is edited and discarded freely.
    async with _as(app_pool, tenant) as conn:
        draft = await conn.fetchval(
            "INSERT INTO crm.message (conversation_id, direction, sender_kind, body_enc) "
            "VALUES ($1, 'out', 'agency_mailbox', $2) RETURNING message_id",
            ids["conversation"],
            _blob(),
        )
        await conn.execute(seal, draft, _blob())
        assert await conn.execute(delete, draft) == "DELETE 1"
    insert = "INSERT INTO crm.message (conversation_id, direction, status, sender_kind) VALUES ($1, $2, $3, $4)"
    for direction, status, kind in (
        ("out", "draft_autosaved", "platform"),  # errata S247: no platform sender
        ("out", "draft_autosaved", None),
        ("in", "received", "agency_mailbox"),
        ("in", "draft_autosaved", None),
        ("out", "received", "agency_mailbox"),
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, tenant, insert, ids["conversation"], direction, status, kind)
    # One row per Message-ID per agency (BCC + forward, a re-import).
    rfc = _hash("<same@mail.example>")
    one = (
        "INSERT INTO crm.message (conversation_id, direction, status, rfc_message_id_hash) "
        "VALUES ($1, 'in', 'received', $2)"
    )
    await _run(app_pool, tenant, one, ids["conversation"], rfc)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(app_pool, tenant, one, ids["unmatched"], rfc)
    other = await _mail(app_pool)
    await _run(app_pool, other["tenant"], one, other["conversation"], rfc)  # another agency: its own row


async def test_trip_mail_token_and_dispatch_message(app_pool: asyncpg.Pool) -> None:
    ids = await _mail(app_pool)
    async with _as(app_pool, ids["tenant"]) as conn:
        tokens = [
            await conn.fetchval("INSERT INTO crm.trip (title) VALUES ('Laos 7 nights') RETURNING mail_token")
            for _ in range(3)
        ]
        tokens.append(await conn.fetchval("SELECT mail_token FROM crm.trip WHERE trip_id = $1", ids["trip"]))
        assert len(set(tokens)) == 4 and all(len(t) == 12 and t.isalnum() for t in tokens)
        with pytest.raises(asyncpg.UniqueViolationError):
            async with conn.transaction():
                await conn.execute("UPDATE crm.trip SET mail_token = $1 WHERE trip_id = $2", tokens[0], ids["trip"])
        dispatch = await conn.fetchval(
            "INSERT INTO crm.dispatch_queue (traveler_id, trip_id, channel, purpose, origin, message_id) "
            "VALUES ($1, $2, 'email', 'transactional', 'user_action', $3) RETURNING dispatch_id",
            ids["lead"],
            ids["trip"],
            ids["draft"],
        )
        decide = (
            "UPDATE crm.dispatch_queue SET status = $2, gate_result = $3, decided_at = now() WHERE dispatch_id = $1"
        )
        requeue = (
            "UPDATE crm.dispatch_queue SET status = 'queued', gate_result = NULL, decided_at = NULL "
            "WHERE dispatch_id = $1"
        )
        # Card R: a dead mailbox holds the row; reconnect re-queues it.
        await conn.execute(decide, dispatch, "held", "held_reconnect")
        await conn.execute(requeue, dispatch)
        await conn.execute(decide, dispatch, "held", "no_message")
        await conn.execute(requeue, dispatch)
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(decide, dispatch, "held", "platform_sender")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE crm.dispatch_queue SET message_id = gen_random_uuid() WHERE dispatch_id = $1", dispatch
                )
        await conn.execute(decide, dispatch, "dropped", "send_failed")
    # Erasing the message lets go of the row (SET NULL); it does not block.
    done = await _erase(app_pool, ids["tenant"], "conversation", ids["conversation"])
    assert done["counts"] == {"message": 2, "conversation": 1}
    async with _as(app_pool, ids["tenant"]) as conn:
        assert await conn.fetchval("SELECT message_id FROM crm.dispatch_queue WHERE dispatch_id = $1", dispatch) is None


async def test_erasure_takes_the_mail_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _mail(app_pool)
    tenant = ids["tenant"]
    async with _as(app_pool, tenant) as conn:
        # The companion's own thread (no trip) and a message of it.
        companion_conv = await conn.fetchval(
            "INSERT INTO crm.conversation (traveler_id) VALUES ($1) RETURNING conversation_id", ids["companion"]
        )
        await conn.execute(
            "INSERT INTO crm.message (conversation_id, direction, status, body_enc) VALUES ($1, 'in', 'received', $2)",
            companion_conv,
            _blob(),
        )
    dry = await _erase(app_pool, tenant, "trip", ids["trip"], dry_run=True)
    assert dry["counts"]["message"] == 2 and dry["counts"]["conversation"] == 1
    trip = await _erase(app_pool, tenant, "trip", ids["trip"])
    assert trip["counts"]["message"] == 2 and trip["counts"]["conversation"] == 1
    traveller = await _erase(app_pool, tenant, "traveler", ids["companion"], ids["token2"])
    assert traveller["counts"]["message"] == 1 and traveller["counts"]["conversation"] == 1
    unmatched = await _erase(app_pool, tenant, "conversation", ids["unmatched"])
    assert unmatched["counts"] == {"message": 1, "conversation": 1}
    assert await _sqlstate(_erase(app_pool, tenant, "conversation", uuid.uuid4())) == "CRM13"
    # The agency erase: mailboxes, the address, replies and imports go too, no CRM16.
    other = await _mail(app_pool)
    async with _as(app_pool, other["tenant"]) as conn:
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    erased = await _erase(app_pool, other["tenant"], "tenant", other["tenant"])
    for table in MAIL_TABLES:
        assert await _count(app_pool, other["tenant"], table) == 0, table
    assert erased["counts"]["channel_account"] == 1 and erased["counts"]["ingest_address"] == 1
    assert await _count(app_pool, tenant, "saved_reply") == 1  # another agency is untouched


async def test_a_held_trip_keeps_its_mail(app_pool: asyncpg.Pool) -> None:
    ids = await _mail(app_pool)
    tenant = ids["tenant"]
    await _hold(app_pool, tenant, "trip", ids["trip"], "member:m")
    trash = (
        "UPDATE crm.conversation SET deleted_at = now(), deleted_by = 'm', purge_after = now() "
        "WHERE conversation_id = $1"
    )
    assert await _sqlstate(_run(app_pool, tenant, trash, ids["conversation"])) == "CRM10"
    unlink = "UPDATE crm.conversation SET trip_id = NULL WHERE conversation_id = $1"
    assert await _sqlstate(_run(app_pool, tenant, unlink, ids["conversation"])) == "CRM10"
    # Even a draft of the held trip's thread stays (the record), and nothing moves out of the thread.
    assert await _sqlstate(_run(app_pool, tenant, "DELETE FROM crm.message WHERE message_id = $1", ids["draft"])) == (
        "CRM10"
    )
    move = "UPDATE crm.message SET conversation_id = $2 WHERE message_id = $1"
    assert await _sqlstate(_run(app_pool, tenant, move, ids["inbound"], ids["unmatched"])) == "CRM10"
    assert await _sqlstate(_erase(app_pool, tenant, "conversation", ids["conversation"])) == "CRM10"
    assert await _sqlstate(_erase(app_pool, tenant, "trip", ids["trip"], dry_run=True)) == "CRM10"
    # A trip hold also reaches the traveller its conversation names when that traveller is erased.
    async with _as(app_pool, tenant) as conn:
        assert await conn.fetchval("SELECT crm.legal_hold_blocks('conversation', $1)", ids["conversation"])
        assert not await conn.fetchval("SELECT crm.legal_hold_blocks('conversation', $1)", ids["unmatched"])
    # The Unmatched thread is not held: linking it to a client (a link set from NULL) is allowed.
    await _run(
        app_pool,
        tenant,
        "UPDATE crm.conversation SET traveler_id = $2 WHERE conversation_id = $1",
        ids["unmatched"],
        ids["companion"],
    )
