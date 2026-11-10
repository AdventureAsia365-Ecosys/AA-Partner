"""crm-050 (AA-1067, a contract file) on PostgreSQL with the RDS role shape: instalments and the live schedule status.

- ``crm.payment_schedule``: kind instalment with seq 1..10 (deposit / balance keep seq 1), one live row per proposal,
  kind and seq, percent 0 < pct <= 100; the seal (CRM31) covers seq and percent.
- ``crm.v_schedule_status``: paid_amount (payments minus refund entries), open_amount, live_status partial / overdue /
  paid / due; read through the caller's policies (another agency sees nothing).
- ``crm.proposal.schedule_plan`` (builder versions, an array) and ``cancellation_policy_id``, sealed after draft.
- ``crm.tenant_settings.schedule_template`` shape; ``crm.invoice`` kinds instalment (with its row) and
  cancellation_fee (without one).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _agency, _ctx
from tests.integration.test_crm016_schema import _lifecycle
from tests.integration.test_crm019_schema import _builder, _publish

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "050_crm_payment_schedule_instalments.sql"


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = "member:test") -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        return await conn.fetchval(sql, *args)


async def _rows(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any) -> list[asyncpg.Record]:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, "member:test")
        return list(await conn.fetch(sql, *args))


async def _instalment(pool: asyncpg.Pool, ids: dict[str, Any], seq: int, amount: int, due: str) -> uuid.UUID:
    return await _run(
        pool,
        ids["tenant"],
        "INSERT INTO crm.payment_schedule (trip_id, proposal_id, kind, seq, percent, amount, currency, due_on) "
        f"VALUES ($1, $2, 'instalment', $3, 20, $4, 'USD', {due}) RETURNING schedule_id",  # noqa: S608 — fixed
        ids["trip"],
        ids["proposal"],
        seq,
        amount,
    )


async def _pay(pool: asyncpg.Pool, ids: dict[str, Any], schedule: uuid.UUID, amount: int) -> None:
    await _run(
        pool,
        ids["tenant"],
        "INSERT INTO crm.payment (trip_id, schedule_id, amount, currency, method, received_on) "
        "VALUES ($1, $2, $3, 'USD', 'cash', current_date)",
        ids["trip"],
        schedule,
        amount,
    )


async def test_crm050_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    # Runs first in this file: the crm-016 / crm-019 / crm-024 / crm-048 tests re-apply their own seals; this puts the
    # crm-050 seals back (and proves the file is idempotent).
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-050'") == 1
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    for reader in READERS:
        assert not await admin.fetchval("SELECT has_table_privilege($1, 'crm.v_schedule_status', 'SELECT')", reader)


async def test_instalments_have_their_own_seq_and_one_live_row_each(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    t = ids["tenant"]
    first = await _instalment(app_pool, ids, 1, 200, "current_date + 30")
    await _instalment(app_pool, ids, 2, 100, "current_date + 45")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _instalment(app_pool, ids, 1, 50, "current_date + 40")
    for sql in (
        "INSERT INTO crm.payment_schedule (trip_id, proposal_id, kind, seq, amount, currency, due_on) "
        "VALUES ($1, $2, 'instalment', 11, 10, 'USD', current_date)",
        "INSERT INTO crm.payment_schedule (trip_id, proposal_id, kind, seq, amount, currency, due_on) "
        "VALUES ($1, $2, 'balance', 2, 10, 'USD', current_date)",
        "INSERT INTO crm.payment_schedule (trip_id, proposal_id, kind, seq, percent, amount, currency, due_on) "
        "VALUES ($1, $2, 'instalment', 3, 0, 10, 'USD', current_date)",
        "INSERT INTO crm.payment_schedule (trip_id, proposal_id, kind, amount, currency, due_on) "
        "VALUES ($1, $2, 'extra', 10, 'USD', current_date)",
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, sql, ids["trip"], ids["proposal"])
    # A void instalment frees its seq; a paid one is sealed (seq and percent too).
    await _run(app_pool, t, "UPDATE crm.payment_schedule SET status = 'void' WHERE schedule_id = $1", first)
    again = await _instalment(app_pool, ids, 1, 200, "current_date + 30")
    await _run(app_pool, t, "UPDATE crm.payment_schedule SET status = 'paid', paid_at = now() WHERE schedule_id = $1",
               again)  # fmt: skip
    for col in ("seq = 2", "percent = 25"):
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, t, f"UPDATE crm.payment_schedule SET {col} WHERE schedule_id = $1", again)  # noqa: S608
        assert err.value.sqlstate == "CRM31", col


async def test_the_view_derives_partial_overdue_and_paid(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    t = ids["tenant"]
    late = await _instalment(app_pool, ids, 1, 200, "current_date - 3")
    soon = await _instalment(app_pool, ids, 2, 100, "current_date + 10")
    await _pay(app_pool, ids, late, 50)
    await _pay(app_pool, ids, soon, 40)
    await _pay(app_pool, ids, ids["deposit"], 300)
    by_id = {
        r["schedule_id"]: r
        for r in await _rows(
            app_pool,
            t,
            "SELECT schedule_id, kind, seq, paid_amount, open_amount, live_status, "
            "days_overdue FROM crm.v_schedule_status WHERE trip_id = $1",
            ids["trip"],
        )  # fmt: skip
    }
    assert (by_id[late]["live_status"], by_id[late]["open_amount"], by_id[late]["days_overdue"]) == ("overdue", 150, 3)
    assert (by_id[soon]["live_status"], by_id[soon]["paid_amount"], by_id[soon]["open_amount"]) == ("partial", 40, 60)
    # Paid in full but not settled yet (the invoice path settles it): live_status paid, nothing open.
    assert (by_id[ids["deposit"]]["live_status"], by_id[ids["deposit"]]["open_amount"]) == ("paid", 0)
    assert by_id[ids["balance"]]["live_status"] == "due"
    other = await _agency(app_pool)
    assert await _run(app_pool, other["tenant"], "SELECT count(*) FROM crm.v_schedule_status") == 0


async def test_plan_policy_and_template_shapes_and_the_seal(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    b = await _builder(app_pool, ids)
    plan = json.dumps([{"kind": "deposit", "seq": 1, "percent": "30"}, {"kind": "balance", "seq": 1}])
    policy = uuid.uuid4()
    await _run(app_pool, t, "UPDATE crm.proposal SET schedule_plan = $2::jsonb, cancellation_policy_id = $3 "
               "WHERE proposal_id = $1", b["proposal"], plan, policy)  # fmt: skip
    for value in ('{"kind": "deposit"}', "[]", json.dumps([{"x": "y" * 4100}])):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, "UPDATE crm.proposal SET schedule_plan = $2::jsonb WHERE proposal_id = $1",
                       b["proposal"], value)  # fmt: skip
    await _publish(app_pool, ids, b["proposal"])
    for sql in ("UPDATE crm.proposal SET schedule_plan = NULL WHERE proposal_id = $1",
                "UPDATE crm.proposal SET cancellation_policy_id = NULL WHERE proposal_id = $1"):  # fmt: skip
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, t, sql, b["proposal"])
        assert err.value.sqlstate == "CRM31", sql
    # A text version takes no plan.
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(
            app_pool, t,
            "INSERT INTO crm.proposal (trip_id, version, body_enc, currency, total_amount, deposit_amount, "
            "schedule_plan) VALUES ($1, 9, $2, 'USD', 10, 3, $3::jsonb)",
            ids["trip"], os.urandom(40), plan,
        )  # fmt: skip
    await _run(app_pool, t, "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING")
    await _run(app_pool, t, "UPDATE crm.tenant_settings SET schedule_template = $1::jsonb", plan)
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, t, "UPDATE crm.tenant_settings SET schedule_template = '{}'::jsonb")


async def test_invoice_kinds_instalment_and_cancellation_fee(app_pool: asyncpg.Pool) -> None:
    ids = await _lifecycle(app_pool)
    t = ids["tenant"]
    row = await _instalment(app_pool, ids, 1, 200, "current_date + 30")
    insert = (
        "INSERT INTO crm.invoice (trip_id, kind, schedule_id, proposal_id, currency, tax_rate, tax_label, "
        "tax_inclusive) VALUES ($1, $2, $3, $4, 'USD', 0, 'Tax', true) RETURNING invoice_id"
    )
    assert await _run(app_pool, t, insert, ids["trip"], "instalment", row, ids["proposal"]) is not None
    assert await _run(app_pool, t, insert, ids["trip"], "cancellation_fee", None, ids["proposal"]) is not None
    for kind, schedule in (("instalment", None), ("cancellation_fee", ids["balance"]), ("refund", None)):
        with pytest.raises(asyncpg.CheckViolationError):
            await _run(app_pool, t, insert, ids["trip"], kind, schedule, ids["proposal"])
