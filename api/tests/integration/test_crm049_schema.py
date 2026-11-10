"""crm-049 (AA-1067, an expand file) on PostgreSQL with the RDS role shape: the traveller cancellation policy.

- ``crm.cancellation_policy``: a new row is a draft; a draft changes and is deleted freely; a published version
  changes only its default flag and is retired once, never deleted (CRM49); one live default per agency; a draft is
  never the default.
- ``crm.cancellation_policy_tier``: one tier per days_before_min; changes only while its policy is a draft (CRM49).
- ``crm.cancellation_charge``: written once by a member, never changed (CRM49); its decision CHECK (a reason for an
  override or a waiver, the policy charge as is, nothing without a policy); one row per trip.
- Tenant isolation: another agency neither sees a row nor hangs a tier on a foreign policy.
- Erasure: the trip erase takes the charge row, the agency erase the policies (cascades; ``crm.erase_subject``
  unchanged).
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NEW_TABLES = ("cancellation_policy", "cancellation_policy_tier", "cancellation_charge")


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = "member:test") -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        return await conn.fetchval(sql, *args)


async def _policy(pool: asyncpg.Pool, tenant: uuid.UUID, *, publish: bool = False, default: bool = False) -> uuid.UUID:
    await _run(pool, tenant, "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING")
    policy = await _run(
        pool, tenant, "INSERT INTO crm.cancellation_policy (name) VALUES ('Standard') RETURNING policy_id"
    )
    for days, pct in ((31, 10), (15, 25), (8, 50), (0, 100)):
        await _run(
            pool, tenant,
            "INSERT INTO crm.cancellation_policy_tier (policy_id, days_before_min, charge_pct) VALUES ($1, $2, $3)",
            policy, days, pct,
        )  # fmt: skip
    if publish:
        await _run(pool, tenant,
                   "UPDATE crm.cancellation_policy SET published_at = now(), is_default = $2 WHERE policy_id = $1",
                   policy, default)  # fmt: skip
    return policy


async def _charge(pool: asyncpg.Pool, ids: dict[str, Any], policy: uuid.UUID | None, **over: Any) -> uuid.UUID:
    values = {"decision": "policy", "reason": None, "policy_charge": 250, "charge_amount": 250, **over}
    return await _run(
        pool,
        ids["tenant"],
        "INSERT INTO crm.cancellation_charge (trip_id, policy_id, currency, trip_total, days_before, charge_pct, "
        "policy_charge, charge_amount, decision, reason, paid_amount, kept_amount, refund_due) "
        "VALUES ($1, $2, 'USD', 1000, 20, 25, $3, $4, $5, $6, 300, 250, 50) RETURNING charge_id",
        ids["trip"],
        policy,
        values["policy_charge"],
        values["charge_amount"],
        values["decision"],
        values["reason"],
    )


@pytest.mark.parametrize("table", NEW_TABLES)
async def test_the_readers_have_nothing_on_the_new_tables(admin: asyncpg.Connection, table: str) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-049'") == 1
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege)


async def test_a_draft_is_edited_then_published_and_frozen(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    policy = await _policy(app_pool, t)
    with pytest.raises(asyncpg.CheckViolationError):  # a draft is never the default
        await _run(app_pool, t, "UPDATE crm.cancellation_policy SET is_default = true WHERE policy_id = $1", policy)
    with pytest.raises(asyncpg.UniqueViolationError):  # one tier per day count
        await _run(app_pool, t, "INSERT INTO crm.cancellation_policy_tier (policy_id, days_before_min, charge_pct) "
                   "VALUES ($1, 31, 5)", policy)  # fmt: skip
    await _run(app_pool, t, "UPDATE crm.cancellation_policy_tier SET charge_pct = 20 WHERE policy_id = $1 "
               "AND days_before_min = 31", policy)  # fmt: skip
    await _run(app_pool, t, "UPDATE crm.cancellation_policy SET published_at = now(), is_default = true "
               "WHERE policy_id = $1", policy)  # fmt: skip
    for sql in (
        "UPDATE crm.cancellation_policy_tier SET charge_pct = 30 WHERE policy_id = $1",
        "DELETE FROM crm.cancellation_policy_tier WHERE policy_id = $1",
        "INSERT INTO crm.cancellation_policy_tier (policy_id, days_before_min, charge_pct) VALUES ($1, 60, 0)",
        "UPDATE crm.cancellation_policy SET name = 'Renamed' WHERE policy_id = $1",
        "UPDATE crm.cancellation_policy SET published_at = now() + interval '1 day' WHERE policy_id = $1",
        "DELETE FROM crm.cancellation_policy WHERE policy_id = $1",
    ):
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, t, sql, policy)
        assert err.value.sqlstate == "CRM49", sql
    with pytest.raises(asyncpg.PostgresError) as err:  # a new row is a draft
        await _run(app_pool, t, "INSERT INTO crm.cancellation_policy (name, published_at) VALUES ('X', now())")
    assert err.value.sqlstate == "CRM49"
    # A second published default is refused until the first gives it up; a retire is once and drops the default.
    other = await _policy(app_pool, t, publish=True)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(app_pool, t, "UPDATE crm.cancellation_policy SET is_default = true WHERE policy_id = $1", other)
    await _run(app_pool, t, "UPDATE crm.cancellation_policy SET is_default = false, retired_at = now() "
               "WHERE policy_id = $1", policy)  # fmt: skip
    await _run(app_pool, t, "UPDATE crm.cancellation_policy SET is_default = true WHERE policy_id = $1", other)
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(app_pool, t, "UPDATE crm.cancellation_policy SET retired_at = now() - interval '1 day' "
                   "WHERE policy_id = $1", policy)  # fmt: skip
    assert err.value.sqlstate == "CRM49"
    # A draft is deleted with its tiers.
    draft = await _policy(app_pool, t)
    await _run(app_pool, t, "DELETE FROM crm.cancellation_policy WHERE policy_id = $1", draft)
    assert await _run(app_pool, t, "SELECT count(*) FROM crm.cancellation_policy_tier WHERE policy_id = $1",
                      draft) == 0  # fmt: skip


async def test_another_agency_sees_nothing_and_cannot_hang_a_tier(app_pool: asyncpg.Pool) -> None:
    a = await _agency(app_pool)
    b = await _agency(app_pool)
    policy = await _policy(app_pool, a["tenant"])
    assert await _run(app_pool, b["tenant"], "SELECT count(*) FROM crm.cancellation_policy") == 0
    assert await _run(app_pool, b["tenant"], "SELECT count(*) FROM crm.cancellation_policy_tier") == 0
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _run(app_pool, b["tenant"], "INSERT INTO crm.cancellation_policy_tier (policy_id, days_before_min, "
                   "charge_pct) VALUES ($1, 90, 0)", policy)  # fmt: skip


async def test_the_charge_row_is_written_once_by_a_member(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    policy = await _policy(app_pool, t, publish=True, default=True)
    with pytest.raises(asyncpg.CheckViolationError):  # an override needs a reason
        await _charge(app_pool, ids, policy, decision="override", charge_amount=100)
    with pytest.raises(asyncpg.CheckViolationError):  # a waiver charges nothing
        await _charge(app_pool, ids, policy, decision="waived", reason="Goodwill", charge_amount=10)
    with pytest.raises(asyncpg.CheckViolationError):  # the policy charge is the policy's
        await _charge(app_pool, ids, policy, charge_amount=200)
    with pytest.raises(asyncpg.CheckViolationError):  # no policy, no charge
        await _charge(app_pool, ids, None, decision="no_policy")
    with pytest.raises(asyncpg.PostgresError) as err:  # only a member's cancel writes one
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, t, "job:crm_booking_sync")
            await conn.execute(
                "INSERT INTO crm.cancellation_charge (trip_id, currency, trip_total, policy_charge, charge_amount, "
                "decision, paid_amount, kept_amount, refund_due) VALUES ($1, 'USD', 10, 0, 0, 'no_policy', 0, 0, 0)",
                ids["trip"],
            )
    assert err.value.sqlstate == "CRM49"
    charge = await _charge(app_pool, ids, policy, decision="override", reason="Medical note", charge_amount=100)
    with pytest.raises(asyncpg.UniqueViolationError):  # one per trip
        await _charge(app_pool, ids, policy)
    for sql in ("UPDATE crm.cancellation_charge SET refund_due = 0 WHERE charge_id = $1",
                "DELETE FROM crm.cancellation_charge WHERE charge_id = $1"):  # fmt: skip
        with pytest.raises(asyncpg.PostgresError) as err:
            await _run(app_pool, t, sql, charge)
        assert err.value.sqlstate == "CRM49", sql


async def test_the_erases_take_the_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    policy = await _policy(app_pool, t, publish=True, default=True)
    await _charge(app_pool, ids, policy)
    await _erase(app_pool, t, "trip", ids["trip"])
    assert await _count(app_pool, t, "cancellation_charge") == 0
    assert await _count(app_pool, t, "cancellation_policy") == 1
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, t)
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    await _erase(app_pool, t, "tenant", t)
    assert await _count(app_pool, t, "cancellation_policy") == 0
    assert await _count(app_pool, t, "cancellation_policy_tier") == 0
