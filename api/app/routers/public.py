"""``/crm/public/v1``: the public inquiry form (C-01, card CRM-08). No token: the form's URL key is the only input.

Served on the client host (``go-dev.``, the ALB sends only ``/crm/public/*`` of that host here) so the hosted page posts
to its own origin (no CORS). Not a route class: nothing here reads agency data beyond the form itself, and the agency
is decided by the form key inside :func:`app.tenancy.intake_tx`.

- ``GET  /crm/public/v1/forms/{public_key}``: the form the page renders (field codes, honeypot name, agency label,
  locale, framing origins, privacy link). Public rate limit (``RATE_LIMIT_PUBLIC_PER_MINUTE``).
- ``POST /crm/public/v1/forms/{public_key}/submissions``: one submission (``app.intake.public.submit``): 202
  ``{"status": "received"}`` when stored, a repeat or a honeypot hit alike; 422 ``consent_required`` /
  ``invalid_submission`` (field codes only); 403 ``origin_not_allowed``; 429 per address
  (``RATE_LIMIT_INTAKE_PER_MINUTE``, before anything is read) and per form (``RATE_LIMIT_INTAKE_FORM_PER_HOUR``,
  charged only by a submission that passed the origin, honeypot, consent and answer checks); 413 over 32 KiB, also
  for a chunked body or one whose Content-Length lies: the body is streamed and the read stops at the cap, so no more
  than 32 KiB (plus one chunk) is ever held in memory.

Responses never carry an id, a name, or whether an e-mail is already known.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Path, Request
from fastapi.responses import JSONResponse

from app.db import request_pool
from app.intake import public as intake
from app.ratelimit import PublicRateLimit, client_address
from app.vault.provider import require_keys

router = APIRouter(prefix="/crm/public/v1", tags=["public"])

MAX_BODY_BYTES = 32 * 1024
PublicKey = Annotated[str, Path(min_length=20, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
NO_STORE = {"Cache-Control": "no-store"}


class BodyTooLarge(Exception):
    pass


async def read_capped(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """The request body, read chunk by chunk; :class:`BodyTooLarge` as soon as it passes ``limit`` bytes (a chunked
    body has no Content-Length, and a declared one may lie)."""
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > limit:
            raise BodyTooLarge
    return bytes(buf)


def _limit(request: Request, limiter_name: str, key: str) -> None:
    limiter = getattr(request.app.state, limiter_name)
    if not limiter.allow(key):
        raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": str(int(limiter.window_s))})


@router.get("/forms/{public_key}")
async def get_form(request: Request, public_key: PublicKey, _: PublicRateLimit) -> JSONResponse:
    pool = await request_pool(request)
    form = await intake.read_form(pool, public_key)
    # Short cache on the edge and in the web's own memo: a form changes rarely and the page view should not hit the DB.
    return JSONResponse(form, headers={"Cache-Control": "public, max-age=60"})


@router.post("/forms/{public_key}/submissions", status_code=202)
async def post_submission(
    request: Request,
    public_key: PublicKey,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    settings = request.app.state.settings
    _limit(request, "intake_limiter", client_address(request, settings.rate_limit_xff_hops))
    if content_length is not None and content_length > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body_too_large")
    try:
        body = await read_capped(request)
    except BodyTooLarge:
        raise HTTPException(status_code=413, detail="body_too_large") from None
    try:
        raw: Any = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid_json") from None
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    form_limiter = request.app.state.intake_form_limiter
    await intake.submit(
        pool,
        keys,
        settings,
        public_key,
        raw,
        origin=origin,
        form_quota=lambda: form_limiter.allow(public_key),
        quota_window_s=int(form_limiter.window_s),
    )
    return JSONResponse({"status": "received"}, status_code=202, headers=NO_STORE)
