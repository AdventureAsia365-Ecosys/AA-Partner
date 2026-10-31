"""The money of a late deposit: hold the accepted AA items again, or refund (card CRM-23 slice 4, AA-952; design v3
§3.5 "Deposit link": "Marked received after expiry -> re-hold. If that fails -> paid_unfulfillable + a refund request +
agent notice"; card E; migration crm-028).

:func:`app.payments.service.record_payment` leaves a deposit invoice whose payments reached its total but whose AA hold
ran out (also after the one extension of :mod:`app.payments.claims`) with ``rehold_requested_at`` set: nothing is
settled yet. The Booking sync (actor ``job:crm_booking_sync``; it is the only writer of AA lines, crm-014 CRM24) then,
per invoice, in three steps (no connection held while Booking answers, the AA-947 rule):

1. :func:`plan` (a transaction): the invoice under its row lock and what a new hold needs (the accepted AA choice of
   the version as the client's hold had it, ``app.proposals.holds._lines``).
2. :func:`ask` (no transaction): ``GET /holds/{id}`` of the version's hold first: still active at Booking (the CRM's
   expiry was stale) -> ``hold_live``. Otherwise one quote of the same lines (none of them sold out, and the new net
   total **not above** the expired hold's: the agency's cost never rises behind its back -> ``price_higher``); the
   quote is written down on the invoice (``remember``) before ``POST /holds`` with the Idempotency-Key
   ``crm-invoice:<id>:rehold``, so a lost answer is replayed with the same quote and key (Booking answers with the hold
   it made). Booking's refusal (``sold_out``, ``price_changed`` (the quote's validity ran out), ``tour_not_served``,
   ``invalid``, ``not_found``) ends it; ``rehold_limit`` waits for the next UTC day once (then ends it); no answer is
   tried again next run.
3. :func:`apply` (a transaction, the invoice locked again):
   - **held** (``reheld`` / ``hold_live``): the version's hold columns and the AA lines move to the new hold (held,
     its quote, its expiry, the cost from its line prices with its FX lock), the invoice is ``paid``, its schedule row
     settled with the CRM-18 effects (:func:`app.proposals.service.settle_schedule`: Booked, portal invites, the
     confirm nudged), a bell item;
   - **not held**: the invoice is ``paid_unfulfillable`` (``paid_at`` set: the money is in), one refund request per
     payment for what is left to ask back (proposed by the sync; the §4.6 matrix decides: an agent up to the
     threshold, the owner above), the trip owner's urgent task ``payment_unfulfillable`` and a bell item. The schedule
     row stays due and the trip does not move.

Nothing personal goes to Booking: counts by age band and the agent's rooms (the client's hold body), the hold id.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.payments import claims
from app.proposals import builder, client_page, events, holds
from app.proposals import service as proposals
from app.proposals.aa_options import proposal_ref
from app.tenancy import AnyContext

MAX_INVOICES = 10
UNFULFILLABLE_TASK = "payment_unfulfillable"
# rehold_limit (Booking's 3 re-holds a UTC day) is waited out this long before the money goes back.
LIMIT_WAIT = dt.timedelta(hours=26)
REFUSALS = ("sold_out", "price_changed", "tour_not_served", "invalid", "not_found", "hold_expired")
REFUND_REASON = (
    "The AA services of this trip could not be held again after this payment arrived ({code}). Return the money, or "
    "reject this request if the client agreed another plan."
)

# An invoice Booking answered rehold_limit waits for the next UTC day (Booking counts re-holds per UTC day).
DUE_SQL = """
SELECT i.invoice_id FROM crm.invoice i
 WHERE i.rehold_requested_at IS NOT NULL AND i.status IN ('open', 'payment_claimed') AND i.deleted_at IS NULL
   AND (i.rehold_code IS DISTINCT FROM 'rehold_limit'
        OR (i.updated_at AT TIME ZONE 'UTC')::date < $2::date)
 ORDER BY i.rehold_requested_at
 LIMIT $1
"""
PLAN_SQL = """
SELECT i.invoice_id, i.trip_id, i.status, i.number, i.kind, i.schedule_id, i.total_amount, i.rehold_requested_at,
       i.rehold_quote_id, i.rehold_price_hash, p.proposal_id, p.version, p.currency, p.hold_id, p.accepted_hold_id,
       p.starts_on, p.room_config, t.party_adults, t.party_child_ages
  FROM crm.invoice i
  JOIN crm.proposal p ON p.proposal_id = i.proposal_id AND p.status = 'accepted' AND p.kind = 'builder'
  JOIN crm.trip t ON t.trip_id = i.trip_id AND t.deleted_at IS NULL
 WHERE i.invoice_id = $1 AND i.rehold_requested_at IS NOT NULL AND i.status IN ('open', 'payment_claimed')
   AND i.deleted_at IS NULL
   FOR UPDATE OF i
