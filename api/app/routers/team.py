"""``/crm/v1`` Team & roles, plan and owner recovery (route class AGENCY; card CRM-11, A-18; design v3 §4.6).

Every route opens :func:`app.tenancy.tenant_tx` with its :class:`~app.permissions.Action` (the matrix refuses a role
before any work) and works through :mod:`app.members.team` / :mod:`app.members.recovery`:

=============================================  ==============================================  ===================
Route                                          What                                            Who (matrix)
=============================================  ==============================================  ===================
``GET    /team``                               members, pending invitations, the plan          owner (team_view)
``POST   /team/invitations``                   invite an address with a role (AA system mail)  owner (team_manage)
``DELETE /team/invitations/{id}``              revoke a pending invitation                     owner (team_manage)
``PATCH  /team/members/{id}``                  change the role (409 last_owner)                owner (team_manage)
``POST   /team/members/{id}/remove``           disable + sign out; trips to the reassign queue owner (team_manage)
``GET    /team/reassign-queue``                open trips / clients of inactive members        owner (reassign)
``POST   /team/reassign``                      move them to an active member                   owner (reassign)
``GET    /plan``                               plan, member limit, seats used, features        owner (plan_view)
``GET    /recovery``                           the pending owner recovery (banner)             every role
``POST   /recovery/{id}/cancel``               cancel it                                       every role
=============================================  ==============================================  ===================
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.errors import Unavailable
from app.members import recovery
from app.members import team as team_service
from app.members.mail import require_mailer
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["team"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Role = Literal["owner", "agent", "assistant"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InviteIn(_Body):
    email: str = Field(min_length=3, max_length=254)
    role: Role


class RoleChange(_Body):
    role: Role


class ReassignIn(_Body):
    from_member_id: uuid.UUID
    to_member_id: uuid.UUID
    trip_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)


@router.get("/team")
async def get_team(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TEAM_VIEW) as ctx:
        return await team_service.team(ctx, Vault(keys, ctx))


@router.post("/team/invitations", status_code=201)
async def post_invitation(request: Request, caller: CallerDep, tenant_id: TenantDep, body: InviteIn) -> dict[str, Any]:
    settings = request.app.state.settings
    origin = settings.invitation_origin
    if not origin:
        raise Unavailable("invitations_not_configured", "PARTNER_WEB_ORIGIN is not set")
    mailer = require_mailer(request.app.state.mailer)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TEAM_MANAGE) as ctx:
        return await team_service.invite(
            ctx,
            Vault(keys, ctx),
            mailer,
            email=body.email,
            role=body.role,
            web_origin=origin,
            ttl_hours=settings.invitation_ttl_hours,
        )


@router.delete("/team/invitations/{invitation_id}")
async def delete_invitation(
    request: Request, caller: CallerDep, tenant_id: TenantDep, invitation_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TEAM_MANAGE) as ctx:
        return await team_service.revoke_invitation(ctx, invitation_id)


@router.patch("/team/members/{member_id}")
async def patch_member(
    request: Request, caller: CallerDep, tenant_id: TenantDep, member_id: uuid.UUID, body: RoleChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TEAM_MANAGE) as ctx:
        return await team_service.change_role(ctx, member_id, body.role)


@router.post("/team/members/{member_id}/remove")
async def post_remove(
    request: Request, caller: CallerDep, tenant_id: TenantDep, member_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TEAM_MANAGE) as ctx:
        return await team_service.remove_member(ctx, request.app.state.directory, member_id)


@router.get("/team/reassign-queue")
async def get_reassign_queue(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.REASSIGN) as ctx:
        return await team_service.reassign_queue(ctx)


@router.post("/team/reassign")
async def post_reassign(request: Request, caller: CallerDep, tenant_id: TenantDep, body: ReassignIn) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.REASSIGN) as ctx:
        return await team_service.reassign(ctx, body.from_member_id, body.to_member_id, body.trip_ids)


@router.get("/plan")
async def get_plan(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PLAN_VIEW) as ctx:
        return await team_service.plan(ctx)


@router.get("/recovery")
async def get_recovery(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.RECOVERY_VIEW) as ctx:
        return await recovery.banner(ctx)


@router.post("/recovery/{request_id}/cancel")
async def post_recovery_cancel(
    request: Request, caller: CallerDep, tenant_id: TenantDep, request_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.RECOVERY_CANCEL) as ctx:
        return await recovery.cancel(ctx, request_id)
