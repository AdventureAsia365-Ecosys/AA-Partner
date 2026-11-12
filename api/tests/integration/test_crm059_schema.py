"""crm-059 (AA-1070 item crm-35, an expand file) on PostgreSQL with the RDS role shape: the proposal inclusion lists.

- ``crm.inclusion_item``: kind / text / sort checks, one live line per (agency, kind, English text) ignoring case and
  outer spaces, archived flag; cascades from the agency's settings row (the agency erase).
- ``crm.proposal_inclusion``: the three sources and their columns, one pick of a library line per version; changes
  only while its proposal is a draft (CRM35), so a published version keeps its lists; a trip hold guards it (CRM10);
  the trip erase takes it with the proposal.
- Tenant isolation: another agency sees neither table and cannot hang a line on a foreign version.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase, _hold
from tests.integration.test_crm019_schema import _builder, _publish, _run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "059_crm_proposal_inclusions.sql"
TABLES = ("inclusion_item", "proposal_inclusion")
ITEM_SQL = "INSERT INTO crm.inclusion_item (kind, text_en, text_vi) VALUES ($1, $2, $3) RETURNING inclusion_item_id"
LINE_SQL = (
    "INSERT INTO crm.proposal_inclusion (proposal_id, trip_id, kind, source, inclusion_item_id, text_en) "
    "VALUES ($1, $2, $3, $4, $5, $6) RETURNING proposal_inclusion_id"
)


async def _settings(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    await _run(pool, tenant, "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING")


async def _item(pool: asyncpg.Pool, tenant: uuid.UUID, text: str, kind: str = "included") -> Any:
    return await _run(pool, tenant, ITEM_SQL, kind, text, None)


async def test_registered_an_expand_file_with_the_grants(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-059'") == 1
    assert MIGRATION.read_text(encoding="utf-8").startswith("-- migrate: expand\n")
    for table in TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert await admin.fetchval("SELECT has_table_privilege('crm_app', $1, $2)", f"crm.{table}", privilege)
            for reader in READERS:
                assert not await admin.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege
                )
        row = await admin.fetchrow(
            "SELECT relrowsecurity AS rls, relforcerowsecurity AS forced FROM pg_class WHERE oid = $1::regclass",
            f"crm.{table}",
        )
        assert row["rls"] and row["forced"], table


async def test_the_library_checks_its_lines(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    await _settings(app_pool, t)
    first = await _run(app_pool, t, ITEM_SQL, "not_included", "International flights", "Vé máy bay quốc tế")
    assert first is not None
    with pytest.raises(asyncpg.UniqueViolationError):  # one live line per kind and text, case and spaces ignored
        await _run(app_pool, t, ITEM_SQL, "not_included", "  international FLIGHTS ", None)
    assert await _run(app_pool, t, ITEM_SQL, "included", "International flights", None) is not None  # other kind
    for kind, text_en, text_vi in (
        ("maybe", "Breakfast daily", None),
        ("included", "   ", None),
        ("included", "x" * 501, None),
        ("included", "Line\nbreak", None),
        ("included", "Lunch on day 2", ""),
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, ITEM_SQL, kind, text_en, text_vi)
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, t, "UPDATE crm.inclusion_item SET sort_order = 1000 WHERE inclusion_item_id = $1", first)
    await _run(app_pool, t, "UPDATE crm.inclusion_item SET archived = true WHERE inclusion_item_id = $1", first)
    with pytest.raises(asyncpg.UniqueViolationError):  # an archived line still holds its text
        await _run(app_pool, t, ITEM_SQL, "not_included", "International flights", None)


async def test_the_three_sources_and_one_pick_per_version(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    await _settings(app_pool, t)
    b = await _builder(app_pool, ids)
    p, trip = b["proposal"], ids["trip"]
    item = await _item(app_pool, t, "Airport transfers")
    await _run(app_pool, t, LINE_SQL, p, trip, "included", "library", item, "Airport transfers")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(app_pool, t, LINE_SQL, p, trip, "included", "library", item, "Airport transfers")
    await _run(app_pool, t, LINE_SQL, p, trip, "not_included", "custom", None, "Tips for the boat crew")
    await _run(
        app_pool, t,
        "INSERT INTO crm.proposal_inclusion (proposal_id, trip_id, kind, source, aa_tour_source_id, aa_tour_title, "
        "text_en) VALUES ($1, $2, 'included', 'aa_tour', 'cis-tour-0042', 'Lantern Valley Loop', 'Local guide')",
        p, trip,
    )  # fmt: skip
    for source, item_id in (("library", None), ("custom", item), ("aa_tour", None), ("agency", None)):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, LINE_SQL, p, trip, "included", source, item_id, "Water bottle")
    assert await _count(app_pool, t, "proposal_inclusion") == 3


async def test_the_lines_change_only_in_a_draft(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    b = await _builder(app_pool, ids)
    p, trip = b["proposal"], ids["trip"]
    line = await _run(app_pool, t, LINE_SQL, p, trip, "included", "custom", None, "Daily breakfast")
    await _run(app_pool, t, "UPDATE crm.proposal_inclusion SET sort_order = 1 WHERE proposal_inclusion_id = $1", line)
    await _publish(app_pool, ids, p)
    for sql, args in (
        (LINE_SQL, (p, trip, "not_included", "custom", None, "Visa fees")),
        ("UPDATE crm.proposal_inclusion SET text_en = 'Dinner' WHERE proposal_inclusion_id = $1", (line,)),
        ("DELETE FROM crm.proposal_inclusion WHERE proposal_inclusion_id = $1", (line,)),
    ):
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, t, sql, *args)
        assert err.value.sqlstate == "CRM35"
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, t)
        assert await conn.fetchval("SELECT text_en FROM crm.proposal_inclusion") == "Daily breakfast"


async def test_another_agency_sees_nothing_and_cannot_attach(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    await _settings(app_pool, ids["tenant"])
    await _settings(app_pool, other["tenant"])
    b = await _builder(app_pool, ids)
    await _item(app_pool, ids["tenant"], "Entrance fees")
    await _run(app_pool, ids["tenant"], LINE_SQL, b["proposal"], ids["trip"], "included", "custom", None, "Guide")
    for table in TABLES:
        assert await _count(app_pool, ids["tenant"], table) == 1
        assert await _count(app_pool, other["tenant"], table) == 0
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _run(app_pool, other["tenant"], LINE_SQL, b["proposal"], ids["trip"], "included", "custom", None, "X")
    # The same text in another agency's library is no clash.
    assert await _item(app_pool, other["tenant"], "Entrance fees") is not None


async def test_the_trip_and_agency_erase_take_the_lines(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    await _settings(app_pool, t)
    b = await _builder(app_pool, ids)
    await _run(app_pool, t, LINE_SQL, b["proposal"], ids["trip"], "included", "custom", None, "Boat ride")
    await _publish(app_pool, ids, b["proposal"])  # a published version's lines are erased with it (app.erasing)
    await _erase(app_pool, t, "trip", ids["trip"])
    assert await _count(app_pool, t, "proposal_inclusion") == 0
    again = await _agency(app_pool)
    await _settings(app_pool, again["tenant"])
    b = await _builder(app_pool, again)
    await _item(app_pool, again["tenant"], "Hotel taxes")
    await _run(app_pool, again["tenant"], LINE_SQL, b["proposal"], again["trip"], "included", "custom", None, "Tea")
    await _run(app_pool, again["tenant"], "UPDATE crm.tenant SET status = 'offboarding'", actor="seed:test")
    await _erase(app_pool, again["tenant"], "tenant", again["tenant"])
    for table in TABLES:
        assert await _count(app_pool, again["tenant"], table) == 0


async def test_a_trip_hold_guards_the_lines(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    b = await _builder(app_pool, ids)
    line = await _run(app_pool, t, LINE_SQL, b["proposal"], ids["trip"], "included", "custom", None, "Snacks")
    await _hold(app_pool, t, "trip", ids["trip"], "member:m")
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(app_pool, t, "DELETE FROM crm.proposal_inclusion WHERE proposal_inclusion_id = $1", line)
    assert err.value.sqlstate == "CRM10"
    assert await _count(app_pool, t, "proposal_inclusion") == 1
