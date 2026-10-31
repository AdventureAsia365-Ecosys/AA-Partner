"""Agency invoices, the agency side (card CRM-23 slice 2, AA-952; design v3 group F, M7, R27, A-10 Receivables, the
trip's Proposal tab). Routes in :mod:`app.routers.invoices`; the document in :mod:`app.invoices.render`.

- **From a schedule row** (deposit / balance of the accepted proposal, still due): one charge line with the row's
  amount, currency and due date, always tax-inclusive (it bills what the client accepted), plus "included" lines: the
  titles of the trip's live agency and AA booking lines, without prices (design §2.9 rule 3). One live invoice per
  schedule row (409 ``invoice_exists``; a void one frees it).
- **Service fee / adjustment**: charge lines the member types (quantity x unit amount), the agency's tax setting. A
  service fee needs Settings > Payments "service fees" on (409 ``service_fee_disabled``, design Fix E3).
- **Draft** edits (memo, due date, tax, the lines of a typed invoice) -> **issue**: the next number
  (``crm.invoice_sequence``, gapless under the row lock), today's date in the agency timezone, a due date no earlier
  than today; the database checks the lines against the totals (CRM39) and seals the invoice (CRM31) -> **void** a
  draft or an issued invoice with a reason (never one that still holds money: 409 ``invoice_has_payments``, payments
  net of their refunds; nor one with a refund request still open: 409 ``refund_pending``).
- The trip's ``crm.trip_commitment`` row (kind ``invoice``) follows the status: a paid invoice blocks the trip delete.
- Slice 3 (:mod:`app.payments.service`): every view carries the invoice's payments, refunds and refund requests with
  ``paid_amount`` / ``refunded_amount`` / ``balance_amount`` (total - payments; a refund never reopens an invoice), and
  the document prints them with the agency's own pay-by instructions (its manual payment method).

Every function takes the open context and never opens one. Amounts are the client's sell amounts (no net, cost or
margin key: nothing for the margin serializer to strip). Totals are computed here, rounded half up to the cent.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg

from app import stages
from app.errors import Conflict, Invalid, NotFound
from app.invoices import render
from app.ledger import fx
from app.payments import claims
from app.tenancy import AnyContext, TenantContext
from app.vault import Vault

CENT = Decimal("0.01")
KINDS = ("deposit", "balance", "service_fee", "adjustment")
TYPED_KINDS = ("service_fee", "adjustment")
STATUSES = ("draft", "open", "payment_claimed", "paid", "paid_unfulfillable", "void", "expired")
RECEIVABLE = ("open", "payment_claimed", "expired")
MAX_LINES = 50
MAX_INCLUDED = 40
MAX_DESCRIPTION = 300
MAX_MEMO = 2000
MAX_QUANTITY = Decimal("9999")
MAX_AMOUNT = Decimal("999999999999.99")
MAX_LIST = 200
PREFIX = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,11}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
SEALED_SQLSTATES = ("CRM31", "CRM39")
DEFAULT_SETTINGS: dict[str, Any] = {
    "invoice_prefix": "INV",
    "invoice_tax_label": "Tax",
    "invoice_tax_rate": Decimal("0"),
    "invoice_tax_inclusive": True,
    "invoice_due_days": 7,
    "service_fee_enabled": False,
}
SCHEDULE_LABEL = {"deposit": "Deposit", "balance": "Balance"}

SETTINGS_SQL = """
SELECT invoice_prefix, invoice_tax_label, invoice_tax_rate, invoice_tax_inclusive, invoice_due_days,
       service_fee_enabled
  FROM crm.tenant_settings
"""
UPSERT_SETTINGS_SQL = """
INSERT INTO crm.tenant_settings (invoice_prefix, invoice_tax_label, invoice_tax_rate, invoice_tax_inclusive,
                                 invoice_due_days, service_fee_enabled)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (tenant_id) DO UPDATE
   SET invoice_prefix = EXCLUDED.invoice_prefix, invoice_tax_label = EXCLUDED.invoice_tax_label,
       invoice_tax_rate = EXCLUDED.invoice_tax_rate, invoice_tax_inclusive = EXCLUDED.invoice_tax_inclusive,
       invoice_due_days = EXCLUDED.invoice_due_days, service_fee_enabled = EXCLUDED.service_fee_enabled
"""
TRIP_SQL = "SELECT trip_id, stage FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
SCHEDULE_SQL = """
SELECT s.schedule_id, s.trip_id, s.proposal_id, s.kind, s.amount, s.currency, s.due_on, s.status,
       p.status AS proposal_status, p.version
  FROM crm.payment_schedule s
  JOIN crm.proposal p ON p.proposal_id = s.proposal_id
 WHERE s.schedule_id = $1 AND s.trip_id = $2 AND s.deleted_at IS NULL
   FOR UPDATE OF s
