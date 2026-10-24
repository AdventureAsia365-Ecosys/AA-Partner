"""crm-014 (AA-944, card CRM-16) on PostgreSQL 15 with the RDS role shape: the agency-line ledger and suppliers.

- **R** (card): under Strict two confirmed lines of one supplier with one confirmation number -> unique_violation
  (``booking_line_conf_uq``, the API's 409 ``duplicate_confirmation_no``); a cancelled line frees the number; Basic
  allows duplicates unless the supplier is ``confirmation_unique``.
- **E** (card): a confirmed line's money is frozen at the rate of its confirmation day; a later override or platform
  row for that day changes no stored base amount, and the FX block cannot be updated.
- Margins: the assistant reads no money row (also through the security_invoker view) and writes none; owner and agent
  read them; the NOLOGIN readers have nothing on the new tables (42501).
- The Adventure Asia supplier row: one per agency, created by ``crm.ensure_aa_supplier()`` only, never changed by
  crm_app (CRM22); AA lines are read-only for members (CRM24).
- The confirm path: the level's missing codes (CRM23), the assistant's missing commission does not block, an agency
  line is confirmed only through ``crm.booking_line_confirm`` (CRM26), no rate -> CRM25.
- ``crm.fx_resolve``: the override of the day wins, then the platform rate of the day, then the latest earlier day
  (``stale_days``); inverse and USD crosses; manual mode reads overrides only.
- Erasure: trip and agency erases take lines, money, suppliers and overrides (no CRM16); a trip hold reaches its
  lines; the supplier scope refuses the AA row and a supplier lines name.
"""

from __future__ import annotations

import random
import string
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import ISS, _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "014_crm_ledger_suppliers.sql"
TODAY = "(now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date"
OVERRIDE_SQL = (
    "INSERT INTO crm.fx_rate_override (rate_date, base, quote, rate, set_by) "
    f"VALUES ({TODAY} - $5::int, $1, $2, $3::numeric, $4)"
)
TRASH_LINE_SQL = (
    "UPDATE crm.booking_line SET deleted_at = now(), deleted_by = 'm', purge_after = now() WHERE booking_line_id = $1"
)
NEW_TABLES = ("agency_supplier", "booking_line", "booking_line_money", "fx_rate_override", "fx_rate_platform")


def _currency() -> str:
    """A currency code no other test uses: platform rates are shared by every agency."""
    return "Q" + "".join(random.choices(string.ascii_uppercase, k=2))  # noqa: S311 — test data


async def _members(pool: asyncpg.Pool, ids: dict[str, Any]) -> dict[str, uuid.UUID]:
    """The agency's owner (from _agency) plus an agent and an assistant."""
    out = {"owner": ids["member"]}
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "seed:test")
        for role in ("agent", "assistant"):
            out[role] = await conn.fetchval(
                "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, $3, 'active') "
                "RETURNING member_id",
                ISS,
                f"sub-{uuid.uuid4()}",
                role,
            )
    return out


@asynccontextmanager
async def _as(
    pool: asyncpg.Pool, tenant: uuid.UUID, role: str | None = None, member: uuid.UUID | None = None, actor: str = ""
) -> AsyncIterator[asyncpg.Connection]:
    """One crm_app transaction in the agency: a member request (role + member:<id>) or a job / seed / platform actor."""
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor or f"member:{member}")
        if role:
            await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        yield conn


async def _ledger(pool: asyncpg.Pool, *, level: str = "basic", base: str = "USD") -> dict[str, Any]:
    """An agency (_agency) with members, settings, the AA row and one own supplier."""
    ids = await _agency(pool)
    ids["members"] = await _members(pool, ids)
    async with _as(pool, ids["tenant"], actor="seed:test") as conn:
        await conn.execute(
            "INSERT INTO crm.tenant_settings (booking_validation_level, base_currency) VALUES ($1, $2)", level, base
        )
        ids["aa"] = await conn.fetchval("SELECT crm.ensure_aa_supplier()")
        ids["supplier"] = await conn.fetchval(
            "INSERT INTO crm.agency_supplier (name, category) VALUES ('Hanoi Pearl Hotel', 'accommodation') "
            "RETURNING agency_supplier_id"
        )
    return ids


