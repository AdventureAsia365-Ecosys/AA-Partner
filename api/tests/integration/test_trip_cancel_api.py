"""AA-957 (card CRM-27) end to end on PostgreSQL with the RDS role shape, as crm_app through ``tenant_tx()`` and the
Booking sync, against the in-memory Booking (``tests/aa_fakes.py``).

- **Done when**: a cancelled synthetic trip leaves no live hold, no live client link and no expected commission; the
  dry run lists every effect with Booking's fee; the run voids the unpaid invoice, proposes the refund the member set
  (a partial refund keeps the rest on the ledger, card E), cancels the expected commission, opens the supplier task,
  and once Booking answers the AA booking is cancelled with its fee as a ``cancellation_cost`` (card E).
- **R**: cancel while Booking is down -> "cancellation pending", retried, never ``cancelled`` early; the trip moves
  nowhere else meanwhile (409 ``cancellation_pending``) and the date rule leaves it.
- A refused cancel (``forbidden``) opens the owner task ``aa_cancel_failed``; pressing Cancel again asks Booking again.
- Module 13: the confirm makes the expected commission row; claim, receive at the receipt day's rate, write off; the
  assistant gets 403 on every commission route; the overdue owner task once; a claimed commission blocks the delete.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app import stages
from app.booking import aa_lines
from app.cancellation import booking_fees
from app.commissions import service as commissions
from app.errors import Conflict, Forbidden
from app.invoices import service as invoices
from app.ledger import lines, suppliers
from app.payments import service as payments
from app.permissions import Action
from app.stages_job import run_agency as date_rule
from app.tenancy import system_tx, tenant_tx
from app.vault import KeyProvider, Vault
from app.vault.wrapping import LocalKeyWrapper
from tests.aa_fakes import FakeBooking, booking_client
from tests.integration.test_aa_lines_api import _body, _due_now, _scalar, _setup, _sync

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

REASON = "client_request"


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


async def _agency_line(pool: asyncpg.Pool, s: dict[str, Any], keys: KeyProvider, commission: str = "40") -> uuid.UUID:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        supplier = {"name": "Riverside Lodge", "category": "accommodation", "commission_due_days": 10}
        own = await suppliers.create_supplier(ctx, Vault(keys, ctx), supplier)
        body = {
            "agency_supplier_id": uuid.UUID(own["agency_supplier_id"]),
            "title": "Lodge 2 nights",
            "confirmation_no": "RL-7",
            "service_from": "2026-12-01",
            "service_to": "2026-12-03",
        }
        line_id = uuid.UUID((await lines.create_line(ctx, s["trip"], body))["booking_line_id"])
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
