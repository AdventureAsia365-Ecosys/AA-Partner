"""The client's pay box and the bank-transfer claim (card CRM-23 slice 4, AA-952; design v3 §3.5 "Deposit link (C31,
Fix A3, Fix A8)", F6 step 5, WF-9; migration crm-028).

The pay link is the client's own proposal link (C-04 "Accept and deposit"): after the client accepted, the page lists
the issued deposit / balance invoices of that version with what to pay, by when, and how (the agency's own pay-by
instructions: the agency is the merchant, errata S247 BYO).

- **Pay window** (:func:`pay_window`, derived, never stored): a deposit invoice of an accepted day-by-day version that
  holds AA items can be paid while that hold lives (``crm.proposal.hold_expires_at``, moved by the extension below and
  by the re-hold). Past it the window is ``expired``: the page shows no bank details and no "I have paid", and a card
  payment never starts (:func:`card_payment`: 410 ``pay_link_expired`` before any provider is asked). Other invoices
  have no window (``open`` until paid or void). The invoice status ``expired`` is not written: the money that still
  arrives after the window must reach ``paid`` / ``paid_unfulfillable``, which crm-024 allows from ``open`` /
  ``payment_claimed`` only.
- **"I have paid"** (:func:`claim`, the client link): ``open`` -> ``payment_claimed`` (``claimed_at``), the trip
  owner's urgent task ``verify_transfer`` (due at the window's end) and a bell item. When the invoice pays for an AA
  hold, the caller then asks Booking **once** to extend that hold (:func:`extend`, ``POST /holds/{id}/extend``, reason
  ``payment_claimed``, Idempotency-Key ``crm-invoice:<id>:extend``; Booking decides the length, proposed +72 h, and
  allows one extension per hold) and writes the answer (:func:`finish_claim`): ``hold_extended_at`` + the proposal's
  hold expiry + the task's due time, or Booking's refusal code in ``hold_extend_code``. ``booking_unavailable`` is
  retried by the Booking sync (:func:`extend_due`). A second claim of a claimed invoice answers the same, nothing new.
- **Terms first** (AA-955 x AA-952, S261 live finding F1): while the client still has to accept the agency's newer
  booking terms (:func:`app.esign.terms.reaccept_needed`), a deposit invoice is not payable by the client either: the
  window reads ``terms`` (no bank details, no "I have paid"), and a claim or a card session is 409
  ``terms_reaccept_required`` **before** anything is written (no claim, no verify task, no bell, no hold extension),
  the same code the agent's "Record payment" answers.
- **"Not received"** (:func:`reject_claim`, owner / agent): ``payment_claimed`` -> ``open``; the task is closed. The
  window is the hold's again (the extension already used stays used).

Every function takes the open context and never opens one. Nothing personal is sent to Booking (the hold id only);
bell details carry the invoice number and kind only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, NoReturn

from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.errors import Conflict, Gone, NotFound
from app.esign import terms as esign_terms
from app.payments import providers
from app.proposals import events
from app.tenancy import AnyContext, TenantContext

VERIFY_TASK = "verify_transfer"
CLAIM_REASON = "payment_claimed"
# Without an AA hold the agent gets this long to check the transfer.
VERIFY_WITHOUT_HOLD = dt.timedelta(days=2)
EXTENDED = "extended"
NOT_NEEDED = "not_needed"
UNAVAILABLE = "booking_unavailable"
EXTEND_CODES = ("extend_limit", "hold_expired", "invalid", "not_found")
CLIENT_KINDS = ("deposit", "balance")

# One invoice with what its pay window needs (the proposal's hold), for the client page and the agency views.
INVOICE_SQL = """
SELECT i.invoice_id, i.trip_id, i.kind, i.status, i.number, i.currency, i.total_amount, i.due_on, i.proposal_id,
       i.schedule_id, i.claimed_at, i.hold_extended_at, i.hold_extend_code, i.rehold_requested_at, i.rehold_code,
       p.kind AS proposal_kind, p.accepted_hold_id, p.hold_id, p.hold_expires_at,
       (SELECT coalesce(sum(x.amount), 0) FROM crm.payment x
         WHERE x.invoice_id = i.invoice_id AND x.entry_kind = 'payment') AS paid_amount
  FROM crm.invoice i LEFT JOIN crm.proposal p ON p.proposal_id = i.proposal_id
 WHERE i.invoice_id = $1 AND i.deleted_at IS NULL
