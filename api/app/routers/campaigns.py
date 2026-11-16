"""``/crm/v1`` campaigns (route class AGENCY; card CRM-36, AA-968, migrations crm-073 / crm-074: R14, A-15 Marketing).

==============================================  ===============================================  =====================
Route                                           What                                             Who
==============================================  ===============================================  =====================
``GET    /campaigns``                           the agency's campaigns with their counts          owner, agent
``GET    /campaigns/options``                   the editor's mailboxes, client tags, AA blocks,   owner, agent
                                                limits in force
``GET    /campaigns/tours?q=``                  served AA tours by title (the CRM's cache)        owner, agent
``GET    /campaigns/blocks/{id}``               one AA campaign block's text                      owner, agent
``POST   /campaigns``                           a new draft (gate codes in the answer)            owner, agent
``GET    /campaigns/{id}``                      one campaign: text, AA subject, counts            owner, agent
``PATCH  /campaigns/{id}``                      edit a draft or a held campaign (``version``)     owner, agent
``DELETE /campaigns/{id}``                      delete a draft                                    owner, agent
``POST   /campaigns/{id}/preview``              gate, eligible and left-out counts, days          owner, agent
``POST   /campaigns/{id}/schedule``             plan and schedule (``expected``, ``version``)     owner, agent
``POST   /campaigns/{id}/cancel``               cancel (planned clients skipped, unsent undone)   owner, agent
==============================================  ===============================================  =====================

Every write needs the ``campaigns`` feature on the agency's plan (403 ``feature_not_allowed``); reads answer
``feature_allowed`` so the page can say so. The unsubscribe routes are public (:mod:`app.routers.public`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.campaigns import audience, model, service
from app.db import request_pool
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["campaigns"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CampaignIn(_Body):
    name: str = Field(max_length=400)
    locale: str = Field(default="en", max_length=5)
    subject: str = Field(default="", max_length=2000)
    body: str = Field(default="", max_length=60_000)
    channel_account_id: uuid.UUID | None = None
    aa_tour_ids: list[str] = Field(default_factory=list, max_length=model.LIST_ITEMS)
    aa_content_id: uuid.UUID | None = None
    brand_only: bool = False
    audience_tags: list[str] = Field(default_factory=list, max_length=model.LIST_ITEMS)


class CampaignChange(CampaignIn):
    version: int = Field(ge=1)


class ScheduleIn(_Body):
    scheduled_at: dt.datetime | None = None
    expected: int = Field(ge=0)
    version: int = Field(ge=1)


class VersionIn(_Body):
    version: int = Field(ge=1)


def campaign_links(request: Request) -> audience.Links:
    """Where the agency's campaign links may point (:func:`app.campaigns.audience.links_from`)."""
    return audience.links_from(getattr(request.app.state, "settings", None))


@router.get("/campaigns")
async def get_campaigns(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MARKETING_VIEW) as ctx:
        return await service.listing(ctx)


@router.get("/campaigns/options")
async def get_options(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MARKETING_VIEW) as ctx:
        return await service.options(ctx, Vault(keys, ctx), campaign_links(request))


@router.get("/campaigns/tours")
async def get_tours(
    request: Request, caller: CallerDep, tenant_id: TenantDep, q: Annotated[str, Query(max_length=200)] = ""
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MARKETING_VIEW) as ctx:
        return await service.tours(ctx, q)


@router.get("/campaigns/blocks/{content_id}")
async def get_block(request: Request, caller: CallerDep, tenant_id: TenantDep, content_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MARKETING_VIEW) as ctx:
        return await service.block(ctx, content_id)


@router.post("/campaigns", status_code=201)
async def post_campaign(request: Request, caller: CallerDep, tenant_id: TenantDep, body: CampaignIn) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CAMPAIGN_SEND) as ctx:
        return await service.create(ctx, Vault(keys, ctx), body.model_dump(), campaign_links(request))


@router.get("/campaigns/{campaign_id}")
async def get_campaign(
    request: Request, caller: CallerDep, tenant_id: TenantDep, campaign_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MARKETING_VIEW) as ctx:
        return await service.detail(ctx, Vault(keys, ctx), campaign_id)


@router.patch("/campaigns/{campaign_id}")
async def patch_campaign(
    request: Request, caller: CallerDep, tenant_id: TenantDep, campaign_id: uuid.UUID, body: CampaignChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    fields = body.model_dump(exclude={"version"})
    async with tenant_tx(pool, caller, tenant_id, action=Action.CAMPAIGN_SEND) as ctx:
        return await service.update(ctx, Vault(keys, ctx), campaign_id, fields, body.version, campaign_links(request))


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(
    request: Request, caller: CallerDep, tenant_id: TenantDep, campaign_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CAMPAIGN_SEND) as ctx:
        return await service.delete(ctx, campaign_id)


@router.post("/campaigns/{campaign_id}/preview")
async def post_preview(
    request: Request, caller: CallerDep, tenant_id: TenantDep, campaign_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CAMPAIGN_SEND) as ctx:
        return await service.preview(ctx, Vault(keys, ctx), campaign_id, campaign_links(request))


@router.post("/campaigns/{campaign_id}/schedule")
async def post_schedule(
    request: Request, caller: CallerDep, tenant_id: TenantDep, campaign_id: uuid.UUID, body: ScheduleIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CAMPAIGN_SEND) as ctx:
        return await service.schedule(
            ctx, Vault(keys, ctx), campaign_id, at=body.scheduled_at, expected=body.expected, version=body.version,
            links=campaign_links(request),
        )  # fmt: skip


@router.post("/campaigns/{campaign_id}/cancel")
async def post_cancel(
    request: Request, caller: CallerDep, tenant_id: TenantDep, campaign_id: uuid.UUID, body: VersionIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CAMPAIGN_SEND) as ctx:
        return await service.cancel(ctx, campaign_id, body.version)
