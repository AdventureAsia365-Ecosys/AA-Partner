"""crm-018 (AA-978 follow-up) on PostgreSQL 15 with the RDS role shape: the 90-day window of crm.membership_inbox.

- ``crm.lifecycle_purge_expired`` deletes the agency's inbox rows received more than 90 days ago (a dry run counts
  only), never another agency's, never a recent one, and nothing under an agency legal hold.
- The function stays crm_eraser-only and SECURITY DEFINER; crm_app still cannot delete an inbox row itself.
- Guard 0 refuses to replace a purge function that a later key taught another table.
- The lifecycle job records the count as ``membership_inbox_purged``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.lifecycle import job as lifecycle_job
from app.ops import console
from app.tenancy import lifecycle_tx
from tests.integration.conftest import OWNER
from tests.integration.test_crm010_schema import EVIDENCE_PURGED, _agency, _count, _ctx

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "018_crm_membership_inbox_purge.sql"
FEED = "platform:feed-tenant-membership"


async def _inbox_at(pool: asyncpg.Pool, tenant: uuid.UUID, *days_ago: int) -> None:
    """Inbox rows received ``days_ago`` days ago (written as the feed does, created_at back-dated)."""
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, FEED)
        for days in days_ago:
            await conn.execute(
                "INSERT INTO crm.membership_inbox (event_id, event_type, outcome, reason_code, created_at) "
                "VALUES ($1, 'membership.activated', 'ignored', 'informational', now() - make_interval(days => $2))",
                uuid.uuid4(),
                days,
            )


async def _purge(pool: asyncpg.Pool, tenant: uuid.UUID, *, dry_run: bool) -> dict[str, int]:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant)
        await conn.execute("SET LOCAL ROLE crm_eraser")
        return json.loads(await conn.fetchval("SELECT crm.lifecycle_purge_expired($1)", dry_run))


async def test_crm018_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-018'") == 1
    later = await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-025'")
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        if later:
            # crm-025 (AA-955) replaced the function with more steps: this file refuses to put its older text back.
            with pytest.raises(asyncpg.RaiseError, match="a later key replaced it"):
                await admin.execute(MIGRATION.read_text(encoding="utf-8"))
            await admin.execute("ROLLBACK")  # the file's own BEGIN is still open
        else:
            await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    src = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.lifecycle_purge_expired(boolean)'::regprocedure"
    )
    assert "'membership_inbox'" in src and "'llm_call'" in src and "'placeholder_map'" in src


async def test_the_purge_keeps_90_days_of_inbox_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    await _inbox_at(app_pool, ids["tenant"], 120, 91, 89, 0)
    await _inbox_at(app_pool, other["tenant"], 200)
    expected = {"held": 0, "llm_call": 0, "placeholder_map": 0, "membership_inbox": 2, **EVIDENCE_PURGED}
    assert await _purge(app_pool, ids["tenant"], dry_run=True) == expected
    assert await _count(app_pool, ids["tenant"], "membership_inbox") == 4  # a dry run deletes nothing
    assert await _purge(app_pool, ids["tenant"], dry_run=False) == expected
    assert await _count(app_pool, ids["tenant"], "membership_inbox") == 2  # the 89-day and today's rows
    assert await _count(app_pool, other["tenant"], "membership_inbox") == 1  # FORCE RLS: only app.tenant_id's rows
    assert (await _purge(app_pool, ids["tenant"], dry_run=False))["membership_inbox"] == 0


async def test_an_agency_hold_keeps_the_inbox(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    await _inbox_at(app_pool, ids["tenant"], 100)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], "platform:l1")
        await conn.execute("INSERT INTO crm.legal_hold (scope, reason_code) VALUES ('tenant', 'regulator_request')")
    assert await _purge(app_pool, ids["tenant"], dry_run=False) == {
        "held": 1,
        "llm_call": 0,
        "placeholder_map": 0,
        "membership_inbox": 0,
        **EVIDENCE_PURGED,
    }
    assert await _count(app_pool, ids["tenant"], "membership_inbox") == 1


async def test_the_purge_stays_eraser_only_and_crm_app_still_cannot_delete(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    await _inbox_at(app_pool, ids["tenant"], 100)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, ids["tenant"])
            await conn.fetchval("SELECT crm.lifecycle_purge_expired(false)")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, ids["tenant"])
            await conn.execute("SELECT set_config('app.erasing', 'on', true)")
            await conn.execute("DELETE FROM crm.membership_inbox")
    assert await _count(app_pool, ids["tenant"], "membership_inbox") == 1


async def test_guard0_refuses_a_purge_function_a_later_key_extended(admin: asyncpg.Connection) -> None:
    """A purge function that also erases a table this file does not know (a key between crm-010 and crm-018 replaced
    it) is not overwritten: the file raises and the transaction rolls back."""
    original = await admin.fetchval("SELECT pg_get_functiondef('crm.lifecycle_purge_expired(boolean)'::regprocedure)")
    extended = original.replace(
        "RETURN jsonb_build_object('llm_call', n,",
        "PERFORM crm.erase_rows('some_future_table', 'true', NULL, NULL, true);\n    "
        "RETURN jsonb_build_object('llm_call', n,",
    )
    assert extended != original
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(extended)
        with pytest.raises(asyncpg.RaiseError, match="some_future_table"):
            await admin.execute(MIGRATION.read_text(encoding="utf-8"))
        await admin.execute("ROLLBACK")  # the failed file left its BEGIN open
        await admin.execute(original)
    finally:
        await admin.execute("RESET ROLE")
    src = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.lifecycle_purge_expired(boolean)'::regprocedure"
    )
    assert "some_future_table" not in src


async def test_the_lifecycle_job_counts_the_purged_inbox_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    await _inbox_at(app_pool, ids["tenant"], 95, 5)
    async with lifecycle_tx(app_pool, ids["tenant"], actor=lifecycle_job.ACTOR) as ctx:
        dry = await lifecycle_job.run_agency(ctx, dry_run=True, keys=None, store=None)
    assert dry["membership_inbox_purged"] == 1
    assert await _count(app_pool, ids["tenant"], "membership_inbox") == 2
    async with lifecycle_tx(app_pool, ids["tenant"], actor=lifecycle_job.ACTOR) as ctx:
        done = await lifecycle_job.run_agency(ctx, dry_run=False, keys=None, store=None)
    assert done["membership_inbox_purged"] == 1
    assert await _count(app_pool, ids["tenant"], "membership_inbox") == 1


async def test_the_l1_summary_carries_the_lock_source_and_the_feed(app_pool: asyncpg.Pool) -> None:
    """crm.ops.v1 1.2 (AA-978 follow-up) on the real catalogue: crm_app reads lock_source and the feed singleton inside
    the platform context of the summary."""
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], FEED)
        await conn.execute(
            "UPDATE crm.tenant SET status = 'locked', lock_source = 'cis_membership' WHERE tenant_id = $1",
            ids["tenant"],
        )
    async with app_pool.acquire() as conn:
        await conn.execute(
            "UPDATE crm.membership_feed_state SET last_ok_at = now() - interval '42 seconds', failures = 0"
        )
    summary = await console.agency_summary(app_pool, ids["tenant"], staff_sub="staff-sub-018")
    assert summary["state"] == "locked" and summary["lock_source"] == "cis_membership"
    assert summary["membership_feed_state"] == "ok" and 41 <= summary["membership_feed_lag_s"] <= 60