"""
LOCK_SQL = INVOICE_SQL.rstrip() + "\n   FOR UPDATE OF i"
CLIENT_LIST_SQL = """
SELECT i.invoice_id, i.trip_id, i.kind, i.status, i.number, i.currency, i.total_amount, i.due_on, i.proposal_id,
       i.schedule_id, i.claimed_at, i.hold_extended_at, i.hold_extend_code, i.rehold_requested_at, i.rehold_code,
       p.kind AS proposal_kind, p.accepted_hold_id, p.hold_id, p.hold_expires_at,
       (SELECT coalesce(sum(x.amount), 0) FROM crm.payment x
         WHERE x.invoice_id = i.invoice_id AND x.entry_kind = 'payment') AS paid_amount
  FROM crm.invoice i JOIN crm.proposal p ON p.proposal_id = i.proposal_id
 WHERE i.proposal_id = $1 AND i.deleted_at IS NULL AND i.issued_at IS NOT NULL AND i.status <> 'void'
   AND i.kind = ANY($2::text[])
 ORDER BY i.due_on, i.kind
"""
PAY_BY_SQL = """
SELECT display_name, instructions FROM crm.payment_provider_account
 WHERE provider = 'manual' AND status = 'active' AND deleted_at IS NULL AND instructions IS NOT NULL
 ORDER BY created_at LIMIT 1
"""
CARD_ACCOUNT_SQL = """
SELECT account_id, provider FROM crm.payment_provider_account
 WHERE status = 'active' AND deleted_at IS NULL AND charges_enabled AND provider <> 'manual'
 ORDER BY provider, created_at
 LIMIT 1
"""
CLAIM_SQL = "UPDATE crm.invoice SET status = 'payment_claimed', claimed_at = now() WHERE invoice_id = $1"
UNCLAIM_SQL = "UPDATE crm.invoice SET status = 'open' WHERE invoice_id = $1"
EXTENDED_SQL = """
UPDATE crm.invoice SET hold_extended_at = now(), hold_extend_code = 'extended'
 WHERE invoice_id = $1 AND hold_extended_at IS NULL
"""
EXTEND_CODE_SQL = """
UPDATE crm.invoice SET hold_extend_code = $2 WHERE invoice_id = $1 AND hold_extended_at IS NULL
"""
# The proposal's hold moves only forward and only for the hold the extension was asked for.
HOLD_EXPIRY_SQL = """
UPDATE crm.proposal SET hold_expires_at = $3
 WHERE proposal_id = $1 AND hold_id = $2 AND hold_expires_at < $3
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
SELECT $1, $2, 'urgent', t.owner_member_id, $3, $4 FROM crm.trip t WHERE t.trip_id = $1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""
TASK_DUE_SQL = """
UPDATE crm.task SET due_at = $3
 WHERE trip_id = $1 AND kind = $2 AND status = 'open' AND deleted_at IS NULL AND NOT due_overridden
"""
CLOSE_TASK_SQL = """
UPDATE crm.task SET status = $3, done_at = now(), done_by = $4
 WHERE trip_id = $1 AND kind = $2 AND status = 'open' AND deleted_at IS NULL
"""
OTHER_CLAIMS_SQL = """
SELECT EXISTS (SELECT 1 FROM crm.invoice WHERE trip_id = $1 AND invoice_id <> $2 AND status = 'payment_claimed'
                  AND deleted_at IS NULL)
"""
COMMITMENT_SQL = """
INSERT INTO crm.trip_commitment (trip_id, kind, line_status, source_ref) VALUES ($1, 'invoice', $2, $3)
ON CONFLICT (tenant_id, kind, source_ref) DO UPDATE SET line_status = EXCLUDED.line_status
"""
EXTEND_DUE_SQL = """
SELECT i.invoice_id, p.hold_id FROM crm.invoice i JOIN crm.proposal p ON p.proposal_id = i.proposal_id
 WHERE i.status = 'payment_claimed' AND i.hold_extended_at IS NULL AND i.hold_extend_code = $1
   AND i.deleted_at IS NULL AND p.hold_id IS NOT NULL AND p.hold_expires_at > now()
 ORDER BY i.claimed_at
 LIMIT $2
"""


