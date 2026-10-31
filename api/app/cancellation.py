"""Cancel a trip in one action (AA-957, card CRM-27; design v3 C12, §2.7 "Cancel trip", §3.5 "Cancellation (C12)", lane
§3.4). Called by :func:`app.stages.cancel` (the route ``POST /trips/{id}/cancel?dry_run=``) and, for the end of a
pending cancellation, by the Booking sync (:mod:`app.booking.sync`). Decisions: ``docs/implementation-notes/AA-957.md``.

The dry run lists every effect and changes nothing; the run applies them in the member's transaction:

1. **AA lines**: holds are released through Booking (:func:`app.booking.aa_lines.close_trip`); every AA booking
   (``bk_booking_id``, not cancelled) gets one ``cancel`` request in the outage queue (``crm.aa_request``), sent by
   ``crm_booking_sync`` with ``POST /bookings/{id}/cancel``. Booking's fee becomes a ``cancellation_cost`` on the
   line. The route adds Booking's dry-run fee per booking to the preview (:func:`booking_fees`, outside any
   transaction).
2. **Agency suppliers**: the confirmed agency lines are listed and one owner task ``cancel_with_suppliers`` opens (the
   CRM never calls an agency's supplier); the date rule closes it once no confirmed agency line is left.
3. **Invoices**: drafts and issued invoices holding no money are voided; one the client says they paid is kept (its
   ``verify_transfer`` task stays open); one holding money is kept and its payments get refund requests. The due
   balance rows of the payment schedule are voided with them (S262 live finding), except a row a kept invoice bills
   (it settles through that invoice). From then on the trip takes no new invoice, "Mark paid" or client link
   (:func:`ensure_not_cancelled`, 409 ``trip_cancelled``).
4. **Refunds**: one ``proposed`` request per payment with something left to ask back, for the amount the member set
   (default everything left; lower keeps the non-refundable part; 0 none), the reason naming the booking terms the
   client accepted (§4.6 approval as usual). A payment without an invoice gets the owner task ``cancel_refund_review``.
5. **Commission**: expected commission is cancelled (``trip_cancelled``) unless the member keeps it (the supplier
   still pays); a claimed one stays.
6. **Ops release** withdrawn (AA-956).
7. **Workflows stop**: every queued send is cancelled (``workflows_stopped`` counts the automated ones) and the
   :data:`CANCEL_HOOKS` later modules register run here.
8. **Stage**: ``cancelled`` at once when no AA booking waits; otherwise the trip keeps its stage with a pending
   ``crm.trip_cancellation`` (card R: never "cancelled" early) until Booking cancelled every AA booking, then
   :func:`finish_if_done` moves it.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid
from app.tenancy import AnyContext, TenantContext

# Trip cancel reason (crm-012) -> booking.b2b.v1 CancelRequest.reason_code.
BOOKING_REASON = {
    "client_request": "client_cancelled",
    "client_illness": "client_cancelled",
    "payment_not_received": "client_cancelled",
    "travel_advisory": "agency_cancelled",
    "force_majeure": "agency_cancelled",
    "supplier_failure": "agency_cancelled",
    "duplicate": "agency_cancelled",
    "aa_cancelled": "departure_cancelled",
    "other": "other",
}
SUPPLIER_TASK = "cancel_with_suppliers"
REFUND_REVIEW_TASK = "cancel_refund_review"
CANCEL_FAILED_TASK = "aa_cancel_failed"
# Open tasks a cancel leaves open: the client says they paid (record the money and refund it, or "Not received").
KEEP_TASKS = ("verify_transfer",)
# Booking refusals that mean "nothing left to cancel at AA" (no fee): Booking has no such booking for the agency.
# ``invalid`` is not one: Booking also sends it for a booking that is live but not cancellable by the agency (a trip
# already in progress at AA) and for a request it could not read, so the sync closes the lines on ``invalid`` only when
# the feed already said ``booking.cancelled`` (no open line of the booking left; :data:`CANCEL_CHECK_CODES`).
CANCEL_DONE_CODES = frozenset({"not_found"})
CANCEL_CHECK_CODES = frozenset({"invalid"})
MAX_PREVIEW_FEES = 10
MAX_LIST = 50
CENT = Decimal("0.01")

CancelHook = Callable[[TenantContext, uuid.UUID, bool], Awaitable[int]]
# (name, hook): a module that runs automations for a trip registers here (Module 10); called with dry_run, returns
# how many runs it stopped (or would stop). Their sum joins workflows_stopped.
CANCEL_HOOKS: list[tuple[str, CancelHook]] = []


def register_cancel_hook(name: str, hook: CancelHook) -> None:
    if any(n == name for n, _ in CANCEL_HOOKS):
        return
    CANCEL_HOOKS.append((name, hook))


PENDING_SQL = """
SELECT trip_cancellation_id, status, reason_code, from_stage, aa_bookings, effects, created_at, finished_at
  FROM crm.trip_cancellation WHERE trip_id = $1
