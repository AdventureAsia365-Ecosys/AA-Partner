"""App-level rate limit on the unauthenticated routes (ADR 0010 decision 2).

CloudFront -> VPC origin -> ALB skips the API Gateway throttling CIS and Booking get, and WAF arrives only with the
first real agency, so the API limits its public routes itself: a fixed one-minute window per client key, in process
memory (one small task on Dev; each task counts on its own, which is fine for a ceiling).

The client address, in order:

1. ``CloudFront-Viewer-Address`` (``<ip>:<port>``; IPv6 without brackets): CloudFront sets it from the TCP connection
   and a viewer cannot set it (AA-931 ``crm_edge_spike.tf`` forwards it with the managed origin request policy
   ``AllViewerAndCloudFrontHeaders``). Only CloudFront reaches the app: the ALB rule ``crm_api`` also requires the
   CloudFront origin-verify header. The port is stripped.
2. Fallback when that header is absent or not an address: the ``X-Forwarded-For`` entry ``RATE_LIMIT_XFF_HOPS`` from
   the right (entries further left are whatever the viewer sent and are never trusted).
3. Without either (local runs, tests): the socket peer.

The key is the IPv4 address, or the **/64** of an IPv6 address: one IPv6 subscriber usually holds a whole /64 and could
otherwise rotate addresses inside it to get a fresh count per request.

At most :data:`MAX_TRACKED_KEYS` keys are tracked per window. When the table is full, the least recently seen key is
evicted (an idle one), instead of resetting every count, so a flood of new keys cannot clear the counts of the
addresses that are busy right now.
"""

from __future__ import annotations

import ipaddress
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request

WINDOW_S = 60.0
MAX_TRACKED_KEYS = 10_000
IPV6_PREFIX = 64
VIEWER_ADDRESS_HEADER = "cloudfront-viewer-address"


class FixedWindowLimiter:
    def __init__(
        self,
        limit_per_window: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        self._limit = limit_per_window
        self._clock = clock
        self._max_keys = max_keys
        self._window_start = clock()
        self._counts: OrderedDict[str, int] = OrderedDict()

    def allow(self, key: str) -> bool:
        if self._limit <= 0:
            return True
        now = self._clock()
        if now - self._window_start >= WINDOW_S:
            self._window_start = now
            self._counts.clear()
        count = self._counts.pop(key, 0) + 1
        self._counts[key] = count  # re-inserted last: the most recently seen key
        while len(self._counts) > self._max_keys:
            self._counts.popitem(last=False)  # evict the least recently seen key only
        return count <= self._limit

    def tracked(self) -> int:
        return len(self._counts)


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text.strip().strip("[]"))
    except ValueError:
        return None


def _viewer_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``203.0.113.7:46532`` or ``2001:db8::1:46532`` (CloudFront appends ``:<port>``, IPv6 without brackets)."""
    host, sep, port = value.strip().rpartition(":")
    if not sep or not port.isdigit():
        return None
    return _ip(host)


def address_key(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """The limiter key: the IPv4 address, or the /64 network of an IPv6 address (an IPv4-mapped one counts as IPv4)."""
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{addr}/{IPV6_PREFIX}", strict=False))
    return str(addr)


def client_address(request: Request, hops: int) -> str:
    """The limiter key of the caller (see the module docstring for the order of sources)."""
    viewer = _viewer_address(request.headers.get(VIEWER_ADDRESS_HEADER, ""))
    if viewer is not None:
        return address_key(viewer)
    forwarded = request.headers.get("x-forwarded-for", "")
    entries = [e.strip() for e in forwarded.split(",") if e.strip()]
    if entries:
        chosen = entries[-hops] if len(entries) >= hops else entries[0]
        addr = _ip(chosen)
        return address_key(addr) if addr is not None else chosen
    peer = request.client.host if request.client else ""
    addr = _ip(peer)
    return address_key(addr) if addr is not None else (peer or "unknown")


def public_rate_limit(request: Request) -> None:
    """Dependency for the public routes: 429 once the caller's address has used its minute."""
    limiter: FixedWindowLimiter = request.app.state.public_limiter
    hops: int = request.app.state.settings.rate_limit_xff_hops
    if not limiter.allow(client_address(request, hops)):
        raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": str(int(WINDOW_S))})


PublicRateLimit = Annotated[None, Depends(public_rate_limit)]
