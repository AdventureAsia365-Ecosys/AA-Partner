"""``/crm/v1`` proposals, payment schedule, portal access and agreements (route class AGENCY; card CRM-18, AA-946:
the trip's Proposal tab, Settings > Agreements).

==============================================  ===============================================  =====================
Route                                           What                                             Who
==============================================  ===============================================  =====================
``GET  /trips/{id}/lifecycle``                  proposals, schedule, payments, portal progress   every role
``POST /trips/{id}/proposals``                  draft a new version (text + sell amounts)        every role
``PATCH /proposals/{id}``                       edit a draft                                     every role
``POST /proposals/{id}/send``                   send (price hash, signed link once)              owner, agent
``POST /proposals/{id}/withdraw``               withdraw a draft or a sent version               owner, agent
``POST /proposals/{id}/reissue-link``           AA-950: revoke the client's link, send a new one owner, agent
``GET  /proposals/{id}/client-activity``        AA-950: viewed, hold, choices (R19), events      every role
``GET  /bell``                                  AA-950: my unseen events, bundled                every role
``POST /bell/seen``                             AA-950: mark them seen (all, or some trips')     every role
``POST /schedule/{id}/mark-paid``               record a payment received (deposit -> Booked)    owner, agent
``POST /schedule/{id}/void``                    void a due row                                   owner, agent
``POST /trips/{id}/portal-access``              invite one traveller (or re-issue their link)    owner, agent;
                                                                                                 assistant: own trip
``POST /portal-access/{id}/revoke``             end one traveller's portal access                owner, agent;
                                                                                                 assistant: own trip
``GET  /agreements``                            terms, waiver, declination; merge fields         every role
``POST /agreements``                            draft a version                                  owner
``PATCH /agreements/{id}``                      edit a draft                                     owner
``POST /agreements/{id}/publish``               publish (retires the published one of the kind)  owner
==============================================  ===============================================  =====================

Amounts are the client's sell amounts; nothing here is margin-bearing. The traveller side is
:mod:`app.routers.client_pages`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.permissions import Action, require
from app.pipeline import trip_owner
from app.portal import access
from app.proposals import agreements, events, service
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["proposals"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Amount = Annotated[Decimal, Field(gt=0, le=Decimal("999999999999.99"), decimal_places=2)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposalIn(_Body):
    body: str = Field(min_length=1, max_length=service.MAX_BODY)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    total_amount: Amount
    deposit_amount: Amount
    balance_due_on: dt.date | None = None
    terms_doc_id: uuid.UUID | None = None


class ProposalChange(_Body):
    body: str | None = Field(default=None, min_length=1, max_length=service.MAX_BODY)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    total_amount: Amount | None = None
    deposit_amount: Amount | None = None
    balance_due_on: dt.date | None = None
    terms_doc_id: uuid.UUID | None = None


class MarkPaidIn(_Body):
    method: Literal["bank_transfer", "cash", "card_offline", "other"]
    received_on: dt.date | None = None


class InviteIn(_Body):
    traveler_id: uuid.UUID


class AgreementIn(_Body):
    kind: Literal["booking_terms", "waiver", "insurance_declination"]
    version_label: str = Field(min_length=1, max_length=agreements.MAX_LABEL)
    body_text: str = Field(min_length=1, max_length=agreements.MAX_TEXT)


class AgreementChange(_Body):
    version_label: str | None = Field(default=None, min_length=1, max_length=agreements.MAX_LABEL)
    body_text: str | None = Field(default=None, min_length=1, max_length=agreements.MAX_TEXT)


@router.get("/trips/{trip_id}/lifecycle")
async def get_lifecycle(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.trip_overview(ctx, Vault(keys, ctx), trip_id)


@router.post("/trips/{trip_id}/proposals", status_code=201)
async def post_proposal(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: ProposalIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await service.create_draft(
            ctx,
            Vault(keys, ctx),
            trip_id,
            body=body.body,
            currency=body.currency,
            total=body.total_amount,
            deposit=body.deposit_amount,
            balance_due_on=body.balance_due_on,
            terms_doc_id=body.terms_doc_id,
        )


@router.patch("/proposals/{proposal_id}")
async def patch_proposal(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, body: ProposalChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await service.update_draft(ctx, Vault(keys, ctx), proposal_id, body.model_dump(exclude_unset=True))


@router.post("/proposals/{proposal_id}/send")
async def post_proposal_send(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_PUBLISH) as ctx:
        return await service.send(ctx, Vault(keys, ctx), proposal_id)


@router.post("/proposals/{proposal_id}/reissue-link")
async def post_proposal_reissue_link(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_PUBLISH) as ctx:
        return await service.reissue_link(ctx, proposal_id)


@router.get("/proposals/{proposal_id}/client-activity")
async def get_proposal_client_activity(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.client_activity(ctx, proposal_id)


@router.get("/bell")
async def get_bell(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await events.bell(ctx)


class BellSeenBody(_Body):
    trip_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)


@router.post("/bell/seen")
async def post_bell_seen(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: BellSeenBody
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await events.mark_seen(ctx, body.trip_ids)


@router.post("/proposals/{proposal_id}/withdraw")
async def post_proposal_withdraw(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_PUBLISH) as ctx:
        return await service.withdraw(ctx, proposal_id)


@router.post("/schedule/{schedule_id}/mark-paid")
async def post_mark_paid(
    request: Request, caller: CallerDep, tenant_id: TenantDep, schedule_id: uuid.UUID, body: MarkPaidIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        return await service.mark_paid(
            ctx, Vault(keys, ctx), schedule_id, received_on=body.received_on, method=body.method
        )


@router.post("/schedule/{schedule_id}/void")
async def post_schedule_void(
    request: Request, caller: CallerDep, tenant_id: TenantDep, schedule_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        return await service.void(ctx, schedule_id)


@router.post("/trips/{trip_id}/portal-access", status_code=201)
async def post_portal_access(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: InviteIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        owner = await trip_owner(ctx, trip_id)
        require(ctx, Action.MESSAGE_SEND, object_kind="trip", object_id=str(trip_id), assigned=owner == ctx.member_id)
        return await access.invite(ctx, trip_id, body.traveler_id)


@router.post("/portal-access/{access_id}/revoke")
async def post_portal_revoke(
    request: Request, caller: CallerDep, tenant_id: TenantDep, access_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        trip_id = await access.trip_of(ctx, access_id)
        owner = await trip_owner(ctx, trip_id)
        require(ctx, Action.MESSAGE_SEND, object_kind="trip", object_id=str(trip_id), assigned=owner == ctx.member_id)
        return await access.revoke(ctx, access_id)


@router.get("/agreements")
async def get_agreements(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await agreements.list_docs(ctx)


@router.post("/agreements", status_code=201)
async def post_agreement(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: AgreementIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TERMS_PUBLISH) as ctx:
        return await agreements.create(ctx, kind=body.kind, version_label=body.version_label, body_text=body.body_text)


@router.patch("/agreements/{doc_id}")
async def patch_agreement(
    request: Request, caller: CallerDep, tenant_id: TenantDep, doc_id: uuid.UUID, body: AgreementChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TERMS_PUBLISH) as ctx:
        return await agreements.update(ctx, doc_id, body.model_dump(exclude_unset=True, exclude_none=True))


@router.post("/agreements/{doc_id}/publish")
async def post_agreement_publish(
    request: Request, caller: CallerDep, tenant_id: TenantDep, doc_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TERMS_PUBLISH) as ctx:
        return await agreements.publish(ctx, doc_id)
