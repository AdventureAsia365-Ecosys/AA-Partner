"""AA-957 (card CRM-27) end to end on PostgreSQL with the RDS role shape, as crm_app through ``tenant_tx()`` and the
Booking sync, against the in-memory Booking (``tests/aa_fakes.py``).

- **Done when**: a cancelled synthetic trip leaves no live hold, no live client link and no expected commission; the
  dry run lists every effect with Booking's fee; the run voids the unpaid invoice, proposes the refund the member set
  (a partial refund keeps the rest on the ledger, card E), cancels the expected commission, opens the supplier task,
  and once Booking answers the AA booking is cancelled with its fee as a ``cancellation_cost`` (card E).
- **R**: cancel while Booking is down -> "cancellation pending", retried, never ``cancelled`` early; the trip moves
  nowhere else meanwhile (409 ``cancellation_pending``) and the date rule leaves it.
- A refused cancel (``forbidden``) opens the owner task ``aa_cancel_failed``; pressing Cancel again asks Booking again.
- ``invalid`` for a booking still live at AA is a refusal too (never "cancelled" with the fee lost); the feed's
  ``booking.cancelled`` then completes the cancellation and closes the task; a late ``invalid`` after the feed
  cancelled the booking is done without a task.
- A cancel that runs out of attempts opens ``aa_cancel_failed`` and the trip stays "cancellation pending".
- While a cancellation is pending, a queued hold is dropped (``trip_closed``) instead of placing a new AA hold.
- Module 13: the confirm makes the expected commission row; claim, receive at the receipt day's rate, write off; the
  assistant gets 403 on every commission route; the overdue owner task once; a claimed commission blocks the delete.
- Review fix pass: an Undo never moves a trip whose cancellation is pending; the single commission read is audited;
  the commission sweep skips a trip whose cancellation is pending; the AA fee's FX day is the agency-timezone day of
  Booking's ``cancelled_at``; a trip's commission totals are the trip's own; an invoice the cancel kept for its money
  is voided once the refund executed; the supplier task lists each supplier as one checklist line.
- S262 round 3: a trip whose cancellation waits for AA takes no new AA quote, agency line, dates or traveller-visible
  document (409 ``trip_cancelled``) and is not deleted (409 ``cancellation_pending``); Undo claim forgets the claim.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections import Counter
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app import cancellation, pipeline, stages
from app.booking import aa_lines, sync
from app.cancellation import booking_fees
from app.commissions import service as commissions
from app.documents import service as documents
from app.documents.store import MemoryDocumentStore
from app.errors import Conflict, Forbidden
from app.invoices import service as invoices
from app.ledger import lines, suppliers
from app.lifecycle import trash
from app.payments import service as payments
from app.permissions import Action
from app.portal import access
from app.proposals import service as proposals
from app.reports import queries as report_queries
from app.stages_job import run_agency as date_rule
from app.tenancy import system_tx, tenant_tx
from app.vault import KeyProvider, Vault
from app.vault.wrapping import LocalKeyWrapper
from tests.aa_fakes import FakeBooking, booking_client
from tests.integration.test_aa_lines_api import _body as _aa_body
from tests.integration.test_aa_lines_api import _due_now, _scalar
from tests.integration.test_aa_lines_api import _setup as _aa_setup
from tests.integration.test_lifecycle_api import _accept as _lc_accept
from tests.integration.test_lifecycle_api import _proposal as _lc_proposal
from tests.integration.test_lifecycle_api import _schedule as _lc_schedule
from tests.integration.test_lifecycle_api import _setup as _lc_setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

REASON = "client_request"
# Our own catalog row: the AA lines tests retire T-100 and leave the shared catalog sync in backoff (as the builder and
# margin tests do with T-949).
TOUR = "T-957"
PDF = b"%PDF-1.7\nsynthetic\n%%EOF"


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    s = await _aa_setup(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state) "
            "VALUES ($1, 'Mekong Delta 3 days', 'VN', 3, 'served') "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served'",
            TOUR,
        )
    return s


def _body(**over: Any) -> dict[str, Any]:
    return _aa_body(tour_source_id=TOUR, **over)


async def _sync(pool: asyncpg.Pool, fake: FakeBooking, tenant: uuid.UUID) -> Counter[str]:
    """One run of the Booking sync job for this test's agency, feed included: the job's own per-agency step
    (``BookingSync.run_agency``, what ``__call__`` runs for each agency). AA-1032: the shared helper runs the whole job,
    which walks every agency the session has made by then (hundreds by this file) and sent their leftover requests to
    this test's fake; that made this file ~300 s of the ~450 s CI integration run. Stricter, not looser: an error of
    this agency's run raises here instead of being counted as ``agency_failed``."""
    counts: Counter[str] = Counter()
    await sync.BookingSync(booking_client(fake)).run_agency(pool, tenant, counts, feed=True)
    return counts


def _keys() -> KeyProvider:
    return KeyProvider({"local": LocalKeyWrapper({"k1": os.urandom(32)}, "k1")}, "local")


async def _aa_booking(pool: asyncpg.Pool, s: dict[str, Any], fake: FakeBooking) -> uuid.UUID:
    """An AA line quoted, held and confirmed by Booking (a real AA booking, ``b-h<n>``)."""
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        line_id = uuid.UUID((await aa_lines.request_line(ctx, s["trip"], _body()))["booking_line_id"])
    await _sync(pool, fake, s["tenant"])
    for action in ("hold", "confirm"):
        async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
            await aa_lines.ask(ctx, line_id, action)
        await _sync(pool, fake, s["tenant"])
    return line_id


