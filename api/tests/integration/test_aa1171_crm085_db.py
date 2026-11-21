"""AA-1171 (crm-085) on PostgreSQL with the RDS role shape: the six crm.ops.* settings are defined exactly as compiled
(area ops, platform scope, single approval, enforced, today's constants as defaults), a value outside the bounds is
refused, crm-085 is registered, and the last-alert read of the ops check registry parses. Rolled back; invented actor
values only."""

from __future__ import annotations

import json

import asyncpg
import pytest

from app import platform_settings as ps
from app.jobs import queue
from app.observability import ops_alerts

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

KEYS = (
    ps.OPS_JOBS_STUCK_AFTER_S,
    ps.OPS_JOBS_OVERDUE_FACTOR,
    ps.OPS_AUDIT_EXPORT_LAG_MIN,
    ps.OPS_AUDIT_VERIFY_LAG_H,
    ps.OPS_AUDIT_PARTITION_MONTHS_AHEAD,
    ps.OPS_MEMBERSHIP_FEED_STUCK_FAILURES,
)
STAFF = "platform:zz-staff-1"
REASON = "zz test reason long enough for the check"
PROPOSE = (
    "INSERT INTO crm.setting_value (key, value, version, proposed_by, reason) "
    "VALUES ($1, $2::jsonb, $3, $4, $5) RETURNING id"
)


async def test_the_settings_are_the_compiled_definitions(admin: asyncpg.Connection) -> None:
    for key in KEYS:
        row = await admin.fetchrow(
            "SELECT value_type, default_value::text AS d, min_value::float8 AS lo, max_value::float8 AS hi, area,"
            " scope_kind, approval, env_override, enforced FROM crm.setting_definition WHERE key = $1",
            key,
        )
        d = ps.READER.definition(key)
        assert row is not None, key
        assert row["value_type"] == d.value_type and json.loads(row["d"]) == d.default_value, key
        assert (row["lo"], row["hi"]) == (float(d.min_value), float(d.max_value)), key
        assert (row["area"], row["scope_kind"], row["approval"]) == ("ops", "platform", "single"), key
        assert row["env_override"] is None and row["enforced"] is True, key
    assert await admin.fetchval("SELECT 1 AS ok FROM shared.schema_versions WHERE version = 'crm-085'") == 1


async def test_a_value_outside_the_bounds_is_refused(app_pool: asyncpg.Pool) -> None:
    async with app_pool.acquire() as c:
        tx = c.transaction()
        await tx.start()
        try:
            await c.execute("SELECT set_config('app.actor', $1, true)", STAFF)
            with pytest.raises(asyncpg.PostgresError):
                async with c.transaction():
                    await c.fetchval(PROPOSE, ps.OPS_JOBS_STUCK_AFTER_S, "60", 1, STAFF, REASON)
            assert await c.fetchval(PROPOSE, ps.OPS_JOBS_STUCK_AFTER_S, "1200", 1, STAFF, REASON) is not None
        finally:
            await tx.rollback()


async def test_the_last_alert_read_parses(admin: asyncpg.Connection) -> None:
    last = await queue.last_alerts(admin, ops_alerts.OPS_ALERT_KIND)  # type: ignore[arg-type]
    assert isinstance(last, dict)
