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


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

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

        async def capped_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail=TOO_LARGE)
            return message

        await self.app(scope, capped_receive, send)


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
