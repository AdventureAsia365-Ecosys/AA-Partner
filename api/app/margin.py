"""Margin protection on every ``/crm/v1`` response (design v3 §4.6 "Enforcement" 2 and 3, Fix B12; card CRM-11).

The per-role serializer allow-list: a role holding :data:`app.permissions.Action.VIEW_MARGIN` (owner, agent) gets the
response as the route built it; every other role — the assistant, and a request whose role is not known (no
``tenant_tx()`` ran, e.g. ``/memberships``) — gets it with every **margin-bearing key** removed, at any depth:
B2B price ("net"), cost, margin, commission (design §4.6 field list: ``booking_line_money.cost``,
``commission_expected``, ``option.net_delta``, ``commission.*``, margin metrics, the quote's ``b2b_price``, proposal
margin totals). Keys are matched by name, so a field added by a later card is covered without a list to update:

    net, net_*, *_net, *_net_*        b2b, b2b_*, *_b2b*
    cost, cost_*, *_cost, *_cost_*    margin*, *_margin*        commission*, *_commission*

How it runs: :func:`class_router` (``app.auth.route_guard``) builds every AGENCY router with
:class:`MarginFilteredRoute`. ``tenant_tx()`` records the member's role in :data:`current_member_role` (a context
variable of the request's task); after the route returns, the route class reads it and filters the JSON body. A key
removed for a role that should not see it is logged (``crm_margin_key_stripped``, names only) — a route should not
have built it in the first place. ``tests/unit/test_margin_filter.py`` is the contract test over every ``/crm/v1``
route.

Known limits (AA-939 review, accepted for now):

- name matching over-strips for the filtered roles: a harmless key such as ``cost_center`` or
  ``net_promoter_score`` is removed too (it fails closed). Rename such a field rather than widen the pattern;
- only JSON bodies are filtered. A ``StreamingResponse`` or a non-JSON body (CSV export, a file) passes unchanged, so a
  route that streams money data must check :data:`app.permissions.Action.VIEW_MARGIN` itself. No ``/crm/v1`` route
  streams or exports today; the export card adds its own check and a contract test.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute

from app.permissions import Action, may

log = logging.getLogger(__name__)

# The member role of the request (set by app.tenancy.tenant_tx after the membership check; None = unknown).
current_member_role: ContextVar[str | None] = ContextVar("crm_member_role", default=None)

MARGIN_KEY = re.compile(r"(^|_)(net|b2b|cost|costs|margin|margins|commission|commissions)(_|$)")
_CANDIDATE = re.compile(rb"net|b2b|cost|margin|commission", re.IGNORECASE)


def is_margin_key(key: str) -> bool:
    return bool(MARGIN_KEY.search(key.lower()))


def sees_margin(role: str | None) -> bool:
    return role is not None and may(role, Action.VIEW_MARGIN)


def strip_margin(value: Any, removed: set[str] | None = None) -> Any:
    """A copy of ``value`` without margin-bearing keys at any depth (lists and dicts walked; other values kept)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and is_margin_key(k):
                if removed is not None:
                    removed.add(k)
                continue
            out[k] = strip_margin(v, removed)
        return out
    if isinstance(value, list):
        return [strip_margin(v, removed) for v in value]
    return value


def filter_body(body: bytes, role: str | None) -> tuple[bytes, set[str]]:
    """The JSON body for this role, and the keys removed. Non-JSON or margin-free bodies come back unchanged."""
    if sees_margin(role) or not body or not _CANDIDATE.search(body):
        return body, set()
    try:
        data = json.loads(body)
    except ValueError:
        return body, set()
    removed: set[str] = set()
    cleaned = strip_margin(data, removed)
    if not removed:
        return body, set()
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode(), removed


class MarginFilteredRoute(APIRoute):
    """An APIRoute whose JSON responses pass :func:`filter_body` for the request's member role."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()
        path = self.path

        async def handler(request: Request) -> Response:
            token = current_member_role.set(None)
            try:
                response = await original(request)
                role = current_member_role.get()
            finally:
                current_member_role.reset(token)
            media = (response.media_type or response.headers.get("content-type") or "").lower()
            body = getattr(response, "body", None)
            if not isinstance(body, bytes | bytearray) or "json" not in media:
                return response
            cleaned, removed = filter_body(bytes(body), role)
            if not removed:
                return response
            log.warning("crm_margin_key_stripped path=%s role=%s keys=%s", path, role, ",".join(sorted(removed)))
            headers = {k: v for k, v in response.headers.items() if k.lower() not in {"content-length", "content-type"}}
            return Response(
                content=cleaned, status_code=response.status_code, headers=headers, media_type="application/json"
            )

        return handler
