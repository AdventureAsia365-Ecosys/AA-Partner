"""``/crm/v1`` the agency's side of the Module 8 ops release (route class AGENCY; AA-956, card CRM-26).

==============================================  ===============================================  =====================
Route                                           What                                             Who
==============================================  ===============================================  =====================
``GET  /trips/{id}/ops-release``                whether AA ground operations hold the trip's     every role (activity:
                                                details: status, completeness, refresh / view    owner, agent)
                                                times, purge countdown or receipt; the ops lines
                                                of the trip's Activity (A-03)
``POST /trips/{id}/ops-release/withdraw``       withdraw the live release (C12 step 6 and the    owner, agent
                                                agency's own choice; AA ops lose the details at  (``trip_cancel``)
                                                once, purged before departure)
==============================================  ===============================================  =====================

The document itself is never served here: the agency has the source data. The trip cancel (``POST
/trips/{id}/cancel``, :func:`app.stages.cancel`) withdraws the release in the same run.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Request

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.ops_release import service
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx

router = class_router(AGENCY, tags=["ops-release"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


@router.get("/trips/{trip_id}/ops-release")
async def get_ops_release(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.agency_view(ctx, trip_id)


@router.post("/trips/{trip_id}/ops-release/withdraw")
async def post_withdraw(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    notices: service.Notices = []
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_CANCEL) as ctx:
        view = await service.withdraw(ctx, trip_id, "agency_withdrawn", notices=notices)
    service.emit(notices)  # committed: AA ops are told (ops check ops_release_withdrawn, S263)
    return view
