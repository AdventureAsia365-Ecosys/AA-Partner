"""``/crm/v1/data``: portability exports and the data-request log (AA-967, card CRM-35, Module 18).

================================================  =======================================================  ==========
Route                                             What                                                     Who
================================================  =======================================================  ==========
``GET  /data/exports``                            the latest exports (status, progress, checksum, expiry)  owner
``POST /data/exports``                            queue an agency or one client's export                   owner
``POST /data/exports/{id}/link``                  a single-use download link (minutes)                     owner
``GET  /data/exports/{id}/download?token=``       the ZIP (with the link's token)                          owner
``GET  /data/requests``                           the data-request log                                     owner
``POST /data/requests``                           log a request a client made                              owner
``POST /data/requests/{id}/extend``               move the due date (up to received + 92 days)             owner
``POST /data/requests/{id}/close``                done / refused / withdrawn with an outcome               owner
================================================  =======================================================  ==========

Permission-matrix actions: ``agency_export`` (an agency export, its link and download), ``client_export`` (a client
export), ``data_settings`` (the log and the export list); all three are owner-only (AA-939 matrix), and the services
check the owner again with a committed ``denied`` event. A client's erasure stays ``POST /travelers/{id}/erasure``
(:mod:`app.routers.lifecycle`). AA staff tokens never reach these routes (route class AGENCY: 403).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.permissions import Action
from app.portability import exports, requests
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["data"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExportIn(_Body):
    scope: Literal["tenant", "traveler"] = "tenant"
    traveler_id: uuid.UUID | None = None
    data_request_id: uuid.UUID | None = None


class RequestIn(_Body):
    kind: Literal[requests.KINDS]  # type: ignore[valid-type]
    channel: Literal[requests.CHANNELS]  # type: ignore[valid-type]
    received_on: dt.date
    due_on: dt.date | None = None
    traveler_id: uuid.UUID | None = None


class ExtendIn(_Body):
    due_on: dt.date


class CloseIn(_Body):
    status: Literal["done", "refused", "withdrawn"]
    outcome: str = Field(min_length=1, max_length=40)


@router.get("/data/exports")
async def get_exports(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DATA_SETTINGS) as ctx:
        return await exports.list_exports(ctx)


@router.post("/data/exports", status_code=201)
async def post_export(request: Request, caller: CallerDep, tenant_id: TenantDep, body: ExportIn) -> dict[str, Any]:
    pool = await request_pool(request)
    action = Action.AGENCY_EXPORT if body.scope == "tenant" else Action.CLIENT_EXPORT
    async with tenant_tx(pool, caller, tenant_id, action=action) as ctx:
        return await exports.request(ctx, body.scope, body.traveler_id, data_request_id=body.data_request_id)


@router.post("/data/exports/{export_id}/link")
async def post_export_link(
    request: Request, caller: CallerDep, tenant_id: TenantDep, export_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
        return await exports.create_link(ctx, export_id, request.app.state.documents)


@router.get("/data/exports/{export_id}/download")
async def get_export_download(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    export_id: uuid.UUID,
    token: Annotated[str, Query(min_length=20, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")],
) -> Response:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
        content, name, digest = await exports.download(
            ctx, Vault(keys, ctx), request.app.state.documents, export_id, token
        )
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Content-SHA256": digest,
        },
    )


@router.get("/data/requests")
async def get_requests(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DATA_SETTINGS) as ctx:
        return await requests.list_requests(ctx)


@router.post("/data/requests", status_code=201)
async def post_request(request: Request, caller: CallerDep, tenant_id: TenantDep, body: RequestIn) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DATA_SETTINGS) as ctx:
        return await requests.create(
            ctx,
            kind=body.kind,
            channel=body.channel,
            received_on=body.received_on,
            due_on=body.due_on,
            traveler_id=body.traveler_id,
        )


@router.post("/data/requests/{data_request_id}/extend")
async def post_request_extend(
    request: Request, caller: CallerDep, tenant_id: TenantDep, data_request_id: uuid.UUID, body: ExtendIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DATA_SETTINGS) as ctx:
        return await requests.extend(ctx, data_request_id, body.due_on)


@router.post("/data/requests/{data_request_id}/close")
async def post_request_close(
    request: Request, caller: CallerDep, tenant_id: TenantDep, data_request_id: uuid.UUID, body: CloseIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DATA_SETTINGS) as ctx:
        return await requests.close(ctx, data_request_id, body.status, body.outcome)