"""
PAID_SQL = "SELECT coalesce(sum(amount), 0) FROM crm.payment WHERE invoice_id = $1 AND entry_kind = 'payment'"
REMEMBER_SQL = """
UPDATE crm.invoice SET rehold_quote_id = $2, rehold_price_hash = $3
 WHERE invoice_id = $1 AND rehold_requested_at IS NOT NULL AND rehold_quote_id IS NULL
   AND status IN ('open', 'payment_claimed')
RETURNING invoice_id
"""
PROPOSAL_REHELD_SQL = """
UPDATE crm.proposal SET hold_id = $2, hold_quote_id = $3, hold_expires_at = $4, hold_placed_at = now(),
       hold_count = hold_count + 1, hold_set_ref = coalesce(hold_set_ref, 'crm-proposal:' || proposal_id::text),
       hold_selection_sha256 = coalesce(hold_selection_sha256, accepted_selection_sha256), hold_release_id = NULL
 WHERE proposal_id = $1
"""
PROPOSAL_LIVE_SQL = "UPDATE crm.proposal SET hold_expires_at = $3 WHERE proposal_id = $1 AND hold_id = $2"
LINES_SQL = """
UPDATE crm.booking_line SET bk_hold_id = $2, hold_expires_at = $3, aa_sub_status = 'held', aa_result_code = NULL,
       bk_quote_id = $4, aa_price_hash = $5
 WHERE booking_line_id = ANY($1::uuid[]) AND supplier_kind = 'aa' AND status = 'planning' AND deleted_at IS NULL
   AND aa_sub_status IN ('held', 'expired')
RETURNING booking_line_id
"""
LINE_COUNT_SQL = """
SELECT count(*) FROM crm.booking_line WHERE booking_line_id = ANY($1::uuid[]) AND supplier_kind = 'aa'
   AND deleted_at IS NULL
"""
MONEY_SQL = "SELECT crm.booking_line_aa_money($1, $2, $3, $4, $5, $6)"
COMMIT_SQL = """
INSERT INTO crm.trip_commitment (trip_id, kind, line_status, source_ref, checked_at)
VALUES ($1, 'aa_line', 'held', $2, now())
ON CONFLICT (tenant_id, kind, source_ref) DO UPDATE SET line_status = EXCLUDED.line_status, checked_at = now()
"""
INVOICE_COMMIT_SQL = """
INSERT INTO crm.trip_commitment (trip_id, kind, line_status, source_ref) VALUES ($1, 'invoice', $2, $3)
ON CONFLICT (tenant_id, kind, source_ref) DO UPDATE SET line_status = EXCLUDED.line_status
"""
PAID_INVOICE_SQL = "UPDATE crm.invoice SET status = $2, paid_at = now(), rehold_code = $3 WHERE invoice_id = $1"
WAIT_CODE_SQL = "UPDATE crm.invoice SET rehold_code = $2 WHERE invoice_id = $1"
PAYMENTS_SQL = """
SELECT p.payment_id, p.trip_id, p.amount, p.currency,
       p.amount - coalesce((SELECT sum(r.amount) FROM crm.refund_request r WHERE r.payment_id = p.payment_id
                              AND r.status <> 'rejected' AND r.deleted_at IS NULL), 0) AS left_amount
  FROM crm.payment p WHERE p.invoice_id = $1 AND p.entry_kind = 'payment'
 ORDER BY p.received_on, p.created_at
"""
REFUND_SQL = """
INSERT INTO crm.refund_request (refund_id, trip_id, invoice_id, payment_id, amount, currency, reason)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
SELECT $1, $2, 'urgent', t.owner_member_id, 'created', now() + interval '1 day' FROM crm.trip t WHERE t.trip_id = $1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""


@dataclass
class Plan:
    invoice_id: uuid.UUID
    tenant_id: uuid.UUID
    hold_id: str | None
    aa_ids: list[str]
    body: dict[str, Any]
    quote_id: str | None
    price_hash: str | None
    requested_at: dt.datetime
    key: str = ""

    def __post_init__(self) -> None:
        self.key = f"crm-invoice:{self.invoice_id}:rehold"


@dataclass
class Asked:
    # held: reheld | hold_live; ended: a refusal code; wait: rehold_limit (try again later); retry: no answer
    outcome: str
    code: str | None = None
    held: contract.HeldSet | None = None
    lines: dict[str, Decimal] = field(default_factory=dict)


