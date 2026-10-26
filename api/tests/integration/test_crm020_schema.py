"""crm-020 (AA-979, CRM-P2b) on PostgreSQL 15 with the RDS role shape: crm.studio_invitation.

- A platform table: no tenant_id (a CIS tenant may have no CRM agency), FORCE row security on the actor and the token.
- Only a platform actor issues, re-issues and revokes; the token holder only claims, accepts or expires; the activation
  job only records an attempt and activates; the tenant, the CIS invitation and a bound identity never change (CRM37).
- crm_app never deletes; the NOLOGIN readers have nothing; Guard 0 refuses to run before crm-019.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from tests.integration.conftest import OWNER

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "020_crm_studio_invitation.sql"
PLATFORM = "platform:staff-sub-1"
JOB = "job:crm_studio_activation"
ISS = "https://cognito-idp.example.test/pool"


def _hash() -> bytes:
    return hashlib.sha256(os.urandom(16)).digest()


async def _as(conn: asyncpg.Connection, actor: str = "", token_hash: bytes | None = None) -> None:
    await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
    await conn.execute(
        "SELECT set_config('app.invite_token_hash', $1, true)", token_hash.hex() if token_hash is not None else ""
    )


async def _issue(pool: asyncpg.Pool, token_hash: bytes | None = None) -> tuple[uuid.UUID, bytes]:
    token_hash = token_hash or _hash()
    async with pool.acquire() as conn, conn.transaction():
        await _as(conn, PLATFORM)
        row_id = await conn.fetchval(
            "INSERT INTO crm.studio_invitation (cis_tenant_id, cis_invitation_id, email_enc, token_hash, expires_at) "
            "VALUES ($1, $2, $3, $4, now() + interval '7 days') RETURNING studio_invitation_id",
            uuid.uuid4(),
            uuid.uuid4(),
            os.urandom(64),
            token_hash,
        )
    return row_id, token_hash


async def _update(pool: asyncpg.Pool, actor: str, sql: str, *args, token_hash: bytes | None = None) -> str:
    async with pool.acquire() as conn, conn.transaction():
        await _as(conn, actor, token_hash)
        return await conn.execute(sql, *args)


async def test_shape_and_privileges(admin: asyncpg.Connection) -> None:
    rel = await admin.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'crm.studio_invitation'::regclass"
    )
    assert rel["relrowsecurity"] and rel["relforcerowsecurity"]
    cols = {
        r["attname"]
        for r in await admin.fetch(
            "SELECT attname FROM pg_attribute WHERE attrelid = 'crm.studio_invitation'::regclass AND attnum > 0"
        )
    }
    assert "tenant_id" not in cols and {"cis_tenant_id", "cis_invitation_id", "email_enc", "token_hash"} <= cols
    for role in ("crm_ai_reader", "crm_insights", "crm_support_reader", "crm_eraser"):
        assert not await admin.fetchval("SELECT has_table_privilege($1, 'crm.studio_invitation', 'SELECT')", role)
    assert not await admin.fetchval("SELECT has_table_privilege('crm_app', 'crm.studio_invitation', 'DELETE')")


async def test_rows_are_invisible_without_a_platform_actor_or_the_token(app_pool: asyncpg.Pool) -> None:
    row_id, token_hash = await _issue(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _as(conn, "member:someone")
        assert (
            await conn.fetchval("SELECT count(*) FROM crm.studio_invitation WHERE studio_invitation_id = $1", row_id)
            == 0
        )
        await _as(conn, "invitation:x", _hash())
        assert await conn.fetchval("SELECT count(*) FROM crm.studio_invitation") == 0
        await _as(conn, "invitation:x", token_hash)
        assert await conn.fetchval("SELECT studio_invitation_id FROM crm.studio_invitation") == row_id
        await _as(conn, JOB)
        assert (
            await conn.fetchval("SELECT count(*) FROM crm.studio_invitation WHERE studio_invitation_id = $1", row_id)
            == 1
        )


async def test_only_a_platform_actor_issues(app_pool: asyncpg.Pool) -> None:
    for actor in ("", "member:m1", JOB, "invitation:x"):
        with pytest.raises((asyncpg.PostgresError,)):
            async with app_pool.acquire() as conn, conn.transaction():
                await _as(conn, actor)
                await conn.execute(
                    "INSERT INTO crm.studio_invitation (cis_tenant_id, cis_invitation_id, email_enc, token_hash, "
                    "expires_at) VALUES ($1, $2, $3, $4, now() + interval '1 day')",
                    uuid.uuid4(),
                    uuid.uuid4(),
                    os.urandom(64),
                    _hash(),
                )


async def test_claim_accept_activate_happy_path(app_pool: asyncpg.Pool) -> None:
    row_id, token_hash = await _issue(app_pool)
    holder = f"invitation:{row_id}"
    await _update(
        app_pool,
        holder,
        "UPDATE crm.studio_invitation SET status = 'claimed', cognito_iss = $1, "
        "cognito_sub = 'sub-1', claimed_at = now()",
        ISS,
        token_hash=token_hash,
    )
    await _update(
        app_pool,
        holder,
        "UPDATE crm.studio_invitation SET status = 'accepted', accepted_at = now()",
        token_hash=token_hash,
    )
    await _update(
        app_pool,
        JOB,
        "UPDATE crm.studio_invitation SET activation_attempts = activation_attempts + 1, "
        "activation_error = 'cis_unavailable' WHERE studio_invitation_id = $1",
        row_id,
    )
    await _update(
        app_pool,
        JOB,
        "UPDATE crm.studio_invitation SET status = 'activated', activated_at = now(), "
        "activation_attempts = activation_attempts + 1, activation_error = NULL "
        "WHERE studio_invitation_id = $1",
        row_id,
    )
    async with app_pool.acquire() as conn, conn.transaction():
        await _as(conn, PLATFORM)
        row = await conn.fetchrow(
            "SELECT status, activation_attempts FROM crm.studio_invitation WHERE studio_invitation_id = $1", row_id
        )
    assert dict(row) == {"status": "activated", "activation_attempts": 2}


async def test_the_holder_cannot_activate_or_change_the_identity(app_pool: asyncpg.Pool) -> None:
    row_id, token_hash = await _issue(app_pool)
    holder = f"invitation:{row_id}"
    with pytest.raises(asyncpg.PostgresError):  # skips claimed
        await _update(
            app_pool,
            holder,
            "UPDATE crm.studio_invitation SET status = 'accepted', accepted_at = now(), "
            "cognito_iss = $1, cognito_sub = 's', claimed_at = now()",
            ISS,
            token_hash=token_hash,
        )
    await _update(
        app_pool,
        holder,
        "UPDATE crm.studio_invitation SET status = 'claimed', cognito_iss = $1, "
        "cognito_sub = 'sub-1', claimed_at = now()",
        ISS,
        token_hash=token_hash,
    )
    with pytest.raises(asyncpg.PostgresError) as exc:
        await _update(app_pool, holder, "UPDATE crm.studio_invitation SET cognito_sub = 'sub-2'", token_hash=token_hash)
    assert exc.value.sqlstate == "CRM37"
    with pytest.raises(asyncpg.PostgresError):
        await _update(
            app_pool, holder, "UPDATE crm.studio_invitation SET token_hash = $1", _hash(), token_hash=token_hash
        )


async def test_platform_reissues_an_expired_invitation_and_revokes(app_pool: asyncpg.Pool) -> None:
    row_id, token_hash = await _issue(app_pool)
    await _update(
        app_pool, f"invitation:{row_id}", "UPDATE crm.studio_invitation SET status = 'expired'", token_hash=token_hash
    )
    new_hash = _hash()
    await _update(
        app_pool,
        PLATFORM,
        "UPDATE crm.studio_invitation SET status = 'pending', token_hash = $2, "
        "email_enc = $3, issued_at = now(), expires_at = now() + interval '7 days' "
        "WHERE studio_invitation_id = $1",
        row_id,
        new_hash,
        os.urandom(64),
    )
    with pytest.raises(asyncpg.PostgresError):  # a platform actor never claims for someone
        await _update(
            app_pool,
            PLATFORM,
            "UPDATE crm.studio_invitation SET status = 'claimed', cognito_iss = $2, "
            "cognito_sub = 's', claimed_at = now() WHERE studio_invitation_id = $1",
            row_id,
            ISS,
        )
    with pytest.raises(asyncpg.PostgresError):  # the CIS invitation is fixed
        await _update(
            app_pool,
            PLATFORM,
            "UPDATE crm.studio_invitation SET cis_invitation_id = $2 WHERE studio_invitation_id = $1",
            row_id,
            uuid.uuid4(),
        )
    await _update(
        app_pool,
        PLATFORM,
        "UPDATE crm.studio_invitation SET status = 'revoked' WHERE studio_invitation_id = $1",
        row_id,
    )


async def test_the_job_only_activates_an_accepted_invitation(app_pool: asyncpg.Pool) -> None:
    row_id, _ = await _issue(app_pool)
    with pytest.raises(asyncpg.PostgresError):
        await _update(
            app_pool, JOB, "UPDATE crm.studio_invitation SET status = 'revoked' WHERE studio_invitation_id = $1", row_id
        )
    with pytest.raises(asyncpg.PostgresError):
        await _update(
            app_pool,
            JOB,
            "UPDATE crm.studio_invitation SET activation_attempts = 1 WHERE studio_invitation_id = $1",
            row_id,
        )


async def test_crm_app_cannot_delete(app_pool: asyncpg.Pool) -> None:
    row_id, _ = await _issue(app_pool)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_pool.acquire() as conn, conn.transaction():
            await _as(conn, PLATFORM)
            await conn.execute("DELETE FROM crm.studio_invitation WHERE studio_invitation_id = $1", row_id)


async def test_guard_0_needs_crm_019(admin: asyncpg.Connection) -> None:
    tx = admin.transaction()
    await tx.start()
    try:
        await admin.execute("DELETE FROM shared.schema_versions WHERE version = 'crm-019'")
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        with pytest.raises(asyncpg.RaiseError, match="crm-019 is not registered"):
            await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await tx.rollback()
