"""``/crm/v1/trip-planner`` (route class AGENCY; AA-1139): the agency app's TripPlanner module.

==========================================  =======================================================  ==================
Route                                       What                                                     Who
==========================================  =======================================================  ==================
``GET  /trip-planner``                      CIS's module (planner link, custom domains, lead route,  owner, agent
                                            theme, locale, AI sessions quota) + TripPlanner's usage  (TRIPPLANNER_VIEW)
                                            and webhook key version (each ``null`` when TripPlanner
                                            cannot answer: the page says so for that part only)
``GET  /trip-planner/leads``                the newest leads with their delivery status per channel  owner, agent
``PUT  /trip-planner/lead-route``           where leads go while the agency has no CRM add-on         owner
                                            (none / webhook / e-mail; CIS's checks)                  (MANAGE)
``PUT  /trip-planner/appearance``           the planner's theme (brand colours) and language         owner
``POST /trip-planner/webhook-key``          a new webhook signing key, answered once                 owner
==========================================  =======================================================  ==================

CIS owns the site settings (``tenant.addons.v1`` 1.1, :mod:`app.cis.tripplanner`, scope ``cis.tenant/addons``) and
TripPlanner the leads, usage and key (``lead.submitted.v1`` 1.1, :mod:`app.tripplanner.planner`, scope
``tripplanner.leads/manage``); the CRM stores nothing of them and reads no table of another app (root ADR 0008a).

As for the add-ons (:mod:`app.routers.addons`), no database connection is held across a remote call: ``tenant_tx()``
checks the membership and the role first (a refusal is on record), the remote call runs with no slot of the fair
share, and a write's outcome is recorded in a second short ``tenant_tx()`` in the agency audit:
``tripplanner_manage`` allowed (object ``tripplanner_route`` / ``tripplanner_appearance`` / ``tripplanner_webhook_key``,
reason ``saved`` / ``rotated``) or denied (reason ``cis_<reason>`` / ``tp_<code>``, the closed sets
:data:`app.cis.tripplanner.REASONS` and :data:`app.tripplanner.planner.REASONS`). Never in the audit or a log: the
webhook URL, the e-mail address, a colour or the key.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.cis import tripplanner as cis_tp
from app.db import request_pool
from app.errors import Conflict, Invalid, NotFound, ServiceError, Unavailable
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.tripplanner import planner as tp

log = logging.getLogger(__name__)

router = class_router(AGENCY, tags=["tripplanner"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
NO_STORE = {"Cache-Control": "no-store"}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class LeadRouteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_channel: Literal["none", "webhook", "email"]
    lead_webhook_url: str | None = Field(default=None, max_length=cis_tp.WEBHOOK_URL_MAX)
    lead_email: str | None = Field(default=None, max_length=cis_tp.EMAIL_MAX)

    @field_validator("lead_webhook_url", "lead_email")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class AppearanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme: dict[str, str] = Field(max_length=len(cis_tp.THEME_TOKENS))
    locale: Literal["en", "vi"]

    @field_validator("theme")
    @classmethod
    def _brand_tokens(cls, value: dict[str, str]) -> dict[str, str]:
        for key, colour in value.items():
            if key not in cis_tp.THEME_TOKENS or not _HEX.match(colour):
                raise ValueError("theme names the brand tokens only, each #rrggbb")
        return {k: c.lower() for k, c in value.items()}


def _cis(request: Request) -> Any:
    """The CRM's CIS client config (shared with the add-ons and the studio activation); None = unconfigured."""
    return getattr(request.app.state, "studio_activation", None)


def _tp(request: Request) -> Any:
    """TripPlanner's origin + the CRM's M2M client (the lead pull's config); None = unconfigured."""
    return getattr(request.app.state, "tp_planner", None)


def _cis_error(exc: cis_tp.ModuleRefused | cis_tp.ModuleUnavailable, tenant_id: uuid.UUID) -> ServiceError:
    if isinstance(exc, cis_tp.ModuleUnavailable):
        log.warning("crm_tripplanner_cis_unavailable tenant_id=%s code=%s", tenant_id, exc.code)
        return Unavailable("tripplanner_unavailable", "The Trip planner settings cannot be read right now.")
    if exc.status == 404:
        return NotFound(exc.reason, "This agency has no Adventure Asia account for the Trip planner yet.")
    if exc.status == 409:
        return Conflict(exc.reason, "Adventure Asia cannot take this change now.")
    return Invalid(exc.reason, "The value was not accepted.")


def _tp_error(exc: tp.PlannerRefused | tp.PlannerUnavailable, tenant_id: uuid.UUID) -> ServiceError:
    if isinstance(exc, tp.PlannerRefused) and exc.code == "unknown_tenant":
        return NotFound("tripplanner_not_active", "This agency has no Trip planner.")
    if isinstance(exc, tp.PlannerRefused):
        return Unavailable(exc.code, "The webhook signing key cannot be made right now.")
    log.warning("crm_tripplanner_tp_unavailable tenant_id=%s code=%s", tenant_id, exc.code)
    return Unavailable("planner_unavailable", "The Trip planner cannot answer right now; try again later.")


async def _soft(call: Any) -> dict[str, Any] | None:
    """A TripPlanner part of the module page: None when TripPlanner cannot answer (the page says so for that part)."""
    try:
        return await call
    except (tp.PlannerUnavailable, tp.PlannerRefused):
        return None


async def _check(request: Request, caller: Caller, tenant_id: uuid.UUID, action: Action) -> uuid.UUID:
    """The membership and the role (a refusal is on record); answers the member id. Nothing is read."""
    async with tenant_tx(await request_pool(request), caller, tenant_id, action=action) as ctx:
        return ctx.member_id


