"""AA-1113 (crm-077; S284 tech lead redesign): the CRM's own monthly cap per model connection, the setting
``crm.ai.connection_monthly_cap_usd`` (CIS Platform > Settings, CRM tab, maker-checker; its guide in
``app/platform_settings/guides.json``).

A model connection has one budget at its provider. CIS (with TripPlanner) and the CRM spend on the same connections,
each through its own gateway and ledger, and neither reads the other's (root ADR 0008a). So each app keeps its OWN
monthly cap per connection, and the shares together stay under the provider budget: the CIS share is the
connection's monthly budget in the CIS gateway registry, the provider budget the CIS setting
``cis.spend.connection_provider_budget_usd`` (CIS 453), and this setting is the CRM share. CIS AI gateway > Budgets
reads it through the CRM settings relay (crm.ops.v1 ``/settings``) and flags a connection whose shares add up to more
than the provider budget.

The value is a JSON object {CIS connection key: USD per month}; a connection left out has no CRM cap. Read from the
settings snapshot (never the database on the call path). A value that is not such an object raises
:class:`CapUnreadable`: the caller (``app.llm.connection_cap``) treats it like any unreadable spend, never as "no cap".
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from app import platform_settings as ps


class CapUnreadable(Exception):
    """The setting could not be read or is not an object of amounts >= 0."""


def parse(value: Any) -> dict[str, Decimal]:
    """{connection key: USD} from the setting value; :class:`CapUnreadable` on any other shape."""
    if not isinstance(value, Mapping):
        raise CapUnreadable("not a JSON object")
    out: dict[str, Decimal] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key or isinstance(raw, bool) or not isinstance(raw, int | float | str):
            raise CapUnreadable(f"entry {key!r} is not a number")
        try:
            usd = Decimal(str(raw))
        except InvalidOperation:
            raise CapUnreadable(f"entry {key!r} is not a number") from None
        if not usd.is_finite() or usd < 0:
            raise CapUnreadable(f"entry {key!r} is below 0")
        out[key] = usd
    return out


def caps() -> dict[str, Decimal]:
    """Every CRM cap in force, by CIS connection key. Raises :class:`CapUnreadable`."""
    try:
        value = ps.get(ps.AI_CONNECTION_MONTHLY_CAP)
    except Exception as exc:  # noqa: BLE001 — the snapshot read never fails silently into "no cap"
        raise CapUnreadable(type(exc).__name__) from exc
    return parse(value)


def cap_for(connection_key: str) -> Decimal | None:
    """The CRM's monthly cap on one connection, None when it has none. Raises :class:`CapUnreadable`."""
    return caps().get(connection_key)
