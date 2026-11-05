"""Token-audience guards of the three route classes (root ADR 0006b decision 6; card CRM-05).

Every router of a class is built with :func:`class_router`, which puts the class guard on the router itself, so a
route added to it cannot skip the guard (``tests/unit/test_route_guard.py`` walks every app route and checks it).

The guard, in order:

1. no ``Authorization: Bearer`` -> **401** ``missing_bearer_token``;
2. the token does not verify against any configured ``(issuer, client)`` pair -> **401** ``invalid_token``; no key set
   ever loaded for its issuer -> **503** ``auth_unavailable``;
3. it verifies, but its ``(issuer kind, client kind)`` is not one the class accepts -> **403** ``wrong_audience``
   (a ``cis-admin`` or ``breakglass`` token on ``/crm/v1``, a partner token on ``/crm/ops/v1``, ...);
4. the class needs a group and the token holds none of them -> **403** ``missing_group``;
5. AA-1019 (root ADR 0006a A10c): the Dev-only CRM staff E2E identity outside ``dev`` / ``test`` / ``local`` ->
   **403** ``e2e_read_only`` (the token cannot exist elsewhere: this is the second lock); AA-1035 (A10d) the Dev-only
   CIS customer_success E2E identity likewise;
6. AA-969 (CRM-37a): on the agency class only, more than ``RATE_LIMIT_MEMBER_PER_MINUTE`` requests in a minute by
   one member for one agency (``iss`` + ``sub`` + the canonical ``X-AA-Tenant-Id`` uuid, ``app.state.member_limiter``)
   -> **429** ``rate_limited``. The agency is part of the key only once this task verified the caller's membership of
   it (:meth:`app.tenant_gate.TenantGate.is_member`, set by ``tenant_tx()``); every other header value (an agency the
   caller is not, or not yet, known to belong to, a fresh random uuid, junk, none) shares the caller's one
   ``unverified`` bucket (review round 3: a fresh uuid per request opened a fresh count and could evict every real
   member's counter from the limiter's table). So one identity has at most (its agencies + 1) counts.

Then the route gets a :class:`Caller` (no e-mail or name: ids only, plus the ``e2e_read_only`` flag of the Dev-only
E2E CIS admin, :mod:`app.auth.e2e_staff`). Tenant membership is not checked here: the agency
routes open :func:`app.tenancy.tenant_tx`, which checks it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import tenant_gate
from app.auth import e2e_staff
from app.auth.identity_config import WORKFORCE, Identity, RouteClass
from app.auth.identity_verifier import AuthUnavailable, InvalidToken, MultiIssuerVerifier
from app.settings import DEV_ONLY_ENVIRONMENTS

log = logging.getLogger(__name__)

# The longest spelling uuid.UUID() accepts ("urn:uuid:" + 36) with room for braces; longer is never an agency id.
MAX_TENANT_HEADER = 64
# The member ceiling's bucket for every X-AA-Tenant-Id this task has not seen the caller verified for.
UNVERIFIED = "unverified"
bearer = HTTPBearer(auto_error=False, scheme_name="CognitoAccessToken", description="Cognito access token")


@dataclass(frozen=True)
class Caller:
    """A verified caller allowed into one route class. ``iss`` + ``sub`` are the identity (never the e-mail)."""

    route_class: str
    issuer_kind: str
    client_kind: str
    iss: str
    sub: str
    groups: tuple[str, ...]
    # The Dev-only E2E CIS admin (root ADR 0006a A10b; :mod:`app.auth.e2e_staff`): a WORKFORCE token whose ``email``
    # claim is that address. A boolean only: the address itself is not kept.
    e2e_read_only: bool = False
    # AA-1019 (A10c; :mod:`app.auth.e2e_staff`): the Dev-only CRM staff E2E identity. Every staff context refuses an
    # agency that is not synthetic for it (403 ``e2e_synthetic_only``, :mod:`app.tenancy`).
    e2e_synthetic_only: bool = False
    # AA-1035 (A10d; :mod:`app.auth.e2e_staff`): the Dev-only CIS customer_success E2E identity. No write at all:
    # require_group and the studio invitation relay refuse it (403 ``e2e_read_only``). Deliberately not
    # ``e2e_read_only``, which opens the A10b-1 invitation path (:func:`app.members.studio.check_e2e_invitation`).
    e2e_no_write: bool = False

    def in_group(self, group: str) -> bool:
        return group in self.groups

    def actor(self) -> str:
        """Audit actor for rows written on this caller's behalf before a membership is known."""
        return f"{self.issuer_kind}:{self.sub}"


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def guard_for(route_class: RouteClass) -> Callable[..., Awaitable[Caller]]:
    """The FastAPI dependency of one route class. Stores the caller on ``request.state.caller`` too."""

    async def guard(
        request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
    ) -> Caller:
        if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
            raise _unauthorized("missing_bearer_token")
        verifier: MultiIssuerVerifier | None = request.app.state.verifier
        identity: Identity = request.app.state.identity
        if verifier is None:
            raise _unauthorized("invalid_token")
        try:
            principal = await verifier.verify(credentials.credentials)
        except InvalidToken as exc:
            log.info("route_guard_rejected class=%s reason=%s", route_class.name, exc)
            raise _unauthorized("invalid_token") from exc
        except AuthUnavailable as exc:
            log.warning("route_guard_auth_unavailable class=%s", route_class.name)
            raise HTTPException(status_code=503, detail="auth_unavailable") from exc
        issuer_kind = identity.issuer_kind(principal.iss)
        client_kind = identity.client_kind(issuer_kind or "", principal.client_id)
        if issuer_kind is None or client_kind is None or (issuer_kind, client_kind) not in route_class.accepts:
            log.warning(
                "route_guard_wrong_audience class=%s issuer=%s client=%s", route_class.name, issuer_kind, client_kind
            )
            raise HTTPException(status_code=403, detail="wrong_audience")
        if route_class.groups_any and not route_class.groups_any.intersection(principal.groups):
            log.warning("route_guard_missing_group class=%s client=%s", route_class.name, client_kind)
            raise HTTPException(status_code=403, detail="missing_group")
        e2e_crm_staff = issuer_kind == WORKFORCE and e2e_staff.is_e2e_crm_staff(principal.email)
        if e2e_crm_staff and _environment(request) not in DEV_ONLY_ENVIRONMENTS:
            log.warning("route_guard_e2e_crm_staff_refused class=%s", route_class.name)
            raise HTTPException(status_code=403, detail=e2e_staff.E2E_READ_ONLY)
        e2e_cis_cs = issuer_kind == WORKFORCE and e2e_staff.is_e2e_cis_cs(principal.email)
        if e2e_cis_cs and _environment(request) not in DEV_ONLY_ENVIRONMENTS:
            log.warning("route_guard_e2e_cis_cs_refused class=%s", route_class.name)
            raise HTTPException(status_code=403, detail=e2e_staff.E2E_READ_ONLY)
        caller = Caller(
            route_class=route_class.name,
            issuer_kind=issuer_kind,
            client_kind=client_kind,
            iss=principal.iss,
            sub=principal.sub,
            groups=principal.groups,
            e2e_read_only=issuer_kind == WORKFORCE and e2e_staff.is_e2e_cis_admin(principal.email),
            e2e_synthetic_only=e2e_crm_staff,
            e2e_no_write=e2e_cis_cs,
        )
        if route_class.name == "agency":
            _member_rate_limit(request, caller)
        request.state.caller = caller
        return caller

    guard.route_class = route_class  # type: ignore[attr-defined] — read by the route-coverage test
    return guard


