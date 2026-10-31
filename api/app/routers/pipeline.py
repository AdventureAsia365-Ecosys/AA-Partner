"""``/crm/v1`` pipeline, trips, tasks, members and intake forms (route class AGENCY; card CRM-08, A-02 / A-03 / A-18).

Every route opens :func:`app.tenancy.tenant_tx` for the agency in ``X-AA-Tenant-Id`` (membership check, RLS) and does
its work through :mod:`app.pipeline` / :mod:`app.intake.forms`. Personal data is decrypted only through
:class:`app.vault.Vault`, and the audit events are queued on the context (``ctx.audit``):

=========================================  ==================================================  ==================
Route                                      What                                                Who
=========================================  ==================================================  ==================
``GET  /pipeline``                         board: open stages, cards, counts (one list_view)   every role
``GET  /trips/{trip_id}``                  trip workspace (one view event)                     every role
``PATCH /trips/{trip_id}``                 waiting on, title, start / end date (AA-937)        every role
``POST /trips/{trip_id}/stage``            move along an edge, gates with an override reason   every role
``POST /trips/{trip_id}/stage/undo``       take back the latest own move within 10 s           every role
``POST /trips/{trip_id}/cancel``           cancel (dry run first: the effect list)             owner, agent
``POST /pipeline/bulk-stage``              preview / run a move of up to 200 trips             every role
``POST /pipeline/bulk-stage/undo``         take back a bulk move within 10 s                   every role
``GET  /trips/{trip_id}/stage2``           Traveller info: completeness, planning form link    every role
``POST /trips/{trip_id}/stage2/send``      send the planning form (signed link, Undo 10 s)     owner, agent;
                                                                                               assistant: own trip
``PUT  /trips/{trip_id}/stage2``           fill the planning form in from a call               every role
``GET  /settings/pipeline``                timezone, Stage 2 auto-send, call-slot rules        owner
``PUT  /settings/pipeline``                change them                                         owner
``POST /trips/{trip_id}/owner``            assign                                              owner (reassign);
                                                                                               others take
``POST /trips/{trip_id}/notes``            internal note (encrypted)                           every role
``POST /tasks/{task_id}/status``           open / done / skipped                               every role
``GET  /members``                          ids and roles (for the assign menu)                 every role
``GET  /intake-forms``                     the agency's forms + public keys                    every role
``POST /intake-forms``                     create a form                                       owner
``PATCH /intake-forms/{form_id}``          label, status, origins, default owner, privacy url  owner
=========================================  ==================================================  ==================

The traveller routes (directory, profile, reveal with a reason, party, documents) are in :mod:`app.routers.travellers`
(AA-937). AA-939: each route names its permission-matrix action (``app.permissions``) to ``tenant_tx()``, and every
response passes the margin serializer (``app.margin``).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app import stages
from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.errors import NotFound
from app.intake import completeness, stage2
from app.intake import forms as intake_forms
from app.intake import settings as pipeline_settings
from app.ops_release import service as ops_release
from app.permissions import Action, require
from app.pipeline import (
    BoardFilter,
    add_note,
    assign,
    board,
    members,
    move_stage,
    set_task_status,
    trip_detail,
    trip_owner,
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
    override_reason: str | None = Field(default=None, max_length=40)
    undo: bool = False


class StageUndo(_Body):
    history_id: uuid.UUID


class CancelIn(_Body):
    cancel_reason_code: str | None = Field(default=None, max_length=40)


class BulkStage(_Body):
    to: str = Field(max_length=20)
    trip_ids: list[uuid.UUID] | None = Field(default=None, max_length=stages.MAX_BULK)
    from_stage: str | None = Field(default=None, max_length=20)
    lost_reason: str | None = Field(default=None, max_length=40)
    override_reason: str | None = Field(default=None, max_length=40)
    dry_run: bool = True
    preview_hash: str | None = Field(default=None, max_length=64)


class BulkUndo(_Body):
    batch_id: uuid.UUID


class PipelineSettings(_Body):
    timezone: str | None = Field(default=None, max_length=64)
    stage2_auto_send: bool | None = None
    call_slot_rules: dict[str, Any] | None = None


class OwnerChange(_Body):
    member_id: uuid.UUID | None = None


class TripChange(_Body):
    waiting_on: str | None = Field(default=None, max_length=20)
    title: str | None = Field(default=None, max_length=200)
    start_date: dt.date | None = None
    end_date: dt.date | None = None


class NoteIn(_Body):
    body: str = Field(min_length=1, max_length=8000)


class TaskStatus(_Body):
    status: Literal["open", "done", "skipped"]


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
    # The matrix again (tenant_tx refused already): Settings > Forms is owner-only (design §4.6 "team, settings").
    require(ctx, Action.SETTINGS_FORMS_MANAGE, object_kind="intake_form")


def _stage_action(to: str) -> Action:
    """Cancelling a trip is owner / agent (design §4.6); every other move is a trip edit."""
    return Action.TRIP_CANCEL if to == "cancelled" else Action.TRIP_EDIT


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
    async with tenant_tx(pool, caller, tenant_id, action=Action.PIPELINE_VIEW) as ctx:
        return await board(ctx, Vault(keys, ctx), BoardFilter(waiting_on=waiting_on, owner=owner))


@router.get("/trips/{trip_id}")
async def get_trip(request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await trip_detail(ctx, Vault(keys, ctx), trip_id)


@router.patch("/trips/{trip_id}")
async def patch_trip(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: TripChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    changes = body.model_dump(exclude_unset=True)
    keys = require_keys(request.app.state.keys) if "start_date" in changes else None
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await update_trip(ctx, trip_id, changes, Vault(keys, ctx) if keys is not None else None)


@router.post("/trips/{trip_id}/stage")
async def post_stage(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: StageMove
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = request.app.state.keys
    async with tenant_tx(pool, caller, tenant_id, action=_stage_action(body.to)) as ctx:
        return await move_stage(
            ctx,
            trip_id,
            body.to,
            lost_reason=body.lost_reason,
            undo=body.undo,
            override_reason=body.override_reason,
            vault=Vault(keys, ctx) if keys is not None else None,
        )


@router.post("/trips/{trip_id}/stage/undo")
async def post_stage_undo(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: StageUndo
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await stages.undo(ctx, trip_id, body.history_id)


@router.post("/trips/{trip_id}/cancel")
async def post_cancel(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    trip_id: uuid.UUID,
    body: CancelIn,
    dry_run: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    pool = await request_pool(request)
    notices: ops_release.Notices = []
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_CANCEL) as ctx:
        out = await stages.cancel(ctx, trip_id, reason=body.cancel_reason_code, dry_run=dry_run, notices=notices)
    ops_release.emit(notices)  # committed: AA ops are told the release was withdrawn (C12 step 6)
    return out


@router.post("/pipeline/bulk-stage")
async def post_bulk_stage(request: Request, caller: CallerDep, tenant_id: TenantDep, body: BulkStage) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = request.app.state.keys
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await stages.bulk_move(
            ctx,
            to=body.to,
            trip_ids=body.trip_ids,
            from_stage=body.from_stage,
            lost_reason=body.lost_reason,
            override_reason=body.override_reason,
            dry_run=body.dry_run,
            expected_hash=body.preview_hash,
            vault=Vault(keys, ctx) if keys is not None else None,
        )


@router.post("/pipeline/bulk-stage/undo")
async def post_bulk_undo(request: Request, caller: CallerDep, tenant_id: TenantDep, body: BulkUndo) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await stages.bulk_undo(ctx, body.batch_id)


@router.get("/trips/{trip_id}/stage2")
async def get_stage2(request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        vault = Vault(keys, ctx)
        done = await completeness.compute(ctx, vault, trip_id)
        if done is None:
            raise NotFound("trip_not_found")
        lead, _token = await completeness.lead_token(ctx, vault, trip_id)
        return {
            **stage2.definition(),
            "completeness": done.view(),
            "answers": await stage2.prefill(ctx, vault, lead),
        }


@router.post("/trips/{trip_id}/stage2/send", status_code=201)
async def post_stage2_send(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        owner = await trip_owner(ctx, trip_id)
        require(ctx, Action.MESSAGE_SEND, object_kind="trip", object_id=str(trip_id), assigned=owner == ctx.member_id)
        sent = await stage2.send_link(ctx, Vault(keys, ctx), trip_id)
        await stages.refresh_waiting_on(ctx, trip_id)
        return sent


@router.put("/trips/{trip_id}/stage2")
async def put_stage2(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: dict[str, Any]
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await stage2.fill_from_call(ctx, Vault(keys, ctx), trip_id, body)


@router.get("/settings/pipeline")
async def get_pipeline_settings(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SETTINGS_FORMS_MANAGE) as ctx:
        return await pipeline_settings.read(ctx)


@router.put("/settings/pipeline")
async def put_pipeline_settings(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: PipelineSettings
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SETTINGS_FORMS_MANAGE) as ctx:
        _owner(ctx)
        return await pipeline_settings.update(ctx, body.model_dump(exclude_unset=True))


@router.post("/trips/{trip_id}/owner")
async def post_owner(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: OwnerChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_TAKE) as ctx:
        return await assign(ctx, trip_id, body.member_id)


@router.post("/trips/{trip_id}/notes", status_code=201)
async def post_note(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: NoteIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.NOTE_ADD) as ctx:
        return await add_note(ctx, Vault(keys, ctx), trip_id, body.body)


@router.post("/tasks/{task_id}/status")
async def post_task_status(
    request: Request, caller: CallerDep, tenant_id: TenantDep, task_id: uuid.UUID, body: TaskStatus
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TASK_UPDATE) as ctx:
        return await set_task_status(ctx, task_id, body.status)


@router.get("/members")
async def get_members(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MEMBERS_LIST) as ctx:
        return {"members": await members(ctx)}


@router.get("/intake-forms")
async def get_forms(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PIPELINE_VIEW) as ctx:
        return {"forms": [f.__dict__ for f in await intake_forms.list_forms(ctx)]}


@router.post("/intake-forms", status_code=201)
async def post_form(request: Request, caller: CallerDep, tenant_id: TenantDep, body: FormIn) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SETTINGS_FORMS_MANAGE) as ctx:
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
    async with tenant_tx(pool, caller, tenant_id, action=Action.SETTINGS_FORMS_MANAGE) as ctx:
        _owner(ctx)
        changes = body.model_dump(exclude_unset=True)
        form = await intake_forms.update_form(ctx, form_id, changes)
        ctx.audit("update", "allowed", "intake_form", form.intake_form_id, fields=tuple(sorted(changes)))
        return form.__dict__
