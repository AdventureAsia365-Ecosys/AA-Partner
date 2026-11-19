"""crm-042 (AA-1027 F4 interim path, a contract file) on PostgreSQL with the RDS role shape.

- The draft stage's Amazon Nova Lite entry on acc2 (position 2, family amazon) is enabled, priced exactly as the CIS
  catalogue row ``nova-lite`` (0.077 / 0.308 USD per million tokens): the ledger price matches the catalogue. Since
  crm-078 (AA-1138) it is the fallback behind Claude Haiku 5.5 on acc1 at position 0 (``test_crm078_schema.py``).
- The crm-037 Claude Sonnet entry (position 1) is kept (model, family, prices) but switched off.
- The profile itself (writer, kill switch on, two attempts, 30 s) is unchanged.
- The file is idempotent: a second run as ``crm_owner`` on the route it left changes nothing and its guard still passes
  (crm-078 is then applied again to restore the current route).
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


async def test_the_draft_stage_keeps_nova_lite_enabled_at_position_2(admin: asyncpg.Connection) -> None:
    rows = {r["position"]: r for r in await admin.fetch(ROUTE_SQL)}
    nova = rows[2]
    assert (nova["provider"], nova["model"], nova["family"], nova["is_enabled"]) == (
        "bedrock",
        NOVA_LITE,
        "amazon",
        True,
    )
    assert (nova["price_in"], nova["price_out"]) == (Decimal("0.0770"), Decimal("0.3080"))


async def test_the_claude_sonnet_entry_is_kept_switched_off(admin: asyncpg.Connection) -> None:
    row = {r["position"]: r for r in await admin.fetch(ROUTE_SQL)}[1]
    assert row["family"] == "anthropic" and row["model"].startswith("us.anthropic.")
    assert row["is_enabled"] is False and row["price_in"] > 0 and row["price_out"] > 0


async def test_the_profile_is_unchanged(admin: asyncpg.Connection) -> None:
    profile = await admin.fetchrow(
        "SELECT role, is_active, max_attempts, route_deadline_s FROM crm.llm_stage_profile "
        "WHERE stage = 'crm_draft_reply'"
    )
    assert dict(profile) == {"role": "writer", "is_active": True, "max_attempts": 2, "route_deadline_s": 30}


async def _run_as_owner(admin: asyncpg.Connection, sql: str) -> None:
    # The file carries its own BEGIN / COMMIT; run it as the owner the way Migration Apply does.
    await admin.execute("SET ROLE crm_owner")
    try:
        await admin.execute(sql)
    finally:
        await admin.execute("RESET ROLE")


async def test_a_second_run_changes_nothing(admin: asyncpg.Connection) -> None:
    final = [dict(r) for r in await admin.fetch(ROUTE_SQL)]
    # crm-078 switched position 0 on later; put the route back the way crm-042 left it (one enabled entry) first.
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute(
            "UPDATE crm.llm_stage_route SET is_enabled = false WHERE stage = 'crm_draft_reply' AND position = 0"
        )
    before = [dict(r) for r in await admin.fetch(ROUTE_SQL)]
    await _run_as_owner(admin, MIGRATION.read_text())
    assert [dict(r) for r in await admin.fetch(ROUTE_SQL)] == before
    # crm-078 again restores the route the later tests expect.
    await _run_as_owner(admin, MIGRATION.with_name("078_crm_draft_route_haiku_acc1.sql").read_text())
    assert [dict(r) for r in await admin.fetch(ROUTE_SQL)] == final
