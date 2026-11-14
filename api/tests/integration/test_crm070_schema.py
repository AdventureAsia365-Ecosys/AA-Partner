"""AA-1096 lane l5 (S281) — crm-070: the one-release env overrides of the CRM settings registry are retired (conftest
replays every migration as crm_owner). Registered; no definition names an env var; each definition equals the compiled
default the reader serves; a re-run changes nothing. Invented values only."""

from __future__ import annotations

import json
from pathlib import Path

import asyncpg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

API = Path(__file__).resolve().parents[2]
M070 = API / "migrations" / "070_crm_retire_settings_env_overrides.sql"
DEFAULTS = API / "app" / "platform_settings" / "compiled_defaults.json"
RETIRED = API / "tests" / "unit" / "snapshots" / "aa1096_retired_env_overrides.json"


async def test_crm070_is_registered_and_no_definition_names_an_env_var(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-070'") == 1
    assert await admin.fetchval("SELECT count(*) FROM crm.setting_definition WHERE env_override IS NOT NULL") == 0


async def test_the_edited_rows_equal_the_compiled_defaults(admin: asyncpg.Connection) -> None:
    retired = json.loads(RETIRED.read_text(encoding="utf-8"))
    by_key = {d["key"]: d for d in json.loads(DEFAULTS.read_text(encoding="utf-8"))["definitions"]}
    rows = await admin.fetch("SELECT key, description FROM crm.setting_definition WHERE key = ANY($1::text[])",
                             list(retired))  # fmt: skip
    assert len(rows) == len(retired) == 7
    for r in rows:
        assert r["description"] == by_key[r["key"]]["description"], r["key"]
        assert not any(env in r["description"] for env in retired.values()), r["key"]


async def test_a_rerun_changes_nothing(admin: asyncpg.Connection) -> None:
    sql = "SELECT key, env_override, description FROM crm.setting_definition ORDER BY key"
    before = [tuple(r) for r in await admin.fetch(sql)]
    await admin.execute("SET ROLE crm_owner")
    try:
        await admin.execute(M070.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert [tuple(r) for r in await admin.fetch(sql)] == before
