"""``/crm/v1`` access log (route class AGENCY; card CRM-12, A-18 Settings > Data & privacy).

==============================================  =================================================  ================
Route                                           What                                               Who
==============================================  =================================================  ================
``GET  /audit/events``                          the agency's audit, newest first, filtered, paged  owner
``GET  /audit/events/export?format=csv|jsonl``  the same rows as a file (at most 50,000)           owner
``POST /audit/sign-in``                         records that the caller opened this agency         every role
==============================================  =================================================  ================

A non-owner reading the log gets 403 ``role_forbidden`` from the permission matrix (``tenant_tx(action=)``, AA-939;
``audit_log.require_owner`` checks again with ``owner_only``) and leaves a ``denied`` event. The web calls
``sign-in`` from its ``/auth/agency`` route each time a session picks an agency (design §4.9 "sign-ins").
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request, Response

from app import audit_log
from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx

router = class_router(AGENCY, tags=["audit"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


def audit_filter(
    actor: Annotated[Literal["user", "ai", "aa_ops", "aa_support", "aa_emergency", "system"] | None, Query()] = None,
    action: Annotated[str | None, Query(max_length=40)] = None,
    outcome: Annotated[Literal["allowed", "denied"] | None, Query()] = None,
    member_id: Annotated[uuid.UUID | None, Query()] = None,
    object_kind: Annotated[str | None, Query(max_length=40)] = None,
    object_id: Annotated[str | None, Query(max_length=80)] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> audit_log.AuditFilter:
    return audit_log.AuditFilter(
        actor=actor,
        action=action,
        outcome=outcome,
        member_id=member_id,
        object_kind=object_kind,
        object_id=object_id,
        date_from=date_from,
        date_to=date_to,
    )


FilterDep = Annotated[audit_log.AuditFilter, Depends(audit_filter)]


@router.get("/audit/events")
async def list_audit_events(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    filters: FilterDep,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=audit_log.PAGE_MAX)] = 50,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.ACCESS_LOG_VIEW) as ctx:
        return await audit_log.list_events(ctx, filters, cursor=cursor, limit=limit)


@router.get("/audit/events/export")
async def export_audit_events(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    filters: FilterDep,
    fmt: Annotated[Literal["csv", "jsonl"], Query(alias="format")] = "csv",
) -> Response:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.ACCESS_LOG_VIEW) as ctx:
        body, count = await audit_log.export_events(ctx, filters, fmt)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    media = "text/csv; charset=utf-8" if fmt == "csv" else "application/x-ndjson"
    return Response(
        content=body,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="access-log-{stamp}.{fmt}"',
            "X-Row-Count": str(count),
            "Cache-Control": "no-store",
        },
    )


@router.post("/audit/sign-in")
async def record_sign_in(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SESSION_VIEW) as ctx:
        return audit_log.record_sign_in(ctx)
