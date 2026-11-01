"""crm-038 (AA-959, card CRM-29a) on PostgreSQL with the RDS role shape: workflow rules, runs and the task columns.

- ``crm.workflow_run.trigger_key`` is the idempotency key: a second run of the same rule for the same key is refused
  by the unique key (``ON CONFLICT DO NOTHING`` in the engine), so a tick that runs twice starts nothing twice (card E).
- A run names a trip or a client (exactly one); its state columns stay consistent (active = next_at, no finish).
- ``crm.task``: a custom task (kind ``custom_<hex>``) carries a sealed title and the coded kinds never do; several
  custom tasks stay open on one trip while crm-005's one-open-per-(trip, kind) still holds for the coded kinds; a task
  names a trip or a client, never both.
- crm_app deletes neither a rule nor a run; status moves follow ``crm.status_transition``; RLS isolates both tables.
- ``crm.erase_subject``: the trip scope takes the trip's runs, the traveller scope the client's tasks and runs, the
  agency scope rules and runs (the erase knows every table).
- A hold on the client refuses the change of their run (the hold guard).
- The file runs again as a no-op and is a contract file (no migrate directive).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase
from tests.integration.test_crm014_schema import _as, _members

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "038_crm_workflows_tasks.sql"
SHAPE_SQL = """
SELECT (SELECT count(*) FROM pg_constraint WHERE connamespace = 'crm'::regnamespace) AS constraints,
       (SELECT count(*) FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid
         WHERE c.relnamespace = 'crm'::regnamespace AND NOT g.tgisinternal) AS triggers,
       (SELECT count(*) FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid
         WHERE c.relnamespace = 'crm'::regnamespace) AS policies,
       (SELECT count(*) FROM crm.status_transition) AS transitions,
       (SELECT count(*) FROM pg_indexes WHERE schemaname = 'crm') AS indexes,
       (SELECT relacl::text FROM pg_class WHERE oid = 'crm.workflow_run'::regclass) AS acl
