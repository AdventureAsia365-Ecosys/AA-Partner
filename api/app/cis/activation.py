"""The CRM's call to CIS ``tenant.membership.v1`` 1.1 ``activateInvitation`` (AA-979 P2b, root ADR 0010a).

``POST <CIS_API_URL>/tenant/membership/v1/invitations/{cis_invitation_id}/activate`` with ``{"identity": {iss, sub}}``
and an M2M token of scope ``cis.tenant/activate`` (asked for alone, like every M2M call here). CIS binds the person's
partner-pool identity to the invited Content studio membership; CIS decides the Content studio role, this call carries
only the verified identity.

Outcomes (:class:`Outcome`):

- ``activated``  200 (CIS answers ``changed: false`` for the same identity again: a retry after a lost answer);
- ``retry``      a transport failure, a 5xx, a 429, a 401 (the cached token is dropped first) or a 403 (the scope is not
                 granted yet: Infra not applied) — the job tries again with its backoff;
- ``refused``    404 (CIS has no such invitation) or 409 (another identity, a removed membership, a second live
                 membership): permanent, the code goes into ``activation_error``.

Nothing from the body is logged or kept beyond the error code.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from app.cis.http import DEFAULT_TIMEOUT_S, HttpError, Transport, urllib_transport
from app.cis.m2m import TokenError, TokenProvider

SCOPE_TENANT_ACTIVATE = "cis.tenant/activate"
PATH = "/tenant/membership/v1/invitations/{invitation_id}/activate"
REASONS = frozenset({"identity_mismatch", "membership_removed", "already_member"})


@dataclass(frozen=True)
class ActivationConfig:
    base_url: str
    tokens: TokenProvider
    transport: Transport = urllib_transport
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class Outcome:
    kind: str  # activated | retry | refused
    code: str  # an error code (^[a-z][a-z0-9_]{0,39}$) or "ok"


async def activate(config: ActivationConfig, cis_invitation_id: uuid.UUID, iss: str, sub: str) -> Outcome:
    try:
        token = await config.tokens.token(SCOPE_TENANT_ACTIVATE)
    except TokenError as exc:
        return Outcome("retry", exc.error_class)
    url = f"{config.base_url}{PATH.format(invitation_id=cis_invitation_id)}"
    body = json.dumps({"identity": {"iss": iss, "sub": sub}}).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}
    try:
        resp = await config.transport("POST", url, headers, body, config.timeout_s)
    except HttpError as exc:
        return Outcome("retry", f"cis_{exc.error_class}"[:40])
    if resp.status == 200:
        return Outcome("activated", "ok")
    if resp.status == 401:
        config.tokens.invalidate(SCOPE_TENANT_ACTIVATE)
        return Outcome("retry", "cis_unauthorized")
    if resp.status == 403:
        return Outcome("retry", "cis_forbidden")
    if resp.status == 404:
        return Outcome("refused", "cis_invitation_not_found")
    if resp.status == 409:
        reason = _reason(resp.body)
        return Outcome("refused", f"cis_{reason}" if reason else "cis_conflict")
    if resp.status == 400:
        return Outcome("refused", "cis_invalid_request")
    return Outcome("retry", f"cis_http_{resp.status}"[:40])


def _reason(body: bytes) -> str | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if reason in REASONS else None
