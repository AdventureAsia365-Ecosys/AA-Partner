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

- ``GET  /crm/public/v1/s/{token}`` (AA-943): the Stage 2 planning form a signed link opens
  (:func:`app.intake.stage2_public.read`): pages with their R31 branches, the trip style, the agency's name and the
  bands the client already gave (F2 only). 404 ``link_not_found`` for an unknown, expired, revoked or foreign token.
- ``POST /crm/public/v1/s/{token}``: the answers (:func:`app.intake.stage2_public.submit`): 202; 422
  ``health_consent_required`` / ``invalid_submission``; 403 ``origin_not_allowed``; the same per-address limit and
  32 KiB cap as the inquiry form.

- ``GET  /crm/public/v1/calendar/{token}.ics`` (AA-961): a member's ICS calendar feed
  (:func:`app.calendar.public.feed`), fetched by their calendar app; ``text/calendar``, no names, titles or money;
  404 ``feed_not_found`` for an unknown, revoked or expired token, an inactive member or a locked agency (subscribed
  calendars stop updating). Public rate limit.

- ``POST /crm/public/v1/hooks/twilio/{hook_id}`` and ``.../{hook_id}/status`` (AA-958): an agency's Twilio webhooks
  (an inbound SMS / WhatsApp message, a status callback), form-encoded, at most 64 KiB
  (:func:`app.messaging.webhook.handle`). ``X-Twilio-Signature`` is checked with the agency's own Auth Token before
  anything is stored: 403 ``invalid_signature``; 404 ``hook_not_found``; else 200 with an empty TwiML response. **Not**
  under the per-address public limit: Twilio posts every agency's webhooks from one shared pool of addresses, and a
  429 would lose a client's message for good (Twilio does not retry an inbound webhook). The signature authenticates
  them; only unknown hook ids (404) are counted, per address (``HOOK_MISSES_PER_MINUTE``), and an address over that
  ceiling is answered 429 before any work, so random-id floods stay cheap.

Responses never carry an id, a name, or whether an e-mail is already known.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Path, Request
from fastapi.responses import JSONResponse, Response

from app.calendar import public as calendar_public
from app.db import request_pool
from app.errors import NotFound
from app.intake import public as intake
from app.intake import stage2_public
from app.messaging import webhook as messaging_hooks
from app.ratelimit import PublicRateLimit, client_address
from app.vault.provider import require_keys

router = APIRouter(prefix="/crm/public/v1", tags=["public"])

MAX_BODY_BYTES = 32 * 1024
PublicKey = Annotated[str, Path(min_length=20, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
LinkToken = Annotated[str, Path(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")]
HookId = Annotated[str, Path(min_length=32, max_length=32, pattern=r"^[0-9a-f]+$")]
MAX_HOOK_BYTES = 64 * 1024
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
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


async def _json_body(request: Request, content_length: int | None) -> Any:
    if content_length is not None and content_length > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body_too_large")
    try:
        body = await read_capped(request)
    except BodyTooLarge:
        raise HTTPException(status_code=413, detail="body_too_large") from None
    try:
        return json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid_json") from None


@router.get("/s/{token}")
async def get_stage2(request: Request, token: LinkToken, _: PublicRateLimit) -> JSONResponse:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    form = await stage2_public.read(pool, keys, token)
    return JSONResponse(form, headers={**NO_STORE, "Referrer-Policy": "no-referrer"})


@router.post("/s/{token}", status_code=202)
async def post_stage2(
    request: Request,
    token: LinkToken,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    settings = request.app.state.settings
    _limit(request, "intake_limiter", client_address(request, settings.rate_limit_xff_hops))
    raw = await _json_body(request, content_length)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    result = await stage2_public.submit(pool, keys, settings, token, raw, origin=origin)
    return JSONResponse(result, status_code=202, headers=NO_STORE)


@router.get("/calendar/{token}.ics")
async def get_calendar_feed(request: Request, token: LinkToken, _: PublicRateLimit) -> Response:
    pool = await request_pool(request)
    body = await calendar_public.feed(pool, token, web_origin=request.app.state.settings.invitation_origin)
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Cache-Control": "private, max-age=300",
            "Referrer-Policy": "no-referrer",
            "Content-Disposition": 'inline; filename="crm-calendar.ics"',
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _hook(request: Request, hook_id: str, kind: str, signature: str | None) -> Response:
    runtime = getattr(request.app.state, "messaging_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="messaging_not_configured")
    misses = request.app.state.hook_miss_limiter
    address = client_address(request, request.app.state.settings.rate_limit_xff_hops)
    if misses.exhausted(address):
        raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": str(int(misses.window_s))})
    try:
        body = await read_capped(request, MAX_HOOK_BYTES)
    except BodyTooLarge:
        raise HTTPException(status_code=413, detail="body_too_large") from None
    try:
        params = urllib.parse.parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=200)
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid_form") from None
    pool = await request_pool(request)
    try:
        await messaging_hooks.handle(
            pool, runtime, messaging_hooks.HookRequest(hook_id=hook_id, kind=kind, params=params, signature=signature)
        )
    except NotFound:
        misses.allow(address)  # only a miss is counted: genuine (signed) callbacks never use the address's minute
        raise
    return Response(EMPTY_TWIML, media_type="text/xml", headers=NO_STORE)


@router.post("/hooks/twilio/{hook_id}")
async def post_twilio_inbound(
    request: Request,
    hook_id: HookId,
    x_twilio_signature: Annotated[str | None, Header()] = None,
) -> Response:
    return await _hook(request, hook_id, "inbound", x_twilio_signature)


@router.post("/hooks/twilio/{hook_id}/status")
async def post_twilio_status(
    request: Request,
    hook_id: HookId,
    x_twilio_signature: Annotated[str | None, Header()] = None,
) -> Response:
    return await _hook(request, hook_id, "status", x_twilio_signature)
