"""CRM-15 (AA-943) on PostgreSQL 15 with the RDS role shape, as crm_app: the stage engine, the Stage 2 form, the date
rule, through the same Python calls the routes make.

- **R** (card): Qualifying -> Proposal at 79 % without an override -> 409 ``stage2_incomplete`` with ``missing``; with
  an override -> moved, ``override_reason_code`` on the history row; after "fill in from call" to 80 % -> moved.
- **E** (card): a booked trip on its departure day (agency timezone) -> travelling after one date-rule run, reason
  ``date_rule_gate_unmet``, an open urgent task; Undo within 10 s cancels the auto-sent planning form.
- Undo after 10 s -> 409 ``undo_expired``; bulk dry run = run, a stale preview -> 409; cancel dry run changes nothing,
  cancel from Inquiry -> 409, the assistant -> 403.
- Public Stage 2: a crafted hidden-branch answer is dropped and counted; health without consent -> 422 and nothing
  stored; a bad, revoked or foreign token -> 404 ``link_not_found``; prefill shows bands only.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import asyncpg
import pytest

from app import stages, stages_job
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.intake import public as intake
from app.intake import settings as pipeline_settings
from app.intake import stage2, stage2_public
from app.tenancy import system_tx, tenant_tx
from app.vault import KeyProvider, Vault
from tests.integration.test_crm005_intake import (
    SETTINGS,
    SETTINGS_WITH_ORIGIN,
    _agency,
    _form,
    _keys,
    _owner_fetch,
    _process_all,
    answers,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PAGE = SETTINGS_WITH_ORIGIN.intake_page_origins[0]
PLANNED = {
    "accommodation_tier": "comfort",
    "group_pref": "private",
    "fitness_band": "2_4h",
    "interests": ["food", "culture"],
    "occasion": "anniversary",
    "budget_flex": "some",
    "decision_maker": "partner",
    "booking_timeline": "within_month",
    "competing_quotes": False,
}


async def _trip(pool: asyncpg.Pool, keys: KeyProvider, *roles: str, **over: Any) -> dict[str, Any]:
    """An agency (the roles), a form, one processed inquiry -> a trip at Inquiry with a lead."""
    tenant_id, people = await _agency(pool, *(roles or ("owner",)))
    owner, owner_id = people[0]
    form = await _form(pool, tenant_id, owner)
    await intake.submit(pool, keys, SETTINGS, form.public_key, answers(**over), origin=None)
    [r] = await _process_all(pool, keys, tenant_id)
    return {"tenant": tenant_id, "people": people, "owner": owner, "trip": uuid.UUID(r["trip_id"])}


async def _move(pool: asyncpg.Pool, keys: KeyProvider, t: dict[str, Any], to: str, who: int = 0, **kw: Any) -> Any:
    caller = t["people"][who][0]
    async with tenant_tx(pool, caller, t["tenant"]) as ctx:
        return await stages.move(ctx, t["trip"], to, vault=Vault(keys, ctx), **kw)


async def _owner_one(admin: asyncpg.Connection, t: dict[str, Any], sql: str, *args: Any) -> Any:
    rows = await _owner_fetch(admin, t["tenant"], sql, *args)
    return rows[0] if rows else None


async def test_r_the_proposal_gate_and_its_override(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    t = await _trip(app_pool, keys)
    await _move(app_pool, keys, t, "qualifying")
    with pytest.raises(Conflict) as refused:
        await _move(app_pool, keys, t, "proposal")
    assert refused.value.code == "stage2_incomplete"
    assert refused.value.extra["s2"] == 0 and "accommodation_tier" in refused.value.extra["missing"]
    with pytest.raises(Invalid):
        await _move(app_pool, keys, t, "proposal", override_reason="manual_booking")  # not a Stage 2 reason
    moved = await _move(app_pool, keys, t, "proposal", override_reason="client_in_hurry")
    assert moved["override_reason_code"] == "client_in_hurry" and moved["waiting_on"] == "client"
    row = await _owner_one(
        admin,
        t,
        "SELECT override, override_reason_code FROM crm.trip_stage_history WHERE trip_id = $1 ORDER BY at DESC LIMIT 1",
        t["trip"],
    )
    assert dict(row) == {"override": True, "override_reason_code": "client_in_hurry"}
    # Back to Qualifying, fill the plan in from a call, then the gate opens without an override.
    await _move(app_pool, keys, t, "qualifying")
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        partial = await stage2.fill_from_call(ctx, Vault(keys, ctx), t["trip"], {"accommodation_tier": "comfort"})
    assert partial["completeness"]["s2"] < 80
    tasks = await _owner_fetch(
        admin, t["tenant"], "SELECT status FROM crm.task WHERE trip_id = $1 AND kind = 'get_planning_info'", t["trip"]
    )
    assert [r["status"] for r in tasks] == ["open"]
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        full = await stage2.fill_from_call(ctx, Vault(keys, ctx), t["trip"], PLANNED)
    assert full["completeness"]["s2"] == 100
    tasks = await _owner_fetch(
        admin, t["tenant"], "SELECT status FROM crm.task WHERE trip_id = $1 AND kind = 'get_planning_info'", t["trip"]
    )
    assert [r["status"] for r in tasks] == ["done"]  # reaching 80 % closes "Get planning info"
    moved = await _move(app_pool, keys, t, "proposal")
    assert moved["stage"] == "proposal" and moved["override_reason_code"] is None


async def test_the_booking_gate_and_the_generic_cancel_refusal(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    t = await _trip(app_pool, keys)
    await _move(app_pool, keys, t, "qualifying")
    await _move(app_pool, keys, t, "proposal", override_reason="info_by_phone")
    with pytest.raises(Conflict) as refused:
        await _move(app_pool, keys, t, "booked")
    assert refused.value.code == "gate_unmet" and refused.value.extra["gate"] == "booking"
    await _move(app_pool, keys, t, "booked", override_reason="manual_booking")
    with pytest.raises(Conflict) as refused:
        await _move(app_pool, keys, t, "cancelled")
    assert refused.value.code == "use_cancel_flow"
    with pytest.raises(Conflict) as refused:
        await _move(app_pool, keys, t, "lost", lost_reason="price")
    assert refused.value.code == "transition_not_allowed"


async def test_e_auto_send_and_undo_within_10_s(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    t = await _trip(app_pool, keys)
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        await pipeline_settings.update(ctx, {"stage2_auto_send": True})
    moved = await _move(app_pool, keys, t, "qualifying")
    assert moved["stage2_sent"]["link_path"].startswith("/c/s/") and moved["waiting_on"] == "client"
    dispatch = await _owner_one(admin, t, "SELECT status, origin FROM crm.dispatch_queue WHERE trip_id = $1", t["trip"])
    assert dict(dispatch) == {"status": "queued", "origin": "user_action"}
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        undone = await stages.undo(ctx, t["trip"], uuid.UUID(moved["undo"]["history_id"]))
    assert undone["stage"] == "inquiry" and undone["dispatch_cancelled"] == 1 and undone["links_revoked"] == 1
    dispatch = await _owner_one(
        admin, t, "SELECT status, gate_result FROM crm.dispatch_queue WHERE trip_id = $1", t["trip"]
    )
    assert dict(dispatch) == {"status": "cancelled", "gate_result": "undone"}
    link = await _owner_one(admin, t, "SELECT status FROM crm.client_link WHERE trip_id = $1", t["trip"])
    assert link["status"] == "revoked"
    planning = await _owner_one(
        admin, t, "SELECT status FROM crm.task WHERE trip_id = $1 AND kind = $2", t["trip"], "get_planning_info"
    )
    assert planning["status"] == "skipped"
    # An undo is not undone, and the same move cannot be undone twice.
    with pytest.raises(Conflict) as refused:
        async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
            await stages.undo(ctx, t["trip"], uuid.UUID(moved["undo"]["history_id"]))
    assert refused.value.code == "undo_expired"


async def test_undo_after_10_s_or_by_another_member_is_refused(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    t = await _trip(app_pool, keys, "owner", "agent")
    moved = await _move(app_pool, keys, t, "qualifying")
    with pytest.raises(Conflict) as refused:  # the agent did not make the move
        async with tenant_tx(app_pool, t["people"][1][0], t["tenant"]) as ctx:
            await stages.undo(ctx, t["trip"], uuid.UUID(moved["undo"]["history_id"]))
    assert refused.value.code == "undo_expired"
    # A move 11 s old (written as the owner role, which may set ``at``).
    old = uuid.uuid4()
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(t["tenant"]))
        await admin.execute("SELECT set_config('app.gate_override', 'stage2', true)")
        await admin.execute("UPDATE crm.trip SET stage = 'proposal' WHERE trip_id = $1", t["trip"])
        await admin.execute(
            "INSERT INTO crm.trip_stage_history (history_id, trip_id, from_stage, to_stage, reason_code, at, "
            "created_by) VALUES ($1, $2, 'qualifying', 'proposal', 'agent_move', now() - interval '11 seconds', $3)",
            old,
            t["trip"],
            f"member:{t['people'][0][1]}",
        )
    with pytest.raises(Conflict) as refused:
        async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
            await stages.undo(ctx, t["trip"], old)
    assert refused.value.code == "undo_expired"


async def test_bulk_preview_equals_the_run_and_a_stale_preview_is_refused(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    t = await _trip(app_pool, keys)
    form = await _form(app_pool, t["tenant"], t["owner"])
    for n in range(2):
        other = answers(email=f"x{uuid.uuid4().hex[:8]}@example.test", phone=f"+8491234567{n}")
        await intake.submit(app_pool, keys, SETTINGS, form.public_key, other, origin=None)
    await _process_all(app_pool, keys, t["tenant"])
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        preview = await stages.bulk_move(
            ctx,
            to="qualifying",
            trip_ids=None,
            from_stage="inquiry",
            lost_reason=None,
            override_reason=None,
            dry_run=True,
            expected_hash=None,
        )
    assert len(preview["eligible"]) == 3 and preview["blocked"] == []
    with pytest.raises(Conflict) as refused:
        async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
            await stages.bulk_move(
                ctx,
                to="qualifying",
                trip_ids=None,
                from_stage="inquiry",
                lost_reason=None,
                override_reason=None,
                dry_run=False,
                expected_hash="0" * 64,
            )
    assert refused.value.code == "preview_stale"
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        run = await stages.bulk_move(
            ctx,
            to="qualifying",
            trip_ids=None,
            from_stage="inquiry",
            lost_reason=None,
            override_reason=None,
            dry_run=False,
            expected_hash=preview["preview_hash"],
        )
    assert run["moved"] == 3
    rows = await _owner_fetch(
        admin, t["tenant"], "SELECT DISTINCT batch_id FROM crm.trip_stage_history WHERE batch_id IS NOT NULL"
    )
    assert [str(r["batch_id"]) for r in rows] == [run["batch_id"]]
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        undone = await stages.bulk_undo(ctx, uuid.UUID(run["batch_id"]))
    assert undone["undone"] == 3
    stages_now = await _owner_fetch(admin, t["tenant"], "SELECT stage FROM crm.trip")
    assert {r["stage"] for r in stages_now} == {"inquiry"}


async def test_bulk_override_goes_only_to_trips_whose_gate_still_stands(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """Review round 1: a bulk Qualifying -> Proposal with an override where one trip is already at 100 % moves every
    trip; the complete one carries no override. Wrong reasons are refused before the preview."""
    keys = _keys()
    t = await _trip(app_pool, keys)
    form = await _form(app_pool, t["tenant"], t["owner"])
    other = answers(email=f"x{uuid.uuid4().hex[:8]}@example.test", phone="+84912345670")
    await intake.submit(app_pool, keys, SETTINGS, form.public_key, other, origin=None)
    await _process_all(app_pool, keys, t["tenant"])
    trips = [r["trip_id"] for r in await _owner_fetch(admin, t["tenant"], "SELECT trip_id FROM crm.trip")]
    assert len(trips) == 2
    for trip_id in trips:
        await _move(app_pool, keys, {**t, "trip": trip_id}, "qualifying")
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        full = await stage2.fill_from_call(ctx, Vault(keys, ctx), t["trip"], PLANNED)
    assert full["completeness"]["s2"] == 100
    common: dict[str, Any] = {"to": "proposal", "trip_ids": None, "from_stage": "qualifying", "lost_reason": None}
    for reason, code in (("manual_booking", "invalid_override_reason"), ("bribe", "invalid_override_reason")):
        with pytest.raises(Invalid) as refused:
            async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
                await stages.bulk_move(ctx, **common, override_reason=reason, dry_run=True, expected_hash=None)
        assert refused.value.code == code
    for kw, code in (
        ({"to": "lost", "lost_reason": None, "override_reason": None}, "lost_reason_required"),
        ({"to": "lost", "lost_reason": "price", "override_reason": "other"}, "override_not_needed"),
        ({"to": "inquiry", "lost_reason": "price", "override_reason": None}, "lost_reason_not_allowed"),
    ):
        with pytest.raises(Invalid) as refused:
            async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
                await stages.bulk_move(
                    ctx, trip_ids=None, from_stage="qualifying", dry_run=True, expected_hash=None, **kw
                )
        assert refused.value.code == code
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        preview = await stages.bulk_move(ctx, **common, override_reason="client_in_hurry", dry_run=True,
                                         expected_hash=None)  # fmt: skip
    assert len(preview["eligible"]) == 2 and preview["blocked"] == []
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        run = await stages.bulk_move(ctx, **common, override_reason="client_in_hurry", dry_run=False,
                                     expected_hash=preview["preview_hash"])  # fmt: skip
    assert run["moved"] == 2
    rows = await _owner_fetch(
        admin,
        t["tenant"],
        "SELECT trip_id, override_reason_code FROM crm.trip_stage_history WHERE batch_id = $1",
        uuid.UUID(run["batch_id"]),
    )
    assert {r["trip_id"]: r["override_reason_code"] for r in rows} == {
        t["trip"]: None,
        next(x for x in trips if x != t["trip"]): "client_in_hurry",
    }


async def test_auto_send_never_blocks_the_move(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    """Review round 1: auto-send on, an archived lead -> the trip still reaches Qualifying (stage2_skipped); an
    assistant moving a trip not assigned to them moves it without the send (the message rule)."""
    keys = _keys()
    t = await _trip(app_pool, keys, "owner", "assistant")
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        await pipeline_settings.update(ctx, {"stage2_auto_send": True})
        await ctx.conn.execute(
            "UPDATE crm.traveler SET archived_at = now(), archive_reason = 'agent', archived_by = 'member:test' "
            "WHERE traveler_id = (SELECT traveler_id FROM crm.trip_traveler WHERE trip_id = $1 AND role = 'lead')",
            t["trip"],
        )
    moved = await _move(app_pool, keys, t, "qualifying")
    assert moved["stage"] == "qualifying"
    assert moved["stage2_sent"] is None and moved["stage2_skipped"] == "traveler_archived"
    links = await _owner_fetch(admin, t["tenant"], "SELECT 1 FROM crm.client_link WHERE trip_id = $1", t["trip"])
    assert links == []  # the savepoint took the half-made link back
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        await ctx.conn.execute(
            "UPDATE crm.traveler SET archived_at = NULL, archive_reason = NULL, archived_by = NULL "
            "WHERE traveler_id = (SELECT traveler_id FROM crm.trip_traveler WHERE trip_id = $1 AND role = 'lead')",
            t["trip"],
        )
    await _move(app_pool, keys, t, "inquiry")
    moved = await _move(app_pool, keys, t, "qualifying", who=1)  # the assistant; the trip is not assigned to them
    assert moved["stage"] == "qualifying"
    assert moved["stage2_sent"] is None and moved["stage2_skipped"] == "assigned_only"
    dispatch = await _owner_fetch(admin, t["tenant"], "SELECT 1 FROM crm.dispatch_queue WHERE trip_id = $1", t["trip"])
    assert dispatch == []


async def test_cancel_dry_run_then_run(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    t = await _trip(app_pool, keys, "owner", "assistant")
    with pytest.raises(Conflict) as refused:
        async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
            await stages.cancel(ctx, t["trip"], reason="client_request", dry_run=True)
    assert refused.value.code == "transition_not_allowed"  # from Inquiry: mark it lost instead
    await _move(app_pool, keys, t, "qualifying")
    await _move(app_pool, keys, t, "proposal", override_reason="other")
    await _move(app_pool, keys, t, "booked", override_reason="manual_booking")
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        dry = await stages.cancel(ctx, t["trip"], reason="client_request", dry_run=True)
    assert dry["dry_run"] and {e["kind"] for e in dry["effects"]} >= {"dispatch_cancelled", "tasks_skipped"}
    assert (await _owner_one(admin, t, "SELECT stage FROM crm.trip WHERE trip_id = $1", t["trip"]))["stage"] == "booked"
    with pytest.raises(Invalid):
        async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
            await stages.cancel(ctx, t["trip"], reason="bored", dry_run=False)
    with pytest.raises(Forbidden) as denied:
        async with tenant_tx(app_pool, t["people"][1][0], t["tenant"], action=stages_action()) as ctx:
            await stages.cancel(ctx, t["trip"], reason="client_request", dry_run=False)
    assert denied.value.code == "role_forbidden"
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        done = await stages.cancel(ctx, t["trip"], reason="client_request", dry_run=False)
    assert done["stage"] == "cancelled"
    row = await _owner_one(
        admin, t, "SELECT stage, cancel_reason_code, waiting_on FROM crm.trip WHERE trip_id = $1", t["trip"]
    )
    assert dict(row) == {"stage": "cancelled", "cancel_reason_code": "client_request", "waiting_on": "none"}
    open_tasks = await _owner_fetch(
        admin, t["tenant"], "SELECT 1 FROM crm.task WHERE trip_id = $1 AND status = 'open'", t["trip"]
    )
    assert open_tasks == []


def stages_action() -> Any:
    from app.permissions import Action

    return Action.TRIP_CANCEL


async def test_e_the_date_rule_in_the_agency_timezone(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    t = await _trip(app_pool, keys)
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        await pipeline_settings.update(ctx, {"timezone": "Pacific/Kiritimati"})  # UTC+14: "today" runs ahead
        today = await ctx.conn.fetchval("SELECT (now() AT TIME ZONE 'Pacific/Kiritimati')::date")
        await ctx.conn.execute("UPDATE crm.trip SET start_date = $2 WHERE trip_id = $1", t["trip"], today)
    await _move(app_pool, keys, t, "qualifying")
    await _move(app_pool, keys, t, "proposal", override_reason="other")
    await _move(app_pool, keys, t, "booked", override_reason="manual_booking")
    async with system_tx(app_pool, t["tenant"], actor=stages_job.ACTOR) as ctx:
        counts = await stages_job.run_agency(ctx)
    assert counts["booked_to_travelling"] == 1 and counts["urgent_tasks"] == 1
    hist = await _owner_one(
        admin,
        t,
        "SELECT reason_code, created_by FROM crm.trip_stage_history WHERE trip_id = $1 ORDER BY at DESC LIMIT 1",
        t["trip"],
    )
    assert dict(hist) == {"reason_code": "date_rule_gate_unmet", "created_by": stages_job.ACTOR}
    task = await _owner_one(
        admin,
        t,
        "SELECT priority, status FROM crm.task WHERE trip_id = $1 AND kind = $2",
        t["trip"],
        "date_rule_gate_unmet",
    )
    assert dict(task) == {"priority": "urgent", "status": "open"}
    # Travelling without a return date: one task, however often the rule runs.
    for _ in range(2):
        async with system_tx(app_pool, t["tenant"], actor=stages_job.ACTOR) as ctx:
            await stages_job.run_agency(ctx)
    missing = await _owner_fetch(
        admin, t["tenant"], "SELECT 1 FROM crm.task WHERE trip_id = $1 AND kind = $2", t["trip"], "return_date_missing"
    )
    assert len(missing) == 1
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        await ctx.conn.execute("UPDATE crm.trip SET end_date = $2 WHERE trip_id = $1", t["trip"], today)
    async with system_tx(app_pool, t["tenant"], actor=stages_job.ACTOR) as ctx:
        counts = await stages_job.run_agency(ctx)
    assert counts["travelling_to_completed"] == 1
    # The handler runs every agency in the registry and reports counts only.
    result = await stages_job.date_rule(app_pool, None)  # type: ignore[arg-type]
    assert all(isinstance(v, int) for v in result.values())
    assert stages_job.schedule_key(dt.datetime(2026, 10, 24, 7, 5)) == "crm_stage_date_rule:schedule:2026-10-24T07"


async def test_settings_are_owner_values_checked(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    t = await _trip(app_pool, keys)
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        assert (await pipeline_settings.read(ctx))["timezone"] == "Asia/Ho_Chi_Minh"
    for bad, code in (
        ({"timezone": "Mars/Olympus"}, "invalid_timezone"),
        ({"timezone": "Singapore"}, "invalid_timezone"),  # in pg_timezone_names, not the Area/Location shape
        ({"call_slot_rules": {"windows": []}}, "invalid_call_slot_rules"),
        ({"stage2_auto_send": "yes"}, "invalid_stage2_auto_send"),
    ):
        with pytest.raises(Invalid) as refused:
            async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
                await pipeline_settings.update(ctx, bad)
        assert refused.value.code == code
    rules = {"windows": [{"dow": 2, "start": "09:00", "end": "11:00"}], "duration_min": 30, "horizon_days": 7}
    async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
        out = await pipeline_settings.update(ctx, {"call_slot_rules": rules, "timezone": "Asia/Bangkok"})
    assert out == {"timezone": "Asia/Bangkok", "stage2_auto_send": False, "call_slot_rules": rules}


# ── the public Stage 2 form ──────────────────────────────────────────────────────────────────────


async def _send(pool: asyncpg.Pool, keys: KeyProvider, t: dict[str, Any]) -> str:
    async with tenant_tx(pool, t["owner"], t["tenant"]) as ctx:
        sent = await stage2.send_link(ctx, Vault(keys, ctx), t["trip"])
    return str(sent["link_path"]).removeprefix("/c/s/")


async def test_public_stage2_drops_hidden_answers_and_needs_health_consent(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    t = await _trip(app_pool, keys, trip_style="relaxed")
    token = await _send(app_pool, keys, t)
    page = await stage2_public.read(app_pool, keys, token)
    assert page["trip_style"] == "relaxed" and page["schema_key"] == stage2.SCHEMA_KEY
    assert set(page["prefill"]) <= set(stage2.PROFILE_FIELDS)  # bands only, never F1
    assert not {"name", "email", "phone", "display_name"} & set(json.dumps(page["prefill"]))
    link = await _owner_one(admin, t, "SELECT status FROM crm.client_link WHERE trip_id = $1", t["trip"])
    assert link["status"] == "opened"
    with pytest.raises(Invalid) as refused:
        await stage2_public.submit(
            app_pool, keys, SETTINGS_WITH_ORIGIN, token, {"dietary_flags": ["vegan"]}, origin=PAGE
        )
    assert refused.value.code == "health_consent_required"
    with pytest.raises(Forbidden):
        await stage2_public.submit(app_pool, keys, SETTINGS_WITH_ORIGIN, token, PLANNED, origin="https://evil.test")
    crafted = {**PLANNED, "altitude_ok": True, "trek_experience": "experienced", "hacker": 1}  # hidden for relaxed
    out = await stage2_public.submit(app_pool, keys, SETTINGS_WITH_ORIGIN, token, crafted, origin=PAGE)
    assert out == {"status": "received"}
    sub = await _owner_one(
        admin,
        t,
        "SELECT hidden_answers_dropped, outcome, client_link_id IS NOT NULL AS linked "
        "FROM crm.form_submission WHERE stage = 's2'",
    )
    assert dict(sub) == {"hidden_answers_dropped": 3, "outcome": "applied", "linked": True}
    profile = await _owner_fetch(
        admin,
        t["tenant"],
        "SELECT altitude_ok, trek_experience, accommodation_tier "
        "FROM crm.traveler_profile WHERE accommodation_tier IS NOT NULL",
    )
    assert [dict(r) for r in profile] == [
        {"altitude_ok": None, "trek_experience": None, "accommodation_tier": "comfort"}
    ]
    # The link stays usable until it expires (Fix B11); the page knows it was submitted.
    assert (await stage2_public.read(app_pool, keys, token))["submitted"] is True


async def test_public_stage2_refuses_a_flood_of_unknown_keys(app_pool: asyncpg.Pool) -> None:
    """Review round 1: 1500 junk keys (~13 KB, under the body cap) is a 422, never a 500 from the crm-005
    hidden_answers_dropped CHECK (0..1000); fill from call has the same limit."""
    keys = _keys()
    t = await _trip(app_pool, keys)
    token = await _send(app_pool, keys, t)
    flood = {**PLANNED, **{f"k{n}": 1 for n in range(1500)}}
    with pytest.raises(Invalid) as refused:
        await stage2_public.submit(app_pool, keys, SETTINGS_WITH_ORIGIN, token, flood, origin=PAGE)
    assert refused.value.code == "invalid_submission"
    with pytest.raises(Invalid) as refused:
        async with tenant_tx(app_pool, t["owner"], t["tenant"]) as ctx:
            await stage2.fill_from_call(ctx, Vault(keys, ctx), t["trip"], flood)
    assert refused.value.code == "invalid_submission"


async def test_public_stage2_tokens_cannot_be_probed(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    t = await _trip(app_pool, keys)
    token = await _send(app_pool, keys, t)
    for bad in ("x" * 43, token[:-1] + ("A" if token[-1] != "A" else "B"), ""):
        with pytest.raises(NotFound) as missing:
            await stage2_public.read(app_pool, keys, bad)
        assert missing.value.code == "link_not_found"
    new_token = await _send(app_pool, keys, t)  # a re-send revokes the old link
    with pytest.raises(NotFound):
        await stage2_public.read(app_pool, keys, token)
    assert (await stage2_public.read(app_pool, keys, new_token))["submitted"] is False


async def test_health_with_consent_goes_to_the_f1_health_row(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    t = await _trip(app_pool, keys, trip_style="adventure")
    token = await _send(app_pool, keys, t)
    body = {
        **PLANNED,
        "trek_experience": "some",
        "altitude_ok": True,
        "health_consent": True,
        "dietary_flags": ["vegetarian"],
        "must_do_detail": "Ha Long Bay at sunrise",
    }
    await stage2_public.submit(app_pool, keys, SETTINGS_WITH_ORIGIN, token, body, origin=PAGE)
    consent = await _owner_one(admin, t, "SELECT source, value FROM crm.consent WHERE purpose = 'health_data'")
    assert dict(consent) == {"source": "form", "value": True}
    assert await _owner_one(admin, t, "SELECT 1 FROM crm.traveler_health") is not None
    note = await _owner_one(admin, t, "SELECT kind, body_enc FROM crm.note WHERE kind = 'stage2_free_text'")
    assert note is not None and b"Ha Long" not in bytes(note["body_enc"])  # encrypted
    flags = await _owner_one(
        admin, t, "SELECT dietary_flags, altitude_ok FROM crm.traveler_profile WHERE accommodation_tier IS NOT NULL"
    )
    assert dict(flags) == {"dietary_flags": ["vegetarian"], "altitude_ok": True}


# ── R15 call slots ───────────────────────────────────────────────────────────────────────────────


async def test_r15_two_inquiries_for_one_slot(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    tenant_id, people = await _agency(app_pool, "owner")
    owner = people[0][0]
    rules = {
        "windows": [{"dow": d, "start": "08:00", "end": "20:00"} for d in range(1, 8)],
        "duration_min": 30,
        "horizon_days": 3,
    }
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await pipeline_settings.update(ctx, {"call_slot_rules": rules})
    form = await _form(app_pool, tenant_id, owner)
    shown = await intake.read_form(app_pool, form.public_key)
    assert shown["call_slots"] and shown["call_minutes"] == 30 and shown["timezone"] == "Asia/Ho_Chi_Minh"
    assert all(s.endswith("Z") for s in shown["call_slots"])
    slot = shown["call_slots"][0]
    with pytest.raises(Invalid) as refused:  # a start the form does not offer
        await intake.submit(
            app_pool, keys, SETTINGS, form.public_key, answers(call_slot="2020-01-01T00:00:00Z"), origin=None
        )
    assert refused.value.extra["fields"] == ["call_slot"]
    for n in range(2):  # both accepted while the slot looks free; the job books one
        await intake.submit(
            app_pool,
            keys,
            SETTINGS,
            form.public_key,
            answers(email=f"call{n}@example.test", phone=f"+8491234500{n}", call_slot=slot),
            origin=None,
        )
    await _process_all(app_pool, keys, tenant_id)
    booked = await _owner_fetch(admin, tenant_id, "SELECT status, source FROM crm.meeting_slot")
    assert [dict(r) for r in booked] == [{"status": "requested", "source": "form"}]
    kinds = sorted(
        r["kind"]
        for r in await _owner_fetch(
            admin, tenant_id, "SELECT kind FROM crm.task WHERE kind IN ('call_scheduled', 'call_slot_taken')"
        )
    )
    assert kinds == ["call_scheduled", "call_slot_taken"]
    assert slot not in (await intake.read_form(app_pool, form.public_key))["call_slots"]
    # Stage 1 completeness is on the trips the job created.
    s1 = await _owner_fetch(admin, tenant_id, "SELECT (intake_completeness->>'s1')::int AS s1 FROM crm.trip")
    assert {r["s1"] for r in s1} == {100}
