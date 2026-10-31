"""Payments against invoices, refunds, the agency's own payment methods (card CRM-23 slice 3, AA-952; design v3 group
F, M7, §4.6, §6.3, A-10). Routes in :mod:`app.routers.payments`.

- **Record a payment** (``PAYMENT_MARK_RECEIVED``, owner / agent) on an issued invoice: the amount (partial payments
  allowed, never above the balance: 422 ``amount_above_balance``), the method, the day it arrived (today or earlier),
  the agency's own reference. Provider ``manual``: the money reached the agency's own account outside the CRM. When the
  payments reach the total the invoice is paid, and a deposit / balance invoice settles its schedule row with the
  CRM-18 effects (:func:`app.proposals.service.settle_schedule`: Proposal -> Booked, portal invites, the Booking
  confirm). The trip's ``trip_commitment`` rows follow (payment succeeded; invoice paid). AA-952 slice 4: a claimed
  invoice's verify task closes with the money; when the money completes a deposit whose AA hold ran out
  (:func:`app.payments.claims.holds_aa`), nothing settles here: ``rehold_requested_at`` is set and the Booking sync
  holds the AA items again first (:mod:`app.booking.rehold`: paid, or paid_unfulfillable with refund requests).
- **Refund requests** (design §4.6, C12, C31): proposed by owner / agent for (part of) one payment (never more than
  is left to ask back: 422 ``amount_above_refundable``) -> approved / rejected (``REFUND_APPROVE``: the owner any
  amount, an agent up to ``tenant_settings.refund_owner_threshold_base``, judged in the base currency on everything
  approved on the invoice so far plus this request (a rejection: this request alone); no rate for the day = only the
  owner; an agent never approves a request they made: 403 ``refund_self_approval``) -> executed
  (``PAYMENT_MARK_RECEIVED``): the payment's provider refunds (manual: the member records the money returned) and a
  refund entry joins ``crm.payment``. A fully refunded payment's commitment reads ``refunded`` (still ``has_money``:
  the trip delete stays blocked); once every payment of an invoice is refunded, the invoice can be voided.
- **Payment methods** (``PAYMENT_SETTINGS``, owner): the manual method's name and pay-by instructions (printed on the
  agency's invoices); disconnect; another provider is 409 ``provider_not_available`` until it has code.

Every function takes the open context and never opens one. Amounts are the client's sell amounts (no margin key).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.invoices import service as invoices
from app.ledger import fx
from app.payments import claims, providers
from app.permissions import Action, Rule, require, rule_for
from app.proposals import service as proposals
from app.tenancy import AnyContext, TenantContext
from app.vault import Vault

METHODS = ("bank_transfer", "cash", "card_offline", "other")
PAYABLE = ("open", "payment_claimed")
REFUND_STATUSES = ("proposed", "approved", "rejected", "executed")
MAX_REFERENCE = 120
MAX_NOTE = 500
MAX_LIST = 200
CENT = Decimal("0.01")

INVOICE_LOCK_SQL = """
SELECT invoice_id, trip_id, kind, status, schedule_id, currency, total_amount, number
  FROM crm.invoice WHERE invoice_id = $1 AND deleted_at IS NULL
   FOR UPDATE
"""
PAID_SUM_SQL = """
SELECT coalesce(sum(amount), 0) FROM crm.payment WHERE invoice_id = $1 AND entry_kind = 'payment'
"""
INSERT_PAYMENT_SQL = """
INSERT INTO crm.payment (payment_id, trip_id, schedule_id, invoice_id, amount, currency, method, received_on, provider,
                         provider_ref, entry_kind, refund_of)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
"""
INVOICE_PAID_SQL = "UPDATE crm.invoice SET status = 'paid', paid_at = now() WHERE invoice_id = $1"
REHOLD_SQL = """
UPDATE crm.invoice SET rehold_requested_at = now(), rehold_code = NULL, rehold_quote_id = NULL,
       rehold_price_hash = NULL
 WHERE invoice_id = $1
