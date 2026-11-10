"""The "Bookings with AA" page (AA-1068 item crm-26; booking.b2b.v1 1.5 ``GET /invoices``, ``/invoices/{number}``,
``/billing-status``; ``crm.aa_invoice_cache``, crm-051).

What the page shows: the agency's AA bookings (its AA lines with a Booking hold or booking, their trip and status, the
quoted B2B price), the AA invoices (software fee and tour settlement) with their billing status, and per booking what
AA invoiced: a **mismatch** flag when AA billed a booking in full (a ``balance`` or ``full`` line) at another amount or
currency than the sum of the quotes of its confirmed lines (several CRM lines of one accepted proposal share one AA
booking: Booking invoices per booking, so the comparison is per booking, as in ``crm.v_trip_pnl``), and a
**past-due banner** while AA's credit status is ``warn`` / ``past_due`` (holds are paused while ``past_due``: a new
AA hold would be refused with ``credit_hold_overdue``).

How it reads:

1. The membership and the role (``view_margin``: the B2B price is the agency's AA cost) and the cached state.
2. Booking, outside any transaction (no connection held while it answers): up to :data:`MAX_PAGES` pages of
   invoices, the billing status, and the detail of each settlement invoice the copy has not read yet (or whose status
   changed), at most :data:`MAX_DETAILS` per open (the rest the next time).
3. The copy (``crm.aa_invoice_cache``) is upserted with what Booking answered, then the page is built from it and the
   agency's AA lines. Booking down, not configured or refusing the agency: the page shows the copy as it stands with
   ``source = "cache"`` and the reason code; nothing is invented.

The copy keeps only the summary and, per AA booking, the amount invoiced and whether a balance / full line billed it
(``booking_amounts``): the trip P&L (``crm.v_trip_pnl``) takes that as the AA cost once complete. Booking answers
``no-store``; the CRM never keeps the lines, the payments or the issuer text (read live by :func:`invoice_detail`).
Money keys carry ``b2b`` in their names, so the margin serializer would strip them for a role without ``view_margin``
(the routes refuse that role first).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.errors import Conflict, NotFound, UnavailableRetry
from app.tenancy import TenantContext

PAGE = 100
MAX_PAGES = 3
MAX_DETAILS = 20
MAX_LINES = 500
RETRY_AFTER_S = 60
BILLED_ROLES = frozenset({"deposit", "balance", "full", "adjustment"})
COMPLETE_ROLES = frozenset({"balance", "full"})

CACHE_SQL = """
SELECT number, invoice_kind, aa_status, currency, total_amount, balance_amount, issued_on, due_on, overdue_days,
       booking_amounts, detail_read, refreshed_at
  FROM crm.aa_invoice_cache
 WHERE deleted_at IS NULL
 ORDER BY issued_on DESC, number DESC
"""
AA_SUPPLIER_SQL = "SELECT crm.ensure_aa_supplier()"
UPSERT_SQL = """
INSERT INTO crm.aa_invoice_cache (number, agency_supplier_id, invoice_kind, aa_status, currency, total_amount,
                                  balance_amount, issued_on, due_on, overdue_days, booking_amounts, detail_read,
                                  refreshed_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, COALESCE($11::jsonb, '{}'::jsonb), $11::jsonb IS NOT NULL, now())
ON CONFLICT (tenant_id, number) DO UPDATE
   SET invoice_kind = EXCLUDED.invoice_kind, aa_status = EXCLUDED.aa_status, currency = EXCLUDED.currency,
       total_amount = EXCLUDED.total_amount, balance_amount = EXCLUDED.balance_amount,
       issued_on = EXCLUDED.issued_on, due_on = EXCLUDED.due_on, overdue_days = EXCLUDED.overdue_days,
       booking_amounts = CASE WHEN $11::jsonb IS NULL THEN crm.aa_invoice_cache.booking_amounts ELSE $11::jsonb END,
       detail_read = crm.aa_invoice_cache.detail_read OR $11::jsonb IS NOT NULL,
       refreshed_at = now()
