"""``/crm/v1`` tags and the ready-to-travel list (route class AGENCY; card CRM-29b, AA-960, migration crm-061: R3,
R29, R36; A-02 / A-06 / A-07 / A-15 / Settings > Tags).

==============================================  ===============================================  =====================
Route                                           What                                             Who
==============================================  ===============================================  =====================
``GET    /tags``                                the vocabulary (``applies_to``, ``archived``)    every role
                                                with how many records carry each tag
``POST   /tags``                                a new agency tag (name, tone, applies_to)        every role
``PATCH  /tags/{id}``                           rename, recolour, archive / restore              owner
``DELETE /tags/{id}``                           delete it and its links                          owner
``POST   /tags/{id}/bulk``                      add / remove on a selection (dry run first:      owner, agent
                                                the count; the apply sends ``expected``)
``GET    /trips/{id}/tags``                     the trip's tags (system tags with reasons)       every role
``PUT    /trips/{id}/tags/{tag_id}``            put an agency tag on the trip                    every role
``DELETE /trips/{id}/tags/{tag_id}``            take it off                                      every role
``GET    /travelers/{id}/tags``                 the client's tags                                every role
``PUT    /travelers/{id}/tags/{tag_id}``        put an agency tag on the client                  every role
``DELETE /travelers/{id}/tags/{tag_id}``        take it off                                      every role
``GET    /marketing/ready-to-travel``           R36: clients ready to travel again, with the     owner, agent
                                                reasons and their marketing consent
==============================================  ===============================================  =====================

System tags are the app's (409 ``system_tag`` on any write). The coverage of the automations (R30) is
``GET /workflows/coverage`` in :mod:`app.routers.workflows`.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tags import ready, service
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["tags"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TagIn(_Body):
    applies_to: Literal["trip", "client"]
    name: str = Field(max_length=200)
    tone: str = Field(default="neutral", max_length=20)


class TagChange(_Body):
    name: str | None = Field(default=None, max_length=200)
    tone: str | None = Field(default=None, max_length=20)
    archived: bool | None = None


class BulkFilter(_Body):
    stage: str | None = Field(default=None, max_length=20)
    owner: str | None = Field(default=None, max_length=40)
    tag: str | None = Field(default=None, max_length=40)
    archived: bool | None = None


class BulkIn(_Body):
    action: Literal["add", "remove"]
    ids: list[uuid.UUID] = Field(default_factory=list, max_length=service.MAX_BULK)
    filter: BulkFilter | None = None
    dry_run: bool = True
    expected: int | None = Field(default=None, ge=0, le=service.MAX_BULK)


@router.get("/tags")
async def get_tags(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    applies_to: Annotated[Literal["trip", "client"] | None, Query()] = None,
    archived: bool = False,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PIPELINE_VIEW) as ctx:
        return await service.vocabulary(ctx, applies_to=applies_to, archived=archived)


@router.post("/tags", status_code=201)
async def post_tag(request: Request, caller: CallerDep, tenant_id: TenantDep, body: TagIn) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TAG_APPLY) as ctx:
        return await service.create(ctx, body.applies_to, body.name, body.tone)


@router.patch("/tags/{tag_id}")
async def patch_tag(
    request: Request, caller: CallerDep, tenant_id: TenantDep, tag_id: uuid.UUID, body: TagChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TAG_MANAGE) as ctx:
        return await service.update(ctx, tag_id, name=body.name, tone=body.tone, archived=body.archived)


@router.delete("/tags/{tag_id}")
async def delete_tag(request: Request, caller: CallerDep, tenant_id: TenantDep, tag_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TAG_MANAGE) as ctx:
        return await service.delete(ctx, tag_id)


@router.post("/tags/{tag_id}/bulk")
async def post_bulk(
    request: Request, caller: CallerDep, tenant_id: TenantDep, tag_id: uuid.UUID, body: BulkIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    f = body.filter or BulkFilter()
    selection = service.Selection(
        ids=tuple(dict.fromkeys(body.ids)), stage=f.stage, owner=f.owner, tag=f.tag, archived=f.archived
    )
    async with tenant_tx(pool, caller, tenant_id, action=Action.TAG_BULK_APPLY) as ctx:
        return await service.bulk(
            ctx, tag_id, action=body.action, selection=selection, dry_run=body.dry_run, expected=body.expected
        )


@router.get("/trips/{trip_id}/tags")
async def get_trip_tags(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.of_subject(ctx, trip_id=trip_id)


@router.put("/trips/{trip_id}/tags/{tag_id}")
async def put_trip_tag(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, tag_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TAG_APPLY) as ctx:
        return await service.put(ctx, tag_id, trip_id=trip_id)


@router.delete("/trips/{trip_id}/tags/{tag_id}")
async def delete_trip_tag(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, tag_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TAG_APPLY) as ctx:
        return await service.remove(ctx, tag_id, trip_id=trip_id)


@router.get("/travelers/{traveler_id}/tags")
async def get_client_tags(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_VIEW) as ctx:
        return await service.of_subject(ctx, traveler_id=traveler_id)


@router.put("/travelers/{traveler_id}/tags/{tag_id}")
async def put_client_tag(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID, tag_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TAG_APPLY) as ctx:
        return await service.put(ctx, tag_id, traveler_id=traveler_id)


@router.delete("/travelers/{traveler_id}/tags/{tag_id}")
async def delete_client_tag(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID, tag_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TAG_APPLY) as ctx:
        return await service.remove(ctx, tag_id, traveler_id=traveler_id)


@router.get("/marketing/ready-to-travel")
async def get_ready_to_travel(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    owner: Annotated[Literal["anyone", "me"], Query()] = "anyone",
    page: Annotated[int, Query(ge=1, le=ready.MAX_PAGE)] = 1,
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MARKETING_VIEW) as ctx:
        return await ready.listing(ctx, Vault(keys, ctx), owner=owner, page=page)
