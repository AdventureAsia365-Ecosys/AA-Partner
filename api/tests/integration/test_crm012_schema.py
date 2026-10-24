"""crm-012 (AA-943, card CRM-15) on PostgreSQL 15 with the RDS role shape: the Modules 1 + 2 rules in the database.

- Every trip edge of design §3.5 is open and nothing else (lost only before booked, cancelled only from booked /
  pre_departure / travelling).
- **R** (card): qualifying -> proposal under 80 % without an override is refused (CRM19 ``stage2_incomplete``); the
  booking and Stage 3 gates need their override until CRM-18 / CRM-25.
- Undo (Fix B8): only the exact reverse of the trip's latest move, within 15 s, by the same actor, never of a cancel
  (CRM20); crm_app cannot backdate or re-attribute a history row.
- ``client_link`` / ``meeting_slot``: base template, no reader grant (42501), no delete for crm_app, the token lookup
  finds live links only, one live call per agency start time, a Stage 2 submission carries its link's trip and client.
- Erasure: trip, traveller and agency erases succeed with links and slots present; the hold rule reaches them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "012_crm_modules_1_2.sql"
EDGES = {
    ("inquiry", "qualifying"),
    ("qualifying", "inquiry"),
    ("inquiry", "lost"),
    ("qualifying", "lost"),
    ("lost", "inquiry"),
    ("qualifying", "proposal"),
    ("proposal", "qualifying"),
    ("proposal", "booked"),
    ("proposal", "lost"),
    ("booked", "pre_departure"),
    ("booked", "travelling"),
    ("pre_departure", "travelling"),
    ("travelling", "completed"),
    ("booked", "cancelled"),
    ("pre_departure", "cancelled"),
    ("travelling", "cancelled"),
}
GATES = {("qualifying", "proposal"): "stage2", ("proposal", "booked"): "booking", ("booked", "pre_departure"): "stage3"}
FULL = json.dumps({"s1": 100, "s2": 100, "s3": 100})


def _member(ids: dict[str, Any]) -> str:
    return f"member:{ids['member']}"


async def _owner_set(admin: asyncpg.Connection, tenant: uuid.UUID, sql: str, *args: object) -> None:
    """Put a row in a state as crm_owner (FORCE RLS: the agency set first), bypassing no rule but the actor pin."""
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await _ctx(admin, tenant, "seed:test")
        await admin.execute(sql, *args)


async def _put_stage(admin: asyncpg.Connection, ids: dict[str, Any], stage: str) -> None:
    """Walk the trip to ``stage`` along the edges, gates overridden (the arrangement of a test, not the rule)."""
    path = {
        "inquiry": [],
        "qualifying": ["qualifying"],
        "proposal": ["qualifying", "proposal"],
        "booked": ["qualifying", "proposal", "booked"],
        "pre_departure": ["qualifying", "proposal", "booked", "pre_departure"],
        "travelling": ["qualifying", "proposal", "booked", "travelling"],
        "completed": ["qualifying", "proposal", "booked", "travelling", "completed"],
        "lost": ["lost"],
        "cancelled": ["qualifying", "proposal", "booked", "cancelled"],
    }[stage]
    for step in path:
        async with admin.transaction():
            await admin.execute(f"SET LOCAL ROLE {OWNER}")
            await _ctx(admin, ids["tenant"], "seed:test")
            gate = {"proposal": "stage2", "booked": "booking", "pre_departure": "stage3"}.get(step, "")
            await admin.execute("SELECT set_config('app.gate_override', $1, true)", gate)
            await admin.execute(
                "UPDATE crm.trip SET stage = $2, lost_reason_code = $3, cancel_reason_code = $4 WHERE trip_id = $1",
                ids["trip"],
                step,
                "other" if step == "lost" else None,
                "client_request" if step == "cancelled" else None,
            )


async def _move(
    pool: asyncpg.Pool,
    ids: dict[str, Any],
    to: str,
    *,
    actor: str | None = None,
    override: str = "",
    undo: str = "",
    history: bool = True,
    s2: int | None = None,
) -> uuid.UUID | None:
    """One move as crm_app the way the stage engine will make it: settings, the UPDATE, the history row."""
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], actor or _member(ids))
        await conn.execute("SELECT set_config('app.gate_override', $1, true)", override)
        await conn.execute("SELECT set_config('app.stage_undo', $1, true)", undo)
        before = await conn.fetchval("SELECT stage FROM crm.trip WHERE trip_id = $1 FOR UPDATE", ids["trip"])
        if s2 is not None:
            await conn.execute(
                "UPDATE crm.trip SET intake_completeness = jsonb_set(intake_completeness, '{s2}', to_jsonb($2::int)) "
                "WHERE trip_id = $1",
                ids["trip"],
                s2,
            )
        await conn.execute(
            "UPDATE crm.trip SET stage = $2, lost_reason_code = $3, cancel_reason_code = $4 WHERE trip_id = $1",
            ids["trip"],
            to,
            "other" if to == "lost" else None,
            "client_request" if to == "cancelled" else None,
        )
        if not history:
            return None
        return await conn.fetchval(
            "INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code, undo_of, override, "
            "override_reason_code) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING history_id",
            ids["trip"],
            before,
            to,
            "undo" if undo else "agent_move",
            uuid.UUID(undo) if undo else None,
            bool(override),
            "other" if override else None,
        )


async def test_crm012_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-012'") == 1
    before = await admin.fetchval("SELECT count(*) FROM crm.status_transition")
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM crm.status_transition") == before


async def test_the_trip_edges_are_exactly_the_design_ones(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch("SELECT from_status, to_status FROM crm.status_transition WHERE table_name = 'trip'")
    assert {(r["from_status"], r["to_status"]) for r in rows} == EDGES


@pytest.mark.parametrize("edge", sorted(EDGES))
async def test_every_edge_moves_with_its_gate_met(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, edge: tuple[str, str]
) -> None:
    ids = await _agency(app_pool)
    await _put_stage(admin, ids, edge[0])
    await _move(app_pool, ids, edge[1], override=GATES.get(edge, ""))
    assert await _owner_stage(admin, ids) == edge[1]


@pytest.mark.parametrize(
    "edge",
    [
        ("booked", "lost"),
        ("inquiry", "cancelled"),
        ("inquiry", "proposal"),
        ("completed", "travelling"),
        ("cancelled", "booked"),
        ("lost", "qualifying"),
    ],
)
async def test_a_non_edge_is_refused(app_pool: asyncpg.Pool, admin: asyncpg.Connection, edge: tuple[str, str]) -> None:
    ids = await _agency(app_pool)
    await _put_stage(admin, ids, edge[0])
    with pytest.raises(asyncpg.CheckViolationError, match="not an allowed transition"):
        await _move(app_pool, ids, edge[1], override="stage2")


async def _owner_stage(admin: asyncpg.Connection, ids: dict[str, Any]) -> str:
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        await _ctx(admin, ids["tenant"])
        return str(await admin.fetchval("SELECT stage FROM crm.trip WHERE trip_id = $1", ids["trip"]))


async def test_r_the_proposal_gate_needs_80_percent_or_an_override(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    ids = await _agency(app_pool)
    await _put_stage(admin, ids, "qualifying")
    for s2, override in ((60, ""), (79, ""), (79, "booking")):
        with pytest.raises(asyncpg.PostgresError, match="stage2_incomplete") as err:
            await _move(app_pool, ids, "proposal", s2=s2, override=override)
        assert err.value.sqlstate == "CRM19"
    await _move(app_pool, ids, "proposal", s2=80)
    await _move(app_pool, ids, "qualifying")
    await _move(app_pool, ids, "proposal", s2=10, override="stage2")  # the logged override
    assert await _owner_stage(admin, ids) == "proposal"


@pytest.mark.parametrize(
    ("start", "to", "gate"), [("proposal", "booked", "booking"), ("booked", "pre_departure", "stage3")]
)
async def test_the_booking_and_stage3_gates_need_their_override(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, start: str, to: str, gate: str
) -> None:
    ids = await _agency(app_pool)
    await _put_stage(admin, ids, start)
    for override in ("", "stage2"):
        with pytest.raises(asyncpg.PostgresError, match=f"gate_unmet:{gate}") as err:
            await _move(app_pool, ids, to, override=override)
        assert err.value.sqlstate == "CRM19"
    await _move(app_pool, ids, to, override=gate)


async def test_undo_takes_back_the_latest_move_of_its_actor_within_15_s(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    ids = await _agency(app_pool)
    moved = await _move(app_pool, ids, "qualifying")
    # Another actor, a wrong target and a garbage id are refused.
    for kwargs in (
        {"to": "inquiry", "actor": "member:someone-else"},
        {"to": "lost"},
        {"to": "inquiry", "bad": "not-a-uuid"},
        {"to": "inquiry", "bad": str(uuid.uuid4())},
    ):
        bad = kwargs.pop("bad", None)
        with pytest.raises(asyncpg.PostgresError, match="undo_not_allowed") as err:
            await _move(app_pool, ids, kwargs.pop("to"), undo=bad or str(moved), history=False, **kwargs)  # type: ignore[arg-type]
        assert err.value.sqlstate == "CRM20"
    undone = await _move(app_pool, ids, "inquiry", undo=str(moved))
    assert await _owner_stage(admin, ids) == "inquiry"
    # An undo is not undone, and a move that is no longer the latest is not undone either.
    with pytest.raises(asyncpg.PostgresError, match="undo_not_allowed"):
        await _move(app_pool, ids, "qualifying", undo=str(undone), history=False)
    with pytest.raises(asyncpg.PostgresError, match="undo_not_allowed"):
        await _move(app_pool, ids, "qualifying", undo=str(moved), history=False)


async def test_undo_older_than_15_s_or_of_a_cancel_is_refused(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    ids = await _agency(app_pool)
    await _move(app_pool, ids, "qualifying", history=False)
    old = uuid.uuid4()
    await _owner_set(
        admin,
        ids["tenant"],
        "INSERT INTO crm.trip_stage_history (history_id, trip_id, from_stage, to_stage, reason_code, at, created_by) "
        "VALUES ($1, $2, 'inquiry', 'qualifying', 'agent_move', now() - interval '20 seconds', $3)",
        old,
        ids["trip"],
        _member(ids),
    )
    with pytest.raises(asyncpg.PostgresError, match="undo_not_allowed") as err:
        await _move(app_pool, ids, "inquiry", undo=str(old), history=False)
    assert err.value.sqlstate == "CRM20"
    # A cancel is never undone (its effects have their own flow).
    other = await _agency(app_pool)
    await _put_stage(admin, other, "booked")
    cancel = await _move(app_pool, other, "cancelled")
    with pytest.raises(asyncpg.PostgresError, match="undo_not_allowed"):
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, other["tenant"], _member(other))
            await conn.execute("SELECT set_config('app.stage_undo', $1, true)", str(cancel))
            await conn.execute(
                "UPDATE crm.trip SET stage = 'booked', cancel_reason_code = NULL WHERE trip_id = $1", other["trip"]
            )


async def test_a_forged_history_row_cannot_move_a_trip_past_the_edge_table(app_pool: asyncpg.Pool) -> None:
    """crm_app can write a history row of its own (only at and created_by are pinned); naming it in app.stage_undo
    must not move the trip along a non-edge (inquiry -> completed here)."""
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], _member(ids))
            forged = await conn.fetchval(
                "INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code) "
                "VALUES ($1, 'completed', 'inquiry', 'agent_move') RETURNING history_id",
                ids["trip"],
            )
        with pytest.raises(asyncpg.PostgresError, match="undo_not_allowed") as err:
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], _member(ids))
                await conn.execute("SELECT set_config('app.stage_undo', $1, true)", str(forged))
                await conn.execute("UPDATE crm.trip SET stage = 'completed' WHERE trip_id = $1", ids["trip"])
        assert err.value.sqlstate == "CRM20"


async def test_crm_app_cannot_backdate_or_reattribute_a_history_row(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], _member(ids))
        row = await conn.fetchrow(
            "INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code, at, created_by) "
            "VALUES ($1, 'inquiry', 'qualifying', 'agent_move', now() - interval '1 day', 'member:forged') "
            "RETURNING at, created_by, now() AS tx_now",
            ids["trip"],
        )
    assert row["at"] == row["tx_now"] and row["created_by"] == _member(ids)
    other = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, other["tenant"], _member(other))
        foreign = await conn.fetchval(
            "INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code) "
            "VALUES ($1, NULL, 'inquiry', 'intake') RETURNING history_id",
            other["trip"],
        )
    second = await _agency(app_pool)
    async with app_pool.acquire() as conn:
        mine = None
        async with conn.transaction():
            await _ctx(conn, second["tenant"], _member(second))
            mine = await conn.fetchval(
                "INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code) "
                "VALUES ($1, NULL, 'inquiry', 'intake') RETURNING history_id",
                second["trip"],
            )
        with pytest.raises(asyncpg.PostgresError):  # another agency's row: the same-agency key refuses it
            async with conn.transaction():
                await _ctx(conn, second["tenant"], _member(second))
                await conn.execute(
                    "INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code, undo_of) "
                    "VALUES ($1, 'inquiry', 'inquiry_x', 'undo', $2)",
                    second["trip"],
                    foreign,
                )
        third_trip = uuid.uuid4()
        async with conn.transaction():
            await _ctx(conn, second["tenant"], _member(second))
            await conn.execute("INSERT INTO crm.trip (trip_id, title) VALUES ($1, 'Laos 5 nights')", third_trip)
        with pytest.raises(asyncpg.PostgresError, match="undo_not_allowed"):  # same agency, another trip
            async with conn.transaction():
                await _ctx(conn, second["tenant"], _member(second))
                await conn.execute(
                    "INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code, undo_of) "
                    "VALUES ($1, 'qualifying', 'inquiry', 'undo', $2)",
                    third_trip,
                    mine,
                )
        for sql in (
            "INSERT INTO crm.trip_stage_history (trip_id, to_stage, reason_code, override) "
            "VALUES ($1, 'x_y', 'a', true)",
            "INSERT INTO crm.trip_stage_history (trip_id, to_stage, reason_code, override_reason_code) "
            "VALUES ($1, 'x_y', 'a', 'other')",
            "INSERT INTO crm.trip_stage_history (trip_id, to_stage, reason_code, override, override_reason_code) "
            "VALUES ($1, 'x_y', 'a', true, 'bribe')",
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await _ctx(conn, second["tenant"], _member(second))
                    await conn.execute(sql, second["trip"])


async def test_the_new_trip_task_and_settings_columns(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "seed:test")
            await conn.execute("INSERT INTO crm.tenant_settings DEFAULT VALUES")
            row = await conn.fetchrow(
                "SELECT s.timezone, s.stage2_auto_send, s.call_slot_rules, t.intake_completeness, "
                "t.last_activity_at IS NOT NULL AS has_activity, t.cancel_reason_code "
                "FROM crm.tenant_settings s, crm.trip t"
            )
            assert dict(row) == {
                "timezone": "Asia/Ho_Chi_Minh",
                "stage2_auto_send": False,
                "call_slot_rules": "{}",
                "intake_completeness": json.dumps({"s1": 0, "s2": 0, "s3": 0}),
                "has_activity": True,
                "cancel_reason_code": None,
            }
            assert (
                await conn.fetchval(
                    "INSERT INTO crm.task (trip_id, kind) VALUES ($1, 'x') RETURNING priority", ids["trip"]
                )
                == "normal"
            )
            good_rules = {
                "windows": [{"dow": 1, "start": "09:00", "end": "12:00"}],
                "duration_min": 30,
                "horizon_days": 14,
            }
            await conn.execute(
                "UPDATE crm.tenant_settings SET timezone = 'America/Argentina/Buenos_Aires', call_slot_rules = $1",
                json.dumps(good_rules),
            )
            await conn.execute("UPDATE crm.trip SET intake_completeness = $1", FULL)
        bad = [
            ("UPDATE crm.trip SET intake_completeness = $1", json.dumps({"s1": 0, "s2": 101, "s3": 0})),
            ("UPDATE crm.trip SET intake_completeness = $1", json.dumps({"s1": 0, "s2": 50.5, "s3": 0})),
            ("UPDATE crm.trip SET intake_completeness = $1", json.dumps({"s1": 0, "s2": "80", "s3": 0})),
            ("UPDATE crm.trip SET intake_completeness = $1", json.dumps({"s1": 0, "s2": 80})),
            ("UPDATE crm.trip SET intake_completeness = $1", json.dumps({"s1": 0, "s2": 80, "s3": 0, "x": 1})),
            ("UPDATE crm.trip SET cancel_reason_code = $1", "client_request"),  # not cancelled
            ("UPDATE crm.task SET priority = $1", "high"),
            ("UPDATE crm.tenant_settings SET timezone = $1", "Asia/Ho Chi Minh"),
            ("UPDATE crm.tenant_settings SET timezone = $1", "'; DROP"),
            ("UPDATE crm.tenant_settings SET call_slot_rules = $1", json.dumps({"windows": []})),
            (
                "UPDATE crm.tenant_settings SET call_slot_rules = $1",
                json.dumps({**good_rules, "windows": [{"dow": 8, "start": "09:00", "end": "12:00"}]}),
            ),
            (
                "UPDATE crm.tenant_settings SET call_slot_rules = $1",
                json.dumps({**good_rules, "windows": [{"dow": 1, "start": "12:00", "end": "09:00"}]}),
            ),
            ("UPDATE crm.tenant_settings SET call_slot_rules = $1", json.dumps({**good_rules, "duration_min": 5})),
            ("UPDATE crm.tenant_settings SET call_slot_rules = $1", json.dumps({**good_rules, "note": "call me"})),
        ]
        for sql, value in bad:
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await _ctx(conn, ids["tenant"], "seed:test")
                    await conn.execute(sql, value)


# ── client_link, meeting_slot ────────────────────────────────────────────────────────────────────


def _token() -> tuple[str, bytes]:
    raw = os.urandom(32).hex()
    return raw, hashlib.sha256(raw.encode()).digest()


async def _link(
    pool: asyncpg.Pool, ids: dict[str, Any], *, status: str = "sent", days: int = 14
) -> tuple[uuid.UUID, bytes]:
    _raw, digest = _token()
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], _member(ids))
        link = await conn.fetchval(
            "INSERT INTO crm.client_link (trip_id, traveler_id, purpose, token_hash, expires_at) "
            "VALUES ($1, $2, 'stage2_form', $3, now() + make_interval(days => $4)) RETURNING link_id",
            ids["trip"],
            ids["lead"],
            digest,
            days,
        )
        if status == "revoked":
            await conn.execute(
                "UPDATE crm.client_link SET status = 'revoked', revoked_at = now() WHERE link_id = $1", link
            )
    return link, digest


async def _lookup(pool: asyncpg.Pool, digest: bytes | None) -> list[asyncpg.Record]:
    async with pool.acquire() as conn, conn.transaction():
        if digest is not None:
            await conn.execute("SELECT set_config('app.client_link_hash', $1, true)", digest.hex())
        return await conn.fetch("SELECT link_id, tenant_id FROM crm.client_link")


async def test_the_link_lookup_finds_one_live_link_by_its_hash(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    link, digest = await _link(app_pool, ids)
    await _link(app_pool, other)
    revoked, revoked_digest = await _link(app_pool, ids, status="revoked")
    assert await _lookup(app_pool, None) == []  # no hash, no tenant: nothing
    assert [(r["link_id"], r["tenant_id"]) for r in await _lookup(app_pool, digest)] == [(link, ids["tenant"])]
    assert await _lookup(app_pool, revoked_digest) == []
    assert await _lookup(app_pool, _token()[1]) == []
    assert revoked != link
    # Expired by time: the policy hides it without any status move.
    _raw, expired_digest = _token()
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], _member(ids))
        await conn.execute(
            "INSERT INTO crm.client_link (trip_id, traveler_id, purpose, token_hash, expires_at, created_at) "
            "VALUES ($1, $2, 'stage2_form', $3, now() - interval '1 minute', now() - interval '1 day')",
            ids["trip"],
            ids["lead"],
            expired_digest,
        )
    assert await _lookup(app_pool, expired_digest) == []
    # Inside the agency (client_link_tx sets app.tenant_id after the lookup) every link is visible again.
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        assert await conn.fetchval("SELECT count(*) FROM crm.client_link") == 3


async def test_readers_get_nothing_and_crm_app_never_deletes(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    await _link(app_pool, ids)
    async with app_pool.acquire() as conn:
        for table in ("client_link", "meeting_slot"):
            for reader in ("crm_ai_reader", "crm_insights", "crm_support_reader", "crm_eraser"):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with conn.transaction():
                        await _ctx(conn, ids["tenant"])
                        await conn.execute(f"SET LOCAL ROLE {reader}")
                        await conn.fetch(f"SELECT * FROM crm.{table}")  # noqa: S608 — test, fixed names
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await _ctx(conn, ids["tenant"])
                    await conn.execute(f"DELETE FROM crm.{table}")  # noqa: S608 — test, fixed names


async def test_a_link_moves_along_its_statuses_and_keeps_its_shape(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    link, _digest = await _link(app_pool, ids)
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], _member(ids))
            await conn.execute(
                "UPDATE crm.client_link SET status = 'opened', first_opened_at = now() WHERE link_id = $1", link
            )
            await conn.execute(
                "UPDATE crm.client_link SET status = 'submitted', submitted_at = now() WHERE link_id = $1", link
            )
        for sql in (
            "UPDATE crm.client_link SET status = 'sent' WHERE link_id = $1",  # no way back
            "UPDATE crm.client_link SET status = 'revoked' WHERE link_id = $1",  # revoked_at goes with it
            "UPDATE crm.client_link SET expires_at = created_at + interval '31 days' WHERE link_id = $1",
            "UPDATE crm.client_link SET token_hash = '\\x00'::bytea WHERE link_id = $1",
            "UPDATE crm.client_link SET purpose = 'proposal' WHERE link_id = $1",
            "UPDATE crm.client_link SET traveler_id = NULL WHERE link_id = $1",
            "UPDATE crm.client_link SET reissued_from = link_id WHERE link_id = $1",
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await _ctx(conn, ids["tenant"], _member(ids))
                    await conn.execute(sql, link)
    other = await _agency(app_pool)
    with pytest.raises(asyncpg.ForeignKeyViolationError):  # another agency's trip
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, ids["tenant"], _member(ids))
            await conn.execute(
                "INSERT INTO crm.client_link (trip_id, traveler_id, purpose, token_hash, expires_at) "
                "VALUES ($1, $2, 'stage2_form', $3, now() + interval '1 day')",
                other["trip"],
                ids["lead"],
                _token()[1],
            )


async def _slot(conn: asyncpg.Connection, ids: dict[str, Any], starts: str, **cols: Any) -> uuid.UUID:
    values = {
        "trip_id": ids["trip"],
        "starts_at": dt.datetime.fromisoformat(starts),
        "duration_min": 30,
        "source": "form" if "form_submission_id" in cols else "agent",
        **cols,
    }
    return await conn.fetchval(
        f"INSERT INTO crm.meeting_slot ({', '.join(values)}) VALUES "  # noqa: S608 — test, fixed names
        f"({', '.join(f'${i}' for i in range(1, len(values) + 1))}) RETURNING slot_id",
        *values.values(),
    )


async def test_one_live_call_per_agency_start_time(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    starts = "2026-11-02T03:00:00+00:00"
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], _member(ids))
            first = await _slot(conn, ids, starts)
        with pytest.raises(asyncpg.UniqueViolationError):
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], _member(ids))
                await _slot(conn, ids, starts)
        async with conn.transaction():  # another agency has its own calendar
            await _ctx(conn, other["tenant"], _member(other))
            await _slot(conn, other, starts)
        async with conn.transaction():  # a cancelled slot frees the time
            await _ctx(conn, ids["tenant"], _member(ids))
            await conn.execute("UPDATE crm.meeting_slot SET status = 'cancelled' WHERE slot_id = $1", first)
            await _slot(conn, ids, starts)
        for sql in (
            "UPDATE crm.meeting_slot SET status = 'requested' WHERE slot_id = $1",  # cancelled is final
            "UPDATE crm.meeting_slot SET duration_min = 5 WHERE slot_id = $1",
            "UPDATE crm.meeting_slot SET source = 'form' WHERE slot_id = $1",  # form needs its submission
            "UPDATE crm.meeting_slot SET trip_id = NULL WHERE slot_id = $1",  # needs a trip or a traveller
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await _ctx(conn, ids["tenant"], _member(ids))
                    await conn.execute(sql, first)


async def _submission(
    conn: asyncpg.Connection, ids: dict[str, Any], *, stage: str = "s2", link: uuid.UUID | None = None, **cols: Any
) -> uuid.UUID:
    values = {
        "stage": stage,
        "schema_key": "stage2.v1",
        "payload_enc": os.urandom(40),
        "idempotency_key": f"idem-{uuid.uuid4()}",
        "trip_id": ids["trip"],
        "traveler_id": ids["lead"],
        "client_link_id": link,
        **cols,
    }
    names = list(values)
    return await conn.fetchval(
        f"INSERT INTO crm.form_submission ({', '.join(names)}) VALUES "  # noqa: S608 — test, fixed names
        f"({', '.join(f'${i}' for i in range(1, len(names) + 1))}) RETURNING submission_id",
        *values.values(),
    )


async def test_a_stage2_submission_carries_its_links_trip_and_client(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    link, _digest = await _link(app_pool, ids)
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await _ctx(conn, ids["tenant"], "job:test")
            sub = await _submission(conn, ids, link=link)
            await conn.execute(
                "UPDATE crm.form_submission SET status = 'processed', outcome = 'applied', processed_at = now() "
                "WHERE submission_id = $1",
                sub,
            )
            await _submission(conn, ids)  # filled in from a call: no link
        with pytest.raises(asyncpg.ForeignKeyViolationError):  # the companion is not the link's client
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], "job:test")
                await _submission(conn, ids, link=link, traveler_id=ids["companion"])
        with pytest.raises(asyncpg.CheckViolationError):  # a Stage 1 inquiry comes through a public form
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], "job:test")
                await _submission(conn, ids, stage="s1")
        with pytest.raises(asyncpg.CheckViolationError):  # a link answer names its trip and client
            async with conn.transaction():
                await _ctx(conn, ids["tenant"], "job:test")
                await _submission(conn, ids, link=link, trip_id=None)


async def _full_agency(app_pool: asyncpg.Pool) -> dict[str, Any]:
    """The crm-010 agency plus a dispatch row, a link carried by it, a Stage 2 submission through the link and a call
    slot that submission booked for the lead before any trip was named on it."""
    ids = await _agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], _member(ids))
        ids["dispatch"] = await conn.fetchval(
            "INSERT INTO crm.dispatch_queue (traveler_id, trip_id, channel, purpose, origin) "
            "VALUES ($1, $2, 'email', 'transactional', 'user_action') RETURNING dispatch_id",
            ids["lead"],
            ids["trip"],
        )
        ids["link"] = await conn.fetchval(
            "INSERT INTO crm.client_link (trip_id, traveler_id, purpose, token_hash, expires_at, dispatch_id) "
            "VALUES ($1, $2, 'stage2_form', $3, now() + interval '14 days', $4) RETURNING link_id",
            ids["trip"],
            ids["lead"],
            _token()[1],
            ids["dispatch"],
        )
        ids["submission"] = await _submission(conn, ids, link=ids["link"])
        ids["slot"] = await _slot(
            conn,
            ids,
            "2026-11-03T03:00:00+00:00",
            trip_id=None,
            traveler_id=ids["lead"],
            form_submission_id=ids["submission"],
        )
    return ids


async def _trash_trip(pool: asyncpg.Pool, ids: dict[str, Any]) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], _member(ids))
        await conn.execute("UPDATE crm.trip SET deleted_at = now(), deleted_by = 'member:x', purge_after = now()")


async def test_the_trip_erase_takes_its_links_and_slots(app_pool: asyncpg.Pool) -> None:
    ids = await _full_agency(app_pool)
    dry = await _erase(app_pool, ids["tenant"], "trip", ids["trip"], dry_run=True)
    assert dry["counts"]["client_link"] == 1 and dry["counts"]["meeting_slot"] == 0  # the slot names no trip here
    done = await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    assert done["counts"] == {**dry["counts"]}
    assert await _count(app_pool, ids["tenant"], "client_link") == 0
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        row = await conn.fetchrow(
            "SELECT trip_id, client_link_id FROM crm.form_submission WHERE submission_id = $1", ids["submission"]
        )
    assert dict(row) == {"trip_id": None, "client_link_id": None}  # the submission is the client's, unlinked


async def test_the_traveller_erase_takes_their_links_slots_and_submissions(app_pool: asyncpg.Pool) -> None:
    ids = await _full_agency(app_pool)
    await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    done = await _erase(app_pool, ids["tenant"], "traveler", ids["lead"], ids["token"])
    assert done["counts"]["meeting_slot"] == 1 and done["counts"]["form_submission"] == 1
    for table in ("meeting_slot", "client_link", "form_submission"):
        assert await _count(app_pool, ids["tenant"], table) == 0, table


async def test_the_traveller_erase_takes_a_link_on_a_live_trip_before_its_submission(app_pool: asyncpg.Pool) -> None:
    """The companion's link on another trip: erasing the companion (not on that trip's party) deletes the submission,
    then the link, without a foreign key error."""
    ids = await _full_agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], _member(ids))
        await conn.execute("DELETE FROM crm.trip_party WHERE traveler_token = $1", ids["token2"])
        await conn.execute("DELETE FROM crm.trip_traveler WHERE traveler_id = $1", ids["companion"])
        link = await conn.fetchval(
            "INSERT INTO crm.client_link (trip_id, traveler_id, purpose, token_hash, expires_at) "
            "VALUES ($1, $2, 'stage2_form', $3, now() + interval '1 day') RETURNING link_id",
            ids["trip"],
            ids["companion"],
            _token()[1],
        )
        await _submission(conn, ids, link=link, traveler_id=ids["companion"])
    done = await _erase(app_pool, ids["tenant"], "traveler", ids["companion"], ids["token2"])
    assert done["counts"]["client_link"] == 1 and done["counts"]["form_submission"] == 1


async def test_the_agency_erase_empties_links_and_slots(app_pool: asyncpg.Pool) -> None:
    ids = await _full_agency(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    done = await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert done["counts"]["client_link"] == 1 and done["counts"]["meeting_slot"] == 1


async def test_the_hold_rule_reaches_links_and_slots(app_pool: asyncpg.Pool) -> None:
    ids = await _full_agency(app_pool)
    # A client held through the slot alone: the companion is named only on a slot of the trip.
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], _member(ids))
        await _slot(conn, ids, "2026-11-04T03:00:00+00:00", traveler_id=ids["companion"])
        await conn.execute("DELETE FROM crm.trip_party WHERE traveler_token = $1", ids["token2"])
        await conn.execute("DELETE FROM crm.trip_traveler WHERE traveler_id = $1", ids["companion"])
        await conn.execute(
            "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) VALUES ('traveler', $1, 'dispute')",
            ids["companion"],
        )
    for dry_run in (True, False):
        with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
            await _erase(app_pool, ids["tenant"], "trip", ids["trip"], dry_run=dry_run)
        assert err.value.sqlstate == "CRM10"
    # The database refuses moving a held link or slot off its subject on every path (rule 4h).
    for sql in (
        "UPDATE crm.meeting_slot SET traveler_id = $1 WHERE traveler_id = $2",
        "UPDATE crm.client_link SET traveler_id = $1 WHERE traveler_id = $2",
    ):
        async with app_pool.acquire() as conn:
            with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
                async with conn.transaction():
                    await _ctx(conn, ids["tenant"], _member(ids))
                    await conn.execute(
                        "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) "
                        "VALUES ('traveler', $1, 'dispute') ON CONFLICT DO NOTHING",
                        ids["lead"],
                    )
                    await conn.execute(sql, ids["companion"], ids["lead"])
            assert err.value.sqlstate == "CRM10"


async def test_the_traveller_erase_refuses_a_slot_that_names_a_held_client(app_pool: asyncpg.Pool) -> None:
    """A slot booked by the lead's submission but naming the held companion would be deleted by the lead's erase."""
    ids = await _full_agency(app_pool)
    await _erase(app_pool, ids["tenant"], "trip", ids["trip"])
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"], _member(ids))
        await conn.execute(
            "UPDATE crm.meeting_slot SET traveler_id = $1 WHERE slot_id = $2", ids["companion"], ids["slot"]
        )
        await conn.execute(
            "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) VALUES ('traveler', $1, 'dispute')",
            ids["companion"],
        )
    with pytest.raises(asyncpg.PostgresError, match="legal_hold") as err:
        await _erase(app_pool, ids["tenant"], "traveler", ids["lead"], ids["token"], dry_run=True)
    assert err.value.sqlstate == "CRM10"


async def test_the_erase_text_names_the_new_tables(admin: asyncpg.Connection) -> None:
    """crm-014 / crm-015 replace crm.erase_subject again; they must start from this text (S250 schedule §1)."""
    source = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    held = await admin.fetchval("SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_held(text,uuid,uuid)'::regprocedure")
    for table in ("client_link", "meeting_slot"):
        assert f"'{table}'" in source and f"crm.{table}" in held
    guards = {
        r["relname"]
        for r in await admin.fetch(
            "SELECT c.relname FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid "
            "WHERE g.tgfoid = 'crm.guard_held_change()'::regprocedure"
        )
    }
    assert {"client_link", "meeting_slot"} <= guards