def _member_rate_limit(request: Request, caller: Caller) -> None:
    """AA-969: one member's requests for one agency per minute (``app.state.member_limiter``; absent = off)."""
    limiter = getattr(request.app.state, "member_limiter", None)
    if limiter is None:
        return
    bucket = _tenant_bucket(caller, request.headers.get("x-aa-tenant-id"))
    key = f"{caller.iss}|{caller.sub}|{bucket}"
    if not limiter.allow(key):
        log.warning("route_guard_member_rate_limited class=%s", caller.route_class)
        raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": str(int(limiter.window_s))})


def _tenant_bucket(caller: Caller, header: str | None) -> str:
    """The agency part of the member ceiling's key: the canonical id (``str(uuid.UUID(...))``, so every spelling
    ``uuid.UUID`` accepts shares one count) **only** when this task already verified the caller's membership of that
    agency (:meth:`app.tenant_gate.TenantGate.is_member`); anything else shares the bucket :data:`UNVERIFIED`.

    Review round 2 keyed every valid uuid, so a token holder sending a fresh random uuid per request was never limited
    (each request still cost a gate slot and two queries before 403 ``not_a_member``), and more than 10 000 of them a
    minute evicted every real member's counter. The membership mark decides no access (``tenant_tx()`` checks the
    membership on every request); it only picks the bucket."""
    if header and len(header) <= MAX_TENANT_HEADER:
        try:
            tenant_id = uuid.UUID(header)
        except ValueError:
            return UNVERIFIED
        if tenant_gate.GATE.is_member(caller.iss, caller.sub, tenant_id):
            return str(tenant_id)
    return UNVERIFIED