def _money(value: Any) -> str:
    return str(Decimal(value).quantize(Decimal("0.01")))


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def holds_aa(row: Any) -> bool:
    """The invoice pays the deposit of an accepted day-by-day version that holds AA items: its window is that hold."""
    return (
        row["kind"] == "deposit"
        and row["proposal_kind"] == "builder"
        and row["accepted_hold_id"] is not None
        and row["schedule_id"] is not None
    )


def pay_window(row: Any, now: dt.datetime, *, terms_first: bool = False) -> dict[str, Any]:
    """``{"state", "until"}``: ``open`` (payable), ``terms`` (payable once the client accepted the agency's newer
    booking terms: ``terms_first``, the client view only), ``claimed`` (the client says paid; the agent checks),
    ``settling`` (the money is in, the AA hold is being placed again), ``expired`` (the AA hold ran out: no bank
    details, no claim, no card session), ``paid``, ``closed`` (void or a draft). ``until`` = the AA hold's expiry,
    when there is one."""
    status = row["status"]
    until = row["hold_expires_at"] if holds_aa(row) else None
    if status in ("paid", "paid_unfulfillable"):
        return {"state": "paid", "until": None}
    if status not in ("open", "payment_claimed"):
        return {"state": "closed", "until": None}
    if row["rehold_requested_at"] is not None:
        return {"state": "settling", "until": None}
    if status == "payment_claimed":
        return {"state": "claimed", "until": _iso(until)}
    if holds_aa(row) and (until is None or until <= now):
        return {"state": "expired", "until": _iso(until)}
    if terms_first:
        return {"state": "terms", "until": _iso(until)}
    return {"state": "open", "until": _iso(until)}


async def terms_pending(ctx: AnyContext, row: Any) -> Any:
    """The newer booking terms version the client must accept before paying this invoice (a deposit only, the rule of
    :func:`app.proposals.service.require_terms_current`), or None."""
    if row["kind"] != "deposit":
        return None
    return await esign_terms.reaccept_needed(ctx, row["trip_id"])


async def _refuse_terms_first(ctx: AnyContext, row: Any, invoice_id: uuid.UUID, action: str) -> None:
    """409 ``terms_reaccept_required`` (audited) while the client still has newer booking terms to accept."""
    newer = await terms_pending(ctx, row)
    if newer is None:
        return
    ctx.audit(action, "denied", "invoice", str(invoice_id), reason="terms_reaccept_required")
    raise Conflict(
        "terms_reaccept_required",
        "accept the updated booking terms on this page before you pay",
        version_label=newer["version_label"],
    )


def agency_view(row: Any, now: dt.datetime) -> dict[str, Any]:
    """The claim / hold facts an agency invoice view adds (WF-9: "Client says paid; verify before ...")."""
    return {
        "pay_window": pay_window(row, now),
        "claimed_at": _iso(row["claimed_at"]),
        "hold_extended_at": _iso(row["hold_extended_at"]),
        "hold_extend_code": row["hold_extend_code"],
        "rehold": None
        if row["rehold_requested_at"] is None
        else {"requested_at": _iso(row["rehold_requested_at"]), "code": row["rehold_code"]},
    }


