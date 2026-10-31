"""crm-031 (AA-966 CRM-34, spec module 17) on PostgreSQL 15 with the RDS role shape: the trip-level insight fact.

- ``crm.v_insight_trip_fact`` counts a trip ONCE whatever its destinations (``crm.v_insight_fact`` has one row per
  destination): trips per agency, month and band combination in ``trip_count``; a trashed trip never counts; no trip
  id, no token, no destination.
- Only ``crm_insights`` of the readers selects it; nothing is written through it (GROUP BY: not updatable); FORCE RLS
  on ``crm.trip`` keeps a read inside the agency of ``app.tenant_id``.
- The file is an expand file (ADR 0017 header) and registers its key.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.tenancy import tenant_tx
from tests.integration.test_crm006_schema import _agency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "031_crm_insight_trip_fact.sql"
START = dt.date(2031, 5, 14)  # far from today: no other test's trips share its month


async def _trip(conn: asyncpg.Connection, destinations: list[str], adults: int, band: str = "over_5000") -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO crm.trip (title, stage, waiting_on, destinations, party_adults, start_date, budget_band) "
        "VALUES ('Paper lantern loop', 'inquiry', 'none', $1, $2, $3, $4) RETURNING trip_id",
        destinations,
        adults,
        START,
        band,
    )


async def test_the_file_is_an_expand_file() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert text.startswith("-- migrate: expand\n")
    code = "\n".join(line for line in text.splitlines() if not line.startswith("--"))
    assert "REVOKE" not in code and "DO $$" not in code and "CREATE OR REPLACE" not in code


async def test_a_trip_counts_once_whatever_its_destinations(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 AS ok FROM shared.schema_versions WHERE version = 'crm-031'") == 1
    tenant, who = await _agency(app_pool)
    async with tenant_tx(app_pool, who, tenant) as ctx:
        await _trip(ctx.conn, ["rivera", "lumen_bay", "north_pass", "jade_cove"], 1)
        await _trip(ctx.conn, ["rivera"], 1)
        await _trip(ctx.conn, ["lumen_bay"], 3, band="1000_2000")
        trashed = await _trip(ctx.conn, ["rivera", "north_pass"], 5)
        await ctx.conn.execute(
            "UPDATE crm.trip SET deleted_at = now(), deleted_by = 'seed:test', purge_after = now() + interval '7 days' "
            "WHERE trip_id = $1",
            trashed,
        )
    month = START.replace(day=1)
    async with tenant_tx(app_pool, who, tenant, reader_role="crm_insights") as ctx:
        rows = await ctx.conn.fetch(
            "SELECT budget_band, party_size, trip_count, month FROM crm.v_insight_trip_fact ORDER BY budget_band"
        )
        per_destination = await ctx.conn.fetchval(
            "SELECT count(*)::int AS n FROM crm.v_insight_fact WHERE budget_band = 'over_5000'"
        )
        columns = [
            r["column_name"]
            for r in await ctx.conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'crm' AND table_name = 'v_insight_trip_fact' ORDER BY ordinal_position"
            )
        ]
    assert [(r["budget_band"], r["party_size"], r["trip_count"], r["month"]) for r in rows] == [
        ("1000_2000", 3, 1, month),
        ("over_5000", 1, 2, month),  # two 1-traveller trips, one of them to four places: 2 trips, not 5 rows
    ]
    assert per_destination == 5  # the destination view still has one row per destination
    assert columns == [
        "tenant_id", "month", "budget_band", "age_band", "party_size", "stage_reached", "lost_reason_code",
        "trip_count",
    ]  # fmt: skip


async def test_only_the_insights_reader_selects_it_and_nothing_writes_through_it(app_pool: asyncpg.Pool) -> None:
    (a, who_a), (b, who_b) = await _agency(app_pool), await _agency(app_pool)
    async with tenant_tx(app_pool, who_a, a) as ctx:
        await _trip(ctx.conn, ["rivera"], 2)
    async with tenant_tx(app_pool, who_b, b) as ctx:
        await _trip(ctx.conn, ["rivera"], 2)
        await _trip(ctx.conn, ["lumen_bay"], 2)
    async with tenant_tx(app_pool, who_a, a, reader_role="crm_insights") as ctx:
        assert await ctx.conn.fetchval("SELECT sum(trip_count)::int AS n FROM crm.v_insight_trip_fact") == 1  # RLS
    for reader in ("crm_ai_reader", "crm_support_reader", "crm_eraser"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with tenant_tx(app_pool, who_a, a, reader_role=reader) as ctx:
                await ctx.conn.fetchval("SELECT count(*) AS n FROM crm.v_insight_trip_fact")
    for sql in (
        "UPDATE crm.v_insight_trip_fact SET budget_band = 'not_sure'",
        "DELETE FROM crm.v_insight_trip_fact",
        "INSERT INTO crm.v_insight_trip_fact (budget_band) VALUES ('not_sure')",
    ):
        with pytest.raises(asyncpg.PostgresError) as err:
            async with tenant_tx(app_pool, who_a, a) as ctx:
                await ctx.conn.execute(sql)
        assert err.value.sqlstate in ("0A000", "55000", "42501"), sql  # not updatable (or refused outright)
