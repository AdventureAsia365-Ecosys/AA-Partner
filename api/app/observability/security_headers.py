"""Security headers on every CRM API response (AA-1111, S294 lane of the ZAP baseline triage).

The CIS and Booking APIs got these in S284; the CRM API (``/crm/*``, behind the CloudFront edge of the agency and
client hosts) still answered without them. Every response now carries:

- ``X-Content-Type-Options: nosniff`` — a browser never sniffs a JSON, text or calendar body into something it would
  run;
- ``Strict-Transport-Security`` — the same value as the CRM web, the admins and the other APIs (two years,
  sub-domains, no preload): every CRM host is HTTPS only.

A header the endpoint already set wins (the calendar feed and the link pages keep their own). The ``Server`` header is
not sent at all: the image starts uvicorn with ``--no-server-header`` (Dockerfile).

JSON bodies also name their character set (S294 hardening): a bare ``application/json`` content type (FastAPI's
``JSONResponse`` default) becomes ``application/json; charset=utf-8``, so no client has to guess the encoding. A
content type that already carries parameters, or any other media type (NDJSON, CSV, calendar, files), is left as is.

Pure ASGI (not ``BaseHTTPMiddleware``): no extra task, no body buffering. Registered just inside the request id
middleware and outside the body ceiling in ``app/main.py`` (so a 413 carries the set too); the unhandled 500 (answered
by Starlette's ServerErrorMiddleware, outside every user middleware) gets the same headers from
:func:`app.observability.request_id.unhandled_error_handler`.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}


JSON_MEDIA_TYPE = "application/json"
JSON_CONTENT_TYPE = "application/json; charset=utf-8"


def with_json_charset(content_type: str) -> str:
    """``application/json`` (any case, no parameters) -> ``application/json; charset=utf-8``; others unchanged."""
    return JSON_CONTENT_TYPE if content_type.strip().lower() == JSON_MEDIA_TYPE else content_type


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
                content_type = headers.get("content-type")
                if content_type is not None:
                    charset = with_json_charset(content_type)
                    if charset != content_type:
                        headers["content-type"] = charset
            await send(message)

        await self.app(scope, receive, send_with_headers)
