"""The stage engine's Python branches (AA-943, CRM-15): edges, gates, Undo refusals, bulk, cancel, waiting_on, the
hourly date-rule hook. The SQL and the DB rules are covered on PostgreSQL by tests/integration/test_crm012_*.py."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from app import stages, stages_job
from app.errors import Conflict, Invalid, NotFound
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, tenant_ctx

pytestmark = pytest.mark.asyncio

TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000f1")
NOW = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)


def trip(stage: str = "inquiry", s2: int = 0) -> dict[str, Any]:
    return {
        "trip_id": TRIP,
        "stage": stage,
        "owner_member_id": MEMBER,
        "intake_completeness": json.dumps({"s1": 0, "s2": s2, "s3": 0}),
        "lost_reason_code": None,
        "waiting_on": "agency",
    }


def conn_for(stage: str, s2: int = 0) -> ScriptedConn:
    return (
        ScriptedConn()
        .on("FOR UPDATE", trip(stage, s2))
        .on("INSERT INTO crm.trip_stage_history", {"history_id": uuid.uuid4(), "at": NOW})
    )


async def test_the_edge_map_mirrors_the_design() -> None:
    assert stages.allowed_moves("booked") == ["pre_departure", "travelling"]  # cancel is its own flow
    assert "lost" not in stages.ALLOWED_MOVES["booked"] and "cancelled" not in stages.ALLOWED_MOVES["inquiry"]
    assert set(stages.ALLOWED_MOVES) == set(stages.STAGES)
    for (frm, to), gate in stages.GATES.items():
        assert to in stages.ALLOWED_MOVES[frm] and stages.OVERRIDE_REASONS[gate]


async def test_bulk_block_codes_and_the_preview_hash() -> None:
    assert stages._blocked_code("inquiry", "inquiry", 0, None) == "already_in_stage"
    assert stages._blocked_code("inquiry", "booked", 0, None) == "transition_not_allowed"
    assert stages._blocked_code("qualifying", "proposal", 79, None) == "stage2_incomplete"
    assert stages._blocked_code("qualifying", "proposal", 80, None) is None
    assert stages._blocked_code("proposal", "booked", 100, None) == "gate_unmet"
    assert stages._blocked_code("proposal", "booked", 0, "manual_booking") is None
    one = stages.preview_hash("qualifying", ["b", "a"], None, None)
    assert (
        one
        == stages.preview_hash("qualifying", ["a", "b"], None, None)
        != stages.preview_hash("lost", ["a"], None, None)
    )
    assert stages._rows("UPDATE 3") == 3 and stages._rows("OK") == 0 and stages._rows("") == 0


async def test_move_refusals() -> None:
    ctx = tenant_ctx(conn_for("inquiry"))
    for to, code in (
        ("cancelled", "use_cancel_flow"),
        ("inquiry", "already_in_stage"),
        ("booked", "transition_not_allowed"),
    ):
        with pytest.raises(Conflict) as refused:
            await stages.move(ctx, TRIP, to)
        assert refused.value.code == code
    with pytest.raises(Invalid):
        await stages.move(ctx, TRIP, "somewhere")
    with pytest.raises(Invalid):
        await stages.move(ctx, TRIP, "lost", lost_reason="bored")
    with pytest.raises(Invalid):
        await stages.move(ctx, TRIP, "qualifying", lost_reason="price")
    with pytest.raises(Invalid):
        await stages.move(ctx, TRIP, "qualifying", override_reason="other")  # no gate here
    with pytest.raises(NotFound):
        await stages.move(tenant_ctx(), TRIP, "qualifying")


async def test_the_gates_without_a_vault_read_the_stored_percentage() -> None:
    with pytest.raises(Conflict) as refused:
        await stages.move(tenant_ctx(conn_for("qualifying", 79)), TRIP, "proposal")
    assert refused.value.code == "stage2_incomplete" and refused.value.extra["s2"] == 79
    ctx = tenant_ctx(conn_for("qualifying", 85))
    moved = await stages.move(ctx, TRIP, "proposal")
    assert moved["override_reason_code"] is None and moved["waiting_on"] == "client"
    with pytest.raises(Invalid):
        await stages.move(tenant_ctx(conn_for("qualifying", 85)), TRIP, "proposal", override_reason="other")
    with pytest.raises(Invalid):
        await stages.move(tenant_ctx(conn_for("qualifying", 10)), TRIP, "proposal", override_reason="manual_booking")
    conn = conn_for("proposal")
    moved = await stages.move(tenant_ctx(conn), TRIP, "booked", override_reason="manual_booking")
    assert conn.sql("set_config('app.gate_override'") == [("booking",), ()]  # set for the UPDATE, then cleared
    assert moved["waiting_on"] == "none" and ctx.audit_events[-1].reason == "agent_move"


async def test_db_refusals_map_to_409s() -> None:
    gate = asyncpg.PostgresError("crm: stage2_incomplete")
    gate.sqlstate = "CRM19"  # type: ignore[attr-defined]
    conn = conn_for("qualifying", 90).on("UPDATE crm.trip SET stage", gate)
    with pytest.raises(Conflict) as refused:
        await stages.move(tenant_ctx(conn), TRIP, "proposal")
    assert refused.value.code == "gate_unmet"
    conn = conn_for("inquiry").on("UPDATE crm.trip SET stage", asyncpg.CheckViolationError("no"))
    with pytest.raises(Conflict) as refused:
        await stages.move(tenant_ctx(conn), TRIP, "qualifying")
    assert refused.value.code == "transition_not_allowed"
    undo = asyncpg.PostgresError("crm: undo_not_allowed")
    undo.sqlstate = "CRM20"  # type: ignore[attr-defined]
    assert stages._db_refusal(undo, "inquiry").code == "undo_expired"


def last_move(**over: Any) -> dict[str, Any]:
    base = {
        "history_id": uuid.uuid4(),
        "from_stage": "inquiry",
        "to_stage": "qualifying",
        "at": NOW,
        "created_by": f"member:{MEMBER}",
        "undo_of": None,
        "batch_id": None,
    }
    base.update(over)
    return base


@pytest.mark.parametrize(
    "over",
    [
        {"created_by": "member:someone-else"},
        {"undo_of": uuid.uuid4()},
        {"to_stage": "proposal"},
        {"from_stage": None},
        {"at": NOW - timedelta(seconds=11)},
    ],
)
async def test_undo_refusals(over: dict[str, Any]) -> None:
    row = last_move(**over)
    conn = conn_for("qualifying").on("ORDER BY at DESC, created_at DESC LIMIT 2", [row]).on("SELECT now()", NOW)
    with pytest.raises(Conflict) as refused:
        await stages.undo(tenant_ctx(conn), TRIP, row["history_id"])
    assert refused.value.code == "undo_expired"


async def test_undo_takes_back_the_move_and_what_it_queued() -> None:
    row = last_move(from_stage="lost", to_stage="inquiry")
    conn = (
        conn_for("inquiry")
        .on("ORDER BY at DESC, created_at DESC LIMIT 2", [row])
        .on("SELECT now()", NOW + timedelta(seconds=3))
        .on("reason_code LIKE", "lost_price")
    )
    out = await stages.undo(tenant_ctx(conn), TRIP, row["history_id"])
    assert out["stage"] == "lost" and conn.sql("UPDATE crm.trip SET stage")[0][2] == "price"
    assert conn.sql("set_config('app.stage_undo'") == [(str(row["history_id"]),), ()]
    with pytest.raises(NotFound):
        await stages.undo(tenant_ctx(), TRIP, row["history_id"])
    with pytest.raises(Conflict):  # the other move of the same instant: nobody can tell which is the latest
        twin = conn_for("inquiry").on("ORDER BY at DESC, created_at DESC LIMIT 2", [row, last_move()])
        await stages.undo(tenant_ctx(twin.on("SELECT now()", NOW)), TRIP, row["history_id"])


async def test_bulk_and_cancel_input_rules() -> None:
    ctx = tenant_ctx(ScriptedConn())
    for kw in (
        {"to": "cancelled", "trip_ids": [TRIP], "from_stage": None},
        {"to": "nowhere", "trip_ids": [TRIP], "from_stage": None},
        {"to": "lost", "trip_ids": None, "from_stage": None},
        {"to": "lost", "trip_ids": [TRIP], "from_stage": "inquiry"},
        {"to": "lost", "trip_ids": [], "from_stage": None},
        {"to": "lost", "trip_ids": None, "from_stage": "nowhere"},
    ):
        with pytest.raises((Conflict, Invalid)):
            await stages.bulk_move(
                ctx,
                lost_reason=None,
                override_reason=None,
                dry_run=True,
                expected_hash=None,
                **kw,  # type: ignore[arg-type]
            )
    many = ScriptedConn().on("FROM crm.trip", [trip() for _ in range(stages.MAX_BULK + 1)])
    with pytest.raises(Invalid):
        await stages.bulk_move(
            tenant_ctx(many),
            to="lost",
            trip_ids=None,
            from_stage="inquiry",
            lost_reason="price",
            override_reason=None,
            dry_run=True,
            expected_hash=None,
        )
    missing = await stages.bulk_move(
        tenant_ctx(ScriptedConn()),
        to="qualifying",
        trip_ids=[TRIP],
        from_stage=None,
        lost_reason=None,
        override_reason=None,
        dry_run=True,
        expected_hash=None,
    )
    assert missing["blocked"] == [{"trip_id": str(TRIP), "code": "trip_not_found"}] and missing["eligible"] == []
    with pytest.raises(Conflict):
        await stages.bulk_move(
            tenant_ctx(ScriptedConn()),
            to="qualifying",
            trip_ids=[TRIP],
            from_stage=None,
            lost_reason=None,
            override_reason=None,
            dry_run=False,
            expected_hash=missing["preview_hash"],
        )
    with pytest.raises(NotFound):
        await stages.bulk_undo(tenant_ctx(ScriptedConn()), uuid.uuid4())
    with pytest.raises(Invalid):
        await stages.cancel(ctx, TRIP, reason=None, dry_run=True)
    with pytest.raises(NotFound):
        await stages.cancel(ctx, TRIP, reason="other", dry_run=True)


async def test_waiting_on_hooks_answer_for_booked_trips_only() -> None:
    async def supplier(_ctx: Any, facts: stages.TripFacts) -> str | None:
        return "supplier" if facts.stage == "booked" else None

    async def broken(_ctx: Any, _facts: stages.TripFacts) -> str | None:
        return "nobody"

    ctx = tenant_ctx(ScriptedConn())
    try:
        assert await stages.derive_waiting_on(ctx, TRIP, "booked") == "none"
        stages.register_waiting_on_hook(50, "supplier_lines", supplier)
        stages.register_waiting_on_hook(50, "supplier_lines", supplier)  # registering again replaces it
        assert len(stages.WAITING_ON_HOOKS) == 1
        assert await stages.derive_waiting_on(ctx, TRIP, "booked") == "supplier"
        assert await stages.derive_waiting_on(ctx, TRIP, "pre_departure") == "none"
        assert await stages.derive_waiting_on(ctx, TRIP, "travelling") == "none"
        stages.register_waiting_on_hook(10, "broken", broken)
        with pytest.raises(ValueError, match="broken"):
            await stages.derive_waiting_on(ctx, TRIP, "booked")
    finally:
        stages.WAITING_ON_HOOKS.clear()
    assert (
        await stages.derive_waiting_on(tenant_ctx(ScriptedConn().on("crm.client_link", True)), TRIP, "qualifying")
        == "client"
    )
    assert (
        await stages.derive_waiting_on(tenant_ctx(ScriptedConn().on("trip_commitment", True)), TRIP, "booked") == "aa"
    )
    assert await stages.refresh_waiting_on(tenant_ctx(ScriptedConn()), TRIP) is None


async def test_the_hourly_date_rule_hook() -> None:
    from tests.unit.test_job_handlers import FakeEnqueuePool

    clock = {"now": datetime(2026, 10, 24, 4, 5, tzinfo=UTC)}
    hook = stages_job.HourlyStageDateRule(now=lambda: clock["now"])
    pool = FakeEnqueuePool()
    assert await hook(pool) == "job-1"
    kind, payload, key, max_attempts, created_by, _delay = pool.inserts[0]
    assert (kind, key, max_attempts, created_by) == (
        "crm_stage_date_rule",
        "crm_stage_date_rule:schedule:2026-10-24T04",
        3,
        "scheduler:crm-stage-date-rule",
    )
    assert json.loads(payload) == {"purpose": "hourly_schedule"}
    assert await hook(pool) is None