async def _line(pool: asyncpg.Pool, ids: dict[str, Any], *, conf: str | None = "HP-1001", supplier: Any = None) -> Any:
    async with _as(pool, ids["tenant"], "agent", ids["members"]["agent"]) as conn:
        return await conn.fetchval(
            "INSERT INTO crm.booking_line (trip_id, agency_supplier_id, title, confirmation_no) "
            "VALUES ($1, $2, 'Hanoi 2 nights', $3) RETURNING booking_line_id",
            ids["trip"],
            supplier or ids["supplier"],
            conf,
        )


async def _money(
    pool: asyncpg.Pool, ids: dict[str, Any], line: Any, kind: str, amount: str, currency: str, role: str = "agent"
) -> Any:
    async with _as(pool, ids["tenant"], role, ids["members"][role]) as conn:
        return await conn.fetchval(
            "SELECT crm.booking_line_money_put($1, $2, $3::numeric, $4)", line, kind, amount, currency
        )


async def _confirm(pool: asyncpg.Pool, ids: dict[str, Any], line: Any, role: str = "agent") -> Any:
    async with _as(pool, ids["tenant"], role, ids["members"][role]) as conn:
        return await conn.fetchval("SELECT crm.booking_line_confirm($1)", line)


async def _rate(
    conn: asyncpg.Connection, base: str, quote: str, rate: str, *, days_ago: int = 0, source: str = "synthetic"
) -> None:
    await conn.execute(
        f"INSERT INTO crm.fx_rate_platform (rate_date, base, quote, source, rate) "  # noqa: S608 — fixed SQL
        f"VALUES ({TODAY} - $5::int, $1, $2, $3, $4::numeric) ON CONFLICT DO NOTHING",
        base,
        quote,
        source,
        rate,
        days_ago,
    )


