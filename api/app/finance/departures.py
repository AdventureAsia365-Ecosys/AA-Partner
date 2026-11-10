"""Group departure costs and the departure P&L (AA-1068 item crm-32; ``crm.departure_cost``, ``crm.v_departure_trip``,
``crm.trip.group_code``, crm-051).

- A **departure ref** is an AA departure id (the ``departure_id`` of a live AA line) or the agency's own group code
  (``crm.trip.group_code``, set on the trip: :func:`set_group_code`). The live trips (not lost or cancelled) on a ref
  share its costs.
- A **departure cost** (:func:`create_cost`, :func:`update_cost`, :func:`delete_cost`) is something the agency pays
  for the whole group (its own tour leader, a group flight block, a welcome dinner) to one of its suppliers:
  ``per_departure`` (split by pax over the trips, the shares add up exactly) or ``per_pax`` (an amount per
  traveller). The FX of the cost day is frozen when it is written (409 ``fx_rate_missing`` without a rate). Each
  trip's share is in its P&L (``crm.v_trip_pnl.departure_cost_base``).
- The **departure P&L** (:func:`departure_pnl`): the costs, the trips with their pax, their share and their own
  margin, and the totals. The departure roster page (a later card) shows it; the trip's Money card links here.

Owner / agent only (``view_margin`` to read, ``money_fields_edit`` to write); the group code is a trip edit.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound
from app.finance.bills import _amount, _text
from app.finance.pnl import money
from app.ledger import fx
from app.tenancy import TenantContext

REF = re.compile(r"^[A-Za-z0-9:_.-]{1,80}$")
BASES = ("per_departure", "per_pax")
MAX_COSTS = 100

TRIP_SQL = "SELECT stage FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
GROUP_SQL = "UPDATE crm.trip SET group_code = $2 WHERE trip_id = $1"
SUPPLIER_SQL = "SELECT kind, deleted_at FROM crm.agency_supplier WHERE agency_supplier_id = $1"
COST_COLUMNS = """
c.departure_cost_id, c.departure_ref, c.agency_supplier_id, s.name AS supplier_name, c.title, c.amount, c.currency,
c.basis, c.cost_date, c.base_currency, c.amount_base, c.fx_rate, c.fx_rate_date, c.fx_source, c.stale_days
"""
COSTS_SQL = f"""
SELECT {COST_COLUMNS}
  FROM crm.departure_cost c JOIN crm.agency_supplier s ON s.agency_supplier_id = c.agency_supplier_id
 WHERE c.departure_ref = $1 AND c.deleted_at IS NULL
 ORDER BY c.cost_date, c.created_at, c.departure_cost_id
 LIMIT $2
"""  # noqa: S608 — fixed text
COST_SQL = f"""
SELECT {COST_COLUMNS}
  FROM crm.departure_cost c JOIN crm.agency_supplier s ON s.agency_supplier_id = c.agency_supplier_id
 WHERE c.departure_cost_id = $1 AND c.deleted_at IS NULL
"""  # noqa: S608 — fixed text
INSERT_SQL = """
INSERT INTO crm.departure_cost (departure_ref, agency_supplier_id, title, amount, currency, basis, cost_date,
                                base_currency, amount_base, fx_rate, fx_rate_date, fx_source, stale_days)
VALUES ($1, $2, $3, $4::numeric, $5, $6, $7, $8, round($4::numeric * $9::numeric, 2), $9::numeric, $10, $11, $12)
RETURNING departure_cost_id
"""
UPDATE_SQL = """
UPDATE crm.departure_cost
   SET agency_supplier_id = $2, title = $3, amount = $4::numeric, currency = $5, basis = $6, cost_date = $7,
       base_currency = $8, amount_base = round($4::numeric * $9::numeric, 2), fx_rate = $9::numeric,
       fx_rate_date = $10, fx_source = $11, stale_days = $12
 WHERE departure_cost_id = $1