async def due(ctx: AnyContext, now: dt.datetime, limit: int = MAX_INVOICES) -> list[uuid.UUID]:
    """The invoices waiting for a re-hold (after Booking's ``rehold_limit``: from the next UTC day of ``now``)."""
    return [r["invoice_id"] for r in await ctx.conn.fetch(DUE_SQL, limit, now.astimezone(dt.UTC).date())]


async def plan(ctx: AnyContext, invoice_id: uuid.UUID) -> Plan | None:
    """Step 1: None when the invoice no longer waits (another run settled it) or its AA lines are not written yet
    (the sync writes them first in the same run; the next run picks the invoice up)."""
    row = await ctx.conn.fetchrow(PLAN_SQL, invoice_id)
    if row is None:
        return None
    if Decimal(await ctx.conn.fetchval(PAID_SQL, invoice_id)) < Decimal(row["total_amount"]):
        return None  # pragma: no cover — record_payment requests a re-hold only once the payments reach the total
    s = await builder._structure(ctx, row["proposal_id"])  # type: ignore[arg-type]
    picks = [dict(r) for r in await ctx.conn.fetch(builder.PICKS_SQL, row["proposal_id"])]
    aa_ids = client_page.aa_chosen(client_page.effective(s, picks)["choice"], builder._option_map(s))
    if aa_ids and int(await ctx.conn.fetchval(LINE_COUNT_SQL, [uuid.UUID(o) for o in aa_ids])) < len(aa_ids):
        return None
    body = {
        "proposal_ref": proposal_ref(row["proposal_id"]),
        "proposal_version": int(row["version"]),
        "currency": row["currency"],
        "lines": holds._lines(row, s, aa_ids),
    }
    return Plan(
        invoice_id=invoice_id,
        tenant_id=ctx.tenant_id,
        hold_id=row["hold_id"] or row["accepted_hold_id"],
        aa_ids=aa_ids,
        body=body,
        quote_id=row["rehold_quote_id"],
        price_hash=row["rehold_price_hash"],
        requested_at=row["rehold_requested_at"],
    )


def _refused(exc: BookingRefusal, p: Plan, now: dt.datetime) -> Asked:
    if exc.code == "rehold_limit":
        if now - p.requested_at < LIMIT_WAIT:
            return Asked("wait", "rehold_limit")
        return Asked("ended", "rehold_limit")
    return Asked("ended", exc.code if exc.code in REFUSALS else "hold_refused")


Remember = Callable[[str, str], Awaitable[bool]]


async def ask(client: BookingClient, p: Plan, remember: Remember, now: dt.datetime) -> Asked:
    """Step 2: Booking only (``remember``'s short transaction aside)."""
    if not p.aa_ids:
        return Asked("ended", "no_aa_items")
    current: contract.HeldSet | None = None
    if p.hold_id is not None:
        try:
            current = contract.held_set(await client.get_hold(p.tenant_id, p.hold_id))
        except BookingRefusal as exc:
            if exc.code != "not_found":
                return Asked("retry", exc.code)
        except (BookingUnavailable, contract.ContractError):
            return Asked("retry", "booking_unavailable")
    if current is not None and current.status == "active" and current.expires_at > now:
        return Asked("held", "hold_live", current)
    quote_id, price_hash = p.quote_id, p.price_hash
    if quote_id is None or price_hash is None:
        if current is None:
            return Asked("ended", "not_found")  # nothing to compare the new price with: the agent decides
        try:
            data = await client.quote(p.tenant_id, p.body)
            lines = {oid: contract.quote_line(data, oid) for oid in p.aa_ids}
        except BookingRefusal as exc:
            return _refused(exc, p, now)
        except (BookingUnavailable, contract.ContractError):
            return Asked("retry", "booking_unavailable")
        if any(line.availability == "sold_out" for line in lines.values()):
            return Asked("ended", "sold_out")
        old = [current.lines.get(oid) for oid in p.aa_ids]
        new_total = sum((line.b2b_price for line in lines.values()), Decimal(0))
        same_currency = all(line.currency == current.currency for line in lines.values())
        if any(o is None for o in old) or not same_currency:
            return Asked("ended", "price_higher")
        if new_total > sum((o.b2b_price for o in old if o is not None), Decimal(0)):
            return Asked("ended", "price_higher")
        first = next(iter(lines.values()))
        quote_id, price_hash = first.quote_id, first.price_hash
        if not await remember(quote_id, price_hash):
            return Asked("retry", "busy")  # another run wrote its quote first: it goes on with that one
    body = {"quote_id": quote_id, "price_hash": price_hash, "proposal_ref": p.body["proposal_ref"],
            "proposal_version": p.body["proposal_version"]}  # fmt: skip
    try:
        held = contract.held_set(await client.hold(p.tenant_id, body, idempotency_key=p.key))
    except BookingRefusal as exc:
        return _refused(exc, p, now)
    except (BookingUnavailable, contract.ContractError):
        return Asked("retry", "booking_unavailable")  # Booking may have made it: replayed with the same key
    if held.status != "active":
        return Asked("ended", "hold_expired")
    return Asked("held", "reheld", held)


