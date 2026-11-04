"""Sentry error tracking for the AA-Partner API and its crm.job worker (AA-933; copied from AA-Booking AA-871).

Same shape as AA-CIS-App ``shared/sentry_init.py`` (AA-870): ``send_default_pii=False``, no request bodies, no cookies,
request headers reduced to an allowlist, key-like query parameters redacted, 10 % traces and profiles. On top of it:
frame locals are never sent (``include_local_variables=False``), the ``user`` block is dropped, and the
:mod:`app.observability.scrub` redaction runs over exception values, messages, extra, tags, contexts and breadcrumbs.

Off unless ``SENTRY_DSN`` is set (the task definition maps it from Secrets Manager). ``release`` = the image's
``GIT_SHA``, ``environment`` = ``ENVIRONMENT``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import sentry_sdk

from app.observability.request_id import redact_path
from app.observability.scrub import FILTERED, scrub
from app.settings import SERVICE_NAME, Settings

log = logging.getLogger(__name__)

# Request headers that may be sent with an event. Everything else (Authorization, cookies, any custom key) is dropped.
SAFE_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "content-length",
        "content-type",
        "host",
        "user-agent",
        "x-forwarded-proto",
        "x-request-id",
    }
)
TRACES_SAMPLE_RATE = 0.1
PROFILES_SAMPLE_RATE = 0.1
# Tags this code sets itself; not passed through the scrubber.
_KEEP_TAGS = frozenset({"request_id", "job_id", "job_kind", "service", "environment", "release"})
# Contexts the SDK fills from the process, never from a request: sent as they are.
_SDK_CONTEXTS = frozenset({"runtime", "os", "trace"})
# Exceptions whose message echoes input values (asyncpg: bad query arguments; pydantic: input_value=...): only the type
# and the stack are sent.
_VALUE_ECHOING_MODULES = frozenset({"asyncpg", "pydantic", "pydantic_core", "jsonschema", "json"})


def _scrub_request(request: dict) -> None:
    request.pop("data", None)
    request.pop("cookies", None)
    request.pop("env", None)
    headers = request.get("headers")
    if isinstance(headers, dict):
        request["headers"] = {k: v for k, v in headers.items() if str(k).lower() in SAFE_HEADERS}
    # The query string can carry customer values (search terms, emails, phone numbers): never sent. The URL keeps
    # only scheme, host and path.
    request.pop("query_string", None)
    if isinstance(request.get("url"), str):
        request["url"] = redact_url(request["url"])


def redact_url(url: str) -> str:
    """``url`` without query or fragment, its bearer-like path segments (client link tokens, calendar feed tokens,
    Twilio hook ids) replaced by ``[token]`` (:func:`app.observability.request_id.redact_path`, AA-969 F4), then the
    PII scrub. A working link must never reach a third party: error events and the 10 % transactions carry this URL."""
    base = url.split("?", 1)[0].split("#", 1)[0]
    scheme, sep, rest = base.partition("://")
    if sep:
        host, slash, path = rest.partition("/")
        base = f"{scheme}://{host}{slash}{redact_path(path) or ''}" if slash else base
    else:
        base = redact_path(base) or ""
    return scrub(base)


_METHOD = re.compile(r"^(GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS) ")
# Span data keys the SDK fills with a URL or a path (httpx / urllib / ASGI spans).
_URL_KEYS = ("url", "http.url", "http.target", "path", "server.address.url", "url.full", "url.path")


def _redact_described_url(text: str) -> str:
    """A span description: ``GET https://host/path?q`` (HTTP client spans) or a bare URL / path."""
    match = _METHOD.match(text)
    if match:
        return match.group(0) + redact_url(text[match.end() :])
    if "/" in text and " " not in text:
        return redact_url(text)
    return scrub(text)


def _scrub_spans(spans: Any) -> None:
    """Every span of a transaction: URL-bearing descriptions and data keys redacted like the request URL."""
    if not isinstance(spans, list):
        return
    for span in spans:
        if not isinstance(span, dict):
            continue
        if isinstance(span.get("description"), str):
            span["description"] = _redact_described_url(span["description"])
        data = span.get("data")
        if isinstance(data, dict):
            for key in _URL_KEYS:
                if isinstance(data.get(key), str):
                    data[key] = redact_url(data[key])


