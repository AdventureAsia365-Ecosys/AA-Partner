"""The profit of a trip and the profit cube (AA-1068 items crm-30 and crm-31; ``crm.v_trip_pnl``, crm-051).

The database computes the numbers (one view, so the trip card, the cube and its CSV always agree); this module shapes
them. Every amount is a decimal string in the agency's base currency.

- **Trip** (:func:`trip_pnl`): revenue by kind (sales, service fees, cancellation fees, minus refunds; all net of tax),
  costs (AA at the quote or at the AA invoice once billed in full, agency lines at the supplier bill once billed else
  planned, AA cancellation fees, the departure share, the sales partner commission: 0 until that card), supplier
  commission income, margin, margin % and per traveller. ``notes`` names what is not in the numbers yet (lines still
  planning, an invoice without an FX rate, money frozen in an earlier base currency, AA invoices that differ from the
  quote, a cancelled trip whose charge stands in for its sales) so the card can say so.
- **Cube** (:func:`profit_cube`): the trips that travel in the report window (start date, else the planned month)
  and reached booked (or were cancelled after it), grouped by one dimension: ``lead_source``, ``sales_partner``,
  ``product`` (AA tour vs the agency's own ancillary lines, from the confirmed lines' sell and cost), ``travel_month``
  or ``owner_member``. Rows carry trips, travellers, revenue, cost, commission income, margin and margin %.

Owner / agent only: the routes ask ``view_margin`` before any read (the margin serializer would strip the keys, and
the database returns no money row to anyone else, so an assistant's numbers would be wrong, not just hidden).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.errors import Invalid, NotFound
from app.reports import period as periods
from app.reports import service as reports
from app.tenancy import TenantContext

CENT = Decimal("0.01")
DIMENSIONS = ("lead_source", "sales_partner", "product", "travel_month", "owner_member")
# Trips with money: booked or later, or cancelled (a cancelled trip may keep fees and costs).
MONEY_STAGES = ("booked", "pre_departure", "travelling", "completed", "cancelled")
MAX_TRIPS = 5000

TRIP_SQL = "SELECT * FROM crm.v_trip_pnl WHERE trip_id = $1"
CUBE_SQL = """
SELECT p.trip_id, p.pax, p.lead_source, p.sales_partner_id, p.owner_member_id, p.revenue_base, p.total_cost_base,
       p.commission_income_base, p.margin_base, p.aa_sell_base, p.aa_cost_base, p.agency_sell_base,
       p.agency_cost_base, p.revenue_unconverted, p.lines_other_base,
       date_trunc('month', COALESCE(t.start_date, t.month))::date AS travel_month
  FROM crm.v_trip_pnl p
  JOIN crm.trip t ON t.trip_id = p.trip_id
 WHERE p.stage = ANY($1::text[])
   AND COALESCE(t.start_date, t.month) >= $2 AND COALESCE(t.start_date, t.month) < $3
   AND ($4::uuid IS NULL OR t.owner_member_id = $4)
 ORDER BY p.trip_id
 LIMIT $5