"""
AA_BOOKINGS_SQL = """
SELECT l.bk_booking_id AS booking_id, (array_agg(l.booking_line_id ORDER BY l.booking_line_id))[1] AS line_id,
       array_agg(l.title ORDER BY l.service_from NULLS LAST, l.title) AS titles,
       bool_or(EXISTS (SELECT 1 FROM crm.aa_request r
                        WHERE r.booking_line_id = l.booking_line_id AND r.kind = 'cancel' AND r.status = 'pending'
                          AND r.deleted_at IS NULL)) AS asked,
       (SELECT r.result_code FROM crm.aa_request r JOIN crm.booking_line x ON x.booking_line_id = r.booking_line_id
         WHERE x.trip_id = $1 AND x.bk_booking_id = l.bk_booking_id AND r.kind = 'cancel' AND r.status = 'failed'
         ORDER BY r.finished_at DESC LIMIT 1) AS failed_code
  FROM crm.booking_line l
 WHERE l.trip_id = $1 AND l.supplier_kind = 'aa' AND l.bk_booking_id IS NOT NULL AND l.status <> 'cancelled'
   AND l.deleted_at IS NULL
 GROUP BY l.bk_booking_id
 ORDER BY l.bk_booking_id
"""
ASK_CANCEL_SQL = """
INSERT INTO crm.aa_request (trip_id, booking_line_id, kind) VALUES ($1, $2, 'cancel')
ON CONFLICT (tenant_id, booking_line_id, kind) WHERE status = 'pending' AND deleted_at IS NULL DO NOTHING
RETURNING aa_request_id
"""
SUPPLIER_LINES_SQL = """
SELECT l.booking_line_id, l.title, s.agency_supplier_id, s.name AS supplier_name, l.confirmation_no
  FROM crm.booking_line l JOIN crm.agency_supplier s ON s.agency_supplier_id = l.agency_supplier_id
 WHERE l.trip_id = $1 AND l.supplier_kind = 'agency' AND l.status = 'confirmed' AND l.deleted_at IS NULL
 ORDER BY s.name, l.service_from NULLS LAST, l.title
 LIMIT $2
"""
INVOICES_SQL = """
SELECT i.invoice_id, i.number, i.kind, i.status, i.issued_at,
       (SELECT coalesce(sum(CASE WHEN p.entry_kind = 'payment' THEN p.amount ELSE -p.amount END), 0)
          FROM crm.payment p WHERE p.invoice_id = i.invoice_id) AS held,
       EXISTS (SELECT 1 FROM crm.refund_request r
                WHERE r.invoice_id = i.invoice_id AND r.status IN ('proposed', 'approved') AND r.deleted_at IS NULL)
         AS refund_open
  FROM crm.invoice i
 WHERE i.trip_id = $1 AND i.deleted_at IS NULL AND i.status IN ('draft', 'open', 'payment_claimed', 'expired', 'paid',
                                                                  'paid_unfulfillable')
 ORDER BY i.created_at
   FOR UPDATE OF i
"""
PAYMENTS_SQL = """
SELECT p.payment_id, p.invoice_id, p.amount, p.currency, p.received_on, i.number,
       p.amount - (SELECT coalesce(sum(r.amount), 0) FROM crm.refund_request r
                    WHERE r.payment_id = p.payment_id AND r.status <> 'rejected' AND r.deleted_at IS NULL) AS refundable
  FROM crm.payment p LEFT JOIN crm.invoice i ON i.invoice_id = p.invoice_id
 WHERE p.trip_id = $1 AND p.entry_kind = 'payment'
 ORDER BY p.received_on, p.created_at
"""
TERMS_SQL = """
SELECT d.version_label, a.accepted_at
  FROM crm.terms_acceptance a JOIN crm.agreement_doc d ON d.doc_id = a.doc_id
 WHERE a.trip_id = $1 AND d.kind = 'booking_terms'
 ORDER BY a.accepted_at DESC LIMIT 1
"""
COMMISSIONS_SQL = """
SELECT c.commission_id, c.status, l.title, s.name AS supplier_name, m.amount AS expected_amount,
       m.currency AS expected_currency
  FROM crm.commission c
  JOIN crm.booking_line l ON l.booking_line_id = c.booking_line_id
  JOIN crm.agency_supplier s ON s.agency_supplier_id = l.agency_supplier_id
  LEFT JOIN crm.v_booking_line_money_current m ON m.booking_line_id = c.booking_line_id
       AND m.kind = 'commission_expected'
 WHERE c.trip_id = $1 AND c.status IN ('expected', 'claimed')
 ORDER BY s.name, l.title
"""
WORKFLOW_DISPATCH_SQL = """
SELECT count(*)::int FROM crm.dispatch_queue
 WHERE trip_id = $1 AND status IN ('queued', 'held') AND origin = 'workflow' AND deleted_at IS NULL
