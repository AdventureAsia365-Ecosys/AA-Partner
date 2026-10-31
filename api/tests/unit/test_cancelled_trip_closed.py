"""AA-957 S262 round 3 (design v3 C12 §2.7): a cancelled trip, or one whose cancellation waits for AA, is closed for
planning as well as billing. Every write that would add to it answers 409 ``trip_cancelled`` before anything is
written: a new AA quote, a hold / confirm / re-quote (a release still goes: it frees AA's places), a new or confirmed
agency line, new trip dates, a document shown to the traveller, a builder edit, a builder send and a proposal text.
A trip whose cancellation is pending cannot be deleted (409 ``cancellation_pending``). Undo claim forgets the claim.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from app import cancellation, pipeline
from app.booking import aa_lines
from app.commissions import service as commissions
from app.documents import service as documents
from app.documents.store import MemoryDocumentStore
from app.errors import Conflict
from app.ledger import lines
from app.lifecycle import trash
from app.proposals import builder, text_proposal
from app.proposals import service as proposals
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault, tenant_ctx

TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000e1")
LINE = uuid.UUID("00000000-0000-4000-8000-0000000000e2")
DOC = uuid.UUID("00000000-0000-4000-8000-0000000000e3")
PROPOSAL = uuid.UUID("00000000-0000-4000-8000-0000000000e4")
PDF = b"%PDF-1.7\nsynthetic\n%%EOF"
CANCELLED = {"stage": "cancelled", "cancelling": True}
PENDING = {"stage": "booked", "cancelling": True}
OPEN = {"stage": "booked", "cancelling": False}
AA_BODY = {
    "tour_source_id": "T-100",
    "component_id": "tp:1",
    "component_kind": "trip_plan",
    "service_date": "2026-12-01",
    "pax": {"adult": 2, "child": 0, "infant": 0},
    "currency": "USD",
}


def flat(sql: str) -> str:
    return " ".join(sql.split())


def world(closed: dict[str, Any], *rules: tuple[str, Any]) -> ScriptedConn:
    conn = ScriptedConn()
    for fragment, answer in rules:
        conn.on(flat(fragment), answer)
    return conn.on(flat(cancellation.CLOSED_SQL), closed)


def writes(conn: ScriptedConn) -> list[str]:
    return [sql for sql, _ in conn.calls if sql.startswith(("INSERT", "UPDATE", "DELETE", "SELECT crm.aa_line"))]


async def refused(conn: ScriptedConn, call: Any) -> str:
    with pytest.raises(Conflict) as err:
        await call
    assert err.value.status_code == 409 and writes(conn) == []
    return err.value.code


AA_LINE = {"trip_id": TRIP, "supplier_kind": "aa", "aa_pending": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [CANCELLED, PENDING])
async def test_a_closed_trip_asks_aa_for_nothing_new(closed: dict[str, Any]) -> None:
    conn = world(closed)
    assert await refused(conn, aa_lines.request_line(tenant_ctx(conn), TRIP, AA_BODY)) == "trip_cancelled"
    for action in ("hold", "confirm", "requote"):
        conn = world(closed, (aa_lines.LINE_SQL, AA_LINE))
        assert await refused(conn, aa_lines.ask(tenant_ctx(conn), LINE, action)) == "trip_cancelled"


@pytest.mark.asyncio
async def test_a_release_is_not_refused_by_the_closed_trip() -> None:
    """A release frees AA's places: the closed check is skipped (the line's own rules answer next)."""
    conn = world(CANCELLED, (aa_lines.LINE_SQL, AA_LINE))
    with pytest.raises(Exception):  # noqa: B017, PT011 - the stub row is too thin for the actions check
        await aa_lines.ask(tenant_ctx(conn), LINE, "release")
    assert conn.sql(flat(cancellation.CLOSED_SQL)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [CANCELLED, PENDING])
async def test_a_closed_trip_takes_no_new_or_confirmed_agency_line(closed: dict[str, Any]) -> None:
    conn = world(closed, (lines.TRIP_SQL, "booked"))
    body = {"agency_supplier_id": str(uuid.uuid4()), "title": "Lodge"}
    assert await refused(conn, lines.create_line(tenant_ctx(conn), TRIP, body)) == "trip_cancelled"
    row = {"trip_id": TRIP, "status": "planning", "supplier_kind": "own"}
    conn = world(closed, (lines.TRIP_OF_LINE_SQL, row))
    assert await refused(conn, lines.confirm(tenant_ctx(conn), LINE)) == "trip_cancelled"


@pytest.mark.asyncio
async def test_a_closed_trip_keeps_its_dates_but_its_waiting_on_still_changes() -> None:
    conn = world(CANCELLED, (pipeline.TRIP_DATES_SQL, {"start_date": None, "end_date": None}))
    change = {"start_date": dt.date(2026, 12, 1)}
    assert await refused(conn, pipeline.update_trip(tenant_ctx(conn), TRIP, change)) == "trip_cancelled"
    # Waiting on is no date: the closed check is not asked (the UPDATE answers; None = the trip is gone).
    conn = world(CANCELLED)
    with pytest.raises(Exception):  # noqa: B017, PT011 - NotFound from the stub's empty UPDATE
        await pipeline.update_trip(tenant_ctx(conn), TRIP, {"waiting_on": "client"})
    assert conn.sql(flat(cancellation.CLOSED_SQL)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [CANCELLED, PENDING])
async def test_a_closed_trip_shows_no_new_document_to_its_travellers(closed: dict[str, Any]) -> None:
    conn = world(closed, ("SELECT 1 FROM crm.trip WHERE trip_id", 1))
    upload = documents.upload(
        tenant_ctx(conn),
        FakeVault(),
        MemoryDocumentStore(),
        traveler_id=None,
        trip_id=TRIP,
        kind="other",
        filename="Voucher.pdf",
        content_type="application/pdf",
        content=PDF,
        visible_to_travelers=True,
    )
    assert await refused(conn, upload) == "trip_cancelled"
    row = {"document_id": DOC, "trip_id": TRIP, "write_only": False}
    conn = world(closed, (documents.ROW_SQL, row))
    assert await refused(conn, documents.set_visibility(tenant_ctx(conn), DOC, True)) == "trip_cancelled"
    # Hiding a document (and an internal upload) stays possible.
    conn = world(closed, (documents.ROW_SQL, row))
    assert (await documents.set_visibility(tenant_ctx(conn), DOC, False))["visible_to_travelers"] is False
    assert conn.sql(flat(cancellation.CLOSED_SQL)) == []


@pytest.mark.asyncio
async def test_an_internal_document_still_goes_on_a_cancelled_trip() -> None:
    conn = world(
        CANCELLED,
        ("SELECT 1 FROM crm.trip WHERE trip_id", 1),
        ("RETURNING created_at", {"created_at": dt.datetime.now(dt.UTC)}),
    )
    got = await documents.upload(
        tenant_ctx(conn),
        FakeVault(),
        MemoryDocumentStore(),
        traveler_id=None,
        trip_id=TRIP,
        kind="other",
        filename="Supplier cancellation.pdf",
        content_type="application/pdf",
        content=PDF,
    )
    assert got["visible_to_travelers"] is False and conn.sql(flat(cancellation.CLOSED_SQL)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [CANCELLED, PENDING])
async def test_a_closed_trip_builder_is_read_only_and_sends_nothing(closed: dict[str, Any]) -> None:
    draft = {"proposal_id": PROPOSAL, "trip_id": TRIP, "kind": builder.KIND, "status": "draft", "edit_seq": 3}
    conn = world(closed, (builder.LOCK_SQL, draft))
    assert await refused(conn, builder._lock(tenant_ctx(conn), PROPOSAL, 3)) == "trip_cancelled"
    published = {**draft, "status": "published"}
    conn = world(closed, (proposals.ONE_SQL, published))
    assert await refused(conn, proposals.send(tenant_ctx(conn), FakeVault(), PROPOSAL)) == "trip_cancelled"
    conn = world(closed, (text_proposal.LOCK_SQL, {**published, "status": "sent"}))
    send = text_proposal.send(tenant_ctx(conn), FakeVault(), PROPOSAL, fmt="whatsapp", locale=None, price_hash="ab")
    assert await refused(conn, send) == "trip_cancelled"


@pytest.mark.asyncio
async def test_an_open_trip_builder_draft_is_still_edited() -> None:
    draft = {"proposal_id": PROPOSAL, "trip_id": TRIP, "kind": builder.KIND, "status": "draft", "edit_seq": 3}
    conn = world(OPEN, (builder.LOCK_SQL, draft))
    assert (await builder._lock(tenant_ctx(conn), PROPOSAL, 3))["edit_seq"] == 3


@pytest.mark.asyncio
async def test_a_trip_whose_cancellation_waits_for_aa_is_not_deleted() -> None:
    row = {"trip_id": TRIP, "owner_member_id": None, "deleted_at": None}
    pending = {"status": "pending", "reason_code": "client_request", "from_stage": "booked", "aa_bookings": 1,
               "created_at": dt.datetime.now(dt.UTC), "finished_at": None}  # fmt: skip
    conn = (
        ScriptedConn()
        .on(flat(trash.ROW_SQL["trip"]), row)
        .on(flat(trash.HOLD_SQL), False)
        .on(flat(cancellation.PENDING_SQL), pending)
    )
    ctx = tenant_ctx(conn)
    assert await refused(conn, trash.move_to_trash(ctx, "trip", TRIP)) == "cancellation_pending"
    assert ctx.audit_events[-1].reason == "cancellation_pending" and conn.sql(flat(trash.BLOCKERS_SQL)) == []
    # Once AA confirmed it (done), the usual delete blockers decide.
    conn = (
        ScriptedConn()
        .on(flat(trash.ROW_SQL["trip"]), row)
        .on(flat(trash.HOLD_SQL), False)
        .on(flat(cancellation.PENDING_SQL), {**pending, "status": "done"})
        .on(flat(trash.BLOCKERS_SQL), ["has_money"])
    )
    assert await refused(conn, trash.move_to_trash(tenant_ctx(conn), "trip", TRIP)) == "has_money"


def test_undo_claim_forgets_the_claim() -> None:
    sql = flat(commissions.UNCLAIM_SQL)
    assert "claimed_at = NULL" in sql and "claim_ref = NULL" in sql and "status = 'expected'" in sql


@pytest.mark.asyncio
async def test_the_support_access_view_names_the_agency_zone() -> None:
    """S262 round 3: the support banner and Settings print the window's end in the agency's zone (it printed UTC)."""
    from app.invoices.service import TIMEZONE_SQL
    from app.support import grants

    conn = ScriptedConn().on(flat(TIMEZONE_SQL), "Asia/Bangkok")
    assert (await grants.support_access(tenant_ctx(conn)))["timezone"] == "Asia/Bangkok"
    assert (await grants.support_access(tenant_ctx(ScriptedConn())))["timezone"] == "Asia/Ho_Chi_Minh"  # unset
