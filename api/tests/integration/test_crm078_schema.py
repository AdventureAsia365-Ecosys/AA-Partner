"""crm-078 (AA-1138, a contract file) on PostgreSQL with the RDS role shape.

- ``crm.llm_stage_route`` has ``connection_key`` (NULL = the CRM process's own connection) and ``sends_temperature``
  (default true): every route entry that existed before keeps NULL / true.
- The connection key CHECK takes NULL or a short lower-case key only.
- crm_draft_reply: Claude Haiku 5.5 on ``bedrock_acc1`` first (priced as the CIS catalogue row ``haiku-5-5``, no
  temperature), then Amazon Nova Lite on the process's own connection; the profile lets both run.
- The copies of crm-069 (notes options, supplier confirm) keep Nova Lite only.
- The file is idempotent: a second run as ``crm_owner`` changes nothing and its guard still passes.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "078_crm_draft_route_haiku_acc1.sql"
HAIKU = "us.anthropic.claude-haiku-5-5"
NOVA_LITE = "us.amazon.nova-lite-v1:0"
ROUTE_SQL = (
    "SELECT position, provider, model, family, connection_key, sends_temperature, price_in_per_mtok AS price_in, "
    "price_out_per_mtok AS price_out, is_enabled FROM crm.llm_stage_route WHERE stage = $1 ORDER BY position"
)


async def test_crm078_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 AS ok FROM shared.schema_versions WHERE version = 'crm-078'") == 1


async def test_the_route_columns_exist_with_their_defaults(admin: asyncpg.Connection) -> None:
    cols = {
        r["column_name"]: r
        for r in await admin.fetch(
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'crm' AND table_name = 'llm_stage_route' "
            "AND column_name IN ('connection_key', 'sends_temperature')"
        )
    }
    assert (cols["connection_key"]["data_type"], cols["connection_key"]["is_nullable"]) == ("text", "YES")
    assert (cols["sends_temperature"]["data_type"], cols["sends_temperature"]["is_nullable"]) == ("boolean", "NO")
    assert cols["sends_temperature"]["column_default"] == "true"


async def test_the_draft_route_is_haiku_on_acc1_then_nova_lite(admin: asyncpg.Connection) -> None:
    rows = [r for r in await admin.fetch(ROUTE_SQL, "crm_draft_reply") if r["is_enabled"]]
    assert [
        (r["position"], r["provider"], r["model"], r["family"], r["connection_key"], r["sends_temperature"])
        for r in rows
    ] == [
        (0, "bedrock", HAIKU, "anthropic", "bedrock_acc1", False),
        (2, "bedrock", NOVA_LITE, "amazon", None, True),
    ]
    assert (rows[0]["price_in"], rows[0]["price_out"]) == (Decimal("0.1100"), Decimal("0.5500"))
    attempts = await admin.fetchval("SELECT max_attempts FROM crm.llm_stage_profile WHERE stage = 'crm_draft_reply'")
    assert attempts >= 2


@pytest.mark.parametrize("stage", ["crm_notes_options", "crm_supplier_confirm"])
async def test_the_closed_list_stages_keep_nova_lite_only(admin: asyncpg.Connection, stage: str) -> None:
    rows = [r for r in await admin.fetch(ROUTE_SQL, stage) if r["is_enabled"]]
    assert [(r["model"], r["connection_key"], r["sends_temperature"]) for r in rows] == [(NOVA_LITE, None, True)]


@pytest.mark.parametrize("key", ["ACC1", "1acc", "acc 1", "a" * 32])
async def test_the_connection_key_check_refuses_a_bad_key(admin: asyncpg.Connection, key: str) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        with pytest.raises(asyncpg.CheckViolationError):
            async with admin.transaction():
                await admin.execute(
                    "UPDATE crm.llm_stage_route SET connection_key = $1 WHERE stage = 'crm_draft_reply' "
                    "AND position = 1",
                    key,
                )


async def test_a_second_run_changes_nothing(admin: asyncpg.Connection) -> None:
    before = [dict(r) for r in await admin.fetch(ROUTE_SQL, "crm_draft_reply")]
    stamp = await admin.fetchval(
        "SELECT updated_at FROM crm.llm_stage_route WHERE stage = 'crm_draft_reply' AND position = 0"
    )
    await admin.execute("SET ROLE crm_owner")
    try:
        await admin.execute(MIGRATION.read_text())
    finally:
        await admin.execute("RESET ROLE")
    assert [dict(r) for r in await admin.fetch(ROUTE_SQL, "crm_draft_reply")] == before
    assert (
        await admin.fetchval(
            "SELECT updated_at FROM crm.llm_stage_route WHERE stage = 'crm_draft_reply' AND position = 0"
        )
        == stamp
    )
