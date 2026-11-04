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

The ceiling is :data:`DEFAULT_MAX_BYTES` (1 MiB: the largest JSON body of a normal route is a 100 000-character
message) except for the few upload routes in :data:`LARGE_BODY_ROUTES`, each a little above the route's own cap, so
the route keeps answering its own code (``import_too_large``, ``body_too_large``) for a body just over that cap.
"""

from __future__ import annotations

import json

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
_CONTENT_LENGTH = b"content-length"


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


class BodyBudget:
    """Body bytes buffered at once in this process (one per app; ``app.state`` is not reachable from an ASGI
    middleware's constructor, so the middleware owns it)."""

    def __init__(self, size: int = IN_FLIGHT_BUDGET_BYTES) -> None:
        self.size = size
        self.used = 0

    def take(self, n: int) -> bool:
        if self.used + n > self.size:
            return False
        self.used += n
        return True

    def give_back(self, n: int) -> None:
        self.used = max(0, self.used - n)


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, budget: BodyBudget | None = None) -> None:
        self.app = app
        self.budget = budget if budget is not None else BodyBudget()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = max_body_bytes(scope.get("path") or "")
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await _refuse(send)
            return
        received = 0
        charged = 0
        budget = self.budget

        async def capped_receive() -> Message:
            nonlocal received, charged
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail=TOO_LARGE)
                if received > SMALL_BODY_BYTES:
                    due = received - charged
                    if not budget.take(due):
                        raise HTTPException(
                            status_code=503, detail=TOO_BUSY, headers={"Retry-After": str(RETRY_AFTER_BUSY_S)}
                        )
                    charged = received
            return message

        try:
            await self.app(scope, capped_receive, send)
        finally:
            if charged:
                budget.give_back(charged)


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
