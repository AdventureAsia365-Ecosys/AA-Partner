"""``/crm/v1`` Adventure Asia supply (route class AGENCY; card CRM-19, AA-947: the trip's Bookings tab, AA items).

=============================================  =============================================  ======================
Route                                          What                                           Who
=============================================  =============================================  ======================
``GET  /aa/catalog``                           served AA tours from the catalog cache         every role
``GET  /aa/products/{source_id}``              components, departures, "from" price (live)    every role
``POST /trips/{id}/aa-lines``                  a new AA line from a quote ("AA pending")      every role (a sell
                                                                                              amount: owner, agent)
``POST /lines/{id}/aa/{action}``               hold / confirm / release / requote             every role (a new
                                                                                              sell: owner, agent;
                                                                                              confirm: owner, agent)
=============================================  =============================================  ======================

Every ask is queued in ``crm.aa_request`` and sent by ``crm_booking_sync`` (:mod:`app.booking.sync`); the answer is
the line as it stands (``aa.pending`` while Booking has not answered). An AA line is never edited (403
``aa_line_read_only`` on the ledger routes). A confirm creates a real AA booking, so it needs ``aa_confirm`` (owner
and agent; the assistant gets 403 ``role_forbidden``, checked in :func:`app.booking.aa_lines.ask`). The product
look-up calls Booking live after the membership check, outside the database transaction.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.booking import aa_lines, products
from app.catalog import store as catalog
from app.db import request_pool
from app.errors import Unavailable
from app.permissions import Action, require
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx

router = class_router(AGENCY, tags=["aa"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
ComponentKind = Literal["trip_plan", "hotel_room", "transfer", "guide", "activity", "entrance", "meal"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaxIn(_Body):
    adult: int = Field(ge=1, le=60)
    child: int = Field(default=0, ge=0, le=60)
    infant: int = Field(default=0, ge=0, le=20)


class RoomIn(_Body):
    occupancy: Literal["single", "double", "twin", "triple", "family"]
    count: int = Field(default=1, ge=1, le=30)


class AaLineIn(_Body):
    tour_source_id: str = Field(min_length=1, max_length=128)
    component_id: str = Field(pattern=r"^[A-Za-z0-9:_.-]{1,64}$")
    component_kind: ComponentKind
    departure_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9:_.-]{1,64}$")
    service_date: dt.date
    nights: int | None = Field(default=None, ge=1, le=60)
    pax: PaxIn
    rooms: list[RoomIn] | None = Field(default=None, max_length=30)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    sell_amount: Decimal | None = Field(default=None, ge=0, le=Decimal("999999999999.99"))


class AskIn(_Body):
    sell_amount: Decimal | None = Field(default=None, ge=0, le=Decimal("999999999999.99"))


def _booking(request: Request) -> tuple[Any, Any]:
    client = getattr(request.app.state, "booking_client", None)
    cache = getattr(request.app.state, "aa_products", None)
    if client is None or cache is None:
        raise Unavailable("booking_not_configured", "AA products are not connected on this environment")
    return client, cache


@router.get("/aa/catalog")
async def get_catalog(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    q: Annotated[str | None, Query(max_length=80)] = None,
    country: Annotated[str | None, Query(pattern=r"^[A-Z]{2}$")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return {"tours": await catalog.search(ctx, (q or "").strip() or None, country, limit)}


@router.get("/aa/products/{source_id}")
async def get_product(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    source_id: str,
    currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
) -> dict[str, Any]:
    client, cache = _booking(request)
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        tour = await catalog.tour(ctx, source_id)
    # Booking is called after the membership check, outside the transaction (no connection held while it answers).
    out = await products.lookup(cache, client, tenant_id, source_id, currency, today=dt.datetime.now(dt.UTC).date())
    return {**out, "tour": tour}


@router.post("/trips/{trip_id}/aa-lines", status_code=201)
async def post_aa_line(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: AaLineIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        if body.sell_amount is not None:
            require(ctx, Action.MONEY_FIELDS_EDIT, object_kind="booking_line")
        return await aa_lines.request_line(ctx, trip_id, body.model_dump(mode="python"))


@router.post("/lines/{line_id}/aa/{action}")
async def post_aa_action(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    line_id: uuid.UUID,
    action: Literal["hold", "confirm", "release", "requote"],
    body: AskIn | None = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        if body is not None and body.sell_amount is not None:
            require(ctx, Action.MONEY_FIELDS_EDIT, object_kind="booking_line", object_id=str(line_id))
        return await aa_lines.ask(ctx, line_id, action, body.model_dump() if body is not None else None)