async def _heads(pool: asyncpg.Pool, ids: dict[str, Any], line: Any) -> dict[str, asyncpg.Record]:
    async with _as(pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        rows = await conn.fetch(
            "SELECT kind, amount, currency, amount_base, fx_rate, fx_rate_date, fx_source, fx_basis, stale_days "
            "FROM crm.v_booking_line_money_current WHERE booking_line_id = $1",
            line,
        )
    return {r["kind"]: r for r in rows}


async def _sqlstate(coro: Any) -> str:
    with pytest.raises(asyncpg.PostgresError) as err:
        await coro
    return str(err.value.sqlstate)


async def _set_level(pool: asyncpg.Pool, ids: dict[str, Any], level: str) -> None:
    async with _as(pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        await conn.execute("UPDATE crm.tenant_settings SET booking_validation_level = $1", level)


async def _ready_line(pool: asyncpg.Pool, ids: dict[str, Any], conf: str = "HP-1001", currency: str = "USD") -> Any:
    line = await _line(pool, ids, conf=conf)
    await _money(pool, ids, line, "cost", "100", currency)
    await _money(pool, ids, line, "sell", "130", currency)
    await _money(pool, ids, line, "commission_expected", "10", currency)
    return line


async def test_crm014_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    # Runs first in this file: crm-012 / crm-013 tests re-apply their own erase_subject text before it.
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-014'") == 1
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    source = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    for table in ("client_link", "meeting_slot", "membership_inbox", *NEW_TABLES[:-1]):
        assert f"'{table}'" in source


async def test_r_strict_refuses_a_duplicate_confirmation_number(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool, level="strict")
    first = await _ready_line(app_pool, ids, conf="HP-1001")
    await _confirm(app_pool, ids, first)
    second = await _ready_line(app_pool, ids, conf="hp 1001")  # normalised: same number
    with pytest.raises(asyncpg.UniqueViolationError) as err:
        await _confirm(app_pool, ids, second)
    assert err.value.constraint_name == "booking_line_conf_uq"
    async with _as(app_pool, ids["tenant"], "agent", ids["members"]["agent"]) as conn:
        assert (
            await conn.fetchval("SELECT status FROM crm.booking_line WHERE booking_line_id = $1", second) == "planning"
        )
        await conn.execute("UPDATE crm.booking_line SET status = 'cancelled' WHERE booking_line_id = $1", first)
    await _confirm(app_pool, ids, second)  # a cancelled line frees its number


async def test_r_a_line_confirmed_before_strict_still_counts(app_pool: asyncpg.Pool) -> None:
    """Basic -> Strict: the older confirmed line has no key, the new one is refused all the same."""
    ids = await _ledger(app_pool, level="basic")
    await _confirm(app_pool, ids, await _ready_line(app_pool, ids, conf="OLD-9"))
    await _set_level(app_pool, ids, "strict")
    with pytest.raises(asyncpg.UniqueViolationError) as err:
        await _confirm(app_pool, ids, await _ready_line(app_pool, ids, conf="old 9"))
    assert err.value.constraint_name == "booking_line_conf_uq"
    await _confirm(app_pool, ids, await _ready_line(app_pool, ids, conf="NEW-1"))


async def test_basic_allows_duplicates_unless_the_supplier_is_confirmation_unique(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool, level="basic")
    for _ in range(2):
        await _confirm(app_pool, ids, await _ready_line(app_pool, ids, conf="SAME-1"))
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        await conn.execute(
            "UPDATE crm.agency_supplier SET confirmation_unique = true WHERE agency_supplier_id = $1", ids["supplier"]
        )
    await _confirm(app_pool, ids, await _ready_line(app_pool, ids, conf="UNIQ-1"))
    with pytest.raises(asyncpg.UniqueViolationError):
        await _confirm(app_pool, ids, await _ready_line(app_pool, ids, conf="UNIQ-1"))


async def test_e_a_later_fx_row_does_not_change_a_confirmed_margin(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    cur = _currency()
    async with _as(app_pool, ids["tenant"], actor="job:crm_fx_fetch") as conn:
        await _rate(conn, cur, "USD", "0.5")
    line = await _ready_line(app_pool, ids, currency=cur)
    planning = await _heads(app_pool, ids, line)
    assert {k: r["fx_rate"] for k, r in planning.items()} == {"cost": None, "sell": None, "commission_expected": None}
    result = await _confirm(app_pool, ids, line)
    assert '"frozen": 3' in result and '"stale_days": 0' in result
    before = await _heads(app_pool, ids, line)
    assert before["cost"]["amount_base"] == Decimal("50.00") and before["sell"]["amount_base"] == Decimal("65.00")
    assert {r["fx_source"] for r in before.values()} == {"platform_daily"}
    assert {r["fx_basis"] for r in before.values()} == {"booking_date"}
    # A later override for the same day and a new platform row of another source: nothing stored moves.
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        await conn.execute(OVERRIDE_SQL, cur, "USD", "0.9", ids["members"]["owner"], 0)
        await _rate(conn, cur, "USD", "0.7", source="ecb")
    assert await _heads(app_pool, ids, line) == before
    # The FX block is never updated, by anyone.
    async with app_pool.acquire() as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], f"member:{ids['members']['owner']}")
                await conn.execute("UPDATE crm.booking_line_money SET fx_rate = 1")
    # A correction in the same currency keeps the frozen rate; another currency resolves today's rate.
    await _money(app_pool, ids, line, "cost", "120", cur)
    await _money(app_pool, ids, line, "sell", "200", "USD")
    after = await _heads(app_pool, ids, line)
    assert after["cost"]["fx_rate"] == Decimal("0.5") and after["cost"]["amount_base"] == Decimal("60.00")
    assert after["sell"]["fx_rate"] == Decimal("1") and after["sell"]["amount_base"] == Decimal("200.00")
    # Writing the same value again adds no row.
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        n = await conn.fetchval("SELECT count(*) FROM crm.booking_line_money WHERE booking_line_id = $1", line)
    await _money(app_pool, ids, line, "cost", "120", cur)
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        assert await conn.fetchval("SELECT count(*) FROM crm.booking_line_money WHERE booking_line_id = $1", line) == n


async def test_the_assistant_reads_and_writes_no_money(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    line = await _ready_line(app_pool, ids)
    for role, want in (("owner", 3), ("agent", 3), ("assistant", 0)):
        async with _as(app_pool, ids["tenant"], role, ids["members"][role]) as conn:
            assert await conn.fetchval("SELECT count(*) FROM crm.booking_line_money") == want, role
            assert await conn.fetchval("SELECT count(*) FROM crm.v_booking_line_money_current") == want, role
            assert await conn.fetchval("SELECT count(*) FROM crm.booking_line") == 1  # the line itself is not margin
    async with _as(app_pool, ids["tenant"], actor="job:crm_b2b_events_pull") as conn:
        assert await conn.fetchval("SELECT count(*) FROM crm.booking_line_money") == 3
    assert await _sqlstate(_money(app_pool, ids, line, "cost", "1", "USD", role="assistant")) == "42501"
    async with app_pool.acquire() as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], f"member:{ids['members']['owner']}")
                await conn.execute("SELECT set_config('app.member_role', 'owner', true)")
                await conn.execute(
                    "INSERT INTO crm.booking_line_money (booking_line_id, kind, amount, currency) "
                    "VALUES ($1, 'cost', 1, 'USD')",
                    line,
                )
        for reader in READERS:
            for table in (*NEW_TABLES, "v_booking_line_money_current"):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert not await conn.fetchval(
                        "SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege
                    ), (reader, table, privilege)


