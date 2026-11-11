"""``/crm/v1`` sales partners and their commission (route class AGENCY; AA-1071, crm-056; S275 study items crm-28 and
crm-29). Service: :mod:`app.partners.service`.

=================================================  ============================================  ====================
Route                                              What                                          Who
=================================================  ============================================  ====================
``GET    /sales-partners``                         the partners (terms and open totals only for  every member
                                                   view_margin; links for the owner)
``POST   /sales-partners``                         a new partner                                 owner
``GET    /sales-partners/{id}``                    one partner with its contact (logged)         owner
``PATCH  /sales-partners/{id}``                    edit, deactivate, clear the contact           owner
``DELETE /sales-partners/{id}``                    delete an unused partner (else 409)           owner
``POST   /sales-partners/{id}/links``              issue the intake or view link (shown once)    owner
``POST   /sales-partners/{id}/links/{link}/revoke`` revoke a link                                owner
``GET    /sales-partners/{id}/statement``          the partner's commissions as CSV              owner, agent
``PUT    /trips/{id}/sales-partner``               set or clear the trip's partner               every member
``POST   /trips/{id}/partner-commission/sync``     refresh the trip's accrued commission         owner, agent
``GET    /partner-commissions``                    by due date; status open (default) /          owner, agent
                                                   accrued / approved / paid / cancelled / all;
                                                   sales_partner_id, trip_id; base totals
``POST   /partner-commissions/{id}/approve``       approve (after a fresh recalculation)         owner
``POST   /partner-commissions/{id}/unapprove``     back to accrued                               owner
``POST   /partner-commissions/{id}/pay``           paid (date, own reference)                    owner
``POST   /partner-commissions/{id}/cancel``        cancelled by the owner                        owner
=================================================  ============================================  ====================

The commission and the partner's terms are margin fields (design §4.6): the assistant sees the partners' names only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.partners import service
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["sales-partners"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Kind = Literal["sub_agent", "referrer", "freelancer", "ota"]
CommissionType = Literal["percent", "fixed_per_pax", "net_rate"]
Rate = Annotated[Decimal, Field(ge=0, le=Decimal("9999999.99"), decimal_places=2)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PartnerIn(_Body):
    kind: Kind
    name: str = Field(min_length=1, max_length=120)
    contact_email: str | None = Field(default=None, max_length=200)
    contact_phone: str | None = Field(default=None, max_length=40)
    commission_type: CommissionType = "percent"
    rate: Rate = Decimal(0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    basis: Literal["revenue", "margin"] = "revenue"
    payout_due_days: int = Field(default=30, ge=0, le=365)


class PartnerChange(_Body):
    kind: Kind | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    contact_email: str | None = Field(default=None, max_length=200)
    contact_phone: str | None = Field(default=None, max_length=40)
    commission_type: CommissionType | None = None
    rate: Rate | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    basis: Literal["revenue", "margin"] | None = None
    payout_due_days: int | None = Field(default=None, ge=0, le=365)
    status: Literal["active", "inactive"] | None = None


class LinkIn(_Body):
    purpose: Literal["intake", "view"]
    intake_form_id: uuid.UUID | None = None
    days: int | None = Field(default=None, ge=1, le=400)


class TripPartnerIn(_Body):
    sales_partner_id: uuid.UUID | None


class PayIn(_Body):
    paid_on: dt.date | None = None
    reference: str | None = Field(default=None, max_length=120)


@router.get("/sales-partners")
async def get_partners(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.list_partners(ctx, Vault(keys, ctx))


@router.post("/sales-partners", status_code=201)
async def post_partner(request: Request, caller: CallerDep, tenant_id: TenantDep, body: PartnerIn) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SALES_PARTNER_MANAGE) as ctx:
        return await service.create_partner(ctx, Vault(keys, ctx), body.model_dump())


@router.get("/sales-partners/{partner_id}")
async def get_partner(
    request: Request, caller: CallerDep, tenant_id: TenantDep, partner_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SALES_PARTNER_MANAGE) as ctx:
        return await service.get_partner(ctx, Vault(keys, ctx), partner_id)


@router.patch("/sales-partners/{partner_id}")
async def patch_partner(
    request: Request, caller: CallerDep, tenant_id: TenantDep, partner_id: uuid.UUID, body: PartnerChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SALES_PARTNER_MANAGE) as ctx:
        return await service.update_partner(ctx, Vault(keys, ctx), partner_id, body.model_dump(exclude_unset=True))


@router.delete("/sales-partners/{partner_id}")
async def delete_partner(
    request: Request, caller: CallerDep, tenant_id: TenantDep, partner_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SALES_PARTNER_MANAGE) as ctx:
        return await service.delete_partner(ctx, partner_id)


@router.post("/sales-partners/{partner_id}/links", status_code=201)
async def post_link(
    request: Request, caller: CallerDep, tenant_id: TenantDep, partner_id: uuid.UUID, body: LinkIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SALES_PARTNER_MANAGE) as ctx:
        return await service.issue_link(
            ctx, partner_id, body.purpose, intake_form_id=body.intake_form_id, days=body.days
        )


@router.post("/sales-partners/{partner_id}/links/{link_id}/revoke")
async def post_revoke_link(
    request: Request, caller: CallerDep, tenant_id: TenantDep, partner_id: uuid.UUID, link_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SALES_PARTNER_MANAGE) as ctx:
        return await service.revoke_link(ctx, partner_id, link_id)


@router.get("/sales-partners/{partner_id}/statement")
async def get_statement(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    partner_id: uuid.UUID,
    status: Annotated[str | None, Query(max_length=24)] = None,
) -> Response:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.VIEW_MARGIN) as ctx:
        name, body = await service.statement_csv(ctx, partner_id, status=status)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


@router.put("/trips/{trip_id}/sales-partner")
async def put_trip_partner(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: TripPartnerIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await service.set_trip_partner(ctx, trip_id, body.sales_partner_id)


@router.post("/trips/{trip_id}/partner-commission/sync")
async def post_trip_sync(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.VIEW_MARGIN) as ctx:
        return await service.recalculate(ctx, trip_id)


@router.get("/partner-commissions")
async def get_commissions(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    status: Annotated[str | None, Query(max_length=24)] = None,
    sales_partner_id: uuid.UUID | None = None,
    trip_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.VIEW_MARGIN) as ctx:
        return await service.list_commissions(ctx, status=status, partner_id=sales_partner_id, trip_id=trip_id)


@router.post("/partner-commissions/{commission_id}/approve")
async def post_approve(
    request: Request, caller: CallerDep, tenant_id: TenantDep, commission_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PARTNER_COMMISSION_APPROVE) as ctx:
        return await service.approve(ctx, commission_id)


@router.post("/partner-commissions/{commission_id}/unapprove")
async def post_unapprove(
    request: Request, caller: CallerDep, tenant_id: TenantDep, commission_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PARTNER_COMMISSION_APPROVE) as ctx:
        return await service.unapprove(ctx, commission_id)


@router.post("/partner-commissions/{commission_id}/pay")
async def post_pay(
    request: Request, caller: CallerDep, tenant_id: TenantDep, commission_id: uuid.UUID, body: PayIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PARTNER_COMMISSION_APPROVE) as ctx:
        return await service.pay(ctx, commission_id, paid_on=body.paid_on, reference=body.reference)


@router.post("/partner-commissions/{commission_id}/cancel")
async def post_cancel(
    request: Request, caller: CallerDep, tenant_id: TenantDep, commission_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PARTNER_COMMISSION_APPROVE) as ctx:
        return await service.cancel(ctx, commission_id)