"""
STEPS = '[{"action": "create_task", "kind": "custom", "title": "Check the visa rules"}]'
RULE_SQL = """
INSERT INTO crm.workflow_rule (name, trigger_kind, trigger_value, steps)
VALUES ($1, 'stage_enter', 'booked', $2::jsonb) RETURNING workflow_rule_id
"""
RUN_SQL = """
INSERT INTO crm.workflow_run (workflow_rule_id, rule_version, trip_id, traveler_id, trigger_key, next_at)
VALUES ($1, 1, $2, $3, $4, now())
ON CONFLICT (tenant_id, workflow_rule_id, trigger_key) DO NOTHING
RETURNING workflow_run_id
"""


def _blob() -> bytes:
    return os.urandom(40)


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    ids = await _agency(pool)
    ids["members"] = await _members(pool, ids)
    async with _as(pool, ids["tenant"], "owner", ids["member"]) as conn:
        ids["rule"] = await conn.fetchval(RULE_SQL, "Pre-departure checklist", STEPS)
    return ids


async def test_crm038_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-038'") == 1
    before = dict(await admin.fetchrow(SHAPE_SQL))
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert dict(await admin.fetchrow(SHAPE_SQL)) == before
    text = MIGRATION.read_text(encoding="utf-8")
    assert not any(line.lstrip().lower().startswith("-- migrate:") for line in text.splitlines())  # contract


async def test_crm_app_deletes_neither_rules_nor_runs(admin: asyncpg.Connection) -> None:
    for table in ("workflow_rule", "workflow_run"):
        grants = {
            p: await admin.fetchval("SELECT has_table_privilege('crm_app', $1, $2)", f"crm.{table}", p)
            for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
        assert grants == {"SELECT": True, "INSERT": True, "UPDATE": True, "DELETE": False}, table
        for reader in ("crm_ai_reader", "crm_insights", "crm_support_reader"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, 'SELECT')", reader, f"crm.{table}")


async def test_a_trigger_key_starts_one_run(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    async with _as(app_pool, ids["tenant"], actor="job:crm_workflow_tick") as conn:
        first = await conn.fetchval(RUN_SQL, ids["rule"], ids["trip"], None, "stage:abc")
        again = await conn.fetchval(RUN_SQL, ids["rule"], ids["trip"], None, "stage:abc")
        other = await conn.fetchval(RUN_SQL, ids["rule"], ids["trip"], None, "stage:def")
    assert first is not None and again is None and other is not None


@pytest.mark.parametrize(
    ("trip", "traveler", "key"),
    [(False, False, "manual:x"), (True, True, "manual:y"), (True, False, "Bad key")],
)
async def test_a_run_names_one_subject_and_a_shaped_key(app_pool: asyncpg.Pool, trip: bool, traveler: bool, key: str):
    ids = await _setup(app_pool)
    with pytest.raises(asyncpg.CheckViolationError):
        async with _as(app_pool, ids["tenant"], actor="job:crm_workflow_run") as conn:
            await conn.fetchval(
                RUN_SQL, ids["rule"], ids["trip"] if trip else None, ids["lead"] if traveler else None, key
            )


async def test_run_state_and_status_moves(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    async with _as(app_pool, ids["tenant"], actor="job:crm_workflow_run") as conn:
        run = await conn.fetchval(RUN_SQL, ids["rule"], ids["trip"], None, "manual:one")
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute("UPDATE crm.workflow_run SET status = 'completed' WHERE workflow_run_id = $1", run)
        await conn.execute(
            "UPDATE crm.workflow_run SET status = 'stopped', stop_reason = 'trip_cancelled', next_at = NULL, "
            "finished_at = now() WHERE workflow_run_id = $1",
            run,
        )
        with pytest.raises(asyncpg.PostgresError, match="transition"):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE crm.workflow_run SET status = 'active', stop_reason = NULL, next_at = now(), "
                    "finished_at = NULL WHERE workflow_run_id = $1",
                    run,
                )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute("DELETE FROM crm.workflow_run WHERE workflow_run_id = $1", run)
        await conn.execute("UPDATE crm.workflow_rule SET status = 'archived' WHERE workflow_rule_id = $1", ids["rule"])
        with pytest.raises(asyncpg.PostgresError, match="transition"):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE crm.workflow_rule SET status = 'active' WHERE workflow_rule_id = $1", ids["rule"]
                )


@pytest.mark.parametrize(
    ("trigger_kind", "trigger_value", "offset", "steps"),
    [
        ("manual", "booked", 0, STEPS),
        ("stage_enter", None, 0, STEPS),
        ("stage_enter", "booked", 5, STEPS),
        ("date_anchor", "departure", 500, STEPS),
        ("stage_enter", "Booked", 0, STEPS),
        ("stage_enter", "booked", 0, "[]"),
        ("stage_enter", "booked", 0, '{"a": 1}'),
        ("sometimes", "booked", 0, STEPS),
    ],
)
async def test_the_rule_shape_is_checked(
    app_pool: asyncpg.Pool, trigger_kind: str, trigger_value: str | None, offset: int, steps: str
) -> None:
    ids = await _setup(app_pool)
    with pytest.raises(asyncpg.CheckViolationError):
        async with _as(app_pool, ids["tenant"], "owner", ids["member"]) as conn:
            await conn.execute(
                "INSERT INTO crm.workflow_rule (name, trigger_kind, trigger_value, trigger_offset_days, steps) "
                "VALUES ('Rule', $1, $2, $3, $4::jsonb)",
                trigger_kind,
                trigger_value,
                offset,
                steps,
            )


async def test_one_live_rule_per_library_template(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    insert = (
        "INSERT INTO crm.workflow_rule (name, template_key, template_version, trigger_kind, trigger_value, "
        "trigger_offset_days, steps) VALUES ('Arrival flight', 'arrival_flight_t48h', 1, 'date_anchor', 'departure', "
        "-2, $1::jsonb) RETURNING workflow_rule_id"
    )
    async with _as(app_pool, ids["tenant"], "owner", ids["member"]) as conn:
        first = await conn.fetchval(insert, STEPS)
        with pytest.raises(asyncpg.UniqueViolationError):
            async with conn.transaction():
                await conn.fetchval(insert, STEPS)
        await conn.execute("UPDATE crm.workflow_rule SET status = 'archived' WHERE workflow_rule_id = $1", first)
        assert await conn.fetchval(insert, STEPS) is not None  # an archived copy frees the template


async def test_custom_tasks_carry_a_title_and_stay_open_side_by_side(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    insert = "INSERT INTO crm.task (trip_id, traveler_id, kind, title_enc) VALUES ($1, $2, $3, $4) RETURNING task_id"
    async with _as(app_pool, ids["tenant"], "agent", ids["members"]["agent"]) as conn:
        for _ in range(2):
            await conn.fetchval(insert, ids["trip"], None, f"custom_{uuid.uuid4().hex}", _blob())
        assert await conn.fetchval(insert, None, ids["lead"], f"custom_{uuid.uuid4().hex}", _blob()) is not None
        assert await conn.fetchval(insert, None, None, f"custom_{uuid.uuid4().hex}", _blob()) is not None
        for bad in (
            (ids["trip"], None, f"custom_{uuid.uuid4().hex}", None),  # a custom task without its title
            (ids["trip"], None, "confirm_arrival_flight", _blob()),  # a coded kind with a title
            (ids["trip"], None, f"custom_{uuid.uuid4().hex}", b"short"),  # not a sealed value
            (ids["trip"], ids["lead"], f"custom_{uuid.uuid4().hex}", _blob()),  # a trip and a client
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.fetchval(insert, *bad)
        await conn.fetchval(insert, ids["trip"], None, "confirm_arrival_flight", None)
        with pytest.raises(asyncpg.UniqueViolationError):  # crm-005: one open task of a coded kind per trip
            async with conn.transaction():
                await conn.fetchval(insert, ids["trip"], None, "confirm_arrival_flight", None)


async def test_rls_isolates_rules_and_runs(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    other = await _setup(app_pool)
    async with _as(app_pool, ids["tenant"], actor="job:crm_workflow_run") as conn:
        await conn.fetchval(RUN_SQL, ids["rule"], ids["trip"], None, "manual:a")
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, other["tenant"])
        assert await conn.fetchval("SELECT count(*) FROM crm.workflow_run WHERE tenant_id = $1", ids["tenant"]) == 0
        assert await conn.fetchval("SELECT count(*) FROM crm.workflow_rule WHERE tenant_id = $1", ids["tenant"]) == 0
        with pytest.raises(asyncpg.PostgresError):  # the other agency's rule is not visible to the run's key
            async with conn.transaction():
                await conn.fetchval(RUN_SQL, ids["rule"], other["trip"], None, "manual:b")


async def test_the_erase_takes_the_workflow_rows(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    async with _as(app_pool, ids["tenant"], actor="job:crm_workflow_run") as conn:
        trip_run = await conn.fetchval(RUN_SQL, ids["rule"], ids["trip"], None, "manual:trip")
        client_run = await conn.fetchval(RUN_SQL, ids["rule"], None, ids["companion"], "special:birthday:2026-11-01")
        await conn.execute(
            "INSERT INTO crm.task (trip_id, kind, title_enc, workflow_run_id) VALUES ($1, $2, $3, $4)",
            ids["trip"],
            f"custom_{uuid.uuid4().hex}",
            _blob(),
            trip_run,
        )
        await conn.execute(
            "INSERT INTO crm.task (traveler_id, kind, workflow_run_id) VALUES ($1, 'special_date_birthday', $2)",
            ids["companion"],
            client_run,
        )
    # Trip scope: the trip's tasks, then its runs.
    done = await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert done["counts"]["workflow_run"] == 1 and done["counts"]["task"] == 2
    # Traveller scope (the companion is on no live trip now): the client's own task and run.
    done = await _erase(app_pool, ids["tenant"], "traveler", ids["companion"], ids["token2"])
    assert done["counts"]["task"] == 1 and done["counts"]["workflow_run"] == 1
    assert await _count(app_pool, ids["tenant"], "workflow_run") == 0
    # Agency scope: the rules.
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    done = await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert done["counts"]["workflow_rule"] == 1
    assert await _count(app_pool, ids["tenant"], "workflow_rule") == 0


async def test_a_hold_on_the_client_keeps_their_run(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    async with _as(app_pool, ids["tenant"], actor="job:crm_workflow_run") as conn:
        run = await conn.fetchval(RUN_SQL, ids["rule"], None, ids["lead"], "special:passport_expiry:2027-06-01")
    async with _as(app_pool, ids["tenant"], "owner", ids["member"]) as conn:
        await conn.execute(
            "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) VALUES ('traveler', $1, 'litigation')",
            ids["lead"],
        )
    with pytest.raises(asyncpg.PostgresError, match="legal_hold"):
        async with _as(app_pool, ids["tenant"], actor="job:crm_workflow_run") as conn:
            await conn.execute(
                "UPDATE crm.workflow_run SET traveler_id = NULL, trip_id = $2 WHERE workflow_run_id = $1",
                run,
                ids["trip"],
            )
