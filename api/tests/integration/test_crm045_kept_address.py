"""crm-045 (AA-1035 R2 close-out) on PostgreSQL with the RDS role shape: the kept address of crm.studio_invitation.

- Four nullable columns, all or none (studio_invitation_kept_chk); crm_app may write them.
- Only a platform actor sets the kept address (issue, re-issue); the token holder may only clear it (the claim does);
  the activation job never touches it (CRM37). Every crm-020 rule still holds.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "045_crm_studio_invitation_kept_address.sql"
PLATFORM = "platform:staff-sub-1045"
JOB = "job:crm_studio_activation"
ISS = "https://cognito-idp.example.test/pool"
KEPT = (os.urandom(60), os.urandom(184), "local", "local-1")


def _hash() -> bytes:
    return hashlib.sha256(os.urandom(16)).digest()


async def _as(conn: asyncpg.Connection, actor: str = "", token_hash: bytes | None = None) -> None:
    await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
    await conn.execute(
        "SELECT set_config('app.invite_token_hash', $1, true)", token_hash.hex() if token_hash is not None else ""
    )


async def _issue(pool: asyncpg.Pool, kept: tuple | None = KEPT) -> tuple[uuid.UUID, bytes]:
    token_hash = _hash()
    kept = kept or (None, None, None, None)
    async with pool.acquire() as conn, conn.transaction():
        await _as(conn, PLATFORM)
        row_id = await conn.fetchval(
            "INSERT INTO crm.studio_invitation (cis_tenant_id, cis_invitation_id, email_enc, token_hash, expires_at, "
            "email_kept, email_kept_dek, email_kept_kek_provider, email_kept_kek_id) "
            "VALUES ($1, $2, $3, $4, now() + interval '7 days', $5, $6, $7, $8) RETURNING studio_invitation_id",
            uuid.uuid4(),
            uuid.uuid4(),
            os.urandom(64),
            token_hash,
            *kept,
        )
    return row_id, token_hash


async def _update(pool: asyncpg.Pool, actor: str, sql: str, *args, token_hash: bytes | None = None) -> str:
    async with pool.acquire() as conn, conn.transaction():
        await _as(conn, actor, token_hash)
        return await conn.execute(sql, *args)


async def _kept(pool: asyncpg.Pool, row_id: uuid.UUID) -> tuple:
    async with pool.acquire() as conn, conn.transaction():
        await _as(conn, PLATFORM)
        row = await conn.fetchrow(
            "SELECT email_kept, email_kept_dek, email_kept_kek_provider, email_kept_kek_id FROM crm.studio_invitation "
            "WHERE studio_invitation_id = $1",
            row_id,
        )
    return tuple(row)


async def test_registered_and_shaped(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'crm-045'") == 1
    for col in ("email_kept", "email_kept_dek", "email_kept_kek_provider", "email_kept_kek_id"):
        assert await admin.fetchval(
            "SELECT NOT attnotnull FROM pg_attribute WHERE attrelid = 'crm.studio_invitation'::regclass "
            "AND attname = $1",
            col,
        )
        assert await admin.fetchval(
            "SELECT has_column_privilege('crm_app', 'crm.studio_invitation', $1, 'UPDATE')", col
        )
    text = MIGRATION.read_text(encoding="utf-8")
    assert "-- migrate:" not in text  # contract: no directive
    assert "pii:contact" in (
        await admin.fetchval(
            "SELECT col_description('crm.studio_invitation'::regclass, attnum) FROM pg_attribute "
            "WHERE attrelid = 'crm.studio_invitation'::regclass AND attname = 'email_kept'"
        )
    )


async def test_all_four_or_none(app_pool: asyncpg.Pool) -> None:
    await _issue(app_pool, kept=None)
    with pytest.raises(asyncpg.CheckViolationError):
        await _issue(app_pool, kept=(os.urandom(60), None, "local", "local-1"))
    with pytest.raises(asyncpg.CheckViolationError):
        await _issue(app_pool, kept=(os.urandom(60), os.urandom(184), "vault", "local-1"))


async def test_the_platform_keeps_it_and_reissues_with_it(app_pool: asyncpg.Pool) -> None:
    row_id, token_hash = await _issue(app_pool, kept=None)
    await _update(
        app_pool,
        PLATFORM,
        "UPDATE crm.studio_invitation SET token_hash = $2, email_enc = $3, issued_at = now(), "
        "expires_at = now() + interval '7 days', email_kept = $4, email_kept_dek = $5, email_kept_kek_provider = $6, "
        "email_kept_kek_id = $7 WHERE studio_invitation_id = $1",
        row_id,
        _hash(),
        os.urandom(64),
        *KEPT,
    )
    assert await _kept(app_pool, row_id) == KEPT


async def test_the_holder_only_clears_it_at_the_claim(app_pool: asyncpg.Pool) -> None:
    row_id, token_hash = await _issue(app_pool)
    holder = f"invitation:{row_id}"
    with pytest.raises(asyncpg.PostgresError) as exc:  # never a different kept address
        await _update(
            app_pool, holder, "UPDATE crm.studio_invitation SET email_kept = $1", os.urandom(60), token_hash=token_hash
        )
    assert exc.value.sqlstate == "CRM37"
    await _update(
        app_pool,
        holder,
        "UPDATE crm.studio_invitation SET status = 'claimed', cognito_iss = $1, cognito_sub = 'sub-1', "
        "claimed_at = now(), email_kept = NULL, email_kept_dek = NULL, email_kept_kek_provider = NULL, "
        "email_kept_kek_id = NULL",
        ISS,
        token_hash=token_hash,
    )
    assert await _kept(app_pool, row_id) == (None, None, None, None)


async def test_the_job_never_touches_it(app_pool: asyncpg.Pool) -> None:
    row_id, _ = await _issue(app_pool)
    with pytest.raises(asyncpg.PostgresError) as exc:
        await _update(
            app_pool,
            JOB,
            "UPDATE crm.studio_invitation SET email_kept = NULL, email_kept_dek = NULL, "
            "email_kept_kek_provider = NULL, email_kept_kek_id = NULL WHERE studio_invitation_id = $1",
            row_id,
        )
    assert exc.value.sqlstate == "CRM37"


async def test_the_crm020_rules_still_hold(app_pool: asyncpg.Pool) -> None:
    row_id, token_hash = await _issue(app_pool)
    holder = f"invitation:{row_id}"
    with pytest.raises(asyncpg.PostgresError) as exc:  # the holder never changes the token
        await _update(
            app_pool, holder, "UPDATE crm.studio_invitation SET token_hash = $1", _hash(), token_hash=token_hash
        )
    assert exc.value.sqlstate == "CRM37"
    with pytest.raises(asyncpg.PostgresError):  # a platform actor never claims
        await _update(
            app_pool,
            PLATFORM,
            "UPDATE crm.studio_invitation SET status = 'claimed', cognito_iss = $2, cognito_sub = 's', "
            "claimed_at = now() WHERE studio_invitation_id = $1",
            row_id,
            ISS,
        )
