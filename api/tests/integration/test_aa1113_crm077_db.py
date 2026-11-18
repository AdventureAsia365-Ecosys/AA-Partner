"""AA-1113 (crm-077) on PostgreSQL with the RDS role shape: the setting crm.ai.connection_monthly_cap_usd is defined as
compiled (json, {} default, entries 0..1000000), accepts an object of amounts and refuses anything else; crm-077 is
registered. Rolled back; invented connection keys and values only."""

from __future__ import annotations

import json

import asyncpg
import pytest

from app import platform_settings as ps

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

KEY = "crm.ai.connection_monthly_cap_usd"
STAFF = "platform:zz-staff-1"
REASON = "zz test reason long enough for the check"
PROPOSE = (
    "INSERT INTO crm.setting_value (key, value, version, proposed_by, reason) "
    "VALUES ($1, $2::jsonb, $3, $4, $5) RETURNING id"
)


async def test_the_cap_setting_is_the_compiled_definition(admin: asyncpg.Connection) -> None:
    row = await admin.fetchrow(
        "SELECT value_type, default_value::text AS d, min_value::float AS lo, max_value::float AS hi, unit, area,"
        " scope_kind, impact, approval, evidence_required, env_override, enforced"
        " FROM crm.setting_definition WHERE key = $1",
        KEY,
    )
    d = ps.READER.definition(ps.AI_CONNECTION_MONTHLY_CAP)
    assert ps.AI_CONNECTION_MONTHLY_CAP == KEY
    assert row["value_type"] == d.value_type == "json"
    assert json.loads(row["d"]) == d.default_value == {}
    assert (row["lo"], row["hi"], row["unit"], row["area"]) == (0.0, 1000000.0, "USD/month", "ai")
    assert (row["scope_kind"], row["impact"], row["approval"]) == ("platform", "high", "maker_checker")
    assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
    assert await admin.fetchval("SELECT 1 AS ok FROM shared.schema_versions WHERE version = 'crm-077'") == 1


async def test_a_value_is_an_object_of_amounts_within_bounds(app_pool: asyncpg.Pool) -> None:
    async with app_pool.acquire() as c:
        tx = c.transaction()
        await tx.start()
        try:
            await c.execute("SELECT set_config('app.actor', $1, true)", STAFF)
            version = 1
            assert await c.fetchval(PROPOSE, KEY, json.dumps({"zz-a": 20, "zz-b": 0}), version, STAFF, REASON)
            for bad in ({"zz-a": -1}, {"zz-a": 1000001}, {"zz-a": "lots"}, "zz"):
                version += 1
                with pytest.raises(asyncpg.CheckViolationError):
                    async with c.transaction():
                        await c.fetchval(PROPOSE, KEY, json.dumps(bad), version, STAFF, REASON)
        finally:
            await tx.rollback()