"""
DELETE_SQL = "DELETE FROM crm.departure_cost WHERE departure_cost_id = $1"
TRIPS_SQL = """
SELECT d.trip_id, d.pax, d.stage, d.start_date, t.title, p.departure_cost_base, p.revenue_base, p.total_cost_base,
       p.margin_base
  FROM crm.v_departure_trip d
  JOIN crm.trip t ON t.trip_id = d.trip_id
  LEFT JOIN crm.v_trip_pnl p ON p.trip_id = d.trip_id
 WHERE d.departure_ref = $1
 ORDER BY d.trip_id
"""
TRIP_REFS_SQL = "SELECT departure_ref FROM crm.v_departure_trip WHERE trip_id = $1 ORDER BY departure_ref"


def ref(value: Any) -> str:
    if not isinstance(value, str) or not REF.match(value):
        raise Invalid("invalid_departure_ref", "1 to 80 letters, digits or : _ . -")
    return value


def cost_view(row: Any) -> dict[str, Any]:
    return {
        "departure_cost_id": str(row["departure_cost_id"]),
        "departure_ref": row["departure_ref"],
        "agency_supplier_id": str(row["agency_supplier_id"]),
        "supplier_name": row["supplier_name"],
        "title": row["title"],
        "basis": row["basis"],
        "cost_date": row["cost_date"].isoformat(),
        "group_cost": {
            "amount": str(row["amount"]),
            "currency": row["currency"],
            "amount_base": str(row["amount_base"]),
            "base_currency": row["base_currency"],
            "fx": {
                "rate": str(row["fx_rate"]),
                "rate_date": row["fx_rate_date"].isoformat(),
                "source": row["fx_source"],
                "stale_days": row["stale_days"],
            },  # fmt: skip
        },
    }


async def set_group_code(ctx: TenantContext, trip_id: uuid.UUID, code: str | None) -> dict[str, Any]:
    if await ctx.conn.fetchval(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    value = None if code in (None, "") else ref(code)
    await ctx.conn.execute(GROUP_SQL, trip_id, value)
    ctx.audit("update", "allowed", "trip", str(trip_id), fields=("group_code",))
    return {"trip_id": str(trip_id), "group_code": value, "departure_refs": await trip_refs(ctx, trip_id)}


async def trip_refs(ctx: TenantContext, trip_id: uuid.UUID) -> list[str]:
    return [r["departure_ref"] for r in await ctx.conn.fetch(TRIP_REFS_SQL, trip_id)]


async def _validated(ctx: TenantContext, body: dict[str, Any]) -> tuple[Any, ...]:
    supplier_id: uuid.UUID = body["agency_supplier_id"]
    supplier = await ctx.conn.fetchrow(SUPPLIER_SQL, supplier_id)
    if supplier is None or supplier["deleted_at"] is not None:
        raise NotFound("supplier_not_found")
    title = _text(body.get("title"), "title", 120)
    amount = _amount(body.get("amount"))
    currency = fx.currency(body.get("currency"))
    basis = body.get("basis") or "per_departure"
    if basis not in BASES:
        raise Invalid("invalid_basis", ", ".join(BASES))
    cost_date: dt.date = body.get("cost_date") or await fx.agency_today(ctx)
    today = await fx.agency_today(ctx)
    if abs((cost_date - today).days) > 730:
        raise Invalid("invalid_cost_date", "within two years of today")
    settings = await fx.settings(ctx)
    # A future cost day converts at today's rate (the latest known); the stored date stays the cost day.
    rate = await fx.resolve(ctx, currency, settings["base_currency"], min(cost_date, today))
    if rate is None:
        raise Conflict("fx_rate_missing", "no exchange rate for this currency in the last 30 days", currency=currency)
    return (supplier_id, title, amount, currency, basis, cost_date, settings["base_currency"], rate.rate,
            rate.rate_date, rate.source, min(rate.stale_days, 366))  # fmt: skip


async def create_cost(ctx: TenantContext, body: dict[str, Any]) -> dict[str, Any]:
    departure = ref(body.get("departure_ref"))
    if len(await ctx.conn.fetch(COSTS_SQL, departure, MAX_COSTS)) >= MAX_COSTS:
        raise Conflict("too_many_costs", f"at most {MAX_COSTS} costs per departure")
    values = await _validated(ctx, body)
    try:
        cost_id = await ctx.conn.fetchval(INSERT_SQL, departure, *values)
    except asyncpg.CheckViolationError:
        raise Invalid("invalid_departure_cost", "check the amount and the title") from None
    ctx.audit("create", "allowed", "departure_cost", str(cost_id))
    return cost_view(await ctx.conn.fetchrow(COST_SQL, cost_id))


async def update_cost(ctx: TenantContext, cost_id: uuid.UUID, changes: dict[str, Any]) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(COST_SQL, cost_id)
    if row is None:
        raise NotFound("departure_cost_not_found")
    merged = {
        "agency_supplier_id": row["agency_supplier_id"],
        "title": row["title"],
        "amount": row["amount"],
        "currency": row["currency"],
        "basis": row["basis"],
        "cost_date": row["cost_date"],
        **{k: v for k, v in changes.items() if v is not None},
    }
    values = await _validated(ctx, merged)
    await ctx.conn.execute(UPDATE_SQL, cost_id, *values)
    ctx.audit("update", "allowed", "departure_cost", str(cost_id), fields=tuple(sorted(changes)))
    return cost_view(await ctx.conn.fetchrow(COST_SQL, cost_id))


async def delete_cost(ctx: TenantContext, cost_id: uuid.UUID) -> dict[str, Any]:
    if await ctx.conn.fetchrow(COST_SQL, cost_id) is None:
        raise NotFound("departure_cost_not_found")
    await ctx.conn.execute(DELETE_SQL, cost_id)
    ctx.audit("delete", "allowed", "departure_cost", str(cost_id))
    return {"departure_cost_id": str(cost_id), "deleted": True}


async def departure_pnl(ctx: TenantContext, departure_ref: str) -> dict[str, Any]:
    departure = ref(departure_ref)
    costs = [cost_view(r) for r in await ctx.conn.fetch(COSTS_SQL, departure, MAX_COSTS)]
    trips = list(await ctx.conn.fetch(TRIPS_SQL, departure))
    settings = await fx.settings(ctx)
    base = settings["base_currency"]
    pax = sum(int(t["pax"]) for t in trips)
    cost_total = sum(
        (Decimal(c["group_cost"]["amount_base"]) * (pax if c["basis"] == "per_pax" else 1)
         for c in costs if c["group_cost"]["base_currency"] == base),
        Decimal(0),
    )  # fmt: skip
    totals = {
        "pax": pax,
        "trips": len(trips),
        "departure_cost_base": money(cost_total),
        "revenue_base": money(sum((Decimal(t["revenue_base"] or 0) for t in trips), Decimal(0))),
        "trip_cost_base": money(sum((Decimal(t["total_cost_base"] or 0) for t in trips), Decimal(0))),
        "margin_base": money(sum((Decimal(t["margin_base"] or 0) for t in trips), Decimal(0))),
    }
    ctx.audit("view", "allowed", "departure", departure, object_ids=tuple(str(t["trip_id"]) for t in trips))
    return {
        "departure_ref": departure,
        "base_currency": base,
        "costs": costs,
        "trips": [
            {
                "trip_id": str(t["trip_id"]),
                "title": t["title"],
                "stage": t["stage"],
                "start_date": t["start_date"].isoformat() if t["start_date"] else None,
                "pax": t["pax"],
                "departure_cost_base": money(t["departure_cost_base"]),
                "revenue_base": money(t["revenue_base"]),
                "total_cost_base": money(t["total_cost_base"]),
                "margin_base": money(t["margin_base"]),
            }
            for t in trips
        ],
        "totals": totals,
        "costs_other_base": sum(1 for c in costs if c["group_cost"]["base_currency"] != base),
    }
