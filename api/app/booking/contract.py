"""Reading ``booking.b2b.v1`` answers (root contract, minor 1.1; 1.2 for :func:`held_set`; 1.4 for :func:`tariff`) into
small typed values (AA-947, AA-952, AA-1030; :func:`fx_rates`, 1.0 ``GET /fx-rates``, for the S270 tariff currencies).

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
# 1.1 Event.incident_code (departure.incident / departure.incident_cleared): a fixed list, never free text (AA-961).
INCIDENT_CODES = frozenset({"weather", "road", "health", "political", "supplier", "other"})
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


def booking_lines(data: Any) -> dict[str, str]:
    """Every line of a ``Booking`` answer: ``line_ref`` -> status (AA-952: one confirm answers a whole hold set, so the
    sync writes each line of the set from it). A line with an unknown status is left out."""
    body = _obj(data, "booking")
    out: dict[str, str] = {}
    for line in body.get("lines") or []:
        if (
            isinstance(line, dict)
            and isinstance(line.get("line_ref"), str)
            and line.get("status") in BOOKING_LINE_STATUSES
        ):
            out[line["line_ref"]] = line["status"]
    return out


@dataclass(frozen=True)
class HeldLine:
    b2b_price: Decimal
    line_status: str | None


@dataclass(frozen=True)
class HeldSet:
    """A ``Hold`` answer with the quote it holds (contract 1.2, AA-952): what the CRM needs to write the AA lines of an
    accepted proposal (the net = each line's ``b2b_price`` with the quote's FX lock) and to confirm it later."""

    hold_id: str
    quote_id: str
    status: str
    expires_at: dt.datetime
    price_hash: str
    currency: str
    rate_per_usd: Decimal
    rate_date: dt.date
    lines: dict[str, HeldLine]


def held_set(data: Any) -> HeldSet:
    """A 1.1 answer (no ``price_hash`` / ``fx`` / line prices) is a :class:`ContractError`: Booking is not on 1.2 yet,
    the caller retries later and writes nothing."""
    body = _obj(data, "hold")
    status = _str(body, "status")
    if status not in HOLD_STATUSES:
        raise ContractError("hold status is unknown")
    currency = _str(body, "currency", _CURRENCY)
    fx = _obj(body.get("fx"), "fx")
    rate = _decimal(fx.get("rate_per_usd"), _RATE, "fx.rate_per_usd")
    if rate <= 0:
        raise ContractError("fx.rate_per_usd is not positive")
    lines: dict[str, HeldLine] = {}
    for line in body.get("lines") or []:
        if not isinstance(line, dict):
            raise ContractError("a hold line is not an object")
        ref = _str(line, "line_ref", _ID)
        amount, line_currency = money(line.get("b2b_price"), "b2b_price")
        if line_currency != currency or amount < 0:
            raise ContractError("b2b_price is not in the hold currency")
        status_of_line = line.get("line_status") if line.get("line_status") in AVAILABILITY else None
        lines[ref] = HeldLine(b2b_price=amount, line_status=status_of_line)
    return HeldSet(
        hold_id=_str(body, "hold_id", _ID),
        quote_id=_str(body, "quote_id", _ID),
        status=status,
        expires_at=_datetime(body.get("expires_at"), "expires_at"),
        price_hash=_str(body, "price_hash", _HASH),
        currency=currency,
        rate_per_usd=rate,
        rate_date=_date(fx.get("rate_date"), "fx.rate_date"),
        lines=lines,
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
    incident_code: str | None = None
    occurred_at: dt.datetime | None = None


def _occurred_at(value: Any) -> dt.datetime | None:
    """The event's ``occurred_at``; None when absent or unreadable (tolerant: only the incident flag uses it, and
    falls back to the time the CRM read the event)."""
    try:
        return _datetime(value, "occurred_at")
    except ContractError:
        return None


def event(item: Any) -> Event:
    """One feed item. Raises :class:`ContractError` when it has no usable id or type (recorded as rejected when the id
    is readable, see :func:`event_id`)."""
    body = _obj(item, "event")
    kind = _str(body, "type", _TYPE)
    decline = body.get("decline_code")
    incident = body.get("incident_code")
    return Event(
        event_id=_str(body, "event_id", _ID),
        type=kind,
        known=kind in EVENT_TYPES,
        hold_id=_str(body, "hold_id", _ID, optional=True),
        booking_id=_str(body, "booking_id", _ID, optional=True),
        line_ref=_str(body, "line_ref", _ID, optional=True),
        departure_id=_str(body, "departure_id", _ID, optional=True),
        decline_code=decline if decline in DECLINE_CODES else None,
        incident_code=incident if incident in INCIDENT_CODES else None,
        occurred_at=_occurred_at(body.get("occurred_at")),
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


# ── AA-957 (C12): POST /bookings/{id}/cancel ────────────────────────────────────────────────────────────────────
CANCEL_REASONS = frozenset({"client_cancelled", "agency_cancelled", "departure_cancelled", "other"})


@dataclass(frozen=True)
class Cancellation:
    """A ``CancellationResult``: the fee Booking charges (in the quote currency), what it refunds the agency, and
    whether the booking is cancelled now (``cancelled_at``; None on a dry run)."""

    booking_id: str
    fee: Decimal
    currency: str
    refundable: Decimal
    fee_pct: str
    days_before_departure: int
    cancelled_at: dt.datetime | None


def cancellation(data: Any) -> Cancellation:
    body = _obj(data, "cancellation")
    fee, currency = money(body.get("fee"), "fee")
    refundable, refund_currency = money(body.get("refundable_b2b_amount"), "refundable_b2b_amount")
    if fee < 0 or refundable < 0 or refund_currency != currency:
        raise ContractError("fee / refundable_b2b_amount are not one non-negative currency")
    basis = _obj(body.get("fee_basis"), "fee_basis")
    pct = _str(basis, "fee_pct", re.compile(r"^\d+(\.\d{1,2})?$"))
    days = basis.get("days_before_departure")
    if not isinstance(days, int) or isinstance(days, bool):
        raise ContractError("fee_basis.days_before_departure is not an integer")
    raw_at = body.get("cancelled_at")
    return Cancellation(
        booking_id=_str(body, "booking_id", _ID),
        fee=fee,
        currency=currency,
        refundable=refundable,
        fee_pct=pct,
        days_before_departure=days,
        cancelled_at=None if raw_at is None else _datetime(raw_at, "cancelled_at"),
    )


# ── AA-1030 (contract 1.4): GET /tariff ─────────────────────────────────────────────────────────────────────────
_DISCOUNT = re.compile(r"^\d+(\.\d{1,4})?$")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIER = re.compile(r"^[a-z][a-z0-9_]{1,31}$")  # booking.partner_tiers.code
_SOURCE_ID = re.compile(r"^.{1,128}$")
TARIFF_BASES = frozenset({"per_person_band_min_twin_share"})
MAX_TARIFF_TEXT = 500


def _count(data: dict[str, Any], key: str, minimum: int = 1) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ContractError(f"{key} is missing or malformed")
    return value


def _count_or_none(data: dict[str, Any], key: str, minimum: int = 1) -> int | None:
    return None if data.get(key) is None else _count(data, key, minimum)


def _text(data: dict[str, Any], key: str) -> str | None:
    """An optional display text (title, country, season): None, or a string of at most 500 characters."""
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_TARIFF_TEXT:
        raise ContractError(f"{key} is malformed")
    return value


def _day(value: Any, name: str) -> dt.date:
    if not isinstance(value, str) or not _ISO_DAY.match(value):
        raise ContractError(f"{name} is not a date")
    return _date(value, name)


@dataclass(frozen=True)
class TariffItem:
    """One window x pax band of a tour (``TariffItem``): every departure date from ``valid_from`` to ``valid_to``
    prices the same. ``price`` is the whole line for ``priced_for_pax`` travellers, in the tariff currency."""

    source_id: str
    title: str | None
    country: str | None
    duration_days: int | None
    season: str | None
    valid_from: dt.date
    valid_to: dt.date
    pax_min: int
    pax_max: int | None
    priced_for_pax: int
    priced_for_date: dt.date
    price: Decimal
    price_per_person: Decimal
    agency_discount_pct: Decimal
    rrp_per_person: Decimal | None
    rrp_currency: str | None


@dataclass(frozen=True)
class Tariff:
    currency: str
    priced_on: dt.date
    basis: str
    tier_code: str
    rate_per_usd: Decimal
    rate_date: dt.date
    truncated: bool
    items: tuple[TariffItem, ...]


def _tariff_item(value: Any, currency: str) -> TariffItem:
    item = _obj(value, "tariff item")
    tour = _obj(item.get("tour"), "tour")
    price, price_currency = money(item.get("price"), "price")
    per_person, per_person_currency = money(item.get("price_per_person"), "price_per_person")
    if price_currency != currency or per_person_currency != currency or price < 0 or per_person < 0:
        raise ContractError("price / price_per_person are not in the tariff currency")
    rrp_amount: Decimal | None = None
    rrp_currency: str | None = None
    if item.get("rrp_per_person") is not None:  # AA's public price (price.from.v1): its own currency, may differ
        rrp_amount, rrp_currency = money(item.get("rrp_per_person"), "rrp_per_person")
        if rrp_amount < 0:
            raise ContractError("rrp_per_person is negative")
    pax_min = _count(item, "pax_min")
    pax_max = _count_or_none(item, "pax_max")
    if pax_max is not None and pax_max < pax_min:
        raise ContractError("pax_max is below pax_min")
    valid_from = _day(item.get("valid_from"), "valid_from")
    valid_to = _day(item.get("valid_to"), "valid_to")
    if valid_to < valid_from:
        raise ContractError("valid_to is before valid_from")
    discount = _decimal(item.get("agency_discount_pct"), _DISCOUNT, "agency_discount_pct")
    if discount >= 1:
        raise ContractError("agency_discount_pct is not a fraction below 1")
    return TariffItem(
        source_id=_str(tour, "source_id", _SOURCE_ID),
        title=_text(item, "title"),
        country=_text(item, "country"),
        duration_days=_count_or_none(item, "duration_days", minimum=0),
        season=_text(item, "season"),
        valid_from=valid_from,
        valid_to=valid_to,
        pax_min=pax_min,
        pax_max=pax_max,
        priced_for_pax=_count(item, "priced_for_pax"),
        priced_for_date=_day(item.get("priced_for_date"), "priced_for_date"),
        price=price,
        price_per_person=per_person,
        agency_discount_pct=discount,
        rrp_per_person=rrp_amount,
        rrp_currency=rrp_currency,
    )


def tariff(data: Any, currency: str | None = None) -> Tariff:
    """A 1.4 ``Tariff`` answer (the calling agency's own price per tour, season and pax band). Every row must read:
    a half-read tariff is never shown (the route answers 503, as a quote that breaks the contract). ``currency``: the
    one asked for; an answer in another currency is a :class:`ContractError`."""
    body = _obj(data, "tariff")
    tariff_currency = _str(body, "currency", _CURRENCY)
    if currency is not None and tariff_currency != currency:
        raise ContractError("the tariff is not in the asked currency")
    basis = _str(body, "basis")
    if basis not in TARIFF_BASES:
        raise ContractError("basis is unknown")
    tier = _str(body, "tier_code", _TIER)
    fx = _obj(body.get("fx"), "fx")
    rate = _decimal(fx.get("rate_per_usd"), _RATE, "fx.rate_per_usd")
    if rate <= 0:
        raise ContractError("fx.rate_per_usd is not positive")
    truncated = body.get("truncated")
    if not isinstance(truncated, bool):
        raise ContractError("truncated is not a boolean")
    items = body.get("items")
    if not isinstance(items, list):
        raise ContractError("items is not a list")
    return Tariff(
        currency=tariff_currency,
        priced_on=_day(body.get("priced_on"), "priced_on"),
        basis=basis,
        tier_code=tier,
        rate_per_usd=rate,
        rate_date=_date(fx.get("rate_date"), "fx.rate_date"),
        truncated=truncated,
        items=tuple(_tariff_item(x, tariff_currency) for x in items),
    )


# ── S270 (AA-1029 / AA-1030 follow-up): GET /fx-rates (contract 1.0) ───────────────────────────────────────────────
@dataclass(frozen=True)
class FxRates:
    """The FX rates Booking has approved and in force on ``on`` (``FxRates``): the currencies that have one (USD, the
    base, is never listed)."""

    on: dt.date
    currencies: frozenset[str]


def fx_rates(data: Any, on: dt.date) -> FxRates:
    """A ``FxRates`` answer for the day asked (``on``). Only what the CRM uses is checked: the day, the base currency
    and each rate's currency and positive ``rate_per_usd``; ``as_of`` and ``source`` are not read."""
    body = _obj(data, "fx rates")
    if _day(body.get("date"), "date") != on:
        raise ContractError("the rates are not for the asked day")
    if body.get("base_currency") != "USD":
        raise ContractError("base_currency is not USD")
    rates = body.get("rates")
    if not isinstance(rates, list):
        raise ContractError("rates is not a list")
    currencies: set[str] = set()
    for value in rates:
        rate = _obj(value, "rate")
        if _decimal(rate.get("rate_per_usd"), _RATE, "rate_per_usd") <= 0:
            raise ContractError("rate_per_usd is not positive")
        currencies.add(_str(rate, "currency", _CURRENCY))
    return FxRates(on=on, currencies=frozenset(currencies - {"USD"}))


# ── 1.5 (AA-1037): the agency's AA invoices and billing status (AA-1068, the "Bookings with AA" page) ───────────────
_INVOICE_NO = re.compile(r"^AA[ST]-\d{4}-\d{6}$")
_INVOICE_AMOUNT = re.compile(r"^-?\d+\.\d{2}$")
INVOICE_KINDS = frozenset({"saas", "settlement"})
INVOICE_STATUSES = frozenset({"issued", "paid", "void"})
CREDIT_STATUSES = frozenset({"ok", "warn", "past_due"})
LINE_ROLES = frozenset({"subscription", "extra_seats", "deposit", "balance", "full", "cancellation_fee", "adjustment"})


@dataclass(frozen=True)
class AgencyInvoiceSummary:
    number: str
    invoice_kind: str
    status: str
    currency: str
    total: Decimal
    balance: Decimal
    issued_on: dt.date
    due_on: dt.date
    overdue_days: int


@dataclass(frozen=True)
class AgencyInvoiceLine:
    line_no: int
    line_role: str
    description: str
    amount: Decimal
    hold_id: str | None
    booking_id: str | None
    voided: bool


@dataclass(frozen=True)
class AgencyInvoice:
    summary: AgencyInvoiceSummary
    lines: tuple[AgencyInvoiceLine, ...]
    payments: tuple[tuple[dt.date, Decimal, str], ...]
    payment_instructions: str | None
    invoice_inbox: str | None


@dataclass(frozen=True)
class BillingStatus:
    status: str
    holds_paused: bool
    overdue_invoices: int
    oldest_overdue_days: int
    overdue_amount: tuple[tuple[Decimal, str], ...]
    paid_through: dt.date | None


def _enum(data: dict[str, Any], key: str, allowed: frozenset[str]) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"{key} is unknown")
    return value


