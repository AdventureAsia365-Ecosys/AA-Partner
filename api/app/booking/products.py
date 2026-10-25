"""The add form's live look-up of one AA product (AA-947): its bookable components, its fixed departures and AA's
public "from" price in the chosen currency (the warning shows before the agent asks for a quote).

``GET /products`` has no filter by tour, so the agency's product list is read page by page (at most :data:`MAX_PAGES`)
and kept in memory for :data:`TTL_S` per agency (the offer changes rarely; the quote is the price that counts). Booking
down is 503 ``booking_unavailable`` (the form says AA cannot be reached; nothing is stored); an agency AA has not
activated as a B2B partner is 409 ``aa_partner_not_active``.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections.abc import Callable
from typing import Any

from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.errors import Conflict, NotFound, Unavailable

TTL_S = 300
MAX_PAGES = 20
DEPARTURE_DAYS = 365


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