"""


def money(value: Any) -> str:
    return str(Decimal(value or 0).quantize(CENT))


def margin_pct(margin: Decimal, revenue: Decimal) -> str | None:
    if revenue <= 0:
        return None
    return str((margin * 100 / revenue).quantize(CENT))


def view(row: Any) -> dict[str, Any]:
    """The trip card's body from one ``crm.v_trip_pnl`` row."""
    notes = [
        code
        for code, on in (
            ("lines_planning", row["lines_planning"] > 0),
            ("revenue_unconverted", row["revenue_unconverted"] > 0),
            ("lines_other_base", row["lines_other_base"] > 0),
            ("aa_invoice_mismatch", row["aa_lines_mismatch"] > 0),
            # A trip cancelled with a charge (crm-049): the charge stands in for its sales and refunds.
            ("cancellation_charge", bool(row["cancellation_charged"])),
            ("no_revenue", Decimal(row["revenue_base"]) == 0),
        )
        if on
    ]
    return {
        "trip_id": str(row["trip_id"]),
        "base_currency": row["base_currency"],
        "pax": row["pax"],
        "revenue": {
            "sales_base": money(row["revenue_sales_base"]),
            "service_fees_base": money(row["revenue_service_fee_base"]),
            "cancellation_fees_base": money(row["revenue_cancellation_fee_base"]),
            "cancellation_charged": bool(row["cancellation_charged"]),
            "refunds_base": money(row["refunds_base"]),
            "total_base": money(row["revenue_base"]),
        },
        "costs": {
            "aa_quoted_base": money(row["aa_cost_quoted_base"]),
            "aa_base": money(row["aa_cost_base"]),
            "agency_planned_base": money(row["agency_cost_planned_base"]),
            "agency_base": money(row["agency_cost_base"]),
            # Only once a supplier billed a line (crm-25): actual - planned of the billed lines.
            "variance_base": money(row["cost_variance_base"]) if row["lines_billed"] else None,
            "cancellation_base": money(row["cancellation_cost_base"]),
            "departure_base": money(row["departure_cost_base"]),
            "partner_commission_base": money(row["partner_commission_base"]),
            "total_base": money(row["total_cost_base"]),
        },
        "commission_income_base": money(row["commission_income_base"]),
        "margin_base": money(row["margin_base"]),
        "margin_pct": None if row["margin_pct"] is None else money(row["margin_pct"]),
        "margin_per_pax_base": money(row["margin_per_pax_base"]),
        "counts": {
            "lines_confirmed": row["lines_confirmed"],
            "lines_planning": row["lines_planning"],
            "lines_billed": row["lines_billed"],
            "lines_other_base": row["lines_other_base"],
            "aa_lines_invoiced": row["aa_lines_invoiced"],
            "aa_lines_mismatch": row["aa_lines_mismatch"],
            "departure_costs": row["departure_costs"],
            "revenue_unconverted": row["revenue_unconverted"],
        },
        "notes": notes,
    }


# AA-1069 (crm-053): lines asked from their supplier are not confirmed yet either; the crm-051 view counts only the
# planning ones, so the card adds these to its "not counted yet" count.
REQUESTED_SQL = """
SELECT count(*)::int FROM crm.booking_line
 WHERE trip_id = $1 AND supplier_kind = 'agency' AND status = 'requested' AND deleted_at IS NULL
"""


async def trip_pnl(ctx: TenantContext, trip_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if row is None:
        raise NotFound("trip_not_found")
    ctx.audit("view", "allowed", "trip_pnl", str(trip_id))
    requested = int(await ctx.conn.fetchval(REQUESTED_SQL, trip_id) or 0)
    return view({**dict(row), "lines_planning": row["lines_planning"] + requested} if requested else row)


# ── the cube ─────────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Bucket:
    key: str
    trips: set[uuid.UUID] = field(default_factory=set)
    pax: int = 0
    revenue: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)
    income: Decimal = Decimal(0)

    def add(self, trip: uuid.UUID, pax: int, revenue: Any, cost: Any, income: Any = 0) -> None:
        if trip not in self.trips:
            self.trips.add(trip)
            self.pax += pax
        self.revenue += Decimal(revenue or 0)
        self.cost += Decimal(cost or 0)
        self.income += Decimal(income or 0)

    @property
    def margin(self) -> Decimal:
        return self.revenue + self.income - self.cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "trips": len(self.trips),
            "pax": self.pax,
            "revenue_base": money(self.revenue),
            "cost_base": money(self.cost),
            "commission_income_base": money(self.income),
            "margin_base": money(self.margin),
            "margin_pct": margin_pct(self.margin, self.revenue),
        }


def _key(dimension: str) -> Callable[[Any], str]:
    if dimension == "lead_source":
        return lambda r: r["lead_source"] or "unknown"
    if dimension == "sales_partner":
        return lambda r: str(r["sales_partner_id"]) if r["sales_partner_id"] else "none"
    if dimension == "travel_month":
        return lambda r: r["travel_month"].isoformat()[:7] if r["travel_month"] else "unknown"
    return lambda r: str(r["owner_member_id"]) if r["owner_member_id"] else "unassigned"


