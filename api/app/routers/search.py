"""``/crm/v1`` global search (route class AGENCY; card CRM-32, design v3 §2.3 A-00, R35).

======================  ==================================================================  ==========
Route                   What                                                                Who
======================  ==================================================================  ==========
``GET  /search/config`` whether search is on for the agency, the kinds the member may see   every role
                        and the platform bounds (no record is read)
``POST /search``        one search; the term in the body (never in a URL or a log)          every role
======================  ==================================================================  ==========

Off by default per agency (setting ``crm.search.enabled``): ``POST /search`` answers 403 ``search_not_enabled``. A
partial e-mail or phone answers 422 ``partial_contact_search``, a term that folds to too few letters 422
``query_too_short`` (:mod:`app.search.query`). What each role may find: :mod:`app.search.kinds`.

The body is parsed here, not by FastAPI (AA-963 review): FastAPI's default 422 echoes the submitted value (``input``),
which would put the term, or the value of an unexpected key, in the answer. Every refusal of the body is an app error
with a code and no value: ``query_too_long`` (the term is longer than the body bound), ``invalid_query`` (anything
else: not JSON, not an object, ``q`` missing or not text, another key).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import platform_settings as ps
from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.errors import Invalid
from app.permissions import Action
from app.routers.agency import tenant_header
from app.search import service
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["search"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]

# The body's hard bound on the raw term, well above the setting's own max_value (200): the policy is the platform
# setting crm.search.query_max_chars, checked by app.search.query.classify on the collapsed term (white space runs
# count once there, so a raw term with long space runs is still judged by the setting, not by this bound).
BODY_MAX_CHARS = 1000  # technical-constant: request size bound, not a policy value


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=BODY_MAX_CHARS)


def _search_body(raw: bytes) -> SearchIn:
    """The body as :class:`SearchIn`, refusing with a code only (never the term, never another key's value)."""
    try:
        return SearchIn.model_validate_json(raw)
    except ValidationError as exc:
        if any(e["type"] == "string_too_long" and tuple(e["loc"]) == ("q",) for e in exc.errors(include_input=False)):
            limit = int(ps.get(ps.SEARCH_QUERY_MAX_CHARS))
            raise Invalid("query_too_long", f"at most {limit} characters", field="q") from None
        raise Invalid("invalid_query", 'send one JSON object {"q": "<the term>"}', field="q") from None


@router.get("/search/config")
async def get_search_config(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SESSION_VIEW) as ctx:
        return await service.config(ctx)


@router.post(
    "/search",
    openapi_extra={
        "requestBody": {"required": True, "content": {"application/json": {"schema": SearchIn.model_json_schema()}}}
    },
)
async def post_search(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    body = _search_body(await request.body())
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SESSION_VIEW) as ctx:
        return await service.search(ctx, Vault(keys, ctx), body.q)
