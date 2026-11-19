"""crm-079 (AA-1143, decision G3; a contract file) on PostgreSQL with the RDS role shape.

- ``crm.llm_connection_share``: platform configuration (no tenant column), no seed row; the CHECKs refuse a key that is
  not a CIS registry key, a negative or too large share, a short reason, a bad ``updated_by``;
- ``crm_app`` reads and replaces the copy (insert, update, delete); no reader gets anything.
Invented connection keys only.
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.conftest import READERS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

REASON = "zz test: the CIS budget save reason"
INSERT_SQL = (
    "INSERT INTO crm.llm_connection_share (connection_key, share_usd, reason, updated_by) VALUES ($1, $2, $3, $4)"
)


async def test_crm079_is_registered_and_seeds_no_row(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-079'") == 1
    assert await admin.fetchval("SELECT count(*) FROM crm.llm_connection_share") == 0


@pytest.mark.parametrize(
    "key, usd, reason, by",
    [
        ("zz_bad", 1, REASON, "platform:zz"),
        ("zz-a", -1, REASON, "platform:zz"),
        ("zz-a", 1000001, REASON, "platform:zz"),
        ("zz-a", 1, "too short", "platform:zz"),
        ("zz-a", 1, REASON, "bad by value"),
    ],
)
async def test_bad_values_are_refused(admin: asyncpg.Connection, key, usd, reason, by) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(INSERT_SQL, key, usd, reason, by)


async def test_crm_app_replaces_the_copy(app_pool: asyncpg.Pool) -> None:
    async with app_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(INSERT_SQL, "zz-a", 5, REASON, "platform:zz-staff")
            await conn.execute("UPDATE crm.llm_connection_share SET share_usd = 8 WHERE connection_key = 'zz-a'")
            assert float(await conn.fetchval("SELECT share_usd FROM crm.llm_connection_share")) == 8.0
            await conn.execute("DELETE FROM crm.llm_connection_share WHERE connection_key = 'zz-a'")
        finally:
            await tx.rollback()


async def test_readers_get_nothing(admin: asyncpg.Connection) -> None:
    for role in READERS:
        allowed = await admin.fetchval(
            "SELECT has_table_privilege($1, 'crm.llm_connection_share', 'SELECT') AS ok", role
        )
        assert allowed is False, role