"""
LIVE_FOR_SCHEDULE_SQL = """
SELECT invoice_id FROM crm.invoice WHERE schedule_id = $1 AND status <> 'void' AND deleted_at IS NULL
"""
INCLUDED_SQL = """
SELECT title, supplier_kind FROM crm.booking_line
 WHERE trip_id = $1 AND deleted_at IS NULL AND status <> 'cancelled'
 ORDER BY service_from NULLS LAST, created_at, booking_line_id
 LIMIT $2
"""
INSERT_SQL = """
INSERT INTO crm.invoice (invoice_id, trip_id, kind, schedule_id, proposal_id, currency, tax_rate, tax_label,
                         tax_inclusive, memo, due_on)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""
INSERT_LINE_SQL = """
INSERT INTO crm.invoice_line (invoice_id, trip_id, position, kind, source, description, quantity, unit_amount, amount)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""
DELETE_LINES_SQL = "DELETE FROM crm.invoice_line WHERE invoice_id = $1"
LINES_SQL = """
SELECT line_id, position, kind, source, description, quantity, unit_amount, amount FROM crm.invoice_line
 WHERE invoice_id = $1 ORDER BY position
"""
# AA-952 slice 4 (crm-028): the claim / hold / re-hold facts and the proposal's AA hold (the pay window, derived in
# app.payments.claims) ride along; scalar sub-selects, so the row lock stays on the invoice alone.
_COLUMNS = """
SELECT invoice_id, trip_id, kind, status, schedule_id, proposal_id, number, currency, subtotal_amount, tax_amount,
       total_amount, tax_rate, tax_label, tax_inclusive, memo, due_on, issue_date, issued_at, paid_at, voided_at,
       void_reason, created_at, updated_at, claimed_at, hold_extended_at, hold_extend_code, rehold_requested_at,
       rehold_code,
       (SELECT p.kind FROM crm.proposal p WHERE p.proposal_id = invoice.proposal_id) AS proposal_kind,
       (SELECT p.accepted_hold_id FROM crm.proposal p WHERE p.proposal_id = invoice.proposal_id) AS accepted_hold_id,
       (SELECT p.hold_id FROM crm.proposal p WHERE p.proposal_id = invoice.proposal_id) AS hold_id,
       (SELECT p.hold_expires_at FROM crm.proposal p WHERE p.proposal_id = invoice.proposal_id) AS hold_expires_at
"""
ONE_SQL = _COLUMNS + "  FROM crm.invoice WHERE invoice_id = $1 AND deleted_at IS NULL\n"
LOCK_SQL = ONE_SQL.rstrip() + "\n   FOR UPDATE"
TRIP_LIST_SQL = (
    _COLUMNS
    + """  FROM crm.invoice WHERE trip_id = $1 AND deleted_at IS NULL
 ORDER BY created_at DESC
 LIMIT $2
"""
)
AGENCY_LIST_SQL = """
SELECT i.invoice_id, i.trip_id, i.kind, i.status, i.number, i.currency, i.total_amount, i.due_on, i.issue_date,
       i.paid_at, t.title AS trip_title, i.schedule_id, i.claimed_at, i.hold_extended_at, i.hold_extend_code,
       i.rehold_requested_at, i.rehold_code, p.kind AS proposal_kind, p.accepted_hold_id, p.hold_id,
       p.hold_expires_at,
       (SELECT coalesce(sum(x.amount), 0) FROM crm.payment x
         WHERE x.invoice_id = i.invoice_id AND x.entry_kind = 'payment') AS paid_amount
  FROM crm.invoice i JOIN crm.trip t ON t.trip_id = i.trip_id AND t.deleted_at IS NULL
  LEFT JOIN crm.proposal p ON p.proposal_id = i.proposal_id
 WHERE i.deleted_at IS NULL AND i.status = ANY($1::text[])
 ORDER BY i.due_on NULLS LAST, i.created_at
 LIMIT $2
"""
TOTALS_SQL = """
UPDATE crm.invoice SET subtotal_amount = $2, tax_amount = $3, total_amount = $4 WHERE invoice_id = $1
"""
DRAFT_UPDATE_SQL = """
UPDATE crm.invoice SET memo = $2, due_on = $3, tax_rate = $4, tax_label = $5, tax_inclusive = $6 WHERE invoice_id = $1
"""
NEXT_NUMBER_SQL = """
INSERT INTO crm.invoice_sequence (last_number) VALUES (1)
ON CONFLICT (tenant_id) DO UPDATE SET last_number = crm.invoice_sequence.last_number + 1
RETURNING last_number
"""
ISSUE_SQL = """
UPDATE crm.invoice SET status = 'open', number_seq = $2, number = $3, issue_date = $4, due_on = $5, issued_at = now(),
       issued_by = $6
 WHERE invoice_id = $1
