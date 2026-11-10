"""Supplier bills and the actual cost of a line (AA-1068 item crm-25; ``crm.supplier_bill`` and
``crm.supplier_bill_line``, crm-051).

- **Record** (:func:`create_bill`): a bill of one of the agency's own suppliers (number, bill date, currency, amount,
  note) and the agency lines of that supplier it pays (an amount each, in the bill's currency; their sum may be less
  than the bill: the rest is not a trip cost). The FX of the bill date is frozen on the bill (``crm.fx_resolve`` into
  the base currency: 409 ``fx_rate_missing`` without one within 30 days). Each line then gets its ``actual_cost`` money
  row (``crm.booking_line_actual_cost``: the sum of its live bill lines at the latest bill's FX).
- **Change** (:func:`update_bill`): only the status (recorded -> paid with a paid day, recorded / paid -> void) and the
  note. A wrong amount is fixed by voiding the bill and recording it again; voiding recomputes its lines' actual cost
  (a line left with no live bill is not billed any more: the P&L goes back to its planned cost).
- **Refusals** (worded here, held by the database): a line of another supplier, an AA line or a trashed line 409
  ``bill_line_invalid`` (CRM51); a second currency on one line 409 ``bill_currency_mixed``; a live bill number twice
  for one supplier 409 ``duplicate_bill_number``; a bill of the AA supplier 422 ``aa_supplier_bill`` (AA's invoices
  come from Booking, :mod:`app.finance.aa_invoices`).

Owner / agent only (``money_fields_edit`` to write, ``view_margin`` to read): the bill is a cost.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound
from app.ledger import fx
from app.tenancy import TenantContext

MAX_LINES = 50
MAX_LIST = 200
STATUSES = ("recorded", "paid", "void")

SUPPLIER_SQL = "SELECT kind, deleted_at FROM crm.agency_supplier WHERE agency_supplier_id = $1"
INSERT_SQL = """
INSERT INTO crm.supplier_bill (agency_supplier_id, number, bill_date, currency, amount, note, base_currency,
                               amount_base, fx_rate, fx_rate_date, fx_source, stale_days)
VALUES ($1, $2, $3, $4, $5::numeric, $6, $7, round($5::numeric * $8::numeric, 2), $8::numeric, $9, $10, $11)
RETURNING supplier_bill_id
"""
LINE_SQL = "INSERT INTO crm.supplier_bill_line (supplier_bill_id, booking_line_id, amount) VALUES ($1, $2, $3)"
ACTUAL_SQL = "SELECT crm.booking_line_actual_cost($1)"
BILL_SQL = """
SELECT b.supplier_bill_id, b.agency_supplier_id, s.name AS supplier_name, b.number, b.bill_date, b.currency, b.amount,
       b.status, b.paid_on, b.note, b.base_currency, b.amount_base, b.fx_rate, b.fx_rate_date, b.fx_source,
       b.stale_days, b.created_at
  FROM crm.supplier_bill b JOIN crm.agency_supplier s ON s.agency_supplier_id = b.agency_supplier_id
 WHERE b.supplier_bill_id = $1
"""
LIST_SQL = """
SELECT b.supplier_bill_id, b.agency_supplier_id, s.name AS supplier_name, b.number, b.bill_date, b.currency, b.amount,
       b.status, b.paid_on, b.note, b.base_currency, b.amount_base, b.fx_rate, b.fx_rate_date, b.fx_source,
       b.stale_days, b.created_at
  FROM crm.supplier_bill b JOIN crm.agency_supplier s ON s.agency_supplier_id = b.agency_supplier_id
 WHERE ($1::uuid IS NULL OR b.agency_supplier_id = $1)
   AND ($2::uuid IS NULL OR EXISTS (SELECT 1 FROM crm.supplier_bill_line x JOIN crm.booking_line l
                                      ON l.booking_line_id = x.booking_line_id
                                     WHERE x.supplier_bill_id = b.supplier_bill_id AND l.trip_id = $2))
 ORDER BY b.bill_date DESC, b.created_at DESC, b.supplier_bill_id
 LIMIT $3
"""
LINES_OF_SQL = """
SELECT x.supplier_bill_id, x.booking_line_id, x.amount, l.title AS line_title, l.trip_id, t.title AS trip_title
  FROM crm.supplier_bill_line x
  JOIN crm.booking_line l ON l.booking_line_id = x.booking_line_id
  JOIN crm.trip t ON t.trip_id = l.trip_id
 WHERE x.supplier_bill_id = ANY($1::uuid[])
 ORDER BY x.created_at, x.booking_line_id
