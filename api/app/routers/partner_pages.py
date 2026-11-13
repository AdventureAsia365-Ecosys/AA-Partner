"""``/crm/public/v1`` sales partner pages (AA-1071, crm-056; S275 study item crm-29). No token header: the partner's
signed link (43 characters, SHA-256 stored); served on the client host. Service: :mod:`app.partners.public`.

- ``GET  /crm/public/v1/r/{token}``: the inquiry form of the partner's intake link (the public form's answer).
- ``POST /crm/public/v1/r/{token}/submissions``: one inquiry, stamped with the partner: 202 ``{"status": "received"}``
  (stored, a repeat or a honeypot hit alike); 422 ``consent_required`` / ``invalid_submission``; 403
  ``origin_not_allowed``; 429 per address (the intake limiter, before anything is read) and per form; 413 over 32 KiB.
- The view, by the host-only cookie ``aa_partner_link`` (the web's ``/c/a/<token>`` landing sets it, Path
  ``/crm/public/v1/partner``) and the session cookie ``aa_partner_s`` on the same path:
  ``GET /partner`` (the agency name; signed in: the partner's trips and commissions), ``POST /partner/code`` (202, the
  sign-in code by e-mail), ``POST /partner/session`` (200 + the session cookie; 422 a refused code),
  ``POST /partner/sign-out``. Writes take the intake limiter per address (the anonymous ceiling).

Unknown, expired, revoked or foreign link, or no cookie: 404 ``link_not_found``.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Cookie, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.db import request_pool
from app.errors import NotFound
from app.intake import public as intake_public
from app.partners import public as partner
from app.ratelimit import PublicRateLimit, client_address
from app.routers.public import MAX_BODY_BYTES, LinkToken, _json_body, _limit
from app.vault.provider import require_keys

router = APIRouter(prefix="/crm/public/v1", tags=["partner-pages"])

HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
LinkCookie = Annotated[str | None, Cookie(alias=partner.LINK_COOKIE)]
SessionCookie = Annotated[str | None, Cookie(alias=partner.SESSION_COOKIE)]
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _json(body: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, headers=HEADERS)


def _write_limit(request: Request) -> None:
    settings = request.app.state.settings
    _limit(request, "intake_limiter", client_address(request, settings.rate_limit_xff_hops))


def _cookie_token(token: str | None) -> str:
    if not token or not _TOKEN.match(token):
        raise NotFound("link_not_found")
    return token


@router.get("/r/{token}")
async def get_intake_form(request: Request, token: LinkToken, _: PublicRateLimit) -> JSONResponse:
    pool = await request_pool(request)
    return _json(await partner.read_form(pool, token))


@router.post("/r/{token}/submissions", status_code=202)
async def post_intake(
    request: Request,
    token: LinkToken,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    _write_limit(request)
    if content_length is not None and content_length > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body_too_large")
    raw = await _json_body(request, content_length)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    form_limiter = request.app.state.intake_form_limiter
    await partner.submit(
        pool,
        keys,
        request.app.state.settings,
        token,
        raw,
        origin=origin,
        form_quota=lambda form_id, limit=None: form_limiter.allow(intake_public.form_bucket(form_id), limit=limit),
        quota_window_s=int(form_limiter.window_s),
    )
    return _json({"status": "received"}, 202)


@router.get("/partner")
async def get_view(
    request: Request, _: PublicRateLimit, token: LinkCookie = None, session: SessionCookie = None
) -> JSONResponse:
    pool = await request_pool(request)
    return _json(await partner.home(pool, getattr(request.app.state, "keys", None), _cookie_token(token), session))


@router.post("/partner/code", status_code=202)
async def post_code(
    request: Request, token: LinkCookie = None, origin: Annotated[str | None, Header()] = None
) -> JSONResponse:
    _write_limit(request)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    result = await partner.request_code(pool, keys, request.app.state.settings, _cookie_token(token), origin=origin)
    return _json(result, 202)


@router.post("/partner/session")
async def post_session(
    request: Request,
    token: LinkCookie = None,
    origin: Annotated[str | None, Header()] = None,
    content_length: Annotated[int | None, Header()] = None,
) -> JSONResponse:
    _write_limit(request)
    raw = await _json_body(request, content_length)
    pool = await request_pool(request)
    body = raw if isinstance(raw, dict) else {}
    session, result = await partner.verify_code(
        pool, request.app.state.settings, _cookie_token(token), body.get("code"), origin=origin
    )
    if session is None:
        return _json(result, 422)
    response = _json(result)
    response.set_cookie(
        partner.SESSION_COOKIE,
        session,
        max_age=partner.SESSION_HOURS * 3600,
        path=partner.VIEW_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


@router.post("/partner/sign-out")
async def post_sign_out(
    request: Request,
    token: LinkCookie = None,
    session: SessionCookie = None,
    origin: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    _write_limit(request)
    partner.check_origin(request.app.state.settings, origin)
    pool = await request_pool(request)
    response = _json(await partner.sign_out(pool, _cookie_token(token), session))
    response.delete_cookie(
        partner.SESSION_COOKIE, path=partner.VIEW_PATH, secure=True, httponly=True, samesite="strict"
    )
    return response