RETURNING issued_at
"""
VOID_SQL = """
UPDATE crm.invoice SET status = 'void', voided_at = now(), voided_by = $2, void_reason = $3 WHERE invoice_id = $1
RETURNING voided_at
"""
# What still holds a void back (slice 3): the money the invoice kept (payments - refund entries) and a refund request
# not yet executed or rejected. A partly paid invoice whose payments were all refunded can be voided.
VOID_BLOCK_SQL = """
SELECT (SELECT coalesce(sum(CASE WHEN entry_kind = 'payment' THEN amount ELSE -amount END), 0) FROM crm.payment
         WHERE invoice_id = $1) AS held,
       EXISTS (SELECT 1 FROM crm.refund_request
                WHERE invoice_id = $1 AND status IN ('proposed', 'approved') AND deleted_at IS NULL) AS refund_open
"""
PAYMENTS_SQL = """
SELECT payment_id, invoice_id, entry_kind, amount, currency, method, received_on, provider, provider_ref, refund_of,
       created_at
  FROM crm.payment WHERE invoice_id = $1 ORDER BY received_on, created_at
"""
TRIP_PAYMENTS_SQL = """
SELECT payment_id, invoice_id, entry_kind, amount, currency, method, received_on, provider, provider_ref, refund_of,
       created_at
  FROM crm.payment WHERE trip_id = $1 AND invoice_id IS NOT NULL ORDER BY received_on, created_at
"""
REFUNDS_SQL = """
SELECT refund_id, invoice_id, payment_id, amount, currency, reason, status, requested_by, decided_by, decided_at,
       decision_note, executed_at, refund_payment_id, created_at
  FROM crm.refund_request WHERE invoice_id = $1 AND deleted_at IS NULL ORDER BY created_at
"""
TRIP_REFUNDS_SQL = """
SELECT refund_id, invoice_id, payment_id, amount, currency, reason, status, requested_by, decided_by, decided_at,
       decision_note, executed_at, refund_payment_id, created_at
  FROM crm.refund_request WHERE trip_id = $1 AND deleted_at IS NULL ORDER BY created_at
"""
PAY_BY_SQL = """
SELECT display_name, instructions FROM crm.payment_provider_account
 WHERE status = 'active' AND deleted_at IS NULL AND instructions IS NOT NULL
 ORDER BY provider, created_at
