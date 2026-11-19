"""The CRM's calls to CIS ``tenant.addons.v1`` 1.1 for the agency app's TripPlanner module (AA-1139): the agency's
Trip planner as CIS holds it (planner addresses, custom domains, lead route, theme, locale, AI sessions quota) and the
owner's edits of the lead route and the appearance.

``GET <CIS_API_URL>/tenant/v1/tenants/{tenant_id}/tripplanner``                  -> :func:`get_module`
``PUT <CIS_API_URL>/tenant/v1/tenants/{tenant_id}/tripplanner/lead-route``       -> :func:`put_lead_route`
``PUT <CIS_API_URL>/tenant/v1/tenants/{tenant_id}/tripplanner/appearance``       -> :func:`put_appearance`

The same client config, token and scope as :mod:`app.cis.addons` (``cis.tenant/addons``); the CRM checks the person's
role first and names the member by its CRM member id. CIS applies its Tenant 360 editor's checks (an https webhook
URL whose host can be public, an e-mail address whose domain is not internal, the brand tokens only) and audits the
write as ``crm-member:<id>``.

The CIS answer is copied field by field (a field CIS adds later is dropped) and every value is checked: a body that is
not the contract shape is :class:`ModuleUnavailable` (``cis_contract``). Outcomes:

- a transport or token failure, a 5xx, a 401 (the cached token is dropped), a 403 (scope not granted) or a 404 without
  ``tenant_not_found`` (CIS does not serve 1.1 yet): :class:`ModuleUnavailable`;
- 404 ``tenant_not_found``: :class:`ModuleRefused` 404 ``tripplanner_no_account``;
- 409 ``tripplanner_not_active`` / ``tenant_offboarded`` and 422 with the editor's code (which names the field):
  :class:`ModuleRefused` with CIS's reason; an unknown reason is the status's generic code.

Nothing from a body is logged: the webhook URL and the e-mail are the agency's business targets, shown back to it only.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from app.cis.addons import SCOPE_TENANT_ADDONS
from app.cis.http import HttpError
from app.cis.m2m import TokenError

MODULE_PATH = "/tenant/v1/tenants/{tenant_id}/tripplanner"
TIMEOUT_S = 5.0

CHANNELS = ("none", "webhook", "email")
EFFECTIVE = frozenset({"crm", "webhook", "email", "none"})
LOCALES = ("en", "vi")
DOMAIN_STATUSES = frozenset({"pending", "validating", "attaching", "active", "failed"})
# The kit tokens the planner's brand layer themes (AA-CIS-App frontend/app/_kit/core/brandTheme.ts BRAND_TOKENS).
THEME_TOKENS = ("accent", "accent-deep", "accent-soft", "accent-tint", "accent-border", "on-accent")
WEBHOOK_URL_MAX = 500  # technical-constant: the CIS 390 CHECK length of a stored URL (contract shape)
EMAIL_MAX = 254  # technical-constant: the RFC 5321 address length, the CIS 390 CHECK
# CIS's stable refusal codes of contract 1.1 (Problem.reason); any other one is the status's generic code.
REASONS = frozenset(
    {
        "tripplanner_not_active",
        "tenant_offboarded",
        "lead_channel_invalid",
        "lead_webhook_url_required",
        "lead_webhook_url_too_long",
        "lead_webhook_url_not_https",
        "lead_webhook_url_invalid",
        "lead_webhook_url_not_public",
        "lead_email_required",
        "lead_email_invalid",
        "lead_email_not_public",
        "theme_invalid",
        "theme_token_unknown",
        "theme_colour_invalid",
        "locale_invalid",
        # the CRM's own codes for a refusal CIS gave without a known reason, or for no CIS account (audited as cis_*)
        "tripplanner_invalid",
        "tripplanner_refused",
        "tripplanner_no_account",
    }
)
MODULE_FIELDS = (
    "active",
    "in_service",
    "planner",
    "domains",
    "crm_core",
    "lead_route",
    "theme",
    "locale",
    "ai_sessions_monthly",
)
ROUTE_FIELDS = ("lead_channel", "lead_webhook_url", "lead_email", "effective_channel", "updated_at")
_HEX = re.compile(r"^#[0-9a-f]{6}$")


class ModuleUnavailable(Exception):
    """CIS could not answer (or not in the contract's shape). ``code`` only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code[:40]


class ModuleRefused(Exception):
    """CIS refused: ``status`` is CIS's HTTP status, ``reason`` the contract's code (or a generic one)."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


class _Shape(ValueError):
    """The body is not the contract's shape."""


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise _Shape
    return value


def _text(value: Any, max_len: int, *, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > max_len:
        raise _Shape
    return value


def _one_of(value: Any, allowed: frozenset[str] | tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _Shape
    return value


def _planner(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("domains"), list):
        raise _Shape
    url = _text(value.get("url"), 600, nullable=False)
    if not str(url).startswith("https://"):
        raise _Shape
    return {
        "url": url,
        "domains": [_text(d, 253, nullable=False) for d in value["domains"]],
        "launcher": _bool(value.get("launcher")),
    }


def _domains(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _Shape
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise _Shape
        out.append(
            {
                "domain": _text(item.get("domain"), 253, nullable=False),
                "status": _one_of(item.get("status"), DOMAIN_STATUSES),
            }
        )
    return out


def _route(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _Shape
    return {
        "lead_channel": _one_of(value.get("lead_channel"), CHANNELS),
        "lead_webhook_url": _text(value.get("lead_webhook_url"), WEBHOOK_URL_MAX),
        "lead_email": _text(value.get("lead_email"), EMAIL_MAX),
        "effective_channel": _one_of(value.get("effective_channel"), EFFECTIVE),
        "updated_at": _text(value.get("updated_at"), 64),
    }


def _theme(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > len(THEME_TOKENS):
        raise _Shape
    out = {}
    for key, colour in value.items():
        if key not in THEME_TOKENS or not isinstance(colour, str) or not _HEX.match(colour):
            raise _Shape
        out[key] = colour
    return out


def _quota(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _Shape
    return value


def parse_module(body: Any) -> dict[str, Any]:
    """The contract's ``TripPlannerModule``, copied field by field. Raises :class:`ModuleUnavailable`."""
    try:
        if not isinstance(body, dict):
            raise _Shape
        return {
            "active": _bool(body.get("active")),
            "in_service": _bool(body.get("in_service")),
            "planner": _planner(body.get("planner")),
            "domains": _domains(body.get("domains")),
            "crm_core": _bool(body.get("crm_core")),
            "lead_route": _route(body.get("lead_route")),
            "theme": _theme(body.get("theme")),
            "locale": _one_of(body.get("locale"), LOCALES),
            "ai_sessions_monthly": _quota(body.get("ai_sessions_monthly")),
        }
    except _Shape:
        raise ModuleUnavailable("cis_contract") from None


def _reason(body: bytes) -> str | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if isinstance(reason, str) else None


async def _call(config: Any, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """One call with the add-ons token; returns the parsed module, raises the outcomes above."""
    if config is None:
        raise ModuleUnavailable("not_configured")
    try:
        token = await config.tokens.token(SCOPE_TENANT_ADDONS)
    except TokenError as exc:
        raise ModuleUnavailable(exc.error_class) from None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    try:
        resp = await config.transport(method, f"{config.base_url}{path}", headers, body, TIMEOUT_S)
    except HttpError as exc:
        raise ModuleUnavailable(f"cis_{exc.error_class}") from None
    return _outcome(config, resp.status, resp.body)


def _outcome(config: Any, status: int, raw: bytes) -> dict[str, Any]:
    """The parsed module of a 200, else the refusal / unavailability it stands for (module docstring)."""
    if status == 200:
        try:
            return parse_module(json.loads(raw))
        except ValueError:
            raise ModuleUnavailable("cis_contract") from None
    if status == 401:
        config.tokens.invalidate(SCOPE_TENANT_ADDONS)
        raise ModuleUnavailable("cis_unauthorized")
    reason = _reason(raw)
    if status == 404:
        if reason == "tenant_not_found":
            raise ModuleRefused(404, "tripplanner_no_account")
        raise ModuleUnavailable("cis_not_served")  # CIS does not serve tenant.addons.v1 1.1 yet
    if status in (400, 409, 422):
        known = reason if reason in REASONS else {400: "tripplanner_invalid", 409: "tripplanner_refused"}.get(status)
        raise ModuleRefused(status, known or "tripplanner_invalid")
    raise ModuleUnavailable(f"cis_http_{status}")


async def get_module(config: Any, tenant_id: uuid.UUID) -> dict[str, Any]:
    """The agency's Trip planner (``TripPlannerModule``)."""
    return await _call(config, "GET", MODULE_PATH.format(tenant_id=tenant_id), None)


async def put_lead_route(
    config: Any,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    channel: str,
    webhook_url: str | None,
    email: str | None,
) -> dict[str, Any]:
    """Relay the owner's lead route (``TripPlannerLeadRouteIn``); answers the module after the change."""
    if channel not in CHANNELS:
        raise ValueError("unknown channel")
    payload = {"lead_channel": channel, "lead_webhook_url": webhook_url, "lead_email": email,
               "crm_member_id": str(member_id)}  # fmt: skip
    return await _call(config, "PUT", MODULE_PATH.format(tenant_id=tenant_id) + "/lead-route", payload)


async def put_appearance(
    config: Any, tenant_id: uuid.UUID, member_id: uuid.UUID, *, theme: dict[str, str], locale: str
) -> dict[str, Any]:
    """Relay the owner's theme and locale (``TripPlannerAppearanceIn``); answers the module after the change."""
    if locale not in LOCALES:
        raise ValueError("unknown locale")
    payload = {"theme": theme, "locale": locale, "crm_member_id": str(member_id)}
    return await _call(config, "PUT", MODULE_PATH.format(tenant_id=tenant_id) + "/appearance", payload)