"""
AA_LINES_SQL = """
SELECT l.booking_line_id, l.trip_id, t.title AS trip_title, t.start_date, l.title, l.status, l.aa_sub_status,
       l.bk_booking_id, l.bk_hold_id, l.service_from, m.amount AS quoted_amount, m.currency AS quoted_currency
  FROM crm.booking_line l
  JOIN crm.trip t ON t.trip_id = l.trip_id AND t.deleted_at IS NULL
  LEFT JOIN crm.v_booking_line_money_current m ON m.booking_line_id = l.booking_line_id AND m.kind = 'cost'
 WHERE l.supplier_kind = 'aa' AND l.deleted_at IS NULL AND (l.bk_booking_id IS NOT NULL OR l.bk_hold_id IS NOT NULL)
 ORDER BY l.service_from NULLS LAST, l.created_at, l.booking_line_id
 LIMIT $1
"""


@dataclass
class Fetched:
    """What Booking answered in one open (None = not read); ``error``: the code when it could not answer."""

    invoices: list[contract.AgencyInvoiceSummary] | None = None
    details: dict[str, contract.AgencyInvoice] = field(default_factory=dict)
    billing: contract.BillingStatus | None = None
    error: str | None = None


def booking_error_code(exc: Exception) -> str:
    if isinstance(exc, BookingRefusal) and exc.code in ("unknown_partner", "forbidden"):
        return "aa_partner_not_active"
    if isinstance(exc, contract.ContractError):
        return "booking_contract"
    return "booking_unavailable"


def booking_amounts(invoice: contract.AgencyInvoice) -> dict[str, dict[str, Any]]:
    """Per AA booking id of a settlement invoice: the sum of its live deposit / balance / full / adjustment lines and
    whether a balance or full line billed it (``complete``). Cancellation fees are left out: Booking reports them as
    the line's cancellation cost (the cancel answer, AA-957)."""
    out: dict[str, dict[str, Any]] = {}
    for line in invoice.lines:
        if line.voided or not line.booking_id or line.line_role not in BILLED_ROLES:
            continue
        item = out.setdefault(line.booking_id, {"amount": Decimal(0), "complete": False})
        item["amount"] += line.amount
        item["complete"] = item["complete"] or line.line_role in COMPLETE_ROLES
    cent = Decimal("0.01")
    return {k: {"amount": str(v["amount"].quantize(cent)), "complete": v["complete"]} for k, v in out.items()}


def _needs_detail(inv: contract.AgencyInvoiceSummary, known: dict[str, Any]) -> bool:
    if inv.invoice_kind != "settlement":
        return False
    cached = known.get(inv.number)
    return cached is None or not cached["detail_read"] or cached["aa_status"] != inv.status


async def fetch(
    client: BookingClient | None, tenant_id: uuid.UUID, known: dict[str, Any], *, details: bool = True
) -> Fetched:
    """Read Booking for one open of the page (never raises: a failure is ``Fetched.error``)."""
    if client is None:
        return Fetched(error="booking_not_configured")
    out = Fetched()
    try:
        invoices: list[contract.AgencyInvoiceSummary] = []
        for page in range(MAX_PAGES):
            answer = await client.invoices(tenant_id, limit=PAGE, offset=page * PAGE)
            items, total = contract.agency_invoice_page(answer)
            invoices += items
            if len(invoices) >= total or not items:
                break
        out.invoices = invoices
        out.billing = contract.billing_status(await client.billing_status(tenant_id))
        if details:
            todo = [inv.number for inv in invoices if _needs_detail(inv, known)][:MAX_DETAILS]
            for number in todo:
                out.details[number] = contract.agency_invoice(await client.invoice(tenant_id, number))
    except (BookingUnavailable, BookingRefusal, contract.ContractError) as exc:
        out.error = booking_error_code(exc)
    return out


async def cached_state(ctx: TenantContext) -> dict[str, Any]:
    return {r["number"]: r for r in await ctx.conn.fetch(CACHE_SQL)}