async def _agency_line(
    pool: asyncpg.Pool,
    s: dict[str, Any],
    keys: KeyProvider,
    commission: str = "40",
    *,
    trip: uuid.UUID | None = None,
    supplier_name: str = "Riverside Lodge",
) -> uuid.UUID:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        supplier = {"name": supplier_name, "category": "accommodation", "commission_due_days": 10}
        own = await suppliers.create_supplier(ctx, Vault(keys, ctx), supplier)
        body = {
            "agency_supplier_id": uuid.UUID(own["agency_supplier_id"]),
            "title": "Lodge 2 nights",
            "confirmation_no": "RL-7",
            "service_from": "2026-12-01",
            "service_to": "2026-12-03",
        }
        line_id = uuid.UUID((await lines.create_line(ctx, trip or s["trip"], body))["booking_line_id"])
        money = {
            "cost": {"amount": "300", "currency": "USD"},
            "sell": {"amount": "400", "currency": "USD"},
            "commission_expected": {"amount": commission, "currency": "USD"},
        }
        await lines.put_money(ctx, line_id, money)
        await lines.confirm(ctx, line_id)
    return line_id


async def _paid_invoice(pool: asyncpg.Pool, s: dict[str, Any], keys: KeyProvider, pay: str) -> dict[str, Any]:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        draft = await invoices.create_typed(ctx, s["trip"], kind="adjustment", currency="USD",
                                            lines=[{"description": "Trip deposit", "quantity": "1",
                                                    "unit_amount": "500"}])  # fmt: skip
        issued = await invoices.issue(ctx, uuid.UUID(draft["invoice_id"]))
        return await payments.record_payment(ctx, Vault(keys, ctx), uuid.UUID(issued["invoice_id"]),
                                             amount=Decimal(pay), method="bank_transfer", received_on=None)  # fmt: skip


async def _draft_invoice(pool: asyncpg.Pool, s: dict[str, Any]) -> uuid.UUID:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        draft = await invoices.create_typed(ctx, s["trip"], kind="adjustment", currency="USD",
                                            lines=[{"description": "Visa letter", "quantity": "1",
                                                    "unit_amount": "40"}])  # fmt: skip
    return uuid.UUID(draft["invoice_id"])


async def _cancel(pool: asyncpg.Pool, s: dict[str, Any], *, dry_run: bool, **kw: Any) -> dict[str, Any]:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"], action=Action.TRIP_CANCEL) as ctx:
        return await stages.cancel(ctx, s["trip"], reason=REASON, dry_run=dry_run, **kw)


async def _stage(pool: asyncpg.Pool, s: dict[str, Any]) -> str:
    return await _scalar(pool, s["tenant"], "SELECT stage FROM crm.trip WHERE trip_id = $1", s["trip"])


def _effects(out: dict[str, Any]) -> dict[str, int]:
    return {e["kind"]: e["count"] for e in out["effects"]}