@router.get("/trip-planner")
async def get_trip_planner(request: Request, caller: CallerDep, tenant_id: TenantDep) -> JSONResponse:
    await _check(request, caller, tenant_id, Action.TRIPPLANNER_VIEW)
    try:
        module = await cis_tp.get_module(_cis(request), tenant_id)
    except (cis_tp.ModuleRefused, cis_tp.ModuleUnavailable) as exc:
        raise _cis_error(exc, tenant_id) from None
    usage = key = None
    if module["active"]:
        usage = await _soft(tp.usage(_tp(request), tenant_id))
        key = await _soft(tp.key_info(_tp(request), tenant_id))
    return JSONResponse({"module": module, "usage": usage, "webhook_key": key}, headers=NO_STORE)


@router.get("/trip-planner/leads")
async def get_trip_planner_leads(request: Request, caller: CallerDep, tenant_id: TenantDep) -> JSONResponse:
    await _check(request, caller, tenant_id, Action.TRIPPLANNER_VIEW)
    try:
        body = await tp.leads(_tp(request), tenant_id)
    except (tp.PlannerRefused, tp.PlannerUnavailable) as exc:
        raise _tp_error(exc, tenant_id) from None
    return JSONResponse(body, headers=NO_STORE)


@router.put("/trip-planner/lead-route")
async def put_trip_planner_lead_route(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: LeadRouteIn
) -> JSONResponse:
    member_id = await _check(request, caller, tenant_id, Action.TRIPPLANNER_MANAGE)
    try:
        module = await cis_tp.put_lead_route(
            _cis(request),
            tenant_id,
            member_id,
            channel=body.lead_channel,
            webhook_url=body.lead_webhook_url,
            email=body.lead_email,
        )
    except cis_tp.ModuleRefused as exc:
        async with tenant_tx(await request_pool(request), caller, tenant_id, action=Action.TRIPPLANNER_MANAGE) as ctx:
            ctx.audit(Action.TRIPPLANNER_MANAGE.value, "denied", "tripplanner_route", reason=f"cis_{exc.reason}"[:64])
        raise _cis_error(exc, tenant_id) from None
    except cis_tp.ModuleUnavailable as exc:
        raise _cis_error(exc, tenant_id) from None
    async with tenant_tx(await request_pool(request), caller, tenant_id, action=Action.TRIPPLANNER_MANAGE) as ctx:
        ctx.audit(Action.TRIPPLANNER_MANAGE.value, "allowed", "tripplanner_route", reason="saved")
    log.info("crm_tripplanner_route_set tenant_id=%s channel=%s", tenant_id, body.lead_channel)
    return JSONResponse({"module": module}, headers=NO_STORE)


@router.put("/trip-planner/appearance")
async def put_trip_planner_appearance(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: AppearanceIn
) -> JSONResponse:
    member_id = await _check(request, caller, tenant_id, Action.TRIPPLANNER_MANAGE)
    try:
        module = await cis_tp.put_appearance(_cis(request), tenant_id, member_id, theme=body.theme, locale=body.locale)
    except cis_tp.ModuleRefused as exc:
        async with tenant_tx(await request_pool(request), caller, tenant_id, action=Action.TRIPPLANNER_MANAGE) as ctx:
            ctx.audit(
                Action.TRIPPLANNER_MANAGE.value, "denied", "tripplanner_appearance", reason=f"cis_{exc.reason}"[:64]
            )
        raise _cis_error(exc, tenant_id) from None
    except cis_tp.ModuleUnavailable as exc:
        raise _cis_error(exc, tenant_id) from None
    async with tenant_tx(await request_pool(request), caller, tenant_id, action=Action.TRIPPLANNER_MANAGE) as ctx:
        ctx.audit(Action.TRIPPLANNER_MANAGE.value, "allowed", "tripplanner_appearance", reason="saved")
    log.info("crm_tripplanner_appearance_set tenant_id=%s locale=%s", tenant_id, body.locale)
    return JSONResponse({"module": module}, headers=NO_STORE)


@router.post("/trip-planner/webhook-key")
async def post_trip_planner_webhook_key(request: Request, caller: CallerDep, tenant_id: TenantDep) -> JSONResponse:
    """A new signing key, answered once (the page shows it once; the CRM never stores or logs it)."""
    member_id = await _check(request, caller, tenant_id, Action.TRIPPLANNER_MANAGE)
    try:
        rotated = await tp.rotate_key(_tp(request), tenant_id, member_id)
    except tp.PlannerRefused as exc:
        async with tenant_tx(await request_pool(request), caller, tenant_id, action=Action.TRIPPLANNER_MANAGE) as ctx:
            ctx.audit(
                Action.TRIPPLANNER_MANAGE.value, "denied", "tripplanner_webhook_key", reason=f"tp_{exc.code}"[:64]
            )
        raise _tp_error(exc, tenant_id) from None
    except tp.PlannerUnavailable as exc:
        raise _tp_error(exc, tenant_id) from None
    async with tenant_tx(await request_pool(request), caller, tenant_id, action=Action.TRIPPLANNER_MANAGE) as ctx:
        ctx.audit(Action.TRIPPLANNER_MANAGE.value, "allowed", "tripplanner_webhook_key", reason="rotated")
    log.info("crm_tripplanner_key_rotated tenant_id=%s version=%s", tenant_id, rotated["version"])
    return JSONResponse(rotated, headers=NO_STORE)
