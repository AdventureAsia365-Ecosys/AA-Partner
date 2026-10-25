"""A small HTTPS client on the standard library (``urllib``, in a worker thread), as ``app.fx.fetch`` does: no new
dependency for the two JSON APIs (Google, Microsoft Graph). Tests pass their own :data:`Transport`."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.mail.providers import Transient

TIMEOUT_S = 15.0
MAX_BODY = 1024 * 1024


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes

    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.body or b"{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


Transport = Callable[[HttpRequest], HttpResponse]


def urllib_transport(request: HttpRequest) -> HttpResponse:
    if not request.url.startswith("https://"):
        raise ValueError("https only")
    req = urllib.request.Request(  # noqa: S310 — https URLs of fixed provider hosts only
        request.url, data=request.body, headers=request.headers, method=request.method
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310
            return HttpResponse(status=resp.status, body=resp.read(MAX_BODY))
    except urllib.error.HTTPError as exc:
        return HttpResponse(status=exc.code, body=exc.read(MAX_BODY) if exc.fp else b"")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Transient("unreachable") from exc


class HttpClient:
    def __init__(self, transport: Transport | None = None) -> None:
        self._transport = transport or urllib_transport

    async def call(
        self, method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None
    ) -> HttpResponse:
        request = HttpRequest(method=method, url=url, headers=dict(headers or {}), body=body)
        return await asyncio.to_thread(self._transport, request)

    async def form(self, url: str, fields: dict[str, str]) -> HttpResponse:
        return await self.call(
            "POST",
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            body=urllib.parse.urlencode(fields).encode(),
        )
