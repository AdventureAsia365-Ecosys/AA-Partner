"""crm-039 (AA-939 follow-up, a contract file) on PostgreSQL with the RDS role shape.

The partner-account clean-up job (``app.actor`` = ``job:crm_partner_accounts``) reads every ``crm.studio_invitation``
row and writes none: an insert or an update under that actor is refused (row security WITH CHECK, guard CRM37); other
actors keep exactly what crm-020 gave them; the table keeps three policies.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

from tests.integration.test_crm020_schema import PLATFORM, _as, _issue, _update

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "039_crm_partner_account_cleanup.sql"
SWEEP = "job:crm_partner_accounts"


async def test_crm039_is_registered_and_keeps_three_policies(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-039'") == 1
    assert (
        await admin.fetchval("SELECT count(*) FROM pg_policy WHERE polrelid = 'crm.studio_invitation'::regclass") == 3
    )


async def test_the_cleanup_job_reads_every_row(app_pool: asyncpg.Pool) -> None:
    row_id, _ = await _issue(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _as(conn, SWEEP)
        assert (
            await conn.fetchval(
                "SELECT count(*) AS n FROM crm.studio_invitation WHERE studio_invitation_id = $1", row_id
            )
            == 1
        )
        await _as(conn, "job:crm_other_job")
        assert (
            await conn.fetchval(
                "SELECT count(*) AS n FROM crm.studio_invitation WHERE studio_invitation_id = $1", row_id
            )
            == 0
        )


async def test_the_cleanup_job_writes_nothing(app_pool: asyncpg.Pool) -> None:
    row_id, _ = await _issue(app_pool)
    with pytest.raises(asyncpg.PostgresError):
        await _update(
            app_pool,
            SWEEP,
            "UPDATE crm.studio_invitation SET status = 'revoked' WHERE studio_invitation_id = $1",
            row_id,
        )
    with pytest.raises(asyncpg.PostgresError):
        async with app_pool.acquire() as conn, conn.transaction():
            await _as(conn, SWEEP)
            await conn.execute(
                "INSERT INTO crm.studio_invitation (cis_tenant_id, cis_invitation_id, email_enc, token_hash, "
                "expires_at) VALUES (gen_random_uuid(), gen_random_uuid(), $1, $2, now() + interval '1 day')",
                b"x" * 64,
                b"y" * 32,
            )
    # A platform actor still revokes (crm-020 unchanged for it).
    assert (
        await _update(
            app_pool,
            PLATFORM,
            "UPDATE crm.studio_invitation SET status = 'revoked' WHERE studio_invitation_id = $1",
            row_id,
        )
        == "UPDATE 1"
    )


async def test_reapplying_crm039_is_a_no_op(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE crm_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert (
        await admin.fetchval("SELECT count(*) FROM pg_policy WHERE polrelid = 'crm.studio_invitation'::regclass") == 3
    )


async def test_guard_0_needs_crm_038(admin: asyncpg.Connection) -> None:
    tx = admin.transaction()
    await tx.start()
    try:
        await admin.execute("DELETE FROM shared.schema_versions WHERE version = 'crm-038'")
        await admin.execute("SET LOCAL ROLE crm_owner")
        with pytest.raises(asyncpg.RaiseError, match="crm-038"):
            await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await tx.rollback()
