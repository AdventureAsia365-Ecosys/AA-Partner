"""``/crm/v1``: the agency API (route class AGENCY, root ADR 0006b decision 6).

Every route here is behind the AGENCY guard (partner-pool web client only; the Dev EXTERNAL ``cis-portal`` bridge
was removed at AA-979 P2b, its tokens get 401). ``cis-admin`` and ``breakglass`` tokens get 403. The agency is chosen
per request with the ``X-AA-Tenant-Id`` header and every read goes through :func:`app.tenancy.tenant_tx` (membership
check, RLS context).

Responses are ids, roles and the agency's own label/locale only (never an e-mail or a name from the token).
AA-1036: ``/me`` also says whether the agency holds the CRM add-on (``crm_entitled``,
:func:`app.entitlements.crm_entitled`, from the projection of tenant.entitlements.v1), for the agency app's app
switcher; it refuses nothing. AA-1036 (R3): ``ai_paused`` is true while AA has switched the CRM's AI off for the whole
app (``crm.llm_app_profile.ai_enabled`` false, :func:`app.llm.app_profile.for_call`: no row, a missing table or an
unreadable row = AI on), so the agency app shows one banner instead of a failure per draft. The staff reason
(``disabled_reason``) is never sent. AA-1030: ``permissions`` carries the matrix answers the web hides links by
(``aa_tariff_view``: the AA prices page); the API still refuses on its own.
Margin-bearing fields arrive with later cards behind the per-role serializer allow-list (design v3 §4.6).

``GET /crm/v1/memberships`` is the one route without ``X-AA-Tenant-Id``: it lists the agencies the caller belongs to
(:func:`app.tenancy.caller_memberships`, crm-004 lookup policies) so the web can pick the agency to open (AA-935).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request

from app import entitlements
from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.llm import app_profile
from app.permissions import Action, may
from app.tenancy import caller_memberships, tenant_tx

router = class_router(AGENCY, tags=["agency"])

TENANT_LABEL_SQL = "SELECT display_name, default_locale FROM crm.tenant WHERE tenant_id = $1"


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
    async with tenant_tx(pool, caller, tenant_id, action=Action.SESSION_VIEW) as ctx:
        label = await ctx.conn.fetchrow(TENANT_LABEL_SQL, ctx.tenant_id)
        return {
            "tenant_id": str(ctx.tenant_id),
            "member_id": str(ctx.member_id),
            "member_role": ctx.member_role,
            "display_name": label["display_name"] if label else None,
            "default_locale": label["default_locale"] if label else None,
            "crm_entitled": await entitlements.crm_entitled(ctx),
            "ai_paused": not (await app_profile.for_call(ctx.conn)).ai_enabled,
            # AA-1030: what the web shows or hides by the permission matrix (one source for both sides).
            "permissions": {"aa_tariff_view": may(ctx.member_role, Action.AA_TARIFF_VIEW)},
        }


@router.get("/memberships")
async def memberships(request: Request, caller: Annotated[Caller, Depends(current_caller)]) -> dict[str, Any]:
    """The agencies the caller is an active member of (locked ones included, flagged), for the agency picker."""
    pool = await request_pool(request)
    found = await caller_memberships(pool, caller)
    return {
        "memberships": [
            {
                "tenant_id": str(m.tenant_id),
                "member_id": str(m.member_id),
                "member_role": m.member_role,
                "display_name": m.display_name,
                "default_locale": m.default_locale,
                "tenant_status": m.tenant_status,
            }
            for m in found
        ]
    }
