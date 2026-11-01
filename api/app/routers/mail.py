"""``/crm/v1`` mail (route class AGENCY; card CRM-17, AA-945: Settings > Mailbox, the trip Messages tab, the Unmatched
inbox, saved replies, the history import).

=============================================  =============================================  ======================
Route                                          What                                           Who
=============================================  =============================================  ======================
``GET    /mailbox``                            my mailbox + the agency's: kind, address,      every role
                                               health, held count; which providers are on
``POST   /mailbox/oauth/start``                Gmail / Microsoft 365 consent URL (PKCE)       own: every role;
``POST   /mailbox/oauth/complete``             code + state -> connected, held re-queued      agency: owner
``PUT    /mailbox/smtp``                       host / port / TLS / user / app password,       (OWN_MAILBOX_CONNECT /
                                               login checked before it is stored              INTEGRATIONS_MANAGE)
``POST   /mailbox/test``                       health check now + a test mail to me
``DELETE /mailbox``                            disconnect (the credential is deleted)
``GET    /mailbox/ingest-address``             the forward-in address + Gmail forwarding code every role
``GET    /mail/senders``                       the sender table (R24)                         every role
``GET    /conversations``                      by trip / client / Unmatched / no trip (ids)   every role
``GET    /conversations/{id}/messages``        the thread, decrypted (one view event)         every role
``POST   /conversations/{id}/link``            Unmatched -> a client (and trip)               every role
``PUT    /messages/drafts/{id}``               autosave a draft (id chosen by the composer)   every role
``DELETE /messages/drafts/{id}``               discard my draft                               every role
``POST   /messages``                           send (10 s Undo; assistant: assigned trips)    MESSAGE_SEND
``POST   /messages/{id}/undo``                 undo within the delay -> draft                 the author
``POST   /messages/{id}/edit``                 a held / failed message back to a draft        the author
``GET    /saved-replies``                      agency-wide + mine                             every role
``POST   /saved-replies``                      create (agency-wide: owner)                    every role
``PATCH  /saved-replies/{id}``                 edit                                           owner / author
``DELETE /saved-replies/{id}``                 delete                                         owner / author
``POST   /mail/imports?source_kind=``          mbox / CSV upload (raw body, <= 25 MB)         every role
``GET    /mail/imports/{id}``                  progress (counts)                              the member / owner
=============================================  =============================================  ======================

No response carries a ciphertext, blind index or secret (``tests/unit/test_vault_rules.py``); the credential is never
read back.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.errors import Invalid, PayloadTooLarge, Unavailable
from app.mail import conversations, health, history_import, mailbox, oauth, senders
from app.mail.providers import ProviderError
from app.mail.state import MailRuntime
from app.mail.store import require_ingest_store
from app.permissions import Action, require
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["mail"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Scope = Literal["own", "agency"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OAuthStart(_Body):
    provider: Literal["google", "microsoft"]
    scope: Scope = "own"


class OAuthComplete(_Body):
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=4096)


class SmtpIn(_Body):
    scope: Scope = "own"
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    security: Literal["starttls", "tls"]
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=512)
    address: str = Field(min_length=3, max_length=320)


class ScopeIn(_Body):
    scope: Scope = "own"


class LinkIn(_Body):
    traveler_id: uuid.UUID
    trip_id: uuid.UUID | None = None


class DraftIn(_Body):
    conversation_id: uuid.UUID | None = None
    trip_id: uuid.UUID | None = None
    subject: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=100_000)


class SendIn(DraftIn):
    message_id: uuid.UUID


class ReplyIn(_Body):
    scope: Scope = "own"
    locale: Literal["en", "vi"] = "en"
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=20_000)


class ReplyChange(_Body):
    locale: Literal["en", "vi"] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=80)
    body: str | None = Field(default=None, min_length=1, max_length=20_000)


def _runtime(request: Request) -> MailRuntime:
    runtime: MailRuntime | None = getattr(request.app.state, "mail_runtime", None)
    if runtime is None:
        raise Unavailable("mail_not_configured")
    return runtime


def _connect_action(scope: str) -> Action:
    return Action.INTEGRATIONS_MANAGE if scope == "agency" else Action.OWN_MAILBOX_CONNECT


# ── mailbox ──────────────────────────────────────────────────────────────────────────────────────


@router.get("/mailbox")
async def get_mailbox(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.OWN_MAILBOX_CONNECT) as ctx:
        return await mailbox.view(ctx, Vault(keys, ctx), request.app.state.settings)


@router.post("/mailbox/oauth/start")
async def post_oauth_start(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: OAuthStart
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=_connect_action(body.scope)) as ctx:
        return await oauth.start(ctx, Vault(keys, ctx), request.app.state.settings, body.provider, body.scope)


@router.post("/mailbox/oauth/complete")
async def post_oauth_complete(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: OAuthComplete
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    runtime = _runtime(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.OWN_MAILBOX_CONNECT) as ctx:
        vault = Vault(keys, ctx)
        done = await oauth.complete(ctx, vault, request.app.state.settings, runtime.providers, body.code, body.state)
        # The scope sealed in the state decides the permission (an agent's state can only say "own").
        require(ctx, _connect_action(done.scope), object_kind="channel_account")
        return await mailbox.connect(ctx, vault, scope=done.scope, provider=done.provider, secret=done.secret)


@router.put("/mailbox/smtp")
async def put_smtp(request: Request, caller: CallerDep, tenant_id: TenantDep, body: SmtpIn) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    runtime = _runtime(request)
    action = _connect_action(body.scope)
    # Authorise first: membership of this agency and the permission for the scope. The SMTP check below dials a host
    # the caller names, so it must never run for someone who may not connect this mailbox (no port / login oracle).
    async with tenant_tx(pool, caller, tenant_id, action=action):
        pass
    secret = body.model_dump(exclude={"scope"}) | {"address": body.address.strip().lower()}
    try:
        # The login is proved before anything is stored, outside any transaction (it may take TIMEOUT_S).
        await runtime.providers["smtp"].check(secret)
    except ProviderError as exc:
        raise Invalid(f"smtp_{exc.code}" if not exc.code.startswith("smtp_") else exc.code) from None
    async with tenant_tx(pool, caller, tenant_id, action=action) as ctx:
        return await mailbox.connect(ctx, Vault(keys, ctx), scope=body.scope, provider="smtp", secret=secret)


@router.post("/mailbox/test")
async def post_mailbox_test(request: Request, caller: CallerDep, tenant_id: TenantDep, body: ScopeIn) -> dict[str, Any]:
    pool = await request_pool(request)
    runtime = _runtime(request)
    to_address = None
    directory = request.app.state.directory
    if directory is not None and caller.iss == directory.issuer:
        to_address = await directory.email_for(caller.sub)
    async with tenant_tx(pool, caller, tenant_id, action=_connect_action(body.scope)) as ctx:
        account = await mailbox.mailbox_id(ctx, body.scope)
        return await health.test_mailbox(ctx, runtime, account, to_address)


@router.delete("/mailbox")
async def delete_mailbox(
    request: Request, caller: CallerDep, tenant_id: TenantDep, scope: Annotated[Scope, Query()] = "own"
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=_connect_action(scope)) as ctx:
        return await mailbox.disconnect(ctx, scope)


@router.get("/mailbox/ingest-address")
async def get_ingest_address(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.OWN_MAILBOX_CONNECT) as ctx:
        return await mailbox.ingest_address(ctx, Vault(keys, ctx), request.app.state.settings)


@router.get("/mail/senders")
async def get_senders(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SESSION_VIEW):
        return senders.table()


# ── conversations and messages ───────────────────────────────────────────────────────────────────


@router.get("/conversations")
async def get_conversations(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    trip_id: uuid.UUID | None = None,
    traveler_id: uuid.UUID | None = None,
    unmatched: bool = False,
    no_trip: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await conversations.list_conversations(
            ctx, trip_id=trip_id, traveler_id=traveler_id, unmatched=unmatched, no_trip=no_trip, limit=limit
        )


@router.get("/conversations/{conversation_id}/messages")
async def get_messages(
    request: Request, caller: CallerDep, tenant_id: TenantDep, conversation_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await conversations.conversation_messages(ctx, Vault(keys, ctx), conversation_id)


@router.post("/conversations/{conversation_id}/link")
async def post_link(
    request: Request, caller: CallerDep, tenant_id: TenantDep, conversation_id: uuid.UUID, body: LinkIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_EDIT) as ctx:
        return await conversations.link(ctx, conversation_id, body.traveler_id, body.trip_id)


@router.put("/messages/drafts/{message_id}")
async def put_draft(
    request: Request, caller: CallerDep, tenant_id: TenantDep, message_id: uuid.UUID, body: DraftIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await conversations.save_draft(
            ctx,
            Vault(keys, ctx),
            message_id,
            conversation_id=body.conversation_id,
            trip_id=body.trip_id,
            subject=body.subject,
            body=body.body,
        )


@router.delete("/messages/drafts/{message_id}")
async def delete_draft(
    request: Request, caller: CallerDep, tenant_id: TenantDep, message_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await conversations.discard_draft(ctx, message_id)


@router.post("/messages", status_code=202)
async def post_message(request: Request, caller: CallerDep, tenant_id: TenantDep, body: SendIn) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MESSAGE_SEND) as ctx:
        return await conversations.send(
            ctx,
            Vault(keys, ctx),
            body.message_id,
            conversation_id=body.conversation_id,
            trip_id=body.trip_id,
            subject=body.subject,
            body=body.body,
        )


@router.post("/messages/{message_id}/undo")
async def post_undo(request: Request, caller: CallerDep, tenant_id: TenantDep, message_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MESSAGE_SEND) as ctx:
        return await conversations.undo(ctx, message_id)


@router.post("/messages/{message_id}/edit")
async def post_edit(request: Request, caller: CallerDep, tenant_id: TenantDep, message_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await conversations.back_to_draft(ctx, message_id)


# ── saved replies ────────────────────────────────────────────────────────────────────────────────


@router.get("/saved-replies")
async def get_replies(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await conversations.saved_replies(ctx, Vault(keys, ctx))


@router.post("/saved-replies", status_code=201)
async def post_reply(request: Request, caller: CallerDep, tenant_id: TenantDep, body: ReplyIn) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await conversations.create_reply(
            ctx, Vault(keys, ctx), scope=body.scope, locale=body.locale, title=body.title, body=body.body
        )


@router.patch("/saved-replies/{reply_id}")
async def patch_reply(
    request: Request, caller: CallerDep, tenant_id: TenantDep, reply_id: uuid.UUID, body: ReplyChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await conversations.update_reply(
            ctx, Vault(keys, ctx), reply_id, locale=body.locale, title=body.title, body=body.body
        )


@router.delete("/saved-replies/{reply_id}")
async def delete_reply(
    request: Request, caller: CallerDep, tenant_id: TenantDep, reply_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await conversations.delete_reply(ctx, reply_id)


# ── history import ───────────────────────────────────────────────────────────────────────────────


async def _read_capped(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLarge("import_too_large", f"at most {limit // (1024 * 1024)} MB")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLarge("import_too_large", f"at most {limit // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/mail/imports", status_code=202)
async def post_import(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    source_kind: Annotated[Literal["mbox", "csv"], Query()],
) -> dict[str, Any]:
    store = require_ingest_store(getattr(request.app.state, "ingest_store", None))
    data = await _read_capped(request, history_import.MAX_BYTES)
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.OWN_MAILBOX_CONNECT) as ctx:
        return await history_import.create_import(ctx, store, source_kind=source_kind, data=data)


@router.get("/mail/imports/{import_id}")
async def get_import(request: Request, caller: CallerDep, tenant_id: TenantDep, import_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.OWN_MAILBOX_CONNECT) as ctx:
        return await history_import.import_status(ctx, import_id)
