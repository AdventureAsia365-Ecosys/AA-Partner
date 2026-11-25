"""``/crm/v1`` agency calendar and ICS feeds (route class AGENCY; card CRM-30, AA-961, design R15 / A-09).

==============================================  ===============================================  =====================
Route                                           What                                             Who
==============================================  ===============================================  =====================
``GET  /calendar?from=&to=&scope=``             departures, returns, payment / invoice due,      every role
                                                signing deadlines, tasks, calls (scope ``mine``
                                                default, or ``agency``)
``GET  /calendar/feed``                         my ICS feed (active, or the last revoked one)    every role
``POST /calendar/feed``                         a new feed (``{"scope"}``); the URL once; an     every role (own)
                                                older active feed of mine is revoked
``POST /calendar/feeds/{id}/revoke``            stop a feed (mine; an owner: any)                every role
``GET  /calendar/feeds``                        the agency's active feeds                        owner
==============================================  ===============================================  =====================

The feed itself is served on ``/crm/public/v1/calendar/{token}.ics`` (:mod:`app.routers.public`). No Google or
Microsoft calendar is written in this slice (bring-your-own sync comes with a later card).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.calendar import connections, events, feeds, oauth_sync
from app.db import request_pool
from app.errors import Unavailable
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["calendar"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


class FeedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["mine", "agency"] = "mine"


class ConnectStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["google", "microsoft"]
    scope: Literal["mine", "agency"] = "mine"


class ConnectComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=4096)


def _clients(request: Request) -> dict[str, Any]:
    runtime = getattr(request.app.state, "mail_runtime", None)
    if runtime is None:
        raise Unavailable("mail_not_configured")
    return runtime.calendars


@router.get("/calendar")
async def get_calendar(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    start: Annotated[dt.date | None, Query(alias="from")] = None,
    end: Annotated[dt.date | None, Query(alias="to")] = None,
    scope: Literal["mine", "agency"] = "mine",
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CALENDAR_VIEW) as ctx:
        return await events.agency_calendar(ctx, start=start, end=end, mine=scope == "mine")


@router.get("/calendar/feed")
async def get_feed(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CALENDAR_FEED_MANAGE) as ctx:
        return await feeds.status(ctx)


@router.post("/calendar/feed", status_code=201)
async def post_feed(request: Request, caller: CallerDep, tenant_id: TenantDep, body: FeedIn) -> dict[str, Any]:
    pool = await request_pool(request)
    origin = request.app.state.settings.invitation_origin
    async with tenant_tx(pool, caller, tenant_id, action=Action.CALENDAR_FEED_MANAGE) as ctx:
        return await feeds.create(ctx, scope=body.scope, origin=origin)


@router.post("/calendar/feeds/{feed_id}/revoke")
async def post_revoke(request: Request, caller: CallerDep, tenant_id: TenantDep, feed_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CALENDAR_FEED_MANAGE) as ctx:
        return await feeds.revoke(ctx, feed_id)


@router.get("/calendar/feeds")
async def get_team_feeds(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CALENDAR_FEED_MANAGE) as ctx:
        return await feeds.team(ctx)


# ── AA-1011: the member's own Google / Microsoft 365 calendar ────────────────────────────────────


@router.get("/calendar/connections")
async def get_connections(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    clients = _clients(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CALENDAR_FEED_MANAGE) as ctx:
        return await connections.view(ctx, Vault(keys, ctx), clients)


@router.post("/calendar/connections/oauth/start")
async def post_connection_start(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: ConnectStart
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CALENDAR_FEED_MANAGE) as ctx:
        return await oauth_sync.start(ctx, Vault(keys, ctx), request.app.state.settings, body.provider, body.scope)


@router.post("/calendar/connections/oauth/complete", status_code=201)
async def post_connection_complete(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: ConnectComplete
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    clients = _clients(request)
    settings = request.app.state.settings
    async with tenant_tx(pool, caller, tenant_id, action=Action.CALENDAR_FEED_MANAGE) as ctx:
        vault = Vault(keys, ctx)
        done = await oauth_sync.complete(ctx, vault, settings, clients, body.code, body.state)
        return await connections.connect(ctx, vault, provider=done.provider, scope=done.scope, secret=done.secret)


@router.post("/calendar/connections/{connection_id}/disconnect")
async def post_connection_disconnect(
    request: Request, caller: CallerDep, tenant_id: TenantDep, connection_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CALENDAR_FEED_MANAGE) as ctx:
        return await connections.disconnect(ctx, Vault(keys, ctx), connection_id)
