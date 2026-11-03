"""crm-041 (AA-1025 H1c, a contract file) on PostgreSQL with the RDS role shape.

- ``crm.llm_app_profile``: platform configuration (no tenant column), no seed row; the CHECKs refuse another app, a
  negative or too large budget, another degrade mode, a too long reason, a bad ``updated_by``;
- ``crm_app`` may read, insert and update the row but never delete or truncate it (a deleted row would switch AI back
  on); no reader gets anything.
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.conftest import READERS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

UPSERT_SQL = (
    "INSERT INTO crm.llm_app_profile (app, ai_enabled, default_daily_budget_usd, disabled_reason, updated_by) "
    "VALUES ('crm', false, 1.5, 'zz test switch', 'platform:zz-staff') "
    "ON CONFLICT (app) DO UPDATE SET ai_enabled = EXCLUDED.ai_enabled, updated_at = now()"
)


async def test_crm041_is_registered_and_seeds_no_row(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-041'") == 1
    assert await admin.fetchval("SELECT count(*) FROM crm.llm_app_profile") == 0


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO crm.llm_app_profile (app) VALUES ('cis')",
        "INSERT INTO crm.llm_app_profile (app, default_daily_budget_usd) VALUES ('crm', -1)",
        "INSERT INTO crm.llm_app_profile (app, default_daily_budget_usd) VALUES ('crm', 100001)",
        "INSERT INTO crm.llm_app_profile (app, degrade_mode) VALUES ('crm', 'queue_retry')",
        "INSERT INTO crm.llm_app_profile (app, disabled_reason) VALUES ('crm', repeat('x', 501))",
        "INSERT INTO crm.llm_app_profile (app, updated_by) VALUES ('crm', 'bad by value')",
    ],
)
async def test_bad_values_are_refused(admin: asyncpg.Connection, sql: str) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(sql)


async def test_crm_app_writes_the_row_but_never_deletes_it(app_pool: asyncpg.Pool) -> None:
    async with app_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(UPSERT_SQL)
            row = await conn.fetchrow(
                "SELECT ai_enabled, default_daily_budget_usd AS budget, degrade_mode FROM crm.llm_app_profile"
            )
            assert row["ai_enabled"] is False and float(row["budget"]) == 1.5
            assert row["degrade_mode"] == "manual_reply_needed"
            await conn.execute(UPSERT_SQL)  # the PUT's upsert runs again on the existing row
            for sql in ("DELETE FROM crm.llm_app_profile", "TRUNCATE crm.llm_app_profile"):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with conn.transaction():  # a savepoint: the refusal leaves the outer transaction usable
                        await conn.execute(sql)
        finally:
            await tx.rollback()


async def test_readers_get_nothing(admin: asyncpg.Connection) -> None:
    for role in READERS:
        allowed = await admin.fetchval("SELECT has_table_privilege($1, 'crm.llm_app_profile', 'SELECT') AS ok", role)
        assert allowed is False, role