async def test_the_confirm_needs_the_levels_fields_and_never_blocks_the_assistant(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool, level="basic")
    bare = await _line(app_pool, ids, conf=None)
    with pytest.raises(asyncpg.PostgresError, match="line_incomplete:confirmation_no,cost,sell") as err:
        await _confirm(app_pool, ids, bare)
    assert err.value.sqlstate == "CRM23"
    async with _as(app_pool, ids["tenant"], "assistant", ids["members"]["assistant"]) as conn:
        assert await conn.fetchval("SELECT crm.booking_line_confirm_check($1)", bare) == [
            "confirmation_no",
            "cost",
            "sell",
        ]
    await _set_level(app_pool, ids, "strict")
    line = await _line(app_pool, ids, conf="C-77")
    await _money(app_pool, ids, line, "cost", "100", "USD")
    await _money(app_pool, ids, line, "sell", "130", "USD")
    assert await _sqlstate(_confirm(app_pool, ids, line, role="agent")) == "CRM23"  # strict: commission
    result = await _confirm(app_pool, ids, line, role="assistant")  # R18: never blocked
    assert '"pending": ["commission"]' in result
    await _set_level(app_pool, ids, "off")
    await _confirm(app_pool, ids, await _line(app_pool, ids, conf=None))


async def test_a_member_cannot_confirm_around_the_function_and_a_line_is_trashed_while_planning(
    app_pool: asyncpg.Pool,
) -> None:
    ids = await _ledger(app_pool)
    line = await _ready_line(app_pool, ids)
    async with app_pool.acquire() as conn:
        for sql in (
            "UPDATE crm.booking_line SET status = 'confirmed' WHERE booking_line_id = $1",
            "INSERT INTO crm.booking_line (trip_id, agency_supplier_id, title, status) "
            "SELECT trip_id, agency_supplier_id, 'x', 'confirmed' FROM crm.booking_line WHERE booking_line_id = $1",
        ):
            with pytest.raises(asyncpg.PostgresError, match="line_not_editable") as err:
                async with conn.transaction():
                    await _ctx(conn, ids["tenant"], f"member:{ids['members']['agent']}")
                    await conn.execute(sql, line)
            assert err.value.sqlstate == "CRM26"
    await _confirm(app_pool, ids, line)
    trash = TRASH_LINE_SQL
    async with app_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError, match="line_not_editable"):
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], f"member:{ids['members']['agent']}")
                await conn.execute(trash, line)
    async with _as(app_pool, ids["tenant"], "agent", ids["members"]["agent"]) as conn:
        await conn.execute(
            "UPDATE crm.booking_line SET status = 'cancelled', visible_to_travelers = true WHERE booking_line_id = $1",
            line,
        )
        row = await conn.fetchrow(
            "SELECT visible_to_travelers, confirmed_at FROM crm.booking_line WHERE booking_line_id = $1", line
        )
        assert row["visible_to_travelers"] is False and row["confirmed_at"] is not None  # R22
        planning = await conn.fetchval(
            "INSERT INTO crm.booking_line (trip_id, agency_supplier_id, title) VALUES ($1, $2, 'Boat') "
            "RETURNING booking_line_id",
            ids["trip"],
            ids["supplier"],
        )
        await conn.execute(trash, planning)
    assert await _sqlstate(_money(app_pool, ids, line, "cost", "1", "USD")) == "CRM26"  # cancelled: no new money