FACTS_SQL = """
SELECT i.invoice_id, i.kind, i.status, i.schedule_id, i.rehold_requested_at, p.kind AS proposal_kind,
       p.accepted_hold_id, p.hold_id, p.hold_expires_at
  FROM crm.invoice i LEFT JOIN crm.proposal p ON p.proposal_id = i.proposal_id
 WHERE i.invoice_id = $1
"""


async def window_facts(ctx: AnyContext, invoice_id: uuid.UUID) -> Any:
    """What :func:`holds_aa` and the hold's expiry need (the caller holds the invoice lock)."""
    return await ctx.conn.fetchrow(FACTS_SQL, invoice_id)


def _client_invoice(row: Any, now: dt.datetime, pay_by: Any, *, terms_first: bool = False) -> dict[str, Any]:
    window = pay_window(row, now, terms_first=terms_first)
    balance = max(Decimal(row["total_amount"]) - Decimal(row["paid_amount"]), Decimal(0))
    payable = window["state"] == "open"
    return {
        "invoice_id": str(row["invoice_id"]),
        "number": row["number"],
        "kind": row["kind"],
        "currency": row["currency"],
        "total_amount": _money(row["total_amount"]),
        "balance_amount": _money(balance),
        "due_on": _iso(row["due_on"]),
        "pay_window": window,
        # Bank details only while the window is open (design: "bank details show only while the hold is active").
        "pay_by": None
        if not payable or pay_by is None
        else {"name": pay_by["display_name"], "instructions": pay_by["instructions"]},
        "can_claim": payable,
    }


async def client_invoices(ctx: AnyContext, proposal_id: uuid.UUID, now: dt.datetime) -> list[dict[str, Any]]:
    """The accepted page's pay box: the issued deposit / balance invoices of this version (never a service fee or an
    adjustment of the trip, never another version's)."""
    rows = await ctx.conn.fetch(CLIENT_LIST_SQL, proposal_id, list(CLIENT_KINDS))
    if not rows:
        return []
    pay_by = await ctx.conn.fetchrow(PAY_BY_SQL)
    # One version, one trip: the terms rule is read once (deposit invoices only).
    deposit = next((r for r in rows if r["kind"] == "deposit"), None)
    pending = deposit is not None and await terms_pending(ctx, deposit) is not None
    return [_client_invoice(r, now, pay_by, terms_first=pending and r["kind"] == "deposit") for r in rows]


async def _client_row(ctx: AnyContext, proposal_id: uuid.UUID, invoice_id: uuid.UUID) -> Any:
    row = await ctx.conn.fetchrow(LOCK_SQL, invoice_id)
    # Another version's or another trip's invoice (or none) reads as unknown: the link opens its own version only.
    if row is None or row["proposal_id"] != proposal_id or row["kind"] not in CLIENT_KINDS or row["number"] is None:
        raise NotFound("invoice_not_found")
    return row


@dataclass(frozen=True)
class Claim:
    invoice_id: uuid.UUID
    trip_id: uuid.UUID
    proposal_id: uuid.UUID | None
    #: the AA hold to extend once (None: nothing to extend, or extended already)
    hold_id: str | None
    new: bool