async def store(ctx: TenantContext, fetched: Fetched) -> int:
    """Upsert what Booking answered into the copy; the number of invoices written."""
    summaries = list(fetched.invoices or [])
    seen = {s.number for s in summaries}
    summaries += [d.summary for n, d in fetched.details.items() if n not in seen]
    if not summaries:
        return 0
    aa_supplier = await ctx.conn.fetchval(AA_SUPPLIER_SQL)
    for s in summaries:
        detail = fetched.details.get(s.number)
        amounts = json.dumps(booking_amounts(detail), sort_keys=True) if detail is not None else None
        await ctx.conn.execute(
            UPSERT_SQL, s.number, aa_supplier, s.invoice_kind, s.status, s.currency, s.total, s.balance, s.issued_on,
            s.due_on, s.overdue_days, amounts,
        )  # fmt: skip
    return len(summaries)


def _amounts(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def banner(billing: contract.BillingStatus | None, cache: list[Any]) -> dict[str, Any] | None:
    """The past-due banner: Booking's credit status when it answered, else the copy's overdue invoices."""
    if billing is not None:
        if billing.status == "ok":
            return None
        return {
            "level": billing.status,
            "holds_paused": billing.holds_paused,
            "overdue_invoices": billing.overdue_invoices,
            "oldest_overdue_days": billing.oldest_overdue_days,
            "overdue_b2b": [{"amount": str(a), "currency": c} for a, c in billing.overdue_amount],
            "source": "live",
        }
    overdue = [r for r in cache if r["aa_status"] == "issued" and r["overdue_days"] > 0 and r["balance_amount"] > 0]
    if not overdue:
        return None
    sums: dict[str, Decimal] = {}
    for r in overdue:
        sums[r["currency"]] = sums.get(r["currency"], Decimal(0)) + Decimal(r["balance_amount"])
    return {
        "level": "overdue",
        "holds_paused": None,
        "overdue_invoices": len(overdue),
        "oldest_overdue_days": max(r["overdue_days"] for r in overdue),
        "overdue_b2b": [{"amount": str(v), "currency": k} for k, v in sorted(sums.items())],
        "source": "cache",
    }


def booking_quotes(lines: list[Any]) -> dict[str, dict[str, Any]]:
    """Per AA booking id: the quoted cost of its confirmed lines (``quoted``: {amount, currency} when every one has a
    quote in one currency, else None) and how many there are (``lines``). Invoiced amounts compare with this sum."""
    groups: dict[str, list[Any]] = {}
    for line in lines:
        if line["bk_booking_id"] and line["status"] == "confirmed":
            groups.setdefault(line["bk_booking_id"], []).append(line)
    out: dict[str, dict[str, Any]] = {}
    for booking_id, group in groups.items():
        currencies = {x["quoted_currency"] for x in group}
        quoted = None
        if len(currencies) == 1 and all(x["quoted_amount"] is not None for x in group):
            total = sum((Decimal(x["quoted_amount"]) for x in group), Decimal(0))
            quoted = {"amount": total.quantize(Decimal("0.01")), "currency": group[0]["quoted_currency"]}
        out[booking_id] = {"quoted": quoted, "lines": len(group)}
    return out


def build(cache: list[Any], lines: list[Any], fetched: Fetched, today: dt.date) -> dict[str, Any]:
    """The page body from the copy and the agency's AA lines."""
    by_booking: dict[str, list[Any]] = {}
    for line in lines:
        if line["bk_booking_id"]:
            by_booking.setdefault(line["bk_booking_id"], []).append(line)
    quotes = booking_quotes(lines)
    invoiced: dict[str, dict[str, Any]] = {}  # booking id -> {currency, amount, complete, numbers}
    invoices = []
    for r in cache:
        amounts = _amounts(r["booking_amounts"])
        bookings = []
        for booking_id, item in sorted(amounts.items()):
            crm_lines = by_booking.get(booking_id, [])
            bookings.append(
                {
                    "booking_id": booking_id,
                    "b2b_amount": item.get("amount"),
                    "complete": bool(item.get("complete")),
                    "lines": [
                        {"booking_line_id": str(x["booking_line_id"]), "trip_id": str(x["trip_id"]),
                         "trip_title": x["trip_title"]}
                        for x in crm_lines
                    ],
                }
            )  # fmt: skip
            if r["invoice_kind"] == "settlement" and r["aa_status"] != "void":
                agg = invoiced.setdefault(
                    booking_id, {"currency": r["currency"], "amount": Decimal(0), "complete": False, "numbers": []}
                )
                if agg["currency"] == r["currency"]:
                    agg["amount"] += Decimal(str(item.get("amount") or 0))
                agg["complete"] = agg["complete"] or bool(item.get("complete"))
                agg["numbers"].append(r["number"])
        invoices.append(
            {
                "number": r["number"],
                "invoice_kind": r["invoice_kind"],
                "status": r["aa_status"],
                "currency": r["currency"],
                "b2b_total": str(r["total_amount"]),
                "b2b_balance": str(r["balance_amount"]),
                "issued_on": r["issued_on"].isoformat(),
                "due_on": r["due_on"].isoformat(),
                "overdue_days": r["overdue_days"],
                "overdue": r["aa_status"] == "issued" and r["balance_amount"] > 0 and r["due_on"] < today,
                "detail_read": r["detail_read"],
                "bookings": bookings,
            }
        )
    rows = []
    mismatched: set[str] = set()
    for line in lines:
        agg = invoiced.get(line["bk_booking_id"] or "")
        group = quotes.get(line["bk_booking_id"] or "")
        booking_quoted = group["quoted"] if group else None
        quoted = (
            {"amount": str(line["quoted_amount"]), "currency": line["quoted_currency"]}
            if line["quoted_amount"] is not None
            else None
        )
        billed = None
        mismatch = False
        if agg is not None:
            billed = {"amount": str(agg["amount"].quantize(Decimal("0.01"))), "currency": agg["currency"],
                      "complete": agg["complete"], "invoices": sorted(set(agg["numbers"]))}  # fmt: skip
            # Per booking: the invoiced sum against the sum of the quotes of the booking's confirmed lines.
            mismatch = bool(
                agg["complete"]
                and line["status"] == "confirmed"
                and (
                    booking_quoted is None
                    or agg["currency"] != booking_quoted["currency"]
                    or agg["amount"] != booking_quoted["amount"]
                )
            )
            if mismatch:
                mismatched.add(line["bk_booking_id"])
        rows.append(
            {
                "booking_line_id": str(line["booking_line_id"]),
                "trip_id": str(line["trip_id"]),
                "trip_title": line["trip_title"],
                "trip_start_date": line["start_date"].isoformat() if line["start_date"] else None,
                "title": line["title"],
                "status": line["status"],
                "aa_sub_status": line["aa_sub_status"],
                "booking_id": line["bk_booking_id"],
                "hold_id": line["bk_hold_id"],
                "service_from": line["service_from"].isoformat() if line["service_from"] else None,
                "quoted_b2b": quoted,
                "booking_quoted_b2b": None
                if booking_quoted is None
                else {"amount": str(booking_quoted["amount"]), "currency": booking_quoted["currency"]},
                "booking_lines": group["lines"] if group else 0,
                "invoiced_b2b": billed,
                "mismatch": mismatch,
            }
        )
    refreshed = max((r["refreshed_at"] for r in cache), default=None)
    return {
        "source": "live" if fetched.error is None else "cache",
        "booking_error": fetched.error,
        "refreshed_at": refreshed.isoformat() if refreshed else None,
        "billing_status": None
        if fetched.billing is None
        else {
            "status": fetched.billing.status,
            "holds_paused": fetched.billing.holds_paused,
            "paid_through": fetched.billing.paid_through.isoformat() if fetched.billing.paid_through else None,
        },
        "banner": banner(fetched.billing, cache),
        "invoices": invoices,
        "bookings": rows,
        "mismatches": len(mismatched),  # AA bookings (not lines) billed in full at another amount
    }


Tx = Callable[[], AbstractAsyncContextManager[TenantContext]]


async def _cache_and_lines(ctx: TenantContext, *, cache: bool = True) -> tuple[list[Any], list[Any]]:
    """The copy (when asked) and the agency's AA lines with a Booking hold or booking."""
    rows = list(await ctx.conn.fetch(CACHE_SQL)) if cache else []
    return rows, list(await ctx.conn.fetch(AA_LINES_SQL, MAX_LINES))


async def page(tx: Tx, client: BookingClient | None, tenant_id: uuid.UUID, today: dt.date) -> dict[str, Any]:
    """The whole page: the cached state (tx 1), Booking (no transaction), the upsert and the build (tx 2)."""
    async with tx() as ctx:
        known = await cached_state(ctx)
    fetched = await fetch(client, tenant_id, known)
    async with tx() as ctx:
        written = await store(ctx, fetched)
        cache, lines = await _cache_and_lines(ctx)
        out = build(cache, lines, fetched, today)
        ctx.audit(
            "list_view", "allowed", "aa_invoice", None, reason=f"source_{out['source']}",
            object_ids=tuple(i["number"] for i in out["invoices"])[:100],
        )  # fmt: skip
        out["refreshed_invoices"] = written
        return out


async def invoice_detail(tx: Tx, client: BookingClient | None, tenant_id: uuid.UUID, number: str) -> dict[str, Any]:
    """One AA invoice read live (lines, approved payments, how to pay), its lines linked to the agency's AA lines."""
    if not contract._INVOICE_NO.match(number):  # noqa: SLF001 — the contract's own number pattern
        raise NotFound("invoice_not_found")
    async with tx() as ctx:
        pass  # the membership and the role before Booking is asked
    if client is None:
        raise _unavailable("booking_not_configured")
    try:
        invoice = contract.agency_invoice(await client.invoice(tenant_id, number))
    except BookingRefusal as exc:
        if exc.status == 404:
            raise NotFound("invoice_not_found") from None
        raise _unavailable(booking_error_code(exc)) from None
    except (BookingUnavailable, contract.ContractError) as exc:
        raise _unavailable(booking_error_code(exc)) from None
    async with tx() as ctx:
        await store(ctx, Fetched(details={number: invoice}))
        _, lines = await _cache_and_lines(ctx, cache=False)
        ctx.audit("view", "allowed", "aa_invoice", number)
    by_booking: dict[str, list[Any]] = {}
    for x in lines:
        if x["bk_booking_id"]:
            by_booking.setdefault(x["bk_booking_id"], []).append(x)
    s = invoice.summary
    return {
        "number": s.number,
        "invoice_kind": s.invoice_kind,
        "status": s.status,
        "currency": s.currency,
        "b2b_total": str(s.total),
        "b2b_balance": str(s.balance),
        "issued_on": s.issued_on.isoformat(),
        "due_on": s.due_on.isoformat(),
        "overdue_days": s.overdue_days,
        "lines": [
            {
                "line_no": x.line_no,
                "line_role": x.line_role,
                "description": x.description,
                "b2b_amount": str(x.amount),
                "booking_id": x.booking_id,
                "voided": x.voided,
                "crm_lines": [
                    {
                        "booking_line_id": str(c["booking_line_id"]),
                        "trip_id": str(c["trip_id"]),
                        "trip_title": c["trip_title"],
                    }
                    for c in by_booking.get(x.booking_id or "", [])
                ],
            }
            for x in invoice.lines
        ],  # fmt: skip
        "payments": [{"received_on": d.isoformat(), "amount": str(a), "currency": c} for d, a, c in invoice.payments],
        "payment_instructions": invoice.payment_instructions,
        "invoice_inbox": invoice.invoice_inbox,
    }


def _unavailable(code: str) -> Exception:
    if code == "aa_partner_not_active":
        return Conflict("aa_partner_not_active", "Your agency is not linked to AA B2B, or AA has suspended the link")
    return UnavailableRetry("booking_unavailable", RETRY_AFTER_S, "AA did not answer; try again in a minute")
