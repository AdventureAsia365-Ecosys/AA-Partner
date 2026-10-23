"""``/crm/v1`` pipeline, trips, tasks, members and intake forms (route class AGENCY; card CRM-08, A-02 / A-03 / A-18).

Every route opens :func:`app.tenancy.tenant_tx` for the agency in ``X-AA-Tenant-Id`` (membership check, RLS) and does
its work through :mod:`app.pipeline` / :mod:`app.intake.forms`. Personal data is decrypted only through
:class:`app.vault.Vault`, and the audit events are queued on the context (``ctx.audit``):

=========================================  ==================================================  ==================
Route                                      What                                                Who
=========================================  ==================================================  ==================
``GET  /pipeline``                         board: open stages, cards, counts (one list_view)   every role
``GET  /trips/{trip_id}``                  trip workspace (one view event)                     every role
``PATCH /trips/{trip_id}``                 waiting on, title                                   every role
``POST /trips/{trip_id}/stage``            move (inquiry <-> qualifying, -> lost, reopen)      every role
``POST /trips/{trip_id}/owner``            assign                                              owner; others take
``POST /trips/{trip_id}/notes``            internal note (encrypted)                           every role
``POST /tasks/{task_id}/status``           open / done / skipped                               every role
``POST /travelers/{traveler_id}/reveal``   e-mail or phone, one field (one reveal event)       every role
``GET  /members``                          ids and roles (for the assign menu)                 every role
``GET  /intake-forms``                     the agency's forms + public keys                    every role
``POST /intake-forms``                     create a form                                       owner
``PATCH /intake-forms/{form_id}``          label, status, origins, default owner, privacy url  owner
=========================================  ==================================================  ==================

No margin, price or commission field exists yet (CRM-11 adds the per-role serializer allow-list, design §4.6).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.errors import Forbidden
from app.intake import forms as intake_forms
from app.pipeline import (
    BoardFilter,
    add_note,
    assign,
    board,
    members,
    move_stage,
    reveal_contact,
    set_task_status,
    trip_detail,
    update_trip,
)
from app.routers.agency import tenant_header
from app.tenancy import TenantContext, tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["pipeline"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StageMove(_Body):
    to: Literal[
        "inquiry", "qualifying", "proposal", "booked", "pre_departure", "travelling", "completed", "lost", "cancelled"
    ]
    lost_reason: str | None = Field(default=None, max_length=40)
    undo: bool = False


class OwnerChange(_Body):
    member_id: uuid.UUID | None = None


class TripChange(_Body):
    waiting_on: str | None = Field(default=None, max_length=20)
    title: str | None = Field(default=None, max_length=200)


class NoteIn(_Body):
    body: str = Field(min_length=1, max_length=8000)


class TaskStatus(_Body):
    status: Literal["open", "done", "skipped"]


class Reveal(_Body):
    field: Literal["email", "phone"]


class FormIn(_Body):
    label: str = Field(min_length=1, max_length=80)
    allowed_origins: list[str] = Field(default_factory=list, max_length=10)
    default_owner_member_id: uuid.UUID | None = None
    privacy_notice_url: str | None = Field(default=None, max_length=500)


class FormChange(_Body):
    label: str | None = Field(default=None, max_length=80)
    status: Literal["active", "disabled"] | None = None
    allowed_origins: list[str] | None = Field(default=None, max_length=10)
    default_owner_member_id: uuid.UUID | None = None
    privacy_notice_url: str | None = Field(default=None, max_length=500)


def _owner(ctx: TenantContext) -> None:
    if ctx.member_role != "owner":
        raise Forbidden("owner_only", "only the agency owner changes forms")


@router.get("/pipeline")
async def get_pipeline(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    waiting_on: Annotated[Literal["agency", "client", "aa", "supplier", "none"] | None, Query()] = None,
    owner: Annotated[Literal["anyone", "me"], Query()] = "anyone",
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        return await board(ctx, Vault(keys, ctx), BoardFilter(waiting_on=waiting_on, owner=owner))


@router.get("/trips/{trip_id}")
async def get_trip(request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        return await trip_detail(ctx, Vault(keys, ctx), trip_id)


@router.patch("/trips/{trip_id}")
async def patch_trip(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: TripChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        return await update_trip(ctx, trip_id, body.model_dump(exclude_unset=True))


@router.post("/trips/{trip_id}/stage")
async def post_stage(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: StageMove
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        return await move_stage(ctx, trip_id, body.to, lost_reason=body.lost_reason, undo=body.undo)


@router.post("/trips/{trip_id}/owner")
async def post_owner(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: OwnerChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        return await assign(ctx, trip_id, body.member_id)


@router.post("/trips/{trip_id}/notes", status_code=201)
async def post_note(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: NoteIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        return await add_note(ctx, Vault(keys, ctx), trip_id, body.body)


@router.post("/tasks/{task_id}/status")
async def post_task_status(
    request: Request, caller: CallerDep, tenant_id: TenantDep, task_id: uuid.UUID, body: TaskStatus
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        return await set_task_status(ctx, task_id, body.status)


@router.post("/travelers/{traveler_id}/reveal")
async def post_reveal(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID, body: Reveal
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        return await reveal_contact(ctx, Vault(keys, ctx), traveler_id, body.field)


@router.get("/members")
async def get_members(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        return {"members": await members(ctx)}


@router.get("/intake-forms")
async def get_forms(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        return {"forms": [f.__dict__ for f in await intake_forms.list_forms(ctx)]}


@router.post("/intake-forms", status_code=201)
async def post_form(request: Request, caller: CallerDep, tenant_id: TenantDep, body: FormIn) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        _owner(ctx)
        form = await intake_forms.create_form(
            ctx,
            label=body.label,
            allowed_origins=body.allowed_origins,
            default_owner_member_id=body.default_owner_member_id,
            privacy_notice_url=body.privacy_notice_url,
        )
        ctx.audit("create", "allowed", "intake_form", form.intake_form_id)
        return form.__dict__


@router.patch("/intake-forms/{form_id}")
async def patch_form(
    request: Request, caller: CallerDep, tenant_id: TenantDep, form_id: uuid.UUID, body: FormChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        _owner(ctx)
        changes = body.model_dump(exclude_unset=True)
        form = await intake_forms.update_form(ctx, form_id, changes)
        ctx.audit("update", "allowed", "intake_form", form.intake_form_id, fields=tuple(sorted(changes)))
        return form.__dict__
