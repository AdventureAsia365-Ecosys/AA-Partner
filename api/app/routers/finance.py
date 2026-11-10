"""``/crm/v1`` CRM finance (route class AGENCY; AA-1068, migration crm-051: trip profit, supplier bills, "Bookings with
AA", group departure costs, the profit cube and the Home "today" tiles).

==================================================  ==========================================  ====================
Route                                               What                                        Who
==================================================  ==========================================  ====================
``GET    /trips/{id}/pnl``                          the trip's Money card (crm.v_trip_pnl)      owner, agent
``PUT    /trips/{id}/group-code``                   the trip's own group departure code         every role
``GET    /supplier-bills?agency_supplier_id=&trip_id=``  the supplier bills                     owner, agent
``POST   /supplier-bills``                          record a bill and the lines it pays         owner, agent
``GET    /supplier-bills/{id}``                     one bill                                    owner, agent
``PATCH  /supplier-bills/{id}``                     paid (paid day) / void, the note            owner, agent
``GET    /aa-bookings``                             AA bookings, AA invoices, billing status    owner, agent
``GET    /aa-bookings/invoices/{number}``           one AA invoice read live                    owner, agent
``GET    /departures/{ref}/pnl``                    a group departure's costs, trips, P&L       owner, agent
``POST   /departure-costs``                         add a group cost                            owner, agent
``PATCH  /departure-costs/{id}``                    change it (FX of its day again)             owner, agent
``DELETE /departure-costs/{id}``                    delete it                                   owner, agent
``GET    /reports/profit?period=&owner=&dimension=``  the profit cube                           owner, agent
``GET    /reports/agenda``                          the Home "today" tiles (counts only)        every role
==================================================  ==========================================  ====================

The profit cube's CSV is the ``profit`` section of ``GET /reports/export`` (owner / agent, :mod:`app.reports.export`).
Booking is called outside any transaction (the membership is checked first, :mod:`app.finance.aa_invoices`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.finance import aa_invoices, agenda, bills, departures, pnl
from app.permissions import Action
from app.reports.period import DEFAULT_PERIOD
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx

router = class_router(AGENCY, tags=["finance"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Period = Annotated[Literal["30d", "90d", "12m", "ytd"], Query()]
Owner = Annotated[
    str | None,
    Query(pattern=r"^(me|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"),
]
Dimension = Annotated[Literal["lead_source", "sales_partner", "product", "travel_month", "owner_member"], Query()]
DepartureRef = Annotated[str, Path(pattern=r"^[A-Za-z0-9:_.-]{1,80}$")]
Amount = Annotated[Decimal, Field(gt=0, le=Decimal("99999999999.99"), max_digits=13, decimal_places=2)]
GROUP_CODE = r"^[A-Za-z0-9:_.-]{1,80}$"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroupCode(_Body):
    group_code: str | None = Field(default=None, pattern=GROUP_CODE)


class BillLineIn(_Body):
    booking_line_id: uuid.UUID
    amount: Annotated[Decimal, Field(ge=0, le=Decimal("99999999999.99"), max_digits=13, decimal_places=2)]


class BillIn(_Body):
    agency_supplier_id: uuid.UUID
    number: str = Field(min_length=1, max_length=80)
    bill_date: dt.date
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount: Amount
    note: str | None = Field(default=None, max_length=500)
    lines: list[BillLineIn] = Field(min_length=1, max_length=bills.MAX_LINES)


class BillChange(_Body):
    status: Literal["paid", "void"] | None = None
    paid_on: dt.date | None = None
    note: str | None = Field(default=None, max_length=500)


class DepartureCostIn(_Body):
    departure_ref: str = Field(pattern=GROUP_CODE)
    agency_supplier_id: uuid.UUID
    title: str = Field(min_length=1, max_length=120)
    amount: Amount
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    basis: Literal["per_departure", "per_pax"] = "per_departure"
    cost_date: dt.date | None = None


class DepartureCostChange(_Body):
    agency_supplier_id: uuid.UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=120)
    amount: Amount | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    basis: Literal["per_departure", "per_pax"] | None = None
    cost_date: dt.date | None = None


# ── the trip ─────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/trips/{trip_id}/pnl")
async def get_trip_pnl(request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.VIEW_MARGIN) as ctx:
        out = await pnl.trip_pnl(ctx, trip_id)
        out["group_code"] = await ctx.conn.fetchval("SELECT group_code FROM crm.trip WHERE trip_id = $1", trip_id)
        out["departure_refs"] = await departures.trip_refs(ctx, trip_id)
        return out


@router.put("/trips/{trip_id}/group-code")
async def put_group_code(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: GroupCode
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await departures.set_group_code(ctx, trip_id, body.group_code)


# ── supplier bills ───────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/supplier-bills")
async def get_bills(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    agency_supplier_id: uuid.UUID | None = None,
    trip_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.VIEW_MARGIN) as ctx:
        return await bills.list_bills(ctx, supplier_id=agency_supplier_id, trip_id=trip_id)


@router.post("/supplier-bills", status_code=201)
async def post_bill(request: Request, caller: CallerDep, tenant_id: TenantDep, body: BillIn) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MONEY_FIELDS_EDIT) as ctx:
        return await bills.create_bill(ctx, body.model_dump(mode="python"))


@router.get("/supplier-bills/{bill_id}")
async def get_bill(request: Request, caller: CallerDep, tenant_id: TenantDep, bill_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.VIEW_MARGIN) as ctx:
        return await bills.get_bill(ctx, bill_id)


@router.patch("/supplier-bills/{bill_id}")
async def patch_bill(
    request: Request, caller: CallerDep, tenant_id: TenantDep, bill_id: uuid.UUID, body: BillChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MONEY_FIELDS_EDIT) as ctx:
        return await bills.update_bill(ctx, bill_id, body.model_dump(exclude_unset=True))


# ── Bookings with AA ─────────────────────────────────────────────────────────────────────────────────────────────
def _booking_client(request: Request) -> Any:
    return getattr(request.app.state, "booking_client", None)


@router.get("/aa-bookings")
async def get_aa_bookings(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    return await aa_invoices.page(
        lambda: tenant_tx(pool, caller, tenant_id, action=Action.VIEW_MARGIN),
        _booking_client(request),
        tenant_id,
        dt.datetime.now(dt.UTC).date(),
    )


@router.get("/aa-bookings/invoices/{number}")
async def get_aa_invoice(
    request: Request, caller: CallerDep, tenant_id: TenantDep, number: Annotated[str, Path(max_length=64)]
) -> dict[str, Any]:
    pool = await request_pool(request)
    return await aa_invoices.invoice_detail(
        lambda: tenant_tx(pool, caller, tenant_id, action=Action.VIEW_MARGIN),
        _booking_client(request),
        tenant_id,
        number,
    )


# ── group departures ─────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/departures/{departure_ref}/pnl")
async def get_departure_pnl(
    request: Request, caller: CallerDep, tenant_id: TenantDep, departure_ref: DepartureRef
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.VIEW_MARGIN) as ctx:
        return await departures.departure_pnl(ctx, departure_ref)


@router.post("/departure-costs", status_code=201)
async def post_departure_cost(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: DepartureCostIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MONEY_FIELDS_EDIT) as ctx:
        return await departures.create_cost(ctx, body.model_dump(mode="python"))


@router.patch("/departure-costs/{cost_id}")
async def patch_departure_cost(
    request: Request, caller: CallerDep, tenant_id: TenantDep, cost_id: uuid.UUID, body: DepartureCostChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MONEY_FIELDS_EDIT) as ctx:
        return await departures.update_cost(ctx, cost_id, body.model_dump(exclude_unset=True, mode="python"))


@router.delete("/departure-costs/{cost_id}")
async def delete_departure_cost(
    request: Request, caller: CallerDep, tenant_id: TenantDep, cost_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MONEY_FIELDS_EDIT) as ctx:
        return await departures.delete_cost(ctx, cost_id)


# ── reports ──────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/reports/profit")
async def get_profit(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    period: Period = DEFAULT_PERIOD,
    owner: Owner = None,
    dimension: Dimension = "lead_source",
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.VIEW_MARGIN) as ctx:
        return await pnl.profit_cube(ctx, period, owner, dimension)


@router.get("/reports/agenda")
async def get_agenda(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.REPORTS_VIEW) as ctx:
        return await agenda.agenda(ctx)
