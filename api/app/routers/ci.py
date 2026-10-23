"""Machine endpoint for the Deploy Dev workflow (ported from AA-Booking ``/booking/ops/deploy-gate``, AA-751 B3).

- ``GET /crm/ci/deploy-gate``: what a deploy would interrupt right now, for the ``preflight`` job of
  ``.github/workflows/deploy-dev.yml``. Authenticated **only** by that workflow's GitHub OIDC token (``app.auth.ci``):
  401 for any other token, 503 when GitHub's JWKS cannot be loaded. Returns
  ``{active, by_status, by_kind, safe_to_deploy, exempt_kinds}``: ``queued`` + ``running`` rows of ``crm.job``, kinds
  in :data:`app.jobs.kinds.DEPLOY_GATE_EXEMPT` not counted.

Not under ``/crm/ops/v1``: that prefix is the ``crm.ops.v1`` contract for the CIS admin (WORKFORCE tokens only).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.auth.ci import DeployCaller, deploy_caller
from app.db import request_pool
from app.jobs import queue
from app.jobs.kinds import DEPLOY_GATE_EXEMPT
from app.ratelimit import PublicRateLimit

router = APIRouter(prefix="/crm/ci", tags=["ci"])

Caller = Annotated[DeployCaller, Depends(deploy_caller)]


@router.get("/deploy-gate")
async def deploy_gate(request: Request, _limit: PublicRateLimit, _: Caller) -> dict[str, Any]:
    # The pool is taken after the token is verified, so an unauthenticated call never touches the database.
    pool = await request_pool(request)
    exempt = sorted(DEPLOY_GATE_EXEMPT)
    counts = await queue.active_count(pool, exempt)
    return {**counts, "safe_to_deploy": counts["active"] == 0, "exempt_kinds": exempt}
