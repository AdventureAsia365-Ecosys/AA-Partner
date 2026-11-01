"""``/crm/v1`` SMS and WhatsApp (route class AGENCY; card CRM-28, AA-958: Settings > Channels and the composer of the
trip Messages tab / Inbox).

=============================================  =============================================  ======================
Route                                          What                                           Who
=============================================  =============================================  ======================
``GET    /messaging``                          status + channels (every role); the owner      every role
                                               also gets the connection, the webhook URLs
                                               and the templates
``PUT    /messaging/connection``               Account SID, Auth Token (+ API key), senders;  owner
                                               the account is read before it is stored        (INTEGRATIONS_MANAGE)
``POST   /messaging/test``                     account read now + a test message to my phone  owner
``POST   /messaging/templates/refresh``        queue a template sync from the account         owner
``DELETE /messaging/connection``               disconnect (the credential is deleted)         owner
``GET    /messaging/compose``                  what the composer may send on a conversation   every role
                                               or a trip (channels, window, templates)
``POST   /messaging/messages``                 send an SMS / WhatsApp (10 s Undo; 409         MESSAGE_SEND
                                               template_required outside the window)
=============================================  =============================================  ======================

Undo / Edit of a queued text message are the mail routes (``POST /messages/{id}/undo`` and ``/edit``); threads,
Unmatched and Link are the mail conversation routes (every channel). No response carries a secret.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.errors import Invalid, Unavailable
from app.messaging import compose, connection, health
from app.messaging.providers import ProviderError
from app.messaging.state import MessagingRuntime
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["messaging"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Channel = Literal["sms", "whatsapp"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectionIn(_Body):
    provider: Literal["twilio", "sandbox"] = "twilio"
    account_sid: str = Field(min_length=34, max_length=34)
    auth_token: str = Field(min_length=16, max_length=128)
    api_key_sid: str | None = Field(default=None, max_length=34)
    api_key_secret: str | None = Field(default=None, max_length=64)
    sms_sender: str | None = Field(default=None, max_length=40)
    whatsapp_sender: str | None = Field(default=None, max_length=40)


class TestIn(_Body):
    channel: Channel | None = None
    to: str | None = Field(default=None, max_length=40)


class SendIn(_Body):
    message_id: uuid.UUID
    conversation_id: uuid.UUID | None = None
    trip_id: uuid.UUID | None = None
    channel: Channel | None = None
    body: str | None = Field(default=None, max_length=5000)
    content_sid: str | None = Field(default=None, pattern=r"^HX[0-9a-f]{32}$")
    variables: dict[str, str] | None = Field(default=None, max_length=100)


def _runtime(request: Request) -> MessagingRuntime:
    runtime: MessagingRuntime | None = getattr(request.app.state, "messaging_runtime", None)
    if runtime is None:
        raise Unavailable("messaging_not_configured")
    return runtime


@router.get("/messaging")
async def get_messaging(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    runtime = _runtime(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SESSION_VIEW) as ctx:
        return await connection.view(ctx, runtime)


@router.put("/messaging/connection")
async def put_connection(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: ConnectionIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    runtime = _runtime(request)
    # Authorise first (membership + owner): the account read below calls the provider with what the caller typed.
    async with tenant_tx(pool, caller, tenant_id, action=Action.INTEGRATIONS_MANAGE):
        pass
    checked = connection.validate(
        provider=body.provider,
        account_sid=body.account_sid,
        auth_token=body.auth_token,
        api_key_sid=body.api_key_sid,
        api_key_secret=body.api_key_secret,
        sms_sender=body.sms_sender,
        whatsapp_sender=body.whatsapp_sender,
        environment_providers=tuple(runtime.providers),
    )
    try:
        # Proved before anything is stored, outside any transaction (a network call).
        await runtime.providers[body.provider].check(checked["secret"])
    except ProviderError as exc:
        raise Invalid(f"messaging_{exc.code}", "the account refused these credentials") from None
    async with tenant_tx(pool, caller, tenant_id, action=Action.INTEGRATIONS_MANAGE) as ctx:
        return await connection.connect(
            ctx,
            Vault(keys, ctx),
            runtime,
            provider=body.provider,
            secret=checked["secret"],
            sms_sender=checked["sms_sender"],
            whatsapp_sender=checked["whatsapp_sender"],
        )


@router.post("/messaging/test")
async def post_test(request: Request, caller: CallerDep, tenant_id: TenantDep, body: TestIn) -> dict[str, Any]:
    pool = await request_pool(request)
    runtime = _runtime(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.INTEGRATIONS_MANAGE) as ctx:
        return await health.test_connection(ctx, runtime, channel=body.channel, to=body.to)


@router.post("/messaging/templates/refresh", status_code=202)
async def post_refresh(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.INTEGRATIONS_MANAGE) as ctx:
        await connection.queue_template_sync(ctx, reason="refresh")
        return {"status": "queued"}


@router.delete("/messaging/connection")
async def delete_connection(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.INTEGRATIONS_MANAGE) as ctx:
        return await connection.disconnect(ctx)


@router.get("/messaging/compose")
async def get_compose(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    conversation_id: uuid.UUID | None = None,
    trip_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await compose.compose_state(ctx, Vault(keys, ctx), conversation_id=conversation_id, trip_id=trip_id)


@router.post("/messaging/messages", status_code=202)
async def post_message(request: Request, caller: CallerDep, tenant_id: TenantDep, body: SendIn) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MESSAGE_SEND) as ctx:
        return await compose.send(
            ctx,
            Vault(keys, ctx),
            body.message_id,
            conversation_id=body.conversation_id,
            trip_id=body.trip_id,
            channel=body.channel,
            body=body.body,
            content_sid=body.content_sid,
            variables=body.variables,
        )
