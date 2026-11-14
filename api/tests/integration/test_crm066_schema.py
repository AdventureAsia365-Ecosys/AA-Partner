"""crm-066 / crm-067 (AA-1093 stage 2) on PostgreSQL 15 with the RDS role shape: the storage of crm_tenant_erase and the
one path that reaches a CIS tenant's Content studio invitations.

- crm-066 and crm-067 are registered; the three platform tables have no tenant column (they outlive the agency erase);
- ``crm.tenant_erasure_report``: one report per request, round and mode; the post outcome and its time go together;
  the body is an object;
- ``crm.erase_studio_invitations``: refused to every actor but ``job:crm_tenant_erase`` (CRM56), EXECUTE for
  ``crm_eraser`` only, counts in a dry run and deletes only the rows of the tenant it names; crm_app has no DELETE on
  the table itself (crm-020 rule kept).
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid

import asyncpg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

JOB = "job:crm_tenant_erase"
PLATFORM = "platform:staff-sub-1"


async def _invitation(pool: asyncpg.Pool, cis_tenant: uuid.UUID) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.actor', $1, true)", PLATFORM)
        await conn.execute(
            "INSERT INTO crm.studio_invitation (cis_tenant_id, cis_invitation_id, email_enc, token_hash, expires_at) "
            "VALUES ($1, $2, $3, $4, now() + interval '7 days')",
            cis_tenant,
            uuid.uuid4(),
            os.urandom(64),
            hashlib.sha256(os.urandom(16)).digest(),
        )


async def _erase(pool: asyncpg.Pool, actor: str, cis_tenant: uuid.UUID, dry_run: bool) -> int:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        await conn.execute("SET LOCAL ROLE crm_eraser")
        n = await conn.fetchval("SELECT crm.erase_studio_invitations($1, $2)", cis_tenant, dry_run)
        assert await conn.fetchval("SELECT current_setting('app.actor', true)") == actor  # restored
        return int(n)


async def _count(pool: asyncpg.Pool, cis_tenant: uuid.UUID) -> int:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.actor', $1, true)", PLATFORM)
        return int(await conn.fetchval("SELECT count(*) FROM crm.studio_invitation WHERE cis_tenant_id = $1", cis_tenant))


async def test_registered_and_platform_tables(admin: asyncpg.Connection) -> None:
    versions = {r["version"] for r in await admin.fetch("SELECT version FROM shared.schema_versions")}
    assert {"crm-066", "crm-067"} <= versions
    for table in ("tenant_erasure_feed_state", "tenant_erasure_saga", "tenant_erasure_report"):
        cols = {
            r["attname"]
            for r in await admin.fetch(
                "SELECT attname FROM pg_attribute WHERE attrelid = $1::regclass AND attnum > 0 AND NOT attisdropped",
                f"crm.{table}",
            )
        }
        assert cols and "tenant_id" not in cols, table


async def test_one_report_per_request_round_and_mode(app_pool: asyncpg.Pool) -> None:
    rid = uuid.uuid4()
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.actor', $1, true)", JOB)
        await conn.execute(
            "INSERT INTO crm.tenant_erasure_saga (request_id, subject_tenant_id, phase, round, step_status, not_before, "
            "is_pilot, is_synthetic, cis_updated_at) VALUES ($1, $2, 'dry_run', 2, 'active', now(), false, true, now())",
            rid,
            uuid.uuid4(),
        )
        insert = (
            "INSERT INTO crm.tenant_erasure_report (report_id, request_id, round, mode, outcome, plan_hash, body, "
            "reported_at) VALUES ($1, $2, 2, 'dry_run', 'ok', $3, $4::jsonb, now())"
        )
        await conn.execute(insert, uuid.uuid4(), rid, "a" * 64, json.dumps({"mode": "dry_run"}))
        with pytest.raises(asyncpg.UniqueViolationError):
            async with conn.transaction():
                await conn.execute(insert, uuid.uuid4(), rid, "a" * 64, json.dumps({"mode": "dry_run"}))
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE crm.tenant_erasure_report SET post_outcome = 'accepted' WHERE request_id = $1", rid
                )
        await conn.execute(
            "UPDATE crm.tenant_erasure_report SET post_outcome = 'accepted', posted_at = now() WHERE request_id = $1",
            rid,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE crm.tenant_erasure_report SET post_outcome = 'refused:Bad Code' WHERE request_id = $1", rid
                )


async def test_studio_invitations_only_through_the_job_actor(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    mine, other = uuid.uuid4(), uuid.uuid4()
    await _invitation(app_pool, mine)
    await _invitation(app_pool, mine)
    await _invitation(app_pool, other)
    assert not await admin.fetchval("SELECT has_table_privilege('crm_app', 'crm.studio_invitation', 'DELETE')")
    assert not await admin.fetchval(
        "SELECT has_function_privilege('crm_app', 'crm.erase_studio_invitations(uuid, boolean)', 'EXECUTE')"
    )
    for actor in (PLATFORM, "job:crm_offboard", "member:someone"):
        with pytest.raises(asyncpg.PostgresError) as err:
            await _erase(app_pool, actor, mine, False)
        assert err.value.sqlstate == "CRM56"
    assert await _erase(app_pool, JOB, mine, True) == 2
    assert await _count(app_pool, mine) == 2
    assert await _erase(app_pool, JOB, mine, False) == 2
    assert await _count(app_pool, mine) == 0
    assert await _count(app_pool, other) == 1  # another tenant's invitation stays
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('app.actor', $1, true)", JOB)
            await conn.fetchval("SELECT crm.erase_studio_invitations($1, false)", other)  # crm_app, not crm_eraser
