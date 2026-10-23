"""Liveness and readiness (same contract as AA-Booking, root ADR 0004 decision 9).

- ``GET /crm/health``: process liveness, no DB call. The ALB target group checks this one, so a DB outage cannot trip
  the ECS circuit breaker into a rollback loop.
- ``GET /crm/health/ready``: DB reachable and every required ``crm-NNN`` migration registered. Used by the deploy smoke
  test and alarms. The body also says which key provider field encryption uses (``keys``: ``kms`` / ``local`` /
  ``not_configured``, AA-934); it never fails readiness, so a missing ``KMS_DATA_KEY_ID`` shows without a 503.

Both are public (no token), so both carry the app-level rate limit (``app.ratelimit``, ADR 0010 decision 2).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.ratelimit import PublicRateLimit
from app.settings import SERVICE_NAME

router = APIRouter(prefix="/crm", tags=["health"])


@router.get("/health")
async def health(request: Request, _: PublicRateLimit) -> dict[str, str]:
    settings = request.app.state.settings
    return {"status": "ok", "service": SERVICE_NAME, "version": settings.app_version, "git_sha": settings.git_sha}


@router.get("/health/ready")
async def ready(request: Request, _: PublicRateLimit) -> JSONResponse:
    result = await request.app.state.db.readiness()
    body = {**result.body, "keys": request.app.state.settings.keys_state}
    return JSONResponse(status_code=200 if result.ok else 503, content=body)
