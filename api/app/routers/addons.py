"""``/crm/v1`` add-ons (route class AGENCY; AA-1036 R3, root ADR 0019 d7): the agency's add-ons and its owner's request.

=====================================  ==============================================================  =============
Route                                  What                                                            Who
=====================================  ==============================================================  =============
``GET  /add-ons``                      each add-on (CRM, Trip planner) active / requested / available,  owner
                                       the pending request and the last decision (CIS's answer)         (PLAN_VIEW)
``POST /add-on-requests``              ask Adventure Asia for an add-on, with an optional note          owner
                                       (201 a new request, 200 the open one, ``created`` false)          (ADDON_REQUEST)
=====================================  ==============================================================  =============

Found live on Dev (S269): an agency owner with the CRM but no Content studio membership followed the CRM's
"Trip planner: request add-on" entry to the studio and got "No Content studio access", so a CRM-only agency could not
ask for an add-on at all. These routes give the CRM owner the same request path: the CRM API relays it to CIS
``tenant.addons.v1`` (:mod:`app.cis.addons`, M2M scope ``cis.tenant/addons``), so the requests keep one store in CIS
(``shared.tenant_addon_request``) and one staff decision path (CIS Tenant 360). CIS applies the studio's rules.

No database connection is held across the CIS call: ``tenant_tx()`` checks the membership and the role first (a
refusal is on record as usual), the CIS call runs with no slot of the fair share, and a second short ``tenant_tx()``
records the outcome in the agency audit: ``addon_request`` allowed (object the CIS request id, reason
``<product>_created`` / ``<product>_open``) or denied (reason ``cis_<CIS reason>``). If that second write fails after
CIS stored a new request, the request stands (CIS's own audit names the CRM member) and the page shows it as requested.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.cis import addons as cis_addons
from app.db import request_pool
from app.errors import Conflict, Invalid, NotFound, RateLimited, ServiceError, Unavailable
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx

log = logging.getLogger(__name__)

router = class_router(AGENCY, tags=["addons"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]

RETRY_AFTER_DEFAULT_S = 3600
NO_STORE = {"Cache-Control": "no-store"}


class AddOnRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: Literal["crm", "tripplanner"]
    note: str | None = Field(default=None, max_length=cis_addons.NOTE_MAX)

    @field_validator("note")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


def _cis(request: Request) -> Any:
    """The CRM's CIS client config (CIS origin + M2M client), shared with the studio activation; None = unconfigured."""
    return getattr(request.app.state, "studio_activation", None)


def _unavailable(exc: cis_addons.AddOnsUnavailable, tenant_id: uuid.UUID) -> ServiceError:
    log.warning("crm_addons_unavailable tenant_id=%s code=%s", tenant_id, exc.code)
    return Unavailable("addons_unavailable", "Add-ons cannot be read right now; try again later.")


def _refused(exc: cis_addons.AddOnsRefused) -> ServiceError:
    if exc.status == 404:
        return NotFound(exc.reason, "This agency has no Adventure Asia account for add-ons yet.")
    if exc.status == 409:
        return Conflict(exc.reason, "Adventure Asia cannot take this request now.")
    if exc.status == 429:
        return RateLimited(exc.reason, exc.retry_after_s or RETRY_AFTER_DEFAULT_S, "Too many add-on requests today.")
    return Invalid(exc.reason, "The request was not accepted.")


@router.get("/add-ons")
async def get_add_ons(request: Request, caller: CallerDep, tenant_id: TenantDep) -> JSONResponse:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PLAN_VIEW):
        pass  # the membership and the role; nothing is read here
    try:
        body = await cis_addons.list_add_ons(_cis(request), tenant_id)
    except cis_addons.AddOnsRefused as exc:
        raise _refused(exc) from None
    except cis_addons.AddOnsUnavailable as exc:
        raise _unavailable(exc, tenant_id) from None
    return JSONResponse(body, headers=NO_STORE)


@router.post("/add-on-requests")
async def post_add_on_request(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: AddOnRequestIn
) -> JSONResponse:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.ADDON_REQUEST) as ctx:
        member_id = ctx.member_id
    refused: cis_addons.AddOnsRefused | None = None
    try:
        done = await cis_addons.request_add_on(_cis(request), tenant_id, body.product, body.note, member_id)
    except cis_addons.AddOnsRefused as exc:
        refused = exc
    except cis_addons.AddOnsUnavailable as exc:
        raise _unavailable(exc, tenant_id) from None
    async with tenant_tx(pool, caller, tenant_id, action=Action.ADDON_REQUEST) as ctx:
        if refused is not None:
            ctx.audit(Action.ADDON_REQUEST.value, "denied", "addon_request", reason=f"cis_{refused.reason}"[:64])
            raise _refused(refused)
        ctx.audit(
            Action.ADDON_REQUEST.value,
            "allowed",
            "addon_request",
            done.request["request_id"],
            reason=f"{body.product}_{'created' if done.created else 'open'}",
        )
    log.info("crm_addon_requested tenant_id=%s product=%s created=%s", tenant_id, body.product, done.created)
    return JSONResponse(
        {"created": done.created, "request": done.request}, status_code=201 if done.created else 200, headers=NO_STORE
    )
