"""``/crm/v1`` reports (route class AGENCY; card CRM-34, AA-966; design v3 A-14 Reports, A-01 Home tiles).

==============================================  ===============================================  =====================
Route                                           What                                             Who
==============================================  ===============================================  =====================
``GET  /reports/summary?period=&owner=``        the whole Reports page                           every role (money:
                                                                                                 owner / agent)
``GET  /reports/tiles``                         the Home tiles (last 12 months)                  every role
``GET  /reports/export?section=&period=&owner=`` one section as CSV (audited ``report_export``)   every role; money
                                                                                                 sections owner /
                                                                                                 agent
==============================================  ===============================================  =====================

``period``: ``30d`` / ``90d`` / ``12m`` (default) / ``ytd``, in the agency's time zone. ``owner``: ``me`` or a member
id (the trips that member owns); absent = everyone. Read-only: nothing here writes except the export's audit event.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request, Response

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.permissions import Action
from app.reports import export, service
from app.reports.period import DEFAULT_PERIOD
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx

router = class_router(AGENCY, tags=["reports"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Period = Annotated[Literal["30d", "90d", "12m", "ytd"], Query()]
Owner = Annotated[
    str | None,
    Query(pattern=r"^(me|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"),
]
Section = Annotated[
    Literal["funnel", "lead_sources", "outcomes", "open_trips", "money_by_month", "top_products", "outstanding",
            "team", "success"],
    Query(),
]  # fmt: skip


@router.get("/reports/summary")
async def get_summary(
    request: Request, caller: CallerDep, tenant_id: TenantDep, period: Period = DEFAULT_PERIOD, owner: Owner = None
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.REPORTS_VIEW) as ctx:
        return await service.summary(ctx, period, owner)


@router.get("/reports/tiles")
async def get_tiles(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.REPORTS_VIEW) as ctx:
        return await service.home_tiles(ctx)


@router.get("/reports/export")
async def get_export(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    section: Section,
    period: Period = DEFAULT_PERIOD,
    owner: Owner = None,
) -> Response:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.REPORTS_VIEW) as ctx:
        body, count = await export.export(ctx, period, owner, section)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="report-{section.replace("_", "-")}-{period}-{stamp}.csv"',
            "X-Row-Count": str(count),
            "Cache-Control": "no-store",
        },
    )
