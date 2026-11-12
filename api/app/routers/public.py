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
  ceiling is answered 429 before any work, so random-id floods stay cheap. AA-969 (review round 3): a refused
  signature never locks a hook out (round 2's per-hook refusal ceiling let anyone who knew a hook id make every
  genuine callback 429 for a minute). Instead a request without ``X-Twilio-Signature`` is refused 403 before the
  database, and a hook id this task already resolved has its token cached (:class:`app.messaging.webhook.HookCache`):
  a bad signature is refused 403 in process at no database cost, a good one goes on; such a known hook also skips the
  misses check and takes its own slot key of the pool (:data:`app.tenant_gate.HOOK_KEY`), so neither a random-id
  flood (:data:`app.tenant_gate.HOOK_UNKNOWN_KEY`) nor anonymous browsing queues ahead of it.

- ``GET  /crm/public/v1/book/{public_key}?tour=&page=`` (AA-1072): the "Book now" widget of the public tour page for
  the agency whose intake form key the page link carries (:func:`app.intake.booking.widget`): the tour, its open
  fixed departures, the agency's name, currency and age bands. 404 ``form_not_found`` / ``booking_not_offered`` /
  ``tour_not_bookable``; 503 ``booking_unavailable``. Public rate limit.
- ``POST /crm/public/v1/book/{public_key}/price``: the live retail price of a departure for a party (a quote, no hold,
  nothing stored); its own per-address limit (``RATE_LIMIT_BOOKING_PRICE_PER_MINUTE``, default 20: each call asks
  Booking), not the intake limit the booking, the hold, the accept and "Pay online" share (S277 live check). 409
  ``price_on_request`` when the agency's retail for that party is below what AA charges it (no owner to approve a
  sale below cost on the public path).
- ``POST /crm/public/v1/book/{public_key}/bookings``: one booking (:func:`app.intake.booking.submit`): 201
  ``{status: created, link_path}`` (the client page to hold, accept and pay; the link also goes by e-mail), 200
  ``{status: already_created}`` for a repeated press, 202 ``{status: received}`` for a honeypot hit; 409 with the
  refusal code (``aa_sold_out``, ``no_retail_price``, ``price_on_request``, ``departure_not_available``, ...), 429 per
  address (the intake limit, then 5 attempts an hour per agency on the attempt rows; a ``price_on_request`` refusal
  does not count), 429 ``form_rate_limited`` per agency (AA-1092: ``RATE_LIMIT_INTAKE_FORM_PER_HOUR`` per form key, its
  own bucket ``book:<key>``, charged once the origin, honeypot and answers passed: many addresses together cannot make
  an agency mail an unbounded number of client links), 403 ``origin_not_allowed``, 32 KiB cap.

- ``GET  /crm/public/v1/hooks/vnpay/ipn`` (AA-1072): the IPN of an agency's own VNPay account
  (:func:`app.payments.gateway.handle_ipn`): always 200 with VNPay's ``{RspCode, Message}`` (``00`` confirmed,
  ``01`` unknown order, ``02`` already confirmed, ``04`` wrong amount, ``97`` bad signature, ``99`` malformed). The
  signature is checked with the agency's own secret before anything is written. An address whose unknown references pass
  ``HOOK_MISSES_PER_MINUTE`` is answered 429 before any work (a known gateway retries). AA-1092: a notice without
  a ``vnp_SecureHash`` of the SHA-512 hex shape is ``97`` before any lookup and counts as a miss of its address (VNPay
  signs every notice); a malformed one (``99``) counts too; a well-formed but wrong signature counts against the
  merchant reference it names (``IPN_REF_REFUSALS_PER_MINUTE``, then 429 for that reference only, before the
  database), never against the address, which VNPay shares between agencies.
