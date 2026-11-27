"""Reading the ``booking.b2b.v1`` 1.5 answers of the agency's AA invoices and billing status (AA-1037, AA-1068: the
"Bookings with AA" page), the same tolerant reader as :mod:`app.booking.contract` (its helpers). Split out of that
module (AA-1188) so each stays under the file-size limit; the reading itself is unchanged.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.booking.contract import (
    _CURRENCY,
    _ID,
    ContractError,
    _count,
    _date,
    _decimal,
    _obj,
    _str,
    money,
)

# ── 1.5 (AA-1037): the agency's AA invoices and billing status (AA-1068, the "Bookings with AA" page) ───────────────
_INVOICE_NO = re.compile(r"^AA[ST]-\d{4}-\d{6}$")
_INVOICE_AMOUNT = re.compile(r"^-?\d+\.\d{2}$")
INVOICE_KINDS = frozenset({"saas", "settlement"})
INVOICE_STATUSES = frozenset({"issued", "paid", "void"})
CREDIT_STATUSES = frozenset({"ok", "warn", "past_due"})
LINE_ROLES = frozenset({"subscription", "extra_seats", "deposit", "balance", "full", "cancellation_fee", "adjustment"})


@dataclass(frozen=True)
class AgencyInvoiceSummary:
    number: str
    invoice_kind: str
    status: str
    currency: str
    total: Decimal
    balance: Decimal
    issued_on: dt.date
    due_on: dt.date
    overdue_days: int


@dataclass(frozen=True)
class AgencyInvoiceLine:
    line_no: int
    line_role: str
    description: str
    amount: Decimal
    hold_id: str | None
    booking_id: str | None
    voided: bool


@dataclass(frozen=True)
class AgencyInvoice:
    summary: AgencyInvoiceSummary
    lines: tuple[AgencyInvoiceLine, ...]
    payments: tuple[tuple[dt.date, Decimal, str], ...]
    payment_instructions: str | None
    invoice_inbox: str | None


@dataclass(frozen=True)
class BillingStatus:
    status: str
    holds_paused: bool
    overdue_invoices: int
    oldest_overdue_days: int
    overdue_amount: tuple[tuple[Decimal, str], ...]
    paid_through: dt.date | None


def _enum(data: dict[str, Any], key: str, allowed: frozenset[str]) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"{key} is unknown")
    return value


def _invoice_summary(value: Any) -> AgencyInvoiceSummary:
    data = _obj(value, "invoice")
    overdue = data.get("overdue_days")
    if not isinstance(overdue, int) or isinstance(overdue, bool) or overdue < 0:
        raise ContractError("overdue_days is not a count")
    return AgencyInvoiceSummary(
        number=_str(data, "number", _INVOICE_NO),
        invoice_kind=_enum(data, "invoice_kind", INVOICE_KINDS),
        status=_enum(data, "status", INVOICE_STATUSES),
        currency=_str(data, "currency", _CURRENCY),
        total=_decimal(data.get("total"), _INVOICE_AMOUNT, "total"),
        balance=_decimal(data.get("balance"), _INVOICE_AMOUNT, "balance"),
        issued_on=_date(data.get("issued_on"), "issued_on"),
        due_on=_date(data.get("due_date"), "due_date"),
        overdue_days=min(overdue, 36500),
    )


def agency_invoice_page(data: Any) -> tuple[list[AgencyInvoiceSummary], int]:
    """``GET /invoices``: the page's invoices and the total that match (a broken item fails the page)."""
    page = _obj(data, "invoice page")
    items = page.get("items")
    total = page.get("total")
    if not isinstance(items, list) or not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ContractError("items or total is malformed")
    return [_invoice_summary(item) for item in items], total


def agency_invoice(data: Any) -> AgencyInvoice:
    """``GET /invoices/{number}``: the invoice with its lines, approved payments and the issuer's payment text."""
    body = _obj(data, "invoice")
    raw_lines, raw_payments = body.get("lines"), body.get("payments")
    if not isinstance(raw_lines, list) or not isinstance(raw_payments, list):
        raise ContractError("lines or payments is not a list")
    lines = []
    for item in raw_lines:
        line = _obj(item, "line")
        number = line.get("line_no")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ContractError("line_no is not a count")
        voided = line.get("voided")
        if not isinstance(voided, bool):
            raise ContractError("voided is not a boolean")
        description = line.get("description")
        lines.append(
            AgencyInvoiceLine(
                line_no=number,
                line_role=_enum(line, "line_role", LINE_ROLES),
                description=description[:300] if isinstance(description, str) else "",
                amount=_decimal(line.get("amount"), _INVOICE_AMOUNT, "amount"),
                hold_id=_str(line, "hold_id", _ID, optional=True),
                booking_id=_str(line, "booking_id", _ID, optional=True),
                voided=voided,
            )
        )
    payments = []
    for item in raw_payments:
        pay = _obj(item, "payment")
        payments.append(
            (_date(pay.get("received_on"), "received_on"),
             _decimal(pay.get("amount"), _INVOICE_AMOUNT, "payment.amount"), _str(pay, "currency", _CURRENCY))
        )  # fmt: skip
    issuer = body.get("issuer")
    instructions = inbox = None
    if isinstance(issuer, dict):
        text = issuer.get("payment_instructions")
        instructions = text[:2000] if isinstance(text, str) else None
        mail = issuer.get("invoice_inbox")
        inbox = mail[:254] if isinstance(mail, str) else None
    return AgencyInvoice(_invoice_summary(body), tuple(lines), tuple(payments), instructions, inbox)


def billing_status(data: Any) -> BillingStatus:
    """``GET /billing-status``: the credit status (holds paused while past_due) and the software fee's paid day."""
    body = _obj(data, "billing status")
    paused = body.get("holds_paused")
    if not isinstance(paused, bool):
        raise ContractError("holds_paused is not a boolean")
    amounts = body.get("overdue_amount")
    if not isinstance(amounts, list):
        raise ContractError("overdue_amount is not a list")
    paid = body.get("paid_through")
    return BillingStatus(
        status=_enum(body, "status", CREDIT_STATUSES),
        holds_paused=paused,
        overdue_invoices=_count(body, "overdue_invoices", 0),
        oldest_overdue_days=_count(body, "oldest_overdue_days", 0),
        overdue_amount=tuple(money(a, "overdue_amount") for a in amounts),
        paid_through=None if paid is None else _date(paid, "paid_through"),
    )
