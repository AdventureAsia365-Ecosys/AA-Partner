"""``/crm/v1/studio/trip-planner`` (route class AGENCY; AA-1139 fix pass): the TripPlanner half of the Content studio's
Trip planner page, for an agency WITHOUT the CRM add-on.

==============================================  ===================================================  ==============
Route                                           What                                                 Who (studio)
==============================================  ===================================================  ==============
``GET  /studio/trip-planner``                   TripPlanner's usage and webhook key version (each    owner, editor
                                                ``null`` when TripPlanner cannot answer)
``GET  /studio/trip-planner/leads``             the newest leads with their delivery status          owner, editor
``POST /studio/trip-planner/webhook-key``       a new webhook signing key, answered once             owner
==============================================  ===================================================  ==============

Why here. An agency without the CRM add-on has no CRM agency (CIS creates one only for a live ``crm`` subscription),
yet that is the agency whose leads go by e-mail or webhook: it needs the signing key and the delivery status. Only this
API holds the M2M client TripPlanner trusts (scope ``tripplanner.leads/manage``), so the studio's server (``web/app/api/
studio/trip-planner``) forwards the person's own partner-pool access token here with the studio's tenant as
``X-AA-Tenant-Id``. CIS serves the other half of the page (site settings, ``/v1/account/tripplanner``) to the studio
token directly.

Who. Not ``crm.member`` (the person may have no CRM membership at all): CIS ``tenant.membership.v1`` says which ACTIVE
Content studio membership the caller's ``(iss, sub)`` holds in that tenant (:mod:`app.cis.studio_member`), never the
request. No membership: 403 ``not_studio_member``; a viewer (reads) or an editor (the key): 403 ``role_forbidden``,
before TripPlanner hears of it. A CRM agency's people keep using ``/crm/v1/trip-planner`` (the CRM roles); this route is
open to them as studio members too, with the studio role.

No database: nothing of the CRM is read or written (no ``tenant_tx()``: there may be no agency). The key rotation is
recorded by TripPlanner (``rotated_by`` ``studio-member:<membership id>``, tp-035) and by a log line here (ids and
version only); never the key, in a log or anywhere else.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.cis import studio_member
from app.routers.agency import tenant_header
from app.routers.tripplanner_module import _soft, _tp, _tp_error
from app.tripplanner import planner as tp

log = logging.getLogger(__name__)

router = class_router(AGENCY, tags=["tripplanner"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
NO_STORE = {"Cache-Control": "no-store"}
READERS = frozenset({"owner", "editor"})
MANAGERS = frozenset({"owner"})


async def _member(request: Request, caller: Caller, tenant_id: uuid.UUID, roles: frozenset[str]):
    """The caller's active studio membership in ``tenant_id`` with one of ``roles`` (CIS's answer), else 403."""
    config = getattr(request.app.state, "studio_activation", None)
    member = await studio_member.lookup(config, caller.iss, caller.sub, tenant_id)
    if member is None:
        log.warning("crm_studio_tripplanner_refused tenant_id=%s reason=not_studio_member", tenant_id)
        raise HTTPException(status_code=403, detail="not_studio_member")
    if member.role not in roles:
        log.warning("crm_studio_tripplanner_refused tenant_id=%s reason=role_forbidden role=%s", tenant_id, member.role)
        raise HTTPException(status_code=403, detail="role_forbidden")
    return member


@router.get("/studio/trip-planner")
async def get_studio_trip_planner(request: Request, caller: CallerDep, tenant_id: TenantDep) -> JSONResponse:
    await _member(request, caller, tenant_id, READERS)
    usage: Any = await _soft(tp.usage(_tp(request), tenant_id))
    key: Any = await _soft(tp.key_info(_tp(request), tenant_id))
    return JSONResponse({"usage": usage, "webhook_key": key}, headers=NO_STORE)


@router.get("/studio/trip-planner/leads")
async def get_studio_trip_planner_leads(request: Request, caller: CallerDep, tenant_id: TenantDep) -> JSONResponse:
    await _member(request, caller, tenant_id, READERS)
    try:
        body = await tp.leads(_tp(request), tenant_id)
    except (tp.PlannerRefused, tp.PlannerUnavailable) as exc:
        raise _tp_error(exc, tenant_id) from None
    return JSONResponse(body, headers=NO_STORE)


@router.post("/studio/trip-planner/webhook-key")
async def post_studio_trip_planner_webhook_key(
    request: Request, caller: CallerDep, tenant_id: TenantDep
) -> JSONResponse:
    """A new signing key, answered once (the studio page shows it once; the CRM never stores or logs it)."""
    member = await _member(request, caller, tenant_id, MANAGERS)
    try:
        rotated = await tp.rotate_key(_tp(request), tenant_id, member.membership_id, studio=True)
    except (tp.PlannerRefused, tp.PlannerUnavailable) as exc:
        log.warning("crm_studio_tripplanner_key_refused tenant_id=%s code=%s", tenant_id, getattr(exc, "code", ""))
        raise _tp_error(exc, tenant_id) from None
    log.info(
        "crm_studio_tripplanner_key_rotated tenant_id=%s membership_id=%s version=%s",
        tenant_id,
        member.membership_id,
        rotated["version"],
    )
    return JSONResponse(rotated, headers=NO_STORE)