- ``GET  /crm/public/v1/pay/stub`` and ``POST /crm/public/v1/pay/stub/{ref}`` (dev / test / local, accounts in mode
  ``stub`` only): the simulated gateway page and its Pay / Cancel buttons (a signed IPN through the same handler,
  then 303 to the client's return page). 404 everywhere else.

Responses never carry an id, a name, or whether an e-mail is already known.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.calendar import public as calendar_public
from app.db import request_pool
from app.errors import NotFound
from app.intake import booking as public_booking
from app.intake import public as intake
from app.intake import stage2_public
from app.messaging import webhook as messaging_hooks
from app.payments import gateway, vnpay
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
    cache: messaging_hooks.HookCache | None = getattr(request.app.state, "hook_cache", None)
    address = client_address(request, request.app.state.settings.rate_limit_xff_hops)
    # A hook id this task knows is a real one: its callbacks never wait on the address's misses (Twilio's addresses
    # are shared with whatever else Twilio posts here).
    if not (cache is not None and cache.known(hook_id)) and misses.exhausted(address):
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
            pool,
            runtime,
            messaging_hooks.HookRequest(hook_id=hook_id, kind=kind, params=params, signature=signature),
            cache=cache,
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


# ── AA-1072: "Book now" and the agency's own payment gateway ─────────────────────────────────────


def _booking_deps(request: Request) -> tuple[Any, Any]:
    return getattr(request.app.state, "aa_products", None), getattr(request.app.state, "booking_client", None)


TourKey = Annotated[str | None, Query(min_length=1, max_length=128)]


@router.get("/book/{public_key}")
async def get_booking_widget(
    request: Request, public_key: PublicKey, _: PublicRateLimit, tour: TourKey = None, page: TourKey = None
) -> JSONResponse:
    pool = await request_pool(request)
    cache, client = _booking_deps(request)
    out = await public_booking.widget(pool, cache, client, public_key, tour=tour, page_id=page)
    return JSONResponse(out, headers=NO_STORE)


@router.post("/book/{public_key}/price")
async def post_booking_price(
    request: Request, public_key: PublicKey, content_length: Annotated[int | None, Header()] = None
) -> JSONResponse:
    settings = request.app.state.settings
    _limit(request, "booking_price_limiter", client_address(request, settings.rate_limit_xff_hops))
    raw = await _json_body(request, content_length)
    pool = await request_pool(request)
    cache, client = _booking_deps(request)
    return JSONResponse(await public_booking.price(pool, cache, client, public_key, raw), headers=NO_STORE)


@router.post("/book/{public_key}/bookings")
async def post_booking(
    request: Request,
    public_key: PublicKey,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    settings = request.app.state.settings
    address = client_address(request, settings.rate_limit_xff_hops)
    _limit(request, "intake_limiter", address)
    raw = await _json_body(request, content_length)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    cache, client = _booking_deps(request)
    form_limiter = request.app.state.intake_form_limiter
    out = await public_booking.submit(pool, keys, settings, cache, client, public_key, raw, origin=origin,
                                      client_ip=address,
                                      form_quota=lambda: form_limiter.allow(f"book:{public_key}"),
                                      quota_window_s=int(form_limiter.window_s))  # fmt: skip
    code = {"created": 201, "received": 202}.get(out["status"], 200)
    return JSONResponse(out, status_code=code, headers={**NO_STORE, "Referrer-Policy": "no-referrer"})


def _vnp_fields(request: Request) -> dict[str, str]:
    """The ``vnp_*`` query fields (at most 40, each value at most 512 characters; others ignored)."""
    out: dict[str, str] = {}
    for key, value in request.query_params.multi_items()[:40]:
        if key.startswith("vnp_") and len(key) <= 40 and len(value) <= 512:
            out[key] = value
    return out


@router.get("/hooks/vnpay/ipn")
async def get_vnpay_ipn(request: Request) -> JSONResponse:
    misses = request.app.state.hook_miss_limiter
    address = client_address(request, request.app.state.settings.rate_limit_xff_hops)
    if misses.exhausted(address):
        raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": str(int(misses.window_s))})
    fields = _vnp_fields(request)
    # AA-1092 (pre-PII review): VNPay signs every notice, so an unsigned or garbled one is refused before any lookup
    # and counted against its address like an unknown reference (it can never be one of VNPay's shared addresses).
    if not vnpay.signature_shaped(fields):
        misses.allow(address)
        return JSONResponse(vnpay.ipn_answer(vnpay.BAD_SIGNATURE), headers=NO_STORE)
    refusals = request.app.state.ipn_ref_refusal_limiter
    # Per (address, reference): a forger who knows a live reference locks only their own address for it, so VNPay's
    # genuine notice for that reference (from VNPay's own addresses) always reaches the handler.
    ref_key = f"ref:{address}:{fields.get('vnp_TxnRef') or ''}"
    if refusals.exhausted(ref_key):
        raise HTTPException(
            status_code=429, detail="rate_limited", headers={"Retry-After": str(int(refusals.window_s))}
        )
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    answer = await gateway.handle_ipn(pool, keys, fields)
    if answer["RspCode"] in (vnpay.NOT_FOUND, vnpay.OTHER):
        misses.allow(address)  # unknown or malformed notices count against the address
    elif answer["RspCode"] == vnpay.BAD_SIGNATURE:
        refusals.allow(ref_key)  # forged notices count against (address, reference), never the address alone
    return JSONResponse(answer, headers=NO_STORE)


@router.get("/pay/stub")
async def get_pay_stub(request: Request, _: PublicRateLimit) -> HTMLResponse:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    page = await gateway.stub_page(pool, keys, request.app.state.settings, _vnp_fields(request))
    return HTMLResponse(page, headers={**NO_STORE, "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})


StubRef = Annotated[str, Path(min_length=16, max_length=40, pattern=r"^[A-Za-z0-9]+$")]


@router.post("/pay/stub/{ref}")
async def post_pay_stub(request: Request, ref: StubRef, _: PublicRateLimit) -> RedirectResponse:
    try:
        body = await read_capped(request, 1024)
    except BodyTooLarge:
        raise HTTPException(status_code=413, detail="body_too_large") from None
    fields = dict(urllib.parse.parse_qsl(body.decode("utf-8", "replace"), max_num_fields=4))
    outcome = "paid" if fields.get("outcome") == "paid" else "cancel"
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    target = await gateway.stub_pay(pool, keys, request.app.state.settings, ref, outcome)
    return RedirectResponse(target, status_code=303, headers=NO_STORE)
