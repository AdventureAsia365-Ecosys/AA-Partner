"""What CIS says about one Content studio invitation (AA-979, root ADR 0006a A10b-1): ``tenant.membership.v1``
``getTenantMembers`` with an M2M token of scope ``cis.tenant/read`` (the membership feed's scope; no new grant).

Used only for the Dev-only E2E CIS admin's studio-invitation relay (:func:`app.members.studio.check_e2e_invitation`):
the request body comes from the caller, CIS's own record of the invitation does not, so the role and the status are
read from CIS, never from the body. Outcomes (:class:`Lookup`):

- ``found``    200 and a membership of that tenant with that ``invitation_id`` (its ``content_studio_role``,
               ``status``);
- ``missing``  200 without it, or 404 (CIS has no such tenant);
- ``error``    a transport failure, a token failure, any other status or a body that is not the contract shape.

Nothing from the body is logged or kept.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.cis.http import HttpError
from app.cis.m2m import TokenError

SCOPE_TENANT_READ = "cis.tenant/read"
PATH = "/tenant/membership/v1/tenants/{tenant_id}/members"


@dataclass(frozen=True)
class Lookup:
    kind: str  # found | missing | error
    role: str | None = None
    status: str | None = None
    code: str = "ok"


async def invitation(config: Any, tenant_id: uuid.UUID, invitation_id: uuid.UUID) -> Lookup:
    """``config`` has ``base_url``, ``tokens``, ``transport`` and ``timeout_s`` (the activation config of
    :mod:`app.cis.activation`: same CIS origin, same M2M client)."""
    try:
        token = await config.tokens.token(SCOPE_TENANT_READ)
    except TokenError as exc:
        return Lookup("error", code=exc.error_class)
    url = f"{config.base_url}{PATH.format(tenant_id=tenant_id)}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        resp = await config.transport("GET", url, headers, None, config.timeout_s)
    except HttpError as exc:
        return Lookup("error", code=f"cis_{exc.error_class}"[:40])
    if resp.status == 404:
        return Lookup("missing", code="cis_tenant_not_found")
    if resp.status == 401:
        config.tokens.invalidate(SCOPE_TENANT_READ)
    if resp.status != 200:
        return Lookup("error", code=f"cis_http_{resp.status}"[:40])
    try:
        data = json.loads(resp.body)
        members = data["members"] if isinstance(data, dict) else None
    except (ValueError, KeyError):
        return Lookup("error", code="cis_contract")
    if not isinstance(members, list) or _id(data.get("tenant_id")) != str(tenant_id):
        return Lookup("error", code="cis_contract")
    for member in members:
        if not isinstance(member, dict) or _id(member.get("invitation_id")) != str(invitation_id):
            continue
        if _id(member.get("tenant_id")) != str(tenant_id):
            return Lookup("error", code="cis_contract")
        role, status = member.get("content_studio_role"), member.get("status")
        if not isinstance(role, str) or not isinstance(status, str):
            return Lookup("error", code="cis_contract")
        return Lookup("found", role=role, status=status)
    return Lookup("missing", code="cis_invitation_not_found")


def _id(value: Any) -> str | None:
    """A uuid string in the canonical lower-case form (``str(uuid.UUID)``), else None."""
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None
