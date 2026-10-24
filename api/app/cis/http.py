"""A minimal HTTPS client for the CIS contracts and the M2M token endpoint (AA-978), on the standard library.

``urllib`` runs in a worker thread (``asyncio.to_thread``) so the event loop never blocks; the CRM makes a few calls a
minute, so a thread per call is cheap and no new runtime dependency is needed. Rules:

- **No redirects.** A 3xx is returned as it is, never followed: a redirect must not carry a bearer token or the client
  secret to another host.
- **Bounded.** A timeout per call and a response body capped at :data:`MAX_BODY_BYTES` (a larger body is an error).
- **Errors as values.** Any HTTP status comes back as an :class:`HttpResponse`; only a transport failure (DNS,
  connect, TLS, timeout, an oversized or truncated body) raises :class:`HttpError`, with its class name only (no URL,
  no body).
"""

from __future__ import annotations

import asyncio
import http.client
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_S = 10.0


class HttpError(Exception):
    """The call did not produce an HTTP response (network, TLS, timeout, oversized body)."""

    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict)


# (method, url, headers, body, timeout_s) -> response. Tests pass a fake with this shape.
Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], Awaitable[HttpResponse]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # urllib's signature
        return None  # urllib then raises HTTPError with the 3xx, returned below as a response


_OPENER = urllib.request.build_opener(_NoRedirect())


def _read(stream, limit: int) -> bytes:  # a urllib response or HTTPError
    body = stream.read(limit + 1)
    if len(body) > limit:
        raise HttpError("response_too_large")
    # read(n) returns what came before the peer closed, without an error: a body shorter than its Content-Length is a
    # transport failure, never a short JSON document (a chunked body cut short raises IncompleteRead instead).
    length = (stream.headers or {}).get("Content-Length")
    if length is not None and length.isdigit() and len(body) != int(length):
        raise HttpError("incomplete_body")
    return body


def _call(method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> HttpResponse:
    if not url.startswith(("https://", "http://")):
        raise HttpError("bad_url")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)  # noqa: S310 — scheme checked
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            return HttpResponse(resp.status, _read(resp, MAX_BODY_BYTES), dict(resp.headers.items()))
    except urllib.error.HTTPError as exc:
        with exc:
            return HttpResponse(exc.code, _read(exc, MAX_BODY_BYTES), dict(exc.headers.items()) if exc.headers else {})
    except HttpError:
        raise
    except TimeoutError:
        raise HttpError("timeout") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:  # IncompleteRead, a bad status line
        reason = getattr(exc, "reason", None)
        raise HttpError("timeout" if isinstance(reason, TimeoutError) else "unreachable") from None


async def urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout_s: float
) -> HttpResponse:
    return await asyncio.to_thread(_call, method, url, headers, body, timeout_s)