async def claim(ctx: AnyContext, proposal_id: uuid.UUID, invoice_id: uuid.UUID, now: dt.datetime) -> Claim:
    """Step 1 of "I have paid" (the client link's transaction): the claim, the task, the bell. 410
    ``pay_link_expired`` once the window ended; 409 ``invoice_not_payable`` for a paid / void invoice; 409
    ``terms_reaccept_required`` (nothing written, so no hold extension follows) while newer booking terms wait."""
    row = await _client_row(ctx, proposal_id, invoice_id)
    window = pay_window(row, now)
    if window["state"] in ("claimed", "settling"):
        return Claim(invoice_id, row["trip_id"], row["proposal_id"], None, new=False)
    if window["state"] == "expired":
        ctx.audit("invoice_claim", "denied", "invoice", str(invoice_id), reason="pay_link_expired")
        raise Gone("pay_link_expired", "this payment link has expired: ask your agent for a new one")
    if window["state"] != "open":
        raise Conflict("invoice_not_payable", "this invoice is paid or no longer payable")
    await _refuse_terms_first(ctx, row, invoice_id, "invoice_claim")
    await ctx.conn.execute(CLAIM_SQL, invoice_id)
    await ctx.conn.execute(COMMITMENT_SQL, row["trip_id"], "payment_claimed", f"invoice:{invoice_id}")
    hold = holds_aa(row)
    due = row["hold_expires_at"] if hold else now + VERIFY_WITHOUT_HOLD
    await ctx.conn.execute(TASK_SQL, row["trip_id"], VERIFY_TASK, "hold_expiry" if hold else "created", due)
    await events.record(ctx, row["trip_id"], events.PAYMENT_CLAIMED, proposal_id=row["proposal_id"], bell=True,
                        detail={"number": row["number"], "kind": row["kind"]})  # fmt: skip
    ctx.audit("invoice_claim", "allowed", "invoice", str(invoice_id), reason=row["kind"])
    needs_extend = hold and row["hold_extended_at"] is None and row["hold_id"] is not None
    if hold and row["hold_id"] is None:
        await ctx.conn.execute(EXTEND_CODE_SQL, invoice_id, NOT_NEEDED)
    return Claim(invoice_id, row["trip_id"], row["proposal_id"], row["hold_id"] if needs_extend else None, new=True)


@dataclass(frozen=True)
class Extension:
    code: str  # extended | booking_unavailable | Booking's refusal code
    expires_at: dt.datetime | None = None


def extend_key(invoice_id: uuid.UUID) -> str:
    return f"crm-invoice:{invoice_id}:extend"


async def extend(client: BookingClient | None, tenant_id: uuid.UUID, invoice_id: uuid.UUID, hold_id: str) -> Extension:
    """Step 2 (no transaction held): Booking's one extension of the hold the invoice pays for. The same key on every
    retry, so a lost answer is answered with the extended hold."""
    if client is None:
        return Extension(UNAVAILABLE)
    try:
        data = await client.extend(tenant_id, hold_id, CLAIM_REASON, idempotency_key=extend_key(invoice_id))
        answer = contract.hold(data)
    except BookingRefusal as exc:
        return Extension(exc.code if exc.code in EXTEND_CODES else "invalid")
    except (BookingUnavailable, contract.ContractError):
        return Extension(UNAVAILABLE)
    if answer.status != "active":
        return Extension("hold_expired")
    return Extension(EXTENDED, answer.expires_at)


async def finish_claim(ctx: AnyContext, invoice_id: uuid.UUID, hold_id: str, ext: Extension) -> dict[str, Any]:
    """Step 3 (a new transaction): Booking's answer on the invoice, the proposal's hold expiry and the task."""
    row = await ctx.conn.fetchrow(LOCK_SQL, invoice_id)
    if row is None or row["hold_extended_at"] is not None:
        return {"code": EXTENDED if row is not None else ext.code, "until": None}
    if ext.code == EXTENDED and ext.expires_at is not None:
        await ctx.conn.execute(EXTENDED_SQL, invoice_id)
        await ctx.conn.execute(HOLD_EXPIRY_SQL, row["proposal_id"], hold_id, ext.expires_at)
        if row["status"] == "payment_claimed":
            await ctx.conn.execute(TASK_DUE_SQL, row["trip_id"], VERIFY_TASK, ext.expires_at)
        ctx.audit("invoice_hold_extend", "allowed", "invoice", str(invoice_id), reason=EXTENDED)
        # The Booking sync refreshes the AA lines' expiry (the feed's hold.extended), nudged now.
        from app.booking import proposal_lines

        await proposal_lines.nudge(ctx, f"extended:{invoice_id}")
        return {"code": EXTENDED, "until": ext.expires_at.isoformat()}
    await ctx.conn.execute(EXTEND_CODE_SQL, invoice_id, ext.code)
    ctx.audit("invoice_hold_extend", "denied", "invoice", str(invoice_id), reason=ext.code)
    return {"code": ext.code, "until": None}


