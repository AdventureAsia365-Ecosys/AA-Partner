"""crm-019 (AA-949, card CRM-21a) on PostgreSQL with the RDS role shape: the proposal builder tables.

- A builder version of ``crm.proposal`` (kind, price_visibility, room_config, deposit_pct, version counters, status
  published); a text version cannot be published; sealed after draft (CRM31), ``sent_at`` set once.
- Days, blocks, options and rules change only while their proposal is a draft (CRM35); an option of a published
  version may change its availability only. Picks are written only on a published / sent version (CRM36).
- Same-agency keys and FORCE RLS: another agency sees nothing and cannot hang a row on a foreign proposal.
- No key has an ON DELETE action: a named row cannot go; the API deletes a draft's rows child first.
- Erasure: the trip and agency erase take every builder row; a trip hold guards them (CRM10).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase, _hold

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "019_crm_proposal_builder.sql"
NEW_TABLES = ("proposal_day", "option_block", "option", "option_rule", "selection")


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = "member:test") -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        return await conn.fetchval(sql, *args)


async def _builder(pool: asyncpg.Pool, ids: dict[str, Any]) -> dict[str, Any]:
    """A builder draft with one day, one block, two options (default = the first) and one rule."""
    out: dict[str, Any] = {"proposal": uuid.uuid4(), "day": uuid.uuid4(), "block": uuid.uuid4()}
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "member:test")
        await conn.execute(
            "INSERT INTO crm.proposal (proposal_id, trip_id, version, body_enc, currency, total_amount, "
            "deposit_amount, kind, deposit_pct, room_config) "
            "VALUES ($1, $2, 1, $3, 'USD', 0, 0, 'builder', 30, '[{\"type\": \"double\", \"adults\": 2}]')",
            out["proposal"],
            ids["trip"],
            os.urandom(40),
        )
        await conn.execute(
            "INSERT INTO crm.proposal_day (proposal_day_id, proposal_id, trip_id, day_no, tz) "
            "VALUES ($1, $2, $3, 1, 'Asia/Kathmandu')",
            out["day"],
            out["proposal"],
            ids["trip"],
        )
        await conn.execute(
            "INSERT INTO crm.option_block (option_block_id, proposal_id, trip_id, proposal_day_id, title, type) "
            "VALUES ($1, $2, $3, $4, 'Lodging', 'lodging')",
            out["block"],
            out["proposal"],
            ids["trip"],
            out["day"],
        )
        out["options"] = []
        for title in ("Homestay", "Riverside Lodge"):
            out["options"].append(
                await conn.fetchval(
                    "INSERT INTO crm.option (proposal_id, trip_id, option_block_id, source, title, sell_amount, "
                    "net_amount, availability) VALUES ($1, $2, $3, 'agency', $4, 280, 140, 'agency') "
                    "RETURNING option_id",
                    out["proposal"],
                    ids["trip"],
                    out["block"],
                    title,
                )
            )
        await conn.execute(
            "UPDATE crm.option_block SET default_option_id = $1 WHERE option_block_id = $2",
            out["options"][0],
            out["block"],
        )
        await conn.execute(
            "INSERT INTO crm.option_rule (proposal_id, trip_id, kind, option_id, target_option_id) "
            "VALUES ($1, $2, 'excludes', $3, $4)",
            out["proposal"],
            ids["trip"],
            out["options"][0],
            out["options"][1],
        )
    return out


async def _publish(pool: asyncpg.Pool, ids: dict[str, Any], proposal: uuid.UUID) -> None:
    await _run(
        pool,
        ids["tenant"],
        "UPDATE crm.proposal SET status = 'published', published_at = now(), price_hash = $2, total_amount = 280, "
        "deposit_amount = 84, balance_due_on = DATE '2026-12-01' WHERE proposal_id = $1",
        proposal,
        os.urandom(32),
    )


async def test_crm019_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    # Runs first in this file: the crm-016 / crm-017 tests re-apply their own proposal seal and erase_subject text.
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-019'") == 1
    before = await admin.fetchval("SELECT count(*) FROM crm.status_transition")
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM crm.status_transition") == before


@pytest.mark.parametrize("table", NEW_TABLES)
async def test_the_readers_have_nothing_on_the_new_tables(admin: asyncpg.Connection, table: str) -> None:
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege)


async def test_a_builder_draft_publishes_and_then_is_sealed(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    # Zero amounts are fine while the builder version was never published.
    await _publish(app_pool, ids, b["proposal"])
    for sql in (
        'UPDATE crm.proposal SET room_config = \'[{"type": "single", "adults": 1}]\' WHERE proposal_id = $1',
        "UPDATE crm.proposal SET price_visibility = 'total_only' WHERE proposal_id = $1",
        "UPDATE crm.proposal SET total_amount = 300 WHERE proposal_id = $1",
    ):
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, ids["tenant"], sql, b["proposal"])
        assert err.value.sqlstate == "CRM31", sql
    # CRM-21b's seam: a published version can be sent (sent_at set once), never re-sent with another time.
    await _run(
        app_pool, ids["tenant"], "UPDATE crm.proposal SET status = 'sent', sent_at = now() WHERE proposal_id = $1",
        b["proposal"],
    )  # fmt: skip
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(
            app_pool, ids["tenant"], "UPDATE crm.proposal SET sent_at = now() + interval '1 day' "
            "WHERE proposal_id = $1",
            b["proposal"],
        )  # fmt: skip
    assert err.value.sqlstate == "CRM31"


async def test_a_text_version_is_never_published_and_needs_amounts(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    proposal = uuid.uuid4()
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(
            app_pool, ids["tenant"], "INSERT INTO crm.proposal (proposal_id, trip_id, version, body_enc, currency, "
            "total_amount, deposit_amount) VALUES ($1, $2, 1, $3, 'USD', 0, 0)", proposal, ids["trip"], os.urandom(40),
        )  # fmt: skip
    await _run(
        app_pool, ids["tenant"], "INSERT INTO crm.proposal (proposal_id, trip_id, version, body_enc, currency, "
        "total_amount, deposit_amount) VALUES ($1, $2, 1, $3, 'USD', 100, 100)", proposal, ids["trip"], os.urandom(40),
    )  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(
            app_pool, ids["tenant"], "UPDATE crm.proposal SET status = 'published', published_at = now(), "
            "price_hash = $2 WHERE proposal_id = $1", proposal, os.urandom(32),
        )  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):  # builder columns stay empty on a text version
        await _run(app_pool, ids["tenant"], "UPDATE crm.proposal SET deposit_pct = 30 WHERE proposal_id = $1", proposal)
    # A builder version needs its deposit share; price_visibility has two values only (Fix A5).
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(
            app_pool, ids["tenant"], "INSERT INTO crm.proposal (trip_id, version, body_enc, currency, total_amount, "
            "deposit_amount, kind) VALUES ($1, 2, $2, 'USD', 0, 0, 'builder')", ids["trip"], os.urandom(40),
        )  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, ids["tenant"], "UPDATE crm.proposal SET price_visibility = 'per_line' "
                   "WHERE proposal_id = $1", proposal)  # fmt: skip


async def test_builder_rows_change_only_while_draft(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    await _publish(app_pool, ids, b["proposal"])
    refused = (
        ("UPDATE crm.proposal_day SET title = 'Kathmandu' WHERE proposal_day_id = $1", b["day"]),
        ("UPDATE crm.option_block SET client_may_swap = false WHERE option_block_id = $1", b["block"]),
        ("UPDATE crm.option SET sell_amount = 1 WHERE option_id = $1", b["options"][0]),
        ("DELETE FROM crm.option_rule WHERE proposal_id = $1", b["proposal"]),
        ("DELETE FROM crm.proposal_day WHERE proposal_day_id = $1", b["day"]),
    )
    for sql, arg in refused:
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, ids["tenant"], sql, arg)
        assert err.value.sqlstate == "CRM35", sql
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(
            app_pool, ids["tenant"], "INSERT INTO crm.proposal_day (proposal_id, trip_id, day_no, tz) "
            "VALUES ($1, $2, 2, 'UTC')", b["proposal"], ids["trip"],
        )  # fmt: skip
    assert err.value.sqlstate == "CRM35"
    # An option that sells out mid-session changes its availability only (CRM-21b).
    await _run(
        app_pool, ids["tenant"], "UPDATE crm.option SET availability_checked_at = now() WHERE option_id = $1",
        b["options"][1],
    )  # fmt: skip


async def test_picks_only_on_a_published_or_sent_version(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    pick = (
        "INSERT INTO crm.selection (proposal_id, trip_id, option_block_id, option_id, chosen_by) "
        "VALUES ($1, $2, $3, $4, 'client') "
        "ON CONFLICT (tenant_id, proposal_id, option_block_id) DO UPDATE SET option_id = EXCLUDED.option_id"
    )
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(app_pool, ids["tenant"], pick, b["proposal"], ids["trip"], b["block"], b["options"][1])
    assert err.value.sqlstate == "CRM36"
    await _publish(app_pool, ids, b["proposal"])
    await _run(app_pool, ids["tenant"], pick, b["proposal"], ids["trip"], b["block"], b["options"][1])
    await _run(app_pool, ids["tenant"], pick, b["proposal"], ids["trip"], b["block"], b["options"][0])
    assert await _count(app_pool, ids["tenant"], "selection") == 1
    # The option must belong to the picked block (same-block key).
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _run(app_pool, ids["tenant"], pick, b["proposal"], ids["trip"], b["block"], uuid.uuid4())


async def test_a_draft_is_deleted_child_first(app_pool: asyncpg.Pool) -> None:
    """No key has an ON DELETE action (a referential action reads the legal holds of every agency): a row that is
    still named cannot go, and the API deletes child first."""
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    for sql, arg in (
        ("DELETE FROM crm.option WHERE option_id = $1", b["options"][0]),  # the block's default (checked at commit)
        ("DELETE FROM crm.option WHERE option_id = $1", b["options"][1]),  # a rule names it
        ("DELETE FROM crm.proposal_day WHERE proposal_day_id = $1", b["day"]),  # its block
    ):
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await _run(app_pool, ids["tenant"], sql, arg)
    # A default must be one of the block's own options.
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _run(
            app_pool, ids["tenant"], "UPDATE crm.option_block SET default_option_id = $2 WHERE option_block_id = $1",
            b["block"], uuid.uuid4(),
        )  # fmt: skip
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "member:test")
        await conn.execute("DELETE FROM crm.option_rule WHERE proposal_id = $1", b["proposal"])
        await conn.execute(
            "UPDATE crm.option_block SET default_option_id = NULL WHERE option_block_id = $1", b["block"]
        )
        await conn.execute("DELETE FROM crm.option WHERE option_block_id = $1", b["block"])
        await conn.execute("DELETE FROM crm.option_block WHERE option_block_id = $1", b["block"])
        await conn.execute("DELETE FROM crm.proposal_day WHERE proposal_day_id = $1", b["day"])
    for table in NEW_TABLES:
        assert await _count(app_pool, ids["tenant"], table) == 0, table
    # Day numbers are unique per version; 0 is the "Before you go" slot, 91 is out of range; tz is an IANA name.
    await _run(app_pool, ids["tenant"], "INSERT INTO crm.proposal_day (proposal_id, trip_id, day_no, tz) "
               "VALUES ($1, $2, 0, 'Asia/Ho_Chi_Minh')", b["proposal"], ids["trip"])  # fmt: skip
    for day_no, tz in ((0, "UTC"), (91, "UTC"), (3, "UTC+7"), (4, "asia/kathmandu")):
        with pytest.raises((asyncpg.CheckViolationError, asyncpg.UniqueViolationError)):
            await _run(app_pool, ids["tenant"], "INSERT INTO crm.proposal_day (proposal_id, trip_id, day_no, tz) "
                       "VALUES ($1, $2, $3, $4)", b["proposal"], ids["trip"], day_no, tz)  # fmt: skip


async def test_option_source_shapes(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    insert = (
        "INSERT INTO crm.option (proposal_id, trip_id, option_block_id, source, title, availability, "
        "choose_later_allowed, aa_tour_source_id, aa_component_id, aa_component_kind) "
        "VALUES ($1, $2, $3, $4, 'X', $5, $6, $7, $8, $9)"
    )
    base = (b["proposal"], ids["trip"], b["block"])
    await _run(app_pool, ids["tenant"], insert, *base, "aa", "on_request", False, "T-1", "tp:1", "trip_plan")
    await _run(app_pool, ids["tenant"], insert, *base, "agency", "agency", True, None, None, None)
    for bad in (
        ("aa", "on_request", True, "T-1", "tp:1", "trip_plan"),  # choose later is for agency items only (Fix B9)
        ("aa", "agency", False, "T-1", "tp:1", "trip_plan"),
        ("aa", "instant", False, None, "tp:1", "trip_plan"),
        ("agency", "instant", False, None, None, None),
        ("agency", "agency", False, "T-1", None, None),
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, ids["tenant"], insert, *base, *bad)


async def test_another_agency_sees_nothing_and_cannot_attach(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    for table in NEW_TABLES[:-1]:
        assert await _count(app_pool, ids["tenant"], table) >= 1, table
        assert await _count(app_pool, other["tenant"], table) == 0, table
    # A row of agency B naming agency A's proposal fails the same-agency key (RLS hides A's row from B).
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _run(
            app_pool, other["tenant"], "INSERT INTO crm.proposal_day (proposal_id, trip_id, day_no, tz) "
            "VALUES ($1, $2, 5, 'UTC')", b["proposal"], ids["trip"],
        )  # fmt: skip
    # Agency B cannot change A's rows: zero rows match under its tenant.
    assert await _run(
        app_pool, other["tenant"], "WITH u AS (UPDATE crm.option SET sell_amount = 1 RETURNING 1) "
        "SELECT count(*) FROM u"
    ) == 0  # fmt: skip


async def test_the_trip_and_agency_erase_take_the_builder_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    await _publish(app_pool, ids, b["proposal"])
    await _run(
        app_pool, ids["tenant"], "INSERT INTO crm.selection (proposal_id, trip_id, option_block_id, option_id, "
        "chosen_by) VALUES ($1, $2, $3, $4, 'client')", b["proposal"], ids["trip"], b["block"], b["options"][1],
    )  # fmt: skip
    done = await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    for table in NEW_TABLES:
        assert done["counts"][table] >= 1, table
        assert await _count(app_pool, ids["tenant"], table) == 0, table
    again = await _agency(app_pool)
    await _builder(app_pool, again)
    await _run(app_pool, again["tenant"], "UPDATE crm.tenant SET status = 'offboarding'", actor="seed:test")
    done = await _erase(app_pool, again["tenant"], "tenant", again["tenant"])
    for table in NEW_TABLES[:-1]:
        assert done["counts"][table] >= 1, table


async def test_a_trip_hold_guards_the_builder_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    await _hold(app_pool, ids["tenant"], "trip", ids["trip"], "member:m")
    with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
        await _run(app_pool, ids["tenant"], "DELETE FROM crm.option WHERE option_id = $1", b["options"][1])
    assert err.value.sqlstate == "CRM10"