async def remember(ctx: AnyContext, invoice_id: uuid.UUID, quote_id: str, price_hash: str) -> bool:
    return await ctx.conn.fetchval(REMEMBER_SQL, invoice_id, quote_id, price_hash) is not None


async def apply(ctx: AnyContext, invoice_id: uuid.UUID, asked: Asked) -> str | None:
    """Step 3 (the invoice locked again): ``paid`` / ``paid_unfulfillable`` / None (it waits: retry or the limit)."""
    row = await ctx.conn.fetchrow(PLAN_SQL, invoice_id)
    if row is None:
        return None  # settled by another run meanwhile
    if asked.outcome == "retry":
        return None
    if asked.outcome == "wait":
        await ctx.conn.execute(WAIT_CODE_SQL, invoice_id, asked.code)
        return None
    actor = str(getattr(ctx, "actor", "system"))
    if asked.outcome == "held":
        await _held(ctx, row, asked)
        await ctx.conn.execute(PAID_INVOICE_SQL, invoice_id, "paid", asked.code)
        await ctx.conn.execute(INVOICE_COMMIT_SQL, row["trip_id"], "paid", f"invoice:{invoice_id}")
        await claims.close_verify(ctx, row["trip_id"], invoice_id, actor)
        schedule = await ctx.conn.fetchrow(proposals.SCHEDULE_ONE_SQL, row["schedule_id"])
        if schedule is not None and schedule["status"] == "due":
            await proposals.settle_schedule(ctx, None, schedule)
        await events.record(ctx, row["trip_id"], events.INVOICE_REHELD, proposal_id=row["proposal_id"], bell=True,
                            detail={"number": row["number"], "kind": row["kind"], "code": asked.code})  # fmt: skip
        ctx.audit("invoice_rehold", "allowed", "invoice", str(invoice_id), reason=asked.code)
        return "paid"
    code = asked.code or "hold_refused"
    await ctx.conn.execute(PAID_INVOICE_SQL, invoice_id, "paid_unfulfillable", code)
    await ctx.conn.execute(INVOICE_COMMIT_SQL, row["trip_id"], "paid_unfulfillable", f"invoice:{invoice_id}")
    await claims.close_verify(ctx, row["trip_id"], invoice_id, actor)
    for pay in await ctx.conn.fetch(PAYMENTS_SQL, invoice_id):
        if Decimal(pay["left_amount"]) > 0:
            await ctx.conn.execute(REFUND_SQL, uuid.uuid4(), pay["trip_id"], invoice_id, pay["payment_id"],
                                   pay["left_amount"], pay["currency"], REFUND_REASON.format(code=code))  # fmt: skip
    await ctx.conn.execute(TASK_SQL, row["trip_id"], UNFULFILLABLE_TASK)
    await events.record(ctx, row["trip_id"], events.INVOICE_UNFULFILLABLE, proposal_id=row["proposal_id"], bell=True,
                        detail={"number": row["number"], "kind": row["kind"], "code": code})  # fmt: skip
    ctx.audit("invoice_rehold", "denied", "invoice", str(invoice_id), reason=code[:40])
    return "paid_unfulfillable"


async def _held(ctx: AnyContext, row: Any, asked: Asked) -> None:
    held = asked.held
    assert held is not None  # noqa: S101 — "held" always carries Booking's hold
    if asked.code == "hold_live":
        await ctx.conn.execute(PROPOSAL_LIVE_SQL, row["proposal_id"], held.hold_id, held.expires_at)
        return
    await ctx.conn.execute(PROPOSAL_REHELD_SQL, row["proposal_id"], held.hold_id, held.quote_id, held.expires_at)
    moved = await ctx.conn.fetch(LINES_SQL, [uuid.UUID(o) for o in held.lines], held.hold_id, held.expires_at,
                                 held.quote_id, held.price_hash)  # fmt: skip
    for r in moved:
        line = held.lines[str(r["booking_line_id"])]
        # The net of the new hold (never above the expired one's), with its FX lock; the sell stays the agency's.
        await ctx.conn.fetchval(MONEY_SQL, r["booking_line_id"], "cost", line.b2b_price, held.currency,
                                held.rate_per_usd, held.rate_date)  # fmt: skip
        ctx.audit("aa_sync", "allowed", "booking_line", str(r["booking_line_id"]), reason="rehold_paid")
    await ctx.conn.execute(COMMIT_SQL, row["trip_id"], held.hold_id)
