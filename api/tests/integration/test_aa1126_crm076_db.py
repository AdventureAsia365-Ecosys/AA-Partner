"""AA-1126 (crm-076) on PostgreSQL with the RDS role shape: crm.tenant_entitlement.ai_budget_usd exists, is NULL by
default and is checked to 0..10000; the setting crm.spend.environment is defined as compiled (dev | prod, default dev)
and refuses another value; the two budget keys carry their fallback descriptions; crm-076 is registered. Rolled back;
invented values only."""

from __future__ import annotations

import json

import asyncpg
import pytest

from app import platform_settings as ps

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_the_plan_budget_column_and_its_check(admin: asyncpg.Connection) -> None:
    col = await admin.fetchrow(
        "SELECT data_type, numeric_precision, numeric_scale, is_nullable, column_default"
        " FROM information_schema.columns"
        " WHERE table_schema = 'crm' AND table_name = 'tenant_entitlement' AND column_name = 'ai_budget_usd'"
    )
    assert (col["data_type"], col["numeric_precision"], col["numeric_scale"]) == ("numeric", 10, 2)
    assert col["is_nullable"] == "YES" and col["column_default"] is None
    check = await admin.fetchval(
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conname = 'tenant_entitlement_ai_budget_chk'"
    )
    assert "ai_budget_usd" in check and "10000" in check
    assert await admin.fetchval(
        "SELECT has_column_privilege('crm_app', 'crm.tenant_entitlement', 'ai_budget_usd', 'UPDATE') AS ok"
    )


async def test_the_spend_setting_is_the_compiled_definition(admin: asyncpg.Connection) -> None:
    row = await admin.fetchrow(
        "SELECT value_type, allowed_values::text AS allowed, default_value::text AS d, scope_kind, approval, enforced"
        " FROM crm.setting_definition WHERE key = 'crm.spend.environment'"
    )
    d = ps.READER.definition(ps.SPEND_ENVIRONMENT)
    assert row["value_type"] == d.value_type == "enum"
    assert json.loads(row["allowed"]) == list(d.allowed_values) == ["dev", "prod"]
    assert json.loads(row["d"]) == d.default_value == "dev"
    assert (row["scope_kind"], row["approval"], row["enforced"]) == ("platform", "maker_checker", True)
    for key in (ps.AI_BUDGET_DEFAULT, ps.AI_BUDGET_MAX):
        desc = await admin.fetchval("SELECT description FROM crm.setting_definition WHERE key = $1", key)
        assert desc == ps.READER.definition(key).description and "crm.ai_budget_usd_monthly" in desc
    assert await admin.fetchval("SELECT 1 AS ok FROM shared.schema_versions WHERE version = 'crm-076'") == 1
