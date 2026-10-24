"""The ``tenant.membership.v1`` shapes the CRM accepts (AA-978), checked by hand against the vendored contract
``docs/contracts/tenant.membership.v1.openapi.yaml`` (a byte copy of the root file; ``tests/unit/test_cis_contract.py``
pins its hash and compares the enums and field lists below with it).

Every object is closed (``additionalProperties: false``): an unknown field refuses the event. A ``crm_role`` anywhere
is its own reason (``crm_role_present``, the AA-978 refusal case: CIS can never grant a CRM role). The CRM never reads
the Content studio role for anything; it is checked only so a malformed event is refused, not half-applied. A value of
the wrong JSON type (a list where a string belongs, ...) is refused like any other bad value, never raised.

:func:`check_event` returns ``None`` for a valid event, else one reason code (the inbox ``reason_code``). It never
returns or logs a value from the event.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

CONTRACT = "tenant.membership.v1"

EVENT_TYPES = frozenset(
    {
        "tenant.created",
        "tenant.locked",
        "tenant.unlocked",
        "tenant.deleted",
        "membership.invited",
        "membership.activated",
        "membership.role_changed",
        "membership.removed",
    }
)
CONTENT_STUDIO_ROLES = frozenset({"owner", "editor", "viewer"})
MEMBERSHIP_STATUSES = frozenset({"invited", "active", "removed"})

EVENT_REQUIRED = frozenset({"event_id", "cursor", "type", "occurred_at", "tenant_id"})
EVENT_FIELDS = EVENT_REQUIRED | {"membership"}
MEMBERSHIP_REQUIRED = frozenset(
    {"membership_id", "tenant_id", "identity", "content_studio_role", "status", "updated_at"}
)
MEMBERSHIP_FIELDS = MEMBERSHIP_REQUIRED | {"invitation_id"}
IDENTITY_FIELDS = frozenset({"iss", "sub"})
PAGE_FIELDS = frozenset({"items", "next_cursor"})

MAX_CURSOR = 200
MAX_ISS = 300
MAX_SUB = 128
MAX_PAGE_ITEMS = 500


class PageError(ValueError):
    """The page itself (not one event) does not match ``MembershipEventPage``: nothing on it is applied."""


def _uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _datetime(value: Any) -> bool:
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _member_of(value: Any, allowed: frozenset[str]) -> bool:
    """``value in allowed`` for a string only (any other JSON value, hashable or not, is simply not a member)."""
    return isinstance(value, str) and value in allowed


def _cursor(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_CURSOR and all("!" <= c <= "~" for c in value)


def parse_page(body: Any) -> tuple[list[Any], str]:
    """``(items, next_cursor)`` of a ``MembershipEventPage``; :class:`PageError` when the page is malformed."""
    if not isinstance(body, dict) or set(body) != PAGE_FIELDS:
        raise PageError("page_shape")
    items, next_cursor = body["items"], body["next_cursor"]
    if not isinstance(items, list) or len(items) > MAX_PAGE_ITEMS:
        raise PageError("page_items")
    if not isinstance(next_cursor, str) or len(next_cursor) > MAX_CURSOR:
        raise PageError("page_cursor")
    if next_cursor and not _cursor(next_cursor):
        raise PageError("page_cursor")
    return items, next_cursor


def _has_crm_role(obj: Any) -> bool:
    if isinstance(obj, dict):
        return "crm_role" in obj or any(_has_crm_role(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_crm_role(v) for v in obj)
    return False


def _check_identity(identity: Any) -> str | None:
    if identity is None:
        return None
    if not isinstance(identity, dict) or set(identity) != IDENTITY_FIELDS:
        return "unknown_field" if isinstance(identity, dict) and set(identity) - IDENTITY_FIELDS else "invalid_field"
    iss, sub = identity["iss"], identity["sub"]
    if not isinstance(iss, str) or not iss.startswith("https://") or len(iss) > MAX_ISS:
        return "invalid_field"
    if not isinstance(sub, str) or not 1 <= len(sub) <= MAX_SUB:
        return "invalid_field"
    return None


def _check_membership(membership: Any, tenant_id: str) -> str | None:
    if not isinstance(membership, dict):
        return "invalid_field"
    if set(membership) - MEMBERSHIP_FIELDS:
        return "unknown_field"
    if MEMBERSHIP_REQUIRED - set(membership):
        return "missing_field"
    # isinstance first: a list or an object is not hashable, and a frozenset test would raise instead of refusing.
    if not _member_of(membership["content_studio_role"], CONTENT_STUDIO_ROLES):
        return "invalid_role"
    if not _member_of(membership["status"], MEMBERSHIP_STATUSES):
        return "invalid_field"
    invitation = membership.get("invitation_id")
    if not (_uuid(membership["membership_id"]) and _uuid(membership["tenant_id"])) or not _datetime(
        membership["updated_at"]
    ):
        return "invalid_field"
    if invitation is not None and not _uuid(invitation):
        return "invalid_field"
    reason = _check_identity(membership["identity"])
    if reason:
        return reason
    if membership["identity"] is None and membership["status"] == "active":
        # The identity binds at activation: an invited membership has none, and neither has a removed one that was
        # never activated (a withdrawn invitation; the CRM records it as ignored / no_identity).
        return "identity_missing"
    if uuid.UUID(membership["tenant_id"]) != uuid.UUID(tenant_id):
        return "tenant_mismatch"
    return None


def check_event(event: Any) -> str | None:
    """``None`` when ``event`` is a valid ``MembershipEvent``; else the reason code it is refused with."""
    if not isinstance(event, dict):
        return "invalid_field"
    if _has_crm_role(event):
        return "crm_role_present"
    if set(event) - EVENT_FIELDS:
        return "unknown_field"
    if EVENT_REQUIRED - set(event):
        return "missing_field"
    if not _member_of(event["type"], EVENT_TYPES):
        return "invalid_type"
    if not (_uuid(event["event_id"]) and _uuid(event["tenant_id"]) and _cursor(event["cursor"])):
        return "invalid_field"
    if not _datetime(event["occurred_at"]):
        return "invalid_field"
    membership = event.get("membership")
    if membership is None:
        return "membership_missing" if event["type"].startswith("membership.") else None
    reason = _check_membership(membership, event["tenant_id"])
    if reason:
        return reason
    if event["type"] == "membership.removed" and membership["status"] != "removed":
        return "status_mismatch"
    return None
