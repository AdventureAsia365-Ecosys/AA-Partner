"""crm-032 (AA-964 CRM-33a, a contract file) on PostgreSQL with the RDS role shape.

- ``crm.llm_call`` gets the root ADR 0009 ledger v2 columns: ``app`` is always ``crm`` (old INSERTs that name only
  the crm-005 columns still work and get ``app = 'crm'``, ``is_shadow = is_experiment = false``), the id / unit /
  pricing CHECKs refuse bad values, the role CHECK admits ``guard`` (the Bedrock Guardrails row).
- Unchanged: the table stays append-only for ``crm_app`` (UPDATE / DELETE refused), FORCE RLS keeps each agency's rows
  apart, the readers get nothing.
- ``crm.tenant_settings.ai_alert_month`` / ``ai_alert_pct``: both or neither, 75 / 90 / 100, the first of a month.
"""

from __future__ import annotations

import datetime as dt
import uuid

import asyncpg
import pytest

from tests.integration.conftest import READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

OLD_INSERT = (
    "INSERT INTO crm.llm_call (stage, role, provider, model, input_tokens, output_tokens, cost_usd_est, latency_ms, "
    "outcome, refusal_code, error_class, scrub_receipt) "
    "VALUES ('crm_draft_reply', 'writer', 'stub', 'stub-echo-1', 3, 6, 0, 1, 'ok', NULL, NULL, '{}'::jsonb) "
    "RETURNING llm_call_id"
)
V2_COLUMNS = (
    "app",
    "attempt_no",
    "request_id",
    "job_id",
    "connection_key",
    "deployment_wire_id",
    "family",
    "cache_read_tokens",
    "cache_write_tokens",
    "units",
    "unit_kind",
    "pricing_mode",
    "prompt_hash",
    "prompt_version",
    "is_shadow",
    "is_experiment",
)


async def _insert(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: object) -> object:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, "member:test")
        return await conn.fetchval(sql, *args)


def _guard_row(**overrides: object) -> tuple[str, tuple[object, ...]]:
    values = {
        "stage": "crm_scrub",
        "role": "guard",
        "provider": "bedrock_guardrails",
        "model": "apply_guardrail",
        "units": 2,
        "unit_kind": "text_unit",
        "pricing_mode": "code_table",
        "request_id": "0a1b2c3d-1111-4222-8333-444455556666",
        "prompt_hash": "ab" * 32,
        "connection_key": "bedrock_guardrails",
        **overrides,
    }
    names = ", ".join(values)
    marks = ", ".join(f"${i}" for i in range(1, len(values) + 1))
    sql = f"INSERT INTO crm.llm_call ({names}, outcome) VALUES ({marks}, 'ok') RETURNING llm_call_id"  # noqa: S608
    return sql, tuple(values.values())


async def test_crm032_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-032'") == 1


async def test_the_v2_columns_exist_with_their_defaults(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(
        "SELECT a.attname AS name, a.attnotnull AS not_null, pg_get_expr(d.adbin, d.adrelid) AS default_expr "
        "FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "WHERE a.attrelid = 'crm.llm_call'::regclass AND a.attnum > 0 AND NOT a.attisdropped"
    )
    columns = {r["name"]: r for r in rows}
    for name in V2_COLUMNS:
        assert name in columns, name
    assert columns["app"]["not_null"] and columns["app"]["default_expr"] == "'crm'::text"
    for flag in ("is_shadow", "is_experiment"):
        assert columns[flag]["not_null"] and columns[flag]["default_expr"] == "false"
    for name in set(V2_COLUMNS) - {"app", "is_shadow", "is_experiment"}:
        assert not columns[name]["not_null"], name


async def test_an_old_insert_still_works_and_reads_as_a_crm_row(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    call = await _insert(app_pool, ids["tenant"], OLD_INSERT)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        row = await conn.fetchrow(
            "SELECT app, is_shadow, is_experiment, attempt_no FROM crm.llm_call WHERE llm_call_id = $1", call
        )
    assert dict(row) == {"app": "crm", "is_shadow": False, "is_experiment": False, "attempt_no": None}


async def test_a_guard_row_with_units_is_accepted(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    sql, args = _guard_row()
    assert await _insert(app_pool, ids["tenant"], sql, *args)


@pytest.mark.parametrize(
    "overrides",
    [
        {"role": "poet"},
        {"units": 2, "unit_kind": None},
        {"units": None, "unit_kind": "text_unit"},
        {"units": -1},
        {"pricing_mode": "guess"},
        {"prompt_hash": "not-a-hash"},
        {"request_id": "has space"},
        {"connection_key": "Bedrock"},
        {"attempt_no": 0},
        {"cache_read_tokens": -5},
        {"app": "cis"},
        {"family": "Has Upper"},
        {"deployment_wire_id": "bad id with spaces"},
        {"prompt_version": "v 1"},
    ],
)
async def test_bad_v2_values_are_refused(app_pool: asyncpg.Pool, overrides: dict[str, object]) -> None:
    ids = await _agency(app_pool)
    sql, args = _guard_row(**overrides)
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(app_pool, ids["tenant"], sql, *args)


async def test_the_ledger_stays_append_only_and_private(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    one, two = await _agency(app_pool), await _agency(app_pool)
    sql, args = _guard_row()
    call = await _insert(app_pool, one["tenant"], sql, *args)
    for statement in (
        "UPDATE crm.llm_call SET units = 9 WHERE llm_call_id = $1",
        "DELETE FROM crm.llm_call WHERE llm_call_id = $1",
    ):
        with pytest.raises(asyncpg.PostgresError):
            await _insert(app_pool, one["tenant"], statement, call)
    before_two = await _count(app_pool, two["tenant"], "llm_call")
    assert await _count(app_pool, one["tenant"], "llm_call") >= 2  # the seed call + the guard row
    assert await _count(app_pool, two["tenant"], "llm_call") == before_two  # the guard row is not visible to two
    assert await admin.fetchval(
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = 'crm.llm_call'::regclass"
    )
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'crm.llm_call'::regclass "
            "AND tgname IN ('llm_call_append_only', 'llm_call_no_truncate')"
        )
        == 2
    )
    for reader in READERS:
        assert not await admin.fetchval("SELECT has_table_privilege($1, 'crm.llm_call', 'SELECT')", reader)


async def test_the_budget_alert_columns(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    upsert = (
        "INSERT INTO crm.tenant_settings (ai_alert_month, ai_alert_pct) VALUES ($1::date, $2) "
        "ON CONFLICT (tenant_id) DO UPDATE SET ai_alert_month = EXCLUDED.ai_alert_month, "
        "ai_alert_pct = EXCLUDED.ai_alert_pct RETURNING ai_alert_pct"
    )
    first = dt.date(2026, 10, 1)
    assert await _insert(app_pool, ids["tenant"], upsert, first, 75) == 75
    assert await _insert(app_pool, ids["tenant"], upsert, first, 90) == 90
    for month, pct in ((first, 80), (dt.date(2026, 10, 15), 75), (None, 75), (first, None)):
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert(app_pool, ids["tenant"], upsert, month, pct)
