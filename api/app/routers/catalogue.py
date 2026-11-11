"""``/crm/v1`` the agency service catalogue (route class AGENCY; AA-1069, migration crm-052, S275 study item crm-23).

=============================================  ==============================================  ====================
Route                                          What                                            Who
=============================================  ==============================================  ====================
``GET    /services?category=&active=``         the catalogue (net and tiers: owner, agent)     every role
``POST   /services``                           add a service (with its tiers)                  owner, agent
``GET    /services/{id}``                      one service                                     every role
``PATCH  /services/{id}``                      change it (``tiers`` replaces the tier list)    owner, agent
``DELETE /services/{id}``                      delete it (options added from it keep their     owner, agent
                                               snapshot)
=============================================  ==============================================  ====================

"Add from catalogue" in the trip builder is ``POST /proposals/{id}/blocks/{block}/catalogue-options``
(:mod:`app.routers.builder`).
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
from app.catalogue import service
from app.db import request_pool
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx

router = class_router(AGENCY, tags=["catalogue"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Unit = Annotated[Decimal, Field(ge=0, le=service.MAX_UNIT, max_digits=10, decimal_places=2)]
Category = Literal["flight", "insurance", "visa", "transfer", "fee"]
Basis = Literal["per_person", "per_adult", "per_booking", "per_room", "per_night", "per_person_night"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TierIn(_Body):
    min_pax: int = Field(ge=1, le=999)
    max_pax: int | None = Field(default=None, ge=1, le=999)
    unit_net: Unit


class ServiceIn(_Body):
    agency_supplier_id: uuid.UUID | None = None
    category: Category
    name: str = Field(min_length=1, max_length=120)
    unit_net: Unit | None = None
    unit_sell: Unit
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    qty_basis: Basis
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    active: bool = True
    tiers: list[TierIn] = Field(default_factory=list, max_length=service.MAX_TIERS)


class ServiceChange(_Body):
    agency_supplier_id: uuid.UUID | None = None
    category: Category | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    unit_net: Unit | None = None
    unit_sell: Unit | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    qty_basis: Basis | None = None
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    active: bool | None = None
    tiers: list[TierIn] | None = Field(default=None, max_length=service.MAX_TIERS)


def _body(model: BaseModel) -> dict[str, Any]:
    """The fields the caller sent (an explicit null clears a nullable field); tiers as plain dicts."""
    data = model.model_dump(exclude_unset=True, mode="python")
    if data.get("tiers") is not None:
        data["tiers"] = [dict(t) for t in data["tiers"]]
    for required in ("category", "name", "unit_sell", "currency", "qty_basis", "active"):
        if required in data and data[required] is None:
            data.pop(required)  # a PATCH null on a NOT NULL field keeps the value
    return data


@router.get("/services")
async def get_services(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    category: Annotated[Category | None, Query()] = None,
    active: Annotated[bool | None, Query()] = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.list_services(ctx, category=category, active=active)


@router.post("/services", status_code=201)
async def post_service(request: Request, caller: CallerDep, tenant_id: TenantDep, body: ServiceIn) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MONEY_FIELDS_EDIT) as ctx:
        return await service.create_service(ctx, _body(body))


@router.get("/services/{service_id}")
async def get_service(
    request: Request, caller: CallerDep, tenant_id: TenantDep, service_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.get_service(ctx, service_id)


@router.patch("/services/{service_id}")
async def patch_service(
    request: Request, caller: CallerDep, tenant_id: TenantDep, service_id: uuid.UUID, body: ServiceChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MONEY_FIELDS_EDIT) as ctx:
        return await service.update_service(ctx, service_id, _body(body))


@router.delete("/services/{service_id}")
async def delete_service(
    request: Request, caller: CallerDep, tenant_id: TenantDep, service_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MONEY_FIELDS_EDIT) as ctx:
        return await service.delete_service(ctx, service_id)
