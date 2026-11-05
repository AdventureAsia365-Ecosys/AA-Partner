"""The tenant scope of an AA desk staff member on ``/crm/ops/v1`` (AA-1035 R2 close-out; root ADR 0019 d4, "the
crm.ops.v1 relay forwards the staff member's scope. admin bypasses scope").

Who is scoped. The OPS class admits two more WORKFORCE groups (``cis-customer_success``, ``cis-support``: the CIS desk
roles, root ADR 0019 d3) next to ``cis-admin`` and ``crm-platform-admin``. A caller in ``cis-admin`` or
``crm-platform-admin`` keeps every agency (:data:`ALL`). Any other OPS caller is a desk caller: its role is the
highest desk group it holds (``customer_success`` before ``support``, the CIS group precedence 16 < 26).

Where the scope comes from. CIS, never the request: the staff member's own access token reaches this API (the CIS
admin relays it), so a scope sent with the request could be written by the staff member. The CRM asks CIS
``tenant.membership.v1`` 1.3 ``getStaffTenantScope`` (M2M ``cis.tenant/read``, the membership feed's own client and
scope: no new grant) for the caller's ``(iss, sub)`` and desk role, and serves that caller only those tenant ids.
The answer is cached per ``(iss, sub, role)`` for :data:`CACHE_TTL_S` (an assignment removed in CIS stops working
here within that time; a cache miss costs one CIS call). Fail closed: CIS unreachable, a token failure or an answer
that is not the contract shape is 503 ``cis_unavailable`` for this caller only; an unknown identity is scope ``none``
(no agency).

What a desk caller may do (``app.routers.ops``): the per-agency reads of its scope (403 ``tenant_outside_scope``
otherwise, whether or not the agency exists), the agency list filtered to its scope, ``/console`` (``can_write``
false, its ``scope``), and the studio invitation relay for a tenant of its scope (support: to the kept address only,
:func:`may_readdress`). Never a platform-wide read (job summary, LLM usage / calls / app profile, data coverage,
insights: 403 ``missing_group``) and never a ``crm-platform-admin`` write (unchanged). Nothing is logged but ids and
codes (never the identity's ``sub``).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request

from app.auth.identity_config import (
    GROUP_CIS_ADMIN,
    GROUP_CIS_CUSTOMER_SUCCESS,
    GROUP_CIS_SUPPORT,
    GROUP_CRM_PLATFORM_ADMIN,
)
from app.auth.route_guard import Caller
from app.cis.http import HttpError
from app.cis.m2m import TokenError

log = logging.getLogger(__name__)

SCOPE_TENANT_READ = "cis.tenant/read"
PATH = "/tenant/membership/v1/staff-scope"
CACHE_TTL_S = 30.0
CACHE_MAX = 512
MAX_TENANT_IDS = 10_000

# Highest first: the CIS WORKFORCE group precedence (cis-customer_success 16, cis-support 26).
DESK_GROUPS: tuple[tuple[str, str], ...] = (
    (GROUP_CIS_CUSTOMER_SUCCESS, "customer_success"),
    (GROUP_CIS_SUPPORT, "support"),
)
FULL_GROUPS = frozenset({GROUP_CIS_ADMIN, GROUP_CRM_PLATFORM_ADMIN})
SCOPE_KINDS = frozenset({"book", "book_pool", "none"})

OUTSIDE = "tenant_outside_scope"
UNAVAILABLE = "cis_unavailable"
READDRESS = "readdress_not_allowed"


@dataclass(frozen=True)
class StaffScope:
    """``kind`` all (cis-admin / crm-platform-admin), book, book_pool or none; ``tenants`` a scoped caller's ids."""

    kind: str
    desk_role: str | None = None
    tenants: frozenset[uuid.UUID] = field(default_factory=frozenset)

    @property
    def all(self) -> bool:
        return self.kind == "all"

    def allows(self, tenant_id: uuid.UUID) -> bool:
        return self.all or tenant_id in self.tenants


ALL = StaffScope("all")


def desk_role(caller: Caller) -> str | None:
    """None for a full caller (cis-admin or crm-platform-admin); else the caller's highest desk role (the OPS class
    admits no other group, so a caller here without a full group holds one)."""
    if FULL_GROUPS.intersection(caller.groups):
        return None
    for group, role in DESK_GROUPS:
        if caller.in_group(group):
            return role
    return None


def may_readdress(caller: Caller) -> bool:
    """May this caller send a studio invitation to an address it names? Every caller but a support-only desk caller
    (support sends an invitation again to the address the CRM kept, AA-1035; CIS refuses it the same way)."""
    return desk_role(caller) != "support"


class ScopeCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._rows: dict[tuple[str, str, str], tuple[StaffScope, float]] = {}

    def get(self, key: tuple[str, str, str]) -> StaffScope | None:
        hit = self._rows.get(key)
        if hit is None or hit[1] <= self._clock():
            self._rows.pop(key, None)
            return None
        return hit[0]

    def put(self, key: tuple[str, str, str], scope: StaffScope) -> None:
        if len(self._rows) >= CACHE_MAX:
            self._rows.clear()  # a staff pool is tens of people: a full table means a flood; start over
        self._rows[key] = (scope, self._clock() + CACHE_TTL_S)

    def clear(self) -> None:
        self._rows.clear()


CACHE = ScopeCache()


def _unavailable(code: str) -> HTTPException:
    log.warning("ops_staff_scope_unavailable code=%s", code)
    return HTTPException(status_code=503, detail=UNAVAILABLE)


def parse_scope(body: bytes, role: str) -> StaffScope:
    """The contract body (StaffTenantScope), or ValueError for anything else."""
    data: Any = json.loads(body)
    if not isinstance(data, dict) or data.get("desk_role") != role or data.get("scope") not in SCOPE_KINDS:
        raise ValueError("not a StaffTenantScope")
    ids = data.get("tenant_ids")
    if not isinstance(ids, list) or len(ids) > MAX_TENANT_IDS or not all(isinstance(i, str) for i in ids):
        raise ValueError("tenant_ids")
    tenants = frozenset(uuid.UUID(i) for i in ids)
    if data["scope"] == "none" and tenants:
        raise ValueError("none with tenants")
    return StaffScope(data["scope"], role, tenants)


async def fetch_scope(config: Any, caller: Caller, role: str) -> StaffScope:
    """Ask CIS (``config``: base_url, tokens, transport, timeout_s, the studio activation config: same CIS origin, same
    M2M client). Raises 503 ``cis_unavailable`` when CIS cannot answer."""
    if config is None:
        raise _unavailable("cis_not_configured")
    try:
        token = await config.tokens.token(SCOPE_TENANT_READ)
    except TokenError as exc:
        raise _unavailable(exc.error_class) from exc
    query = urllib.parse.urlencode({"iss": caller.iss, "sub": caller.sub, "desk_role": role})
    url = f"{config.base_url}{PATH}?{query}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        resp = await config.transport("GET", url, headers, None, config.timeout_s)
    except HttpError as exc:
        raise _unavailable(f"cis_{exc.error_class}"[:40]) from exc
    if resp.status == 401:
        config.tokens.invalidate(SCOPE_TENANT_READ)
    if resp.status != 200:
        raise _unavailable(f"cis_status_{resp.status}")
    try:
        return parse_scope(resp.body, role)
    except (ValueError, TypeError) as exc:
        raise _unavailable("cis_bad_body") from exc


async def scope_of(request: Request, caller: Caller) -> StaffScope:
    """The caller's scope (cached per identity and role; stored on ``request.state.staff_scope`` for the request)."""
    cached = getattr(request.state, "staff_scope", None)
    if isinstance(cached, StaffScope):
        return cached
    role = desk_role(caller)
    if role is None:
        scope = ALL
    else:
        key = (caller.iss, caller.sub, role)
        scope = CACHE.get(key)
        if scope is None:
            scope = await fetch_scope(getattr(request.app.state, "studio_activation", None), caller, role)
            CACHE.put(key, scope)
            log.info("ops_staff_scope_loaded role=%s scope=%s tenants=%s", role, scope.kind, len(scope.tenants))
    request.state.staff_scope = scope
    return scope


async def require_tenant(request: Request, caller: Caller, tenant_id: uuid.UUID) -> StaffScope:
    """403 ``tenant_outside_scope`` when a desk caller's scope does not hold ``tenant_id`` (exists or not)."""
    scope = await scope_of(request, caller)
    if not scope.allows(tenant_id):
        log.warning("ops_staff_scope_refused role=%s scope=%s", scope.desk_role, scope.kind)
        raise HTTPException(status_code=403, detail=OUTSIDE)
    return scope


def require_full(caller: Caller) -> None:
    """A platform-wide read (every agency at once): never a desk caller (403 ``missing_group``, as the class guard
    answers a caller without an admitted group)."""
    if desk_role(caller) is not None:
        raise HTTPException(status_code=403, detail="missing_group")
