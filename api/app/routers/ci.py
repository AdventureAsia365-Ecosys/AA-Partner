"""Machine endpoint for the Deploy Dev workflow (ported from AA-Booking ``/booking/ops/deploy-gate``, AA-751 B3).

- ``GET /crm/ci/deploy-gate``: may a deploy roll the API task now? Read by the ``preflight`` job of
  ``.github/workflows/deploy-dev.yml``. Authenticated **only** by that workflow's GitHub OIDC token (``app.auth.ci``):
  401 for any other token, 503 when GitHub's JWKS cannot be loaded. Since AA-993 G5 (design D1.4)
  ``safe_to_deploy`` is false only while a row of :data:`app.jobs.kinds.DEPLOY_BLOCKING_KINDS` (``crm_dispatch``)
  is ``running`` (``blocking_running`` per kind, reasons in ``blocked_by``). Queued / running rows of every other kind
  no longer block (the worker releases a running job on shutdown, the new task re-claims it); they stay in
  ``active`` / ``by_status`` / ``by_kind`` as information, :data:`app.jobs.kinds.DEPLOY_GATE_EXEMPT` not counted there.
  The CRM does not read CIS rerun waves.

Not under ``/crm/ops/v1``: that prefix is the ``crm.ops.v1`` contract for the CIS admin (WORKFORCE tokens only).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.auth.ci import DeployCaller, deploy_caller
from app.db import request_pool
from app.jobs import queue
from app.jobs.kinds import DEPLOY_BLOCKING_KINDS, DEPLOY_GATE_EXEMPT
from app.ratelimit import PublicRateLimit

router = APIRouter(prefix="/crm/ci", tags=["ci"])

Caller = Annotated[DeployCaller, Depends(deploy_caller)]


@router.get("/deploy-gate")
async def deploy_gate(request: Request, _limit: PublicRateLimit, _: Caller) -> dict[str, Any]:
    # The pool is taken after the token is verified, so an unauthenticated call never touches the database.
    pool = await request_pool(request)
    exempt = sorted(DEPLOY_GATE_EXEMPT)
    blocking = sorted(DEPLOY_BLOCKING_KINDS)
    counts = await queue.active_count(pool, exempt)
    running = await queue.running_count(pool, blocking)
    blocked_by = [f"{n} {kind} job(s) running (deploy-blocking kind)" for kind, n in running.items()]
    return {
        **counts,
        "exempt_kinds": exempt,
        "blocking_kinds": blocking,
        "blocking_running": running,
        "blocked_by": blocked_by,
        "safe_to_deploy": not blocked_by,
    }
