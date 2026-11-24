"""The AA add-ons an agency can quote (AA-1152; booking.b2b.v1 1.11 ``GET /add-ons``, S285 advisories PR-29 T6 and B2
option C).

Booking lists the published add-on offers (an optional activity of an AA supplier sold at some CIS places on the days
it runs) with the agency's own B2B price per traveller and AA's suggested retail price (RRP). The CRM reads them for
the builder's "AA add-on" drawer (:mod:`app.proposals.aa_addons`) and for the agency's own planner prices
(:mod:`app.tripplanner.prices`); the chosen add-on is quoted with ``POST /quotes`` like any ``sv:`` component.

:func:`page` copies the answer field by field (a field Booking adds later is dropped) and checks every value: a body
that breaks the contract raises :class:`app.booking.contract.ContractError` (the caller answers 503, nothing shown).
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.booking import contract
from app.booking.client import BookingClient

_SV = re.compile(r"^sv:[0-9a-f-]{36}$")
_ROLE = frozenset({"operates", "pickup"})
KINDS = frozenset({"transfer", "guide", "activity", "entrance", "meal"})
UNITS = frozenset({"person", "group", "vehicle", "day"})


@dataclass(frozen=True)
class Place:
    destination_id: str
    role: str
    name: str | None


@dataclass(frozen=True)
class Addon:
    component_id: str
    kind: str
    title: str
    unit: str
    duration_minutes: int | None
    min_pax: int
    operating_weekdays: tuple[int, ...]
    start_times: tuple[str, ...]
    first_date: dt.date
    places: tuple[Place, ...]
    b2b_per_person: Decimal | None
    rrp_per_person: Decimal | None


def _int(data: dict[str, Any], key: str, *, optional: bool = False) -> int | None:
    value = data.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise contract.ContractError(f"{key} is not a count")
    return value


def _price(value: Any, name: str, currency: str) -> Decimal | None:
    if value is None:
        return None
    amount, cur = contract.money(value, name)
    if cur != currency:
        raise contract.ContractError(f"{name} is not in the asked currency")
    return amount


def _place(value: Any) -> Place:
    data = contract._obj(value, "place")  # noqa: SLF001 — the shared field readers of the contract module
    role = data.get("role")
    if role not in _ROLE:
        raise contract.ContractError("place.role is malformed")
    name = data.get("name")
    return Place(str(uuid.UUID(str(data.get("destination_id")))), role, name if isinstance(name, str) else None)


def addon(value: Any, currency: str) -> Addon:
    data = contract._obj(value, "add-on")  # noqa: SLF001
    kind, unit = data.get("kind"), data.get("unit")
    if kind not in KINDS or unit not in UNITS:
        raise contract.ContractError("kind or unit is malformed")
    title = contract._str(data, "title")  # noqa: SLF001
    weekdays, times = data.get("operating_weekdays"), data.get("start_times")
    if not isinstance(weekdays, list) or not all(isinstance(w, int) and 1 <= w <= 7 for w in weekdays):
        raise contract.ContractError("operating_weekdays is malformed")
    if not isinstance(times, list) or not all(isinstance(t, str) for t in times):
        raise contract.ContractError("start_times is malformed")
    places = data.get("places")
    if not isinstance(places, list):
        raise contract.ContractError("places is not a list")
    return Addon(
        component_id=contract._str(data, "component_id", _SV),  # noqa: SLF001
        kind=kind,
        title=title[:200],
        unit=unit,
        duration_minutes=_int(data, "duration_minutes", optional=True),
        min_pax=_int(data, "min_pax") or 1,
        operating_weekdays=tuple(weekdays),
        start_times=tuple(times),
        first_date=contract._date(data.get("first_date"), "first_date"),  # noqa: SLF001
        places=tuple(_place(p) for p in places),
        b2b_per_person=_price(data.get("b2b_price_per_person"), "b2b_price_per_person", currency),
        rrp_per_person=_price(data.get("rrp_per_person"), "rrp_per_person", currency),
    )


def page(data: Any, currency: str) -> list[Addon]:
    body = contract._obj(data, "add-on page")  # noqa: SLF001
    items = body.get("items")
    if body.get("currency") != currency or not isinstance(items, list):
        raise contract.ContractError("currency or items is malformed")
    return [addon(item, currency) for item in items]


async def load(
    client: BookingClient,
    tenant_id: uuid.UUID,
    *,
    currency: str,
    pax: int,
    on: dt.date | None = None,
    destination_id: str | None = None,
) -> list[Addon]:
    """The add-ons for ``pax`` adults (of one day and / or place, or all). Raises BookingUnavailable / BookingRefusal /
    ContractError like every Booking read."""
    query = {"currency": currency, "pax": str(max(1, min(pax, 60)))}
    if on is not None:
        query["date"] = on.isoformat()
    if destination_id:
        query["destination_id"] = destination_id
    return page(await client.addons(tenant_id, query), currency)
