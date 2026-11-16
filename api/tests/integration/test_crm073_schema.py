"""crm-073 (AA-968, an expand file) on PostgreSQL with the RDS role shape: campaigns, their recipients and the
agency's suppression list.

- ``crm.campaign``: name, locale, audience codes, gate codes, state and hold code checks; a scheduled campaign has its
  time, a sent one its end.
- ``crm.campaign_recipient``: one per campaign and client; planned -> queued names its message, token hash and URL
  together; skipped names its outcome; the unsubscribe token hash is unique; the lookup policy finds a row by its
  hash only while ``app.campaign_unsubscribe_hash`` is set, and in no other agency.
- ``crm.campaign_suppression``: one per agency and e-mail index (16 bytes), the three reasons; it names no client, so
  the client erase keeps it (an unsubscribed contact imported again stays suppressed, card R).
- Tenant isolation: another agency sees nothing; the client erase takes the recipient rows, the agency erase every row.

Invented names only.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NEW_TABLES = ("campaign", "campaign_recipient", "campaign_suppression")
MEMBER = "member:test"
SEALED = b"\x01" * 40  # an AES-GCM blob's minimum length is all the checks look at
BIDX = b"\x02" * 16


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = MEMBER) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        return await conn.fetchval(sql, *args)


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    ids = await _agency(pool)
    await _run(pool, ids["tenant"], "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING")
    ids["campaign"] = await _run(
        pool, ids["tenant"], "INSERT INTO crm.campaign (name) VALUES ('Spring in the hills') RETURNING campaign_id"
    )
    return ids


async def _recipient(pool: asyncpg.Pool, ids: dict[str, Any], traveler: uuid.UUID | None = None) -> Any:
    return await _run(
        pool,
        ids["tenant"],
        "INSERT INTO crm.campaign_recipient (campaign_id, traveler_id, send_on) VALUES ($1, $2, current_date) "
        "RETURNING campaign_recipient_id",
        ids["campaign"],
        traveler or ids["lead"],
    )


async def _queue(pool: asyncpg.Pool, tenant: uuid.UUID, recipient: Any, token: str) -> None:
    await _run(
        pool,
        tenant,
        "UPDATE crm.campaign_recipient SET state = 'queued', message_id = gen_random_uuid(), queued_at = now(), "
        "dispatch_id = gen_random_uuid(), unsubscribe_hash = $2, unsubscribe_url_enc = $3 "
        "WHERE campaign_recipient_id = $1",
        recipient,
        hashlib.sha256(token.encode()).digest(),
        SEALED,
    )


@pytest.mark.parametrize("table", NEW_TABLES)
async def test_registered_and_no_reader_grant(admin: asyncpg.Connection, table: str) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-073'") == 1
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege)


async def test_campaign_values_and_states(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    bad = [
        "UPDATE crm.campaign SET name = '' WHERE campaign_id = $1",
        "UPDATE crm.campaign SET locale = 'fr' WHERE campaign_id = $1",
        "UPDATE crm.campaign SET subject_enc = '\\x01'::bytea WHERE campaign_id = $1",
        "UPDATE crm.campaign SET audience_tags = '{Bad Code}' WHERE campaign_id = $1",
        "UPDATE crm.campaign SET gate_codes = '{X}' WHERE campaign_id = $1",
        "UPDATE crm.campaign SET daily_limit = 0 WHERE campaign_id = $1",
        "UPDATE crm.campaign SET state = 'paused' WHERE campaign_id = $1",
        "UPDATE crm.campaign SET state = 'scheduled' WHERE campaign_id = $1",  # no time
        "UPDATE crm.campaign SET state = 'held', scheduled_at = now() WHERE campaign_id = $1",  # no hold code
        "UPDATE crm.campaign SET hold_code = 'content_gate' WHERE campaign_id = $1",  # a draft is not held
        "UPDATE crm.campaign SET state = 'sent', scheduled_at = now() WHERE campaign_id = $1",  # no end
    ]
    for sql in bad:
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, sql, ids["campaign"])
    await _run(
        app_pool,
        t,
        "UPDATE crm.campaign SET state = 'held', hold_code = 'aa_tour_retired', scheduled_at = now(), "
        "aa_tour_ids = '{0f6b1c52-7d3e-4a8e-9c11-5e2f7a9b3d40}', audience_tags = '{repeat,sys_ready_to_travel}', "
        "gate_codes = '{link_not_allowed}', daily_limit = 400, subject_enc = $2, body_enc = $2 WHERE campaign_id = $1",
        ids["campaign"],
        SEALED,
    )
    await _run(
        app_pool,
        t,
        "UPDATE crm.campaign SET state = 'sent', hold_code = NULL, finished_at = now() WHERE campaign_id = $1",
        ids["campaign"],
    )


async def test_recipient_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    recipient = await _recipient(app_pool, ids)
    with pytest.raises(asyncpg.UniqueViolationError):  # one row per campaign and client
        await _recipient(app_pool, ids)
    bad = [
        "UPDATE crm.campaign_recipient SET state = 'queued' WHERE campaign_recipient_id = $1",  # no message
        "UPDATE crm.campaign_recipient SET state = 'skipped' WHERE campaign_recipient_id = $1",  # no outcome
        "UPDATE crm.campaign_recipient SET outcome_code = 'no_consent' WHERE campaign_recipient_id = $1",
        "UPDATE crm.campaign_recipient SET state = 'sent' WHERE campaign_recipient_id = $1",
        "UPDATE crm.campaign_recipient SET state = 'queued', message_id = gen_random_uuid(), queued_at = now() "
        "WHERE campaign_recipient_id = $1",  # a queued mail carries its unsubscribe token hash and URL
        "UPDATE crm.campaign_recipient SET unsubscribe_hash = '\\x01'::bytea WHERE campaign_recipient_id = $1",
    ]
    for sql in bad:
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, sql, recipient)
    await _queue(app_pool, t, recipient, "token-one")
    second = await _recipient(app_pool, ids, ids["companion"])
    with pytest.raises(asyncpg.UniqueViolationError):  # one token hash, one mail
        await _queue(app_pool, t, second, "token-one")
    await _run(
        app_pool,
        t,
        "UPDATE crm.campaign_recipient SET state = 'skipped', outcome_code = 'no_consent' "
        "WHERE campaign_recipient_id = $1",
        second,
    )


async def test_the_unsubscribe_lookup_reads_one_row_by_its_hash(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    recipient = await _recipient(app_pool, ids)
    await _queue(app_pool, ids["tenant"], recipient, "token-two")
    digest = hashlib.sha256(b"token-two").hexdigest()
    sql = "SELECT tenant_id FROM crm.campaign_recipient WHERE unsubscribe_hash = decode($1, 'hex')"
    async with app_pool.acquire() as conn, conn.transaction():
        assert await conn.fetchval(sql, digest) is None  # no agency, no hash setting: nothing
        await conn.execute("SELECT set_config('app.campaign_unsubscribe_hash', $1, true)", digest)
        assert await conn.fetchval(sql, digest) == ids["tenant"]
        assert await conn.fetchval("SELECT count(*) FROM crm.campaign_recipient") == 1  # that one row only
        await conn.execute("SELECT set_config('app.campaign_unsubscribe_hash', $1, true)", "00" * 32)
        assert await conn.fetchval(sql, digest) is None


async def test_suppression_rows_and_the_client_erase_keeps_them(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    insert = "INSERT INTO crm.campaign_suppression (email_bidx, reason) VALUES ($1, $2)"
    await _run(app_pool, t, insert, BIDX, "unsubscribed")
    with pytest.raises(asyncpg.UniqueViolationError):  # one row per address
        await _run(app_pool, t, insert, BIDX, "bounced")
    for bidx, reason in ((b"\x03" * 8, "unsubscribed"), (b"\x04" * 16, "forgotten")):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, insert, bidx, reason)
    recipient = await _recipient(app_pool, ids)
    assert recipient is not None
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, t)
        await conn.execute("UPDATE crm.trip SET deleted_at = now(), deleted_by = 'member:x', purge_after = now()")
    await _erase(app_pool, t, "traveler", ids["lead"], ids["token"])
    assert await _count(app_pool, t, "campaign_recipient") == 0  # the client's rows went with the client
    assert await _count(app_pool, t, "campaign_suppression") == 1  # the address stays suppressed
    assert await _count(app_pool, t, "campaign") == 1


async def test_another_agency_sees_nothing_and_the_agency_erase_takes_every_row(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    other = await _setup(app_pool)
    t = ids["tenant"]
    await _recipient(app_pool, ids)
    await _run(app_pool, t, "INSERT INTO crm.campaign_suppression (email_bidx, reason) VALUES ($1, 'unsubscribed')",
               BIDX)  # fmt: skip
    for table in NEW_TABLES:
        assert await _count(app_pool, t, table) == 1, table
    assert await _count(app_pool, other["tenant"], "campaign_recipient") == 0
    assert await _count(app_pool, other["tenant"], "campaign_suppression") == 0
    with pytest.raises(asyncpg.ForeignKeyViolationError):  # another agency's client cannot join this campaign
        await _run(app_pool, other["tenant"],
                   "INSERT INTO crm.campaign_recipient (campaign_id, traveler_id, send_on) "
                   "VALUES ($1, $2, current_date)", ids["campaign"], other["lead"])  # fmt: skip
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, t)
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    await _erase(app_pool, t, "tenant", t)
    for table in NEW_TABLES:
        assert await _count(app_pool, t, table) == 0, table
    assert await _count(app_pool, other["tenant"], "campaign") == 1
