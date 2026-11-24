"""S291 (AA-838, crm-088; C4.3 W48 findings 4 and 5) on PostgreSQL with the RDS role shape: the two crm.jobs.*
settings are defined exactly as compiled, a value outside the bounds is refused, crm-088 is registered; the
depth-named savepoints of the API pool roll back exactly what asyncpg's unique names did; a Booking sync run's
remembered ``active`` status meets a lock made in this process at once; the history prune keeps its retention rules.
Rolled back where it writes; invented values only."""

from __future__ import annotations

import json
import uuid

import asyncpg
import pytest

from app import platform_settings as ps
from app import tenancy, tenant_status
from app.errors import Forbidden
from app.jobs import queue
from app.ops import console

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

KEYS = (ps.JOBS_PROPOSAL_CHECK_EVERY_S, ps.JOBS_HISTORY_PRUNE_EVERY_S)
STAFF_SUB = "staff-zz-perf"
STAFF = "platform:zz-staff-1"
REASON = "zz test reason long enough for the check"
PROPOSE = (
    "INSERT INTO crm.setting_value (key, value, version, proposed_by, reason) "
    "VALUES ($1, $2::jsonb, $3, $4, $5) RETURNING id"
)


async def test_the_settings_are_the_compiled_definitions(admin: asyncpg.Connection) -> None:
    for key in KEYS:
        row = await admin.fetchrow(
            "SELECT value_type, default_value::text AS d, min_value::float8 AS lo, max_value::float8 AS hi, area,"
            " scope_kind, approval, env_override, enforced FROM crm.setting_definition WHERE key = $1",
            key,
        )
        d = ps.READER.definition(key)
        assert row is not None, key
        assert row["value_type"] == d.value_type == "int" and json.loads(row["d"]) == d.default_value, key
        assert (row["lo"], row["hi"]) == (float(d.min_value), float(d.max_value)), key
        assert (row["area"], row["scope_kind"], row["approval"]) == ("jobs", "platform", "single"), key
        assert row["env_override"] is None and row["enforced"] is True, key
    assert await admin.fetchval("SELECT 1 AS ok FROM shared.schema_versions WHERE version = 'crm-088'") == 1


async def test_a_value_outside_the_bounds_is_refused(app_pool: asyncpg.Pool) -> None:
    async with app_pool.acquire() as c:
        tx = c.transaction()
        await tx.start()
        try:
            await c.execute("SELECT set_config('app.actor', $1, true)", STAFF)
            for key, bad in ((ps.JOBS_PROPOSAL_CHECK_EVERY_S, "30"), (ps.JOBS_HISTORY_PRUNE_EVERY_S, "10")):
                with pytest.raises(asyncpg.PostgresError):
                    async with c.transaction():
                        await c.fetchval(PROPOSE, key, bad, 1, STAFF, REASON)
            assert await c.fetchval(PROPOSE, ps.JOBS_HISTORY_PRUNE_EVERY_S, "120", 1, STAFF, REASON) is not None
        finally:
            await tx.rollback()


async def test_depth_named_savepoints_roll_back_what_asyncpg_did(app_pool: asyncpg.Pool) -> None:
    """An outer block that fails after an inner block was rolled back and caught rolls back its own work too (one
    fixed name for every savepoint would roll back only to the inner one); a sibling block after it commits."""
    async with app_pool.acquire() as c:
        async with c.transaction():
            await c.execute("CREATE TEMP TABLE zz_sp (v int) ON COMMIT DROP")
            await c.execute("INSERT INTO zz_sp VALUES (1)")
            with pytest.raises(KeyError):
                async with c.transaction():
                    await c.execute("INSERT INTO zz_sp VALUES (2)")
                    with pytest.raises(ValueError):
                        async with c.transaction():
                            await c.execute("INSERT INTO zz_sp VALUES (3)")
                            raise ValueError
                    await c.execute("INSERT INTO zz_sp VALUES (4)")
                    async with c.transaction():
                        await c.execute("INSERT INTO zz_sp VALUES (5)")
                    raise KeyError
            async with c.transaction():
                await c.execute("INSERT INTO zz_sp VALUES (6)")
            rows = [r["v"] for r in await c.fetch("SELECT v FROM zz_sp ORDER BY v")]
            # A failed statement inside a depth-2 block leaves the outer block usable.
            with pytest.raises(asyncpg.PostgresError):
                async with c.transaction():
                    async with c.transaction():
                        await c.execute("SELECT 1 / 0")
            assert await c.fetchval("SELECT count(*) AS n FROM zz_sp") == 2
    assert rows == [1, 6]


