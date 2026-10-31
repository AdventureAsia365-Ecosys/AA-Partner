"""``/crm/public/v1`` client pages of CRM-18 (AA-946): the proposal accept page and the traveller portal base. No
token: the signed link in the path only (43 characters, SHA-256 stored), plus, in the portal, the session cookie a
verified e-mail code set. Served on the client host (the ALB's ``/crm/public/*`` rule), so the browser calls its own
origin (no CORS) and the per-address limits key on the real visitor.

- ``GET  /crm/public/v1/p/{token}``: the proposal (:func:`app.proposals.public.read`).
- ``POST /crm/public/v1/p/{token}/accept``: accept with the typed full name, the terms tick and the price hash
  (:func:`app.proposals.public.accept`); 409 ``price_changed`` / ``terms_changed``.
- AA-950 (CRM-21b, Fix B11): the same page **by cookie**. The web exchanges the link's token for the host-only cookie
  ``aa_proposal_link`` (httpOnly, Secure, SameSite=Strict, Path ``/crm/public/v1/proposal``) and strips it from the
  URL; the token stays valid until the link expires, so a second device opening the link gets in too.
  ``GET /proposal``, ``POST /proposal/pick`` (Keep / Swap / Choose later), ``POST /proposal/hold`` (the version's hold
  set: 200, 409 ``option_sold_out`` / ``hold_unavailable``, 429 ``recheck_limit``, 503 ``booking_unavailable``, each
  with the fresh page), ``POST /proposal/accept`` (409 ``hold_expired`` with the re-quote offer, ``choice_changed``),
  ``POST /proposal/change-request``. No cookie, or an unusable link: 404 ``link_not_found``.
- AA-952 slice 4 (C-04 deposit, C31, Fix A8), by cookie: ``POST /proposal/invoices/{id}/claim`` ("I have paid":
  the invoice reads ``payment_claimed``, Booking extends the AA hold once, the agent verifies; 410
  ``pay_link_expired`` once the hold ran out) and ``POST /proposal/invoices/{id}/card`` (the card pay button: 410
  ``pay_link_expired`` past the hold, never a payment session; 409 ``card_not_offered`` / ``provider_not_available``
  until the agency's own card provider has code). The accepted page lists the invoices under ``payments``.
- ``GET  /crm/public/v1/portal/{token}``: the portal home (``signed_in: false`` without a session).
- ``POST /crm/public/v1/portal/{token}/code``: e-mail a code (``{"purpose": "sign_in" | "sign"}``).
- ``POST /crm/public/v1/portal/{token}/session``: check a sign-in code; 200 sets the session cookie
  (``aa_portal_s``: httpOnly, Secure, SameSite=Strict, Path = this portal's own path, 12 hours); a wrong code is 422
  ``code_invalid`` with ``attempts_left`` (the counter commits), an old one 422 ``code_expired``.
- ``POST /crm/public/v1/portal/{token}/sign-out``: ends the session and clears the cookie.
- ``PUT  /crm/public/v1/portal/{token}/stage3``: the traveller's Stage 3 details (write-only); **403
  ``stage3_locked`` before the deposit** (card R), 403 ``portal_sign_in_required`` without a session.
- ``POST /crm/public/v1/portal/{token}/sign``: sign the envelope (e-sign v1, AA-955: booking terms + waiver +
  insurance declination; for oneself or, as the adult lead, for a minor) with a fresh signing code; a refused code is
  422 and logged.
- AA-955 (design §2.11 E): ``POST /crm/public/v1/p/{token}/terms`` and ``POST /crm/public/v1/proposal/terms``: accept
  the agency's newer booking terms version on an accepted proposal (409 ``terms_changed`` / ``nothing_to_accept``).

The accept, the terms re-accept and the signing record the client address (the same one the per-address limit uses)
and the browser's User-Agent as evidence (under the vault DEK; only their SHA-256 in the evidence records).

Every write: the Origin check of the client host, a per-address limit before the database is read, a 32 KiB body
cap. 404 ``link_not_found`` for any unusable link. Answers carry ``no-store`` and ``Referrer-Policy: no-referrer``
(the token is in the path). The per-address limit has two buckets (429 ``rate_limited`` + ``Retry-After`` either way):

- anonymous and pre-sign-in writes (every proposal write, the sign-in code, the code check): the intake limiter,
  ``RATE_LIMIT_INTAKE_PER_MINUTE`` (default 5), the inquiry form's anti-abuse ceiling;
- writes only a signed-in traveller can make (Stage 3, flights, passport scan, the signing code, signing, companion
  invite, switch, sign-out), when the request carries a session cookie: the portal write limiter,
  ``RATE_LIMIT_PORTAL_WRITE_PER_MINUTE`` (default 30), so a family filling the portal from one address is not stopped
  by the form ceiling. Without a session cookie they keep the intake limiter. The cookie alone is not trusted: such a
  write also holds a slot of the unproven-session limiter (``RATE_LIMIT_INTAKE_PER_MINUTE``) until the session check
  passes (:func:`_session_write`), so a forged or stale cookie gets the anonymous ceiling of refused writes. See
  ``docs/implementation-notes/AA-953-write-limit.md`` for the per-route list.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.db import request_pool
from app.documents.store import require_store
from app.errors import NotFound
from app.portal import public as portal
from app.proposals import public as proposal_page
from app.ratelimit import PublicRateLimit, client_address
from app.routers.public import BodyTooLarge, LinkToken, _json_body, _limit, read_capped
from app.vault.provider import require_keys

router = APIRouter(prefix="/crm/public/v1", tags=["client-pages"])

HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
SessionCookie = Annotated[str | None, Cookie(alias=portal.SESSION_COOKIE)]
PROPOSAL_COOKIE = "aa_proposal_link"
ProposalCookie = Annotated[str | None, Cookie(alias=PROPOSAL_COOKIE)]
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _write_limit(request: Request) -> None:
    """An anonymous or pre-sign-in write (proposal pages, sign-in code, code check): the intake limiter
    (``RATE_LIMIT_INTAKE_PER_MINUTE``, default 5 per address), the anti-abuse ceiling of the inquiry form."""
    settings = request.app.state.settings
    _limit(request, "intake_limiter", client_address(request, settings.rate_limit_xff_hops))


@asynccontextmanager
async def _session_write(request: Request, session: str | None) -> AsyncIterator[None]:
    """Wrap a write only a signed-in traveller can make (Stage 3, flights, passport scan, the signing code, signing,
    companion invite, switch, sign-out). Every limit runs on entry, before the body or the database is read.

    - No session cookie of the issued shape (none, a malformed one): the anonymous intake limiter, as any write
      without a session (such a request cannot pass the handler's session check anyway).
    - A session cookie of the issued shape: one slot of the **unproven session** limiter
      (``RATE_LIMIT_INTAKE_PER_MINUTE`` per address, the anonymous ceiling) is taken, then the portal write limiter
      (``RATE_LIMIT_PORTAL_WRITE_PER_MINUTE``, default 30 per address: a family filling the portal from one home
      address is not stopped by the inquiry-form ceiling, AA-953 follow-up). The handler checks the session as before;
      once it proved valid (:func:`app.portal.public.mark_session_proven`) the unproven slot is given back. A forged
      or stale cookie keeps its slot, so a caller who is not signed in still gets at most the anonymous ceiling of
      such writes per address per minute, and a parallel burst cannot pass before the first refusal is counted.
    """
    settings = request.app.state.settings
    key = client_address(request, settings.rate_limit_xff_hops)
    if not (session and _TOKEN.match(session)):
        _limit(request, "intake_limiter", key)
        yield
        return
    unproven = request.app.state.portal_unproven_limiter
    _limit(request, "portal_unproven_limiter", key)
    window = unproven.window
    try:
        _limit(request, "portal_write_limiter", key)
    except HTTPException:
        unproven.release(key, window)  # stopped before anything ran: not a refused session
        raise
    with portal.session_proof() as proof:
        try:
            yield
        finally:
            if proof.proven:
                unproven.release(key, window)


def _code_write(request: Request, session: str | None, raw: Any) -> AbstractAsyncContextManager[None]:
    """``POST .../code``: the body (capped, no database read) decides the bucket. A signing code (``purpose: sign``)
    is asked by a signed-in traveller (:func:`_session_write`); a sign-in code is anonymous and e-mails someone, so it
    keeps the intake limiter whatever the cookie."""
    purpose = raw.get("purpose", "sign_in") if isinstance(raw, dict) else None
    return _session_write(request, session if purpose == "sign" else None)


def _evidence(request: Request) -> dict[str, str | None]:
    """The client address and user agent the e-sign evidence keeps (AA-955)."""
    settings = request.app.state.settings
    return {
        "client_ip": client_address(request, settings.rate_limit_xff_hops),
        "user_agent": request.headers.get("user-agent"),
    }


def _json(body: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, headers=HEADERS)


def _cookie_path(token: str) -> str:
    return f"{portal.SESSION_PATH}{token}"


# ── proposal (C-03 thin, C-04 accept) ────────────────────────────────────────────────────────────


@router.get("/p/{token}")
async def get_proposal(request: Request, token: LinkToken, _: PublicRateLimit) -> JSONResponse:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    return _json(await proposal_page.read(pool, keys, token))


@router.post("/p/{token}/accept")
async def post_proposal_accept(
    request: Request,
    token: LinkToken,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    _write_limit(request)
    raw = await _json_body(request, content_length)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    result = await proposal_page.accept(
        pool, keys, request.app.state.settings, token, raw, origin=origin, **_evidence(request)
    )
    return _json(result)


@router.post("/p/{token}/terms")
async def post_proposal_terms(
    request: Request,
    token: LinkToken,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    _write_limit(request)
    raw = await _json_body(request, content_length)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    result = await proposal_page.reaccept_terms(
        pool, keys, request.app.state.settings, token, raw, origin=origin, **_evidence(request)
    )
    return _json(result)


# ── proposal by cookie (AA-950, Fix B11) ─────────────────────────────────────────────────────────


def _cookie_token(token: str | None) -> str:
    if not token or not _TOKEN.match(token):
        raise NotFound("link_not_found")
    return token


@router.get("/proposal")
async def get_proposal_by_cookie(request: Request, _: PublicRateLimit, token: ProposalCookie = None) -> JSONResponse:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    return _json(await proposal_page.read(pool, keys, _cookie_token(token)))


@router.post("/proposal/pick")
async def post_proposal_pick(
    request: Request,
    token: ProposalCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    _write_limit(request)
    raw = await _json_body(request, content_length)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    settings = request.app.state.settings
    return _json(await proposal_page.pick(pool, keys, settings, _cookie_token(token), raw, origin=origin))


@router.post("/proposal/hold")
async def post_proposal_hold(
    request: Request, token: ProposalCookie = None, origin: Annotated[str | None, Header()] = None
) -> JSONResponse:
    _write_limit(request)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    booking = getattr(request.app.state, "booking_client", None)
    status, body = await proposal_page.hold(
        pool, keys, request.app.state.settings, booking, _cookie_token(token), origin=origin
    )
    return _json(body, status)


@router.post("/proposal/accept")
async def post_proposal_accept_by_cookie(
    request: Request,
    token: ProposalCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    _write_limit(request)
    raw = await _json_body(request, content_length)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    settings = request.app.state.settings
    return _json(
        await proposal_page.accept(pool, keys, settings, _cookie_token(token), raw, origin=origin, **_evidence(request))
    )


@router.post("/proposal/terms")
async def post_proposal_terms_by_cookie(
    request: Request,
    token: ProposalCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    _write_limit(request)
    raw = await _json_body(request, content_length)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    settings = request.app.state.settings
    return _json(
        await proposal_page.reaccept_terms(
            pool, keys, settings, _cookie_token(token), raw, origin=origin, **_evidence(request)
        )
    )


@router.post("/proposal/change-request")
async def post_proposal_change_request(
    request: Request,
    token: ProposalCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    _write_limit(request)
    raw = await _json_body(request, content_length)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    settings = request.app.state.settings
    return _json(await proposal_page.change_request(pool, keys, settings, _cookie_token(token), raw, origin=origin))


@router.post("/proposal/invoices/{invoice_id}/claim")
async def post_invoice_claim(
    request: Request, invoice_id: str, token: ProposalCookie = None, origin: Annotated[str | None, Header()] = None
) -> JSONResponse:
    _write_limit(request)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    booking = getattr(request.app.state, "booking_client", None)
    settings = request.app.state.settings
    link = _cookie_token(token)
    return _json(await proposal_page.claim_payment(pool, keys, settings, booking, link, invoice_id, origin=origin))


@router.post("/proposal/invoices/{invoice_id}/card")
async def post_invoice_card(
    request: Request, invoice_id: str, token: ProposalCookie = None, origin: Annotated[str | None, Header()] = None
) -> None:
    """Always refuses today (410 past the hold, else 409): the session is the BYO card provider's follow-up card."""
    _write_limit(request)
    pool = await request_pool(request)
    await proposal_page.card_payment(pool, request.app.state.settings, _cookie_token(token), invoice_id, origin=origin)


# ── portal (C-05 .. C-08, C-10 view) ─────────────────────────────────────────────────────────────


@router.get("/portal/{token}")
async def get_portal(
    request: Request, token: LinkToken, _: PublicRateLimit, session: SessionCookie = None
) -> JSONResponse:
    pool = await request_pool(request)
    return _json(await portal.home(pool, token, session, getattr(request.app.state, "keys", None)))


@router.post("/portal/{token}/code", status_code=202)
async def post_portal_code(
    request: Request,
    token: LinkToken,
    session: SessionCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    raw = await _json_body(request, content_length)
    async with _code_write(request, session, raw):
        purpose = raw.get("purpose", "sign_in") if isinstance(raw, dict) else None
        pool = await request_pool(request)
        keys = require_keys(request.app.state.keys)
        result = await portal.request_code(
            pool,
            keys,
            request.app.state.settings,
            token,
            purpose=purpose if isinstance(purpose, str) else "",
            session=session,
            origin=origin,
        )
        return _json(result, 202)


@router.post("/portal/{token}/session")
async def post_portal_session(
    request: Request,
    token: LinkToken,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    _write_limit(request)
    raw = await _json_body(request, content_length)
    return await _verify(request, token, raw, origin, _cookie_path(token))


async def _verify(request: Request, token: str, raw: Any, origin: str | None, path: str) -> JSONResponse:
    """The code check: 200 + the session cookie on ``path``; 200 ``pick`` (Fix E4, no cookie yet); 422 refused."""
    pool = await request_pool(request)
    body = raw if isinstance(raw, dict) else {}
    session, result = await portal.verify_code(
        pool,
        request.app.state.settings,
        token,
        body.get("code"),
        origin=origin,
        traveler_id=body.get("traveler_id"),
        keys=getattr(request.app.state, "keys", None),
    )
    if session is None:
        return _json(result, 200 if "pick" in result else 422)
    response = _json(result)
    response.set_cookie(
        portal.SESSION_COOKIE,
        session,
        max_age=portal.SESSION_HOURS * 3600,
        path=path,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


@router.post("/portal/{token}/sign-out")
async def post_portal_sign_out(
    request: Request, token: LinkToken, session: SessionCookie = None, origin: Annotated[str | None, Header()] = None
) -> JSONResponse:
    async with _session_write(request, session):
        portal.check_origin(request.app.state.settings, origin)
        pool = await request_pool(request)
        response = _json(await portal.sign_out(pool, token, session))
        response.delete_cookie(portal.SESSION_COOKIE, path=_cookie_path(token), secure=True, httponly=True,
                               samesite="strict")  # fmt: skip
        return response


@router.put("/portal/{token}/stage3")
async def put_portal_stage3(
    request: Request,
    token: LinkToken,
    session: SessionCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    async with _session_write(request, session):
        raw = await _json_body(request, content_length)
        pool = await request_pool(request)
        keys = require_keys(request.app.state.keys)
        result = await portal.submit_stage3(pool, keys, request.app.state.settings, token, session, raw, origin=origin)
        return _json(result)


@router.post("/portal/{token}/sign")
async def post_portal_sign(
    request: Request,
    token: LinkToken,
    session: SessionCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    async with _session_write(request, session):
        raw = await _json_body(request, content_length)
        pool = await request_pool(request)
        keys = require_keys(request.app.state.keys)
        signed, result = await portal.sign(
            pool,
            keys,
            request.app.state.settings,
            token,
            session,
            raw,
            origin=origin,
            sealer=getattr(request.app.state, "sealer", None),
            **_evidence(request),
        )
        return _json(result, 200 if signed else 422)


# ── the traveller portal by cookie (AA-953, CRM-24a; design v3 §2.9 rule 1 for the portal, Fix B11) ────────────────
# The web exchanges the portal link's token (/c/t/<token>) for the host-only cookie aa_portal_link (httpOnly, Secure,
# SameSite=Strict, Path /crm/public/v1/traveller) and strips it from the URL; the token stays valid until the link
# expires (a second device opening the link gets in too, after its own code). The session cookie of these routes is
# scoped to the same path.

TRAVELLER_PATH = "/crm/public/v1/traveller"
PORTAL_LINK_COOKIE = "aa_portal_link"
PortalLinkCookie = Annotated[str | None, Cookie(alias=PORTAL_LINK_COOKIE)]
# A passport scan: 7 MiB of bytes is ~9.4 MiB of base64 (app.documents.service.MAX_BYTES) plus the JSON around it.
SCAN_BODY_BYTES = 10 * 1024 * 1024


async def _scan_body(request: Request, content_length: int | None) -> Any:
    if content_length is not None and content_length > SCAN_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body_too_large")
    try:
        body = await read_capped(request, SCAN_BODY_BYTES)
    except BodyTooLarge:
        raise HTTPException(status_code=413, detail="body_too_large") from None
    try:
        return json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid_json") from None


@router.get("/traveller")
async def get_traveller(
    request: Request, _: PublicRateLimit, token: PortalLinkCookie = None, session: SessionCookie = None
) -> JSONResponse:
    pool = await request_pool(request)
    return _json(await portal.home(pool, _cookie_token(token), session, getattr(request.app.state, "keys", None)))


@router.post("/traveller/code", status_code=202)
async def post_traveller_code(
    request: Request,
    token: PortalLinkCookie = None,
    session: SessionCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    raw = await _json_body(request, content_length)
    async with _code_write(request, session, raw):
        purpose = raw.get("purpose", "sign_in") if isinstance(raw, dict) else None
        pool = await request_pool(request)
        keys = require_keys(request.app.state.keys)
        result = await portal.request_code(
            pool,
            keys,
            request.app.state.settings,
            _cookie_token(token),
            purpose=purpose if isinstance(purpose, str) else "",
            session=session,
            origin=origin,
        )
        return _json(result, 202)


@router.post("/traveller/session")
async def post_traveller_session(
    request: Request,
    token: PortalLinkCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    _write_limit(request)
    raw = await _json_body(request, content_length)
    return await _verify(request, _cookie_token(token), raw, origin, TRAVELLER_PATH)


@router.post("/traveller/switch")
async def post_traveller_switch(
    request: Request,
    token: PortalLinkCookie = None,
    session: SessionCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    async with _session_write(request, session):
        raw = await _json_body(request, content_length)
        pool = await request_pool(request)
        return _json(
            await portal.switch(pool, request.app.state.settings, _cookie_token(token), session, raw, origin=origin)
        )


@router.post("/traveller/sign-out")
async def post_traveller_sign_out(
    request: Request,
    token: PortalLinkCookie = None,
    session: SessionCookie = None,
    origin: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    async with _session_write(request, session):
        portal.check_origin(request.app.state.settings, origin)
        pool = await request_pool(request)
        response = _json(await portal.sign_out(pool, _cookie_token(token), session))
        response.delete_cookie(
            portal.SESSION_COOKIE, path=TRAVELLER_PATH, secure=True, httponly=True, samesite="strict"
        )
        return response


@router.put("/traveller/stage3")
async def put_traveller_stage3(
    request: Request,
    token: PortalLinkCookie = None,
    session: SessionCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    async with _session_write(request, session):
        raw = await _json_body(request, content_length)
        pool = await request_pool(request)
        keys = require_keys(request.app.state.keys)
        return _json(
            await portal.submit_stage3(
                pool, keys, request.app.state.settings, _cookie_token(token), session, raw, origin=origin
            )
        )


@router.post("/traveller/sign")
async def post_traveller_sign(
    request: Request,
    token: PortalLinkCookie = None,
    session: SessionCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    async with _session_write(request, session):
        raw = await _json_body(request, content_length)
        pool = await request_pool(request)
        keys = require_keys(request.app.state.keys)
        signed, result = await portal.sign(
            pool,
            keys,
            request.app.state.settings,
            _cookie_token(token),
            session,
            raw,
            origin=origin,
            sealer=getattr(request.app.state, "sealer", None),
            **_evidence(request),
        )
        return _json(result, 200 if signed else 422)


@router.put("/traveller/flights")
async def put_traveller_flights(
    request: Request,
    token: PortalLinkCookie = None,
    session: SessionCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    async with _session_write(request, session):
        raw = await _json_body(request, content_length)
        pool = await request_pool(request)
        keys = require_keys(request.app.state.keys)
        return _json(
            await portal.save_flights(
                pool, keys, request.app.state.settings, _cookie_token(token), session, raw, origin=origin
            )
        )


@router.post("/traveller/passport-scan")
async def post_traveller_passport_scan(
    request: Request,
    token: PortalLinkCookie = None,
    session: SessionCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    async with _session_write(request, session):
        raw = await _scan_body(request, content_length)
        pool = await request_pool(request)
        keys = require_keys(request.app.state.keys)
        store = require_store(request.app.state.documents)
        return _json(
            await portal.upload_passport_scan(
                pool, keys, store, request.app.state.settings, _cookie_token(token), session, raw, origin=origin
            )
        )


@router.get("/traveller/documents/{document_id}")
async def get_traveller_document(
    request: Request,
    document_id: str,
    _: PublicRateLimit,
    token: PortalLinkCookie = None,
    session: SessionCookie = None,
) -> JSONResponse:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    store = require_store(request.app.state.documents)
    return _json(await portal.open_document(pool, keys, store, _cookie_token(token), session, document_id))


@router.post("/traveller/companions")
async def post_traveller_companion(
    request: Request,
    token: PortalLinkCookie = None,
    session: SessionCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    async with _session_write(request, session):
        raw = await _json_body(request, content_length)
        pool = await request_pool(request)
        keys = require_keys(request.app.state.keys)
        return _json(
            await portal.invite_companion(
                pool, keys, request.app.state.settings, _cookie_token(token), session, raw, origin=origin
            )
        )
