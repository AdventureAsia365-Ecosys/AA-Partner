"""The agency's AA tariff in the CRM (AA-1030; booking.b2b.v1 1.4 ``GET /tariff``): AA's B2B price for this agency per
tour, season and pax band, with its tier or contract rate card discount, read live from Booking.

- **Who.** :data:`app.permissions.Action.AA_TARIFF_VIEW` (owner and agent, like ``VIEW_MARGIN``; the assistant 403).
  The tariff is the B2B price, and the margin serializer neither matches Booking's key names (``price``,
  ``price_per_person``) nor sees the CSV bytes, so the action gate is the guard.
- **JSON** (:func:`view`): Booking's rows read with :func:`app.booking.contract.tariff` (a row that breaks the contract
  fails the whole answer: 503, nothing half-shown), the price keys renamed ``b2b_price`` / ``b2b_price_per_person`` so
  the serializer strips them for a role without ``view_margin`` too (defence in depth). ``agency_discount_pct`` and AA's
  public ``rrp_per_person`` keep their names.
- **CSV**: Booking's own file (its ``tariff.CSV_COLUMNS``), passed on unchanged with Booking's file name; the CRM never
  builds a second file shape.
- **Errors** (:func:`booking_error`): ``unknown_partner`` / ``forbidden`` (no B2B partner row for the agency, or AA
  suspended it) -> 409 ``aa_partner_not_active``; ``invalid`` (not a quote currency) -> 422 ``aa_tariff_invalid``;
  any other refusal -> 422 ``aa_tariff_refused`` with Booking's code; Booking down, a contract break or a CSV that is
  not one -> 503 ``booking_unavailable`` with ``Retry-After``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.errors import Conflict, Invalid, ServiceError, UnavailableRetry

RETRY_AFTER_S = 60
CURRENCIES = ("USD", "EUR", "GBP", "AUD", "SGD", "VND")  # booking.b2b.v1 1.4: the quote currencies (the web's select)


def booking_error(exc: Exception) -> ServiceError:
    """The CRM Problem for a Booking failure of a tariff read (codes only, never Booking's message)."""
    if isinstance(exc, BookingRefusal):
        if exc.code in ("unknown_partner", "forbidden"):
            return Conflict(
                "aa_partner_not_active", "Your agency is not linked to AA B2B, or AA has suspended the link"
            )
        if exc.code == "invalid":
            return Invalid("aa_tariff_invalid", "AA cannot price its tariff in this currency")
        return Invalid("aa_tariff_refused", "AA refused the tariff request", aa_code=exc.code)
    return UnavailableRetry(
        "booking_unavailable", RETRY_AFTER_S, "AA cannot be reached right now; try again in a minute"
    )


def _money(amount: Decimal, currency: str) -> dict[str, str]:
    return {"amount": str(amount), "currency": currency}


def view(tariff: contract.Tariff) -> dict[str, Any]:
    """The CRM's JSON of a read tariff (B2B prices under ``b2b_*`` names)."""
    return {
        "currency": tariff.currency,
        "priced_on": tariff.priced_on.isoformat(),
        "tier_code": tariff.tier_code,
        "basis": tariff.basis,
        "rate_date": tariff.rate_date.isoformat(),
        "truncated": tariff.truncated,
        "items": [
            {
                "source_id": item.source_id,
                "title": item.title,
                "country": item.country,
                "duration_days": item.duration_days,
                "season": item.season,
                "valid_from": item.valid_from.isoformat(),
                "valid_to": item.valid_to.isoformat(),
                "pax_min": item.pax_min,
                "pax_max": item.pax_max,
                "priced_for_pax": item.priced_for_pax,
                "b2b_price": _money(item.price, tariff.currency),
                "b2b_price_per_person": _money(item.price_per_person, tariff.currency),
                "agency_discount_pct": str(item.agency_discount_pct),
                "rrp_per_person": (
                    None
                    if item.rrp_per_person is None or item.rrp_currency is None
                    else _money(item.rrp_per_person, item.rrp_currency)
                ),
            }
            for item in tariff.items
        ],
    }


async def read(
    client: BookingClient, tenant_id: uuid.UUID, *, currency: str, source_id: str | None = None
) -> dict[str, Any]:
    """The agency's tariff as the CRM's JSON. Raises the :func:`booking_error` Problem when Booking cannot answer."""
    try:
        data = await client.tariff(tenant_id, currency=currency, source_id=source_id)
        return view(contract.tariff(data, currency))
    except (BookingRefusal, BookingUnavailable, contract.ContractError) as exc:
        raise booking_error(exc) from None


def fallback_filename(currency: str, today: dt.date) -> str:
    return f"aa-tariff-{currency.lower()}-{today.isoformat()}.csv"


async def csv_file(
    client: BookingClient, tenant_id: uuid.UUID, *, currency: str, today: dt.date, source_id: str | None = None
) -> tuple[bytes, str]:
    """Booking's CSV file of the agency's tariff and the name to save it under (Booking's, else the CRM's)."""
    try:
        body, name = await client.tariff_csv(tenant_id, currency=currency, source_id=source_id)
    except (BookingRefusal, BookingUnavailable) as exc:
        raise booking_error(exc) from None
    return body, name or fallback_filename(currency, today)