def _environment(request: Request) -> str:
    """The process ENVIRONMENT (``app.state.settings``); unknown = "" (refused by every environment check)."""
    settings = getattr(request.app.state, "settings", None)
    value = getattr(settings, "environment", "")
    return value if isinstance(value, str) else ""


def class_router(route_class: RouteClass, **kwargs: Any) -> APIRouter:
    """An APIRouter under the class prefix whose every route runs the class guard first.

    AGENCY routers also use :class:`app.margin.MarginFilteredRoute` (AA-939, design §4.6): their JSON responses lose
    every margin-bearing key unless the member's role may see margins. OPS routers use
    :class:`app.ops.serializer.OpsAllowListRoute` (AA-941, card CRM-13a R): every key must be on the L1 allow-list."""
    if route_class.name == "agency":
        from app.margin import MarginFilteredRoute  # noqa: PLC0415 — keeps the auth module free of app imports

        kwargs.setdefault("route_class", MarginFilteredRoute)
    elif route_class.name == "ops":
        from app.ops.serializer import OpsAllowListRoute  # noqa: PLC0415 — same reason

        kwargs.setdefault("route_class", OpsAllowListRoute)
    return APIRouter(prefix=route_class.prefix, dependencies=[Depends(guard_for(route_class))], **kwargs)


def current_caller(request: Request) -> Caller:
    """The caller the router guard stored (FastAPI runs router dependencies before the route's own)."""
    caller = getattr(request.state, "caller", None)
    if not isinstance(caller, Caller):  # a route mounted outside class_router: refuse rather than guess
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    return caller


def require_group(group: str) -> Callable[[Caller], Caller]:
    """A route-level dependency for a write that needs one specific group (e.g. ``crm-platform-admin`` on ops).

    The Dev-only E2E CIS admin is refused with 403 ``e2e_read_only`` whatever groups its token holds (root ADR 0006a
    A10b: read-only; the trigger never issues it a write group, this is the second lock), and so is the Dev-only CIS
    customer_success E2E identity (A10d, ``e2e_no_write``)."""

    def dependency(caller: Annotated[Caller, Depends(current_caller)]) -> Caller:
        if caller.e2e_read_only or caller.e2e_no_write:
            log.warning("route_guard_e2e_read_only class=%s group=%s", caller.route_class, group)
            raise HTTPException(status_code=403, detail=e2e_staff.E2E_READ_ONLY)
        if not caller.in_group(group):
            raise HTTPException(status_code=403, detail="missing_group")
        return caller

    return dependency