"""
SKIP_TASKS_SQL = """
UPDATE crm.task SET status = 'skipped', done_at = now(), done_by = $2
 WHERE trip_id = $1 AND status = 'open' AND deleted_at IS NULL AND kind <> ALL($3::text[])
"""
OPEN_TASKS_SQL = """
SELECT count(*)::int FROM crm.task
 WHERE trip_id = $1 AND status = 'open' AND deleted_at IS NULL AND kind <> ALL($2::text[])
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
SELECT $1, $2, $3, t.owner_member_id, 'created', now() + interval '1 day' FROM crm.trip t WHERE t.trip_id = $1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""
VOID_SQL = """
UPDATE crm.invoice SET status = 'void', voided_at = now(), voided_by = $2, void_reason = 'Trip cancelled'
 WHERE invoice_id = $1
"""
# S262 (C12 item 3): the due balance rows of the schedule, with the live (not void) invoices that bill each one. A
# deposit row is never void (crm-016 CHECK); a booked trip has paid it anyway.
SCHEDULE_DUE_SQL = """
SELECT s.schedule_id,
       coalesce(array_agg(i.invoice_id) FILTER (WHERE i.invoice_id IS NOT NULL), '{}'::uuid[]) AS live_invoices
  FROM crm.payment_schedule s
  LEFT JOIN crm.invoice i ON i.schedule_id = s.schedule_id AND i.status <> 'void' AND i.deleted_at IS NULL
 WHERE s.trip_id = $1 AND s.status = 'due' AND s.kind = 'balance' AND s.deleted_at IS NULL
 GROUP BY s.schedule_id
 ORDER BY s.schedule_id
"""
SCHEDULE_VOID_SQL = """
UPDATE crm.payment_schedule SET status = 'void' WHERE schedule_id = $1 AND status = 'due' RETURNING schedule_id
"""
# S262 fix pass (M1): the hourly date rule's catch-up for trips cancelled before the run voided the schedule (Dev, 31
# Oct 2026): the due balance rows of a cancelled (or cancelling) trip that no kept invoice bills, i.e. no live invoice
# but a draft (the cancel voids drafts; issuing one is 409 trip_cancelled). ``drafts`` = the drafts that go with it.
SCHEDULE_SWEEP_SQL = """
SELECT s.schedule_id, s.trip_id,
       coalesce(array_agg(i.invoice_id) FILTER (WHERE i.invoice_id IS NOT NULL), '{}'::uuid[]) AS drafts
  FROM crm.payment_schedule s
  JOIN crm.trip t ON t.trip_id = s.trip_id AND t.deleted_at IS NULL
  LEFT JOIN crm.invoice i ON i.schedule_id = s.schedule_id AND i.status = 'draft' AND i.deleted_at IS NULL
 WHERE s.status = 'due' AND s.kind = 'balance' AND s.deleted_at IS NULL
   AND (t.stage = 'cancelled' OR EXISTS (SELECT 1 FROM crm.trip_cancellation x WHERE x.trip_id = t.trip_id))
   AND NOT EXISTS (SELECT 1 FROM crm.invoice k
                    WHERE k.schedule_id = s.schedule_id AND k.deleted_at IS NULL AND k.status NOT IN ('draft', 'void'))
 GROUP BY s.schedule_id, s.trip_id
 ORDER BY s.schedule_id
 LIMIT $1
"""
DRAFT_VOID_SQL = """
UPDATE crm.invoice SET status = 'void', voided_at = now(), voided_by = $2, void_reason = 'Trip cancelled'
 WHERE invoice_id = $1 AND status = 'draft'
"""
SWEEP_BATCH = 500
CLOSED_SQL = """
SELECT t.stage, EXISTS (SELECT 1 FROM crm.trip_cancellation x WHERE x.trip_id = t.trip_id) AS cancelling
  FROM crm.trip t WHERE t.trip_id = $1 AND t.deleted_at IS NULL
"""
INVOICE_COMMITMENT_SQL = """
INSERT INTO crm.trip_commitment (trip_id, kind, line_status, source_ref) VALUES ($1, 'invoice', 'void', $2)
ON CONFLICT (tenant_id, kind, source_ref) DO UPDATE SET line_status = EXCLUDED.line_status
"""
REFUND_SQL = """
INSERT INTO crm.refund_request (refund_id, trip_id, invoice_id, payment_id, amount, currency, reason)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""
CANCEL_COMMISSION_SQL = """
UPDATE crm.commission SET status = 'cancelled', close_reason = 'trip_cancelled', decided_by = $2
 WHERE trip_id = $1 AND status = 'expected' AND NOT (commission_id = ANY($3::uuid[]))
