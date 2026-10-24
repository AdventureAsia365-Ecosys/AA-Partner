"""The pipeline helpers' Python branches (AA-936): board, trip, moves, assignment, notes, tasks, reveal.

The SQL, the DB-enforced edges and RLS are covered on PostgreSQL by tests/integration/test_crm005_intake.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import asyncpg
import pytest

from app import pipeline
from app.errors import Conflict, Forbidden, Invalid, NotFound
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, OTHER_MEMBER, TENANT, FakeVault, enc, tenant_ctx

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000e1")
LEAD = uuid.UUID("00000000-0000-4000-8000-0000000000e2")


def trip_row(**over: Any) -> dict[str, Any]:
    base = {
        "trip_id": TRIP,
        "title": "Vietnam trip",
        "stage": "inquiry",
        "stage_entered_at": NOW,
        "waiting_on": "agency",
        "start_date": None,
        "end_date": None,
        "month": date(2027, 3, 1),
        "date_flex_days": 0,
        "nights": 10,
        "destinations": ["vietnam"],
        "party_adults": 2,
        "party_child_ages": [7],
        "party_type": "family",
        "budget_band": "2000_3500",
        "trip_style": "balanced",
        "owner_member_id": MEMBER,
        "intake_form_id": None,
        "lead_source": "website_form",
        "lost_reason_code": None,
        "last_inquiry_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(over)
    return base


def board_row(**over: Any) -> dict[str, Any]:
    base = {
        **trip_row(),
        "children": 1,
        "lead_traveler_id": LEAD,
        "lead_name_enc": enc("traveler", "display_name_enc", "Hoa N."),
        "companions": 1,
        "next_due_at": NOW,
        "submissions": 2,
    }
    base.update(over)
    return base


async def test_board_groups_cards_decrypts_names_and_audits_once() -> None:
    conn = (
        ScriptedConn()
        .on(
            "WITH visible AS",
            [
                board_row(),
                board_row(trip_id=uuid.uuid4(), stage="qualifying", lead_name_enc=None, owner_member_id=None),
            ],
        )
        .on("SELECT stage, count(*)", [{"stage": "inquiry", "n": 3}, {"stage": "qualifying", "n": 1}])
        .on("SELECT waiting_on, count(*)", [{"waiting_on": "agency", "n": 4}])
    )
    ctx = tenant_ctx(conn)
    view = await pipeline.board(ctx, FakeVault(), pipeline.BoardFilter(waiting_on="agency", owner="me"))
    stages = {s["stage"]: s for s in view["stages"]}
    assert stages["inquiry"]["cards"][0]["lead_name"] == "Hoa N." and stages["inquiry"]["truncated"]
    assert stages["inquiry"]["cards"][0]["is_mine"] and not stages["qualifying"]["cards"][0]["is_mine"]
    assert stages["qualifying"]["cards"][0]["lead_name"] is None
    assert view["waiting_on_counts"] == {"agency": 4, "client": 0, "aa": 0, "supplier": 0, "none": 0}
    assert view["total"] == 4 and view["filter"] == {"waiting_on": "agency", "owner": "me"}
    assert conn.sql("WITH visible AS")[0][2] == MEMBER  # owner=me filters by the caller
    assert [(e.action, e.fields) for e in ctx.audit_events] == [("list_view", ("display_name",))]


async def test_empty_board_writes_no_list_view() -> None:
    ctx = tenant_ctx(ScriptedConn())
    view = await pipeline.board(ctx, FakeVault(), pipeline.BoardFilter())
    assert view["total"] == 0 and ctx.audit_events == []


async def test_trip_detail_decrypts_party_notes_and_safe_answers() -> None:
    sub = uuid.uuid4()
    payload = '{"budget_band": "2000_3500", "email": "x@y.test", "name": "Hoa"}'
    conn = (
        ScriptedConn()
        .on("FROM crm.trip WHERE trip_id", trip_row())
        .on(
            "FROM crm.trip_traveler tt JOIN",
            [
                {
                    "traveler_id": LEAD,
                    "role": "lead",
                    "preferred_channel": "email",
                    "display_name_enc": enc("traveler", "display_name_enc", "Hoa"),
                    "has_email": True,
                    "has_phone": False,
                }
            ],
        )
        .on(
            "FROM crm.note",
            [
                {
                    "note_id": uuid.uuid4(),
                    "kind": "internal",
                    "body_enc": enc("note", "body_enc", "Call"),
                    "created_at": NOW,
                    "created_by": "member:x",
                }
            ],
        )
        .on(
            "FROM crm.form_submission",
            [
                {
                    "submission_id": sub,
                    "payload_enc": enc("form_submission", "payload_enc", payload),
                    "outcome": "created",
                    "status": "processed",
                    "created_at": NOW,
                },
                {
                    "submission_id": uuid.uuid4(),
                    "payload_enc": None,
                    "outcome": "attached",
                    "status": "processed",
                    "created_at": NOW,
                },
            ],
        )
        .on(
            "FROM crm.task",
            [
                {
                    "task_id": uuid.uuid4(),
                    "kind": "reply_inquiry",
                    "assignee_member_id": None,
                    "status": "open",
                    "due_at": NOW,
                    "done_at": None,
                    "created_at": NOW,
                }
            ],
        )
        .on(
            "FROM crm.trip_stage_history",
            [
                {
                    "from_stage": None,
                    "to_stage": "inquiry",
                    "reason_code": "intake",
                    "at": NOW,
                    "created_by": "job:crm_intake_process",
                }
            ],
        )
    )
    ctx = tenant_ctx(conn)
    out = await pipeline.trip_detail(ctx, FakeVault(), TRIP)
    assert out["party"][0]["display_name"] == "Hoa" and out["notes"][0]["body"] == "Call"
    assert out["submissions"][0]["answers"]["budget_band"] == "2000_3500"
    assert "email" not in out["submissions"][0]["answers"] and out["submissions"][1]["answers"] is None
    assert out["allowed_moves"] == ["qualifying", "lost"] and out["trip"]["month"] == "2027-03-01"
    assert ctx.audit_events[0].action == "view"


async def test_trip_detail_unknown_is_404() -> None:
    with pytest.raises(NotFound):
        await pipeline.trip_detail(tenant_ctx(), FakeVault(), TRIP)


async def test_move_stage_branches() -> None:
    conn = ScriptedConn().on("FOR UPDATE", {"stage": "inquiry", "owner_member_id": None})
    ctx = tenant_ctx(conn)
    moved = await pipeline.move_stage(ctx, TRIP, "qualifying", lost_reason=None)
    assert moved["allowed_moves"] == ["inquiry", "lost"] and conn.sql("trip_stage_history")[0][3] == "agent_move"
    with pytest.raises(Conflict):
        await pipeline.move_stage(ctx, TRIP, "inquiry", lost_reason=None)
    with pytest.raises(Invalid):
        await pipeline.move_stage(ctx, TRIP, "lost", lost_reason="bored")
    with pytest.raises(Invalid):
        await pipeline.move_stage(ctx, TRIP, "qualifying", lost_reason="price")
    lost = await pipeline.move_stage(ctx, TRIP, "lost", lost_reason="price")
    assert lost["stage"] == "lost" and conn.sql("trip_stage_history")[-1][3] == "lost_price"
    await pipeline.move_stage(ctx, TRIP, "qualifying", lost_reason=None, undo=True)
    assert conn.sql("trip_stage_history")[-1][3] == "undo"
    conn.on("UPDATE crm.trip SET stage", asyncpg.CheckViolationError("not allowed"))
    with pytest.raises(Conflict) as refused:
        await pipeline.move_stage(ctx, TRIP, "proposal", lost_reason=None)
    assert refused.value.code == "transition_not_allowed"
    with pytest.raises(NotFound):
        await pipeline.move_stage(tenant_ctx(), TRIP, "qualifying", lost_reason=None)


async def test_assign_rules() -> None:
    unassigned = ScriptedConn().on("FOR UPDATE", {"stage": "inquiry", "owner_member_id": None}).on("FROM crm.member", 1)
    agent = tenant_ctx(unassigned, role="agent")
    assert (await pipeline.assign(agent, TRIP, MEMBER))["is_mine"]
    with pytest.raises(Forbidden):
        await pipeline.assign(agent, TRIP, OTHER_MEMBER)
    taken = ScriptedConn().on("FOR UPDATE", {"stage": "inquiry", "owner_member_id": OTHER_MEMBER})
    with pytest.raises(Forbidden):
        await pipeline.assign(tenant_ctx(taken, role="assistant"), TRIP, MEMBER)
    owner = tenant_ctx(ScriptedConn().on("FOR UPDATE", {"stage": "inquiry", "owner_member_id": OTHER_MEMBER}))
    with pytest.raises(Invalid):  # not an active member
        await pipeline.assign(owner, TRIP, uuid.uuid4())
    assert (await pipeline.assign(owner, TRIP, None))["owner_member_id"] is None
    with pytest.raises(NotFound):
        await pipeline.assign(tenant_ctx(), TRIP, None)


async def test_update_trip_branches() -> None:
    conn = ScriptedConn().on("UPDATE crm.trip SET", trip_row(waiting_on="client", title="Laos slow travel"))
    ctx = tenant_ctx(conn)
    out = await pipeline.update_trip(ctx, TRIP, {"waiting_on": "client", "title": " Laos slow travel "})
    assert out["waiting_on"] == "client" and ctx.audit_events[0].fields == ("title", "waiting_on")
    for bad in ({"waiting_on": "nobody"}, {"title": "  "}, {"title": 5}, {}):
        with pytest.raises(Invalid):
            await pipeline.update_trip(ctx, TRIP, bad)
    with pytest.raises(NotFound):
        await pipeline.update_trip(tenant_ctx(), TRIP, {"waiting_on": "none"})


async def test_add_note_and_task_status() -> None:
    conn = (
        ScriptedConn()
        .on("FROM crm.trip WHERE trip_id", 1)
        .on("INSERT INTO crm.note", {"created_at": NOW, "created_by": "member:x"})
    )
    ctx = tenant_ctx(conn)
    note = await pipeline.add_note(ctx, FakeVault(), TRIP, "  Called, will send planning form ")
    assert note["body"] == "Called, will send planning form" and conn.sql("INSERT INTO crm.note")[0][2].startswith(
        b"enc:"
    )
    with pytest.raises(Invalid):
        await pipeline.add_note(ctx, FakeVault(), TRIP, "   ")
    with pytest.raises(NotFound):
        await pipeline.add_note(tenant_ctx(), FakeVault(), TRIP, "x")

    task = uuid.uuid4()
    row = {
        "task_id": task,
        "kind": "reply_inquiry",
        "assignee_member_id": MEMBER,
        "status": "done",
        "due_at": NOW,
        "done_at": NOW,
        "created_at": NOW,
    }
    tconn = ScriptedConn().on("SELECT status FROM crm.task", "open").on("UPDATE crm.task SET status", row)
    out = await pipeline.set_task_status(tenant_ctx(tconn), task, "done")
    assert out["status"] == "done" and tconn.sql("UPDATE crm.task SET status")[0][2] == f"member:{MEMBER}"
    with pytest.raises(Conflict):
        await pipeline.set_task_status(tenant_ctx(tconn), task, "open" if False else "open")  # noqa: SIM210
    with pytest.raises(Invalid):
        await pipeline.set_task_status(tenant_ctx(tconn), task, "maybe")
    with pytest.raises(NotFound):
        await pipeline.set_task_status(tenant_ctx(), task, "done")
    tconn.on("UPDATE crm.task SET status", asyncpg.CheckViolationError("x"))
    with pytest.raises(Conflict):
        await pipeline.set_task_status(tenant_ctx(tconn), task, "skipped")


async def test_members() -> None:
    # The contact reveal moved to app.travellers.reveal (AA-937, tests/unit/test_travellers.py).
    conn = ScriptedConn().on(
        "FROM crm.member",
        [
            {"member_id": MEMBER, "role": "owner", "status": "active"},
            {"member_id": OTHER_MEMBER, "role": "agent", "status": "active"},
        ],
    )
    assert [m["is_me"] for m in await pipeline.members(tenant_ctx(conn))] == [True, False]
    assert TENANT
