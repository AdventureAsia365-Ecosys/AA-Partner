"""AA-952 CRM-23 slice 4 end to end on PostgreSQL with the RDS role shape: the client's pay box on the accepted
proposal page (``client_link_tx()``), the "I have paid" claim with Booking's one hold extension, the re-hold of a late
deposit through the Booking sync (``job:crm_booking_sync``), the overdue reminders, against the in-memory Booking of
``tests/aa_fakes.py`` (synthetic data only).

- Text proposal (no AA hold): the pay box lists the issued deposit with the agency's own bank details; "I have paid"
  -> payment_claimed, one urgent verify task, a bell item, a second claim changes nothing; void waits for the claim
  (409 invoice_claimed); "not received" -> open (task skipped), then void works; the deposit invoice is due on the
  schedule row's date.
- Day-by-day proposal with an AA hold: the claim extends the hold once (Booking's +72 h, Idempotency-Key per invoice):
  the invoice, the proposal's hold expiry and the task's due time follow; a later claim never extends again; the money
  then pays the invoice and the sync confirms the set.
- Money after the extended hold ran out: re-held (same net) -> paid, the row settled, the lines on the new hold, the
  confirm on the new hold; not re-held (higher net) -> paid_unfulfillable, one refund request per payment, an urgent
  task, the trip not booked; the refund requests follow the §4.6 matrix (agent above the threshold 403, owner approves,
  owner rejects).
- The expired link: no bank details, no claim (410 pay_link_expired), no card session (410); a live link without a card
  account is 409 card_not_offered.
- Another agency's link or member sees nothing of the invoice (404).
- Overdue reminders: the day after the due date, then every 3 days, at most 5; a claimed invoice is not reminded; the
  reminder task closes once nothing is left to chase (paid in full, void, the date rule's sweep).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.booking.sync import BookingSync
from app.errors import Conflict, Forbidden, Gone, NotFound
from app.invoices import service as invoices
from app.payments import claims, reminders
from app.payments import service as payments
from app.permissions import Action
from app.proposals import events
from app.proposals import public as page
from app.tenancy import system_tx, tenant_tx
from app.vault import Vault
from tests.aa_fakes import booking_client
from tests.integration.test_builder_api import _pid
from tests.integration.test_client_proposal_api import PLAIN, _raw
from tests.integration.test_invoices_api import _as
from tests.integration.test_lifecycle_api import _accept, _proposal, _setup, _token
from tests.integration.test_proposal_confirm_api import _aa_lines, _stage
from tests.integration.test_proposal_confirm_api import _accepted as _builder_accepted

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BANK = "Account 0000 4444 at the agency's bank, reference: the invoice number"


async def _text_accepted(pool: asyncpg.Pool) -> dict[str, Any]:
    s = await _setup(pool)
    sent = await _proposal(pool, s)
    await _accept(pool, s, sent)
    s["token"] = _token(sent["link_path"])
    return s


async def _deposit_invoice(pool: asyncpg.Pool, s: dict[str, Any], *, bank: bool = True) -> uuid.UUID:
    if bank:
        await _as(pool, s, "owner", lambda ctx: payments.save_manual(ctx, display_name="Bank transfer",
                                                                     instructions=BANK))  # fmt: skip
    rows = await _raw(pool, s, "SELECT schedule_id FROM crm.payment_schedule WHERE trip_id = $1 AND kind = 'deposit' "
                      "AND status = 'due'", s["trip"])  # fmt: skip
    schedule_id = rows[0]["schedule_id"]
    draft = await _as(pool, s, "agent", lambda ctx: invoices.create_from_schedule(ctx, s["trip"], schedule_id))
    invoice_id = uuid.UUID(draft["invoice_id"])
    await _as(pool, s, "agent", lambda ctx: invoices.issue(ctx, invoice_id))
    s["deposit_schedule"] = schedule_id
    return invoice_id


async def _claim(pool: asyncpg.Pool, s: dict[str, Any], invoice_id: uuid.UUID, fake: Any = None) -> dict[str, Any]:
    client = booking_client(fake) if fake is not None else None
    return await page.claim_payment(pool, s["keys"], PLAIN, client, s["token"], str(invoice_id), origin=None)


async def _pay(pool: asyncpg.Pool, s: dict[str, Any], invoice_id: uuid.UUID, amount: Any) -> dict[str, Any]:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        return await payments.record_payment(ctx, Vault(s["keys"], ctx), invoice_id, amount=Decimal(amount),
                                             method="bank_transfer", received_on=None, reference="TT 2001")  # fmt: skip


async def _task(pool: asyncpg.Pool, s: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    rows = await _raw(pool, s, "SELECT status, priority, due_at, due_anchor FROM crm.task WHERE trip_id = $1 "
                      "AND kind = $2 ORDER BY created_at", s["trip"], kind)  # fmt: skip
    return [dict(r) for r in rows]


async def _bell_names(pool: asyncpg.Pool, s: dict[str, Any]) -> list[str]:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        return [i["name"] for i in (await events.bell(ctx))["items"]]


async def _invoice(pool: asyncpg.Pool, s: dict[str, Any], invoice_id: uuid.UUID) -> dict[str, Any]:
    return await _as(pool, s, "agent", lambda ctx: invoices.get(ctx, invoice_id))


async def _sync(pool: asyncpg.Pool, s: dict[str, Any], *, now: dt.datetime | None = None) -> Counter[str]:
    counts: Counter[str] = Counter()
    sync = (
        BookingSync(booking_client(s["fake"]))
        if now is None
        else BookingSync(booking_client(s["fake"]), now=lambda: now)
    )
    await sync.run_agency(pool, s["tenant"], counts, feed=False)
    return counts


def _box(p: dict[str, Any], invoice_id: uuid.UUID) -> dict[str, Any]:
    return next(i for i in p["payments"] if i["invoice_id"] == str(invoice_id))


# ── text proposal: claim, verify task, not received, void ────────────────────────────────────────


async def test_claim_without_a_hold_verify_task_not_received_and_void(app_pool: asyncpg.Pool) -> None:
    s = await _text_accepted(app_pool)
    invoice_id = await _deposit_invoice(app_pool, s)
    # The deposit invoice is due on the schedule row's date (kept from slice 2: it bills the row as accepted).
    row = await _raw(app_pool, s, "SELECT s.due_on AS row_due, i.due_on AS invoice_due FROM crm.invoice i "
                     "JOIN crm.payment_schedule s ON s.schedule_id = i.schedule_id WHERE i.invoice_id = $1",
                     invoice_id)  # fmt: skip
    today = (await _raw(app_pool, s, "SELECT current_date AS d"))[0]["d"]
    assert row[0]["invoice_due"] == max(row[0]["row_due"], today)

    p = await page.read(app_pool, s["keys"], s["token"])
    box = _box(p, invoice_id)
    assert box["pay_window"] == {"state": "open", "until": None} and box["can_claim"]
    assert box["pay_by"] == {"name": "Bank transfer", "instructions": BANK} and box["kind"] == "deposit"
    assert box["total_amount"] == "300.00" and box["balance_amount"] == "300.00"
    assert "trip_id" not in box and "payments" not in box  # the client sees what to pay, nothing internal

    out = await _claim(app_pool, s, invoice_id)
    assert out["outcome"] == "claimed" and out["extension"] is None
    assert _box(out["page"], invoice_id)["pay_window"]["state"] == "claimed"
    assert _box(out["page"], invoice_id)["pay_by"] is None and not _box(out["page"], invoice_id)["can_claim"]
    view = await _invoice(app_pool, s, invoice_id)
    assert (
        view["status"] == "payment_claimed"
        and view["claimed_at"]
        and view["actions"] == ["record_payment", "reject_claim"]
    )
    tasks = await _task(app_pool, s, claims.VERIFY_TASK)
    assert [(t["status"], t["priority"], t["due_anchor"]) for t in tasks] == [("open", "urgent", "created")]
    assert "invoice.payment_claimed" in await _bell_names(app_pool, s)
    again = await _claim(app_pool, s, invoice_id)  # a second press: nothing new
    assert again["outcome"] == "claimed" and len(await _task(app_pool, s, claims.VERIFY_TASK)) == 1

    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", lambda ctx: invoices.void(ctx, invoice_id, "Wrong amount"))
    assert err.value.code == "invoice_claimed"
    with pytest.raises(Forbidden):  # the assistant cannot answer a claim
        await _as(app_pool, s, "assistant", lambda ctx: claims.reject_claim(ctx, invoice_id),
                  action=Action.PAYMENT_MARK_RECEIVED)  # fmt: skip
    await _as(app_pool, s, "agent", lambda ctx: claims.reject_claim(ctx, invoice_id))
    view = await _invoice(app_pool, s, invoice_id)
    assert view["status"] == "open" and view["actions"] == ["record_payment", "void"]
    assert [t["status"] for t in await _task(app_pool, s, claims.VERIFY_TASK)] == ["skipped"]
    with pytest.raises(Conflict) as err:
        await _as(app_pool, s, "agent", lambda ctx: claims.reject_claim(ctx, invoice_id))
    assert err.value.code == "invoice_not_claimed"
    voided = await _as(app_pool, s, "agent", lambda ctx: invoices.void(ctx, invoice_id, "Wrong amount"))
    assert voided["status"] == "void"
    p = await page.read(app_pool, s["keys"], s["token"])
    assert p["payments"] == []  # a void invoice leaves the pay box


async def test_a_claimed_invoice_paid_in_full_closes_the_verify_task(app_pool: asyncpg.Pool) -> None:
    s = await _text_accepted(app_pool)
    invoice_id = await _deposit_invoice(app_pool, s)
    await _claim(app_pool, s, invoice_id)
    paid = await _pay(app_pool, s, invoice_id, "300")
    assert paid["invoice"]["status"] == "paid" and not paid["rehold_pending"]
    assert paid["trip_moved"]["stage"] == "booked"
    assert [t["status"] for t in await _task(app_pool, s, claims.VERIFY_TASK)] == ["done"]
    p = await page.read(app_pool, s["keys"], s["token"])
    assert _box(p, invoice_id)["pay_window"] == {"state": "paid", "until": None}
    with pytest.raises(Conflict) as err:
        await _claim(app_pool, s, invoice_id)
    assert err.value.code == "invoice_not_payable"


# ── day-by-day proposal: the claim extends the AA hold once ───────────────────────────────────────


async def test_claim_extends_the_hold_once_and_the_money_confirms_the_set(app_pool: asyncpg.Pool) -> None:
    s = await _builder_accepted(app_pool)
    fake = s["fake"]
    fake.confirm_line_status = "confirmed"
    await _sync(app_pool, s)  # the AA lines from the accepted hold
    invoice_id = await _deposit_invoice(app_pool, s)
    before = (await _raw(app_pool, s, "SELECT hold_expires_at FROM crm.proposal WHERE proposal_id = $1",
                         _pid(s["v"])))[0]["hold_expires_at"]  # fmt: skip
    p = await page.read(app_pool, s["keys"], s["token"])
    assert _box(p, invoice_id)["pay_window"] == {"state": "open", "until": before.isoformat()}

    out = await _claim(app_pool, s, invoice_id, fake)
    assert fake.extend_keys == [f"crm-invoice:{invoice_id}:extend"]
    after = before + dt.timedelta(hours=72)
    assert out["extension"] == {"code": "extended", "until": after.isoformat()}
    sql = ("SELECT i.hold_extended_at, i.hold_extend_code, p.hold_expires_at FROM crm.invoice i "
           "JOIN crm.proposal p ON p.proposal_id = i.proposal_id WHERE i.invoice_id = $1")  # fmt: skip
    row = (await _raw(app_pool, s, sql, invoice_id))[0]
    assert row["hold_extended_at"] and row["hold_extend_code"] == "extended" and row["hold_expires_at"] == after
    tasks = await _task(app_pool, s, claims.VERIFY_TASK)
    assert [(t["due_anchor"], t["due_at"]) for t in tasks] == [("hold_expiry", after)]
    assert _box(out["page"], invoice_id)["pay_window"] == {"state": "claimed", "until": after.isoformat()}
    # The sync was nudged (the feed's hold.extended refreshes the lines' expiry).
    nudges = await _raw(app_pool, s, "SELECT 1 AS ok FROM crm.job WHERE idempotency_key = $1",
                        f"crm_booking_sync:extended:{invoice_id}")  # fmt: skip
    assert len(nudges) == 1

    # Not received, then claimed again: the one extension stays used (Booking allows one per hold).
    await _as(app_pool, s, "agent", lambda ctx: claims.reject_claim(ctx, invoice_id))
    await _claim(app_pool, s, invoice_id, fake)
    assert len(fake.extend_keys) == 1

    paid = await _pay(app_pool, s, invoice_id, (await _invoice(app_pool, s, invoice_id))["total_amount"])
    assert paid["invoice"]["status"] == "paid" and not paid["rehold_pending"]
    assert paid["trip_moved"]["stage"] == "booked"
    counts = await _sync(app_pool, s)
    assert counts["confirms_queued"] == 1
    assert {(line["status"], line["bk_hold_id"]) for line in await _aa_lines(app_pool, s)} == {
        ("confirmed", s["hold_id"])
    }


async def test_a_claim_booking_cannot_answer_is_extended_by_the_sync(app_pool: asyncpg.Pool) -> None:
    s = await _builder_accepted(app_pool)
    fake = s["fake"]
    await _sync(app_pool, s)
    invoice_id = await _deposit_invoice(app_pool, s)
    fake.down = True
    out = await _claim(app_pool, s, invoice_id, fake)
    assert out["extension"] == {"code": "booking_unavailable", "until": None}
    assert (await _invoice(app_pool, s, invoice_id))["hold_extend_code"] == "booking_unavailable"
    fake.down = False
    counts = await _sync(app_pool, s)
    assert counts["extend_extended"] == 1 and fake.extend_keys == [f"crm-invoice:{invoice_id}:extend"]
    view = await _invoice(app_pool, s, invoice_id)
    assert view["hold_extend_code"] == "extended" and view["hold_extended_at"]
    assert (await _sync(app_pool, s))["extend_extended"] == 0  # once


# ── money after the extended hold ran out ─────────────────────────────────────────────────────────


async def _expire_hold(pool: asyncpg.Pool, s: dict[str, Any]) -> None:
    """What the hold looks like once its (extended) expiry passed, at Booking and in the CRM."""
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)).replace(microsecond=0)
    s["fake"].holds[s["hold_id"]]["expires_at"] = past.isoformat()
    await _raw(pool, s, "UPDATE crm.proposal SET hold_expires_at = $2 WHERE proposal_id = $1", _pid(s["v"]), past)
    async with pool.acquire() as conn, conn.transaction():  # AA lines are written by a job actor only (crm-014)
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        await conn.execute("UPDATE crm.booking_line SET hold_expires_at = $2 WHERE trip_id = $1 "
                           "AND supplier_kind = 'aa'", s["trip"], past)  # fmt: skip


async def test_money_after_the_hold_ran_out_is_reheld_and_confirmed(app_pool: asyncpg.Pool) -> None:
    s = await _builder_accepted(app_pool)
    fake = s["fake"]
    fake.confirm_line_status = "confirmed"
    await _sync(app_pool, s)
    invoice_id = await _deposit_invoice(app_pool, s)
    await _claim(app_pool, s, invoice_id, fake)
    await _expire_hold(app_pool, s)
    total = (await _invoice(app_pool, s, invoice_id))["total_amount"]

    paid = await _pay(app_pool, s, invoice_id, total)
    assert paid["rehold_pending"] and paid["trip_moved"] is None
    view = paid["invoice"]
    assert view["status"] == "payment_claimed" and view["actions"] == [] and view["rehold"]["code"] is None
    assert view["pay_window"]["state"] == "settling" and await _stage(app_pool, s) == "proposal"
    assert [t["status"] for t in await _task(app_pool, s, claims.VERIFY_TASK)] == ["done"]

    counts = await _sync(app_pool, s)
    assert counts["rehold_paid"] == 1
    view = await _invoice(app_pool, s, invoice_id)
    assert view["status"] == "paid" and view["rehold"]["code"] == "reheld" and view["paid_at"]
    new_hold = (await _raw(app_pool, s, "SELECT hold_id, hold_count FROM crm.proposal WHERE proposal_id = $1",
                           _pid(s["v"])))[0]  # fmt: skip
    assert new_hold["hold_id"] != s["hold_id"] and fake.holds[new_hold["hold_id"]]["status"] == "active"
    assert fake.hold_keys.get(f"crm-invoice:{invoice_id}:rehold") == new_hold["hold_id"]
    lines = await _aa_lines(app_pool, s)
    assert {(line["status"], line["aa_sub_status"], line["bk_hold_id"]) for line in lines} == {
        ("planning", "held", new_hold["hold_id"])
    }
    assert await _stage(app_pool, s) == "booked"
    schedule = await _raw(app_pool, s, "SELECT status FROM crm.payment_schedule WHERE schedule_id = $1",
                          s["deposit_schedule"])  # fmt: skip
    assert schedule[0]["status"] == "paid"
    assert "invoice.reheld" in await _bell_names(app_pool, s)

    # The next run confirms the new set (the confirm query follows the proposal's current hold).
    counts = await _sync(app_pool, s)
    assert counts["confirms_queued"] == 1 and fake.bookings.get(new_hold["hold_id"]) is not None
    assert {line["status"] for line in await _aa_lines(app_pool, s)} == {"confirmed"}


async def test_money_after_the_hold_ran_out_not_reheld_goes_back_through_refund_requests(
    app_pool: asyncpg.Pool,
) -> None:
    s = await _builder_accepted(app_pool)
    fake = s["fake"]
    await _sync(app_pool, s)
    invoice_id = await _deposit_invoice(app_pool, s)
    await _expire_hold(app_pool, s)
    total = Decimal((await _invoice(app_pool, s, invoice_id))["total_amount"])
    # The client pays in two transfers (the second after the hold ran out); AA now quotes a higher net.
    await _pay(app_pool, s, invoice_id, "100")
    fake.b2b_price = "310.00"
    paid = await _pay(app_pool, s, invoice_id, total - 100)
    assert paid["rehold_pending"]

    counts = await _sync(app_pool, s)
    assert counts["rehold_paid_unfulfillable"] == 1
    view = await _invoice(app_pool, s, invoice_id)
    assert view["status"] == "paid_unfulfillable" and view["rehold"]["code"] == "price_higher" and view["paid_at"]
    asks = view["refund_requests"]
    assert sorted(Decimal(r["amount"]) for r in asks) == sorted([Decimal(100), total - 100])
    assert {r["status"] for r in asks} == {"proposed"} and {r["requested_by"] for r in asks} == {"job:crm_booking_sync"}
    assert await _stage(app_pool, s) == "proposal"  # nothing settled: the trip is not booked
    schedule = await _raw(app_pool, s, "SELECT status FROM crm.payment_schedule WHERE schedule_id = $1",
                          s["deposit_schedule"])  # fmt: skip
    assert schedule[0]["status"] == "due"
    assert [t["priority"] for t in await _task(app_pool, s, "payment_unfulfillable")] == ["urgent"]
    assert "invoice.unfulfillable" in await _bell_names(app_pool, s)
    assert (await _sync(app_pool, s))["rehold_paid_unfulfillable"] == 0  # once

    # The §4.6 matrix on the requests the sync made: the threshold below both, the agent cannot approve (403); the
    # owner approves one and rejects the other.
    await _raw(app_pool, s, "INSERT INTO crm.tenant_settings (refund_owner_threshold_base) VALUES (50) "
               "ON CONFLICT (tenant_id) DO UPDATE SET refund_owner_threshold_base = 50")  # fmt: skip
    big = next(r for r in asks if Decimal(r["amount"]) == total - 100)
    small = next(r for r in asks if Decimal(r["amount"]) == Decimal(100))
    with pytest.raises(Forbidden):
        await _as(app_pool, s, "agent", lambda ctx: payments.decide_refund(ctx, uuid.UUID(big["refund_id"]),
                                                                          approve=True, note=None))  # fmt: skip
    approved = await _as(app_pool, s, "owner", lambda ctx: payments.decide_refund(
        ctx, uuid.UUID(big["refund_id"]), approve=True, note="Return it"))  # fmt: skip
    rejected = await _as(app_pool, s, "owner", lambda ctx: payments.decide_refund(
        ctx, uuid.UUID(small["refund_id"]), approve=False, note="Kept for the new plan"))  # fmt: skip
    assert (approved["status"], rejected["status"]) == ("approved", "rejected")
    done = await _as(app_pool, s, "agent", lambda ctx: payments.execute_refund(
        ctx, uuid.UUID(big["refund_id"]), method="bank_transfer", refunded_on=None, reference="RF 9"))  # fmt: skip
    assert done["status"] == "executed"


async def test_a_sold_out_rehold_and_a_rehold_limit(app_pool: asyncpg.Pool) -> None:
    s = await _builder_accepted(app_pool)
    fake = s["fake"]
    await _sync(app_pool, s)
    invoice_id = await _deposit_invoice(app_pool, s)
    await _expire_hold(app_pool, s)
    total = (await _invoice(app_pool, s, invoice_id))["total_amount"]
    await _pay(app_pool, s, invoice_id, total)
    fake.hold_refusal = "rehold_limit"  # Booking's 3 re-holds a day: the invoice waits for the next UTC day
    counts = await _sync(app_pool, s)
    assert counts["rehold_wait"] == 1
    view = await _invoice(app_pool, s, invoice_id)
    assert view["status"] == "open" and view["rehold"]["code"] == "rehold_limit"
    assert (await _sync(app_pool, s))["rehold_wait"] == 0  # not asked again the same UTC day
    # The next UTC day the quote it wrote down is presented again; the seats are gone now.
    fake.hold_refusal = "sold_out"
    tomorrow = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    assert (await _sync(app_pool, s, now=tomorrow))["rehold_paid_unfulfillable"] == 1
    assert (await _invoice(app_pool, s, invoice_id))["rehold"]["code"] == "sold_out"


# ── the expired link, card sessions, isolation ────────────────────────────────────────────────────


async def test_the_expired_link_shows_no_details_takes_no_claim_and_starts_no_session(app_pool: asyncpg.Pool) -> None:
    s = await _builder_accepted(app_pool)
    await _sync(app_pool, s)
    invoice_id = await _deposit_invoice(app_pool, s)
    with pytest.raises(Conflict) as err:  # a live window: no card account of the agency's own
        await page.card_payment(app_pool, PLAIN, s["token"], str(invoice_id), origin=None)
    assert err.value.code == "card_not_offered"
    await _expire_hold(app_pool, s)
    box = _box(await page.read(app_pool, s["keys"], s["token"]), invoice_id)
    assert box["pay_window"]["state"] == "expired" and box["pay_by"] is None and not box["can_claim"]
    with pytest.raises(Gone) as gone:
        await _claim(app_pool, s, invoice_id, s["fake"])
    assert gone.value.code == "pay_link_expired" and s["fake"].extend_keys == []
    with pytest.raises(Gone) as gone:
        await page.card_payment(app_pool, PLAIN, s["token"], str(invoice_id), origin=None)
    assert gone.value.code == "pay_link_expired"
    assert (await _invoice(app_pool, s, invoice_id))["status"] == "open"
    denied = await _raw(app_pool, s, "SELECT action, reason FROM crm.audit_event WHERE outcome = 'denied' "
                        "AND object_id = $1 ORDER BY at", str(invoice_id))  # fmt: skip
    assert [(d["action"], d["reason"]) for d in denied] == [
        ("invoice_claim", "pay_link_expired"),
        ("invoice_card_session", "pay_link_expired"),
    ]


async def test_another_agency_sees_nothing_of_the_invoice(app_pool: asyncpg.Pool) -> None:
    s = await _text_accepted(app_pool)
    other = await _text_accepted(app_pool)
    invoice_id = await _deposit_invoice(app_pool, s)
    with pytest.raises(NotFound):  # another agency's client link
        await _claim(app_pool, other, invoice_id)
    with pytest.raises(NotFound):  # another agency's member (RLS)
        await _as(app_pool, other, "agent", lambda ctx: claims.reject_claim(ctx, invoice_id))
    with pytest.raises(NotFound):
        await page.claim_payment(app_pool, s["keys"], PLAIN, None, s["token"], "not-a-uuid", origin=None)
    assert (await _invoice(app_pool, s, invoice_id))["status"] == "open"
    rows = await _raw(app_pool, other, "SELECT count(*) AS n FROM crm.invoice WHERE invoice_id = $1", invoice_id)
    assert rows[0]["n"] == 0


# ── overdue reminders ─────────────────────────────────────────────────────────────────────────────


async def _remind(pool: asyncpg.Pool, s: dict[str, Any], today: dt.date) -> int:
    async with system_tx(pool, s["tenant"], actor="job:crm_stage_date_rule") as ctx:
        return await reminders.overdue(ctx, today)


async def test_overdue_reminders_agent_first(app_pool: asyncpg.Pool) -> None:
    s = await _text_accepted(app_pool)
    invoice_id = await _deposit_invoice(app_pool, s)
    due = dt.date.fromisoformat((await _invoice(app_pool, s, invoice_id))["due_on"])
    assert await _remind(app_pool, s, due) == 0  # not before the day after
    assert await _remind(app_pool, s, due + dt.timedelta(days=1)) == 1
    assert await _remind(app_pool, s, due + dt.timedelta(days=1)) == 0  # every 3 days
    assert [t["status"] for t in await _task(app_pool, s, reminders.TASK)] == ["open"]
    assert "invoice.overdue" in await _bell_names(app_pool, s)
    for _ in range(6):
        await _raw(app_pool, s, "UPDATE crm.invoice SET last_reminded_at = now() - interval '4 days' "
                   "WHERE invoice_id = $1", invoice_id)  # fmt: skip
        await _remind(app_pool, s, due + dt.timedelta(days=30))
    count = await _raw(app_pool, s, "SELECT reminder_count FROM crm.invoice WHERE invoice_id = $1", invoice_id)
    assert count[0]["reminder_count"] == reminders.MAX_REMINDERS
    # A claimed invoice is not chased: the verify task runs instead.
    other = await _text_accepted(app_pool)
    claimed = await _deposit_invoice(app_pool, other)
    await _claim(app_pool, other, claimed)
    assert await _remind(app_pool, other, due + dt.timedelta(days=5)) == 0


async def _reminder_tasks(pool: asyncpg.Pool, s: dict[str, Any]) -> list[tuple[str, str | None]]:
    rows = await _raw(pool, s, "SELECT status, done_by FROM crm.task WHERE trip_id = $1 AND kind = $2 "
                      "ORDER BY created_at", s["trip"], reminders.TASK)  # fmt: skip
    return [(r["status"], r["done_by"]) for r in rows]


async def test_the_reminder_task_closes_once_nothing_is_left_to_chase(app_pool: asyncpg.Pool) -> None:
    s = await _text_accepted(app_pool)
    invoice_id = await _deposit_invoice(app_pool, s)
    due = dt.date.fromisoformat((await _invoice(app_pool, s, invoice_id))["due_on"])
    assert await _remind(app_pool, s, due + dt.timedelta(days=1)) == 1
    assert await _reminder_tasks(app_pool, s) == [("open", None)]
    await _pay(app_pool, s, invoice_id, "100")  # part of it: still to chase
    assert await _reminder_tasks(app_pool, s) == [("open", None)]
    paid = await _pay(app_pool, s, invoice_id, "200")
    assert paid["invoice"]["status"] == "paid"
    member = f"member:{s['members']['agent']}"
    assert await _reminder_tasks(app_pool, s) == [("done", member)]  # the owner no longer chases a paid client
    assert await _remind(app_pool, s, due + dt.timedelta(days=10)) == 0
    assert await _reminder_tasks(app_pool, s) == [("done", member)]

    # A void closes it too.
    other = await _text_accepted(app_pool)
    voided = await _deposit_invoice(app_pool, other)
    other_due = dt.date.fromisoformat((await _invoice(app_pool, other, voided))["due_on"])
    assert await _remind(app_pool, other, other_due + dt.timedelta(days=1)) == 1
    await _as(app_pool, other, "agent", lambda ctx: invoices.void(ctx, voided, "Wrong amount"))
    assert [t[0] for t in await _reminder_tasks(app_pool, other)] == ["done"]

    # The date rule's sweep: a reminder task whose trip has nothing overdue left is closed, whatever settled it; one
    # with an invoice still overdue stays open.
    third = await _text_accepted(app_pool)
    pending = await _deposit_invoice(app_pool, third)
    third_due = dt.date.fromisoformat((await _invoice(app_pool, third, pending))["due_on"])
    await _raw(app_pool, third, "INSERT INTO crm.task (trip_id, kind, priority, due_anchor, due_at) "
               "VALUES ($1, $2, 'normal', 'created', now())", third["trip"], reminders.TASK)  # fmt: skip
    async with system_tx(app_pool, third["tenant"], actor="job:crm_stage_date_rule") as ctx:
        assert await reminders.close_settled(ctx, third_due + dt.timedelta(days=1)) == 0  # overdue then: kept
    async with system_tx(app_pool, third["tenant"], actor="job:crm_stage_date_rule") as ctx:
        assert await reminders.close_settled(ctx, third_due) == 1  # not overdue yet: nothing to chase
    assert await _reminder_tasks(app_pool, third) == [("done", "job:crm_stage_date_rule")]