"""
KEEP_COMMISSION_SQL = "UPDATE crm.commission SET keep_on_cancel = true WHERE commission_id = ANY($1::uuid[])"
# A7 (review): an invoice the cancel kept because it held money is voided once its refunds left nothing on it.
REFUNDED_VOID_SQL = """
SELECT i.issued_at FROM crm.invoice i
 WHERE i.invoice_id = $1 AND i.deleted_at IS NULL AND i.status IN ('open', 'expired')
   AND EXISTS (SELECT 1 FROM crm.trip_cancellation x WHERE x.trip_id = i.trip_id)
"""
# crm-035: crm_app only reads crm.trip_cancellation; the database writes it. ``open`` reads the stage left and the live
# AA bookings itself (pending while any is live, else done); ``finish`` moves pending -> done only once no AA booking
# is live and the trip is cancelled (card R).
OPEN_SQL = "SELECT crm.trip_cancellation_open($1, $2, $3::jsonb)"
FINISH_SQL = "SELECT crm.trip_cancellation_finish($1)"
LEFT_SQL = """
SELECT count(*)::int FROM crm.booking_line
 WHERE trip_id = $1 AND supplier_kind = 'aa' AND bk_booking_id IS NOT NULL AND status <> 'cancelled'
   AND deleted_at IS NULL
"""
TRIP_SQL = "SELECT stage FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE"
# A refused or given-up cancel opened ``aa_cancel_failed``; once every AA booking is cancelled (the feed's
# ``booking.cancelled`` or Cancel again) the task has nothing left to ask.
CLOSE_FAILED_TASK_SQL = """
UPDATE crm.task SET status = 'done', done_at = now(),
       done_by = coalesce(nullif(current_setting('app.actor', true), ''), 'job:crm_booking_sync')
 WHERE trip_id = $1 AND kind = $2 AND status = 'open' AND deleted_at IS NULL
