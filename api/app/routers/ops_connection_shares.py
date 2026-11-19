"""``/crm/ops/v1/llm/connection-shares`` (AA-1143, tech lead decision G3, crm.ops.v1 1.16, crm-079): the CRM's copy of
its monthly share of each LLM connection's budget. CIS holds the budget rows (the connection's total, one share per
app, CIS taking the remainder; AI gateway > Budgets) and sends the CRM its part with the staff member's own WORKFORCE
token, the way it sends the CRM app profile (``PUT /llm/app-profile``, 1.10).

- ``GET``: any full OPS reader (cis-admin or crm-platform-admin): the copy, so the CIS page shows whether it is in step.
- ``PUT``: ``crm-platform-admin`` (the Dev-only E2E CIS admin is read-only, 403 ``e2e_read_only``): the whole copy,
  every CIS connection that has a total with the CRM's share or 0, and the reason of the CIS save (20..500 chars). A
  connection the body does not name is dropped from the copy (the gateway then falls back to the setting
  ``crm.ai.connection_monthly_cap_usd`` for one release).

422 ``invalid_connection_shares`` on a bad body; 503 ``connection_shares_unavailable`` before crm-079. Service:
:mod:`app.llm.connection_share`. The gateway reads the copy on every capped call (:mod:`app.llm.connection_cap`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import HTTPException, Request
from pydantic import Field

from app.auth.identity_config import OPS
from app.auth.route_guard import class_router
from app.db import request_pool
from app.llm import connection_share
from app.routers.ops import FullReader, PlatformAdmin, _In, _Out

router = class_router(OPS, tags=["ops"])

UNAVAILABLE = "connection_shares_unavailable"
INVALID = "invalid_connection_shares"


class ConnectionShareItem(_In):
    connection_key: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")]
    share_usd: Annotated[str, Field(pattern=r"^[0-9]{1,7}(\.[0-9]{1,2})?$")]


class ConnectionSharesIn(_In):
    shares: Annotated[list[ConnectionShareItem], Field(max_length=200)]
    reason: Annotated[str, Field(min_length=connection_share.REASON_MIN, max_length=connection_share.REASON_MAX)]


class ConnectionShareOut(_Out):
    connection_key: str
    share_usd: str


class ConnectionSharesOut(_Out):
    shares: list[ConnectionShareOut]
    updated_at: str | None


@router.get("/llm/connection-shares", response_model=ConnectionSharesOut)
async def get_connection_shares(request: Request, caller: FullReader) -> dict:
    pool = await request_pool(request)
    try:
        return await connection_share.read_copy(pool)
    except connection_share.CopyUnavailable:
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from None


@router.put("/llm/connection-shares", response_model=ConnectionSharesOut)
async def put_connection_shares(request: Request, body: ConnectionSharesIn, caller: PlatformAdmin) -> dict:
    pool = await request_pool(request)
    try:
        shares = connection_share.check_copy([item.model_dump() for item in body.shares])
        return await connection_share.write_copy(pool, shares, reason=body.reason, actor=f"platform:{caller.sub}")
    except ValueError:
        raise HTTPException(status_code=422, detail=INVALID) from None
    except connection_share.CopyUnavailable:
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from None