def _invoice_summary(value: Any) -> AgencyInvoiceSummary:
    data = _obj(value, "invoice")
    overdue = data.get("overdue_days")
    if not isinstance(overdue, int) or isinstance(overdue, bool) or overdue < 0:
        raise ContractError("overdue_days is not a count")
    return AgencyInvoiceSummary(
        number=_str(data, "number", _INVOICE_NO),
        invoice_kind=_enum(data, "invoice_kind", INVOICE_KINDS),
        status=_enum(data, "status", INVOICE_STATUSES),
        currency=_str(data, "currency", _CURRENCY),
        total=_decimal(data.get("total"), _INVOICE_AMOUNT, "total"),
        balance=_decimal(data.get("balance"), _INVOICE_AMOUNT, "balance"),
        issued_on=_date(data.get("issued_on"), "issued_on"),
        due_on=_date(data.get("due_date"), "due_date"),
        overdue_days=min(overdue, 36500),
    )


def agency_invoice_page(data: Any) -> tuple[list[AgencyInvoiceSummary], int]:
    """``GET /invoices``: the page's invoices and the total that match (a broken item fails the page)."""
    page = _obj(data, "invoice page")
    items = page.get("items")
    total = page.get("total")
    if not isinstance(items, list) or not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ContractError("items or total is malformed")
    return [_invoice_summary(item) for item in items], total


