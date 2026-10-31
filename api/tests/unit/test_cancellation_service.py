"""AA-957 (card CRM-27, design v3 C12) without a database: :mod:`app.cancellation` on a scripted connection. The dry
run lists every effect and writes nothing; the run voids the invoices that hold no money, asks back the amounts the
member chose, keeps the commissions the member keeps, opens the supplier / refund-review tasks, asks Booking once per
AA booking, withdraws the ops release, and moves the trip to ``cancelled`` only when no AA booking waits. The SQL
itself runs on PostgreSQL in ``tests/integration/test_trip_cancel_api.py``."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app import cancellation, stages
from app.booking import aa_lines
from app.booking.client import BookingRefusal, BookingUnavailable
from app.commissions import service as commissions
from app.errors import Conflict, Invalid
from app.invoices.service import VOID_BLOCK_SQL
from app.ops_release import service as ops_release
from app.payments import reminders
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, TENANT, system_ctx, tenant_ctx

TRIP = uuid.UUID("00000000-0000-4000-8000-0000000007e1")
LINE_B1 = uuid.UUID("00000000-0000-4000-8000-0000000001b1")
LINE_B2 = uuid.UUID("00000000-0000-4000-8000-0000000001b2")
INV_DRAFT = uuid.UUID("00000000-0000-4000-8000-00000000a001")
INV_OPEN = uuid.UUID("00000000-0000-4000-8000-00000000a002")
INV_CLAIMED = uuid.UUID("00000000-0000-4000-8000-00000000a003")
INV_PAID = uuid.UUID("00000000-0000-4000-8000-00000000a004")
INV_REFUNDING = uuid.UUID("00000000-0000-4000-8000-00000000a005")
PAY_FULL = uuid.UUID("00000000-0000-4000-8000-00000000b001")
PAY_DONE = uuid.UUID("00000000-0000-4000-8000-00000000b002")
PAY_LOOSE = uuid.UUID("00000000-0000-4000-8000-00000000b003")
COM_EXPECTED = uuid.UUID("00000000-0000-4000-8000-00000000c001")
COM_CLAIMED = uuid.UUID("00000000-0000-4000-8000-00000000c002")
REQUEST = uuid.UUID("00000000-0000-4000-8000-00000000d001")
ACTOR = f"member:{MEMBER}"
ISSUED = dt.datetime(2026, 10, 1, 9, tzinfo=dt.UTC)


def flat(sql: str) -> str:
    return " ".join(sql.split())


def _invoice(invoice_id: uuid.UUID, status: str, held: str, *, issued: bool = True, refund_open: bool = False) -> dict:
    return {"invoice_id": invoice_id, "number": f"INV-{str(invoice_id)[-1]}", "kind": "deposit", "status": status,
            "issued_at": ISSUED if issued else None, "held": Decimal(held), "refund_open": refund_open}  # fmt: skip


def _payment(payment_id: uuid.UUID, invoice_id: uuid.UUID | None, amount: str, refundable: str) -> dict:
    return {"payment_id": payment_id, "invoice_id": invoice_id, "amount": Decimal(amount), "currency": "USD",
            "received_on": dt.date(2026, 10, 5), "number": "INV-4" if invoice_id else None,
            "refundable": Decimal(refundable)}  # fmt: skip


INVOICES = [
    _invoice(INV_DRAFT, "draft", "0", issued=False),  # voided, never issued: no commitment, no reminder
    _invoice(INV_OPEN, "open", "0"),  # voided, issued: commitment + reminder closed
    _invoice(INV_CLAIMED, "payment_claimed", "0"),  # the client says they paid: kept
    _invoice(INV_PAID, "paid", "100"),  # holds money: kept, its payment refunded
    _invoice(INV_REFUNDING, "open", "0", refund_open=True),  # a refund still open: neither voided nor held
]
PAYMENTS = [
    _payment(PAY_FULL, INV_PAID, "100", "100"),
    _payment(PAY_DONE, INV_PAID, "40", "-5"),  # over-refunded already: nothing left (never negative)
    _payment(PAY_LOOSE, None, "50", "50"),  # no invoice: the owner reviews it
]
COMMISSIONS = [
    {"commission_id": COM_EXPECTED, "status": "expected", "title": "Lodge", "supplier_name": "Riverside",
     "expected_amount": Decimal("40"), "expected_currency": "USD"},
    {"commission_id": COM_CLAIMED, "status": "claimed", "title": "Boat", "supplier_name": "Bay Cruises",
     "expected_amount": None, "expected_currency": None},
]  # fmt: skip
AA_BOOKINGS = [
    {"booking_id": "b-1", "line_id": LINE_B1, "titles": ["Halong 2D"], "asked": False, "failed_code": None},
    {"booking_id": "b-2", "line_id": LINE_B2, "titles": ["Hue walk"], "asked": True, "failed_code": "forbidden"},
]


def world(*, open_status: Any = '{"status": "done"}', stage: str = "booked") -> ScriptedConn:
    conn = ScriptedConn()
    conn.on(flat(stages.CANCEL_EFFECTS_SQL), {"dispatch": 2, "tasks": 4, "slots": 1, "links": 3})
    conn.on(flat(cancellation.AA_BOOKINGS_SQL), AA_BOOKINGS)
    conn.on(flat(cancellation.SUPPLIER_LINES_SQL), [
        {"booking_line_id": uuid.UUID(int=7), "title": "Lodge", "supplier_name": "Riverside", "confirmation_no": "R-9"}
    ])  # fmt: skip
    conn.on(flat(cancellation.INVOICES_SQL), INVOICES)
    conn.on(flat(cancellation.PAYMENTS_SQL), PAYMENTS)
    conn.on(flat(cancellation.COMMISSIONS_SQL), COMMISSIONS)
    conn.on(flat(cancellation.WORKFLOW_DISPATCH_SQL), 2)
    conn.on(flat(cancellation.OPEN_TASKS_SQL), 3)
    conn.on(flat(cancellation.TERMS_SQL), {"version_label": "v3", "accepted_at": dt.datetime(2026, 9, 2, 10)})
    conn.on(flat(cancellation.ASK_CANCEL_SQL), REQUEST)
    conn.on(flat(cancellation.OPEN_SQL), open_status)
    conn.on(flat(cancellation.PENDING_SQL), {
        "trip_cancellation_id": uuid.uuid4(), "status": "pending", "reason_code": "client_request",
        "from_stage": stage, "aa_bookings": 1, "effects": {}, "created_at": ISSUED, "finished_at": None,
    })  # fmt: skip
    conn.on(flat(commissions.SYNC_SQL), '{"created": 0, "cancelled": 1}')
    return conn


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """The collaborators of other modules (they have their own tests): record what the cancel asks of them."""
    seen: dict[str, list[Any]] = {"close_trip": [], "nudge": [], "withdraw": [], "settled": []}

    async def close_effects(ctx: Any, trip_id: uuid.UUID) -> dict[str, int]:
        return {"held": 2, "booked": 1}

    async def close_trip(ctx: Any, trip_id: uuid.UUID) -> int:
        seen["close_trip"].append(trip_id)
        return 2

    async def nudge(ctx: Any, request_id: uuid.UUID, **_: Any) -> None:
        seen["nudge"].append(request_id)

    async def live_count(ctx: Any, trip_id: uuid.UUID) -> int:
        return 1

    async def withdraw(ctx: Any, trip_id: uuid.UUID, *, notices: Any = None) -> int:
        seen["withdraw"].append((trip_id, notices))
        return 1

    async def settled(ctx: Any, trip_id: uuid.UUID, invoice_id: uuid.UUID, actor: str) -> None:
        seen["settled"].append((invoice_id, actor))

    monkeypatch.setattr(aa_lines, "close_effects", close_effects)
    monkeypatch.setattr(aa_lines, "close_trip", close_trip)
    monkeypatch.setattr(aa_lines, "nudge", nudge)
    monkeypatch.setattr(ops_release, "live_count", live_count)
    monkeypatch.setattr(ops_release, "withdraw_on_cancel", withdraw)
    monkeypatch.setattr(reminders, "settled", settled)
    monkeypatch.setattr(cancellation, "CANCEL_HOOKS", [])
    return seen


def _effects(out: dict[str, Any]) -> dict[str, int]:
    return {e["kind"]: e["count"] for e in out["effects"]}


# ── the dry run ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_dry_run_lists_every_effect_and_writes_nothing(seams: dict[str, list[Any]]) -> None:
    conn = world()
    out = await cancellation.plan(tenant_ctx(conn, role="owner"), TRIP)
    assert _effects(out) == {
        "dispatch_cancelled": 2,
        "workflows_stopped": 2,
        "tasks_skipped": 3,  # the open tasks less the kept verify_transfer
        "call_slots_cancelled": 1,
        "client_links_revoked": 3,
        "aa_holds_released": 2,
        "aa_bookings_cancelled": 2,
        "supplier_lines_to_cancel": 1,
        "invoices_voided": 2,  # the draft and the open one holding nothing; not the one with a refund open
        "invoices_claimed_kept": 1,
        "refund_requests": 1,  # only the payment with something left and an invoice
        "payments_without_invoice": 1,
        "commissions_cancelled": 1,
        "commissions_claimed_kept": 1,
        "ops_release_withdrawn": 1,
    }
    assert conn.sql(flat(cancellation.OPEN_TASKS_SQL)) == [(TRIP, ["verify_transfer"])]
    assert out["invoices_claimed"] == [str(INV_CLAIMED)]
    assert out["terms"] == {"version_label": "v3", "accepted_at": "2026-09-02T10:00:00"}
    assert out["supplier_lines"][0]["confirmation_no"] == "R-9"
    assert out["aa_bookings"][0] == {"booking_id": "b-1", "line_id": str(LINE_B1), "titles": ["Halong 2D"],
                                     "asked": False, "failed_code": None}  # fmt: skip
    full, done, loose = out["payments"]
    assert (full["refundable"], full["refund_amount"], full["needs_invoice"]) == ("100.00", "100.00", False)
    assert (done["refundable"], done["refund_amount"]) == ("0.00", "0.00")  # never a negative refund
    assert loose["needs_invoice"] and loose["invoice_id"] is None and loose["received_on"] == "2026-10-05"
    assert out["commissions"][0]["expected"] == {"amount": "40.00", "currency": "USD"}
    assert out["commissions"][1]["expected"] is None
    # Nothing written: only reads ran, no collaborator that writes was called.
    assert not any(sql.startswith(("UPDATE", "INSERT")) for sql, _ in conn.calls)
    assert seams["close_trip"] == [] and seams["withdraw"] == [] and seams["nudge"] == []


@pytest.mark.asyncio
async def test_the_member_chosen_refund_is_capped_at_what_is_left(seams: dict[str, list[Any]]) -> None:
    lowered = await cancellation.plan(tenant_ctx(world()), TRIP, refunds={PAY_FULL: Decimal("30")})
    assert lowered["payments"][0]["refund_amount"] == "30.00"  # lower keeps the non-refundable part
    capped = await cancellation.plan(tenant_ctx(world()), TRIP, refunds={PAY_FULL: Decimal("900")})
    assert capped["payments"][0]["refund_amount"] == "100.00"  # never more than the payment has left
    none = await cancellation.plan(tenant_ctx(world()), TRIP, refunds={PAY_FULL: Decimal("0")})
    assert none["payments"][0]["refund_amount"] == "0.00" and _effects(none)["refund_requests"] == 0


@pytest.mark.asyncio
async def test_a_member_without_the_margin_right_sees_no_commission(seams: dict[str, list[Any]]) -> None:
    conn = world()
    out = await cancellation.plan(tenant_ctx(conn, role="assistant"), TRIP)
    assert out["commissions"] == []
    assert _effects(out)["commissions_cancelled"] == 0 and _effects(out)["commissions_claimed_kept"] == 0
    assert conn.sql("FROM crm.commission c") == []  # the commission rows are not even read


@pytest.mark.asyncio
async def test_registered_hooks_join_workflows_stopped_once_each(seams: dict[str, list[Any]]) -> None:
    calls: list[bool] = []

    async def hook(ctx: Any, trip_id: uuid.UUID, dry_run: bool) -> int:
        calls.append(dry_run)
        return 4

    cancellation.register_cancel_hook("automations", hook)
    cancellation.register_cancel_hook("automations", hook)  # a second registration of the same name is ignored
    assert len(cancellation.CANCEL_HOOKS) == 1
    out = await cancellation.plan(tenant_ctx(world()), TRIP)
    assert _effects(out)["workflows_stopped"] == 6 and calls == [True]


# ── the run ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_run_applies_every_effect_and_cancels_at_once_when_no_aa_booking_waits(
    seams: dict[str, list[Any]],
) -> None:
    conn = world(open_status='{"status": "done"}')
    ctx = tenant_ctx(conn, role="owner")
    notices: list[Any] = []
    out = await cancellation.run(ctx, TRIP, {"stage": "booked"}, "client_request",
                                 refunds={PAY_FULL: Decimal("60")}, keep_commission=[COM_EXPECTED],
                                 notices=notices)  # fmt: skip
    # Workflows stop, open tasks skip except verify_transfer, slots and links go.
    assert conn.sql(flat(stages.CANCEL_DISPATCH_SQL)) == [(TRIP,)]
    assert conn.sql(flat(cancellation.SKIP_TASKS_SQL)) == [(TRIP, ACTOR, ["verify_transfer"])]
    assert conn.sql(flat(stages.CANCEL_SLOTS_SQL)) == [(TRIP,)] and conn.sql(flat(stages.CANCEL_LINKS_SQL)) == [(TRIP,)]
    # Invoices: both voided; only the issued one gets its commitment and its reminder settled.
    assert conn.sql(flat(cancellation.VOID_SQL)) == [(INV_DRAFT, ACTOR), (INV_OPEN, ACTOR)]
    assert conn.sql(flat(cancellation.INVOICE_COMMITMENT_SQL)) == [(TRIP, f"invoice:{INV_OPEN}")]
    assert seams["settled"] == [(INV_OPEN, ACTOR)]
    # One refund request for the amount the member set, with the reason naming the accepted terms.
    refund = conn.sql(flat(cancellation.REFUND_SQL))
    assert len(refund) == 1
    _rid, trip, invoice, payment, amount, currency, reason = refund[0]
    assert (trip, invoice, payment, amount, currency) == (TRIP, INV_PAID, PAY_FULL, Decimal("60.00"), "USD")
    assert reason == "Trip cancelled (client request). Booking terms v3 accepted on 2026-09-02."
    # The loose payment asks the owner to review it; the confirmed agency line opens the supplier task.
    tasks = [args[1:] for args in conn.sql(flat(cancellation.TASK_SQL))]
    assert tasks == [(cancellation.REFUND_REVIEW_TASK, "urgent"), (cancellation.SUPPLIER_TASK, "urgent")]
    # The kept commission stays expected; the others are cancelled.
    assert conn.sql(flat(cancellation.KEEP_COMMISSION_SQL)) == [([COM_EXPECTED],)]
    assert conn.sql(flat(cancellation.CANCEL_COMMISSION_SQL)) == [(TRIP, ACTOR, [COM_EXPECTED])]
    # AA: holds released, Booking asked only for the booking not asked yet, the request nudged.
    assert seams["close_trip"] == [TRIP]
    assert conn.sql(flat(cancellation.ASK_CANCEL_SQL)) == [(TRIP, LINE_B1)]
    assert seams["nudge"] == [REQUEST]
    assert seams["withdraw"] == [(TRIP, notices)]
    # The cancellation row gets the effect counts, with the refunds actually requested.
    (trip_arg, reason_arg, counts_json), = conn.sql(flat(cancellation.OPEN_SQL))  # fmt: skip
    counts = json.loads(counts_json)
    assert (trip_arg, reason_arg, counts["refund_requests"], counts["aa_bookings_cancelled"]) == (
        TRIP, "client_request", 1, 2,
    )  # fmt: skip
    # Nothing waits at AA: the trip moves to cancelled now, with its history.
    assert conn.sql(flat(stages.MOVE_SQL)) == [(TRIP, "cancelled", None, "client_request", "none")]
    assert conn.sql(flat(stages.HISTORY_SQL)) == [
        (TRIP, "booked", "cancelled", "cancel_client_request", False, None, None, None)
    ]
    assert conn.sql("SET waiting_on = 'aa'") == []
    assert conn.sql(flat(commissions.SYNC_SQL)) == [(TRIP,)]
    audits = [(e.action, e.object_kind, e.reason) for e in ctx.audit_events]
    assert audits[-1] == ("trip_cancel", "trip", "client_request")
    assert ("invoice_void", "invoice", "trip_cancelled") in audits and audits.count(
        ("invoice_void", "invoice", "trip_cancelled")
    ) == 2
    assert ("refund_request", "refund_request", "trip_cancelled") in audits
    assert out["cancellation"]["status"] == "pending" and _effects(out)["invoices_voided"] == 2


@pytest.mark.asyncio
async def test_the_run_keeps_the_trip_pending_while_an_aa_booking_waits(seams: dict[str, list[Any]]) -> None:
    conn = world(open_status={"status": "pending"}).on(flat(cancellation.TERMS_SQL), None)
    ctx = tenant_ctx(conn)
    out = await cancellation.run(ctx, TRIP, {"stage": "booked"}, "force_majeure", refunds={},
                                 keep_commission=[], notices=None)  # fmt: skip
    assert out["terms"] is None
    assert conn.sql(flat(cancellation.REFUND_SQL))[0][6] == (
        "Trip cancelled (force majeure). No booking terms were accepted in the CRM."
    )
    assert conn.sql(flat(stages.MOVE_SQL)) == []  # card R: never "cancelled" early
    assert conn.sql("SET waiting_on = 'aa'") == [(TRIP,)]
    assert conn.sql(flat(cancellation.KEEP_COMMISSION_SQL)) == []
    assert conn.sql(flat(cancellation.CANCEL_COMMISSION_SQL)) == [(TRIP, ACTOR, [])]
    # Default refund: everything left on the payment.
    assert conn.sql(flat(cancellation.REFUND_SQL))[0][4] == Decimal("100.00")
    assert ctx.audit_events[-1].reason == "pending"
    assert out["cancellation"]["requested_at"] == ISSUED.isoformat() and out["cancellation"]["finished_at"] is None


@pytest.mark.asyncio
async def test_the_run_refuses_to_keep_a_commission_it_would_not_cancel(seams: dict[str, list[Any]]) -> None:
    conn = world()
    for keep in ([COM_CLAIMED], [uuid.uuid4()]):  # a claimed one stays anyway; a foreign id is not this trip's
        with pytest.raises(Invalid) as err:
            await cancellation.run(tenant_ctx(conn), TRIP, {"stage": "booked"}, "other", refunds={},
                                   keep_commission=keep, notices=None)  # fmt: skip
        assert err.value.code == "invalid_keep_commission"
    assert not any(sql.startswith(("UPDATE", "INSERT")) for sql, _ in conn.calls)
    assert seams["close_trip"] == []


@pytest.mark.asyncio
async def test_the_run_calls_the_hooks_for_real(seams: dict[str, list[Any]]) -> None:
    calls: list[bool] = []

    async def hook(ctx: Any, trip_id: uuid.UUID, dry_run: bool) -> int:
        calls.append(dry_run)
        return 1

    cancellation.register_cancel_hook("automations", hook)
    await cancellation.run(tenant_ctx(world()), TRIP, {"stage": "booked"}, "duplicate", refunds={},
                           keep_commission=[], notices=None)  # fmt: skip
    assert calls == [True, False]  # the plan's dry run, then the run


@pytest.mark.asyncio
async def test_a_stage_the_database_refuses_is_a_conflict(seams: dict[str, list[Any]]) -> None:
    conn = world().on(flat(stages.MOVE_SQL), asyncpg.CheckViolationError("stage"))
    with pytest.raises(Conflict) as err:
        await cancellation.move_to_cancelled(tenant_ctx(conn), TRIP, "completed", "other")
    assert err.value.code == "transition_not_allowed"
    assert conn.sql(flat(stages.HISTORY_SQL)) == []


# ── Cancel again, the end of a pending cancel, a refunded invoice ────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_again_asks_booking_only_for_bookings_without_a_live_request(seams: dict[str, list[Any]]) -> None:
    conn = world()
    ctx = tenant_ctx(conn)
    out = await cancellation.retry(ctx, TRIP)
    assert conn.sql(flat(cancellation.ASK_CANCEL_SQL)) == [(TRIP, LINE_B1)]
    assert out["requeued"] == 1 and seams["nudge"] == [REQUEST]
    assert [(e.action, e.reason) for e in ctx.audit_events] == [("trip_cancel", "retry")]
    assert out["cancellation"]["status"] == "pending" and len(out["aa_bookings"]) == 2

    conn = world().on(flat(cancellation.ASK_CANCEL_SQL), None)  # a pending request already there
    ctx = tenant_ctx(conn)
    out = await cancellation.retry(ctx, TRIP)
    assert out["requeued"] == 0 and ctx.audit_events == []


@pytest.mark.asyncio
async def test_pending_reads_the_cancellation_row() -> None:
    conn = ScriptedConn()
    assert await cancellation.pending(tenant_ctx(conn), TRIP) is None
    assert not await cancellation.is_pending(tenant_ctx(conn), TRIP)
    finished = dt.datetime(2026, 10, 3, tzinfo=dt.UTC)
    row = {"status": "done", "reason_code": "duplicate", "from_stage": "booked", "aa_bookings": 0,
           "created_at": ISSUED, "finished_at": finished}  # fmt: skip
    conn.on(flat(cancellation.PENDING_SQL), row)
    assert (await cancellation.pending(tenant_ctx(conn), TRIP))["finished_at"] == finished.isoformat()
    assert not await cancellation.is_pending(tenant_ctx(conn), TRIP)
    conn.on(flat(cancellation.PENDING_SQL), {**row, "status": "pending"})
    assert await cancellation.is_pending(tenant_ctx(conn), TRIP)


def _finishing(row: dict[str, Any] | None, left: int, stage: str | None) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on(flat(cancellation.PENDING_SQL), row)
    conn.on(flat(cancellation.LEFT_SQL), left)
    conn.on(flat(cancellation.TRIP_SQL), stage)
    conn.on(flat(commissions.SYNC_SQL), "{}")
    return conn


PENDING_ROW = {"status": "pending", "reason_code": "aa_cancelled"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "left", "stage"),
    [
        (None, 0, "booked"),  # no cancellation
        ({**PENDING_ROW, "status": "done"}, 0, "cancelled"),  # already done
        (PENDING_ROW, 1, "booked"),  # an AA booking is still live
        (PENDING_ROW, 0, None),  # the trip is gone
    ],
)
async def test_finish_if_done_waits_until_every_aa_booking_is_cancelled(
    row: dict[str, Any] | None, left: int, stage: str | None
) -> None:
    conn = _finishing(row, left, stage)
    ctx = system_ctx(conn)
    assert await cancellation.finish_if_done(ctx, TRIP) is False
    assert conn.sql(flat(cancellation.FINISH_SQL)) == [] and ctx.audit_events == []


@pytest.mark.asyncio
async def test_finish_if_done_moves_the_trip_and_closes_the_failed_cancel_task() -> None:
    conn = _finishing(PENDING_ROW, 0, "pre_departure")
    ctx = system_ctx(conn)
    assert await cancellation.finish_if_done(ctx, TRIP) is True
    assert conn.sql(flat(stages.MOVE_SQL)) == [(TRIP, "cancelled", None, "aa_cancelled", "none")]
    assert conn.sql(flat(stages.HISTORY_SQL))[0][1:4] == ("pre_departure", "cancelled", "cancel_aa_cancelled")
    assert conn.sql(flat(cancellation.FINISH_SQL)) == [(TRIP,)]
    assert conn.sql(flat(cancellation.CLOSE_FAILED_TASK_SQL)) == [(TRIP, cancellation.CANCEL_FAILED_TASK)]
    assert conn.sql(flat(commissions.SYNC_SQL)) == [(TRIP,)]
    assert [(e.action, e.object_id, e.reason) for e in ctx.audit_events] == [("trip_cancel", str(TRIP), "aa_cancelled")]

    # A trip already moved (e.g. by hand) only closes the cancellation.
    conn = _finishing(PENDING_ROW, 0, "cancelled")
    assert await cancellation.finish_if_done(system_ctx(conn), TRIP) is True
    assert conn.sql(flat(stages.MOVE_SQL)) == [] and conn.sql(flat(cancellation.FINISH_SQL)) == [(TRIP,)]


def _refunded(row: dict[str, Any] | None, block: dict[str, Any] | None) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on(flat(cancellation.REFUNDED_VOID_SQL), row)
    conn.on(flat(VOID_BLOCK_SQL), block)
    return conn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "block"),
    [
        (None, None),  # not an open / expired invoice of a cancelled trip (e.g. paid: no void edge)
        ({"issued_at": ISSUED}, {"held": Decimal("20"), "refund_open": False}),  # a non-refundable part stays
        ({"issued_at": ISSUED}, {"held": Decimal("0"), "refund_open": True}),  # another refund still open
    ],
)
async def test_a_refunded_invoice_stays_while_it_holds_money(
    seams: dict[str, list[Any]], row: dict[str, Any] | None, block: dict[str, Any] | None
) -> None:
    conn = _refunded(row, block)
    assert await cancellation.void_if_refunded(tenant_ctx(conn), TRIP, INV_PAID, ACTOR) is False
    assert conn.sql(flat(cancellation.VOID_SQL)) == [] and seams["settled"] == []


@pytest.mark.asyncio
async def test_a_refunded_invoice_is_voided_once_nothing_is_held(seams: dict[str, list[Any]]) -> None:
    conn = _refunded({"issued_at": ISSUED}, {"held": Decimal("0"), "refund_open": False})
    ctx = tenant_ctx(conn)
    assert await cancellation.void_if_refunded(ctx, TRIP, INV_OPEN, ACTOR) is True
    assert conn.sql(flat(cancellation.VOID_SQL)) == [(INV_OPEN, ACTOR)]
    assert conn.sql(flat(cancellation.INVOICE_COMMITMENT_SQL)) == [(TRIP, f"invoice:{INV_OPEN}")]
    assert seams["settled"] == [(INV_OPEN, ACTOR)]
    assert [(e.action, e.reason) for e in ctx.audit_events] == [("invoice_void", "trip_cancelled_refunded")]

    conn = _refunded({"issued_at": None}, None)  # never issued: no commitment, no reminder
    assert await cancellation.void_if_refunded(tenant_ctx(conn), TRIP, INV_DRAFT, ACTOR) is True
    assert conn.sql(flat(cancellation.INVOICE_COMMITMENT_SQL)) == [] and len(seams["settled"]) == 1


# ── Booking's fee in the dry run ─────────────────────────────────────────────────────────────────


class FeeClient:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, bool, str]] = []

    async def cancel_booking(
        self, tenant_id: uuid.UUID, booking_id: str, reason: str, *, dry_run: bool, idempotency_key: str
    ) -> dict[str, Any]:
        self.calls.append((booking_id, reason, dry_run, idempotency_key))
        answer = self.answers[booking_id]
        if isinstance(answer, BaseException):
            raise answer
        return answer


FEE = {
    "booking_id": "b-1",
    "dry_run": True,
    "fee": {"amount": "250", "currency": "USD"},
    "refundable_b2b_amount": {"amount": "750.00", "currency": "USD"},
    "fee_basis": {"policy_id": "pol-1", "days_before_departure": 40, "fee_pct": "25"},
    "cancelled_at": None,
}


@pytest.mark.asyncio
async def test_booking_fees_per_booking_and_what_a_refusal_or_an_outage_leaves() -> None:
    client = FeeClient({
        "b-1": FEE,
        "b-2": BookingUnavailable("booking_timeout"),
        "b-3": BookingRefusal(409, "invalid"),
        "b-4": {**FEE, "fee": {"amount": "-1", "currency": "USD"}},  # a contract breach
    })  # fmt: skip
    preview = {"aa_bookings": [{"booking_id": f"b-{i}"} for i in range(1, 5)]}
    out = await cancellation.booking_fees(client, TENANT, preview, "client_illness")
    one, two, three, four = out["aa_bookings"]
    assert one["fee"] == {"amount": "250.00", "currency": "USD", "fee_pct": "25", "days_before_departure": 40}
    assert two["fee"] is None and "fee_code" not in two
    assert three["fee"] is None and three["fee_code"] == "invalid"
    assert four["fee"] is None and four["fee_code"] == "booking_contract"
    assert out["booking_unavailable"] is True  # one booking got no answer: the run will wait for AA
    booking_id, reason, dry_run, key = client.calls[0]
    assert (booking_id, reason, dry_run) == ("b-1", "client_cancelled", True)
    assert key.startswith("crm-cancel-preview:b-1:")
    assert len({c[3] for c in client.calls}) == 4  # a fresh key per preview


@pytest.mark.asyncio
async def test_booking_fees_ask_at_most_ten_bookings_and_none_without_a_client() -> None:
    many = {"aa_bookings": [{"booking_id": f"b-{i}"} for i in range(12)]}
    client = FeeClient({f"b-{i}": {**FEE, "booking_id": f"b-{i}"} for i in range(12)})
    out = await cancellation.booking_fees(client, TENANT, many, "unknown_reason")
    assert len(client.calls) == cancellation.MAX_PREVIEW_FEES
    assert {c[1] for c in client.calls} == {"other"}  # an unmapped reason is "other"
    assert out["aa_bookings"][-1]["fee"] is None and out["booking_unavailable"] is False
    offline = await cancellation.booking_fees(None, TENANT, {"aa_bookings": [{"booking_id": "b-1"}]}, "other")
    assert offline["aa_bookings"][0]["fee"] is None and offline["booking_unavailable"] is True
    assert (await cancellation.booking_fees(None, TENANT, {}, "other"))["booking_unavailable"] is False


def test_bad_kept_commission_lists_and_refund_items_are_refused() -> None:
    assert cancellation._uuid_list(None, "keep_commission") == []
    one = uuid.uuid4()
    assert cancellation._uuid_list([str(one)], "keep_commission") == [one]
    for bad in ("x", ["not-a-uuid"], [str(one)] * 201):
        with pytest.raises(Invalid) as err:
            cancellation._uuid_list(bad, "keep_commission")
        assert err.value.code == "invalid_keep_commission"
    for bad in (["x"], [{"payment_id": str(one), "amount": "-1"}], [{"payment_id": str(one), "amount": "NaN"}]):
        with pytest.raises(Invalid):
            cancellation.refund_choices(bad)
    assert cancellation.refund_choices([{"payment_id": str(one), "amount": "0"}]) == {one: Decimal("0")}
    assert cancellation.today_utc() == dt.datetime.now(dt.UTC).date()
