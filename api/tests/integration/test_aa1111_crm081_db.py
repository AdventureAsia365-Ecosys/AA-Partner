"""AA-1111 (crm-081) on PostgreSQL with the RDS role shape: the setting crm.platform.admin_mfa_required is defined as
compiled (bool, off, platform, maker-checker, high impact, no env override), accepts true / false and refuses anything
else; crm-081 is registered. Rolled back; invented actor values only."""

from __future__ import annotations

import json

import asyncpg
import pytest

from app import platform_settings as ps

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

KEY = "crm.platform.admin_mfa_required"
STAFF = "platform:zz-staff-1"
REASON = "zz test reason long enough for the check"
PROPOSE = (
    "INSERT INTO crm.setting_value (key, value, version, proposed_by, reason) "
    "VALUES ($1, $2::jsonb, $3, $4, $5) RETURNING id"
)


async def test_the_switch_is_the_compiled_definition(admin: asyncpg.Connection) -> None:
    row = await admin.fetchrow(
        "SELECT value_type, default_value::text AS d, area, scope_kind, impact, approval, evidence_required,"
        " env_override, enforced FROM crm.setting_definition WHERE key = $1",
        KEY,
    )
    d = ps.READER.definition(ps.ADMIN_MFA_REQUIRED)
    assert ps.ADMIN_MFA_REQUIRED == KEY
    assert row["value_type"] == d.value_type == "bool"
    assert json.loads(row["d"]) is False and d.default_value is False
    assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
        "platform",
        "platform",
        "high",
        "maker_checker",
    )
    assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
    assert await admin.fetchval("SELECT 1 AS ok FROM shared.schema_versions WHERE version = 'crm-081'") == 1


async def test_a_value_is_true_or_false(app_pool: asyncpg.Pool) -> None:
    async with app_pool.acquire() as c:
        tx = c.transaction()
        await tx.start()
        try:
            await c.execute("SELECT set_config('app.actor', $1, true)", STAFF)
            assert await c.fetchval(PROPOSE, KEY, "true", 1, STAFF, REASON)
            for version, bad in enumerate(('"yes"', "1", "null"), start=2):
                with pytest.raises((asyncpg.CheckViolationError, asyncpg.NotNullViolationError)):
                    async with c.transaction():
                        await c.fetchval(PROPOSE, KEY, bad, version, STAFF, REASON)
        finally:
            await tx.rollback()
