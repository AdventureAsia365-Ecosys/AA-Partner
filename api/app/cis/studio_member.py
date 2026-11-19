"""The caller's Content studio membership in one agency, as CIS holds it (AA-1139 fix pass).

Why. The Trip planner module has a studio page for an agency WITHOUT the CRM add-on (it has no CRM agency: CIS creates
one only for a live ``crm`` subscription), and the TripPlanner half of that page (usage, leads, the webhook signing key)
can only be reached through this API, which holds the M2M client TripPlanner trusts. Such a person has no
``crm.member`` row, so ``tenant_tx()`` cannot authorise them; CIS, the directory of agencies and their members, can.

How. The person's own partner-pool access token reaches this API (the studio BFF forwards it with the studio's tenant
as ``X-AA-Tenant-Id``); the CRM asks CIS ``tenant.membership.v1`` ``GET /memberships?iss=&sub=`` (M2M
``cis.tenant/read``, the membership feed's own client and scope: no new grant) for the caller's ``(iss, sub)`` and keeps
the ACTIVE membership of that tenant only. Never from the request: neither the role nor the membership id. Not cached:
a removal or a role change in CIS takes effect on the next call (the page makes at most a few).

Fail closed: CIS unreachable, a token failure or an answer that is not the contract shape or is about another identity
is 503 ``cis_unavailable``; no active membership in that tenant is ``None`` (the route answers 403). Nothing is logged
but codes (never the identity's ``sub``).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from app.cis.http import HttpError
from app.cis.m2m import TokenError

log = logging.getLogger(__name__)

SCOPE_TENANT_READ = "cis.tenant/read"
PATH = "/tenant/membership/v1/memberships"
ROLES = frozenset({"owner", "editor", "viewer"})
MAX_MEMBERSHIPS = 1000  # technical-constant: a bound on one identity's membership list in a CIS answer (body shape)
UNAVAILABLE = "cis_unavailable"


@dataclass(frozen=True)
class StudioMember:
    membership_id: uuid.UUID
    role: str  # owner | editor | viewer (the Content studio role, never a CRM role)


def _unavailable(code: str) -> HTTPException:
    log.warning("crm_studio_member_unavailable code=%s", code)
    return HTTPException(status_code=503, detail=UNAVAILABLE)


def parse_member(body: bytes, iss: str, sub: str, tenant_id: uuid.UUID) -> StudioMember | None:
    """The active membership of ``tenant_id`` in the contract body (MembershipIdentityMemberships) for exactly the
    identity asked; ValueError for a body of another shape or another identity."""
    data: Any = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    identity = data.get("identity")
    if not isinstance(identity, dict) or identity.get("iss") != iss or identity.get("sub") != sub:
        raise ValueError("identity")
    rows = data.get("memberships")
    if not isinstance(rows, list) or len(rows) > MAX_MEMBERSHIPS:
        raise ValueError("memberships")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("membership")
        if row.get("tenant_id") != str(tenant_id) or row.get("status") != "active":
            continue
        role = row.get("content_studio_role")
        if role not in ROLES:
            raise ValueError("role")
        return StudioMember(uuid.UUID(str(row.get("membership_id"))), role)
    return None


async def lookup(config: Any, iss: str, sub: str, tenant_id: uuid.UUID) -> StudioMember | None:
    """Ask CIS (``config``: the studio activation config, base_url / tokens / transport / timeout_s). 503 when CIS
    cannot answer; None when the identity holds no active membership in ``tenant_id``."""
    if config is None:
        raise _unavailable("cis_not_configured")
    try:
        token = await config.tokens.token(SCOPE_TENANT_READ)
    except TokenError as exc:
        raise _unavailable(exc.error_class) from exc
    url = f"{config.base_url}{PATH}?{urllib.parse.urlencode({'iss': iss, 'sub': sub})}"
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
        return parse_member(resp.body, iss, sub, tenant_id)
    except (ValueError, TypeError) as exc:
        raise _unavailable("cis_bad_body") from exc
