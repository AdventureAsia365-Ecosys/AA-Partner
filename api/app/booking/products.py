"""The add form's live look-up of one AA product (AA-947): its bookable components, its fixed departures and AA's
public "from" price in the chosen currency (the warning shows before the agent asks for a quote).

``GET /products`` has no filter by tour, so the agency's product list is read page by page (at most :data:`MAX_PAGES`)
and kept in memory for :data:`TTL_S` per agency (the offer changes rarely; the quote is the price that counts). Booking
down is 503 ``booking_unavailable`` (the form says AA cannot be reached; nothing is stored); an agency AA has not
activated as a B2B partner is 409 ``aa_partner_not_active``.

AA-1092: :meth:`ProductCache.departures` keeps a tour's departures answer for :data:`DEPARTURES_TTL_S` per (agency,
tour, date window), for the public "Book now" widget only (``app.intake.booking.widget``): every page view of an
anonymous visitor would otherwise ask Booking. Its ``before_fetch`` runs only when Booking is asked (the agency's
per-minute ceiling); the price check and the booking still read departures live. Errors are never kept.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.errors import Conflict, NotFound, Unavailable

TTL_S = 300
MAX_PAGES = 20
DEPARTURE_DAYS = 365
DEPARTURES_TTL_S = 30
MAX_DEPARTURE_KEYS = 2048


def _booking_error(exc: Exception) -> Exception:
    if isinstance(exc, BookingRefusal) and exc.code in ("unknown_partner", "forbidden"):
        return Conflict("aa_partner_not_active", "AA has not activated your agency for AA products yet")
    return Unavailable("booking_unavailable", "AA cannot be reached right now; try again in a minute")


class ProductCache:
    def __init__(self, client: BookingClient, *, ttl_s: float = TTL_S, clock: Callable[[], float] = time.monotonic):
        self._client = client
        self._ttl_s = ttl_s
        self._clock = clock
        self._cache: dict[uuid.UUID, tuple[float, dict[str, contract.Product]]] = {}
        self._departures: OrderedDict[tuple[uuid.UUID, str, str, str], tuple[float, dict[str, Any]]] = OrderedDict()

    async def products(self, tenant_id: uuid.UUID) -> dict[str, contract.Product]:
        cached = self._cache.get(tenant_id)
        if cached is not None and self._clock() < cached[0]:
            return cached[1]
        found: dict[str, contract.Product] = {}
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            items, cursor = contract.products_page(await self._client.products(tenant_id, cursor=cursor))
            for product in items:
                if product.sellable:
                    found[product.source_id] = product
            if not cursor:
                break
        self._cache[tenant_id] = (self._clock() + self._ttl_s, found)
        return found

    async def departures(
        self,
        tenant_id: uuid.UUID,
        source_id: str,
        *,
        date_from: str,
        date_to: str,
        before_fetch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Booking's ``GET /departures`` answer of one tour, kept :data:`DEPARTURES_TTL_S` (AA-1092, the public widget).

        ``before_fetch`` runs only on a miss, right before Booking is asked (it may raise, e.g. the agency's ceiling);
        at most :data:`MAX_DEPARTURE_KEYS` answers are kept (the least recently used goes first)."""
        key = (tenant_id, source_id, date_from, date_to)
        cached = self._departures.get(key)
        if cached is not None and self._clock() < cached[0]:
            self._departures.move_to_end(key)
            return cached[1]
        if before_fetch is not None:
            before_fetch()
        data = await self._client.departures(tenant_id, source_id, date_from=date_from, date_to=date_to)
        self._departures[key] = (self._clock() + DEPARTURES_TTL_S, data)
        self._departures.move_to_end(key)
        while len(self._departures) > MAX_DEPARTURE_KEYS:
            self._departures.popitem(last=False)
        return data


async def lookup(
    cache: ProductCache,
    client: BookingClient,
    tenant_id: uuid.UUID,
    source_id: str,
    currency: str | None,
    *,
    today: dt.date,
) -> dict[str, Any]:
    try:
        product = (await cache.products(tenant_id)).get(source_id)
        if product is None:
            raise NotFound("aa_product_not_found", "AA does not sell this tour to agencies yet")
        departures: list[dict[str, Any]] = []
        if product.has_fixed_departures:
            data = await client.departures(
                tenant_id,
                source_id,
                date_from=today.isoformat(),
                date_to=(today + dt.timedelta(days=DEPARTURE_DAYS)).isoformat(),
            )
            departures = [
                {
                    "departure_id": d.departure_id,
                    "start_date": d.start_date.isoformat(),
                    "available": d.available,
                    "status": d.status,
                    "auto_confirm": d.auto_confirm,
                }
                for d in contract.departures(data)
                if d.status != "cancelled"
            ]
        from_price = None
        if currency:
            try:
                amount = contract.from_price(await client.price_parity(tenant_id, source_id, currency), currency)
                from_price = {"amount": str(amount), "currency": currency} if amount is not None else None
            except (BookingUnavailable, BookingRefusal, contract.ContractError):
                from_price = None  # a warning only: never fails the look-up
    except (BookingUnavailable, BookingRefusal, contract.ContractError) as exc:
        raise _booking_error(exc) from None
    return {
        "tour_source_id": source_id,
        "has_fixed_departures": product.has_fixed_departures,
        "components": [
            {"component_id": c.component_id, "kind": c.kind, "day_no": c.day_no} for c in product.components
        ],
        "departures": departures,
        "from_price": from_price,
    }