async def test_the_aa_supplier_is_built_in_and_aa_lines_are_the_feeds(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        assert await conn.fetchval("SELECT crm.ensure_aa_supplier()") == ids["aa"]
        assert await conn.fetchval("SELECT count(*) FROM crm.agency_supplier WHERE kind = 'aa'") == 1
    for sql in (
        "UPDATE crm.agency_supplier SET name = 'AA' WHERE kind = 'aa'",
        "UPDATE crm.agency_supplier SET deleted_at = now(), deleted_by = 'm', purge_after = now() WHERE kind = 'aa'",
        "UPDATE crm.agency_supplier SET kind = 'aa' WHERE kind = 'own'",
        "INSERT INTO crm.agency_supplier (kind, name) VALUES ('aa', 'Adventure Asia 2')",
    ):
        async with app_pool.acquire() as conn:
            with pytest.raises(asyncpg.PostgresError, match="aa_supplier_builtin") as err:
                async with conn.transaction():
                    await _ctx(conn, ids["tenant"], f"member:{ids['members']['owner']}")
                    await conn.execute(sql)
            assert err.value.sqlstate == "CRM22"
    aa_line = (
        "INSERT INTO crm.booking_line (trip_id, supplier_kind, agency_supplier_id, title, aa_sub_status, bk_hold_id) "
        "VALUES ($1, 'aa', $2, 'Halong cruise', 'held', 'hold-1') RETURNING booking_line_id"
    )
    async with app_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError, match="aa_line_read_only") as err:
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], f"member:{ids['members']['owner']}")
                await conn.fetchval(aa_line, ids["trip"], ids["aa"])
        assert err.value.sqlstate == "CRM24"
    async with _as(app_pool, ids["tenant"], actor="job:crm_b2b_events_pull") as conn:
        line = await conn.fetchval(aa_line, ids["trip"], ids["aa"])
    async with app_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError, match="aa_line_read_only"):
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], f"member:{ids['members']['agent']}")
                await conn.execute("UPDATE crm.booking_line SET title = 'x' WHERE booking_line_id = $1", line)
    assert await _sqlstate(_money(app_pool, ids, line, "cost", "1", "USD")) == "CRM24"
    # An agency line may not name the AA row (and the other way round).
    with pytest.raises(asyncpg.CheckViolationError, match="supplier_kind_mismatch"):
        await _line(app_pool, ids, supplier=ids["aa"])


async def test_a_supplier_that_live_lines_name_is_not_trashed(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    await _line(app_pool, ids)
    async with app_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError, match="supplier_in_use") as err:
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], f"member:{ids['members']['owner']}")
                await conn.execute(
                    "UPDATE crm.agency_supplier SET deleted_at = now(), deleted_by = 'm', purge_after = now() "
                    "WHERE agency_supplier_id = $1",
                    ids["supplier"],
                )
        assert err.value.sqlstate == "CRM27"


