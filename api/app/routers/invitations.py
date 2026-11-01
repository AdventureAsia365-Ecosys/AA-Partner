"""Invitation claim (no token) and accept (partner-pool bearer) — AA-939, root ADR 0006b decision 5.

- ``POST /crm/public/v1/invitations/claim`` ``{"token": ...}``: the person has no account yet, so no bearer. Rate
  limited per address before anything is read (``RATE_LIMIT_CLAIM_PER_MINUTE``). 200 ``{"state": "created",
  "email", "temporary_password"}`` (shown once by the join page), ``{"state": "existing", "email"}`` (sign in with
  the account you have) or ``{"state": "already_claimed", "email"}``; 404 ``invitation_not_found``; 410
  ``invitation_expired`` / ``invitation_used``. ``Cache-Control: no-store``. (An account that never finished its first
  sign-in gets a new temporary password, ``created``; 409 ``account_setup_pending`` is no longer answered, S263.)
- ``POST /crm/v1/invitations/accept`` ``{"token": ...}``: AGENCY class, no ``X-AA-Tenant-Id`` (the token decides the
  agency). The caller's token must carry the ``(iss, sub)`` the claim bound; 200 ``{"tenant_id", "member_id",
  "member_role"}``.

AA-979 P2b: the same two routes serve a Content studio invitation (``crm.studio_invitation``, app.members.studio). The
token is looked up as a CRM invitation first and, when no CRM invitation has it (404 ``invitation_not_found``), as a
studio invitation: one join page and one link format for both. A studio accept answers ``{"kind": "studio",
"tenant_id": <the CIS tenant>, "status": "accepted" | "activated"}``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.errors import NotFound
from app.members import invitations, studio
from app.members.directory import require_directory
from app.ratelimit import client_address
from app.vault.provider import require_keys

public_router = APIRouter(prefix="/crm/public/v1", tags=["public"])
agency_router = class_router(AGENCY, tags=["invitations"])

NO_STORE = {"Cache-Control": "no-store"}


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=64)


@public_router.post("/invitations/claim")
async def post_claim(request: Request, body: TokenIn) -> JSONResponse:
    settings = request.app.state.settings
    limiter = request.app.state.claim_limiter
    if not limiter.allow(client_address(request, settings.rate_limit_xff_hops)):
        raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": str(int(limiter.window_s))})
    directory = require_directory(request.app.state.directory)
    keys = require_keys(request.app.state.keys)
    pool = await request_pool(request)
    try:
        claimed = await invitations.claim(pool, keys, directory, body.token)
    except NotFound as exc:
        if exc.code != "invitation_not_found":
            raise
        claimed = await studio.claim(pool, directory, body.token)  # AA-979: a Content studio invitation
    return JSONResponse(claimed.body(), headers=NO_STORE)


@agency_router.post("/invitations/accept")
async def post_accept(
    request: Request, caller: Annotated[Caller, Depends(current_caller)], body: TokenIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    try:
        return await invitations.accept(pool, caller, body.token)
    except NotFound as exc:
        if exc.code != "invitation_not_found":
            raise
    # AA-979: not a CRM invitation -> a Content studio invitation (or the same 404).
    return await studio.accept(pool, caller, body.token, request.app.state.studio_activation)