async def test_done_when_a_cancelled_trip_leaves_no_live_hold_link_or_expected_commission(
    app_pool: asyncpg.Pool,
) -> None:
    s = await _setup(app_pool)
    keys = _keys()
    fake = FakeBooking(confirm_line_status="confirmed")
    booked = await _aa_booking(app_pool, s, fake)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:  # a second AA line, only held
        held = uuid.UUID((await aa_lines.request_line(ctx, s["trip"], _body(component_id="tp:2")))["booking_line_id"])
    await _sync(app_pool, fake, s["tenant"])
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await aa_lines.ask(ctx, held, "hold")
    await _sync(app_pool, fake, s["tenant"])
    own = await _agency_line(app_pool, s, keys)
    paid = await _paid_invoice(app_pool, s, keys, "300")
    draft = await _draft_invoice(app_pool, s)

    preview = await _cancel(app_pool, s, dry_run=True)
    preview = await booking_fees(booking_client(fake), s["tenant"], preview, REASON)
    effects = _effects(preview)
    assert effects["aa_bookings_cancelled"] == 1 and effects["aa_holds_released"] == 1
    assert effects["supplier_lines_to_cancel"] == 1 and effects["commissions_cancelled"] == 1
    assert effects["invoices_voided"] == 1 and effects["refund_requests"] == 1
    (b,) = preview["aa_bookings"]
    assert b["booking_id"] == "b-h1" and b["fee"] == {"amount": "250.00", "currency": "USD", "fee_pct": "25",
                                                      "days_before_departure": 40}  # fmt: skip
    assert preview["booking_unavailable"] is False and fake.cancellations == {}  # the dry run changed nothing
    payment = preview["payments"][0]
    assert payment["refundable"] == "300.00" and payment["refund_amount"] == "300.00"
    assert await _stage(app_pool, s) == "booked"

    # The run: the member keeps 100 of the payment (card E: the non-refundable part stays on the ledger).
    out = await _cancel(app_pool, s, dry_run=False, refunds=[{"payment_id": payment["payment_id"], "amount": "200"}])
    assert out["stage"] == "booked" and out["cancellation"]["status"] == "pending"  # card R: AA has not answered
    assert await _stage(app_pool, s) == "booked"

    await _due_now(app_pool, s["tenant"])  # the hold release waits out the Undo window
    await _sync(app_pool, fake, s["tenant"])
    assert await _stage(app_pool, s) == "cancelled"
    assert await _scalar(app_pool, s["tenant"], "SELECT status FROM crm.trip_cancellation WHERE trip_id = $1",
                         s["trip"]) == "done"  # fmt: skip
    assert fake.cancellations["b-h1"]["cancelled_at"] is not None
    assert fake.cancel_keys[-1].startswith("crm-cancel:")
    # No live hold, link or expected commission.
    live = await _scalar(app_pool, s["tenant"], "SELECT array_agg(DISTINCT line_status) FROM crm.trip_commitment "
                         "WHERE trip_id = $1 AND kind = 'aa_line'", s["trip"])  # fmt: skip
    assert "held" not in live and "confirmed" not in live
    assert await _scalar(app_pool, s["tenant"], "SELECT count(*) FROM crm.client_link WHERE trip_id = $1 "
                         "AND status IN ('sent', 'opened')", s["trip"]) == 0  # fmt: skip
    assert await _scalar(app_pool, s["tenant"], "SELECT count(*) FROM crm.commission WHERE trip_id = $1 "
                         "AND status = 'expected'", s["trip"]) == 0  # fmt: skip
    # Card E: the fee is a cost line of the cancelled AA booking.
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        line = await lines.line(ctx, booked)
        fee = await ctx.conn.fetchrow(
            "SELECT amount, fx_source FROM crm.v_booking_line_money_current "
            "WHERE booking_line_id = $1 AND kind = 'cancellation_cost'", booked,
        )  # fmt: skip
        ledger = await lines.ledger(ctx, status=None, date_from=None, date_to=None)
        refunds = await ctx.conn.fetch("SELECT amount, status, reason FROM crm.refund_request WHERE trip_id = $1",
                                       s["trip"])  # fmt: skip
        voided = await ctx.conn.fetchval("SELECT status FROM crm.invoice WHERE invoice_id = $1", draft)
        tasks = {r["kind"] for r in await ctx.conn.fetch(
            "SELECT kind FROM crm.task WHERE trip_id = $1 AND status = 'open'", s["trip"])}  # fmt: skip
    assert line["status"] == "cancelled" and line["money"]["cancellation_cost"]["amount"] == "250.00"
    assert fee["amount"] == Decimal("250.00") and fee["fx_source"] == "aa_quote_lock"
    assert ledger["totals"]["cancellation_cost_base"] == "250.00"
    assert [(r["amount"], r["status"]) for r in refunds] == [(Decimal("200.00"), "proposed")]
    assert refunds[0]["reason"].startswith("Trip cancelled (client request).")
    held_money = await _scalar(app_pool, s["tenant"], "SELECT status FROM crm.invoice WHERE invoice_id = $1",
                               uuid.UUID(paid["invoice"]["invoice_id"]))  # fmt: skip
    assert voided == "void" and held_money == "open"  # an invoice holding money is kept (its refund is proposed)
    assert "cancel_with_suppliers" in tasks
    # The supplier line is cancelled by the agent after the call; the date rule closes the task.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await lines.cancel(ctx, own)
    async with system_tx(app_pool, s["tenant"], actor="job:crm_stage_date_rule") as ctx:
        counts = await date_rule(ctx)
    assert counts["cancel_supplier_tasks_closed"] == 1


