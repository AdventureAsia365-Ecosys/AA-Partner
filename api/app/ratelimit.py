"""App-level rate limit on the unauthenticated routes (ADR 0010 decision 2).

CloudFront -> VPC origin -> ALB skips the API Gateway throttling CIS and Booking get, and WAF arrives only with the
first real agency, so the API limits its public routes itself: a fixed one-minute window per client address, in
process memory (one small task on Dev; each task counts on its own, which is fine for a ceiling).

The client address is the ``X-Forwarded-For`` entry ``RATE_LIMIT_XFF_HOPS`` from the right: CloudFront appends the
viewer address, then the ALB appends the address it received from, so entries further left are whatever the viewer
sent and are never trusted. Without the header (local runs, tests) the socket peer is used.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request

WINDOW_S = 60.0
MAX_TRACKED_KEYS = 10_000


class FixedWindowLimiter:
    def __init__(self, limit_per_window: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit_per_window
        self._clock = clock
        self._window_start = clock()
        self._counts: dict[str, int] = {}

    def allow(self, key: str) -> bool:
        if self._limit <= 0:
            return True
        now = self._clock()
        if now - self._window_start >= WINDOW_S or len(self._counts) >= MAX_TRACKED_KEYS:
            self._window_start = now
            self._counts = {}
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        return count <= self._limit


def client_address(request: Request, hops: int) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    entries = [e.strip() for e in forwarded.split(",") if e.strip()]
    if len(entries) >= hops:
        return entries[-hops]
    if entries:
        return entries[0]
    return request.client.host if request.client else "unknown"


def public_rate_limit(request: Request) -> None:
    """Dependency for the public routes: 429 once the caller's address has used its minute."""
    limiter: FixedWindowLimiter = request.app.state.public_limiter
    hops: int = request.app.state.settings.rate_limit_xff_hops
    if not limiter.allow(client_address(request, hops)):
        raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": str(int(WINDOW_S))})


PublicRateLimit = Annotated[None, Depends(public_rate_limit)]
