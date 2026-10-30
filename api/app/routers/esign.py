"""``/crm/v1`` e-sign v1 for the agency (route class AGENCY; card CRM-25, AA-955: the trip's Agreements panel).

==============================================  ===============================================  =====================
Route                                           What                                             Who
==============================================  ===============================================  =====================
``GET  /trips/{id}/agreements``                 who signed (self / guardian), insurance choice,  every role
                                                terms acceptances, re-accept state, signing log
``POST /signatures/{id}/envelope``              the sealed envelope + seal check (a reveal:      every role
                                                ``{"reason": <code>}``)
==============================================  ===============================================  =====================

Agreement versions themselves are in :mod:`app.routers.proposals` (``/agreements``); the traveller side is
:mod:`app.routers.client_pages` (``/portal/{token}/sign``).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.esign import service
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["esign"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


class RevealIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=40)


@router.get("/trips/{trip_id}/agreements")
async def get_trip_agreements(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.trip_agreements(ctx, trip_id)


@router.post("/signatures/{request_id}/envelope")
async def post_envelope(
    request: Request, caller: CallerDep, tenant_id: TenantDep, request_id: uuid.UUID, body: RevealIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    sealer = getattr(request.app.state, "sealer", None)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DOCUMENT_DOWNLOAD) as ctx:
        return await service.envelope(ctx, Vault(keys, ctx), sealer, request_id, body.reason)
