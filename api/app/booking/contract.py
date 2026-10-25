"""Reading ``booking.b2b.v1`` answers (root contract, minor 1.1) into small typed values (AA-947).

A **tolerant reader**: the fields the CRM uses are checked (type, pattern, enum); fields it does not use are ignored,
so an additive minor (contracts README rule 2) never breaks the CRM. A missing or malformed field the CRM needs raises
:class:`ContractError` (the caller treats it as a transient ``booking_contract`` failure: Booking is retried, nothing
half-read is stored). Events are read one by one (:func:`event`): an event of a type this build does not know is still
returned (``known = False``) so the feed records it as ignored and moves on.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_ID = re.compile(r"^[A-Za-z0-9:_.-]{1,64}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_AMOUNT = re.compile(r"^-?\d+(\.\d{1,2})?$")
_RATE = re.compile(r"^\d+(\.\d{1,8})?$")
_CODE = re.compile(r"^[a-z][a-z_]{0,39}$")
_TYPE = re.compile(r"^[a-z][a-z_.]{0,39}$")
AVAILABILITY = frozenset({"available", "on_request", "sold_out"})
HOLD_STATUSES = frozenset({"active", "confirmed", "expired", "released"})
BOOKING_LINE_STATUSES = frozenset({"confirmed", "on_request", "declined", "cancelled"})
EVENT_TYPES = frozenset(
    {
        "hold.expired",
        "hold.extended",
        "line.confirmed",
        "line.declined",
        "booking.cancelled",
        "departure.guaranteed",
        "departure.cancelled",
        "departure.incident",
        "departure.incident_cleared",
        "departure.time_change",
    }
)
DECLINE_CODES = frozenset({"sold_out", "supplier_unavailable", "minimum_not_met", "other"})
COMPONENT_KINDS = frozenset({"trip_plan", "hotel_room", "transfer", "guide", "activity", "entrance", "meal"})


class ContractError(ValueError):
    """An answer the CRM cannot read; the message names the field, never a value."""


def _obj(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{name} is not an object")
    return value


def _str(data: dict[str, Any], key: str, pattern: re.Pattern[str] | None = None, *, optional: bool = False) -> Any:
    value = data.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or (pattern is not None and not pattern.match(value)):
        raise ContractError(f"{key} is missing or malformed")
    return value


def _decimal(value: Any, pattern: re.Pattern[str], name: str) -> Decimal:
    if not isinstance(value, str) or not pattern.match(value):
        raise ContractError(f"{name} is not a decimal string")
    try:
        return Decimal(value)
    except InvalidOperation:  # pragma: no cover — the pattern already guarantees a decimal
        raise ContractError(f"{name} is not a decimal string") from None


def _datetime(value: Any, name: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ContractError(f"{name} is not a date-time")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractError(f"{name} is not a date-time") from None
    if parsed.tzinfo is None:
        raise ContractError(f"{name} has no offset")
    return parsed


def _date(value: Any, name: str) -> dt.date:
    if not isinstance(value, str):
        raise ContractError(f"{name} is not a date")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ContractError(f"{name} is not a date") from None


def money(value: Any, name: str) -> tuple[Decimal, str]:
    data = _obj(value, name)
    return _decimal(data.get("amount"), _AMOUNT, f"{name}.amount"), _str(data, "currency", _CURRENCY)


@dataclass(frozen=True)
class QuoteLine:
    quote_id: str
    price_hash: str
    valid_until: dt.datetime
    currency: str
    rate_per_usd: Decimal
    rate_date: dt.date
    b2b_price: Decimal
    unit: str
    availability: str
    departure_id: str | None


def quote_line(data: Any, line_ref: str) -> QuoteLine:
    """The one line ``line_ref`` of a ``Quote`` (the CRM quotes one AA line per call)."""
    quote = _obj(data, "quote")
    fx = _obj(quote.get("fx"), "fx")
    lines = quote.get("lines")
    if not isinstance(lines, list):
        raise ContractError("lines is not a list")
    mine = [x for x in lines if isinstance(x, dict) and x.get("line_ref") == line_ref]
    if len(mine) != 1:
        raise ContractError("the quote does not answer the line exactly once")
    line = mine[0]
    amount, currency = money(line.get("b2b_price"), "b2b_price")
    quote_currency = _str(quote, "currency", _CURRENCY)
    if currency != quote_currency or amount < 0:
        raise ContractError("b2b_price is not in the quote currency")
    availability = _str(line, "availability")
    if availability not in AVAILABILITY:
        raise ContractError("availability is unknown")
    rate = _decimal(fx.get("rate_per_usd"), _RATE, "fx.rate_per_usd")
    if rate <= 0:
        raise ContractError("fx.rate_per_usd is not positive")
    return QuoteLine(
        quote_id=_str(quote, "quote_id", _ID),
        price_hash=_str(quote, "price_hash", _HASH),
        valid_until=_datetime(quote.get("valid_until"), "valid_until"),
        currency=quote_currency,
        rate_per_usd=rate,
        rate_date=_date(fx.get("rate_date"), "fx.rate_date"),
        b2b_price=amount,
        unit=_str(line, "unit"),
        availability=availability,
        departure_id=_str(line, "departure_id", _ID, optional=True),
    )


@dataclass(frozen=True)
class Hold:
    hold_id: str
    status: str
    expires_at: dt.datetime
    line_status: str | None


def hold(data: Any, line_ref: str | None = None) -> Hold:
    body = _obj(data, "hold")
    status = _str(body, "status")
    if status not in HOLD_STATUSES:
        raise ContractError("hold status is unknown")
    line_status = None
    if line_ref is not None:
        for line in body.get("lines") or []:
            if isinstance(line, dict) and line.get("line_ref") == line_ref:
                line_status = line.get("line_status") if line.get("line_status") in AVAILABILITY else None
    return Hold(
        hold_id=_str(body, "hold_id", _ID),
        status=status,
        expires_at=_datetime(body.get("expires_at"), "expires_at"),
        line_status=line_status,
    )


@dataclass(frozen=True)
class Booking:
    booking_id: str
    hold_id: str
    trip_case_code: str
    line_status: str


def booking(data: Any, line_ref: str) -> Booking:
    body = _obj(data, "booking")
    mine = [x for x in body.get("lines") or [] if isinstance(x, dict) and x.get("line_ref") == line_ref]
    if len(mine) != 1 or mine[0].get("status") not in BOOKING_LINE_STATUSES:
        raise ContractError("the booking does not answer the line exactly once")
    code = _str(body, "trip_case_code")
    if not 1 <= len(code) <= 32 or any(ord(c) < 32 for c in code):
        raise ContractError("trip_case_code is malformed")
    return Booking(
        booking_id=_str(body, "booking_id", _ID),
        hold_id=_str(body, "hold_id", _ID),
        trip_case_code=code,
        line_status=mine[0]["status"],
    )


@dataclass(frozen=True)
class Event:
    event_id: str
    type: str
    known: bool
    hold_id: str | None = None
    booking_id: str | None = None
    line_ref: str | None = None
    departure_id: str | None = None
    decline_code: str | None = None


def event(item: Any) -> Event:
    """One feed item. Raises :class:`ContractError` when it has no usable id or type (recorded as rejected when the id
    is readable, see :func:`event_id`)."""
    body = _obj(item, "event")
    kind = _str(body, "type", _TYPE)
    decline = body.get("decline_code")
    return Event(
        event_id=_str(body, "event_id", _ID),
        type=kind,
        known=kind in EVENT_TYPES,
        hold_id=_str(body, "hold_id", _ID, optional=True),
        booking_id=_str(body, "booking_id", _ID, optional=True),
        line_ref=_str(body, "line_ref", _ID, optional=True),
        departure_id=_str(body, "departure_id", _ID, optional=True),
        decline_code=decline if decline in DECLINE_CODES else None,
    )


def event_id(item: Any) -> str | None:
    """The event id of a feed item, or None when it has none the CRM can key on."""
    value = item.get("event_id") if isinstance(item, dict) else None
    return value if isinstance(value, str) and _ID.match(value) else None


def event_page(data: Any) -> tuple[list[Any], str]:
    body = _obj(data, "event page")
    items = body.get("items")
    if not isinstance(items, list):
        raise ContractError("items is not a list")
    cursor = body.get("next_cursor")
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= 200:
        raise ContractError("next_cursor is missing or malformed")
    return items, cursor


def from_price(data: Any, currency: str) -> Decimal | None:
    """The public "from" price per person of a ``PriceParity`` answer in ``currency`` (None when AA has none)."""
    body = _obj(data, "price parity")
    value = body.get("from_price")
    if value is None:
        return None
    amount, cur = money(value, "from_price")
    if cur != currency or amount < 0:
        raise ContractError("from_price is not in the asked currency")
    return amount


@dataclass(frozen=True)
class Component:
    component_id: str
    kind: str
    day_no: int | None


@dataclass(frozen=True)
class Product:
    source_id: str
    trip_plan_id: str
    sellable: bool
    has_fixed_departures: bool
    components: tuple[Component, ...]


def products_page(data: Any) -> tuple[list[Product], str | None]:
    body = _obj(data, "product page")
    items = body.get("items")
    if not isinstance(items, list):
        raise ContractError("items is not a list")
    out = []
    for item in items:
        product = _obj(item, "product")
        tour = _obj(product.get("tour"), "tour")
        components = []
        for c in product.get("components") or []:
            if (
                isinstance(c, dict)
                and isinstance(c.get("component_id"), str)
                and _ID.match(c["component_id"])
                and (c.get("kind") in COMPONENT_KINDS)
            ):
                day = c.get("day_no")
                components.append(Component(c["component_id"], c["kind"], day if isinstance(day, int) else None))
        out.append(
            Product(
                source_id=_str(tour, "source_id"),
                trip_plan_id=str(product.get("trip_plan_id") or ""),
                sellable=product.get("sellable") is True and product.get("withdrawn") is not True,
                has_fixed_departures=product.get("has_fixed_departures") is True,
                components=tuple(components),
            )
        )
    cursor = body.get("next_cursor")
    return out, cursor if isinstance(cursor, str) and cursor else None


@dataclass(frozen=True)
class Departure:
    departure_id: str
    start_date: dt.date
    available: int
    status: str
    auto_confirm: bool


def departures(data: Any) -> list[Departure]:
    body = _obj(data, "departure page")
    out = []
    for item in body.get("items") or []:
        d = _obj(item, "departure")
        available = d.get("available")
        out.append(
            Departure(
                departure_id=_str(d, "departure_id", _ID),
                start_date=_date(d.get("start_date"), "start_date"),
                available=available if isinstance(available, int) and available >= 0 else 0,
                status=_str(d, "status"),
                auto_confirm=d.get("auto_confirm") is True,
            )
        )
    return out


def problem_code(problem: dict[str, Any]) -> str | None:
    code = problem.get("error")
    return code if isinstance(code, str) and _CODE.match(code) else None
