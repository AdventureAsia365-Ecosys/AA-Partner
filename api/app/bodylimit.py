"""Request body ceiling for every route, enforced before any route code runs (AA-969, CRM-37a security review F1).

FastAPI reads and parses a route's JSON body **before** it runs the route's dependencies, the class guard of
``/crm/v1``, ``/crm/ops/v1`` and ``/crm/staff/v1`` included (``fastapi.routing.get_request_handler``). Without a
ceiling an anonymous caller could post a body of any size to any write route and the task would buffer it whole
before answering 401: one request could exhaust the memory of the task every agency shares. CloudFront and the ALB set
no body limit of their own.

This pure ASGI middleware (no buffering, no extra task, like :mod:`app.observability.request_id`) gives each request
a byte ceiling from its path:

- a declared ``Content-Length`` above the ceiling is answered **413** ``body_too_large`` at once, nothing is read;
- a body without a length (chunked) or with a wrong one is counted while the app reads it, and the read that passes
  the ceiling raises an ``HTTPException(413, "body_too_large")``, which FastAPI re-raises as it is (the route never
  sees a partial body).

Review round 2: a ceiling per request does not bound how many large bodies are buffered **at once**, and it does not
depend on authentication. So the middleware also keeps a **process-wide budget** of body bytes in flight
(:data:`IN_FLIGHT_BUDGET_BYTES`). A body is charged once it passes :data:`SMALL_BODY_BYTES` (64 KiB; the usual JSON
body never is), chunk by chunk as the bytes really arrive (a client that only declares a length reserves nothing),
and the charge is returned when the request's response is done. The read that would take the budget past its size
raises an ``HTTPException(503, "server_busy")`` with ``Retry-After``: large uploads wait, the small requests of every
agency go on, and the task stays far from its memory limit (a buffered body costs about three times its size while
it is joined and parsed). The upload route ``POST /crm/v1/documents`` also reads its body only after the class guard
(:mod:`app.routers.travellers`), so an anonymous caller never gets its 9.4 MiB ceiling.

Review round 3: one shared budget with no deadline let anyone with no token fill it and keep it full (about 33
connections each sending a 1 MiB body to any ``/crm/v1`` write route, then one byte every 90 s: FastAPI reads a
declared body before the class guard, uvicorn has no body-read timeout and the ALB idle timer resets on every byte),
so every agency's upload got 503 for as long as the trickle lasted. Now:

- **No pre-auth bytes on the agency budget.** On the class routes (``/crm/v1``, ``/crm/ops/v1``, ``/crm/staff/v1``) a
  body is read past :data:`SMALL_BODY_BYTES` only once the bearer token verifies (``app.state.verifier``, the same
  check the class guard makes next; it decides no access, only who is charged): no token is **401**
  ``missing_bearer_token``, a bad one **401** ``invalid_token``, before the 65th KiB is buffered. Every other route
  (the anonymous ``/crm/public/v1``: the passport scan) draws on a separate, smaller **anonymous** budget
  (:data:`ANONYMOUS_BUDGET_BYTES`).
- **A cap per owner.** Each budget also caps what one owner holds at once: one verified identity (``iss`` + ``sub``)
  at most :data:`VERIFIED_OWNER_BYTES` (the largest upload, a mail import), one client address (the
  :func:`app.ratelimit.client_address` key: IPv4, or the IPv6 /64) at most :data:`ANONYMOUS_OWNER_BYTES` (one
  passport scan).
- **A minimum body rate.** Once a body passes :data:`SMALL_BODY_BYTES`, each read must arrive by
  ``start + BODY_GRACE_S + received / MIN_BODY_RATE`` (20 s of grace, then 16 KiB/s on average, far below a phone
  upload); a slower body is cut with **408** ``body_too_slow`` and its charge is returned at once. A trickle can keep a
  charge for at most ``BODY_GRACE_S + ceiling / MIN_BODY_RATE``, and must keep sending to do even that.

The ceiling is :data:`DEFAULT_MAX_BYTES` (1 MiB: the largest JSON body of a normal route is a 100 000-character
message) except for the few upload routes in :data:`LARGE_BODY_ROUTES`, each a little above the route's own cap, so
the route keeps answering its own code (``import_too_large``, ``body_too_large``) for a body just over that cap.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth.identity_config import ROUTE_CLASSES
from app.auth.identity_verifier import AuthUnavailable, InvalidToken
from app.documents.service import MAX_BYTES as DOCUMENT_MAX_BYTES
from app.imports.formats import MAX_BYTES as CLIENT_IMPORT_MAX_BYTES
from app.mail.history_import import MAX_BYTES as MAIL_IMPORT_MAX_BYTES

MIB = 1024 * 1024
DEFAULT_MAX_BYTES = 1 * MIB
# Room for the JSON (or multipart-free raw upload) around the route's own byte cap.
SLACK_BYTES = 64 * 1024
# A document travels as base64 (4/3 of the bytes) inside a small JSON object (app.routers.travellers.DocumentIn).
DOCUMENT_BODY_BYTES = (DOCUMENT_MAX_BYTES * 4) // 3 + SLACK_BYTES
# app.routers.client_pages.SCAN_BODY_BYTES (10 MiB): a passport scan, the same base64 shape as a document.
PASSPORT_SCAN_BODY_BYTES = 10 * MIB + SLACK_BYTES

# (path, exact match?) -> ceiling. Checked in order; the first match wins.
LARGE_BODY_ROUTES: tuple[tuple[str, bool, int], ...] = (
    ("/crm/v1/documents", True, DOCUMENT_BODY_BYTES),
    ("/crm/v1/client-imports", False, CLIENT_IMPORT_MAX_BYTES + SLACK_BYTES),
    ("/crm/v1/mail/imports", True, MAIL_IMPORT_MAX_BYTES + SLACK_BYTES),
    ("/crm/public/v1/traveller/passport-scan", True, PASSPORT_SCAN_BODY_BYTES),
)
TOO_LARGE = "body_too_large"
TOO_BUSY = "server_busy"
# Bodies up to this size are never charged to the budget (a normal JSON body; bounded per request by the ceiling).
SMALL_BODY_BYTES = 64 * 1024
# Body bytes buffered at once by this process above SMALL_BODY_BYTES each. ~3x in memory while joined and parsed:
# about 100 MB at the peak, well inside the 512 MB task, and still room for three document uploads at once.
IN_FLIGHT_BUDGET_BYTES = 32 * MIB
RETRY_AFTER_BUSY_S = 2
# Round 3: the anonymous routes' own budget, and the most one owner may hold at once in each budget.
ANONYMOUS_BUDGET_BYTES = 12 * MIB
VERIFIED_OWNER_BYTES = MAIL_IMPORT_MAX_BYTES + SLACK_BYTES
ANONYMOUS_OWNER_BYTES = PASSPORT_SCAN_BODY_BYTES
# Round 3: a body past SMALL_BODY_BYTES must keep this average rate after the grace, else 408 body_too_slow.
BODY_GRACE_S = 20.0
MIN_BODY_RATE = 16 * 1024
TOO_SLOW = "body_too_slow"
# The class routes: their large bodies are charged to the caller's verified identity, never read before it.
CLASS_PREFIXES: tuple[str, ...] = tuple(rc.prefix for rc in ROUTE_CLASSES)
_CONTENT_LENGTH = b"content-length"
_AUTHORIZATION = b"authorization"


def max_body_bytes(path: str) -> int:
    """The byte ceiling of a request to ``path``."""
    for route, exact, limit in LARGE_BODY_ROUTES:
        if path == route or (not exact and path.startswith(route + "/")):
            return limit
    return DEFAULT_MAX_BYTES


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers") or ():
        if name == _CONTENT_LENGTH:
            text = value.decode("latin-1").strip()
            return int(text) if text.isdigit() else None
    return None


def is_class_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in CLASS_PREFIXES)


def _bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or ():
        if name == _AUTHORIZATION:
            scheme, _, token = value.decode("latin-1").strip().partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                return None
            return token.strip()
    return None


class BodyBudget:
    """Body bytes buffered at once in this process, in total and per owner (one per app; ``app.state`` is not
    reachable from an ASGI middleware's constructor, so the middleware owns it)."""

    def __init__(self, size: int = IN_FLIGHT_BUDGET_BYTES, *, owner_cap: int | None = None) -> None:
        self.size = size
        self.owner_cap = size if owner_cap is None else owner_cap
        self.used = 0
        self._owners: dict[str, int] = {}

    def take(self, n: int, owner: str = "") -> bool:
        mine = self._owners.get(owner, 0)
        if self.used + n > self.size or mine + n > self.owner_cap:
            return False
        self.used += n
        self._owners[owner] = mine + n
        return True

    def give_back(self, n: int, owner: str = "") -> None:
        self.used = max(0, self.used - n)
        left = self._owners.get(owner, 0) - n
        if left > 0:
            self._owners[owner] = left
        else:
            self._owners.pop(owner, None)

    def held(self, owner: str) -> int:
        return self._owners.get(owner, 0)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


class BodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        budget: BodyBudget | None = None,
        anonymous_budget: BodyBudget | None = None,
        *,
        grace_s: float = BODY_GRACE_S,
        min_rate: float = MIN_BODY_RATE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.budget = budget if budget is not None else BodyBudget(owner_cap=VERIFIED_OWNER_BYTES)
        self.anonymous = (
            anonymous_budget
            if anonymous_budget is not None
            else BodyBudget(ANONYMOUS_BUDGET_BYTES, owner_cap=ANONYMOUS_OWNER_BYTES)
        )
        self.grace_s = grace_s
        self.min_rate = min_rate
        self.clock = clock

    async def owner(self, scope: Scope) -> tuple[BodyBudget, str]:
        """The budget and owner a large body is charged to (raises the 401 / 503 of an unverified class route)."""
        state: Any = getattr(scope.get("app"), "state", None)
        if is_class_path(scope.get("path") or ""):
            token = _bearer(scope)
            if token is None:
                raise _unauthorized("missing_bearer_token")
            verifier = getattr(state, "verifier", None)
            if verifier is None:
                raise _unauthorized("invalid_token")
            try:
                principal = await verifier.verify(token)
            except InvalidToken:
                raise _unauthorized("invalid_token") from None
            except AuthUnavailable:
                raise HTTPException(status_code=503, detail="auth_unavailable") from None
            return self.budget, f"id:{principal.iss}|{principal.sub}"
        from app.ratelimit import client_address  # noqa: PLC0415 — ratelimit imports FastAPI deps; keep this lazy

        hops = getattr(getattr(state, "settings", None), "rate_limit_xff_hops", 1)
        return self.anonymous, "addr:" + client_address(Request(scope), hops)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = max_body_bytes(scope.get("path") or "")
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await _refuse(send)
            return
        start = self.clock()
        received = 0
        charged = 0
        charge: tuple[BodyBudget, str] | None = None

        async def next_message() -> Message:
            if received <= SMALL_BODY_BYTES:
                return await receive()
            # A body past the small size: the next bytes are due by the minimum rate (round 3, no endless trickle).
            remaining = start + self.grace_s + received / self.min_rate - self.clock()
            try:
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    return await receive()
            except TimeoutError:
                raise HTTPException(status_code=408, detail=TOO_SLOW, headers={"Connection": "close"}) from None

        async def capped_receive() -> Message:
            nonlocal received, charged, charge
            message = await next_message()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail=TOO_LARGE)
                if received > SMALL_BODY_BYTES:
                    if charge is None:
                        charge = await self.owner(scope)
                    due = received - charged
                    if not charge[0].take(due, charge[1]):
                        raise HTTPException(
                            status_code=503, detail=TOO_BUSY, headers={"Retry-After": str(RETRY_AFTER_BUSY_S)}
                        )
                    charged = received
            return message

        try:
            await self.app(scope, capped_receive, send)
        finally:
            if charged and charge is not None:
                charge[0].give_back(charged, charge[1])


async def _refuse(send: Send) -> None:
    body = json.dumps({"detail": TOO_LARGE}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("latin-1")),
                (b"cache-control", b"no-store"),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
