"""crm-040 (AA-964 S263 F1 + F2, a contract file) on PostgreSQL with the RDS role shape.

- ``crm.llm_stage_profile`` / ``crm.llm_stage_route``: platform configuration (no tenant column), seeded with the draft
  stage (writer, active, two Bedrock attempts with family and catalog prices); the CHECKs refuse a bad stage, role,
  attempt count, deadline, provider, half a price; ``crm_app`` may only read them; no reader gets anything.
- ``crm.llm_call`` accepts the ledger v2 statuses ``truncated`` / ``billed_unusable`` and still refuses anything else.
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.conftest import READERS
from tests.integration.test_crm010_schema import _agency, _ctx

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ROW_SQL = (
    "INSERT INTO crm.llm_call (stage, role, provider, model, input_tokens, output_tokens, cost_usd_est, latency_ms, "
    "outcome, refusal_code, error_class, scrub_receipt, attempt_no, family) "
    "VALUES ('crm_draft_reply', 'writer', 'stub', 'us.maker.model-v1:0', 3, 6, 0, 1, $1, NULL, $2, '{}'::jsonb, 1, "
    "'maker') RETURNING llm_call_id"
)


async def test_crm040_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-040'") == 1


async def test_the_draft_stage_is_seeded_with_a_two_step_route(admin: asyncpg.Connection) -> None:
    profile = await admin.fetchrow(
        "SELECT role, is_active, max_attempts, route_deadline_s, degrade_mode FROM crm.llm_stage_profile "
        "WHERE stage = 'crm_draft_reply'"
    )
    assert dict(profile) == {
        "role": "writer",
        "is_active": True,
        "max_attempts": 2,
        "route_deadline_s": 30,
        "degrade_mode": "manual_reply_needed",
    }
    route = await admin.fetch(
        "SELECT position, provider, family, price_in_per_mtok > 0 AS priced FROM crm.llm_stage_route "
        "WHERE stage = 'crm_draft_reply' ORDER BY position"
    )
    assert [(r["position"], r["provider"], r["family"], r["priced"]) for r in route] == [
        (0, "bedrock", "anthropic", True),
        (1, "bedrock", "anthropic", True),
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO crm.llm_stage_profile (stage, role) VALUES ('crm_scrub', 'writer')",
        "INSERT INTO crm.llm_stage_profile (stage, role) VALUES ('cis_x', 'writer')",
        "INSERT INTO crm.llm_stage_profile (stage, role) VALUES ('crm_x', 'guard')",
        "INSERT INTO crm.llm_stage_profile (stage, role, max_attempts) VALUES ('crm_x', 'writer', 7)",
        "INSERT INTO crm.llm_stage_profile (stage, role, route_deadline_s) VALUES ('crm_x', 'writer', 0)",
        "INSERT INTO crm.llm_stage_profile (stage, role, degrade_mode) VALUES ('crm_x', 'writer', 'retry')",
        "INSERT INTO crm.llm_stage_route (stage, position, provider, model) VALUES ('crm_draft_reply', 4, 'openai', "
        "'m-1')",
        "INSERT INTO crm.llm_stage_route (stage, position, model) VALUES ('crm_draft_reply', 6, 'm-1')",
        "INSERT INTO crm.llm_stage_route (stage, position, model, price_in_per_mtok) VALUES ('crm_draft_reply', 4, "
        "'m-1', 1)",
        "INSERT INTO crm.llm_stage_route (stage, position, model, price_in_per_mtok, price_out_per_mtok) VALUES "
        "('crm_draft_reply', 4, 'm-1', 0, 1)",
        "INSERT INTO crm.llm_stage_route (stage, position, model, family) VALUES ('crm_draft_reply', 4, 'm-1', "
        "'Maker')",
        "INSERT INTO crm.llm_stage_route (stage, position, model) VALUES ('crm_draft_reply', 4, 'bad model id')",
    ],
)
async def test_bad_profile_or_route_values_are_refused(admin: asyncpg.Connection, sql: str) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(sql)


async def test_crm_app_reads_the_profiles_and_cannot_change_them(app_pool: asyncpg.Pool) -> None:
    async with app_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT p.stage AS stage, r.position AS position FROM crm.llm_stage_profile p "
            "JOIN crm.llm_stage_route r USING (stage) WHERE p.stage = 'crm_draft_reply'"
        )
        assert len(rows) == 2
        for sql in (
            "UPDATE crm.llm_stage_profile SET is_active = false",
            "INSERT INTO crm.llm_stage_profile (stage, role) VALUES ('crm_x', 'writer')",
            "DELETE FROM crm.llm_stage_route",
            "UPDATE crm.llm_stage_route SET is_enabled = false",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(sql)


async def test_readers_get_nothing_on_the_profiles(admin: asyncpg.Connection) -> None:
    for role in READERS:
        for table in ("crm.llm_stage_profile", "crm.llm_stage_route"):
            allowed = await admin.fetchval("SELECT has_table_privilege($1, $2, 'SELECT') AS ok", role, table)
            assert allowed is False, (role, table)


@pytest.mark.parametrize(("outcome", "error_class"), [("truncated", "truncated"), ("billed_unusable", "other")])
async def test_the_ledger_takes_the_v2_statuses(app_pool: asyncpg.Pool, outcome: str, error_class: str) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "member:test")
        assert await conn.fetchval(ROW_SQL, outcome, error_class)


async def test_the_ledger_still_refuses_an_unknown_status(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "member:test")
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.fetchval(ROW_SQL, "maybe", None)
