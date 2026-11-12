"""AA-1070 item crm-13 without a database: Adventure Asia cancels a departure. The feed opens a case once per trip
(a replay opens nothing, no second task or workflow event), the flag, the guided card, the re-quote (a new version
before booking, a new trip after acceptance, idempotent, every refusal), the forced waiver of the cancel and the case
closing with it; the pipeline filter; the PDFs' brand. The SQL runs on PostgreSQL in
``tests/integration/test_aa1070_aa_cancel_db.py``. Invented names and places only."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest

from app import pipeline
from app.cancel_policy import rules
from app.cancel_policy import service as policy
from app.documents import pdf_job
from app.errors import Conflict, Invalid, NotFound
from app.proposals import builder
from app.travel_ops import aa_cancel, departures
from app.workflows import triggers
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault, system_ctx, tenant_ctx

TRIP = uuid.UUID("00000000-0000-4000-8000-00000000c013")
OTHER = uuid.UUID("00000000-0000-4000-8000-00000000c014")
CASE = uuid.UUID("00000000-0000-4000-8000-00000000c015")
PROP = uuid.UUID("00000000-0000-4000-8000-00000000c016")
NEW_PROP = uuid.UUID("00000000-0000-4000-8000-00000000c017")
DEP = "dep-2031-lotus-a"
START = dt.date(2031, 5, 4)
WHEN = dt.datetime(2031, 4, 1, 3, 0, tzinfo=dt.UTC)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def fired(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, str, uuid.UUID]]:
    out: list[tuple[uuid.UUID, str, uuid.UUID]] = []

    async def on_trip_event(_ctx: Any, trip_id: uuid.UUID, name: str, event_id: uuid.UUID) -> int:
        out.append((trip_id, name, event_id))
        return 1

    monkeypatch.setattr(triggers, "on_trip_event", on_trip_event)
    return out


# ── the feed ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_feed_opens_one_case_per_trip_and_a_replay_opens_nothing(fired: list) -> None:
    opened = {TRIP}

    def insert(trip_id: uuid.UUID, *_: Any) -> Any:
        if trip_id in opened:
            opened.discard(trip_id)
            return CASE
        return None  # the unique key: a case exists already

    conn = ScriptedConn().on("WHERE t.deleted_at IS NULL AND t.stage <> ALL", [{"trip_id": TRIP}, {"trip_id": OTHER}])
    conn.on("INSERT INTO crm.aa_cancel_case", insert)
    ctx = system_ctx(conn)
    assert run(aa_cancel.open_cases(ctx, DEP, "ev-1", WHEN)) == [TRIP]
    assert len(conn.sql("INSERT INTO crm.task")) == 1 and conn.sql("INSERT INTO crm.task")[0] == (TRIP, aa_cancel.TASK)
    assert fired == [(TRIP, "departure.aa_cancelled", aa_cancel.event_id(TRIP, DEP))]
    (event,) = ctx.audit_events
    assert event.object_ids == (str(TRIP),) and event.reason == "departure_cancelled_cases"
    # The same departure again: no case, no task, no event, no audit line.
    again = system_ctx(conn)
    assert run(aa_cancel.open_cases(again, DEP, "ev-2", WHEN)) == []
    assert len(conn.sql("INSERT INTO crm.task")) == 1 and len(fired) == 1 and again.audit_events == []
    # The stage list excludes the closed trips; the event id is stable per trip and departure.
    assert conn.sql("t.stage <> ALL")[0][1] == list(aa_cancel.CLOSED_STAGES)
    assert aa_cancel.event_id(TRIP, DEP) == aa_cancel.event_id(TRIP, DEP) != aa_cancel.event_id(OTHER, DEP)


def test_the_feed_names_a_dotted_departure_only_through_its_trips(fired: list) -> None:
    # The Booking contract allows a dot in a departure id; the audit object id does not (a raise there would roll the
    # feed page back and stall the agency's feed): the event keeps the trips, without the departure id.
    dotted = "dep.2031.lotus-a"
    conn = ScriptedConn().on("t.stage <> ALL", [{"trip_id": TRIP}]).on("INSERT INTO crm.aa_cancel_case", CASE)
    ctx = system_ctx(conn)
    assert run(aa_cancel.open_cases(ctx, dotted, "ev-1", WHEN)) == [TRIP]
    (event,) = ctx.audit_events
    assert event.object_id is None and event.object_ids == (str(TRIP),)
    assert fired == [(TRIP, "departure.aa_cancelled", aa_cancel.event_id(TRIP, dotted))]
    # An id without a dot stays the object id.
    plain = system_ctx(
        ScriptedConn().on("t.stage <> ALL", [{"trip_id": TRIP}]).on("INSERT INTO crm.aa_cancel_case", CASE)
    )
    run(aa_cancel.open_cases(plain, DEP, "ev-1", WHEN))
    assert plain.audit_events[0].object_id == DEP


def test_the_feed_caps_the_trips_of_one_departure(fired: list, caplog: pytest.LogCaptureFixture) -> None:
    rows = [{"trip_id": uuid.uuid4()} for _ in range(3)]
    conn = ScriptedConn().on("t.stage <> ALL", rows)
    old = aa_cancel.MAX_TRIPS
    aa_cancel.MAX_TRIPS = 3
    try:
        assert run(aa_cancel.open_cases(system_ctx(conn), DEP, "ev-1", WHEN)) == []
    finally:
        aa_cancel.MAX_TRIPS = old
    assert "aa_cancel_trips_capped" in caplog.text


def test_a_cancelled_departure_opens_the_cases_from_the_roster_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def open_cases(_ctx: Any, departure_id: str, *_: Any) -> list[uuid.UUID]:
        seen.append(departure_id)
        return []

    monkeypatch.setattr(aa_cancel, "open_cases", open_cases)
    conn = ScriptedConn().on("FROM crm.booking_feed_state", 1)
    conn.on("SELECT departure_status, last_event_at", {"departure_status": "cancelled", "last_event_at": WHEN})
    ctx = system_ctx(conn)
    assert run(departures.apply_event(ctx, "departure.cancelled", DEP, "ev-1", WHEN)) == (
        "applied",
        "departure_cancelled",
    )
    assert seen == [DEP]
    # A newer guaranteed state stays (an older cancel): no case.
    conn.on("SELECT departure_status, last_event_at", {"departure_status": "guaranteed", "last_event_at": WHEN})
    run(departures.apply_event(ctx, "departure.cancelled", DEP, "ev-0", WHEN))
    run(departures.apply_event(ctx, "departure.guaranteed", DEP, "ev-3", WHEN))
    assert seen == [DEP]


# ── reads ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_flag_and_modes() -> None:
    assert run(aa_cancel.flagged(tenant_ctx(), [])) == set()
    conn = ScriptedConn().on("SELECT DISTINCT c.trip_id", [{"trip_id": TRIP}])
    assert run(aa_cancel.flagged(tenant_ctx(conn), [TRIP, OTHER])) == {TRIP}
    assert aa_cancel.requote_mode("proposal", False) == "version"
    assert aa_cancel.requote_mode("proposal", True) == "new_trip"
    assert aa_cancel.requote_mode("booked", True) == "new_trip"
    assert aa_cancel.requote_mode("cancelled", True) is None
    assert aa_cancel.requote_mode("lost", False) is None


def _case_row(**over: Any) -> dict[str, Any]:
    row = {"aa_cancel_case_id": CASE, "departure_id": DEP, "resolution": None, "opened_at": WHEN,
           "resolved_at": None, "proposal_id": None, "rebook_trip_id": None, "proposal_version": None}  # fmt: skip
    return {**row, **over}


TOUR = {"tour_source_id": "T-31", "component_id": "tp:1", "component_kind": "trip_plan", "title": "Lotus bay 3 days",
        "service_from": START}  # fmt: skip


def test_the_trip_card_says_what_the_agent_may_do() -> None:
    conn = ScriptedConn().on("SELECT EXISTS (SELECT 1 FROM crm.proposal", True)
    conn.on("FROM crm.aa_cancel_case c WHERE c.trip_id = $1", [_case_row()]).on("ORDER BY x.rank", TOUR)
    view = run(aa_cancel.trip_view(tenant_ctx(conn), TRIP, "booked"))
    (card,) = view["cases"]
    assert card["active"] and card["can_requote"] and card["can_cancel"] and card["requote_mode"] == "new_trip"
    assert card["tour"]["service_from"] == "2031-05-04" and view["rebooked_from"] is None
    done = ScriptedConn().on("SELECT EXISTS (SELECT 1 FROM crm.proposal", False).on("c.rebook_trip_id = $1", OTHER)
    done.on("FROM crm.aa_cancel_case c WHERE c.trip_id = $1", [_case_row(resolution="requoted", proposal_id=PROP,
                                                                         proposal_version=2)])  # fmt: skip
    view = run(aa_cancel.trip_view(tenant_ctx(done), TRIP, "proposal"))
    (card,) = view["cases"]
    assert not card["active"] and not card["can_requote"] and not card["can_cancel"] and card["tour"] is None
    assert card["proposal_version"] == 2 and view["rebooked_from"] == str(OTHER)


# ── (a) re-quote ─────────────────────────────────────────────────────────────────────────────────────────────────
def _requote_conn(*, resolution: str | None = None, stage: str = "proposal", accepted: bool = False,
                  draft: uuid.UUID | None = None, moved: bool = True) -> ScriptedConn:  # fmt: skip
    conn = ScriptedConn()
    conn.on("FROM crm.aa_cancel_case c WHERE c.aa_cancel_case_id = $1",
            {"aa_cancel_case_id": CASE, "trip_id": TRIP, "departure_id": DEP, "resolution": resolution,
             "proposal_id": PROP if resolution == "requoted" else None, "rebook_trip_id": None})  # fmt: skip
    conn.on(
        "SELECT trip_id, stage, start_date, end_date FROM crm.trip",
        {"trip_id": TRIP, "stage": stage, "start_date": START, "end_date": START + dt.timedelta(days=3)},
    )
    conn.on("SELECT EXISTS (SELECT 1 FROM crm.proposal", accepted)
    conn.on("ORDER BY p.status = 'accepted' DESC", {"proposal_id": PROP, "status": "sent", "version": 1})
    conn.on("AND status = 'draft' AND deleted_at IS NULL", draft)
    conn.on("SELECT min(d.day_no)", 2)
    conn.on("UPDATE crm.option SET aa_departure_id", [{"option_id": uuid.uuid4()}] if moved else [])
    return conn


@pytest.fixture
def made(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    out: list[str] = []

    async def create(*_: Any, **kw: Any) -> dict[str, Any]:
        out.append(f"create:{kw['from_proposal_id']}")
        return {"proposal_id": str(NEW_PROP)}

    async def rebook_copy(_ctx: Any, _vault: Any, trip_id: uuid.UUID, source: uuid.UUID, old: uuid.UUID) -> uuid.UUID:
        out.append(f"rebook:{source}:{old}")
        return NEW_PROP

    async def touch_draft(_ctx: Any, proposal_id: uuid.UUID) -> int:
        out.append(f"touch:{proposal_id}")
        return 3

    monkeypatch.setattr(builder, "create", create)
    monkeypatch.setattr(builder, "rebook_copy", rebook_copy)
    monkeypatch.setattr(builder, "touch_draft", touch_draft)
    return out


def _requote(conn: ScriptedConn, dep: Any = "dep-2031-lotus-b", start: Any = START) -> Any:
    ctx = tenant_ctx(conn, role="agent")
    return run(aa_cancel.requote(ctx, FakeVault(), TRIP, CASE, departure_id=dep, start_date=start)), ctx


def test_requote_before_booking_makes_a_new_version(made: list) -> None:
    conn = _requote_conn()
    out, ctx = _requote(conn, start="2031-06-10")
    assert out == {"proposal_id": str(NEW_PROP), "trip_id": str(TRIP), "mode": "version"}
    assert made == [f"create:{PROP}", f"touch:{NEW_PROP}"]
    assert conn.sql("UPDATE crm.option SET aa_departure_id")[0] == (NEW_PROP, DEP, "dep-2031-lotus-b")
    # The tour is on day 2: day 1 is the day before the picked start.
    assert conn.sql("UPDATE crm.proposal SET starts_on")[0] == (NEW_PROP, dt.date(2031, 6, 9))
    (_case, actor, proposal, rebook) = conn.sql("SET resolution = 'requoted'")[0]
    assert (actor, proposal, rebook) == (f"member:{ctx.member_id}", NEW_PROP, None)
    assert ctx.audit_events[-1].reason == "requoted_version"


def test_requote_uses_the_working_copy_and_never_overwrites_one_without_the_tour(made: list) -> None:
    out, _ = _requote(_requote_conn(draft=PROP))
    assert out["proposal_id"] == str(PROP) and made == [f"touch:{PROP}"]
    with pytest.raises(Conflict) as err:
        _requote(_requote_conn(draft=PROP, moved=False))
    assert err.value.code == "builder_draft_exists"


def test_requote_after_acceptance_opens_a_new_trip(made: list) -> None:
    conn = _requote_conn(stage="booked", accepted=True)
    out, ctx = _requote(conn)
    assert out["mode"] == "new_trip" and out["proposal_id"] == str(NEW_PROP) and out["trip_id"] != str(TRIP)
    new_trip = uuid.UUID(out["trip_id"])
    # The tour is on day 2 of the copied version: the new trip starts on its day 1 (the day before the picked
    # departure), as long as the old trip, and the version's starts_on says the same day.
    day1 = START - dt.timedelta(days=1)
    assert conn.sql("INSERT INTO crm.trip (")[0] == (TRIP, new_trip, day1, day1 + dt.timedelta(days=3))
    assert conn.sql("UPDATE crm.proposal SET starts_on")[0] == (NEW_PROP, day1)
    assert conn.sql("SELECT min(d.day_no)")[0] == (PROP, DEP)  # read on the source before the trip is made
    assert conn.sql("INSERT INTO crm.trip_stage_history")[0] == (new_trip,)
    assert conn.sql("INSERT INTO crm.trip_traveler")[0] == (TRIP, new_trip)
    assert conn.sql("INSERT INTO crm.trip_party")[0] == (TRIP, new_trip)
    assert made[0] == f"rebook:{PROP}:{TRIP}"
    assert conn.sql("SET resolution = 'requoted'")[0][3] == new_trip
    assert [e.reason for e in ctx.audit_events] == ["aa_rebook", "requoted_new_trip"]


def test_requote_is_idempotent_and_refuses_what_it_must(made: list) -> None:
    out, _ = _requote(_requote_conn(resolution="requoted"))
    assert out == {"proposal_id": str(PROP), "trip_id": str(TRIP), "mode": "version"} and made == []
    cases: list[tuple[dict[str, Any], dict[str, Any], type, str]] = [
        ({"resolution": "cancelled"}, {}, Conflict, "aa_cancel_case_closed"),
        ({}, {"dep": DEP}, Invalid, "same_departure"),
        ({}, {"dep": "bad id"}, Invalid, "invalid_departure_id"),
        ({}, {"start": "next week"}, Invalid, "invalid_start_date"),
        ({"stage": "lost"}, {}, Conflict, "trip_not_open_for_proposal"),
    ]
    for conn_kw, call_kw, error, code in cases:
        with pytest.raises(error) as err:
            _requote(_requote_conn(**conn_kw), **call_kw)
        assert err.value.code == code, code
    gone = _requote_conn().on("FROM crm.aa_cancel_case c WHERE c.aa_cancel_case_id = $1", None)
    with pytest.raises(NotFound):
        _requote(gone)
    no_trip = _requote_conn().on("SELECT trip_id, stage, start_date, end_date FROM crm.trip", None)
    with pytest.raises(NotFound):
        _requote(no_trip)
    also_cancelled = _requote_conn().on("SELECT departure_status FROM crm.aa_departure_state", "cancelled")
    with pytest.raises(Conflict) as err:
        _requote(also_cancelled)
    assert err.value.code == "departure_cancelled"
    no_source = _requote_conn().on("ORDER BY p.status = 'accepted' DESC", None)
    with pytest.raises(Conflict) as err:
        _requote(no_source)
    assert err.value.code == "no_builder_version"


# ── (b) cancel ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_cancel_forces_the_waiver_only_for_a_flagged_trip_and_the_aa_reason() -> None:
    flagged = ScriptedConn().on("ORDER BY c.opened_at LIMIT 1", CASE)
    forced = run(aa_cancel.forced_decision(tenant_ctx(flagged), TRIP, "aa_cancelled"))
    assert (forced.mode, forced.reason, forced.forced) == ("waive", "aa_departure_cancelled", True)
    assert run(aa_cancel.forced_decision(tenant_ctx(flagged), TRIP, "client_request")) is None
    assert run(aa_cancel.forced_decision(tenant_ctx(ScriptedConn()), TRIP, "aa_cancelled")) is None


def test_the_cancel_closes_the_case_and_its_task() -> None:
    conn = ScriptedConn().on("SET resolution = 'cancelled'", [{"aa_cancel_case_id": CASE}])
    ctx = tenant_ctx(conn)
    assert run(aa_cancel.close_on_cancel(ctx, TRIP, "member:x")) == 1
    assert conn.sql("UPDATE crm.task SET status = 'done'")[0] == (TRIP, aa_cancel.TASK, "member:x")
    assert ctx.audit_events[0].object_ids == (str(CASE),)
    quiet = ScriptedConn()
    assert run(aa_cancel.close_on_cancel(tenant_ctx(quiet), TRIP, "member:x")) == 0
    assert quiet.sql("UPDATE crm.task") == []


@pytest.mark.asyncio
async def test_a_forced_waiver_needs_no_owner() -> None:
    from tests.unit.test_aa1067_payments_cancellation import PAID, _world

    a = await policy.assess(tenant_ctx(_world(), role="agent"), uuid.uuid4(), PAID,
                            rules.Decision("waive", None, aa_cancel.WAIVER_REASON, forced=True))  # fmt: skip
    assert (a.decision, a.charge, a.needs_owner, a.forced) == ("waived", Decimal("0.00"), False, True)
    assert a.refunds == {"p1": Decimal("300.00"), "p2": Decimal("100.00")}  # full refunds
    assert a.view()["forced"] is True


# ── pipeline and PDFs ────────────────────────────────────────────────────────────────────────────────────────────
def test_the_pipeline_filter_keeps_the_aa_cancelled_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    async def flagged(_ctx: Any, trip_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        return {TRIP} & set(trip_ids)

    async def no_issues(*_: Any) -> dict[uuid.UUID, int]:
        return {}

    monkeypatch.setattr(aa_cancel, "flagged", flagged)
    monkeypatch.setattr(pipeline.passport, "issue_counts", no_issues)

    def card(trip_id: uuid.UUID, stage: str) -> dict[str, Any]:
        return {"trip_id": trip_id, "title": "Lotus", "stage": stage, "waiting_on": "agency", "owner_member_id": None,
                "lead_name_enc": None, "lead_traveler_id": None, "companions": 0, "party_adults": 2, "children": 0,
                "month": None, "start_date": None, "nights": None, "budget_band": None, "lead_source": None,
                "next_due_at": None, "submissions": 0, "last_inquiry_at": None, "stage_entered_at": None,
                "last_activity_at": None, "quiet_days": 0, "s2": 0}  # fmt: skip

    conn = ScriptedConn().on("ORDER BY", [card(TRIP, "booked"), card(OTHER, "proposal")])
    out = run(pipeline.board(tenant_ctx(conn), FakeVault(), pipeline.BoardFilter(issue="aa_cancelled")))
    shown = [(c["trip_id"], c["aa_cancelled"]) for s in out["stages"] for c in s["cards"]]
    assert shown == [(str(TRIP), True)] and out["filter"]["issue"] == "aa_cancelled"


def test_every_pdf_carries_the_agency_brand() -> None:
    conn = ScriptedConn().on("SELECT display_name FROM crm.tenant", "Lotus Trails")
    assert run(pdf_job.agency_brand(tenant_ctx(conn))) == "Lotus Trails"
