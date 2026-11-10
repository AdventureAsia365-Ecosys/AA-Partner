"""crm-048 (AA-1066, a contract file) on PostgreSQL with the RDS role shape: retail pricing.

- ``crm.tenant_settings``: the age bands (11 / 1) and the discount limit (10 %) by default, their CHECK.
- ``crm.proposal.discount_pct`` (builder versions only, 0 < pct <= 100) and ``crm.option.price_rules`` (an array).
- ``crm.option_price``: one row per traveller type of an option; changes only while the proposal is a draft (CRM35);
  cascades from its option; another agency neither sees a row nor hangs one on a foreign option.
- ``crm.retail_price_rule``: each kind carries its own condition; cascades from the agency's settings row.
- ``crm.discount_approval``: pending, then an owner decides as themselves (CRM48); one pending row per version; never
  deleted by crm_app; status moves through ``crm.status_transition``.
- Erasure: the trip erase and the agency erase take the new rows (cascades; ``crm.erase_subject`` unchanged).

The services that write these rows are tested in the code PR; this file uses plain SQL as ``crm_app``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase
from tests.integration.test_crm019_schema import _builder, _publish

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "048_crm_retail_pricing.sql"
NEW_TABLES = ("option_price", "retail_price_rule", "discount_approval")


async def _run(
    pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = "member:test", role: str | None = None
) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        if role is not None:
            await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        return await conn.fetchval(sql, *args)


async def _price(pool: asyncpg.Pool, ids: dict[str, Any], b: dict[str, Any], pax: str = "adult") -> uuid.UUID:
    return await _run(
        pool,
        ids["tenant"],
        "INSERT INTO crm.option_price (proposal_id, option_id, pax_type, unit_sell, unit_net, qty) "
        "VALUES ($1, $2, $3, 140, 70, 2) RETURNING option_price_id",
        b["proposal"],
        b["options"][0],
        pax,
    )


async def _settings(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    await _run(pool, tenant, "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING")


async def _request(pool: asyncpg.Pool, ids: dict[str, Any], b: dict[str, Any], pct: int = 15) -> uuid.UUID:
    return await _run(
        pool,
        ids["tenant"],
        "INSERT INTO crm.discount_approval (trip_id, proposal_id, discount_pct, limit_pct) "
        "VALUES ($1, $2, $3, 10) RETURNING discount_approval_id",
        ids["trip"],
        b["proposal"],
        pct,
        actor="member:agent-1",
        role="agent",
    )


async def test_crm048_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'crm-048'") == 1
    before = await admin.fetchval("SELECT count(*) FROM crm.status_transition")
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM crm.status_transition") == before
    defaults = {
        r["column_name"]: r["column_default"]
        for r in await admin.fetch(
            "SELECT column_name, column_default FROM information_schema.columns WHERE table_schema = 'crm' "
            "AND table_name = 'tenant_settings' AND column_name IN "
            "('child_age_max', 'infant_age_max', 'discount_owner_limit_pct')"
        )
    }
    assert defaults == {"child_age_max": "11", "infant_age_max": "1", "discount_owner_limit_pct": "10"}


@pytest.mark.parametrize("table", NEW_TABLES)
async def test_the_readers_have_nothing_on_the_new_tables(admin: asyncpg.Connection, table: str) -> None:
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege)


async def test_the_settings_check(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    await _settings(app_pool, ids["tenant"])
    for sql in (
        "UPDATE crm.tenant_settings SET infant_age_max = 11",  # not below the child band
        "UPDATE crm.tenant_settings SET child_age_max = 18",
        "UPDATE crm.tenant_settings SET discount_owner_limit_pct = 101",
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, ids["tenant"], sql)
    await _run(
        app_pool, ids["tenant"],
        "UPDATE crm.tenant_settings SET child_age_max = 15, infant_age_max = 2, discount_owner_limit_pct = 5",
    )  # fmt: skip


async def test_proposal_discount_and_option_rules_shapes(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    await _run(app_pool, ids["tenant"], "UPDATE crm.proposal SET discount_pct = 12.5 WHERE proposal_id = $1",
               b["proposal"])  # fmt: skip
    for pct in (0, 101):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, ids["tenant"], "UPDATE crm.proposal SET discount_pct = $2 WHERE proposal_id = $1",
                       b["proposal"], pct)  # fmt: skip
    await _run(
        app_pool, ids["tenant"], "UPDATE crm.option SET price_rules = $2::jsonb WHERE option_id = $1",
        b["options"][0], '[{"rule_id": "x", "name": "High season", "adjust_pct": "20"}]',
    )  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, ids["tenant"], "UPDATE crm.option SET price_rules = '{}'::jsonb WHERE option_id = $1",
                   b["options"][0])  # fmt: skip


async def test_the_discount_of_a_published_version_is_sealed(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    await _run(app_pool, ids["tenant"], "UPDATE crm.proposal SET discount_pct = 15 WHERE proposal_id = $1",
               b["proposal"])  # fmt: skip
    await _publish(app_pool, ids, b["proposal"])
    for pct in (40, None):  # neither a new percent nor clearing it
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, ids["tenant"], "UPDATE crm.proposal SET discount_pct = $2 WHERE proposal_id = $1",
                       b["proposal"], pct)  # fmt: skip
        assert err.value.sqlstate == "CRM31", pct
    # The crm-019 columns stay sealed too (the trigger was recreated with the full list).
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(app_pool, ids["tenant"], "UPDATE crm.proposal SET deposit_pct = 50 WHERE proposal_id = $1",
                   b["proposal"])  # fmt: skip
    assert err.value.sqlstate == "CRM31"
    assert await _run(app_pool, ids["tenant"], "SELECT discount_pct FROM crm.proposal WHERE proposal_id = $1",
                      b["proposal"]) == 15  # fmt: skip


async def test_option_prices_change_only_in_a_draft_and_cascade_with_the_option(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    await _price(app_pool, ids, b)
    await _price(app_pool, ids, b, "child")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _price(app_pool, ids, b)  # one row per traveller type
    with pytest.raises(asyncpg.CheckViolationError):
        await _price(app_pool, ids, b, "senior")
    # The second option has no row: deleting it is unaffected; the rule naming it goes first (no ON DELETE there).
    await _run(app_pool, ids["tenant"], "DELETE FROM crm.option_rule WHERE proposal_id = $1", b["proposal"])
    # Deleting the first option takes its rows along (the default is cleared first, as the API does).
    await _run(
        app_pool, ids["tenant"], "UPDATE crm.option_block SET default_option_id = NULL WHERE option_block_id = $1",
        b["block"],
    )  # fmt: skip
    await _run(app_pool, ids["tenant"], "DELETE FROM crm.option WHERE option_id = $1", b["options"][0])
    assert await _count(app_pool, ids["tenant"], "option_price") == 0

    sealed = await _agency(app_pool)
    c = await _builder(app_pool, sealed)
    await _price(app_pool, sealed, c)
    await _publish(app_pool, sealed, c["proposal"])
    for sql in (
        "UPDATE crm.option_price SET unit_sell = 1 WHERE proposal_id = $1",
        "DELETE FROM crm.option_price WHERE proposal_id = $1",
    ):
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, sealed["tenant"], sql, c["proposal"])
        assert err.value.sqlstate == "CRM35", sql


async def test_another_agency_sees_no_price_and_cannot_hang_one(app_pool: asyncpg.Pool) -> None:
    ids, other = await _agency(app_pool), await _agency(app_pool)
    b = await _builder(app_pool, ids)
    await _price(app_pool, ids, b)
    assert await _count(app_pool, other["tenant"], "option_price") == 0
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _run(
            app_pool, other["tenant"],
            "INSERT INTO crm.option_price (proposal_id, option_id, pax_type, unit_sell, qty) "
            "VALUES ($1, $2, 'adult', 1, 1)",
            b["proposal"], b["options"][0],
        )  # fmt: skip


@pytest.mark.parametrize(
    ("columns", "ok"),
    [
        ("kind, date_from, date_to) VALUES ('High season', 'season', DATE '2026-12-20', DATE '2027-01-05'", True),
        ("kind, date_from) VALUES ('Half season', 'season', DATE '2026-12-20'", False),
        ("kind, min_pax) VALUES ('Group of 6', 'group_size', 6", True),
        ("kind, min_pax) VALUES ('Group of 1', 'group_size', 1", False),
        ("kind, days_before) VALUES ('Early bird', 'early_booking', 90", True),
        ("kind, days_before, min_pax) VALUES ('Mixed', 'early_booking', 90, 4", False),
        ("kind, days_before) VALUES ('Last minute', 'last_minute', 0", True),
        ("kind, days_before) VALUES ('Never', 'birthday', 3", False),
    ],
)
async def test_each_rule_kind_carries_its_condition(app_pool: asyncpg.Pool, columns: str, ok: bool) -> None:
    ids = await _agency(app_pool)
    await _settings(app_pool, ids["tenant"])
    sql = f"INSERT INTO crm.retail_price_rule (adjust_pct, name, {columns}) RETURNING retail_price_rule_id"
    sql = sql.replace("VALUES (", "VALUES (-10, ")
    if ok:
        assert await _run(app_pool, ids["tenant"], sql) is not None
    else:
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, ids["tenant"], sql)


async def test_a_rule_needs_the_agency_settings_row_and_a_non_zero_adjustment(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    rule = (
        "INSERT INTO crm.retail_price_rule (name, kind, min_pax, adjust_pct) VALUES ('Group', 'group_size', 4, $1) "
        "RETURNING retail_price_rule_id"
    )
    await _run(app_pool, ids["tenant"], "DELETE FROM crm.tenant_settings")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _run(app_pool, ids["tenant"], rule, -5)
    await _settings(app_pool, ids["tenant"])
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, ids["tenant"], rule, 0)
    assert await _run(app_pool, ids["tenant"], rule, -5) is not None


async def test_a_discount_approval_is_decided_by_an_owner_once(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    approval = await _request(app_pool, ids, b)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _request(app_pool, ids, b, 20)  # one pending row per version
    decide = (
        "UPDATE crm.discount_approval SET status = $2, decided_at = now(), decided_by = $3 "
        "WHERE discount_approval_id = $1"
    )
    # An agent cannot decide; an owner cannot decide in someone else's name.
    for actor, role in (("member:agent-1", "agent"), ("member:owner-1", "owner")):
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, ids["tenant"], decide, approval, "approved",
                       "member:someone-else" if role == "owner" else actor, actor=actor, role=role)  # fmt: skip
        assert err.value.sqlstate == "CRM48"
    # Only the decision columns change.
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(
            app_pool, ids["tenant"],
            "UPDATE crm.discount_approval SET discount_pct = 5 WHERE discount_approval_id = $1",
            approval, actor="member:owner-1", role="owner",
        )  # fmt: skip
    assert err.value.sqlstate == "CRM48"
    await _run(
        app_pool, ids["tenant"], decide, approval, "approved", "member:owner-1", actor="member:owner-1", role="owner"
    )
    # approved is final: no move back to pending or to rejected.
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, ids["tenant"], decide, approval, "rejected", "member:owner-1", actor="member:owner-1",
                   role="owner")  # fmt: skip
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(
            app_pool, ids["tenant"], "DELETE FROM crm.discount_approval WHERE discount_approval_id = $1", approval
        )
    assert err.value.sqlstate in ("42501", "CRM48")  # crm_app has no DELETE
    # A newer request is allowed once nothing is pending; any member may supersede a pending one.
    second = await _request(app_pool, ids, b, 20)
    await _run(
        app_pool, ids["tenant"],
        "UPDATE crm.discount_approval SET status = 'superseded' WHERE discount_approval_id = $1",
        second, actor="member:agent-1", role="agent",
    )  # fmt: skip


async def test_an_owner_records_their_own_approval_and_an_agent_cannot(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    b = await _builder(app_pool, ids)
    own = (
        "INSERT INTO crm.discount_approval (trip_id, proposal_id, discount_pct, limit_pct, status, decided_at, "
        "decided_by) VALUES ($1, $2, 25, 10, 'approved', now(), $3) RETURNING discount_approval_id"
    )
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(app_pool, ids["tenant"], own, ids["trip"], b["proposal"], "member:agent-1", actor="member:agent-1",
                   role="agent")  # fmt: skip
    assert err.value.sqlstate == "CRM48"
    assert await _run(app_pool, ids["tenant"], own, ids["trip"], b["proposal"], "member:owner-1",
                      actor="member:owner-1", role="owner") is not None  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):  # a discount within the limit needs no approval row
        await _run(
            app_pool, ids["tenant"],
            "INSERT INTO crm.discount_approval (trip_id, proposal_id, discount_pct, limit_pct) VALUES ($1, $2, 5, 10)",
            ids["trip"], b["proposal"], actor="member:agent-1", role="agent",
        )  # fmt: skip


async def test_the_trip_and_agency_erase_take_the_new_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    await _settings(app_pool, ids["tenant"])
    b = await _builder(app_pool, ids)
    await _price(app_pool, ids, b)
    await _request(app_pool, ids, b)
    await _run(
        app_pool, ids["tenant"],
        "INSERT INTO crm.retail_price_rule (name, kind, min_pax, adjust_pct) VALUES ('Group', 'group_size', 4, -5)",
    )  # fmt: skip
    await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert await _count(app_pool, ids["tenant"], "option_price") == 0
    assert await _count(app_pool, ids["tenant"], "discount_approval") == 0
    assert await _count(app_pool, ids["tenant"], "retail_price_rule") == 1  # agency data, not the trip's
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert await _count(app_pool, ids["tenant"], "retail_price_rule") == 0