async def test_fx_resolve_order_staleness_crosses_and_manual_mode(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    a, b = _currency(), _currency()

    async def resolve(conn: asyncpg.Connection, frm: str, to: str) -> tuple[Any, ...] | None:
        row = await conn.fetchrow(f"SELECT * FROM crm.fx_resolve($1, $2, {TODAY})", frm, to)
        return None if row is None else (row["rate"], row["fx_source"], row["stale_days"])

    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        assert await resolve(conn, a, "USD") is None
        assert await resolve(conn, "USD", "USD") == (1, "platform_daily", 0)
        await _rate(conn, a, "USD", "2", days_ago=3)
        assert await resolve(conn, a, "USD") == (Decimal("2"), "platform_daily", 3)
        assert await resolve(conn, "USD", a) == (Decimal("0.5"), "platform_daily", 3)  # inverse
        await _rate(conn, "USD", b, "4", days_ago=3)
        assert await resolve(conn, a, b) == (Decimal("8"), "platform_daily", 3)  # crossed through USD
        await conn.execute(OVERRIDE_SQL, a, "USD", "3", ids["members"]["owner"], 5)
        assert await resolve(conn, a, "USD") == (Decimal("2"), "platform_daily", 3)  # the later day wins
        await conn.execute(OVERRIDE_SQL, a, "USD", "3", ids["members"]["owner"], 3)
        assert await resolve(conn, a, "USD") == (Decimal("3"), "manual", 3)  # an override wins the tie
        await _rate(conn, a, "USD", "2.5")
        assert await resolve(conn, a, "USD") == (Decimal("2.5"), "platform_daily", 0)
        await conn.execute("UPDATE crm.tenant_settings SET fx_mode = 'manual'")
        assert await resolve(conn, a, "USD") == (Decimal("3"), "manual", 3)
        assert await resolve(conn, a, b) is None  # manual mode: no platform cross
    # No rate at all: the confirm refuses and nothing is frozen.
    line = await _ready_line(app_pool, ids, currency=_currency())
    with pytest.raises(asyncpg.PostgresError, match="fx_rate_missing") as err:
        await _confirm(app_pool, ids, line)
    assert err.value.sqlstate == "CRM25"


async def test_the_money_chain_is_linear(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    line = await _ready_line(app_pool, ids)
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await _ctx(admin, ids["tenant"], "seed:test")
        with pytest.raises(asyncpg.CheckViolationError, match="money_chain"):
            async with admin.transaction():
                await admin.execute(
                    "INSERT INTO crm.booking_line_money (booking_line_id, kind, amount, currency) "
                    "VALUES ($1, 'cost', 5, 'USD')",
                    line,
                )
        sell = await admin.fetchval(
            "SELECT booking_line_money_id FROM crm.booking_line_money WHERE booking_line_id = $1 AND kind = 'sell'",
            line,
        )
        with pytest.raises(asyncpg.CheckViolationError, match="money_chain"):
            async with admin.transaction():
                await admin.execute(
                    "INSERT INTO crm.booking_line_money (booking_line_id, kind, amount, currency, supersedes_id) "
                    "VALUES ($1, 'cost', 5, 'USD', $2)",
                    line,
                    sell,
                )
        await admin.execute(
            "INSERT INTO crm.booking_line_money (booking_line_id, kind, amount, currency, supersedes_id) "
            "VALUES ($1, 'sell', 5, 'USD', $2)",
            line,
            sell,
        )
        with pytest.raises(asyncpg.UniqueViolationError):  # a second successor of one row
            async with admin.transaction():
                await admin.execute(
                    "INSERT INTO crm.booking_line_money (booking_line_id, kind, amount, currency, supersedes_id) "
                    "VALUES ($1, 'sell', 6, 'USD', $2)",
                    line,
                    sell,
                )
        with pytest.raises(asyncpg.CheckViolationError):  # a half FX block
            async with admin.transaction():
                await admin.execute(
                    "INSERT INTO crm.booking_line_money (booking_line_id, kind, amount, currency, fx_rate) "
                    "VALUES ($1, 'commission_expected', 5, 'USD', 1)",
                    line,
                )


async def test_erasure_takes_the_ledger_and_the_hold_rule_reaches_it(app_pool: asyncpg.Pool) -> None:
    ids = await _ledger(app_pool)
    await _ready_line(app_pool, ids)
    planning = await _line(app_pool, ids, conf=None)
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        await conn.execute(OVERRIDE_SQL, "EUR", "USD", "1.1", ids["members"]["owner"], 0)
        hold = await conn.fetchval(
            "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) VALUES ('trip', $1, 'dispute') "
            "RETURNING hold_id",
            ids["trip"],
        )
        assert await conn.fetchval("SELECT crm.legal_hold_blocks('booking_line', $1)", planning) is True
        with pytest.raises(asyncpg.PostgresError, match="legal_hold"):
            async with conn.transaction():
                await conn.execute(TRASH_LINE_SQL, planning)
    assert await _sqlstate(_erase(app_pool, ids["tenant"], "booking_line", planning)) == "CRM10"
    async with _as(app_pool, ids["tenant"], "owner", ids["members"]["owner"]) as conn:
        await conn.execute(
            "UPDATE crm.legal_hold SET status = 'released', released_at = now(), released_by = $2 WHERE hold_id = $1",
            hold,
            f"member:{ids['members']['owner']}",
        )
    done = await _erase(app_pool, ids["tenant"], "booking_line", planning)
    assert done["counts"] == {"booking_line_money": 0, "booking_line": 1}
    assert await _sqlstate(_erase(app_pool, ids["tenant"], "agency_supplier", ids["aa"])) == "CRM22"
    assert await _sqlstate(_erase(app_pool, ids["tenant"], "agency_supplier", ids["supplier"])) == "CRM27"
    trip = await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert trip["counts"]["booking_line_money"] == 3 and trip["counts"]["booking_line"] == 1
    supplier = await _erase(app_pool, ids["tenant"], "agency_supplier", ids["supplier"])
    assert supplier["counts"] == {"agency_supplier": 1}
    # The agency erase: the AA row and the overrides go too, no CRM16.
    other = await _ledger(app_pool)
    await _confirm(app_pool, other, await _ready_line(app_pool, other))
    async with _as(app_pool, other["tenant"], "owner", other["members"]["owner"]) as conn:
        await conn.execute(OVERRIDE_SQL, "EUR", "USD", "1.1", other["members"]["owner"], 0)
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    erased = await _erase(app_pool, other["tenant"], "tenant", other["tenant"])
    assert erased["counts"]["agency_supplier"] == 2 and erased["counts"]["fx_rate_override"] == 1
    for table in NEW_TABLES[:-1]:
        assert await _count(app_pool, other["tenant"], table) == 0, table
    assert await _count(app_pool, ids["tenant"], "fx_rate_override") == 1  # another agency is untouched
