"""``/crm/v1``: the agency API (route class AGENCY, root ADR 0006b decision 6).

Every route here is behind the AGENCY guard (partner-pool web client; on Dev before AA-939 also the EXTERNAL
``cis-portal`` client). ``cis-admin`` and ``breakglass`` tokens get 403. The agency is chosen per request with the
``X-AA-Tenant-Id`` header and every read goes through :func:`app.tenancy.tenant_tx` (membership check, RLS context).

Responses are ids and roles only. Margin-bearing fields arrive with later cards behind the per-role serializer
allow-list (design v3 §4.6).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.tenancy import tenant_tx

router = class_router(AGENCY, tags=["agency"])


def tenant_header(x_aa_tenant_id: Annotated[str | None, Header()] = None) -> uuid.UUID:
    if not x_aa_tenant_id:
        raise HTTPException(status_code=400, detail="missing_tenant")
    try:
        return uuid.UUID(x_aa_tenant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_tenant") from None


@router.get("/me")
async def me(
    request: Request,
    caller: Annotated[Caller, Depends(current_caller)],
    tenant_id: Annotated[uuid.UUID, Depends(tenant_header)],
) -> dict[str, Any]:
    """Who the caller is in this agency: the membership ``tenant_tx()`` found (ids and role, no personal data)."""
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id) as ctx:
        return {"tenant_id": str(ctx.tenant_id), "member_id": str(ctx.member_id), "member_role": ctx.member_role}