"""
COMMITMENT_SQL = """
INSERT INTO crm.trip_commitment (trip_id, kind, line_status, source_ref) VALUES ($1, $2, $3, $4)
ON CONFLICT (tenant_id, kind, source_ref) DO UPDATE SET line_status = EXCLUDED.line_status
"""
PAYMENT_SQL = """
SELECT p.payment_id, p.trip_id, p.invoice_id, p.schedule_id, p.amount, p.currency, p.provider, p.provider_ref,
       p.entry_kind
  FROM crm.payment p WHERE p.payment_id = $1
"""
ASKED_SQL = """
SELECT coalesce(sum(amount), 0) FROM crm.refund_request
 WHERE payment_id = $1 AND status <> 'rejected' AND deleted_at IS NULL
"""
REFUNDED_SQL = """
SELECT coalesce(sum(amount), 0) FROM crm.payment WHERE refund_of = $1 AND entry_kind = 'refund'
"""
INSERT_REFUND_SQL = """
INSERT INTO crm.refund_request (refund_id, trip_id, invoice_id, payment_id, amount, currency, reason)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""
REFUND_LOCK_SQL = """
SELECT r.refund_id, r.trip_id, r.invoice_id, r.payment_id, r.amount, r.currency, r.status, r.requested_by
  FROM crm.refund_request r WHERE r.refund_id = $1 AND r.deleted_at IS NULL
   FOR UPDATE
"""
DECIDE_SQL = """
UPDATE crm.refund_request SET status = $2, decided_by = $3, decided_at = now(), decision_note = $4, base_currency = $5,
       base_amount = $6
 WHERE refund_id = $1
"""
EXECUTE_SQL = """
UPDATE crm.refund_request SET status = 'executed', executed_at = now(), refund_payment_id = $2 WHERE refund_id = $1
"""
REFUND_ONE_SQL = """
SELECT refund_id, invoice_id, payment_id, amount, currency, reason, status, requested_by, decided_by, decided_at,
       decision_note, executed_at, refund_payment_id, created_at
  FROM crm.refund_request WHERE refund_id = $1
"""
REFUND_LIST_SQL = """
SELECT r.refund_id, r.invoice_id, r.payment_id, r.amount, r.currency, r.reason, r.status, r.requested_by,
       r.decided_by, r.decided_at, r.decision_note, r.executed_at, r.refund_payment_id, r.created_at, r.trip_id,
       i.number, t.title AS trip_title
  FROM crm.refund_request r
  JOIN crm.invoice i ON i.invoice_id = r.invoice_id
  JOIN crm.trip t ON t.trip_id = r.trip_id
 WHERE r.deleted_at IS NULL AND r.status = ANY($1::text[])
 ORDER BY r.created_at DESC
 LIMIT $2
"""
THRESHOLD_SQL = "SELECT refund_owner_threshold_base FROM crm.tenant_settings"
# The refunds already approved on the invoice, in the base currency (NULL when one has no base amount in today's base
# currency: the agent's check then fails closed). An agent's approval is judged on these plus the one decided, so a
# refund split into small requests still reaches the owner.
APPROVED_BASE_SQL = """
SELECT CASE WHEN bool_or(base_amount IS NULL OR base_currency IS DISTINCT FROM $3) THEN NULL
            ELSE coalesce(sum(base_amount), 0) END
  FROM crm.refund_request
 WHERE invoice_id = $1 AND refund_id <> $2 AND status IN ('approved', 'executed') AND deleted_at IS NULL
"""
DEFAULT_THRESHOLD = Decimal("1000")
ACCOUNTS_SQL = """
SELECT account_id, provider, status, display_name, instructions, external_account_id, default_currency,
       charges_enabled, connected_at, disconnected_at
  FROM crm.payment_provider_account WHERE deleted_at IS NULL
 ORDER BY status, provider, connected_at
"""
ACTIVE_MANUAL_SQL = """
SELECT account_id FROM crm.payment_provider_account
 WHERE provider = 'manual' AND status = 'active' AND deleted_at IS NULL
   FOR UPDATE
"""
INSERT_ACCOUNT_SQL = """
INSERT INTO crm.payment_provider_account (account_id, provider, display_name, instructions)
VALUES ($1, 'manual', $2, $3)
"""
UPDATE_ACCOUNT_SQL = """
UPDATE crm.payment_provider_account SET display_name = $2, instructions = $3 WHERE account_id = $1
"""
DISCONNECT_SQL = """
UPDATE crm.payment_provider_account SET status = 'disconnected', disconnected_at = now()
 WHERE account_id = $1 AND status = 'active'
RETURNING account_id
"""


