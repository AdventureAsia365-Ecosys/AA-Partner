"""crm-042 (AA-1027 F4 interim path, a contract file) on PostgreSQL with the RDS role shape.

- The draft stage's only enabled route entry is Amazon Nova Lite on acc2 (position 2, family amazon), priced exactly as
  the CIS catalogue row ``nova-lite`` (0.077 / 0.308 USD per million tokens): the ledger price matches the catalogue.
- The crm-037 Claude entries are kept (model, family, prices) but switched off, ready for the acc3 return.
- The profile itself (writer, kill switch on, two attempts, 30 s) is unchanged.
- The file is idempotent: a second run as ``crm_owner`` changes nothing and its guard still passes.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "042_crm_draft_route_acc2_nova_lite.sql"
NOVA_LITE = "us.amazon.nova-lite-v1:0"
ROUTE_SQL = (
    "SELECT position, provider, model, family, price_in_per_mtok AS price_in, price_out_per_mtok AS price_out, "
    "is_enabled FROM crm.llm_stage_route WHERE stage = 'crm_draft_reply' ORDER BY position"
)


async def test_crm042_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 AS ok FROM shared.schema_versions WHERE version = 'crm-042'") == 1


async def test_the_draft_stage_routes_to_nova_lite_only(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(ROUTE_SQL)
    enabled = [r for r in rows if r["is_enabled"]]
    assert [(r["position"], r["provider"], r["model"], r["family"]) for r in enabled] == [
        (2, "bedrock", NOVA_LITE, "amazon")
    ]
    [nova] = enabled
    assert (nova["price_in"], nova["price_out"]) == (Decimal("0.0770"), Decimal("0.3080"))


async def test_the_claude_entries_are_kept_switched_off(admin: asyncpg.Connection) -> None:
    rows = {r["position"]: r for r in await admin.fetch(ROUTE_SQL)}
    for position in (0, 1):
        row = rows[position]
        assert row["family"] == "anthropic" and row["model"].startswith("us.anthropic.")
        assert row["is_enabled"] is False and row["price_in"] > 0 and row["price_out"] > 0


async def test_the_profile_is_unchanged(admin: asyncpg.Connection) -> None:
    profile = await admin.fetchrow(
        "SELECT role, is_active, max_attempts, route_deadline_s FROM crm.llm_stage_profile "
        "WHERE stage = 'crm_draft_reply'"
    )
    assert dict(profile) == {"role": "writer", "is_active": True, "max_attempts": 2, "route_deadline_s": 30}


async def test_a_second_run_changes_nothing(admin: asyncpg.Connection) -> None:
    before = [dict(r) for r in await admin.fetch(ROUTE_SQL)]
    sql = MIGRATION.read_text()
    # The file carries its own BEGIN / COMMIT; run it as the owner the way DB Migrate does.
    await admin.execute("SET ROLE crm_owner")
    try:
        await admin.execute(sql)
    finally:
        await admin.execute("RESET ROLE")
    after = [dict(r) for r in await admin.fetch(ROUTE_SQL)]
    assert after == before
