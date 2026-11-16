"""``/crm/v1`` global search (route class AGENCY; card CRM-32, design v3 §2.3 A-00, R35).

======================  ==================================================================  ==========
Route                   What                                                                Who
======================  ==================================================================  ==========
``GET  /search/config`` whether search is on for the agency, the kinds the member may see   every role
                        and the platform bounds (no record is read)
``POST /search``        one search; the term in the body (never in a URL or a log)          every role
======================  ==================================================================  ==========

Off by default per agency (setting ``crm.search.enabled``): ``POST /search`` answers 403 ``search_not_enabled``. A
partial e-mail or phone answers 422 ``partial_contact_search``, a wildcard 422 ``partial_search_refused``
(:mod:`app.search.query`). What each role may find: :mod:`app.search.kinds`.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.permissions import Action
from app.routers.agency import tenant_header
from app.search import service
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["search"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]

# The body's hard bound; the platform setting crm.search.query_max_chars (at most this) is the policy.
BODY_MAX_CHARS = 200  # technical-constant: request size bound, the setting's own max_value


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=BODY_MAX_CHARS)


@router.get("/search/config")
async def get_search_config(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SESSION_VIEW) as ctx:
        return await service.config(ctx)


@router.post("/search")
async def post_search(request: Request, caller: CallerDep, tenant_id: TenantDep, body: SearchIn) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SESSION_VIEW) as ctx:
        return await service.search(ctx, Vault(keys, ctx), body.q)
