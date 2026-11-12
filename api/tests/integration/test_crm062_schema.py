"""crm-062 (AA-1092, a contract file) on PostgreSQL with the RDS role shape: the Book now price ceiling per agency.

- ``crm.tenant_settings.public_price_per_minute``: registered, default 60, the owner's value 10..600 (CHECK), the app
  role reads and writes it for its own agency; another agency sees nothing of it.
- Re-runnable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import OWNER
from tests.integration.test_crm010_schema import _agency, _ctx

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CRM066 = Path(__file__).resolve().parents[2] / "migrations" / "062_crm_book_now_price_ceiling.sql"


@pytest_asyncio.fixture(autouse=True)
async def _crm062(admin: asyncpg.Connection) -> AsyncIterator[None]:
    """crm-062 again (re-runnable) before each test: an earlier test of the session may have re-run an older file."""
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(CRM066.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    yield


async def _settings(conn: asyncpg.Connection) -> None:
    await conn.execute("INSERT INTO crm.tenant_settings (tenant_id) VALUES (current_setting('app.tenant_id')::uuid)"
                       " ON CONFLICT DO NOTHING")  # fmt: skip


async def test_crm062_is_registered_and_the_default_is_60(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-062'") == 1
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "seed:test")
        await _settings(conn)
        assert await conn.fetchval("SELECT public_price_per_minute AS ok FROM crm.tenant_settings") == 60


@pytest.mark.parametrize(("value", "ok"), [(9, False), (10, True), (600, True), (601, False)])
async def test_the_ceiling_is_bounded(app_pool: asyncpg.Pool, value: int, ok: bool) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            await _settings(conn)
        if ok:
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], "seed:test")
                assert await conn.execute("UPDATE crm.tenant_settings SET public_price_per_minute = $1", value) == (
                    "UPDATE 1"
                )
        else:
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await _ctx(conn, ids["tenant"], "seed:test")
                    await conn.execute("UPDATE crm.tenant_settings SET public_price_per_minute = $1", value)


async def test_another_agency_does_not_see_or_change_it(app_pool: asyncpg.Pool) -> None:
    mine, other = await _agency(app_pool), await _agency(app_pool)
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, mine["tenant"], "seed:test")
            await _settings(conn)
            await conn.execute("UPDATE crm.tenant_settings SET public_price_per_minute = 120")
        async with conn.transaction():
            await _ctx(conn, other["tenant"], "seed:test")
            seen = await conn.fetchval(
                "SELECT count(*) AS ok FROM crm.tenant_settings WHERE tenant_id = $1", mine["tenant"]
            )
            changed = await conn.execute(
                "UPDATE crm.tenant_settings SET public_price_per_minute = 10 WHERE tenant_id = $1", mine["tenant"]
            )
        assert (seen, changed) == (0, "UPDATE 0")
        async with conn.transaction():
            await _ctx(conn, mine["tenant"], "seed:test")
            assert await conn.fetchval("SELECT public_price_per_minute AS ok FROM crm.tenant_settings") == 120
