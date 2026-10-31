"""``/crm/v1`` commissions (route class AGENCY; card CRM-27, AA-957: A-12 Commissions, the trip's Bookings tab).

==============================================  ===============================================  =====================
Route                                           What                                             Who
==============================================  ===============================================  =====================
``GET  /commissions``                           by due date; ``status`` open (default),          owner, agent
                                                overdue, received, written_off, cancelled, all;
                                                ``trip_id``; totals in the base currency
``GET  /commissions/{id}``                      one commission                                   owner, agent
``PATCH /commissions/{id}``                     the due date                                     owner, agent
``POST /commissions/{id}/claim``                claimed from the supplier (own reference)        owner, agent
``POST /commissions/{id}/unclaim``              back to expected                                 owner, agent
``POST /commissions/{id}/receive``              the money arrived (receipt-date rate, R17)       owner, agent
``POST /commissions/{id}/write-off``            written off (reason code, note)                  owner, agent
==============================================  ===============================================  =====================

Commission is a margin field (design §4.6): the assistant gets 403 ``role_forbidden`` on every route.
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
from app.commissions import service
from app.db import request_pool
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx

router = class_router(AGENCY, tags=["commissions"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimIn(_Body):
    reference: str | None = Field(default=None, max_length=80)


class ReceiveIn(_Body):
    amount: Annotated[Decimal, Field(gt=0, le=Decimal("999999999999.99"), decimal_places=2)]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    received_on: dt.date | None = None
    reference: str | None = Field(default=None, max_length=80)


class WriteOffIn(_Body):
    reason: Literal["supplier_refused", "too_small", "other"]
    note: str | None = Field(default=None, max_length=500)


class CommissionChange(_Body):
    due_on: dt.date


@router.get("/commissions")
async def get_commissions(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    status: Annotated[str | None, Query(max_length=24)] = None,
    trip_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.VIEW_MARGIN) as ctx:
        return await service.listing(ctx, status=status, trip_id=trip_id)


@router.get("/commissions/{commission_id}")
async def get_commission(
    request: Request, caller: CallerDep, tenant_id: TenantDep, commission_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.VIEW_MARGIN) as ctx:
        return await service.get(ctx, commission_id)


@router.patch("/commissions/{commission_id}")
async def patch_commission(
    request: Request, caller: CallerDep, tenant_id: TenantDep, commission_id: uuid.UUID, body: CommissionChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        return await service.set_due(ctx, commission_id, body.due_on)


@router.post("/commissions/{commission_id}/claim")
async def post_claim(
    request: Request, caller: CallerDep, tenant_id: TenantDep, commission_id: uuid.UUID, body: ClaimIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        return await service.claim(ctx, commission_id, body.reference)


@router.post("/commissions/{commission_id}/unclaim")
async def post_unclaim(
    request: Request, caller: CallerDep, tenant_id: TenantDep, commission_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        return await service.unclaim(ctx, commission_id)


@router.post("/commissions/{commission_id}/receive")
async def post_receive(
    request: Request, caller: CallerDep, tenant_id: TenantDep, commission_id: uuid.UUID, body: ReceiveIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        return await service.receive(ctx, commission_id, body.model_dump())


@router.post("/commissions/{commission_id}/write-off")
async def post_write_off(
    request: Request, caller: CallerDep, tenant_id: TenantDep, commission_id: uuid.UUID, body: WriteOffIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        return await service.write_off(ctx, commission_id, body.reason, body.note)
