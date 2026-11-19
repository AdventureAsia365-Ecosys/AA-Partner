"""The CRM's calls to TripPlanner ``lead.submitted.v1`` 1.1 (tag ``planner``) for the agency app's TripPlanner module
(AA-1139): an agency's newest leads with their delivery status, its usage this month, and its webhook signing key.

``GET  <TRIPPLANNER_API_URL>/trip/m2m/v1/tenants/{id}/planner/leads?limit=n``     -> :func:`leads`
``GET  <TRIPPLANNER_API_URL>/trip/m2m/v1/tenants/{id}/planner/usage``             -> :func:`usage`
``GET  <TRIPPLANNER_API_URL>/trip/m2m/v1/tenants/{id}/planner/webhook-key``       -> :func:`key_info`
``POST <TRIPPLANNER_API_URL>/trip/m2m/v1/tenants/{id}/planner/webhook-key/rotate`` -> :func:`rotate_key`

The lead pull's config (:class:`app.tripplanner.leads.LeadPullConfig`: TripPlanner origin, the CRM's M2M client) with
its own scope ``tripplanner.leads/manage`` (the pull's ``deliver`` token reaches none of these, and this token neither
lists nor acknowledges leads). The CRM checks the person's role before any call (root ADR 0008a: it never reads the
tripplanner schema). Every answer is copied field by field and checked; a body outside the contract is
:class:`PlannerUnavailable` (``tripplanner_contract``). A lead carries ids, times and states only: TripPlanner never
sends a traveller field here, and a field it adds later is dropped. The rotated key is passed to the page once and
never logged or stored.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from app.cis.http import HttpError
from app.cis.m2m import TokenError

SCOPE = "tripplanner.leads/manage"
TIMEOUT_S = 5.0
PAGE = 50  # the contract's maximum page (one screen of the module's leads table)

# TripPlanner's refusals the CRM passes on (and audits as tp_<code>); anything else is "not available now".
REASONS = frozenset({"unknown_tenant", "webhook_key_unavailable"})
CHANNELS = frozenset({"crm", "webhook", "email"})
STATUSES = ("pending", "retrying", "delivered", "rejected", "expired")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CODE = re.compile(r"^[a-z][a-z0-9_:]{0,63}$")
_KEY = re.compile(r"^[0-9a-f]{64}$")
_MONTH = re.compile(r"^[0-9]{4}-[0-9]{2}$")


class PlannerUnavailable(Exception):
    """TripPlanner could not answer (or not in the contract's shape). ``code`` only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code[:40]


class PlannerRefused(Exception):
    """TripPlanner refused: 404 ``unknown_tenant`` (no Trip planner for this agency) or 503 ``webhook_key_unavailable``
    (the deployment holds no signing secret; nothing written)."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


class _Shape(ValueError):
    """The body is not the contract's shape."""


def _int(value: Any, *, nullable: bool = False, minimum: int = 0) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _Shape
    return value


def _str(value: Any, pattern: re.Pattern[str] | None = None, *, nullable: bool = False, max_len: int = 64) -> Any:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > max_len or (pattern is not None and not pattern.match(value)):
        raise _Shape
    return value


def _lead(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or item.get("channel") not in CHANNELS or item.get("status") not in STATUSES:
        raise _Shape
    return {
        "lead_id": _str(item.get("lead_id"), _UUID),
        "created_at": _str(item.get("created_at")),
        "channel": item["channel"],
        "status": item["status"],
        "attempts": _int(item.get("attempts")),
        "delivered_at": _str(item.get("delivered_at"), nullable=True),
        "error": _str(item.get("error"), _CODE, nullable=True),
    }


def parse_leads(body: Any) -> dict[str, Any]:
    """PlannerLeadList, copied field by field. Raises :class:`PlannerUnavailable`."""
    try:
        if not isinstance(body, dict) or not isinstance(body.get("leads"), list) or len(body["leads"]) > PAGE:
            raise _Shape
        month = body.get("month")
        if not isinstance(month, dict):
            raise _Shape
        return {
            "leads": [_lead(x) for x in body["leads"]],
            "month": {k: _int(month.get(k)) for k in ("total", *STATUSES)},
        }
    except _Shape:
        raise PlannerUnavailable("tripplanner_contract") from None


def parse_usage(body: Any) -> dict[str, Any]:
    """PlannerUsage. Raises :class:`PlannerUnavailable`."""
    try:
        if not isinstance(body, dict):
            raise _Shape
        ai, leads_24h = body.get("ai_allowance"), body.get("leads_24h")
        if not isinstance(ai, dict) or not isinstance(leads_24h, dict):
            raise _Shape
        return {
            "month": _str(body.get("month"), _MONTH),
            "ai_allowance": {"used": _int(ai.get("used")), "quota": _int(ai.get("quota"), nullable=True)},
            "leads_24h": {"count": _int(leads_24h.get("count")), "cap": _int(leads_24h.get("cap"), minimum=1)},
        }
    except _Shape:
        raise PlannerUnavailable("tripplanner_contract") from None


def parse_key_info(body: Any) -> dict[str, Any]:
    """PlannerWebhookKey (never a key). Raises :class:`PlannerUnavailable`."""
    try:
        if not isinstance(body, dict) or not isinstance(body.get("available"), bool) or "key" in body:
            raise _Shape
        return {"version": _int(body.get("version")), "rotated_at": _str(body.get("rotated_at"), nullable=True),
                "available": body["available"]}  # fmt: skip
    except _Shape:
        raise PlannerUnavailable("tripplanner_contract") from None


def parse_rotated(body: Any) -> dict[str, Any]:
    """PlannerWebhookKeyRotated. Raises :class:`PlannerUnavailable`."""
    try:
        if not isinstance(body, dict):
            raise _Shape
        return {"version": _int(body.get("version"), minimum=1), "rotated_at": _str(body.get("rotated_at")),
                "key": _str(body.get("key"), _KEY)}  # fmt: skip
    except _Shape:
        raise PlannerUnavailable("tripplanner_contract") from None


def _error_code(body: bytes) -> str | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    code = data.get("error") if isinstance(data, dict) else None
    return code if isinstance(code, str) else None


def _url(config: Any, tenant_id: uuid.UUID, rest: str) -> str:
    return f"{config.base_url.rstrip('/')}/trip/m2m/v1/tenants/{tenant_id}/planner/{rest}"


async def _call(
    config: Any, method: str, tenant_id: uuid.UUID, rest: str, payload: dict[str, Any] | None = None
) -> Any:
    if config is None:
        raise PlannerUnavailable("not_configured")
    url = _url(config, tenant_id, rest)
    try:
        token = await config.tokens.token(SCOPE)
    except TokenError as exc:
        raise PlannerUnavailable(exc.error_class) from None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    try:
        resp = await config.transport(method, url, headers, data, TIMEOUT_S)
    except HttpError as exc:
        raise PlannerUnavailable(f"tripplanner_{exc.error_class}") from None
    if resp.status == 200:
        try:
            return json.loads(resp.body)
        except ValueError:
            raise PlannerUnavailable("tripplanner_contract") from None
    if resp.status == 401:
        config.tokens.invalidate(SCOPE)
    code = _error_code(resp.body)
    if resp.status == 404 and code == "unknown_tenant":
        raise PlannerRefused(404, "unknown_tenant")
    if resp.status == 503 and code == "webhook_key_unavailable":
        raise PlannerRefused(503, "webhook_key_unavailable")
    raise PlannerUnavailable(f"tripplanner_http_{resp.status}")  # 403 (scope not granted yet), 5xx, a 404 path


async def leads(config: Any, tenant_id: uuid.UUID, limit: int = PAGE) -> dict[str, Any]:
    return parse_leads(await _call(config, "GET", tenant_id, f"leads?limit={int(limit)}"))


async def usage(config: Any, tenant_id: uuid.UUID) -> dict[str, Any]:
    return parse_usage(await _call(config, "GET", tenant_id, "usage"))


async def key_info(config: Any, tenant_id: uuid.UUID) -> dict[str, Any]:
    return parse_key_info(await _call(config, "GET", tenant_id, "webhook-key"))


async def rotate_key(config: Any, tenant_id: uuid.UUID, member_id: uuid.UUID) -> dict[str, Any]:
    """A new key version and its key (shown once by the page)."""
    body = await _call(config, "POST", tenant_id, "webhook-key/rotate", {"actor": f"crm-member:{member_id}"})
    return parse_rotated(body)
