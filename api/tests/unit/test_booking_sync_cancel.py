"""AA-957 (card CRM-27, C12) in ``crm_booking_sync`` without a database: :func:`app.booking.sync.apply` turns a cancel
answer into the right outcome (every line of the booking closed, the fee on the line at the agency's day, the trip's
pending cancellation finished) or into a failed request with the owner task ``aa_cancel_failed``; a cancel that keeps
getting no answer gives up the same way. The feed's ``booking.cancelled`` closes the lines and finishes a pending
cancel; the other line events and the departure incidents keep their outcomes. The SQL runs on PostgreSQL in
``tests/integration/test_trip_cancel_api.py`` and ``test_aa_lines_api.py``."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest

from app import cancellation, stages
from app.booking import contract, sync
from app.booking.sync import Outcome, Work, apply, apply_event
from app.ledger import fx
from app.ops_release import service as ops_release
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import system_ctx

TRIP = uuid.UUID("00000000-0000-4000-8000-0000000007e1")
TRIP2 = uuid.UUID("00000000-0000-4000-8000-0000000007e2")
LINE = uuid.UUID("00000000-0000-4000-8000-0000000001a1")
SIBLING = uuid.UUID("00000000-0000-4000-8000-0000000001a2")
REQUEST = uuid.UUID("00000000-0000-4000-8000-00000000d001")
FEE_DAY = dt.date(2026, 11, 1)
CANCELLED_AT = dt.datetime(2026, 10, 31, 20, tzinfo=dt.UTC)  # already 1 Nov in the agency's timezone


def flat(sql: str) -> str:
    return " ".join(sql.split())


def cancellation_result(fee: str = "250.00") -> contract.Cancellation:
    return contract.Cancellation(booking_id="b-1", fee=Decimal(fee), currency="USD", refundable=Decimal("750.00"),
                                 fee_pct="25", days_before_departure=40, cancelled_at=CANCELLED_AT)  # fmt: skip


def work(kind: str = "cancel", attempts: int = 1) -> Work:
    request = {"aa_request_id": REQUEST, "kind": kind, "attempts": attempts, "result_code": None}
    return Work(request, {"booking_line_id": LINE, "bk_booking_id": "b-1", "cancel_reason": "client_request"})


def world(*, open_lines: list[dict[str, Any]], status: str = "pending", deleted: bool = False,
          booking_id: str | None = "b-1") -> ScriptedConn:  # fmt: skip
    conn = ScriptedConn()
    conn.on(flat(sync.REQUEST_STATUS_SQL), status)
    conn.on(flat(sync.LINE_SQL), {
        "booking_line_id": LINE, "trip_id": TRIP, "status": "confirmed", "aa_sub_status": None, "bk_hold_id": "h-1",
        "bk_booking_id": booking_id, "trip_stage": "booked", "cancel_reason": "client_request",
        "trip_deleted_at": dt.datetime(2026, 10, 1, tzinfo=dt.UTC) if deleted else None,
    })  # fmt: skip
    conn.on(flat(sync.BOOKING_LINES_SQL), open_lines)
    conn.on(flat(fx.DAY_OF_SQL), FEE_DAY)
    return conn


OPEN_LINES = [{"booking_line_id": LINE, "bk_hold_id": "h-1"}, {"booking_line_id": SIBLING, "bk_hold_id": None}]


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    seen: dict[str, list[Any]] = {"finish": [], "nudge": [], "waiting": []}

    async def finish_if_done(ctx: Any, trip_id: uuid.UUID) -> bool:
        seen["finish"].append(trip_id)
        return True

    async def nudge(ctx: Any, trip_id: uuid.UUID, reason: str) -> None:
        seen["nudge"].append((trip_id, reason))

    async def refresh_waiting_on(ctx: Any, trip_id: uuid.UUID) -> None:
        seen["waiting"].append(trip_id)

    monkeypatch.setattr(cancellation, "finish_if_done", finish_if_done)
    monkeypatch.setattr(ops_release, "nudge", nudge)
    monkeypatch.setattr(stages, "refresh_waiting_on", refresh_waiting_on)
    return seen


# ── a cancel answer ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_cancelled_booking_closes_every_line_books_the_fee_and_finishes_the_trip(
    seams: dict[str, list[Any]],
) -> None:
    conn = world(open_lines=OPEN_LINES)
    ctx = system_ctx(conn)
    out = Outcome(cancellation=cancellation_result(), code="cancelled")
    assert await apply(ctx, work(), out) == "done"
    assert conn.sql(flat(sync.BOOKING_LINES_SQL)) == [(TRIP, "b-1")]
    assert conn.sql(flat(sync.CLOSE_LINE_SQL)) == [(LINE, None, None, "cancelled_by_agency"),
                                                   (SIBLING, None, None, "cancelled_by_agency")]  # fmt: skip
    assert conn.sql(flat(sync.COMMIT_SQL)) == [(TRIP, "cancelled", "h-1")]  # only the line with a hold
    # The fee lands on the request's line at the agency's day of Booking's cancelled_at.
    assert conn.sql(flat(fx.DAY_OF_SQL)) == [(CANCELLED_AT,)]
    assert conn.sql(flat(sync.FEE_SQL)) == [(LINE, Decimal("250.00"), "USD", FEE_DAY)]
    assert conn.sql(flat(sync.FINISH_SQL)) == [(REQUEST, "done", "cancelled")]
    assert conn.sql(flat(sync.TASK_SQL)) == []
    assert seams["nudge"] == [(TRIP, "aa_cancel")] and seams["finish"] == [TRIP] and seams["waiting"] == [TRIP]
    reasons = [e.reason for e in ctx.audit_events]
    assert reasons == ["cancel_cancelled_by_agency", "cancel_cancelled_by_agency", "cancel_cancelled"]


@pytest.mark.asyncio
async def test_a_free_cancel_books_no_fee_and_a_finished_request_is_left_alone(seams: dict[str, list[Any]]) -> None:
    conn = world(open_lines=OPEN_LINES[:1], status="cancelled", deleted=True)
    out = Outcome(cancellation=cancellation_result("0"), code="cancelled")
    assert await apply(system_ctx(conn), work(), out) == "done"
    assert conn.sql(flat(sync.FEE_SQL)) == [] and conn.sql(flat(fx.DAY_OF_SQL)) == []
    assert conn.sql(flat(sync.FINISH_SQL)) == []  # the request is no longer pending
    assert seams["finish"] == [TRIP] and seams["waiting"] == []  # a deleted trip has no waiting_on to refresh


@pytest.mark.asyncio
async def test_not_found_means_nothing_left_at_aa(seams: dict[str, list[Any]]) -> None:
    conn = world(open_lines=OPEN_LINES[:1])
    assert await apply(system_ctx(conn), work(), Outcome(code="not_found", failed=True)) == "done"
    assert conn.sql(flat(sync.CLOSE_LINE_SQL)) == [(LINE, None, None, "not_found")]
    assert conn.sql(flat(sync.FEE_SQL)) == [] and conn.sql(flat(sync.TASK_SQL)) == []
    assert conn.sql(flat(sync.FINISH_SQL)) == [(REQUEST, "done", "not_found")]
    assert seams["finish"] == [TRIP]


@pytest.mark.asyncio
async def test_invalid_counts_as_done_only_when_the_feed_already_cancelled_every_line(
    seams: dict[str, list[Any]],
) -> None:
    conn = world(open_lines=[])  # booking.cancelled closed them before the answer
    assert await apply(system_ctx(conn), work(), Outcome(code="invalid", failed=True)) == "done"
    assert conn.sql(flat(sync.FINISH_SQL)) == [(REQUEST, "done", "invalid")] and seams["finish"] == [TRIP]

    for seen in seams.values():
        seen.clear()
    conn = world(open_lines=OPEN_LINES)  # the booking may still be live at AA
    ctx = system_ctx(conn)
    assert await apply(ctx, work(), Outcome(code="invalid", failed=True)) == "failed"
    assert conn.sql(flat(sync.CLOSE_LINE_SQL)) == []
    assert conn.sql(flat(sync.TASK_SQL)) == [(TRIP, cancellation.CANCEL_FAILED_TASK)]
    assert conn.sql(flat(sync.FINISH_SQL)) == [(REQUEST, "failed", "invalid")]
    assert seams["finish"] == [] and seams["nudge"] == []  # the trip stays "cancellation pending"
    assert ctx.audit_events[-1].reason == "cancel_invalid"


@pytest.mark.asyncio
async def test_a_refused_cancel_fails_with_the_owner_task(seams: dict[str, list[Any]]) -> None:
    conn = world(open_lines=OPEN_LINES)
    assert await apply(system_ctx(conn), work(), Outcome(code="forbidden", failed=True)) == "failed"
    assert conn.sql(flat(sync.TASK_SQL)) == [(TRIP, cancellation.CANCEL_FAILED_TASK)]
    assert conn.sql(flat(sync.CLOSE_LINE_SQL)) == [] and conn.sql(flat(sync.SUB_SQL)) == []
    assert seams["waiting"] == [TRIP]


@pytest.mark.asyncio
async def test_a_line_without_a_booking_id_closes_nothing(seams: dict[str, list[Any]]) -> None:
    conn = world(open_lines=OPEN_LINES, booking_id=None)
    assert await apply(system_ctx(conn), work(), Outcome(code="not_found", failed=True)) == "done"
    assert conn.sql(flat(sync.BOOKING_LINES_SQL)) == [] and conn.sql(flat(sync.CLOSE_LINE_SQL)) == []
    assert seams["finish"] == [TRIP]


# ── no answer ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_cancel_without_an_answer_retries_with_a_backoff(seams: dict[str, list[Any]]) -> None:
    conn = world(open_lines=OPEN_LINES)
    assert await apply(system_ctx(conn), work(attempts=3), Outcome(retry="booking_timeout")) == "retry"
    assert conn.sql(flat(sync.RETRY_SQL)) == [(REQUEST, "booking_timeout", sync.backoff_seconds(3))]
    assert conn.sql(flat(sync.FINISH_SQL)) == [] and conn.sql(flat(sync.TASK_SQL)) == []


@pytest.mark.asyncio
async def test_a_cancel_that_gives_up_opens_the_owner_task_and_keeps_the_trip_pending(
    seams: dict[str, list[Any]],
) -> None:
    conn = world(open_lines=OPEN_LINES)
    ctx = system_ctx(conn)
    out = Outcome(retry="booking_timeout")
    assert await apply(ctx, work(attempts=sync.MAX_ATTEMPTS), out) == "failed"
    assert conn.sql(flat(sync.FINISH_SQL)) == [(REQUEST, "failed", "gave_up")]
    assert conn.sql(flat(sync.TASK_SQL)) == [(TRIP, cancellation.CANCEL_FAILED_TASK)]
    assert conn.sql(flat(sync.SUB_SQL)) == []  # the line is not marked failed: Booking may still cancel it
    assert [e.reason for e in ctx.audit_events] == ["cancel_gave_up"]
    assert seams["waiting"] == [TRIP] and seams["finish"] == []
    # A deleted trip: the task still opens, nothing to refresh.
    seams["waiting"].clear()
    conn = world(open_lines=OPEN_LINES, deleted=True)
    assert await apply(system_ctx(conn), work(attempts=sync.MAX_ATTEMPTS), out) == "failed"
    assert seams["waiting"] == []


@pytest.mark.asyncio
async def test_a_quote_that_gives_up_marks_a_pending_line_failed_instead(seams: dict[str, list[Any]]) -> None:
    conn = world(open_lines=[]).on(flat(sync.LINE_SQL), {
        "booking_line_id": LINE, "trip_id": TRIP, "status": "planning", "aa_sub_status": "pending",
        "bk_hold_id": None, "bk_booking_id": None, "trip_deleted_at": None,
    })  # fmt: skip
    assert await apply(system_ctx(conn), work("quote", sync.MAX_ATTEMPTS), Outcome(retry="booking_5xx")) == "failed"
    assert conn.sql(flat(sync.SUB_SQL)) == [(LINE, "failed", "gave_up")]
    assert conn.sql(flat(sync.TASK_SQL)) == []


@pytest.mark.asyncio
async def test_a_request_dropped_for_a_closed_trip_is_cancelled_without_a_write(seams: dict[str, list[Any]]) -> None:
    conn = world(open_lines=OPEN_LINES)
    ctx = system_ctx(conn)
    assert await apply(ctx, work("hold"), Outcome(code="trip_closed", cancelled=True)) == "cancelled"
    assert conn.sql(flat(sync.FINISH_SQL)) == [(REQUEST, "cancelled", "trip_closed")]
    assert [e.reason for e in ctx.audit_events] == ["hold_trip_closed"]
    assert conn.sql(flat(sync.BOOKING_LINES_SQL)) == []


# ── the feed ─────────────────────────────────────────────────────────────────────────────────────


def event(kind: str, **kw: Any) -> contract.Event:
    return contract.Event(event_id="ev-1", type=kind, known=True, **kw)


def feed_world(rows: list[dict[str, Any]]) -> ScriptedConn:
    return ScriptedConn().on(flat(sync.EVENT_LINES_SQL), rows)


ROWS = [
    {"booking_line_id": LINE, "trip_id": TRIP, "status": "confirmed", "aa_sub_status": None, "bk_hold_id": "h-1"},
    {"booking_line_id": SIBLING, "trip_id": TRIP, "status": "confirmed", "aa_sub_status": None, "bk_hold_id": None},
]


@pytest.mark.asyncio
async def test_booking_cancelled_closes_its_lines_and_finishes_a_pending_cancel(seams: dict[str, list[Any]]) -> None:
    conn = feed_world(ROWS)
    ctx = system_ctx(conn)
    assert await apply_event(ctx, event("booking.cancelled", booking_id="b-1")) == ("applied", "booking_cancelled")
    assert conn.sql(flat(sync.EVENT_LINES_SQL)) == [(None, None, "b-1")]
    assert conn.sql(flat(sync.CLOSE_LINE_SQL)) == [(LINE, None, None, "booking_cancelled"),
                                                   (SIBLING, None, None, "booking_cancelled")]  # fmt: skip
    assert conn.sql(flat(sync.COMMIT_SQL)) == [(TRIP, "cancelled", "h-1")]
    assert seams["finish"] == [TRIP]  # once per trip, not per line
    assert seams["waiting"] == [TRIP] and seams["nudge"] == [(TRIP, "booking_cancelled")]
    assert [e.reason for e in ctx.audit_events] == ["booking_cancelled", "booking_cancelled"]


@pytest.mark.asyncio
async def test_the_other_line_events_never_finish_a_cancel(seams: dict[str, list[Any]]) -> None:
    line_ref = str(LINE)
    conn = feed_world(ROWS[:1])
    assert await apply_event(system_ctx(conn), event("line.declined", line_ref=line_ref, decline_code="full")) == (
        "applied", "line_declined",
    )  # fmt: skip
    assert conn.sql(flat(sync.EVENT_LINES_SQL)) == [(LINE, None, None)]
    assert conn.sql(flat(sync.CLOSE_LINE_SQL)) == [(LINE, "declined", "full", "declined")]
    assert conn.sql(flat(sync.TASK_SQL)) == [(TRIP, sync.DECLINE_TASK)]
    conn = feed_world(ROWS[:1])
    await apply_event(system_ctx(conn), event("line.confirmed", line_ref=line_ref))
    assert conn.sql(flat(sync.CONFIRM_LINE_SQL)) == [(LINE,)]
    assert conn.sql(flat(sync.COMMIT_SQL)) == [(TRIP, "confirmed", "h-1")]
    conn = feed_world(ROWS[:1])
    await apply_event(system_ctx(conn), event("hold.extended", hold_id="h-1"))
    assert conn.sql(flat(sync.REFRESH_SQL)) == [(TRIP, LINE, "refresh")]
    assert seams["finish"] == []
    assert [n[1] for n in seams["nudge"]] == ["line_declined", "line_confirmed"]  # hold.extended: no ops release


@pytest.mark.asyncio
async def test_hold_expired_and_a_late_event_of_an_old_hold(seams: dict[str, list[Any]]) -> None:
    held = {**ROWS[0], "status": "planning", "aa_sub_status": "held"}
    conn = feed_world([held])
    await apply_event(system_ctx(conn), event("hold.expired", hold_id="h-1"))
    assert conn.sql(flat(sync.SUB_SQL)) == [(LINE, "expired", "hold_expired")]
    assert conn.sql(flat(sync.COMMIT_SQL)) == [(TRIP, "expired", "h-1")]
    conn = feed_world([{**held, "bk_hold_id": "h-2"}])  # the line moved to a new hold (a late deposit re-held it)
    await apply_event(system_ctx(conn), event("hold.expired", hold_id="h-1"))
    assert conn.sql(flat(sync.SUB_SQL)) == []
    assert conn.sql(flat(sync.COMMIT_SQL)) == [(TRIP, "expired", "h-1")]


@pytest.mark.asyncio
async def test_events_the_feed_cannot_place_are_ignored(seams: dict[str, list[Any]]) -> None:
    ctx = system_ctx(feed_world([]))
    assert await apply_event(ctx, contract.Event(event_id="e", type="x.new", known=False)) == (
        "ignored", "unknown_type",
    )  # fmt: skip
    assert await apply_event(ctx, event("departure.guaranteed")) == ("ignored", "informational")
    assert await apply_event(ctx, event("booking.cancelled", line_ref="not-a-uuid")) == ("ignored", "unknown_line")
    assert ctx.conn.sql(flat(sync.EVENT_LINES_SQL)) == [(None, None, None)]  # a bad line_ref reads as none
    assert seams["finish"] == []


@pytest.mark.asyncio
async def test_a_departure_incident_opens_one_flag_and_a_task_per_live_trip(seams: dict[str, list[Any]]) -> None:
    at = dt.datetime(2026, 11, 1, 3, tzinfo=dt.UTC)
    conn = ScriptedConn()
    conn.on(flat(sync.INCIDENT_OPEN_SQL), uuid.UUID(int=31))
    conn.on(flat(sync.INCIDENT_TRIPS_SQL), [{"trip_id": TRIP}, {"trip_id": TRIP2}])
    ctx = system_ctx(conn)
    ev = event("departure.incident", departure_id="dep-1", incident_code="weather", occurred_at=at)
    assert await apply_event(ctx, ev) == ("applied", "departure_incident")
    assert conn.sql(flat(sync.INCIDENT_OPEN_SQL)) == [("dep-1", "weather", at, "ev-1")]
    assert conn.sql(flat(sync.TASK_SQL)) == [(TRIP, sync.INCIDENT_TASK), (TRIP2, sync.INCIDENT_TASK)]
    assert ctx.audit_events[0].object_ids == (str(TRIP), str(TRIP2))
    # A second incident while one is open keeps the first.
    conn = ScriptedConn()
    assert await apply_event(system_ctx(conn), ev) == ("ignored", "incident_already_open")
    assert conn.sql(flat(sync.TASK_SQL)) == []
    # Incomplete: no departure, or no code on an incident.
    assert await apply_event(system_ctx(conn), event("departure.incident", departure_id="dep-1")) == (
        "ignored", "incident_incomplete",
    )  # fmt: skip
    assert await apply_event(system_ctx(conn), event("departure.incident_cleared")) == (
        "ignored", "incident_incomplete",
    )  # fmt: skip


@pytest.mark.asyncio
async def test_clearing_an_incident_needs_an_open_flag(seams: dict[str, list[Any]]) -> None:
    ev = event("departure.incident_cleared", departure_id="dep-1")
    conn = ScriptedConn().on(flat(sync.INCIDENT_CLEAR_SQL), uuid.UUID(int=31))
    ctx = system_ctx(conn)
    assert await apply_event(ctx, ev) == ("applied", "departure_incident_cleared")
    assert conn.sql(flat(sync.INCIDENT_CLEAR_SQL)) == [("dep-1", None, "ev-1")]
    assert ctx.audit_events[0].object_id == str(uuid.UUID(int=31))
    assert await apply_event(system_ctx(ScriptedConn()), ev) == ("ignored", "incident_not_open")


# ── the other answers of a Booking hold set (AA-952), next to the cancel ─────────────────────────


def set_world(**cur: Any) -> ScriptedConn:
    conn = world(open_lines=[]).on(flat(sync.LINE_SQL), {
        "booking_line_id": LINE, "trip_id": TRIP, "status": "planning", "aa_sub_status": "held", "bk_hold_id": "h-1",
        "bk_booking_id": None, "trip_stage": "booked", "cancel_reason": None, "trip_deleted_at": None, **cur,
    })  # fmt: skip
    conn.on(flat(sync.SET_LINES_SQL), [{"booking_line_id": SIBLING}])
    return conn


@pytest.mark.asyncio
async def test_a_release_lets_the_whole_set_go(seams: dict[str, list[Any]]) -> None:
    conn = set_world()
    assert await apply(system_ctx(conn), work("release"), Outcome(released=True, code="released")) == "done"
    assert conn.sql(flat(sync.SET_LINES_SQL)) == [(TRIP, "h-1", LINE)]
    assert conn.sql(flat(sync.CLOSE_LINE_SQL)) == [(LINE, "released", None, None), (SIBLING, "released", None, None)]
    assert conn.sql(flat(sync.CANCEL_OTHERS_SQL)) == [(LINE, REQUEST), (SIBLING, REQUEST)]
    assert conn.sql(flat(sync.COMMIT_SQL)) == [(TRIP, "released", "h-1")]
    assert conn.sql(flat(sync.FINISH_SQL)) == [(REQUEST, "done", "released")]
    assert seams["nudge"] == [(TRIP, "aa_release")] and seams["finish"] == []


@pytest.mark.asyncio
async def test_a_refused_confirm_marks_every_line_of_the_set_and_opens_a_task(seams: dict[str, list[Any]]) -> None:
    conn = set_world()
    assert await apply(system_ctx(conn), work("confirm"), Outcome(code="sold_out", failed=True)) == "failed"
    assert conn.sql(flat(sync.SUB_SQL)) == [(LINE, "sold_out", "sold_out"), (SIBLING, "sold_out", "sold_out")]
    assert conn.sql(flat(sync.TASK_SQL)) == [(TRIP, "aa_confirm_failed")]
    assert conn.sql(flat(sync.FINISH_SQL)) == [(REQUEST, "failed", "sold_out")]
    assert seams["nudge"] == []  # nothing confirmed: no ops release


@pytest.mark.asyncio
async def test_a_confirm_books_the_set_as_booking_answered(seams: dict[str, list[Any]]) -> None:
    conn = set_world()
    out = Outcome(booking=contract.Booking(booking_id="b-1", hold_id="h-1", trip_case_code="TC-9",
                                           line_status="confirmed"),
                  booking_lines={str(LINE): "confirmed", str(SIBLING): "on_request"}, code="confirmed")  # fmt: skip
    assert await apply(system_ctx(conn), work("confirm"), out) == "done"
    assert conn.sql(flat(sync.BOOKED_SQL)) == [
        (LINE, "b-1", "TC-9", "confirmed", None, "confirmed"),
        (SIBLING, "b-1", "TC-9", "planning", "on_request", "aa_on_request"),
    ]
    assert conn.sql(flat(sync.COMMIT_SQL)) == [(TRIP, "confirmed", "h-1")]
    assert seams["nudge"] == [(TRIP, "aa_confirm")]


@pytest.mark.asyncio
async def test_a_hold_landing_on_a_trip_being_cancelled_is_given_back(seams: dict[str, list[Any]]) -> None:
    conn = set_world(aa_sub_status="pending", bk_hold_id=None, cancel_reason="client_request")
    expires = dt.datetime(2026, 11, 2, tzinfo=dt.UTC)
    out = Outcome(hold=contract.Hold(hold_id="h-9", status="active", expires_at=expires, line_status=None),
                  code="held")  # fmt: skip
    assert await apply(system_ctx(conn), work("hold"), out) == "done"
    assert conn.sql(flat(sync.HELD_SQL)) == [(LINE, "h-9", expires)]
    assert conn.sql(flat(sync.COMMIT_SQL)) == [(TRIP, "held", "h-9")]
    assert conn.sql(flat(sync.REFRESH_SQL)) == [(TRIP, LINE, "release")]  # the seats go back at once