"""
COMMITMENT_SQL = """
INSERT INTO crm.trip_commitment (trip_id, kind, line_status, source_ref) VALUES ($1, 'invoice', $2, $3)
ON CONFLICT (tenant_id, kind, source_ref) DO UPDATE SET line_status = EXCLUDED.line_status
"""


def money(value: Any) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def plain(value: Any) -> str:
    """A number without trailing zeros and never in exponent form (10.000 -> "10", 2.50 -> "2.5")."""
    return f"{Decimal(value).normalize():f}"


def _money_text(value: Any) -> str | None:
    return None if value is None else str(money(value))


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def tax_split(line_sum: Decimal, rate: Decimal, inclusive: bool) -> tuple[Decimal, Decimal, Decimal]:
    """(subtotal, tax, total) of the charge lines: inclusive = the tax is inside the line sum; else it is added."""
    line_sum = money(line_sum)
    if inclusive:
        tax = money(line_sum * rate / (Decimal(100) + rate)) if rate else Decimal("0.00")
        return line_sum - tax, tax, line_sum
    tax = money(line_sum * rate / Decimal(100))
    return line_sum, tax, line_sum + tax


def number_text(prefix: str, seq: int) -> str:
    return f"{prefix}-{seq:06d}"


def source_ref(invoice_id: uuid.UUID | str) -> str:
    return f"invoice:{invoice_id}"


def _text(value: Any, field: str, max_len: int, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise Invalid(f"invalid_{field}", "required")
        return None
    if not isinstance(value, str):
        raise Invalid(f"invalid_{field}", "text")
    text = value.strip()
    if not text:
        if required:
            raise Invalid(f"invalid_{field}", "required")
        return None
    if len(text) > max_len:
        raise Invalid(f"{field}_too_long", f"at most {max_len} characters")
    return text


def _one_line(value: Any, field: str, max_len: int) -> str:
    text = _text(value, field, max_len)
    assert text is not None  # noqa: S101 — required above
    if CONTROL.search(text):
        raise Invalid(f"invalid_{field}", "one line of text")
    return text


def _rate(value: Any) -> Decimal:
    try:
        rate = Decimal(str(value))
    except ArithmeticError:
        raise Invalid("invalid_tax_rate", "a percentage from 0 to 100") from None
    if not rate.is_finite() or rate < 0 or rate > 100 or rate != rate.quantize(Decimal("0.001")):
        raise Invalid("invalid_tax_rate", "a percentage from 0 to 100, at most three decimals")
    return rate


def charge_lines(raw: Any) -> list[dict[str, Any]]:
    """Validate typed charge lines: description, quantity (> 0, two decimals), unit amount (> 0, cents)."""
    if not isinstance(raw, list) or not raw:
        raise Invalid("invalid_lines", "one charge line or more")
    if len(raw) > MAX_LINES:
        raise Invalid("too_many_lines", f"at most {MAX_LINES} lines")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise Invalid("invalid_lines", "description, quantity, unit_amount")
        description = _one_line(item.get("description"), "description", MAX_DESCRIPTION)
        try:
            quantity = Decimal(str(item.get("quantity", "1")))
            unit = Decimal(str(item.get("unit_amount")))
        except ArithmeticError:
            raise Invalid("invalid_amount", "numbers") from None
        if not (quantity.is_finite() and unit.is_finite()):
            raise Invalid("invalid_amount", "numbers")
        if quantity <= 0 or quantity > MAX_QUANTITY or quantity != quantity.quantize(CENT):
            raise Invalid("invalid_quantity", "more than 0, at most 9999, two decimals")
        if unit <= 0 or unit > MAX_AMOUNT or unit != unit.quantize(CENT):
            raise Invalid("invalid_unit_amount", "more than 0, in cents")
        out.append({"description": description, "quantity": quantity, "unit_amount": unit,
                    "amount": money(quantity * unit)})  # fmt: skip
    if sum((line["amount"] for line in out), Decimal(0)) > MAX_AMOUNT:
        raise Invalid("invalid_amount", "the total is too large")
    return out


# ── settings ──────────────────────────────────────────────────────────────────────────────────────


async def settings(ctx: AnyContext) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(SETTINGS_SQL)
    if row is None:
        return dict(DEFAULT_SETTINGS)
    return {key: row[key] for key in DEFAULT_SETTINGS}


def settings_view(values: dict[str, Any]) -> dict[str, Any]:
    return {**values, "invoice_tax_rate": plain(values["invoice_tax_rate"])}


async def get_settings(ctx: TenantContext) -> dict[str, Any]:
    return settings_view(await settings(ctx))


async def update_settings(ctx: TenantContext, changes: dict[str, Any]) -> dict[str, Any]:
    merged = {**await settings(ctx), **{k: v for k, v in changes.items() if k in DEFAULT_SETTINGS and v is not None}}
    prefix = merged["invoice_prefix"]
    if not isinstance(prefix, str) or not PREFIX.match(prefix):
        raise Invalid("invalid_invoice_prefix", "1-12 characters: A-Z, 0-9 and -, starting with a letter or digit")
    merged["invoice_tax_label"] = _one_line(merged["invoice_tax_label"], "invoice_tax_label", 24)
    merged["invoice_tax_rate"] = _rate(merged["invoice_tax_rate"])
    days = merged["invoice_due_days"]
    if not isinstance(days, int) or isinstance(days, bool) or not 0 <= days <= 365:
        raise Invalid("invalid_invoice_due_days", "0 to 365 days")
    for key in ("invoice_tax_inclusive", "service_fee_enabled"):
        if not isinstance(merged[key], bool):
            raise Invalid(f"invalid_{key}", "true or false")
    await ctx.conn.execute(
        UPSERT_SETTINGS_SQL,
        prefix,
        merged["invoice_tax_label"],
        merged["invoice_tax_rate"],
        merged["invoice_tax_inclusive"],
        days,
        merged["service_fee_enabled"],
    )
    ctx.audit("update", "allowed", "tenant_settings", str(ctx.tenant_id), fields=tuple(sorted(changes)))
    return settings_view(merged)


# ── views ─────────────────────────────────────────────────────────────────────────────────────────


def _line_view(r: Any) -> dict[str, Any]:
    return {
        "line_id": str(r["line_id"]),
        "position": r["position"],
        "kind": r["kind"],
        "source": r["source"],
        "description": r["description"],
        "quantity": None if r["quantity"] is None else plain(r["quantity"]),
        "unit_amount": _money_text(r["unit_amount"]),
        "amount": _money_text(r["amount"]),
    }


def actions(row: Any, held: Decimal | None = None) -> list[str]:
    """What a member may do next. ``held`` = the money the invoice keeps: payments - refund entries (None when
    unknown: a list without money). A fully refunded invoice can be voided."""
    status = row["status"]
    if status == "draft":
        return ["edit", "issue", "void"]
    if row.get("rehold_requested_at") is not None and status in ("open", "payment_claimed"):
        return []  # AA-952 slice 4: paid in full, the Booking sync is placing the AA hold again
    if status == "payment_claimed":
        # The client says they paid: record what arrived, or "not received" (back to open, then it can be voided).
        return ["record_payment", "reject_claim"]
    if status == "open":
        return ["record_payment"] + (["void"] if not held else [])
    if status == "expired":
        return [] if held else ["void"]
    return []


def payment_view(r: Any) -> dict[str, Any]:
    return {
        "payment_id": str(r["payment_id"]),
        "entry_kind": r["entry_kind"],
        "amount": _money_text(r["amount"]),
        "currency": r["currency"],
        "method": r["method"],
        "received_on": _iso(r["received_on"]),
        "provider": r["provider"],
        "reference": r["provider_ref"],
        "refund_of": str(r["refund_of"]) if r["refund_of"] else None,
    }


def refund_view(r: Any) -> dict[str, Any]:
    return {
        "refund_id": str(r["refund_id"]),
        "invoice_id": str(r["invoice_id"]),
        "payment_id": str(r["payment_id"]),
        "amount": _money_text(r["amount"]),
        "currency": r["currency"],
        "reason": r["reason"],
        "status": r["status"],
        "requested_by": r["requested_by"],
        "decided_by": r["decided_by"],
        "decided_at": _iso(r["decided_at"]),
        "decision_note": r["decision_note"],
        "executed_at": _iso(r["executed_at"]),
    }


def money_summary(total: Any, payments: list[Any], refunds: list[Any]) -> dict[str, Any]:
    """paid = money in, refunded = money returned, balance = total - paid (never below 0: a refund never reopens an
    invoice); per payment, what can still be asked back (its amount - its refunds asked, rejected ones aside)."""
    paid = sum((Decimal(p["amount"]) for p in payments if p["entry_kind"] == "payment"), Decimal(0))
    refunded = sum((Decimal(p["amount"]) for p in payments if p["entry_kind"] == "refund"), Decimal(0))
    asked: dict[Any, Decimal] = {}
    for r in refunds:
        if r["status"] != "rejected":
            asked[r["payment_id"]] = asked.get(r["payment_id"], Decimal(0)) + Decimal(r["amount"])
    views = []
    for p in payments:
        view = payment_view(p)
        if p["entry_kind"] == "payment":
            view["refundable_amount"] = _money_text(max(Decimal(p["amount"]) - asked.get(p["payment_id"], 0), 0))
        views.append(view)
    return {
        "paid_amount": _money_text(paid),
        "refunded_amount": _money_text(refunded),
        "balance_amount": _money_text(max(money(total) - paid, Decimal(0))),
        "payments": views,
        "refund_requests": [refund_view(r) for r in refunds],
        "_held": paid - refunded,
    }


def invoice_view(
    row: Any,
    lines: list[Any] | None = None,
    payments: list[Any] | None = None,
    refunds: list[Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "invoice_id": str(row["invoice_id"]),
        "trip_id": str(row["trip_id"]),
        "kind": row["kind"],
        "status": row["status"],
        "number": row["number"],
        "schedule_id": str(row["schedule_id"]) if row["schedule_id"] else None,
        "proposal_id": str(row["proposal_id"]) if row["proposal_id"] else None,
        "currency": row["currency"],
        "subtotal_amount": _money_text(row["subtotal_amount"]),
        "tax_amount": _money_text(row["tax_amount"]),
        "total_amount": _money_text(row["total_amount"]),
        "tax_rate": plain(row["tax_rate"]),
        "tax_label": row["tax_label"],
        "tax_inclusive": row["tax_inclusive"],
        "memo": row["memo"],
        "due_on": _iso(row["due_on"]),
        "issue_date": _iso(row["issue_date"]),
        "issued_at": _iso(row["issued_at"]),
        "paid_at": _iso(row["paid_at"]),
        "voided_at": _iso(row["voided_at"]),
        "void_reason": row["void_reason"],
        "created_at": _iso(row["created_at"]),
        "actions": actions(row),
        **claims.agency_view(row, dt.datetime.now(dt.UTC)),
    }
    if lines is not None:
        out["lines"] = [_line_view(r) for r in lines]
    if payments is not None:
        summary = money_summary(row["total_amount"], payments, refunds or [])
        out["actions"] = actions(row, summary.pop("_held"))
        out.update(summary)
    return out


async def _row(ctx: AnyContext, invoice_id: uuid.UUID, *, lock: bool = False) -> Any:
    row = await ctx.conn.fetchrow(LOCK_SQL if lock else ONE_SQL, invoice_id)
    if row is None:
        raise NotFound("invoice_not_found")
    return row


async def full(ctx: AnyContext, invoice_id: uuid.UUID) -> dict[str, Any]:
    row = await _row(ctx, invoice_id)
    return invoice_view(
        row,
        await ctx.conn.fetch(LINES_SQL, invoice_id),
        await ctx.conn.fetch(PAYMENTS_SQL, invoice_id),
        await ctx.conn.fetch(REFUNDS_SQL, invoice_id),
    )


async def get(ctx: TenantContext, invoice_id: uuid.UUID) -> dict[str, Any]:
    out = await full(ctx, invoice_id)
    ctx.audit("view", "allowed", "invoice", str(invoice_id))
    return out


async def list_for_trip(ctx: TenantContext, trip_id: uuid.UUID) -> dict[str, Any]:
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    rows = await ctx.conn.fetch(TRIP_LIST_SQL, trip_id, MAX_LIST)
    payments = await ctx.conn.fetch(TRIP_PAYMENTS_SQL, trip_id)
    refunds = await ctx.conn.fetch(TRIP_REFUNDS_SQL, trip_id)
    ctx.audit("list_view", "allowed", "invoice", str(trip_id), object_ids=tuple(str(r["invoice_id"]) for r in rows))
    return {
        "trip_id": str(trip_id),
        "invoices": [
            invoice_view(
                r,
                None,
                [p for p in payments if p["invoice_id"] == r["invoice_id"]],
                [x for x in refunds if x["invoice_id"] == r["invoice_id"]],
            )
            for r in rows
        ],
    }


async def receivables(ctx: TenantContext, status: str | None) -> dict[str, Any]:
    """A-10 Receivables: the agency's invoices by due date (default: the issued, unpaid ones)."""
    if status is None:
        wanted = list(RECEIVABLE)
    elif status in STATUSES:
        wanted = [status]
    else:
        raise Invalid("invalid_status", ", ".join(STATUSES))
    today = await fx.agency_today(ctx)
    now = dt.datetime.now(dt.UTC)
    rows = await ctx.conn.fetch(AGENCY_LIST_SQL, wanted, MAX_LIST)
    ctx.audit("list_view", "allowed", "invoice", None, object_ids=tuple(str(r["invoice_id"]) for r in rows))
    return {
        "today": today.isoformat(),
        "status": status,
        "invoices": [
            {
                "invoice_id": str(r["invoice_id"]),
                "trip_id": str(r["trip_id"]),
                "trip_title": r["trip_title"],
                "kind": r["kind"],
                "status": r["status"],
                "number": r["number"],
                "currency": r["currency"],
                "total_amount": _money_text(r["total_amount"]),
                "due_on": _iso(r["due_on"]),
                "issue_date": _iso(r["issue_date"]),
                "paid_at": _iso(r["paid_at"]),
                "balance_amount": _money_text(max(money(r["total_amount"]) - money(r["paid_amount"]), Decimal(0))),
                "overdue": r["status"] in RECEIVABLE and r["due_on"] is not None and r["due_on"] < today,
                **claims.agency_view(r, now),
            }
            for r in rows
        ],
    }