def _scrub_frames(holder: dict) -> None:
    for frame in (holder.get("stacktrace") or {}).get("frames") or []:
        frame.pop("vars", None)
        for key in ("context_line", "pre_context", "post_context"):
            if key in frame:
                frame[key] = scrub(frame[key])


def before_send(event: dict, hint: Any) -> dict:
    """Drop bodies, cookies, unsafe headers, the user and frame locals; redact PII in every text field."""
    request = event.get("request")
    if isinstance(request, dict):
        _scrub_request(request)
    event.pop("user", None)
    if isinstance(event.get("transaction"), str):
        # The route template normally (``/p/{token}``). When no route matched (a 404, a 307 for a trailing slash, a
        # calendar fetch without ``.ics``) the SDK names the transaction by the full URL
        # (``http://10.0.0.1:8000/crm/public/v1/p/<token>/``): redacted whatever its shape (review round 3).
        event["transaction"] = redact_url(event["transaction"])
    _scrub_spans(event.get("spans"))
    for exc in (event.get("exception") or {}).get("values") or []:
        if str(exc.get("module") or "").split(".")[0] in _VALUE_ECHOING_MODULES:
            exc["value"] = FILTERED
        elif isinstance(exc.get("value"), str):
            exc["value"] = scrub(exc["value"])
        _scrub_frames(exc)
    for thread in (event.get("threads") or {}).get("values") or []:
        _scrub_frames(thread)
    if "message" in event:
        event["message"] = scrub(event["message"])
    if isinstance(event.get("logentry"), dict):
        event["logentry"] = scrub(event["logentry"])
    if isinstance(event.get("extra"), dict):
        event["extra"] = scrub(event["extra"])
    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        # The SDK's own contexts (runtime {"name": "CPython"}, os, trace) hold no customer data; a key scrub would
        # turn `runtime.name` into [Filtered] (seen on AA-Booking, AA-871). Every other context is scrubbed.
        event["contexts"] = {k: v if k in _SDK_CONTEXTS else scrub(v) for k, v in contexts.items()}
    if isinstance(event.get("tags"), dict):
        event["tags"] = scrub(event["tags"], keep=_KEEP_TAGS)
    crumbs = event.get("breadcrumbs")
    if isinstance(crumbs, dict) and isinstance(crumbs.get("values"), list):
        crumbs["values"] = [before_breadcrumb(c, None) for c in crumbs["values"] if isinstance(c, dict)]
    return event


def before_send_transaction(event: dict, hint: Any) -> dict:
    """Transactions (10 % traces) carry the same request block as errors: apply the same scrub (AA-871 review)."""
    return before_send(event, hint)


def before_breadcrumb(crumb: dict, hint: Any) -> dict:
    """Redact PII / secrets in a breadcrumb's message and data (log lines, HTTP URLs, SQL)."""
    data = crumb.get("data")
    if isinstance(data, dict):
        for key in ("url", "http.url", "path"):
            if isinstance(data.get(key), str):
                data[key] = redact_url(data[key])
    for key in ("message", "data"):
        if key in crumb:
            crumb[key] = scrub(crumb[key])
    return crumb


def init_sentry(settings: Settings, service: str = "api") -> bool:
    """Start the SDK when ``SENTRY_DSN`` is set. Returns whether it started."""
    if not settings.sentry_dsn:
        log.info("sentry_disabled (SENTRY_DSN not set)")
        return False
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        environment=settings.environment,
        release=settings.git_sha,
        server_name=SERVICE_NAME,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        traces_sample_rate=TRACES_SAMPLE_RATE,
        profiles_sample_rate=PROFILES_SAMPLE_RATE,
        before_send=before_send,
        before_send_transaction=before_send_transaction,
        before_breadcrumb=before_breadcrumb,
    )
    sentry_sdk.set_tag("service", service)
    log.info("sentry_enabled environment=%s release=%s", settings.environment, settings.git_sha)
    return True
