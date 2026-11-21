"""crm-084 (AA-1147): the two settings of the agency's own services, registered and re-runnable as crm_owner; the
compiled defaults the readers serve until a value is approved are the seed's (test_aa1096_settings_seed_parity)."""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

from tests.integration.conftest import OWNER

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "084_crm_agency_lines_settings.sql"


async def test_the_settings_are_defined(admin: asyncpg.Connection) -> None:
    rows = {
        r["key"]: dict(r)
        for r in await admin.fetch(
            "SELECT key, value_type, scope_kind, min_value, max_value, default_value::text AS default_text "
            "FROM crm.setting_definition WHERE key LIKE 'crm.agency_lines.%'"
        )
    }
    lead = rows["crm.agency_lines.due_lead_days"]
    assert (lead["value_type"], lead["min_value"], lead["max_value"], lead["default_text"]) == ("int", 0, 60, "3")
    assert rows["crm.agency_lines.disclaimer"]["value_type"] == "json"
    assert '"vi"' in rows["crm.agency_lines.disclaimer"]["default_text"]
    assert {r["scope_kind"] for r in rows.values()} == {"platform+tenant"}
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-084'") == 1


async def test_the_file_is_re_runnable(admin: asyncpg.Connection) -> None:
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    count = await admin.fetchval("SELECT count(*) FROM crm.setting_definition WHERE key LIKE 'crm.agency_lines.%'")
    assert count == 2