def _money(value: Any) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _amount(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except ArithmeticError:
        raise Invalid("invalid_amount", "an amount above 0, in cents") from None
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(CENT) or amount > invoices.MAX_AMOUNT:
        raise Invalid("invalid_amount", "an amount above 0, in cents")
    return amount


def _note(value: Any, field: str, max_len: int, *, required: bool) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise Invalid(f"invalid_{field}", "required")
        return None
    if not isinstance(value, str):
        raise Invalid(f"invalid_{field}", "text")
    text = value.strip()
    if len(text) > max_len:
        raise Invalid(f"{field}_too_long", f"at most {max_len} characters")
    if invoices.CONTROL.search(text.replace("\n", " ")):
        raise Invalid(f"invalid_{field}", "text without control characters")
    return text


def _method(method: str) -> str:
    if method not in METHODS:
        raise Invalid("invalid_method", ", ".join(METHODS))
    return method


async def _day(ctx: AnyContext, day: dt.date | None) -> dt.date:
    today = await fx.agency_today(ctx)
    when = day or today
    if when > today:
        raise Invalid("received_in_future", "the day the money moved, today or earlier")
    return when


# ── payments ──────────────────────────────────────────────────────────────────────────────────────


async def record_payment(
    ctx: TenantContext,
    vault: Vault,
    invoice_id: uuid.UUID,
    *,
    amount: Any,
    method: str,
    received_on: dt.date | None,
    reference: Any = None,
) -> dict[str, Any]:
    value = _amount(amount)
    _method(method)
    ref = _note(reference, "reference", MAX_REFERENCE, required=False)
    if ref is not None and "\n" in ref:
        raise Invalid("invalid_reference", "one line")
    row = await ctx.conn.fetchrow(INVOICE_LOCK_SQL, invoice_id)
    if row is None:
        raise NotFound("invoice_not_found")
    if row["status"] not in PAYABLE:
        raise Conflict("invoice_not_payable", "only an issued, unpaid invoice takes a payment")
    schedule = None
    if row["schedule_id"] is not None:
        # Locked after the invoice (the order of invoices.issue and proposals.void). The row is settled through this
        # invoice only: "Mark paid" and the row's void refuse while it is issued, so a row not due is a broken pair.
        schedule = await ctx.conn.fetchrow(proposals.SCHEDULE_ONE_SQL, row["schedule_id"])
        if schedule is None or schedule["status"] != "due":
            raise Conflict("schedule_not_due", "the schedule row this invoice bills is paid or void: void the invoice")
        await proposals.require_terms_current(ctx, schedule)  # AA-955: newer terms are accepted before the deposit
    paid = Decimal(await ctx.conn.fetchval(PAID_SUM_SQL, invoice_id))
    balance = _money(row["total_amount"]) - paid
    if value > balance:
        raise Invalid("amount_above_balance", f"at most {balance} {row['currency']}", balance=str(balance))
    day = await _day(ctx, received_on)
    manual = providers.provider("manual")
    payment_id = uuid.uuid4()
    await ctx.conn.execute(INSERT_PAYMENT_SQL, payment_id, row["trip_id"], row["schedule_id"], invoice_id, value,
                           row["currency"], method, day, manual.name, ref, "payment", None)  # fmt: skip
    await ctx.conn.execute(COMMITMENT_SQL, row["trip_id"], "payment", "succeeded", f"payment:{payment_id}")
    ctx.audit("payment_record", "allowed", "invoice", str(invoice_id), reason=method)
    out: dict[str, Any] = {"payment_id": str(payment_id), "trip_moved": None, "portal": None, "rehold_pending": False}
    if value == balance:
        await claims.close_verify(ctx, row["trip_id"], invoice_id, f"member:{ctx.member_id}")
        facts = await claims.window_facts(ctx, invoice_id)
        if facts is not None and claims.holds_aa(facts) and not _hold_live(facts):
            # AA-952 slice 4 (card E): the money settles a deposit whose AA hold ran out (also after the one
            # extension). The Booking sync holds the accepted AA items again, then pays the invoice and settles the
            # row, or marks it paid_unfulfillable with a refund request (app.booking.rehold). Nothing settles here.
            await ctx.conn.execute(REHOLD_SQL, invoice_id)
            ctx.audit("invoice_rehold", "allowed", "invoice", str(invoice_id), reason="hold_expired")
            from app.booking import proposal_lines  # local: proposal_lines reads the builder, which imports proposals

            await proposal_lines.nudge(ctx, f"rehold:{invoice_id}")
            out["rehold_pending"] = True
        else:
            await ctx.conn.execute(INVOICE_PAID_SQL, invoice_id)
            await ctx.conn.execute(COMMITMENT_SQL, row["trip_id"], "invoice", "paid", invoices.source_ref(invoice_id))
            if schedule is not None:
                out.update(await proposals.settle_schedule(ctx, vault, schedule))
    out["invoice"] = await invoices.full(ctx, invoice_id)
    return out


def _hold_live(facts: Any) -> bool:
    expires = facts["hold_expires_at"]
    return facts["hold_id"] is not None and expires is not None and expires > dt.datetime.now(dt.UTC)


# ── refunds ───────────────────────────────────────────────────────────────────────────────────────


async def request_refund(ctx: TenantContext, payment_id: uuid.UUID, *, amount: Any, reason: Any) -> dict[str, Any]:
    value = _amount(amount)
    text = _note(reason, "reason", MAX_NOTE, required=True)
    payment = await ctx.conn.fetchrow(PAYMENT_SQL, payment_id)
    if payment is None or payment["entry_kind"] != "payment":
        raise NotFound("payment_not_found")
    if payment["invoice_id"] is None:
        raise Conflict("payment_without_invoice", "raise an invoice for this payment's schedule row first")
    # The invoice row lock serialises every money step of the invoice (payments, requests, executions).
    if await ctx.conn.fetchrow(INVOICE_LOCK_SQL, payment["invoice_id"]) is None:
        raise NotFound("invoice_not_found")
    left = _money(payment["amount"]) - Decimal(await ctx.conn.fetchval(ASKED_SQL, payment_id))
    if value > left:
        raise Invalid("amount_above_refundable", f"at most {left} {payment['currency']}", refundable=str(left))
    refund_id = uuid.uuid4()
    await ctx.conn.execute(INSERT_REFUND_SQL, refund_id, payment["trip_id"], payment["invoice_id"], payment_id, value,
                           payment["currency"], text)  # fmt: skip
    ctx.audit("refund_request", "allowed", "refund_request", str(refund_id))
    return await _refund(ctx, refund_id)


async def _refund(ctx: AnyContext, refund_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(REFUND_ONE_SQL, refund_id)
    if row is None:
        raise NotFound("refund_not_found")
    return invoices.refund_view(row)


async def base_amount(ctx: AnyContext, amount: Decimal, currency: str) -> tuple[str, Decimal | None]:
    """The refund in the agency's base currency (the threshold's currency) at today's rate, or None without a rate."""
    base = (await fx.settings(ctx))["base_currency"]
    if currency == base:
        return base, amount
    rate = await fx.resolve(ctx, currency, base, await fx.agency_today(ctx))
    if rate is None:
        return base, None
    return base, _money(amount * rate.rate)


async def decide_refund(ctx: TenantContext, refund_id: uuid.UUID, *, approve: bool, note: Any) -> dict[str, Any]:
    text = _note(note, "note", MAX_NOTE, required=False)
    row = await ctx.conn.fetchrow(REFUND_LOCK_SQL, refund_id)
    if row is None:
        raise NotFound("refund_not_found")
    if row["status"] != "proposed":
        raise Conflict("refund_not_proposed", "this refund request is decided already")
    # The invoice row lock serialises the decisions on its requests (the running total below).
    if await ctx.conn.fetchrow(INVOICE_LOCK_SQL, row["invoice_id"]) is None:
        raise NotFound("invoice_not_found")
    limited = rule_for(ctx.member_role or "", Action.REFUND_APPROVE) is Rule.THRESHOLD
    if approve and limited and row["requested_by"] == f"member:{ctx.member_id}":
        ctx.audit(Action.REFUND_APPROVE.value, "denied", "refund_request", str(refund_id),
                  reason="refund_self_approval")  # fmt: skip
        raise Forbidden("refund_self_approval", "another member approves a refund you asked for")
    base, in_base = await base_amount(ctx, _money(row["amount"]), row["currency"])
    judged = in_base
    if approve and in_base is not None:
        before = await ctx.conn.fetchval(APPROVED_BASE_SQL, row["invoice_id"], refund_id, base)
        judged = None if before is None else in_base + Decimal(before)
    threshold = await ctx.conn.fetchval(THRESHOLD_SQL)
    # §4.6: the owner any amount, an agent up to the threshold on the invoice's approved refunds plus this one (no rate
    # for the day: the owner decides). A rejection moves no money: it is judged on this request alone.
    require(
        ctx,
        Action.REFUND_APPROVE,
        object_kind="refund_request",
        object_id=str(refund_id),
        amount=judged,
        threshold=DEFAULT_THRESHOLD if threshold is None else threshold,
    )
    status = "approved" if approve else "rejected"
    await ctx.conn.execute(DECIDE_SQL, refund_id, status, f"member:{ctx.member_id}", text,
                           base if in_base is not None else None, in_base)  # fmt: skip
    ctx.audit(f"refund_{'approve' if approve else 'reject'}", "allowed", "refund_request", str(refund_id))
    return await _refund(ctx, refund_id)


async def execute_refund(
    ctx: TenantContext,
    refund_id: uuid.UUID,
    *,
    method: str,
    refunded_on: dt.date | None,
    reference: Any = None,
) -> dict[str, Any]:
    _method(method)
    ref = _note(reference, "reference", MAX_REFERENCE, required=False)
    row = await ctx.conn.fetchrow(REFUND_LOCK_SQL, refund_id)
    if row is None:
        raise NotFound("refund_not_found")
    if row["status"] != "approved":
        raise Conflict("refund_not_approved", "a refund is executed once it is approved")
    if await ctx.conn.fetchrow(INVOICE_LOCK_SQL, row["invoice_id"]) is None:
        raise NotFound("invoice_not_found")
    payment = await ctx.conn.fetchrow(PAYMENT_SQL, row["payment_id"])
    if payment is None:  # pragma: no cover — the foreign key keeps it
        raise NotFound("payment_not_found")
    day = await _day(ctx, refunded_on)
    amount = _money(row["amount"])
    result = await providers.provider(payment["provider"]).refund(
        amount=amount, currency=row["currency"], original_ref=payment["provider_ref"], reference=ref
    )
    entry_id = uuid.uuid4()
    await ctx.conn.execute(INSERT_PAYMENT_SQL, entry_id, row["trip_id"], payment["schedule_id"], row["invoice_id"],
                           amount, row["currency"], method, day, payment["provider"], result.provider_ref, "refund",
                           row["payment_id"])  # fmt: skip
    await ctx.conn.execute(EXECUTE_SQL, refund_id, entry_id)
    refunded = Decimal(await ctx.conn.fetchval(REFUNDED_SQL, row["payment_id"]))
    if refunded >= _money(payment["amount"]):
        await ctx.conn.execute(COMMITMENT_SQL, row["trip_id"], "payment", "refunded", f"payment:{row['payment_id']}")
    ctx.audit("refund_execute", "allowed", "refund_request", str(refund_id), reason=method)
    return await _refund(ctx, refund_id)


async def list_refunds(ctx: TenantContext, status: str | None) -> dict[str, Any]:
    """The refund requests to act on (default: proposed and approved), newest first."""
    if status is None:
        wanted = ["proposed", "approved"]
    elif status in REFUND_STATUSES:
        wanted = [status]
    else:
        raise Invalid("invalid_status", ", ".join(REFUND_STATUSES))
    rows = await ctx.conn.fetch(REFUND_LIST_SQL, wanted, MAX_LIST)
    ctx.audit("list_view", "allowed", "refund_request", None, object_ids=tuple(str(r["refund_id"]) for r in rows))
    return {
        "status": status,
        "refund_requests": [
            {
                **invoices.refund_view(r),
                "trip_id": str(r["trip_id"]),
                "trip_title": r["trip_title"],
                "invoice_number": r["number"],
            }  # fmt: skip
            for r in rows
        ],
    }


# ── payment methods ───────────────────────────────────────────────────────────────────────────────


def _account_view(r: Any) -> dict[str, Any]:
    return {
        "account_id": str(r["account_id"]),
        "provider": r["provider"],
        "status": r["status"],
        "display_name": r["display_name"],
        "instructions": r["instructions"],
        "external_account_id": r["external_account_id"],
        "default_currency": r["default_currency"],
        "charges_enabled": r["charges_enabled"],
        "connected_at": r["connected_at"].isoformat() if r["connected_at"] else None,
        "disconnected_at": r["disconnected_at"].isoformat() if r["disconnected_at"] else None,
    }


async def payment_methods(ctx: TenantContext) -> dict[str, Any]:
    rows = await ctx.conn.fetch(ACCOUNTS_SQL)
    return {"accounts": [_account_view(r) for r in rows], "providers": providers.available()}


async def save_manual(ctx: TenantContext, *, display_name: Any, instructions: Any) -> dict[str, Any]:
    name = _note(display_name, "display_name", 80, required=True)
    assert name is not None  # noqa: S101 — required above
    if "\n" in name:
        raise Invalid("invalid_display_name", "one line")
    text = _note(instructions, "instructions", 2000, required=True)
    current = await ctx.conn.fetchval(ACTIVE_MANUAL_SQL)
    if current is None:
        current = uuid.uuid4()
        await ctx.conn.execute(INSERT_ACCOUNT_SQL, current, name, text)
    else:
        await ctx.conn.execute(UPDATE_ACCOUNT_SQL, current, name, text)
    ctx.audit("update", "allowed", "payment_provider_account", str(current), fields=("display_name", "instructions"))
    return await payment_methods(ctx)


async def connect(ctx: TenantContext, provider_name: str) -> dict[str, Any]:
    """A provider account of the agency's own (BYO). Only ``manual`` exists today (:func:`save_manual`)."""
    impl = providers.provider(provider_name)
    if not impl.needs_external_account:
        raise Conflict("use_manual_settings", "the manual method is saved with its instructions")
    raise Conflict("provider_not_available")  # pragma: no cover — no provider with an external account has code yet


async def disconnect(ctx: TenantContext, account_id: uuid.UUID) -> dict[str, Any]:
    if await ctx.conn.fetchval(DISCONNECT_SQL, account_id) is None:
        raise NotFound("account_not_found")
    ctx.audit("update", "allowed", "payment_provider_account", str(account_id), fields=("status",))
    return await payment_methods(ctx)