async def _as_owner(admin: asyncpg.Connection, tenant: uuid.UUID, sql: str) -> None:
    """One statement as crm_owner inside the agency (FORCE RLS still applies to the owner)."""
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await admin.execute("SELECT set_config('app.actor', 'seed:test', true)")
        await admin.execute(sql, tenant)


async def test_a_lock_or_offboarding_in_this_process_reaches_an_open_booking_run_at_once(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool
) -> None:
    """The review case on PostgreSQL: one agency's Booking sync run (a memo scope) opened a context; the agency is then
    locked by AA staff in this process (app.ops.console), or moved to offboarding by a writer that calls
    tenant_status.leaving: the run's next job context is refused. Without the writer hook (another process), the run
    keeps its "active" to its end and the next run is refused."""
    tenant = uuid.uuid4()
    await console.create_agency(
        app_pool, tenant_id=tenant, agency_name="Zz Perf Travel", is_synthetic=True, default_locale="en-AU",
        environment="test", staff_sub=STAFF_SUB, ticket_ref="SUP-291",
    )  # fmt: skip
    with tenant_status.memo():
        async with tenancy.system_tx(app_pool, tenant, actor="job:crm_booking_sync") as ctx:
            assert ctx.status == "active"
        await console.lock_agency(app_pool, tenant, staff_sub=STAFF_SUB, reason_code="test", ticket_ref="SUP-292")
        for _ in range(2):
            with pytest.raises(Forbidden):
                async with tenancy.system_tx(app_pool, tenant, actor="job:crm_booking_sync"):
                    pass
    await console.unlock_agency(app_pool, tenant, staff_sub=STAFF_SUB, ticket_ref="SUP-293")
    tenant_status.forget()  # a fresh process: the agency may be remembered again
    with tenant_status.memo():
        async with tenancy.system_tx(app_pool, tenant, actor="job:crm_booking_sync"):
            pass
        # Another process offboards it (no hook here): this run keeps its "active" to its end (the documented bound).
        await _as_owner(admin, tenant, "UPDATE crm.tenant SET status = 'offboarding' WHERE tenant_id = $1")
        async with tenancy.system_tx(app_pool, tenant, actor="job:crm_booking_sync"):
            pass
    for _ in range(2):  # the next run, and any job outside a run, read it again
        with tenant_status.memo(), pytest.raises(Forbidden):
            async with tenancy.system_tx(app_pool, tenant, actor="job:crm_booking_sync"):
                pass
    with pytest.raises(Forbidden):
        async with tenancy.system_tx(app_pool, tenant, actor="job:crm_intake_process"):
            pass


async def test_the_history_prune_keeps_its_retention_rules(app_pool: asyncpg.Pool) -> None:
    kind = "crm_heartbeat"
    ids = []
    for status, age_days in (("succeeded", 3), ("cancelled", 3), ("failed", 3), ("succeeded", 0)):
        ids.append(
            await app_pool.fetchval(
                "INSERT INTO crm.job (kind, payload, status, finished_at, idempotency_key) "
                "VALUES ($1, '{}'::jsonb, $2, now() - make_interval(days => $3), $4) RETURNING id::text",
                kind,
                status,
                age_days,
                f"zz-s291-{uuid.uuid4()}",
            )  # fmt: skip
        )
    try:
        assert await queue.prune(app_pool, kind, 2) >= 2
        left = {
            r["id"] for r in await app_pool.fetch("SELECT id::text AS id FROM crm.job WHERE id = ANY($1::uuid[])", ids)
        }
        assert left == {ids[2], ids[3]}  # a failed run and a recent one stay
    finally:
        await app_pool.execute("DELETE FROM crm.job WHERE id = ANY($1::uuid[])", ids)