# ── drafts ────────────────────────────────────────────────────────────────────────────────────────


async def _write_totals(ctx: AnyContext, invoice_id: uuid.UUID, rate: Decimal, inclusive: bool) -> None:
    line_sum = sum(
        (Decimal(r["amount"]) for r in await ctx.conn.fetch(LINES_SQL, invoice_id) if r["kind"] == "charge"),
        Decimal(0),
    )
    subtotal, tax, total = tax_split(line_sum, rate, inclusive)
    await ctx.conn.execute(TOTALS_SQL, invoice_id, subtotal, tax, total)


async def _insert_lines(
    ctx: AnyContext,
    invoice_id: uuid.UUID,
    trip_id: uuid.UUID,
    charges: list[dict[str, Any]],
    *,
    source: str,
    start: int = 1,
) -> int:
    position = start
    for line in charges:
        await ctx.conn.execute(
            INSERT_LINE_SQL,
            invoice_id,
            trip_id,
            position,
            "charge",
            source,
            line["description"],
            line["quantity"],
            line["unit_amount"],
            line["amount"],
        )
        position += 1
    return position


async def _insert_included(ctx: AnyContext, invoice_id: uuid.UUID, trip_id: uuid.UUID, start: int) -> None:
    position = start
    for r in await ctx.conn.fetch(INCLUDED_SQL, trip_id, MAX_INCLUDED):
        title = CONTROL.sub(" ", r["title"] or "").strip()[:MAX_DESCRIPTION]
        if not title:
            continue
        source = "aa_line" if r["supplier_kind"] == "aa" else "agency_line"
        await ctx.conn.execute(INSERT_LINE_SQL, invoice_id, trip_id, position, "included", source, title, None, None,
                               None)  # fmt: skip
        position += 1