"""
STATUS_SQL = "UPDATE crm.supplier_bill SET status = $2, paid_on = $3 WHERE supplier_bill_id = $1"
NOTE_SQL = "UPDATE crm.supplier_bill SET note = $2 WHERE supplier_bill_id = $1"
BILLED_LINES_SQL = "SELECT booking_line_id FROM crm.supplier_bill_line WHERE supplier_bill_id = $1"


def _text(value: Any, field: str, limit: int, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise Invalid(f"invalid_{field}", f"1 to {limit} characters")
    text = value.strip()
    if any(ord(c) < 32 for c in text):
        raise Invalid(f"invalid_{field}", "no control characters")
    return text


def _amount(value: Any, field: str = "amount", *, zero: bool = False) -> Decimal:
    try:
        out = Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception:  # noqa: BLE001 — any bad number is the same answer
        raise Invalid(f"invalid_{field}", "a number with up to 2 decimals") from None
    if not out.is_finite() or out < 0 or (out == 0 and not zero) or out > Decimal("99999999999.99"):
        raise Invalid(f"invalid_{field}", "a positive amount")
    return out


def bill_view(row: Any, lines: list[Any]) -> dict[str, Any]:
    allocated = sum((Decimal(x["amount"]) for x in lines), Decimal(0))
    return {
        "supplier_bill_id": str(row["supplier_bill_id"]),
        "agency_supplier_id": str(row["agency_supplier_id"]),
        "supplier_name": row["supplier_name"],
        "number": row["number"],
        "bill_date": row["bill_date"].isoformat(),
        "status": row["status"],
        "paid_on": row["paid_on"].isoformat() if row["paid_on"] else None,
        "note": row["note"],
        # Keyed money objects: the margin serializer strips "bill_cost" for a role without view_margin.
        "bill_cost": {
            "amount": str(row["amount"]),
            "currency": row["currency"],
            "amount_base": str(row["amount_base"]),
            "base_currency": row["base_currency"],
            "fx": {
                "rate": str(row["fx_rate"]),
                "rate_date": row["fx_rate_date"].isoformat(),
                "source": row["fx_source"],
                "stale_days": row["stale_days"],
                **(
                    {"attribution": fx.ATTRIBUTION}
                    if row["fx_source"] == "platform_daily" and row["fx_rate"] != 1
                    else {}
                ),
            },
            "allocated": str(allocated.quantize(Decimal("0.01"))),
            "unallocated": str((Decimal(row["amount"]) - allocated).quantize(Decimal("0.01"))),
        },
        "lines": [
            {
                "booking_line_id": str(x["booking_line_id"]),
                "line_title": x["line_title"],
                "trip_id": str(x["trip_id"]),
                "trip_title": x["trip_title"],
                "line_cost": str(x["amount"]),
            }
            for x in lines
        ],
        "created_at": row["created_at"].isoformat(),
    }


async def _views(ctx: TenantContext, rows: list[Any]) -> list[dict[str, Any]]:
    ids = [r["supplier_bill_id"] for r in rows]
    by_bill: dict[uuid.UUID, list[Any]] = {i: [] for i in ids}
    if ids:
        for x in await ctx.conn.fetch(LINES_OF_SQL, ids):
            by_bill.setdefault(x["supplier_bill_id"], []).append(x)
    return [bill_view(r, by_bill.get(r["supplier_bill_id"], [])) for r in rows]


async def get_bill(ctx: TenantContext, bill_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(BILL_SQL, bill_id)
    if row is None:
        raise NotFound("bill_not_found")
    return (await _views(ctx, [row]))[0]


async def list_bills(
    ctx: TenantContext, *, supplier_id: uuid.UUID | None = None, trip_id: uuid.UUID | None = None
) -> dict[str, Any]:
    rows = list(await ctx.conn.fetch(LIST_SQL, supplier_id, trip_id, MAX_LIST))
    views = await _views(ctx, rows)
    ctx.audit("list_view", "allowed", "supplier_bill", None, object_ids=tuple(v["supplier_bill_id"] for v in views))
    return {"bills": views, "truncated": len(rows) == MAX_LIST}


def _db(exc: asyncpg.PostgresError) -> Exception:
    text = str(exc)
    if isinstance(exc, asyncpg.UniqueViolationError):
        if "supplier_bill_number_uq" in text or exc.constraint_name == "supplier_bill_number_uq":
            return Conflict("duplicate_bill_number", "this supplier already has a live bill with that number")
        return Conflict("duplicate_bill_line", "a line appears twice on the bill")
    if getattr(exc, "sqlstate", "") == "CRM51":
        if "bill_currency_mixed" in text:
            return Conflict("bill_currency_mixed", "the bills of one line use one currency")
        return Conflict("bill_line_invalid", "a bill pays live lines of its own supplier only")
    if getattr(exc, "sqlstate", "") == "CRM24":
        return Conflict("bill_line_invalid", "an AA line's cost comes from AA")
    if isinstance(exc, asyncpg.ForeignKeyViolationError):
        return NotFound("line_not_found")
    return exc


async def _actual(ctx: TenantContext, line_ids: list[uuid.UUID]) -> None:
    for line in sorted(set(line_ids)):
        await ctx.conn.fetchval(ACTUAL_SQL, line)


async def create_bill(ctx: TenantContext, body: dict[str, Any]) -> dict[str, Any]:
    supplier_id: uuid.UUID = body["agency_supplier_id"]
    number = _text(body.get("number"), "number", 80)
    note = _text(body.get("note"), "note", 500, optional=True)
    currency = fx.currency(body.get("currency"))
    amount = _amount(body.get("amount"))
    bill_date: dt.date = body["bill_date"]
    today = await fx.agency_today(ctx)
    if bill_date > today or bill_date < today - dt.timedelta(days=730):
        raise Invalid("invalid_bill_date", "today or a day within the last two years")
    lines = body.get("lines") or []
    if not lines or len(lines) > MAX_LINES:
        raise Invalid("invalid_lines", f"1 to {MAX_LINES} lines")
    parts = [(uuid.UUID(str(x["booking_line_id"])), _amount(x.get("amount"), "line_amount", zero=True)) for x in lines]
    if len({line for line, _ in parts}) != len(parts):
        raise Invalid("invalid_lines", "each line once")
    if sum(a for _, a in parts) > amount:
        raise Invalid("bill_lines_over", "the lines add up to more than the bill")
    supplier = await ctx.conn.fetchrow(SUPPLIER_SQL, supplier_id)
    if supplier is None or supplier["deleted_at"] is not None:
        raise NotFound("supplier_not_found")
    if supplier["kind"] == "aa":
        raise Invalid("aa_supplier_bill", "AA's invoices come from AA; see Bookings with AA")
    settings = await fx.settings(ctx)
    rate = await fx.resolve(ctx, currency, settings["base_currency"], bill_date)
    if rate is None:
        raise Conflict("fx_rate_missing", "no exchange rate for this currency in the last 30 days", currency=currency)
    try:
        async with ctx.conn.transaction():
            bill_id = await ctx.conn.fetchval(
                INSERT_SQL, supplier_id, number, bill_date, currency, amount, note, settings["base_currency"],
                rate.rate, rate.rate_date, rate.source, min(rate.stale_days, 366),
            )  # fmt: skip
            for line, part in parts:
                await ctx.conn.execute(LINE_SQL, bill_id, line, part)
            await _actual(ctx, [line for line, _ in parts])
    except asyncpg.PostgresError as exc:
        raise _db(exc) from None
    ctx.audit("create", "allowed", "supplier_bill", str(bill_id), object_ids=tuple(str(line) for line, _ in parts))
    return await get_bill(ctx, bill_id)


async def update_bill(ctx: TenantContext, bill_id: uuid.UUID, changes: dict[str, Any]) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(BILL_SQL, bill_id)
    if row is None:
        raise NotFound("bill_not_found")
    status = changes.get("status")
    try:
        async with ctx.conn.transaction():
            if "note" in changes:
                await ctx.conn.execute(NOTE_SQL, bill_id, _text(changes["note"], "note", 500, optional=True))
            if status is not None and status != row["status"]:
                if status not in STATUSES or status == "recorded":
                    raise Invalid("invalid_status", "paid or void")
                if row["status"] == "void":
                    raise Conflict("bill_void", "a void bill does not change")
                paid_on = row["paid_on"]
                if status == "paid":
                    paid_on = changes.get("paid_on") or await fx.agency_today(ctx)
                    if paid_on < row["bill_date"]:
                        raise Invalid("invalid_paid_on", "on or after the bill date")
                await ctx.conn.execute(STATUS_SQL, bill_id, status, paid_on)
                if status == "void":
                    await _actual(ctx, [r["booking_line_id"] for r in await ctx.conn.fetch(BILLED_LINES_SQL, bill_id)])
    except asyncpg.PostgresError as exc:
        raise _db(exc) from None
    ctx.audit("update", "allowed", "supplier_bill", str(bill_id), fields=tuple(sorted(changes)))
    return await get_bill(ctx, bill_id)
