"""``/crm/v1`` tasks and automations (route class AGENCY; card CRM-29a, AA-959: A-09 Tasks, A-17 Automations, the trip's
Tasks & notes tab).

==============================================  ===============================================  =====================
Route                                           What                                             Who
==============================================  ===============================================  =====================
``GET  /tasks``                                 open tasks in buckets (``scope`` mine / team /   every role
                                                trip / client; ``member_id``; ``status=done``)
``POST /tasks``                                 a member's own task (title, trip or client,      every role
                                                due time or due rule, priority, assignee)
``GET  /tasks/{id}``                            one task (+ arrival flights for T-48 h)          every role
``PATCH /tasks/{id}``                           title, due time, assignee, priority              every role
``PUT  /tasks/digest``                          the morning digest on / off                      owner
``GET  /workflows``                             rules + AA library + editor options              every role
``POST /workflows``                             a new rule                                       owner
``POST /workflows/install``                     install a library template                       owner
``GET  /workflows/runs``                        runs of a trip / client / rule (step log)        every role
``POST /workflows/runs/{id}/stop``              stop one active run                              owner, agent
``GET  /workflows/{id}``                        one rule                                         every role
``PATCH /workflows/{id}``                       edit (``version`` must match)                    owner
``POST /workflows/{id}/status``                 active / paused / archived                       owner
``GET  /workflows/{id}/preview``                live trips it would apply to now                 every role
``POST /workflows/{id}/apply``                  apply to trips (dry run first)                   owner, agent
==============================================  ===============================================  =====================

``POST /tasks/{id}/status`` (open / done / skipped) stays in :mod:`app.routers.pipeline`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys
from app.workflows import service, tasks

router = class_router(AGENCY, tags=["workflows"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DueRule(_Body):
    anchor: Literal["departure", "return"]
    offset_days: int = Field(ge=-365, le=365)


class TaskIn(_Body):
    title: str = Field(min_length=1, max_length=200)
    trip_id: uuid.UUID | None = None
    traveler_id: uuid.UUID | None = None
    due_at: dt.datetime | None = None
    due_rule: DueRule | None = None
    priority: Literal["normal", "urgent"] = "normal"
    assignee_member_id: uuid.UUID | None = None


class TaskChange(_Body):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    due_at: dt.datetime | None = None
    assignee_member_id: uuid.UUID | None = None
    priority: Literal["normal", "urgent"] | None = None


class DigestIn(_Body):
    enabled: bool


class TriggerIn(_Body):
    kind: str = Field(max_length=20)
    value: str | None = Field(default=None, max_length=64)
    offset_days: int | None = None


class RuleIn(_Body):
    name: str = Field(max_length=200)
    trigger: TriggerIn
    conditions: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] = Field(max_length=20)


class RuleCreate(RuleIn):
    status: Literal["active", "paused"] = "active"


class RuleChange(RuleIn):
    version: int = Field(ge=1)


class InstallIn(_Body):
    template_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    name: str | None = Field(default=None, max_length=200)
    steps: list[dict[str, Any]] | None = Field(default=None, max_length=20)


class StatusIn(_Body):
    status: Literal["active", "paused", "archived"]


class ApplyIn(_Body):
    trip_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    dry_run: bool = True
    request_id: uuid.UUID | None = None


def _rule(body: RuleIn) -> dict[str, Any]:
    return {
        "name": body.name,
        "trigger": body.trigger.model_dump(),
        "conditions": body.conditions,
        "steps": body.steps,
    }


# ── Tasks ─────────────────────────────────────────────────────────────────────────────────────────


@router.get("/tasks")
async def get_tasks(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    scope: Annotated[str, Query(max_length=10)] = "mine",
    member_id: uuid.UUID | None = None,
    trip_id: uuid.UUID | None = None,
    traveler_id: uuid.UUID | None = None,
    status: Annotated[str, Query(max_length=10)] = "open",
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PIPELINE_VIEW) as ctx:
        return await tasks.listing(
            ctx, Vault(keys, ctx), scope=scope, member_id=member_id, trip_id=trip_id, traveler_id=traveler_id,
            status=status,
        )  # fmt: skip


@router.post("/tasks")
async def post_task(request: Request, caller: CallerDep, tenant_id: TenantDep, body: TaskIn) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TASK_UPDATE) as ctx:
        return await tasks.create(
            ctx, Vault(keys, ctx), title=body.title, trip_id=body.trip_id, traveler_id=body.traveler_id,
            due_at=body.due_at, due_rule=(body.due_rule.anchor, body.due_rule.offset_days) if body.due_rule else None,
            priority=body.priority, assignee_member_id=body.assignee_member_id,
        )  # fmt: skip


@router.put("/tasks/digest")
async def put_digest(request: Request, caller: CallerDep, tenant_id: TenantDep, body: DigestIn) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.WORKFLOW_EDIT) as ctx:
        return await tasks.set_digest(ctx, body.enabled)


@router.get("/tasks/{task_id}")
async def get_task(request: Request, caller: CallerDep, tenant_id: TenantDep, task_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await tasks.get(ctx, Vault(keys, ctx), task_id)


@router.patch("/tasks/{task_id}")
async def patch_task(
    request: Request, caller: CallerDep, tenant_id: TenantDep, task_id: uuid.UUID, body: TaskChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    sent = body.model_fields_set
    async with tenant_tx(pool, caller, tenant_id, action=Action.TASK_UPDATE) as ctx:
        return await tasks.update(
            ctx, Vault(keys, ctx), task_id, title=body.title, due_at=body.due_at, set_due="due_at" in sent,
            assignee_member_id=body.assignee_member_id, set_assignee="assignee_member_id" in sent,
            priority=body.priority,
        )  # fmt: skip


# ── Automations ───────────────────────────────────────────────────────────────────────────────────


@router.get("/workflows")
async def get_workflows(
    request: Request, caller: CallerDep, tenant_id: TenantDep, archived: bool = False
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PIPELINE_VIEW) as ctx:
        return await service.overview(ctx, include_archived=archived)


@router.post("/workflows")
async def post_workflow(request: Request, caller: CallerDep, tenant_id: TenantDep, body: RuleCreate) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.WORKFLOW_EDIT) as ctx:
        return await service.create(ctx, _rule(body), status=body.status)


@router.post("/workflows/install")
async def post_install(request: Request, caller: CallerDep, tenant_id: TenantDep, body: InstallIn) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.WORKFLOW_EDIT) as ctx:
        return await service.install(ctx, body.template_key, name=body.name, steps=body.steps)


@router.get("/workflows/runs")
async def get_runs(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    trip_id: uuid.UUID | None = None,
    traveler_id: uuid.UUID | None = None,
    workflow_rule_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.runs(ctx, trip_id=trip_id, traveler_id=traveler_id, rule_id=workflow_rule_id)


@router.post("/workflows/runs/{run_id}/stop")
async def post_stop(request: Request, caller: CallerDep, tenant_id: TenantDep, run_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.WORKFLOW_APPLY) as ctx:
        return await service.stop(ctx, run_id)


@router.get("/workflows/{rule_id}")
async def get_workflow(request: Request, caller: CallerDep, tenant_id: TenantDep, rule_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PIPELINE_VIEW) as ctx:
        return await service.get(ctx, rule_id)


@router.patch("/workflows/{rule_id}")
async def patch_workflow(
    request: Request, caller: CallerDep, tenant_id: TenantDep, rule_id: uuid.UUID, body: RuleChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.WORKFLOW_EDIT) as ctx:
        return await service.update(ctx, rule_id, _rule(body), version=body.version)


@router.post("/workflows/{rule_id}/status")
async def post_workflow_status(
    request: Request, caller: CallerDep, tenant_id: TenantDep, rule_id: uuid.UUID, body: StatusIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.WORKFLOW_EDIT) as ctx:
        return await service.set_status(ctx, rule_id, body.status)


@router.get("/workflows/{rule_id}/preview")
async def get_preview(request: Request, caller: CallerDep, tenant_id: TenantDep, rule_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PIPELINE_VIEW) as ctx:
        return await service.preview(ctx, rule_id)


@router.post("/workflows/{rule_id}/apply")
async def post_apply(
    request: Request, caller: CallerDep, tenant_id: TenantDep, rule_id: uuid.UUID, body: ApplyIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.WORKFLOW_APPLY) as ctx:
        return await service.apply(
            ctx, rule_id, body.trip_ids, dry_run=body.dry_run, request_id=body.request_id or uuid.uuid4()
        )