def agency_invoice(data: Any) -> AgencyInvoice:
    """``GET /invoices/{number}``: the invoice with its lines, approved payments and the issuer's payment text."""
    body = _obj(data, "invoice")
    raw_lines, raw_payments = body.get("lines"), body.get("payments")
    if not isinstance(raw_lines, list) or not isinstance(raw_payments, list):
        raise ContractError("lines or payments is not a list")
    lines = []
    for item in raw_lines:
        line = _obj(item, "line")
        number = line.get("line_no")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ContractError("line_no is not a count")
        voided = line.get("voided")
        if not isinstance(voided, bool):
            raise ContractError("voided is not a boolean")
        description = line.get("description")
        lines.append(
            AgencyInvoiceLine(
                line_no=number,
                line_role=_enum(line, "line_role", LINE_ROLES),
                description=description[:300] if isinstance(description, str) else "",
                amount=_decimal(line.get("amount"), _INVOICE_AMOUNT, "amount"),
                hold_id=_str(line, "hold_id", _ID, optional=True),
                booking_id=_str(line, "booking_id", _ID, optional=True),
                voided=voided,
            )
        )
    payments = []
    for item in raw_payments:
        pay = _obj(item, "payment")
        payments.append(
            (_date(pay.get("received_on"), "received_on"),
             _decimal(pay.get("amount"), _INVOICE_AMOUNT, "payment.amount"), _str(pay, "currency", _CURRENCY))
        )  # fmt: skip
    issuer = body.get("issuer")
    instructions = inbox = None
    if isinstance(issuer, dict):
        text = issuer.get("payment_instructions")
        instructions = text[:2000] if isinstance(text, str) else None
        mail = issuer.get("invoice_inbox")
        inbox = mail[:254] if isinstance(mail, str) else None
    return AgencyInvoice(_invoice_summary(body), tuple(lines), tuple(payments), instructions, inbox)


def billing_status(data: Any) -> BillingStatus:
    """``GET /billing-status``: the credit status (holds paused while past_due) and the software fee's paid day."""
    body = _obj(data, "billing status")
    paused = body.get("holds_paused")
    if not isinstance(paused, bool):
        raise ContractError("holds_paused is not a boolean")
    amounts = body.get("overdue_amount")
    if not isinstance(amounts, list):
        raise ContractError("overdue_amount is not a list")
    paid = body.get("paid_through")
    return BillingStatus(
        status=_enum(body, "status", CREDIT_STATUSES),
        holds_paused=paused,
        overdue_invoices=_count(body, "overdue_invoices", 0),
        oldest_overdue_days=_count(body, "oldest_overdue_days", 0),
        overdue_amount=tuple(money(a, "overdue_amount") for a in amounts),
        paid_through=None if paid is None else _date(paid, "paid_through"),
    )
