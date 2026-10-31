"""``/crm/v1`` AI settings and drafts (route class AGENCY; AA-964, card CRM-33a: A-18 "AI", A-21 AI draft).

=============================================  =============================================  ======================
Route                                          What                                           Who
=============================================  =============================================  ======================
``GET    /settings/ai``                        AI on / off, monthly budget, this month's      every role
                                               spend and alert level, test mode
``PUT    /settings/ai``                        switch AI on / off, set the budget (<= plan)   owner
``POST   /trips/{id}/ai/draft``                one AI draft for the trip (behind ai_enabled;  every role
                                               a suggestion, never sent from here)
=============================================  =============================================  ======================

The service is :mod:`app.llm.agency_ai`; every model call goes through the process's
:class:`app.llm.gateway.LlmGateway` (``app.state.llm_gateway``).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.llm import agency_ai
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["ai"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AiSettingsIn(_Body):
    ai_enabled: bool | None = None
    ai_budget_usd: Decimal | None = Field(default=None, ge=0, le=10_000, decimal_places=2)


class DraftIn(_Body):
    kind: Literal["reply", "follow_up"] = "reply"
    text: str = Field(min_length=1, max_length=agency_ai.MAX_TEXT)
    instruction: str | None = Field(default=None, max_length=agency_ai.MAX_INSTRUCTION)


def _limits(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    return {
        "budget_max": settings.ai_budget_max_usd,
        "provider": settings.crm_llm_provider,
        "rate_per_minute": settings.rate_limit_ai_per_minute,
    }


@router.get("/settings/ai")
async def get_ai_settings(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_USE) as ctx:
        return await agency_ai.ai_settings(ctx, **_limits(request))


@router.put("/settings/ai")
async def put_ai_settings(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: AiSettingsIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_SETTINGS) as ctx:
        return await agency_ai.update_ai_settings(ctx, body.model_dump(exclude_unset=True), **_limits(request))


@router.post("/trips/{trip_id}/ai/draft")
async def post_ai_draft(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: DraftIn
) -> dict[str, Any]:
    keys = require_keys(request.app.state.keys)
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_USE) as ctx:
        return await agency_ai.draft(
            ctx,
            request.app.state.llm_gateway,
            Vault(keys, ctx),
            trip_id,
            kind=body.kind,
            text=body.text,
            instruction=body.instruction,
        )
