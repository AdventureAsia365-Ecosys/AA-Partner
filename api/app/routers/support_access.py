"""``/crm/v1`` AA support access, the agency side (route class AGENCY; AA-971, card CRM-13b, design v3 WF-12).

======================================  ===================================================  ================
Route                                   What                                                 Who
======================================  ===================================================  ================
``GET  /support-access``                the live grant, the sessions live now (the banner),  every role
                                        the last 90 days of sessions
``POST /support-access``                allow AA support for 1-7 days (show identity or not)  owner
``POST /support-access/revoke``         "End now": the grant ends, its session is revoked    owner
======================================  ===================================================  ================

The service is :mod:`app.support.grants`. Only an owner manages it (permission matrix ``support_access_settings``;
crm-033 refuses anyone else in the database too).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.permissions import Action
from app.routers.agency import tenant_header
from app.support import grants
from app.tenancy import tenant_tx

router = class_router(AGENCY, tags=["support-access"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(default=grants.DAYS_DEFAULT, ge=grants.DAYS_MIN, le=grants.DAYS_MAX)
    show_identity: bool = False


@router.get("/support-access")
async def get_support_access(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SUPPORT_ACCESS_VIEW) as ctx:
        return await grants.support_access(ctx)


@router.post("/support-access")
async def post_support_access(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: GrantIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SUPPORT_ACCESS_SETTINGS) as ctx:
        return await grants.grant_access(ctx, days=body.days, show_identity=body.show_identity)


@router.post("/support-access/revoke")
async def revoke_support_access(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SUPPORT_ACCESS_SETTINGS) as ctx:
        return await grants.revoke_access(ctx)