async def extend_due(ctx: AnyContext, limit: int = 10) -> list[tuple[uuid.UUID, str]]:
    """The claimed invoices whose extension got no answer from Booking (retried by the sync while the hold lives)."""
    return [(r["invoice_id"], r["hold_id"]) for r in await ctx.conn.fetch(EXTEND_DUE_SQL, UNAVAILABLE, limit)]


async def reject_claim(ctx: TenantContext, invoice_id: uuid.UUID) -> uuid.UUID:
    """The agent's "Not received" (owner / agent): the transfer did not arrive. ``payment_claimed`` -> ``open``; the
    verify task is closed (skipped) unless another claim of the trip waits. Returns the trip id."""
    row = await ctx.conn.fetchrow(LOCK_SQL, invoice_id)
    if row is None:
        raise NotFound("invoice_not_found")
    if row["status"] != "payment_claimed":
        raise Conflict("invoice_not_claimed", "only an invoice the client says they paid can be marked not received")
    if row["rehold_requested_at"] is not None:
        raise Conflict("invoice_settling", "the money is recorded already; the AA hold is being placed again")
    await ctx.conn.execute(UNCLAIM_SQL, invoice_id)
    await ctx.conn.execute(COMMITMENT_SQL, row["trip_id"], "open", f"invoice:{invoice_id}")
    if not await ctx.conn.fetchval(OTHER_CLAIMS_SQL, row["trip_id"], invoice_id):
        await ctx.conn.execute(CLOSE_TASK_SQL, row["trip_id"], VERIFY_TASK, "skipped", f"member:{ctx.member_id}")
    ctx.audit("invoice_claim_reject", "allowed", "invoice", str(invoice_id))
    return row["trip_id"]


async def close_verify(ctx: AnyContext, trip_id: uuid.UUID, invoice_id: uuid.UUID, actor: str) -> None:
    """The money arrived (a payment settled the claim): the verify task is done unless another claim waits."""
    if not await ctx.conn.fetchval(OTHER_CLAIMS_SQL, trip_id, invoice_id):
        await ctx.conn.execute(CLOSE_TASK_SQL, trip_id, VERIFY_TASK, "done", actor)


async def card_payment(ctx: AnyContext, proposal_id: uuid.UUID, invoice_id: uuid.UUID, now: dt.datetime) -> NoReturn:
    """The card pay button (C31): the window is checked **before** any provider is asked, so a link past the hold's
    expiry never creates a payment session (410 ``pay_link_expired``). With a live window, the agency's own card
    account would start the session; no card provider has code yet (BYO Stripe is the follow-up card), so 409
    ``provider_not_available``."""
    row = await _client_row(ctx, proposal_id, invoice_id)
    window = pay_window(row, now)
    if window["state"] == "expired":
        ctx.audit("invoice_card_session", "denied", "invoice", str(invoice_id), reason="pay_link_expired")
        raise Gone("pay_link_expired", "this payment link has expired: ask your agent for a new one")
    if window["state"] != "open":
        raise Conflict("invoice_not_payable", "this invoice is paid, claimed or no longer payable")
    await _refuse_terms_first(ctx, row, invoice_id, "invoice_card_session")
    account = await ctx.conn.fetchrow(CARD_ACCOUNT_SQL)
    if account is None:
        raise Conflict("card_not_offered", "this agency takes payment by transfer: use the details shown")
    # 409 provider_not_available while no card provider has code; the session itself is the BYO Stripe card's.
    providers.provider(account["provider"])
    raise Conflict("provider_not_available", "card payments are not available yet")
