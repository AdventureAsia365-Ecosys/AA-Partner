"""crm-086 (AA-1152, a contract file) on PostgreSQL with the RDS role shape: ``crm.tenant_settings.price_round_step``
(default ``{}``, an object of at most 512 bytes) and ``show_planner_prices`` (default true), written by ``crm_app``
through a real tenant transaction. The service reading them (:mod:`app.proposals.rounding`) is tested in the code PR.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_crm086_is_registered_with_its_defaults(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-086'") == 1
    rows = await admin.fetch(
        "SELECT column_name, column_default, is_nullable FROM information_schema.columns WHERE table_schema = 'crm'"
        " AND table_name = 'tenant_settings' AND column_name IN ('price_round_step', 'show_planner_prices')"
        " ORDER BY column_name"
    )
    assert [(r["column_name"], r["column_default"], r["is_nullable"]) for r in rows] == [
        ("price_round_step", "'{}'::jsonb", "NO"),
        ("show_planner_prices", "true", "NO"),
    ]
    for column in ("price_round_step", "show_planner_prices"):
        assert await admin.fetchval(
            "SELECT has_column_privilege('crm_app', 'crm.tenant_settings', $1, 'UPDATE') AS ok", column
        )


async def test_crm_app_writes_them_and_the_check_keeps_the_shape(app_pool: asyncpg.Pool) -> None:
    tenant_id = uuid.uuid4()
    async with app_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
            await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant_id)
            await conn.execute(
                "INSERT INTO crm.tenant_settings (price_round_step, show_planner_prices) VALUES ($1::jsonb, false)",
                '{"USD": "5", "VND": "50000"}',
            )
            row = await conn.fetchrow(
                "SELECT price_round_step::text AS steps, show_planner_prices AS shown FROM crm.tenant_settings"
            )
            assert row["steps"] == '{"USD": "5", "VND": "50000"}' and row["shown"] is False
            for bad in ("[]", '"5"', '{"USD": "' + "9" * 600 + '"}'):
                with pytest.raises(asyncpg.CheckViolationError):
                    async with conn.transaction():
                        await conn.execute("UPDATE crm.tenant_settings SET price_round_step = $1::jsonb", bad)
        finally:
            await tx.rollback()