"""


def _money_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(CENT))


def _uuid_list(raw: Any, field: str) -> list[uuid.UUID]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 200:
        raise Invalid(f"invalid_{field}", "a list of ids")
    try:
        return [uuid.UUID(str(x)) for x in raw]
    except ValueError:
        raise Invalid(f"invalid_{field}", "a list of ids") from None


def refund_choices(raw: Any) -> dict[uuid.UUID, Decimal]:
    """``[{payment_id, amount}]`` from the dialog: the amount asked back per payment (0 = none)."""
    if raw is None:
        return {}
    if not isinstance(raw, list) or len(raw) > 200:
        raise Invalid("invalid_refunds", "a list of {payment_id, amount}")
    out: dict[uuid.UUID, Decimal] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise Invalid("invalid_refunds", "a list of {payment_id, amount}")
        try:
            pid = uuid.UUID(str(item.get("payment_id")))
            amount = Decimal(str(item.get("amount")))
        except (ValueError, InvalidOperation):
            raise Invalid("invalid_refunds", "a list of {payment_id, amount}") from None
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(CENT):
            raise Invalid("invalid_refunds", "amounts are 0 or more, at most two decimals")
        out[pid] = amount
    return out


async def pending(ctx: AnyContext, trip_id: uuid.UUID) -> dict[str, Any] | None:
    row = await ctx.conn.fetchrow(PENDING_SQL, trip_id)
    if row is None:
        return None
    return {
        "status": row["status"],
        "reason_code": row["reason_code"],
        "from_stage": row["from_stage"],
        "aa_bookings": row["aa_bookings"],
        "requested_at": row["created_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


async def is_pending(ctx: AnyContext, trip_id: uuid.UUID) -> bool:
    row = await ctx.conn.fetchrow(PENDING_SQL, trip_id)
    return row is not None and row["status"] == "pending"


async def is_closed(ctx: AnyContext, trip_id: uuid.UUID) -> bool:
    """The trip is cancelled, or its cancellation waits for AA (a ``crm.trip_cancellation`` row, pending or done)."""
    row = await ctx.conn.fetchrow(CLOSED_SQL, trip_id)
    return row is not None and (row["stage"] == "cancelled" or bool(row["cancelling"]))


async def ensure_not_cancelled(ctx: AnyContext, trip_id: uuid.UUID) -> None:
    """S262 (C12 §2.7 items 3-4): a cancelled trip, or one whose cancellation waits for AA, bills the client no more:
    no new invoice, no "Mark paid" on a schedule row, no new client link (409 ``trip_cancelled``). What the cancel
    kept stays workable: a payment the client says they made is recorded on its invoice, refunds go on."""
    if await is_closed(ctx, trip_id):
        raise Conflict("trip_cancelled", "this trip is cancelled: it takes no new invoice, payment or client link")


async def _schedule_to_void(ctx: AnyContext, trip_id: uuid.UUID, voided: set[uuid.UUID]) -> list[uuid.UUID]:
    """The due balance rows the cancel voids: those no kept invoice bills (every live invoice of the row is one the
    cancel voids). A row a kept invoice bills (the client says they paid it, or it holds money) settles through it."""
    return [
        r["schedule_id"]
        for r in await ctx.conn.fetch(SCHEDULE_DUE_SQL, trip_id)
        if set(r["live_invoices"] or []) <= voided
    ]


async def void_cancelled_schedule(ctx: AnyContext, actor: str) -> int:
    """The hourly date rule (S262 fix pass): the due balance rows of cancelled or cancelling trips that the cancel run
    did not void (a trip cancelled before it did) are voided now, with their draft invoices, by the same void and audit
    as the run (``payment_schedule_void`` / ``invoice_void``, reason ``trip_cancelled``). A row a kept invoice bills
    (payment claimed, money held, paid) stays: it settles through that invoice. Idempotent: a void row is never picked
    again. Returns the number of rows voided."""
    voided = 0
    for r in await ctx.conn.fetch(SCHEDULE_SWEEP_SQL, SWEEP_BATCH):
        for invoice_id in r["drafts"] or []:
            await ctx.conn.execute(DRAFT_VOID_SQL, invoice_id, actor)
            ctx.audit("invoice_void", "allowed", "invoice", str(invoice_id), reason="trip_cancelled")
        if await ctx.conn.fetchval(SCHEDULE_VOID_SQL, r["schedule_id"]) is not None:
            ctx.audit("payment_schedule_void", "allowed", "payment_schedule", str(r["schedule_id"]),
                      reason="trip_cancelled")  # fmt: skip
            voided += 1
    return voided


async def _aa_bookings(ctx: AnyContext, trip_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        {
            "booking_id": r["booking_id"],
            "line_id": str(r["line_id"]),
            "titles": list(r["titles"]),
            "asked": bool(r["asked"]),
            "failed_code": r["failed_code"],
        }
        for r in await ctx.conn.fetch(AA_BOOKINGS_SQL, trip_id)
    ]


async def _payments(ctx: AnyContext, trip_id: uuid.UUID, choices: dict[uuid.UUID, Decimal]) -> list[dict[str, Any]]:
    out = []
    for r in await ctx.conn.fetch(PAYMENTS_SQL, trip_id):
        left = max(Decimal(r["refundable"]), Decimal(0))
        asked = min(choices.get(r["payment_id"], left), left)
        out.append(
            {
                "payment_id": str(r["payment_id"]),
                "invoice_id": str(r["invoice_id"]) if r["invoice_id"] else None,
                "invoice_number": r["number"],
                "amount": _money_text(Decimal(r["amount"])),
                "currency": r["currency"],
                "received_on": r["received_on"].isoformat(),
                "refundable": _money_text(left),
                "refund_amount": _money_text(asked),
                "needs_invoice": r["invoice_id"] is None,
            }
        )
    return out


async def _invoices(ctx: AnyContext, trip_id: uuid.UUID) -> dict[str, list[Any]]:
    void, claimed, held = [], [], []
    for r in await ctx.conn.fetch(INVOICES_SQL, trip_id):
        if r["status"] == "payment_claimed":
            claimed.append(r)
        elif r["status"] in ("draft", "open", "expired") and r["held"] <= 0 and not r["refund_open"]:
            void.append(r)
        elif r["held"] > 0:
            held.append(r)
    return {"void": void, "claimed": claimed, "held": held}


async def _commissions(ctx: AnyContext, trip_id: uuid.UUID) -> list[dict[str, Any]]:
    from app.margin import sees_margin

    if not sees_margin(getattr(ctx, "member_role", None)):
        return []
    return [
        {
            "commission_id": str(r["commission_id"]),
            "status": r["status"],
            "title": r["title"],
            "supplier_name": r["supplier_name"],
            "expected": None
            if r["expected_amount"] is None
            else {"amount": _money_text(Decimal(r["expected_amount"])), "currency": r["expected_currency"]},
        }
        for r in await ctx.conn.fetch(COMMISSIONS_SQL, trip_id)
    ]


async def _terms(ctx: AnyContext, trip_id: uuid.UUID) -> dict[str, Any] | None:
    row = await ctx.conn.fetchrow(TERMS_SQL, trip_id)
    if row is None:
        return None
    return {"version_label": row["version_label"], "accepted_at": row["accepted_at"].isoformat()}


def refund_reason(reason: str, terms: dict[str, Any] | None) -> str:
    base = f"Trip cancelled ({reason.replace('_', ' ')})."
    if terms is None:
        return base + " No booking terms were accepted in the CRM."
    return f"{base} Booking terms {terms['version_label']} accepted on {terms['accepted_at'][:10]}."


async def plan(
    ctx: TenantContext, trip_id: uuid.UUID, *, refunds: dict[uuid.UUID, Decimal] | None = None
) -> dict[str, Any]:
    """Everything the cancel would do (no write): the counts of :func:`app.stages.cancel`'s effect list and the detail
    blocks the dialog shows (AA bookings, supplier lines, payments to refund, commissions, the accepted terms)."""
    from app.booking import aa_lines
    from app.ops_release import service as ops_release
    from app.stages import CANCEL_EFFECTS_SQL

    counts = await ctx.conn.fetchrow(CANCEL_EFFECTS_SQL, trip_id)
    aa = await aa_lines.close_effects(ctx, trip_id)
    bookings = await _aa_bookings(ctx, trip_id)
    supplier_lines = [
        {"booking_line_id": str(r["booking_line_id"]), "title": r["title"], "supplier_name": r["supplier_name"],
         "confirmation_no": r["confirmation_no"]}
        for r in await ctx.conn.fetch(SUPPLIER_LINES_SQL, trip_id, MAX_LIST)
    ]  # fmt: skip
    invoices = await _invoices(ctx, trip_id)
    schedule = await _schedule_to_void(ctx, trip_id, {r["invoice_id"] for r in invoices["void"]})
    payments = await _payments(ctx, trip_id, refunds or {})
    commissions = await _commissions(ctx, trip_id)
    workflows = int(await ctx.conn.fetchval(WORKFLOW_DISPATCH_SQL, trip_id))
    for _name, hook in CANCEL_HOOKS:
        workflows += await hook(ctx, trip_id, True)
    tasks = int(await ctx.conn.fetchval(OPEN_TASKS_SQL, trip_id, list(KEEP_TASKS)))
    effects = [
        {"kind": "dispatch_cancelled", "count": counts["dispatch"]},
        {"kind": "workflows_stopped", "count": workflows},
        {"kind": "tasks_skipped", "count": tasks},
        {"kind": "call_slots_cancelled", "count": counts["slots"]},
        {"kind": "client_links_revoked", "count": counts["links"]},
        {"kind": "aa_holds_released", "count": aa["held"]},
        {"kind": "aa_bookings_cancelled", "count": len(bookings)},
        {"kind": "supplier_lines_to_cancel", "count": len(supplier_lines)},
        {"kind": "invoices_voided", "count": len(invoices["void"])},
        {"kind": "invoices_claimed_kept", "count": len(invoices["claimed"])},
        {"kind": "schedule_rows_voided", "count": len(schedule)},
        {"kind": "refund_requests", "count": sum(1 for p in payments
                                                  if not p["needs_invoice"] and Decimal(p["refund_amount"]) > 0)},
        {"kind": "payments_without_invoice", "count": sum(1 for p in payments if p["needs_invoice"])},
        {"kind": "commissions_cancelled", "count": sum(1 for c in commissions if c["status"] == "expected")},
        {"kind": "commissions_claimed_kept", "count": sum(1 for c in commissions if c["status"] == "claimed")},
        {"kind": "ops_release_withdrawn", "count": await ops_release.live_count(ctx, trip_id)},
    ]  # fmt: skip
    return {
        "effects": effects,
        "aa_bookings": bookings,
        "supplier_lines": supplier_lines,
        "payments": payments,
        "commissions": commissions,
        "terms": await _terms(ctx, trip_id),
        "invoices_claimed": [str(r["invoice_id"]) for r in invoices["claimed"]],
    }


async def nudge_requests(ctx: AnyContext, request_ids: list[uuid.UUID]) -> None:
    from app.booking import aa_lines

    for request_id in request_ids:
        await aa_lines.nudge(ctx, request_id)


async def ask_booking(ctx: AnyContext, trip_id: uuid.UUID, bookings: list[dict[str, Any]]) -> list[uuid.UUID]:
    """One ``cancel`` request per AA booking not already asked (on the booking's first line)."""
    made = []
    for b in bookings:
        if b["asked"]:
            continue
        rid = await ctx.conn.fetchval(ASK_CANCEL_SQL, trip_id, uuid.UUID(b["line_id"]))
        if rid is not None:
            made.append(rid)
    return made


async def run(
    ctx: TenantContext,
    trip_id: uuid.UUID,
    trip: Any,
    reason: str,
    *,
    refunds: dict[uuid.UUID, Decimal],
    keep_commission: list[uuid.UUID],
    notices: list[Any] | None,
) -> dict[str, Any]:
    """The run (the caller locked the trip and checked the stage and the reason)."""
    from app import stages
    from app.booking import aa_lines
    from app.commissions import service as commissions
    from app.ops_release import service as ops_release
    from app.payments import reminders

    actor = f"member:{ctx.member_id}"
    before = await plan(ctx, trip_id, refunds=refunds)
    unknown_keep = set(keep_commission) - {uuid.UUID(c["commission_id"]) for c in before["commissions"]
                                           if c["status"] == "expected"}  # fmt: skip
    if unknown_keep:
        raise Invalid("invalid_keep_commission", "keep only expected commissions of this trip")
    for _name, hook in CANCEL_HOOKS:
        await hook(ctx, trip_id, False)
    await ctx.conn.execute(stages.CANCEL_DISPATCH_SQL, trip_id)
    await ctx.conn.execute(SKIP_TASKS_SQL, trip_id, actor, list(KEEP_TASKS))
    await ctx.conn.execute(stages.CANCEL_SLOTS_SQL, trip_id)
    await ctx.conn.execute(stages.CANCEL_LINKS_SQL, trip_id)
    # 3. Invoices that hold no money are voided (the commitment and the reminder task follow).
    invoices = await _invoices(ctx, trip_id)
    for r in invoices["void"]:
        await ctx.conn.execute(VOID_SQL, r["invoice_id"], actor)
        if r["issued_at"] is not None:
            await ctx.conn.execute(INVOICE_COMMITMENT_SQL, trip_id, f"invoice:{r['invoice_id']}")
            await reminders.settled(ctx, trip_id, r["invoice_id"], actor)
        ctx.audit("invoice_void", "allowed", "invoice", str(r["invoice_id"]), reason="trip_cancelled")
    # The due balance rows go with them (S262): the client is never billed again for a cancelled trip.
    for schedule_id in await _schedule_to_void(ctx, trip_id, {r["invoice_id"] for r in invoices["void"]}):
        await ctx.conn.execute(SCHEDULE_VOID_SQL, schedule_id)
        ctx.audit("payment_schedule_void", "allowed", "payment_schedule", str(schedule_id), reason="trip_cancelled")
    # 4. Refund requests (proposed; approved under §4.6 as usual).
    text = refund_reason(reason, before["terms"])
    requested = 0
    for p in before["payments"]:
        amount = Decimal(p["refund_amount"])
        if p["needs_invoice"] or amount <= 0:
            continue
        refund_id = uuid.uuid4()
        await ctx.conn.execute(REFUND_SQL, refund_id, trip_id, uuid.UUID(p["invoice_id"]), uuid.UUID(p["payment_id"]),
                               amount, p["currency"], text)  # fmt: skip
        ctx.audit("refund_request", "allowed", "refund_request", str(refund_id), reason="trip_cancelled")
        requested += 1
    if any(p["needs_invoice"] for p in before["payments"]):
        await ctx.conn.execute(TASK_SQL, trip_id, REFUND_REVIEW_TASK, "urgent")
    # 2. Agency suppliers.
    if before["supplier_lines"]:
        await ctx.conn.execute(TASK_SQL, trip_id, SUPPLIER_TASK, "urgent")
    # 5. Commission.
    if keep_commission:
        await ctx.conn.execute(KEEP_COMMISSION_SQL, keep_commission)
    await ctx.conn.execute(CANCEL_COMMISSION_SQL, trip_id, actor, keep_commission)
    # 1. AA: holds released (after the Undo window, as a Lost move), bookings cancelled through Booking.
    await aa_lines.close_trip(ctx, trip_id)
    asked = await ask_booking(ctx, trip_id, before["aa_bookings"])
    # 6. Ops release.
    await ops_release.withdraw_on_cancel(ctx, trip_id, notices=notices)
    counts = {e["kind"]: e["count"] for e in before["effects"]}
    counts["refund_requests"] = requested
    raw = await ctx.conn.fetchval(OPEN_SQL, trip_id, reason, json.dumps(counts))
    status = (json.loads(raw) if isinstance(raw, str) else dict(raw))["status"]
    if status == "done":
        await move_to_cancelled(ctx, trip_id, trip["stage"], reason)
    else:
        await ctx.conn.execute("UPDATE crm.trip SET waiting_on = 'aa', last_activity_at = now() WHERE trip_id = $1",
                               trip_id)  # fmt: skip
    await commissions.sync(ctx, trip_id)
    await nudge_requests(ctx, asked)
    ctx.audit("trip_cancel", "allowed", "trip", str(trip_id), reason=reason if status == "done" else "pending")
    return {**before, "cancellation": await pending(ctx, trip_id)}


async def retry(ctx: TenantContext, trip_id: uuid.UUID) -> dict[str, Any]:
    """Cancel again on a pending cancellation: ask Booking again for every AA booking without a live request (a
    refused or given-up one), at once."""
    bookings = await _aa_bookings(ctx, trip_id)
    asked = await ask_booking(ctx, trip_id, bookings)
    await nudge_requests(ctx, asked)
    if asked:
        ctx.audit("trip_cancel", "allowed", "trip", str(trip_id), reason="retry")
    return {"aa_bookings": await _aa_bookings(ctx, trip_id), "requeued": len(asked),
            "cancellation": await pending(ctx, trip_id)}  # fmt: skip


async def move_to_cancelled(ctx: AnyContext, trip_id: uuid.UUID, from_stage: str, reason: str) -> None:
    from app import stages

    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(stages.MOVE_SQL, trip_id, "cancelled", None, reason, "none")
    except asyncpg.CheckViolationError:
        raise Conflict("transition_not_allowed", allowed=stages.allowed_moves(from_stage)) from None
    await ctx.conn.execute(stages.HISTORY_SQL, trip_id, from_stage, "cancelled", f"cancel_{reason}", False, None,
                           None, None)  # fmt: skip


async def void_if_refunded(ctx: AnyContext, trip_id: uuid.UUID, invoice_id: uuid.UUID, actor: str) -> bool:
    """After a refund executes (:func:`app.payments.service.execute_refund`): an open / expired invoice of a cancelled
    (or cancelling) trip that the cancel kept because it held money is voided once nothing is held on it and no refund
    request is open (C12 item 3, "invoices are voided"). An invoice keeping a non-refundable part, or whose refund was
    rejected, still holds money and stays; a ``paid`` invoice has no ``void`` edge (crm-024) and stays ``paid`` with
    its refund entries."""
    from app.invoices.service import VOID_BLOCK_SQL
    from app.payments import reminders

    row = await ctx.conn.fetchrow(REFUNDED_VOID_SQL, invoice_id)
    if row is None:
        return False
    block = await ctx.conn.fetchrow(VOID_BLOCK_SQL, invoice_id)
    if block is not None and (block["held"] > 0 or block["refund_open"]):
        return False
    await ctx.conn.execute(VOID_SQL, invoice_id, actor)
    if row["issued_at"] is not None:
        await ctx.conn.execute(INVOICE_COMMITMENT_SQL, trip_id, f"invoice:{invoice_id}")
        await reminders.settled(ctx, trip_id, invoice_id, actor)
    ctx.audit("invoice_void", "allowed", "invoice", str(invoice_id), reason="trip_cancelled_refunded")
    return True


async def finish_if_done(ctx: AnyContext, trip_id: uuid.UUID) -> bool:
    """The Booking sync, after a cancel answer or a ``booking.cancelled`` event: once no AA booking of a trip with a
    pending cancellation is left, the trip moves to ``cancelled`` and the cancellation is done."""
    row = await ctx.conn.fetchrow(PENDING_SQL, trip_id)
    if row is None or row["status"] != "pending":
        return False
    if int(await ctx.conn.fetchval(LEFT_SQL, trip_id)):
        return False
    stage = await ctx.conn.fetchval(TRIP_SQL, trip_id)
    if stage is None:
        return False
    if stage != "cancelled":
        await move_to_cancelled(ctx, trip_id, stage, row["reason_code"])
    await ctx.conn.fetchval(FINISH_SQL, trip_id)
    await ctx.conn.execute(CLOSE_FAILED_TASK_SQL, trip_id, CANCEL_FAILED_TASK)
    from app.commissions import service as commissions

    await commissions.sync(ctx, trip_id)
    ctx.audit("trip_cancel", "allowed", "trip", str(trip_id), reason=row["reason_code"])
    return True


async def booking_fees(client: Any, tenant_id: uuid.UUID, preview: dict[str, Any], reason: str) -> dict[str, Any]:
    """The dry run's Booking fee per AA booking (``POST /bookings/{id}/cancel?dry_run=true``), called outside any
    transaction. Booking down or not configured: ``fee`` stays None and ``booking_unavailable`` is true (the run will
    wait for AA: "cancellation pending")."""
    from app.booking import contract
    from app.booking.client import BookingRefusal, BookingUnavailable

    unavailable = False
    for i, b in enumerate(preview.get("aa_bookings", [])):
        b["fee"] = None
        if client is None or i >= MAX_PREVIEW_FEES:
            unavailable = unavailable or client is None
            continue
        try:
            data = await client.cancel_booking(
                tenant_id, b["booking_id"], BOOKING_REASON.get(reason, "other"), dry_run=True,
                idempotency_key=f"crm-cancel-preview:{b['booking_id']}:{uuid.uuid4().hex[:12]}",
            )  # fmt: skip
            c = contract.cancellation(data)
        except BookingUnavailable:
            unavailable = True
            continue
        except (BookingRefusal, contract.ContractError, ValueError) as exc:
            b["fee_code"] = getattr(exc, "code", "booking_contract")
            continue
        b["fee"] = {
            "amount": _money_text(c.fee),
            "currency": c.currency,
            "fee_pct": c.fee_pct,
            "days_before_departure": c.days_before_departure,
        }
    preview["booking_unavailable"] = unavailable
    return preview


def today_utc() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


__all__ = [
    "BOOKING_REASON",
    "CANCEL_CHECK_CODES",
    "CANCEL_DONE_CODES",
    "CANCEL_FAILED_TASK",
    "booking_fees",
    "ensure_not_cancelled",
    "finish_if_done",
    "is_closed",
    "is_pending",
    "pending",
    "plan",
    "refund_choices",
    "register_cancel_hook",
    "retry",
    "run",
    "void_cancelled_schedule",
    "void_if_refunded",
]
