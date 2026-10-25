"""``/crm/public/v1`` client pages of CRM-18 (AA-946): the proposal accept page and the traveller portal base. No
token: the signed link in the path only (43 characters, SHA-256 stored), plus, in the portal, the session cookie a
verified e-mail code set. Served on the client host (the ALB's ``/crm/public/*`` rule), so the browser calls its own
origin (no CORS) and the per-address limits key on the real visitor.

- ``GET  /crm/public/v1/p/{token}``: the proposal (:func:`app.proposals.public.read`).
- ``POST /crm/public/v1/p/{token}/accept``: accept with the typed full name, the terms tick and the price hash
  (:func:`app.proposals.public.accept`); 409 ``price_changed`` / ``terms_changed``.
- ``GET  /crm/public/v1/portal/{token}``: the portal home (``signed_in: false`` without a session).
- ``POST /crm/public/v1/portal/{token}/code``: e-mail a code (``{"purpose": "sign_in" | "sign"}``).
- ``POST /crm/public/v1/portal/{token}/session``: check a sign-in code; 200 sets the session cookie
  (``aa_portal_s``: httpOnly, Secure, SameSite=Strict, Path = this portal's own path, 12 hours); a wrong code is 422
  ``code_invalid`` with ``attempts_left`` (the counter commits), an old one 422 ``code_expired``.
- ``POST /crm/public/v1/portal/{token}/sign-out``: ends the session and clears the cookie.
- ``PUT  /crm/public/v1/portal/{token}/stage3``: the traveller's Stage 3 details (write-only); **403
  ``stage3_locked`` before the deposit** (card R), 403 ``portal_sign_in_required`` without a session.
- ``POST /crm/public/v1/portal/{token}/sign``: sign the waiver (e-sign v0) with a fresh signing code.

Every write: the Origin check of the client host, the per-address limit (``RATE_LIMIT_INTAKE_PER_MINUTE``) before
anything is read, a 32 KiB body cap. 404 ``link_not_found`` for any unusable link. Answers carry ``no-store`` and
``Referrer-Policy: no-referrer`` (the token is in the path).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Header, Request
from fastapi.responses import JSONResponse

from app.db import request_pool
from app.portal import public as portal
from app.proposals import public as proposal_page
from app.ratelimit import PublicRateLimit, client_address
from app.routers.public import LinkToken, _json_body, _limit
from app.vault.provider import require_keys

router = APIRouter(prefix="/crm/public/v1", tags=["client-pages"])

HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
SessionCookie = Annotated[str | None, Cookie(alias=portal.SESSION_COOKIE)]


def _write_limit(request: Request) -> None:
    settings = request.app.state.settings
    _limit(request, "intake_limiter", client_address(request, settings.rate_limit_xff_hops))


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
    result = await proposal_page.accept(pool, keys, request.app.state.settings, token, raw, origin=origin)
    return _json(result)


# ── portal (C-05 .. C-08, C-10 view) ─────────────────────────────────────────────────────────────


@router.get("/portal/{token}")
async def get_portal(
    request: Request, token: LinkToken, _: PublicRateLimit, session: SessionCookie = None
) -> JSONResponse:
    pool = await request_pool(request)
    return _json(await portal.home(pool, token, session))


@router.post("/portal/{token}/code", status_code=202)
async def post_portal_code(
    request: Request,
    token: LinkToken,
    session: SessionCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    _write_limit(request)
    raw = await _json_body(request, content_length)
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
    pool = await request_pool(request)
    code = raw.get("code") if isinstance(raw, dict) else None
    session, result = await portal.verify_code(pool, request.app.state.settings, token, code, origin=origin)
    if session is None:
        return _json(result, 422)
    response = _json(result)
    response.set_cookie(
        portal.SESSION_COOKIE,
        session,
        max_age=portal.SESSION_HOURS * 3600,
        path=_cookie_path(token),
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


@router.post("/portal/{token}/sign-out")
async def post_portal_sign_out(
    request: Request, token: LinkToken, session: SessionCookie = None, origin: Annotated[str | None, Header()] = None
) -> JSONResponse:
    _write_limit(request)
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
    _write_limit(request)
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
    _write_limit(request)
    raw = await _json_body(request, content_length)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    signed, result = await portal.sign(pool, keys, request.app.state.settings, token, session, raw, origin=origin)
    return _json(result, 200 if signed else 422)
