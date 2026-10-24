"""``/crm/ops/v1``: the L1 platform console contract ``crm.ops.v1`` (route class OPS; root ``docs/contracts``).

WORKFORCE ``cis-admin`` tokens only (the CIS admin BFF forwards the staff token); group ``cis-admin`` reads,
``crm-platform-admin`` writes (``require_group``). AA-936 adds the two reads CIS needs to see the CRM (card CRM-08
"CIS AppInfo("crm") + jobs tab", D12):

- ``GET /crm/ops/v1/jobs/summary``: ``crm.job`` per kind (queued, running, failed in 24 h, oldest queued age);
- ``GET /crm/ops/v1/llm/usage?from=&to=``: the LLM usage rollup per day x stage x model (no text, no agency split).

Counts only: no agency id, no trip, no personal data. The agency operations arrive with CRM-13a (AA-941).

AA-939 (card CRM-11, Q1 6.3) adds the owner-recovery operations the L1 console calls, ids and codes only, each written
into that agency's own audit (actor ``platform:<staff sub>``, kind ``aa_ops``):

- ``GET  /crm/ops/v1/tenants/{tenant_id}/members``: ids, roles, statuses, active owner count, pending recovery
  (group ``cis-admin`` or ``crm-platform-admin``);
- ``POST /crm/ops/v1/tenants/{tenant_id}/members/{member_id}/deactivate``: disable a member, the last owner included
  (group ``crm-platform-admin``);
- ``POST /crm/ops/v1/tenants/{tenant_id}/owner-recovery`` ``{candidate_member_id, reason}``: open a recovery, 14 days
  (group ``crm-platform-admin``);
- ``POST /crm/ops/v1/tenants/{tenant_id}/owner-recovery/{request_id}/complete``: not before the 14 days (409
  ``recovery_not_due``) (group ``crm-platform-admin``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from app.auth.identity_config import GROUP_CRM_PLATFORM_ADMIN, OPS
from app.auth.route_guard import Caller, class_router, current_caller, require_group
from app.db import request_pool
from app.jobs import queue
from app.llm.usage import MAX_DAYS, llm_usage
from app.members import recovery

router = class_router(OPS, tags=["ops"])


@router.get("/jobs/summary")
async def jobs_summary(request: Request) -> dict[str, Any]:
    pool = await request_pool(request)
    return {"generated_at": datetime.now(UTC).isoformat(), "kinds": await queue.kind_summary(pool)}


@router.get("/llm/usage")
async def get_llm_usage(
    request: Request,
    from_: Annotated[date, Query(alias="from")],
    to: Annotated[date, Query()],
) -> dict[str, Any]:
    if to < from_ or (to - from_).days > MAX_DAYS:
        raise HTTPException(status_code=422, detail="invalid_range")
    pool = await request_pool(request)
    return {"from": from_.isoformat(), "to": to.isoformat(), "rows": await llm_usage(pool, from_, to)}


PlatformAdmin = Annotated[Caller, Depends(require_group(GROUP_CRM_PLATFORM_ADMIN))]


class RecoveryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_member_id: uuid.UUID
    reason: Literal["owner_left", "owner_unreachable", "no_owner"]


@router.get("/tenants/{tenant_id}/members")
async def tenant_members(
    request: Request, tenant_id: uuid.UUID, caller: Annotated[Caller, Depends(current_caller)]
) -> dict[str, Any]:
    pool = await request_pool(request)
    return await recovery.members(pool, tenant_id, staff_sub=caller.sub)


@router.post("/tenants/{tenant_id}/members/{member_id}/deactivate")
async def deactivate_member(
    request: Request, tenant_id: uuid.UUID, member_id: uuid.UUID, caller: PlatformAdmin
) -> dict[str, Any]:
    pool = await request_pool(request)
    return await recovery.deactivate_member(
        pool, tenant_id, member_id, staff_sub=caller.sub, directory=request.app.state.directory
    )


@router.post("/tenants/{tenant_id}/owner-recovery", status_code=201)
async def open_recovery(
    request: Request, tenant_id: uuid.UUID, body: RecoveryIn, caller: PlatformAdmin
) -> dict[str, Any]:
    pool = await request_pool(request)
    settings = request.app.state.settings
    return await recovery.open_request(
        pool,
        tenant_id,
        candidate_member_id=body.candidate_member_id,
        reason=body.reason,
        staff_sub=caller.sub,
        directory=request.app.state.directory,
        mailer=request.app.state.mailer,
        web_origin=settings.invitation_origin,
    )


@router.post("/tenants/{tenant_id}/owner-recovery/{request_id}/complete")
async def complete_recovery(
    request: Request, tenant_id: uuid.UUID, request_id: uuid.UUID, caller: PlatformAdmin
) -> dict[str, Any]:
    pool = await request_pool(request)
    return await recovery.complete_request(pool, tenant_id, request_id, staff_sub=caller.sub)