def cube(rows: list[Any], dimension: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(rows by the dimension's key, the totals). ``product`` splits each trip's confirmed lines into the AA tour and
    the agency's own ancillary services (line sell - line cost); every other dimension takes the trip's P&L whole."""
    if dimension not in DIMENSIONS:
        raise Invalid("invalid_dimension", f"dimension must be one of {', '.join(DIMENSIONS)}")
    buckets: dict[str, Bucket] = {}
    total = Bucket("total")
    for r in rows:
        if dimension == "product":
            parts = (("aa_tour", r["aa_sell_base"], r["aa_cost_base"]), ("ancillary", r["agency_sell_base"],
                     r["agency_cost_base"]))  # fmt: skip
            for key, sell, cost in parts:
                if Decimal(sell or 0) or Decimal(cost or 0):
                    buckets.setdefault(key, Bucket(key)).add(r["trip_id"], r["pax"], sell, cost)
                    total.add(r["trip_id"], r["pax"], sell, cost)
            continue
        key = _key(dimension)(r)
        args = (r["trip_id"], r["pax"], r["revenue_base"], r["total_cost_base"], r["commission_income_base"])
        buckets.setdefault(key, Bucket(key)).add(*args)
        total.add(*args)
    ordered = sorted(buckets.values(), key=lambda b: (-b.margin, b.key))
    if dimension == "travel_month":
        ordered = sorted(buckets.values(), key=lambda b: b.key)
    return [b.as_dict() for b in ordered], total.as_dict()


@dataclass(frozen=True)
class CubeScope:
    window: periods.Window
    base_currency: str
    owner: uuid.UUID | None


async def cube_rows(ctx: TenantContext, period: str, owner: str | None) -> tuple[CubeScope, list[Any]]:
    sc = await reports.scope(ctx, period, owner)
    rows = await ctx.conn.fetch(CUBE_SQL, list(MONEY_STAGES), sc.window.start, sc.window.end_excl, sc.owner, MAX_TRIPS)
    return CubeScope(sc.window, sc.base_currency, sc.owner), list(rows)


async def profit_cube(
    ctx: TenantContext, period: str, owner: str | None, dimension: str, *, audit: bool = True
) -> dict[str, Any]:
    """The cube for the Reports page (``audit``: one ``view`` event; the CSV export writes its own instead)."""
    if dimension not in DIMENSIONS:
        raise Invalid("invalid_dimension", f"dimension must be one of {', '.join(DIMENSIONS)}")
    sc, rows = await cube_rows(ctx, period, owner)
    out_rows, totals = cube(rows, dimension)
    if audit:
        ctx.audit("view", "allowed", "report", "profit", reason=f"dimension_{dimension}")
    return {
        "dimension": dimension,
        "dimensions": list(DIMENSIONS),
        "period": period,
        "window": sc.window.as_dict(),
        "base_currency": sc.base_currency,
        "rows": out_rows,
        "totals": totals,
        "trips_unconverted": sum(1 for r in rows if r["revenue_unconverted"]),
        "trips_other_base": sum(1 for r in rows if r["lines_other_base"]),
        "truncated": len(rows) == MAX_TRIPS,
    }


def csv_rows(result: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    """The cube as CSV rows (app.reports.export): one row per key, then the total."""
    header = [result["dimension"], "currency", "trips", "travellers", "revenue", "cost", "commission_income",
              "margin", "margin_pct"]  # fmt: skip
    cur = result["base_currency"]

    def line(r: dict[str, Any]) -> list[Any]:
        pct = None if r["margin_pct"] is None else Decimal(r["margin_pct"])
        return [r["key"], cur, r["trips"], r["pax"], Decimal(r["revenue_base"]), Decimal(r["cost_base"]),
                Decimal(r["commission_income_base"]), Decimal(r["margin_base"]), pct]  # fmt: skip

    return header, [line(r) for r in result["rows"]] + [line(result["totals"])]