async def test_r_booking_down_keeps_the_cancellation_pending_and_never_cancelled_early(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking(confirm_line_status="confirmed")
    await _aa_booking(app_pool, s, fake)
    fake.down = True
    preview = await booking_fees(booking_client(fake), s["tenant"], await _cancel(app_pool, s, dry_run=True), REASON)
    assert preview["booking_unavailable"] is True and preview["aa_bookings"][0]["fee"] is None
    await _cancel(app_pool, s, dry_run=False)
    for _ in range(2):
        await _due_now(app_pool, s["tenant"])
        await _sync(app_pool, fake, s["tenant"])
    assert await _stage(app_pool, s) == "booked"
    state = await _scalar(app_pool, s["tenant"], "SELECT row(status, last_error_class) FROM crm.aa_request "
                          "WHERE kind = 'cancel'")  # fmt: skip
    assert tuple(state) == ("pending", "booking_timeout")
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        with pytest.raises(Conflict) as moved:
            await stages.move(ctx, s["trip"], "pre_departure", lost_reason=None, override_reason="manual_stage3")
    assert moved.value.code == "cancellation_pending"
    fake.down = False
    await _due_now(app_pool, s["tenant"])
    await _sync(app_pool, fake, s["tenant"])
    assert await _stage(app_pool, s) == "cancelled"


async def test_a_refused_cancel_opens_a_task_and_cancel_again_asks_again(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking(confirm_line_status="confirmed")
    await _aa_booking(app_pool, s, fake)
    fake.cancel_refusal = "forbidden"
    await _cancel(app_pool, s, dry_run=False)
    await _sync(app_pool, fake, s["tenant"])
    assert await _stage(app_pool, s) == "booked"
    assert await _scalar(app_pool, s["tenant"], "SELECT count(*) FROM crm.task WHERE trip_id = $1 "
                         "AND kind = 'aa_cancel_failed' AND status = 'open'", s["trip"]) == 1  # fmt: skip
    fake.cancel_refusal = None
    again = await _cancel(app_pool, s, dry_run=False)
    assert again["requeued"] == 1 and again["cancellation"]["status"] == "pending"
    await _sync(app_pool, fake, s["tenant"])
    assert await _stage(app_pool, s) == "cancelled"


async def _cancel_request(pool: asyncpg.Pool, s: dict[str, Any]) -> tuple[str, str | None]:
    sql = (
        "SELECT row(status, result_code) FROM crm.aa_request WHERE trip_id = $1 AND kind = 'cancel' "
        "ORDER BY created_at DESC LIMIT 1"
    )
    row = await _scalar(pool, s["tenant"], sql, s["trip"])
    return tuple(row)  # type: ignore[return-value]


async def _failed_task(pool: asyncpg.Pool, s: dict[str, Any]) -> str | None:
    return await _scalar(pool, s["tenant"], "SELECT status FROM crm.task WHERE trip_id = $1 "
                         "AND kind = 'aa_cancel_failed' ORDER BY created_at DESC LIMIT 1", s["trip"])  # fmt: skip


async def test_invalid_for_a_live_booking_keeps_the_trip_pending_until_the_feed_cancels_it(
    app_pool: asyncpg.Pool,
) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking(confirm_line_status="confirmed")
    booked = await _aa_booking(app_pool, s, fake)
    fake.cancel_refusal = "invalid"  # e.g. the trip is already in progress at AA: the booking is still live there
    await _cancel(app_pool, s, dry_run=False)
    await _sync(app_pool, fake, s["tenant"])
    assert await _stage(app_pool, s) == "booked"
    assert await _cancel_request(app_pool, s) == ("failed", "invalid")
    assert await _failed_task(app_pool, s) == "open"
    assert await _scalar(app_pool, s["tenant"], "SELECT status FROM crm.trip_cancellation WHERE trip_id = $1",
                         s["trip"]) == "pending"  # fmt: skip
    assert await _scalar(app_pool, s["tenant"], "SELECT status FROM crm.booking_line WHERE booking_line_id = $1",
                         booked) == "confirmed"  # fmt: skip
    # AA cancels it on its side: the feed's booking.cancelled completes the cancellation and closes the task.
    fake.add_event("booking.cancelled", booking_id="b-h1")
    await _sync(app_pool, fake, s["tenant"])
    assert await _stage(app_pool, s) == "cancelled"
    assert await _scalar(app_pool, s["tenant"], "SELECT status FROM crm.trip_cancellation WHERE trip_id = $1",
                         s["trip"]) == "done"  # fmt: skip
    assert await _failed_task(app_pool, s) == "done"


async def test_a_late_invalid_after_the_feed_cancelled_the_booking_is_done(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking(confirm_line_status="confirmed")
    await _aa_booking(app_pool, s, fake)
    await _cancel(app_pool, s, dry_run=False)
    async with app_pool.acquire() as conn, conn.transaction():  # the cancel request is not due yet
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        await conn.execute("UPDATE crm.aa_request SET next_attempt_at = now() + interval '1 hour' "
                           "WHERE kind = 'cancel' AND status = 'pending'")  # fmt: skip
    fake.add_event("booking.cancelled", booking_id="b-h1")
    await _sync(app_pool, fake, s["tenant"])
    assert await _stage(app_pool, s) == "cancelled"
    fake.cancel_refusal = "invalid"  # Booking: the booking is cancelled already
    await _due_now(app_pool, s["tenant"])
    await _sync(app_pool, fake, s["tenant"])
    assert await _cancel_request(app_pool, s) == ("done", "invalid")
    assert await _failed_task(app_pool, s) is None


async def test_a_cancel_that_runs_out_of_attempts_opens_a_task_and_stays_pending(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking(confirm_line_status="confirmed")
    await _aa_booking(app_pool, s, fake)
    fake.down = True
    await _cancel(app_pool, s, dry_run=False)
    async with app_pool.acquire() as conn, conn.transaction():  # about a day of retries already spent
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        await conn.execute("UPDATE crm.aa_request SET attempts = $1, next_attempt_at = now() "
                           "WHERE kind = 'cancel' AND status = 'pending'", sync.MAX_ATTEMPTS - 1)  # fmt: skip
    await _sync(app_pool, fake, s["tenant"])
    assert await _cancel_request(app_pool, s) == ("failed", "gave_up")
    assert await _failed_task(app_pool, s) == "open"
    assert await _stage(app_pool, s) == "booked"
    assert await _scalar(app_pool, s["tenant"], "SELECT status FROM crm.trip_cancellation WHERE trip_id = $1",
                         s["trip"]) == "pending"  # fmt: skip
    fake.down = False
    again = await _cancel(app_pool, s, dry_run=False)  # Cancel again asks Booking again
    assert again["requeued"] == 1
    await _sync(app_pool, fake, s["tenant"])
    assert await _stage(app_pool, s) == "cancelled" and await _failed_task(app_pool, s) == "done"


async def test_a_pending_cancellation_drops_a_queued_hold(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking(confirm_line_status="confirmed")
    await _aa_booking(app_pool, s, fake)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:  # a second AA line, quoted
        other = uuid.UUID((await aa_lines.request_line(ctx, s["trip"], _body(component_id="tp:2")))["booking_line_id"])
    await _sync(app_pool, fake, s["tenant"])
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await aa_lines.ask(ctx, other, "hold")  # queued, not sent yet
    holds = fake.calls["POST holds"]
    fake.cancel_refusal = "forbidden"  # the cancel is refused: the trip keeps its stage, cancellation pending
    await _cancel(app_pool, s, dry_run=False)
    await _due_now(app_pool, s["tenant"])  # past the Undo window
    # The line's release is not due yet (a claim batch runs in no fixed order: a release sent first would cancel the
    # hold itself), so the hold alone shows the rule.
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        await conn.execute(
            "UPDATE crm.aa_request SET next_attempt_at = now() + interval '1 hour' "
            "WHERE booking_line_id = $1 AND kind = 'release' AND status = 'pending'",
            other,
        )
    await _sync(app_pool, fake, s["tenant"])
    assert await _stage(app_pool, s) == "booked"
    hold = await _scalar(app_pool, s["tenant"], "SELECT row(status, result_code) FROM crm.aa_request "
                         "WHERE booking_line_id = $1 AND kind = 'hold'", other)  # fmt: skip
    assert tuple(hold) == ("cancelled", "trip_closed")
    assert fake.calls["POST holds"] == holds  # no new AA hold for a trip being cancelled


async def test_a_trip_without_aa_bookings_is_cancelled_at_once(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    keys = _keys()
    await _agency_line(app_pool, s, keys)
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        (c,) = (await commissions.listing(ctx, status=None, trip_id=s["trip"]))["commissions"]
    out = await _cancel(app_pool, s, dry_run=False, keep_commission=[c["commission_id"]])  # the supplier still pays
    assert out["stage"] == "cancelled" and out["cancellation"]["status"] == "done"
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        kept = await commissions.get(ctx, uuid.UUID(c["commission_id"]))
    assert kept["status"] == "expected" and kept["keep_on_cancel"] is True


async def test_module_13_expected_claim_receive_write_off_and_the_assistant(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    keys = _keys()
    first = await _agency_line(app_pool, s, keys, commission="40")
    await _agency_line(app_pool, s, keys, commission="25")
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        listed = await commissions.listing(ctx, status=None, trip_id=None)
    assert listed["commission_totals"]["open_count"] == 2 and listed["commission_totals"]["open_base"] == "65.00"
    by_line = {c["booking_line_id"]: c for c in listed["commissions"]}
    one, two = by_line[str(first)], next(c for c in listed["commissions"] if c["booking_line_id"] != str(first))
    assert one["due_on"] == "2026-12-13" and one["commission_expected"]["amount"] == "40.00"  # service end + 10 days
    # The assistant never reaches a commission route (VIEW_MARGIN / PAYMENT_MARK_RECEIVED).
    for action in (Action.VIEW_MARGIN, Action.PAYMENT_MARK_RECEIVED):
        with pytest.raises(Forbidden):
            async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"], action=action):
                pass
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        claimed = await commissions.claim(ctx, uuid.UUID(one["commission_id"]), "INV-SUP-12")
        assert claimed["status"] == "claimed" and claimed["claim_ref"] == "INV-SUP-12"
        today = await ctx.conn.fetchval("SELECT (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date")
        got = await commissions.receive(ctx, uuid.UUID(one["commission_id"]),
                                        {"amount": "35", "currency": "USD", "received_on": today.isoformat(),
                                         "reference": "BANK-1"})  # fmt: skip
        assert got["status"] == "received" and got["commission_received"]["short_amount"] == "5.00"
        assert got["commission_received"]["fx"]["basis"] == "receipt_date"
        with pytest.raises(Conflict) as closed:
            await commissions.write_off(ctx, uuid.UUID(one["commission_id"]), "too_small", None)
        assert closed.value.code == "commission_closed"
        # The second one is overdue once its due date passed: one owner task, once.
        await commissions.set_due(ctx, uuid.UUID(two["commission_id"]), today - dt.timedelta(days=3))
        overdue = await commissions.listing(ctx, status="overdue", trip_id=None)
        assert [c["commission_id"] for c in overdue["commissions"]] == [two["commission_id"]]
        assert overdue["commissions"][0]["days_overdue"] == 3
    assert await _scalar(app_pool, s["tenant"], "SELECT crm.trip_delete_blockers($1)", s["trip"]) == ["has_commission"]
    async with system_tx(app_pool, s["tenant"], actor="job:crm_stage_date_rule") as ctx:
        assert (await date_rule(ctx))["commissions_overdue"] == 1
    async with system_tx(app_pool, s["tenant"], actor="job:crm_stage_date_rule") as ctx:
        assert "commissions_overdue" not in await date_rule(ctx)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        off = await commissions.write_off(ctx, uuid.UUID(two["commission_id"]), "supplier_refused", "Closed season")
    assert off["status"] == "written_off" and off["close_reason"] == "supplier_refused"


async def _other_trip(pool: asyncpg.Pool, s: dict[str, Any]) -> uuid.UUID:
    trip = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        sql = "INSERT INTO crm.trip (trip_id, title, stage, waiting_on) VALUES ($1, 'Hue', 'booked', 'none')"
        await conn.execute(sql, trip)
    return trip


async def test_undo_never_moves_a_trip_whose_cancellation_is_pending(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking(confirm_line_status="confirmed")
    await _aa_booking(app_pool, s, fake)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        moved = await stages.move(ctx, s["trip"], "pre_departure", override_reason="manual_stage3")
    out = await _cancel(app_pool, s, dry_run=False)  # within the Undo window; AA has not answered
    assert out["cancellation"]["status"] == "pending"
    with pytest.raises(Conflict) as undone:
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
            await stages.undo(ctx, s["trip"], uuid.UUID(moved["undo"]["history_id"]))
    assert undone.value.code == "cancellation_pending"
    assert await _stage(app_pool, s) == "pre_departure"


async def test_one_commission_read_is_audited_as_the_list_is(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    await _agency_line(app_pool, s, _keys())
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        (c,) = (await commissions.listing(ctx, status=None, trip_id=s["trip"]))["commissions"]
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        await commissions.get(ctx, uuid.UUID(c["commission_id"]))
    sql = (
        "SELECT count(*) FROM crm.audit_event WHERE action = 'view' AND object_kind = 'commission' "
        "AND object_id = $1 AND actor_ref = $2"
    )
    owner = await _scalar(app_pool, s["tenant"], "SELECT member_id FROM crm.member WHERE role = 'owner' LIMIT 1")
    assert await _scalar(app_pool, s["tenant"], sql, c["commission_id"], f"member:{owner}") == 1


async def test_the_commission_sweep_skips_a_trip_whose_cancellation_is_pending(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking(confirm_line_status="confirmed")
    await _aa_booking(app_pool, s, fake)
    await _agency_line(app_pool, s, _keys())
    await _cancel(app_pool, s, dry_run=False)  # pending; the expected commission is cancelled, the line stays
    async with system_tx(app_pool, s["tenant"], actor="job:crm_stage_date_rule") as ctx:
        assert await commissions.sweep(ctx) == 0  # nothing to create while the cancel waits: never picked
    assert await _scalar(app_pool, s["tenant"], "SELECT count(*) FROM crm.commission WHERE trip_id = $1 "
                         "AND status <> 'cancelled'", s["trip"]) == 0  # fmt: skip


async def test_the_aa_fee_fx_day_is_the_agency_day_of_the_cancellation(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    fake = FakeBooking(confirm_line_status="confirmed")
    booked = await _aa_booking(app_pool, s, fake)
    at = "2026-11-01T20:00:00+00:00"  # 1 Nov in UTC, 2 Nov in the agency's day (Asia/Ho_Chi_Minh by default)
    day_sql = (
        "SELECT ($1::timestamptz AT TIME ZONE coalesce((SELECT timezone FROM crm.tenant_settings), "
        "'Asia/Ho_Chi_Minh'))::date"
    )
    local = await _scalar(app_pool, s["tenant"], day_sql, dt.datetime.fromisoformat(at))
    assert local != dt.date(2026, 11, 1)
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:  # a EUR rate before both days
        rate_sql = (
            "INSERT INTO crm.fx_rate_override (rate_date, base, quote, rate, set_by) "
            "VALUES (DATE '2026-10-30', 'EUR', 'USD', 1.1, $1)"
        )
        await ctx.conn.execute(rate_sql, ctx.member_id)
    # Booking answers with a stored cancellation in another currency than the quote (no quote FX lock to reuse).
    fake.cancellations["b-h1"] = {
        "booking_id": "b-h1", "dry_run": False, "fee": {"amount": "100.00", "currency": "EUR"},
        "refundable_b2b_amount": {"amount": "0.00", "currency": "EUR"},
        "fee_basis": {"policy_id": "pol-1", "days_before_departure": 40, "fee_pct": "25"}, "cancelled_at": at,
    }  # fmt: skip
    await _cancel(app_pool, s, dry_run=False)
    await _sync(app_pool, fake, s["tenant"])
    assert await _stage(app_pool, s) == "cancelled"
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        fee = await ctx.conn.fetchrow("SELECT amount, currency, fx_rate_date FROM crm.v_booking_line_money_current "
                                      "WHERE booking_line_id = $1 AND kind = 'cancellation_cost'", booked)  # fmt: skip
    assert (fee["amount"], fee["currency"], fee["fx_rate_date"]) == (Decimal("100.00"), "EUR", local)


async def test_a_trips_commission_totals_are_its_own(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    keys = _keys()
    await _agency_line(app_pool, s, keys, commission="40")
    await _agency_line(app_pool, s, keys, commission="25", trip=await _other_trip(app_pool, s))
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        mine = await commissions.listing(ctx, status=None, trip_id=s["trip"])
        every = await commissions.listing(ctx, status=None, trip_id=None)
    assert len(mine["commissions"]) == 1
    assert (mine["commission_totals"]["open_count"], mine["commission_totals"]["open_base"]) == (1, "40.00")
    assert (every["commission_totals"]["open_count"], every["commission_totals"]["open_base"]) == (2, "65.00")


async def test_a_kept_invoice_is_voided_once_its_refund_executed_and_the_supplier_task_lists_suppliers(
    app_pool: asyncpg.Pool,
) -> None:
    s = await _setup(app_pool)
    keys = _keys()
    await _agency_line(app_pool, s, keys)
    await _agency_line(app_pool, s, keys, commission="0", supplier_name="Delta Boats")
    paid = await _paid_invoice(app_pool, s, keys, "300")  # 300 of 500: the invoice stays open
    invoice_id = uuid.UUID(paid["invoice"]["invoice_id"])
    out = await _cancel(app_pool, s, dry_run=False)  # no AA booking: cancelled at once
    assert out["stage"] == "cancelled"
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        view = await pipeline.trip_detail(ctx, Vault(keys, ctx), s["trip"])
        refund = await ctx.conn.fetchval("SELECT refund_id FROM crm.refund_request WHERE trip_id = $1", s["trip"])
        assert await ctx.conn.fetchval("SELECT status FROM crm.invoice WHERE invoice_id = $1", invoice_id) == "open"
        await payments.decide_refund(ctx, refund, approve=True, note=None)
    (task,) = [k for k in view["tasks"] if k["kind"] == "cancel_with_suppliers"]
    assert [(c["supplier_name"], [x["title"] for x in c["lines"]]) for c in task["checklist"]] == [
        ("Delta Boats", ["Lodge 2 nights"]),
        ("Riverside Lodge", ["Lodge 2 nights"]),
    ]
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await payments.execute_refund(ctx, refund, method="bank_transfer", refunded_on=None)
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        invoice = await ctx.conn.fetchrow("SELECT status, void_reason FROM crm.invoice WHERE invoice_id = $1",
                                          invoice_id)  # fmt: skip
        commitment = await ctx.conn.fetchval("SELECT line_status FROM crm.trip_commitment WHERE kind = 'invoice' "
                                             "AND source_ref = $1", f"invoice:{invoice_id}")  # fmt: skip
    assert (invoice["status"], invoice["void_reason"]) == ("void", "Trip cancelled")
    assert commitment == "void"


LATE_FEE = [{"description": "Late fee", "quantity": "1", "unit_amount": "20"}]


async def test_a_cancelled_trip_voids_its_due_balance_and_bills_the_client_no_more(app_pool: asyncpg.Pool) -> None:
    """S262 live finding (C12 §2.7 items 3-4): after the cancel the balance row is void (not left Due with "Mark paid"
    and "Create invoice"), its issued invoice holding no money is void, and the trip takes no new invoice (409
    trip_cancelled) and no new portal link."""
    s = await _lc_setup(app_pool)
    await _lc_accept(app_pool, s, await _lc_proposal(app_pool, s))
    rows = (await _lc_schedule(app_pool, s))["schedule"]
    deposit = uuid.UUID(next(r["schedule_id"] for r in rows if r["kind"] == "deposit"))
    balance = uuid.UUID(next(r["schedule_id"] for r in rows if r["kind"] == "balance"))
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        paid = await proposals.mark_paid(ctx, Vault(s["keys"], ctx), deposit, received_on=None, method="cash")
        assert paid["trip_moved"]["stage"] == "booked"
        bill = await invoices.create_from_schedule(ctx, s["trip"], balance)
        await invoices.issue(ctx, uuid.UUID(bill["invoice_id"]))
    preview = await _cancel(app_pool, s, dry_run=True)
    assert _effects(preview)["schedule_rows_voided"] == 1 and _effects(preview)["invoices_voided"] == 1
    out = await _cancel(app_pool, s, dry_run=False)
    assert out["stage"] == "cancelled"  # no AA booking: cancelled at once
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        statuses = {r["kind"]: r["status"] for r in (await proposals.trip_overview(ctx, Vault(s["keys"], ctx),
                                                                                   s["trip"]))["schedule"]}  # fmt: skip
        invoice = await ctx.conn.fetchval("SELECT status FROM crm.invoice WHERE invoice_id = $1",
                                          uuid.UUID(bill["invoice_id"]))  # fmt: skip
        audited = await ctx.conn.fetchval(
            "SELECT count(*) FROM crm.audit_event WHERE action = 'payment_schedule_void' AND object_id = $1",
            str(balance),
        )
    assert statuses == {"deposit": "paid", "balance": "void"} and invoice == "void" and audited == 1
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        with pytest.raises(Conflict) as err:
            await invoices.create_typed(ctx, s["trip"], kind="adjustment", currency="USD", lines=LATE_FEE)
        assert err.value.code == "trip_cancelled"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        with pytest.raises(Conflict) as err:
            await proposals.mark_paid(ctx, Vault(s["keys"], ctx), balance, received_on=None, method="cash")
        assert err.value.code == "schedule_not_due"  # the cancel voided it
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        with pytest.raises(Conflict) as err:
            await access.invite(ctx, s["trip"], s["lead"])
        assert err.value.code == "trip_closed"


async def test_a_trip_whose_cancellation_waits_for_aa_takes_no_new_invoice(app_pool: asyncpg.Pool) -> None:
    """S262: the pending window too (the trip keeps its stage until AA confirms): 409 trip_cancelled."""
    s = await _setup(app_pool)
    fake = FakeBooking(confirm_line_status="confirmed")
    await _aa_booking(app_pool, s, fake)
    out = await _cancel(app_pool, s, dry_run=False)
    assert out["cancellation"]["status"] == "pending" and await _stage(app_pool, s) == "booked"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        with pytest.raises(Conflict) as err:
            await invoices.create_typed(ctx, s["trip"], kind="adjustment", currency="USD", lines=LATE_FEE)
        assert err.value.code == "trip_cancelled"


async def test_a_trip_whose_cancellation_waits_for_aa_is_closed_for_planning(app_pool: asyncpg.Pool) -> None:
    """S262 round 3 (C12 §2.7): no new AA quote, agency line, dates or document for the traveller; no delete until
    AA confirms; an internal document still goes."""
    s = await _setup(app_pool)
    keys = _keys()
    fake = FakeBooking(confirm_line_status="confirmed")
    await _aa_booking(app_pool, s, fake)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        own = await suppliers.create_supplier(
            ctx, Vault(keys, ctx), {"name": "Hillside Inn", "category": "accommodation"}
        )
    out = await _cancel(app_pool, s, dry_run=False)
    assert out["cancellation"]["status"] == "pending"
    line = {"agency_supplier_id": uuid.UUID(own["agency_supplier_id"]), "title": "Inn 1 night"}
    refusals = {
        "aa_quote": lambda ctx: aa_lines.request_line(ctx, s["trip"], _body()),
        "agency_line": lambda ctx: lines.create_line(ctx, s["trip"], line),
        "dates": lambda ctx: pipeline.update_trip(ctx, s["trip"], {"end_date": dt.date(2026, 12, 9)}),
        "shared_doc": lambda ctx: documents.upload(
            ctx,
            Vault(keys, ctx),
            MemoryDocumentStore(),
            traveler_id=None,
            trip_id=s["trip"],
            kind="other",
            filename="Voucher.pdf",
            content_type="application/pdf",
            content=PDF,
            visible_to_travelers=True,
        ),  # fmt: skip
    }
    for name, call in refusals.items():
        with pytest.raises(Conflict) as err:
            async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
                await call(ctx)
        assert err.value.code == "trip_cancelled", name
    with pytest.raises(Conflict) as err:
        async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
            await trash.move_to_trash(ctx, "trip", s["trip"])
    assert err.value.code == "cancellation_pending"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        kept = await documents.upload(ctx, Vault(keys, ctx), MemoryDocumentStore(), traveler_id=None,
                                      trip_id=s["trip"], kind="other", filename="Supplier note.pdf",
                                      content_type="application/pdf", content=PDF)  # fmt: skip
        n_lines = await ctx.conn.fetchval("SELECT count(*) FROM crm.booking_line WHERE trip_id = $1", s["trip"])
    assert kept["visible_to_travelers"] is False and n_lines == 1  # only the AA booking


async def test_undo_claim_forgets_the_claim(app_pool: asyncpg.Pool) -> None:
    """S262 round 3 (AA-957 live D7-02): an expected row never carries the reference of a withdrawn claim."""
    s = await _setup(app_pool)
    await _agency_line(app_pool, s, _keys(), commission="30")
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        [row] = (await commissions.listing(ctx, status=None, trip_id=s["trip"]))["commissions"]
        claimed = await commissions.claim(ctx, uuid.UUID(row["commission_id"]), "SUP-INV-31")
        assert claimed["claim_ref"] == "SUP-INV-31" and claimed["claimed_at"] is not None
        undone = await commissions.unclaim(ctx, uuid.UUID(row["commission_id"]))
    assert undone["status"] == "expected" and undone["claim_ref"] is None and undone["claimed_at"] is None


async def _cancelled_before_the_fix(pool: asyncpg.Pool, *, issue: bool) -> tuple[dict[str, Any], uuid.UUID, uuid.UUID]:
    """A booked trip with a due balance row billed by a draft (or issued) invoice, moved to cancelled without the run's
    schedule step: the shape of the Dev trips cancelled before S262."""
    s = await _lc_setup(pool)
    await _lc_accept(pool, s, await _lc_proposal(pool, s))
    rows = (await _lc_schedule(pool, s))["schedule"]
    deposit = uuid.UUID(next(r["schedule_id"] for r in rows if r["kind"] == "deposit"))
    balance = uuid.UUID(next(r["schedule_id"] for r in rows if r["kind"] == "balance"))
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        await proposals.mark_paid(ctx, Vault(s["keys"], ctx), deposit, received_on=None, method="cash")
        bill = uuid.UUID((await invoices.create_from_schedule(ctx, s["trip"], balance))["invoice_id"])
        if issue:
            await invoices.issue(ctx, bill)
        await cancellation.move_to_cancelled(ctx, s["trip"], "booked", "client_request")
    return s, balance, bill


async def _schedule_and_invoice(pool: asyncpg.Pool, s: dict[str, Any], balance: uuid.UUID, bill: uuid.UUID) -> tuple:
    async with tenant_tx(pool, s["who"]["owner"], s["tenant"]) as ctx:
        row = await ctx.conn.fetchval("SELECT status FROM crm.payment_schedule WHERE schedule_id = $1", balance)
        invoice = await ctx.conn.fetchval("SELECT status FROM crm.invoice WHERE invoice_id = $1", bill)
        audited = await ctx.conn.fetchval(
            "SELECT count(*) FROM crm.audit_event WHERE action = 'payment_schedule_void' AND object_id = $1",
            str(balance),
        )
    return row, invoice, audited


async def _reported_balance_due(pool: asyncpg.Pool, s: dict[str, Any]) -> int:
    """The balance rows the Reports receivables count as not invoiced yet (app.reports.queries.SCHEDULE_DUE_SQL)."""
    async with tenant_tx(pool, s["who"]["owner"], s["tenant"]) as ctx:
        rows = await ctx.conn.fetch(report_queries.SCHEDULE_DUE_SQL, dt.date(2100, 1, 1), None)
    return sum(r["n"] for r in rows if r["kind"] == "balance")


async def test_the_date_rule_voids_the_due_balance_of_a_trip_cancelled_before_the_fix(app_pool: asyncpg.Pool) -> None:
    """S262 fix pass (M1): the hourly date rule voids the due balance row of a cancelled trip and the draft that bills
    it (same audit as the cancel run), once: a second run finds nothing."""
    s, balance, bill = await _cancelled_before_the_fix(app_pool, issue=False)
    assert await _stage(app_pool, s) == "cancelled"
    assert await _schedule_and_invoice(app_pool, s, balance, bill) == ("due", "draft", 0)
    assert await _reported_balance_due(app_pool, s) == 1  # the Reports receivables counted it
    async with system_tx(app_pool, s["tenant"], actor="job:crm_stage_date_rule") as ctx:
        assert (await date_rule(ctx))["cancelled_schedule_rows_voided"] == 1
    assert await _schedule_and_invoice(app_pool, s, balance, bill) == ("void", "void", 1)
    assert await _reported_balance_due(app_pool, s) == 0  # a void row is no receivable
    async with system_tx(app_pool, s["tenant"], actor="job:crm_stage_date_rule") as ctx:
        assert "cancelled_schedule_rows_voided" not in await date_rule(ctx)
    assert await _schedule_and_invoice(app_pool, s, balance, bill) == ("void", "void", 1)


async def test_the_date_rule_keeps_a_balance_row_a_kept_invoice_bills(app_pool: asyncpg.Pool) -> None:
    """S262 fix pass (M1): an issued invoice bills the row (the cancel would have kept it had it held money or been
    claimed): the row settles through that invoice, the date rule leaves both."""
    s, balance, bill = await _cancelled_before_the_fix(app_pool, issue=True)
    async with system_tx(app_pool, s["tenant"], actor="job:crm_stage_date_rule") as ctx:
        assert "cancelled_schedule_rows_voided" not in await date_rule(ctx)
    assert await _schedule_and_invoice(app_pool, s, balance, bill) == ("due", "open", 0)
