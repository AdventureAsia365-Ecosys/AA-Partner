"""``/crm/v1`` duty of care (route class AGENCY; card CRM-30, AA-961, design R37: A-02 Travelling now).

==============================================  ===============================================  =====================
Route                                           What                                             Who
==============================================  ===============================================  =====================
``GET  /duty/travelling?from=&days=``           trips under way or leaving in the window, by     every role
                                                region; travellers once each, no names; the
                                                open AA incident flags with their trips
``GET  /duty/reach?region=&on=``                the travellers in one region on one day           every role
``POST /duty/reveal``                           names / contact / emergency contact of several   every role (logged;
                                                travellers (``traveler_ids``, ``fields``,        reason above 5,
                                                ``reason``)                                      hourly limit, 429)
``GET  /trips/{id}/incidents``                  the AA incident flags on the trip's departures   every role
==============================================  ===============================================  =====================

Nothing here sends a message: the agent reaches the travellers by hand (WhatsApp / SMS is a later module).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.duty import service
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["duty-of-care"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Region = Annotated[str, Query(min_length=2, max_length=7, pattern=r"^([A-Z]{2}|unknown)$")]


class BulkRevealIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    traveler_ids: list[uuid.UUID] = Field(min_length=1, max_length=service.MAX_BULK)
    fields: list[Literal["display_name", "phone", "email", "emergency_contact"]] = Field(min_length=1, max_length=4)
    reason: str | None = Field(default=None, min_length=1, max_length=40)


@router.get("/duty/travelling")
async def get_travelling(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    start: Annotated[dt.date | None, Query(alias="from")] = None,
    days: Annotated[int, Query(ge=1, le=service.MAX_DAYS)] = 7,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DUTY_OF_CARE_VIEW) as ctx:
        return await service.travelling(ctx, start=start, days=days)


@router.get("/duty/reach")
async def get_reach(
    request: Request, caller: CallerDep, tenant_id: TenantDep, region: Region, on: dt.date | None = None
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DUTY_OF_CARE_VIEW) as ctx:
        return await service.reach(ctx, region=region, on=on)


@router.post("/duty/reveal")
async def post_bulk_reveal(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: BulkRevealIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    fields = list(body.fields)
    action = Action.REVEAL_SENSITIVE if service.needs_sensitive(fields) else Action.REVEAL_CONTACT
    async with tenant_tx(pool, caller, tenant_id, action=action) as ctx:
        return await service.bulk_reveal(ctx, Vault(keys, ctx), body.traveler_ids, fields, body.reason)


@router.get("/trips/{trip_id}/incidents")
async def get_trip_incidents(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.trip_incidents(ctx, trip_id)
