"""The CRM's calls to CIS ``tenant.addons.v1`` (AA-1036 R3, root ADR 0019 d7): an agency's add-ons and its CRM owner's
add-on request, for an agency whose owner works in the CRM without a Content studio membership.

``GET  <CIS_API_URL>/tenant/v1/tenants/{tenant_id}/add-ons``          -> :func:`list_add_ons`
``POST <CIS_API_URL>/tenant/v1/tenants/{tenant_id}/add-on-requests``  -> :func:`request_add_on`

An M2M token of scope ``cis.tenant/addons`` (asked for alone, like every M2M call here); the CIS tenant id is the CRM
agency id. CIS is the one store of the requests (``shared.tenant_addon_request``) and AA staff decide in CIS Tenant 360;
the studio owner asks through CIS ``/v1/account/add-on-requests`` with the studio token, the CRM owner through here.
The CRM decides who may ask (its owner: :data:`app.permissions.Action.ADDON_REQUEST`) and names that member by its
CRM member id; nothing else about the person crosses.

The CIS answer is copied field by field (the contract's closed shapes; a field CIS adds later is dropped, never passed
on) and every value is checked: a body that is not the contract shape is :class:`AddOnsUnavailable` (``cis_contract``).
Outcomes:

- a transport failure, a token failure, a 5xx, a 401 (the cached token is dropped first), a 403 (the scope is not
  granted yet: Infra not applied) or a 404 without CIS's ``tenant_not_found`` (CIS does not serve the contract yet):
  :class:`AddOnsUnavailable` (503 ``addons_unavailable`` to the web);
- 404 ``tenant_not_found``: :class:`AddOnsRefused` 404 ``addons_no_account`` (the agency has no CIS account);
- a request refused by CIS's rules: :class:`AddOnsRefused` with CIS's reason (409 ``already_subscribed`` /
  ``tenant_not_in_service``, 429 ``too_many_requests``, 400 ``note_too_long`` / ``unknown_product``).

Nothing from a body is logged; the agency's note is sent to CIS and shown back to the agency, never logged.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.cis.http import HttpError
from app.cis.m2m import TokenError

SCOPE_TENANT_ADDONS = "cis.tenant/addons"
LIST_PATH = "/tenant/v1/tenants/{tenant_id}/add-ons"
REQUEST_PATH = "/tenant/v1/tenants/{tenant_id}/add-on-requests"
# A page load and a click: shorter than the M2M default so the web's 15 s BFF timeout is never reached.
TIMEOUT_S = 5.0

PRODUCTS = ("crm", "tripplanner")
STATES = frozenset({"active", "requested", "available"})
REQUEST_STATUSES = frozenset({"pending", "approved", "declined"})
SUBSCRIPTION_STATUSES = frozenset({"trialing", "active", "past_due"})
SETUPS = frozenset({"in_progress", "done", "failed"})
NOTE_MAX = 500
# CIS's stable refusal codes of the contract (Problem.reason); any other one is passed on as the status's generic code.
REASONS = frozenset(
    {"already_subscribed", "tenant_not_in_service", "too_many_requests", "unknown_product", "note_too_long"}
)

REQUEST_FIELDS = ("request_id", "product", "status", "note", "requested_at", "decided_at", "decision_note", "setup")
ADD_ON_FIELDS = ("product", "state", "subscription_status", "request", "last_decision")


class AddOnsUnavailable(Exception):
    """CIS could not answer (or not in the contract's shape). ``code`` only (``^[a-z][a-z0-9_]{0,39}$``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code[:40]


class AddOnsRefused(Exception):
    """CIS refused: ``status`` is CIS's HTTP status, ``reason`` the contract's code (or a generic one)."""

    def __init__(self, status: int, reason: str, retry_after_s: int | None = None) -> None:
        super().__init__(reason)
        self.status, self.reason, self.retry_after_s = status, reason, retry_after_s


@dataclass(frozen=True)
class Requested:
    created: bool
    request: dict[str, Any]


class _Shape(ValueError):
    """The body is not the contract's shape."""


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise _Shape
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise _Shape from None


def _text(value: Any, *, nullable: bool = True, max_len: int = NOTE_MAX) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > max_len:
        raise _Shape
    return value


def _one_of(value: Any, allowed: frozenset[str], *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise _Shape
    return value


def _request(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _Shape
    return {
        "request_id": _uuid(value.get("request_id")),
        "product": _one_of(value.get("product"), frozenset(PRODUCTS)),
        "status": _one_of(value.get("status"), REQUEST_STATUSES),
        "note": _text(value.get("note")),
        "requested_at": _text(value.get("requested_at"), nullable=False, max_len=64),
        "decided_at": _text(value.get("decided_at"), max_len=64),
        "decision_note": _text(value.get("decision_note")),
        "setup": _one_of(value.get("setup"), SETUPS, nullable=True),
    }


def parse_add_ons(body: Any) -> dict[str, Any]:
    """The contract's ``AddOnList``, copied field by field. Raises :class:`AddOnsUnavailable` (``cis_contract``)."""
    try:
        if not isinstance(body, dict) or not isinstance(body.get("in_service"), bool):
            raise _Shape
        items = body.get("add_ons")
        if not isinstance(items, list) or len(items) != len(PRODUCTS):
            raise _Shape
        add_ons = []
        for item in items:
            if not isinstance(item, dict):
                raise _Shape
            add_ons.append(
                {
                    "product": _one_of(item.get("product"), frozenset(PRODUCTS)),
                    "state": _one_of(item.get("state"), STATES),
                    "subscription_status": _one_of(
                        item.get("subscription_status"), SUBSCRIPTION_STATUSES, nullable=True
                    ),
                    "request": _request(item.get("request")),
                    "last_decision": _request(item.get("last_decision")),
                }
            )
        if sorted(a["product"] for a in add_ons) != sorted(PRODUCTS):
            raise _Shape
    except _Shape:
        raise AddOnsUnavailable("cis_contract") from None
    return {"in_service": body["in_service"], "add_ons": add_ons}


def parse_created(body: Any) -> Requested:
    """The contract's ``AddOnRequestCreated``. Raises :class:`AddOnsUnavailable` (``cis_contract``)."""
    try:
        if not isinstance(body, dict) or not isinstance(body.get("created"), bool):
            raise _Shape
        request = _request(body.get("request"))
        if request is None:
            raise _Shape
    except _Shape:
        raise AddOnsUnavailable("cis_contract") from None
    return Requested(created=body["created"], request=request)


def _problem_reason(body: bytes) -> str | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if isinstance(reason, str) else None


def _retry_after(headers: Any) -> int | None:
    value = next((v for k, v in dict(headers or {}).items() if k.lower() == "retry-after"), None)
    return int(value) if isinstance(value, str) and value.isdigit() else None


async def _call(config: Any, method: str, path: str, body: bytes | None) -> tuple[int, Any]:
    """One call with the add-ons token; returns the decoded 200 / 201 body, raises the outcomes above."""
    if config is None:
        raise AddOnsUnavailable("not_configured")
    try:
        token = await config.tokens.token(SCOPE_TENANT_ADDONS)
    except TokenError as exc:
        raise AddOnsUnavailable(exc.error_class) from None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        resp = await config.transport(method, f"{config.base_url}{path}", headers, body, TIMEOUT_S)
    except HttpError as exc:
        raise AddOnsUnavailable(f"cis_{exc.error_class}") from None
    if resp.status in (200, 201):
        try:
            return resp.status, json.loads(resp.body)
        except ValueError:
            raise AddOnsUnavailable("cis_contract") from None
    if resp.status == 401:
        config.tokens.invalidate(SCOPE_TENANT_ADDONS)
        raise AddOnsUnavailable("cis_unauthorized")
    reason = _problem_reason(resp.body)
    if resp.status == 404:
        if reason == "tenant_not_found":
            raise AddOnsRefused(404, "addons_no_account")
        raise AddOnsUnavailable("cis_not_served")  # CIS does not serve tenant.addons.v1 yet
    if resp.status in (400, 409, 429):
        known = reason if reason in REASONS else {400: "addon_invalid", 409: "addon_refused"}.get(resp.status)
        raise AddOnsRefused(resp.status, known or "too_many_requests", _retry_after(resp.headers))
    raise AddOnsUnavailable(f"cis_http_{resp.status}")  # 403 (scope not granted yet), 5xx, anything else


async def list_add_ons(config: Any, tenant_id: uuid.UUID) -> dict[str, Any]:
    """The agency's add-ons (``AddOnList``). ``config`` has ``base_url``, ``tokens`` and ``transport`` (the CRM's CIS
    client config, :func:`app.main.build_studio_activation`: same CIS origin, same M2M client)."""
    _, body = await _call(config, "GET", LIST_PATH.format(tenant_id=tenant_id), None)
    return parse_add_ons(body)


async def request_add_on(
    config: Any, tenant_id: uuid.UUID, product: str, note: str | None, member_id: uuid.UUID
) -> Requested:
    """Relay the CRM owner's request (``AddOnRequestIn``): 201 a new request, 200 the open one (``created`` false)."""
    if product not in PRODUCTS:
        raise ValueError("unknown product")
    payload = {"product": product, "note": note, "crm_member_id": str(member_id)}
    status, body = await _call(config, "POST", REQUEST_PATH.format(tenant_id=tenant_id), json.dumps(payload).encode())
    created = parse_created(body)
    if created.created != (status == 201):
        raise AddOnsUnavailable("cis_contract")
    return created