def _translate(exc: asyncpg.PostgresError) -> Exception:
    state = getattr(exc, "sqlstate", None)
    if state in SEALED_SQLSTATES:
        return Conflict("invoice_not_draft", "an issued or void invoice never changes")
    if state == "23505" and "invoice_schedule_live_uq" in str(exc):
        return Conflict("invoice_exists", "this schedule row has an invoice already (void it to raise a new one)")
    return exc


async def create_from_schedule(ctx: TenantContext, trip_id: uuid.UUID, schedule_id: uuid.UUID) -> dict[str, Any]:
    if await ctx.conn.fetchrow(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    row = await ctx.conn.fetchrow(SCHEDULE_SQL, schedule_id, trip_id)
    if row is None:
        raise NotFound("schedule_not_found")
    if row["status"] != "due":
        raise Conflict("schedule_not_due", "this row is paid or void already")
    if row["proposal_status"] != "accepted":
        raise Conflict("proposal_not_accepted")
    if await ctx.conn.fetchval(LIVE_FOR_SCHEDULE_SQL, schedule_id) is not None:
        raise Conflict("invoice_exists", "this schedule row has an invoice already (void it to raise a new one)")
    conf = await settings(ctx)
    invoice_id = uuid.uuid4()
    rate = Decimal(conf["invoice_tax_rate"])
    try:
        await ctx.conn.execute(
            INSERT_SQL,
            invoice_id,
            trip_id,
            row["kind"],
            schedule_id,
            row["proposal_id"],
            row["currency"],
            rate,
            conf["invoice_tax_label"],
            True,
            None,
            row["due_on"],
        )
    except asyncpg.PostgresError as exc:
        raise _translate(exc) from None
    amount = money(row["amount"])
    charge = {
        "description": f"{SCHEDULE_LABEL[row['kind']]} (proposal version {row['version']})",
        "quantity": Decimal(1),
        "unit_amount": amount,
        "amount": amount,
    }
    after = await _insert_lines(ctx, invoice_id, trip_id, [charge], source="schedule")
    await _insert_included(ctx, invoice_id, trip_id, after)
    await _write_totals(ctx, invoice_id, rate, True)
    ctx.audit("invoice_create", "allowed", "invoice", str(invoice_id), reason=row["kind"])
    await stages.touch(ctx, trip_id)
    return await full(ctx, invoice_id)


async def create_typed(
    ctx: TenantContext,
    trip_id: uuid.UUID,
    *,
    kind: str,
    currency: str,
    lines: Any,
    memo: Any = None,
    due_on: dt.date | None = None,
    include_trip_lines: bool = False,
) -> dict[str, Any]:
    if kind not in TYPED_KINDS:
        raise Invalid("invalid_kind", ", ".join(TYPED_KINDS))
    fx.currency(currency)
    if await ctx.conn.fetchrow(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    conf = await settings(ctx)
    if kind == "service_fee" and not conf["service_fee_enabled"]:
        raise Conflict("service_fee_disabled", "turn on service fees in Settings > Payments")
    charges = charge_lines(lines)
    text = _text(memo, "memo", MAX_MEMO, required=False)
    invoice_id = uuid.uuid4()
    rate = Decimal(conf["invoice_tax_rate"])
    inclusive = bool(conf["invoice_tax_inclusive"])
    await ctx.conn.execute(INSERT_SQL, invoice_id, trip_id, kind, None, None, currency, rate, conf["invoice_tax_label"],
                           inclusive, text, due_on)  # fmt: skip
    source = "service_fee" if kind == "service_fee" else "manual"
    after = await _insert_lines(ctx, invoice_id, trip_id, charges, source=source)
    if include_trip_lines:
        await _insert_included(ctx, invoice_id, trip_id, after)
    await _write_totals(ctx, invoice_id, rate, inclusive)
    ctx.audit("invoice_create", "allowed", "invoice", str(invoice_id), reason=kind)
    await stages.touch(ctx, trip_id)
    return await full(ctx, invoice_id)


async def update_draft(ctx: TenantContext, invoice_id: uuid.UUID, changes: dict[str, Any]) -> dict[str, Any]:
    row = await _row(ctx, invoice_id, lock=True)
    if row["status"] != "draft":
        raise Conflict("invoice_not_draft", "an issued or void invoice never changes")
    typed = row["kind"] in TYPED_KINDS
    if not typed and ("lines" in changes or "tax_inclusive" in changes):
        raise Conflict("schedule_invoice_fixed", "a deposit or balance invoice bills its schedule row, tax included")
    memo = _text(changes["memo"], "memo", MAX_MEMO, required=False) if "memo" in changes else row["memo"]
    due_on = changes.get("due_on", row["due_on"])
    rate = _rate(changes["tax_rate"]) if changes.get("tax_rate") is not None else Decimal(row["tax_rate"])
    label = (
        _one_line(changes["tax_label"], "tax_label", 24) if changes.get("tax_label") is not None else row["tax_label"]
    )
    inclusive = bool(changes["tax_inclusive"]) if changes.get("tax_inclusive") is not None else row["tax_inclusive"]
    try:
        await ctx.conn.execute(DRAFT_UPDATE_SQL, invoice_id, memo, due_on, rate, label, inclusive)
        if typed and changes.get("lines") is not None:
            charges = charge_lines(changes["lines"])
            had_included = any(r["kind"] == "included" for r in await ctx.conn.fetch(LINES_SQL, invoice_id))
            await ctx.conn.execute(DELETE_LINES_SQL, invoice_id)
            source = "service_fee" if row["kind"] == "service_fee" else "manual"
            after = await _insert_lines(ctx, invoice_id, row["trip_id"], charges, source=source)
            if had_included:
                await _insert_included(ctx, invoice_id, row["trip_id"], after)
        await _write_totals(ctx, invoice_id, rate, inclusive)
    except asyncpg.PostgresError as exc:
        raise _translate(exc) from None
    ctx.audit("invoice_update", "allowed", "invoice", str(invoice_id), fields=tuple(sorted(changes)))
    return await full(ctx, invoice_id)


# ── issue and void ────────────────────────────────────────────────────────────────────────────────


async def issue(ctx: TenantContext, invoice_id: uuid.UUID) -> dict[str, Any]:
    row = await _row(ctx, invoice_id, lock=True)
    if row["status"] != "draft":
        raise Conflict("invoice_not_draft", "this invoice is issued or void already")
    if row["schedule_id"] is not None:
        schedule = await ctx.conn.fetchrow(SCHEDULE_SQL, row["schedule_id"], row["trip_id"])
        if schedule is None or schedule["status"] != "due" or schedule["proposal_status"] != "accepted":
            raise Conflict("schedule_not_due", "the schedule row is paid, void or no longer of the accepted proposal")
    if money(row["total_amount"]) <= 0:
        raise Conflict("invoice_empty", "add a charge line first")
    conf = await settings(ctx)
    today = await fx.agency_today(ctx)
    due_on = row["due_on"] or today + dt.timedelta(days=int(conf["invoice_due_days"]))
    due_on = max(due_on, today)
    seq = int(await ctx.conn.fetchval(NEXT_NUMBER_SQL))
    number = number_text(conf["invoice_prefix"], seq)
    try:
        await ctx.conn.fetchval(ISSUE_SQL, invoice_id, seq, number, today, due_on, f"member:{ctx.member_id}")
    except asyncpg.PostgresError as exc:
        raise _translate(exc) from None
    await ctx.conn.execute(COMMITMENT_SQL, row["trip_id"], "open", source_ref(invoice_id))
    ctx.audit("invoice_issue", "allowed", "invoice", str(invoice_id), reason=row["kind"])
    await stages.touch(ctx, row["trip_id"])
    return await full(ctx, invoice_id)


async def void(ctx: TenantContext, invoice_id: uuid.UUID, reason: Any) -> dict[str, Any]:
    text = _one_line(reason, "reason", 500)
    row = await _row(ctx, invoice_id, lock=True)
    if row["status"] not in ("draft", *RECEIVABLE):
        raise Conflict("invoice_not_voidable", "a paid or void invoice stays: refunds go through a refund request")
    if row["status"] == "payment_claimed":
        # AA-952 slice 4: crm-024 has no payment_claimed -> void; the claim is answered first.
        raise Conflict("invoice_claimed", "the client says they paid: record the payment or mark it not received first")
    block = await ctx.conn.fetchrow(VOID_BLOCK_SQL, invoice_id)
    if block is not None and block["held"] > 0:
        raise Conflict("invoice_has_payments", "refund the payments first, then void the invoice")
    if block is not None and block["refund_open"]:
        raise Conflict("refund_pending", "execute or reject the open refund requests first")
    await ctx.conn.fetchval(VOID_SQL, invoice_id, f"member:{ctx.member_id}", text)
    if row["issued_at"] is not None:
        await ctx.conn.execute(COMMITMENT_SQL, row["trip_id"], "void", source_ref(invoice_id))
    ctx.audit("invoice_void", "allowed", "invoice", str(invoice_id), reason=row["status"])
    await stages.touch(ctx, row["trip_id"])
    return await full(ctx, invoice_id)


# ── the document ──────────────────────────────────────────────────────────────────────────────────

AGENCY_NAME_SQL = "SELECT display_name FROM crm.tenant"
LEAD_NAME_SQL = """
SELECT t.traveler_id, t.display_name_enc
  FROM crm.trip_traveler tt JOIN crm.traveler t ON t.traveler_id = tt.traveler_id
 WHERE tt.trip_id = $1 AND tt.role = 'lead' AND tt.deleted_at IS NULL AND t.deleted_at IS NULL
 LIMIT 1
"""


async def bill_to(ctx: TenantContext, vault: Vault, trip_id: uuid.UUID) -> str | None:
    """The trip lead's display name (what every agency view shows), read now: an invoice copies no name."""
    lead = await ctx.conn.fetchrow(LEAD_NAME_SQL, trip_id)
    if lead is None or lead["display_name_enc"] is None:
        return None
    name = await vault.decrypt("traveler", "display_name_enc", lead["traveler_id"], lead["display_name_enc"],
                               tenant_id=ctx.tenant_id)  # fmt: skip
    return name.strip() or None


async def document(ctx: TenantContext, vault: Vault, invoice_id: uuid.UUID) -> dict[str, Any]:
    """The printable HTML (a JSON envelope: the web opens it in its own tab, the browser prints or saves a PDF)."""
    invoice = await full(ctx, invoice_id)
    agency = await ctx.conn.fetchval(AGENCY_NAME_SQL)
    pay_by = (
        [
            {"display_name": r["display_name"], "instructions": r["instructions"]}
            for r in await ctx.conn.fetch(PAY_BY_SQL)
        ]
        if invoice["status"] in RECEIVABLE or invoice["status"] == "draft"
        else []
    )
    html = render.render(
        invoice,
        agency_name=agency,
        bill_to=await bill_to(ctx, vault, uuid.UUID(invoice["trip_id"])),
        pay_instructions=pay_by,
        payments=invoice["payments"],
        balance=invoice["balance_amount"] if invoice["payments"] else None,
    )
    ctx.audit("view", "allowed", "invoice", str(invoice_id), fields=("document",))
    return {"invoice_id": str(invoice_id), "filename": f"{invoice['number'] or 'draft-invoice'}.html", "html": html}
