"""``/crm/v1`` Module 4 text proposal, "Before you go" and the message template library (route class AGENCY; card
CRM-22, AA-951: A-05 text preview + send, the day 0 section of the builder, R9 templates).

==================================================  ===========================================  ======================
Route                                               What                                         Who
==================================================  ===========================================  ======================
``GET    /proposals/{id}/text``                     e-mail / WhatsApp text, EN / VI, optional    every role
                                                    template; the version's ``price_hash``
``POST   /proposals/{id}/text/send``                send it: 409 ``price_hash_mismatch`` when    owner, agent (e-mail:
                                                    the hash is not the version's (card R)       MESSAGE_SEND too)
``GET    /proposals/{id}/before-you-go``            day 0: picked packs, suggestions from the    every role
                                                    AA items, reminders
``PUT    /proposals/{id}/before-you-go``            pick the packs of a builder draft (``seq``)  every role
``GET    /message-templates``                       the library + AA starters not copied yet     every role
``POST   /message-templates``                       create                                       owner
``PATCH  /message-templates/{id}``                  edit                                         owner
``DELETE /message-templates/{id}``                  delete                                       owner
``POST   /message-templates/import-aa``             copy CIS starter templates (idempotent)      owner
==================================================  ===========================================  ======================

No response carries a net amount, a margin, a ciphertext or a secret.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.mail import templates
from app.permissions import Action
from app.proposals import builder, prep_packs, text_proposal
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["text-proposal"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Format = Literal["email", "whatsapp"]
Locale = Literal["en", "vi"]
GroupKey = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextSendIn(_Body):
    format: Format
    locale: Locale | None = None
    price_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    template_id: uuid.UUID | None = None
    intro: str | None = Field(default=None, max_length=templates.MAX_BODY)
    message_id: uuid.UUID | None = None


class PacksIn(_Body):
    seq: int = Field(ge=1, le=2_000_000_000)
    group_keys: list[GroupKey] = Field(max_length=prep_packs.MAX_PACKS)


class TemplateIn(_Body):
    locale: Locale = "en"
    title: str = Field(min_length=1, max_length=templates.MAX_TITLE)
    body: str = Field(min_length=1, max_length=templates.MAX_BODY)


class TemplateChange(_Body):
    locale: Locale | None = None
    title: str | None = Field(default=None, min_length=1, max_length=templates.MAX_TITLE)
    body: str | None = Field(default=None, min_length=1, max_length=templates.MAX_BODY)


def _client_origin(request: Request) -> str | None:
    """The client host's origin for the proposal link in the text: the first INTAKE_PAGE_ORIGINS entry."""
    settings = getattr(request.app.state, "settings", None)
    origins = getattr(settings, "intake_page_origins", ()) or ()
    return origins[0] if origins else None


@router.get("/proposals/{proposal_id}/text")
async def get_text(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    proposal_id: uuid.UUID,
    format: Annotated[Format, Query()] = "email",  # noqa: A002 — the API's own name
    locale: Annotated[Locale | None, Query()] = None,
    template_id: Annotated[uuid.UUID | None, Query()] = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await text_proposal.preview(
            ctx, Vault(keys, ctx), proposal_id, fmt=format, locale=locale, template_id=template_id
        )


@router.post("/proposals/{proposal_id}/text/send")
async def post_text_send(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, body: TextSendIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_PUBLISH) as ctx:
        return await text_proposal.send(
            ctx,
            Vault(keys, ctx),
            proposal_id,
            fmt=body.format,
            locale=body.locale,
            price_hash=body.price_hash,
            template_id=body.template_id,
            intro=body.intro,
            message_id=body.message_id,
            client_origin=_client_origin(request),
        )


@router.get("/proposals/{proposal_id}/before-you-go")
async def get_before_you_go(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await prep_packs.for_proposal(ctx, proposal_id)


@router.put("/proposals/{proposal_id}/before-you-go")
async def put_before_you_go(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, body: PacksIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.set_before_you_go(ctx, Vault(keys, ctx), proposal_id, body.seq, body.group_keys)


@router.get("/message-templates")
async def get_templates(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await templates.library(ctx, Vault(keys, ctx))


@router.post("/message-templates", status_code=201)
async def post_template(request: Request, caller: CallerDep, tenant_id: TenantDep, body: TemplateIn) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.WORKFLOW_EDIT) as ctx:
        return await templates.create(ctx, Vault(keys, ctx), locale=body.locale, title=body.title, body=body.body)


@router.post("/message-templates/import-aa")
async def post_import_aa(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.WORKFLOW_EDIT) as ctx:
        return await templates.import_starters(ctx, Vault(keys, ctx))


@router.patch("/message-templates/{template_id}")
async def patch_template(
    request: Request, caller: CallerDep, tenant_id: TenantDep, template_id: uuid.UUID, body: TemplateChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.WORKFLOW_EDIT) as ctx:
        return await templates.update(
            ctx, Vault(keys, ctx), template_id, locale=body.locale, title=body.title, body=body.body
        )


@router.delete("/message-templates/{template_id}")
async def delete_template(
    request: Request, caller: CallerDep, tenant_id: TenantDep, template_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.WORKFLOW_EDIT) as ctx:
        return await templates.delete(ctx, template_id)
