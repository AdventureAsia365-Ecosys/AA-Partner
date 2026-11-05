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
- **Bounded bulk reads** (:class:`TariffReader`, S270 review): one read buffers up to 8 MiB from Booking and holds a
  thread of the shared ``asyncio.to_thread`` pool for up to 25 s, so the route never calls Booking directly. At most
  :data:`MAX_BULK_READS` Booking tariff reads run at once in the process and at most one per agency; a read that
  would go over either is answered 429 ``aa_tariff_busy`` with ``Retry-After`` (:data:`RETRY_AFTER_BUSY_S`) and never
  reaches Booking. The same ask (agency, currency, format, tour) while it is in flight waits for that read instead of
  sending a second one. A finished answer is kept for :data:`CACHE_TTL_S` as the bytes the route sends (JSON already
  encoded, CSV as Booking's file), bounded by :data:`CACHE_MAX_BYTES` in all, so page reloads, a currency switched back
  and forth or a repeated Download CSV do not ask Booking for the whole tariff again. The browser still gets
  ``Cache-Control: no-store``. Process memory, like :mod:`app.booking.products`: each task counts on its own.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from app.booking import contract
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.errors import Conflict, Invalid, RateLimited, ServiceError, UnavailableRetry

RETRY_AFTER_S = 60
# The bound on bulk reads (see the module docstring): Booking tariff reads in flight per process, the agency's wait
# before it asks again, and the short cache of finished answers (seconds, bytes in all).
MAX_BULK_READS = 2
RETRY_AFTER_BUSY_S = 5
CACHE_TTL_S = 60.0
CACHE_MAX_BYTES = 24 * 1024 * 1024
BUSY = "aa_tariff_busy"
JSON_MEDIA = "application/json"
CSV_MEDIA = "text/csv; charset=utf-8"
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


def tariff_busy() -> RateLimited:
    return RateLimited(BUSY, RETRY_AFTER_BUSY_S, "AA prices are already being read for your agency; try again shortly")


def encode(view_: dict[str, Any]) -> bytes:
    """The JSON bytes of a tariff view, encoded like FastAPI's ``JSONResponse`` (the margin serializer reads them)."""
    return json.dumps(view_, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")).encode()


@dataclass(frozen=True)
class TariffFile:
    """A finished tariff answer as the route sends it: the body, its media type and (CSV) the file name."""

    body: bytes
    media_type: str
    filename: str | None = None


_Key = tuple[uuid.UUID, str, str, str]


class TariffReader:
    """The route's only way to Booking's tariff: bounded reads in flight, one per agency, and a short cache."""

    def __init__(
        self,
        client: BookingClient,
        *,
        max_reads: int = MAX_BULK_READS,
        ttl_s: float = CACHE_TTL_S,
        max_bytes: int = CACHE_MAX_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_reads < 1:
            raise ValueError("max_reads must be >= 1")
        self._client = client
        self._max_reads = max_reads
        self._ttl_s = ttl_s
        self._max_bytes = max_bytes
        self._clock = clock
        self._inflight: dict[_Key, asyncio.Future[TariffFile]] = {}
        self._tenants: set[uuid.UUID] = set()
        self._cache: OrderedDict[_Key, tuple[float, TariffFile]] = OrderedDict()
        self._bytes = 0

    def in_flight(self) -> int:
        """Booking tariff reads running right now."""
        return len(self._inflight)

    def cached_bytes(self) -> int:
        return self._bytes

    async def get(
        self,
        tenant_id: uuid.UUID,
        *,
        currency: str,
        fmt: Literal["json", "csv"],
        today: dt.date,
        source_id: str | None = None,
    ) -> TariffFile:
        """The answer for this ask: from the cache, joined to the same read in flight, or a new Booking read.
        Raises 429 ``aa_tariff_busy`` (never reaching Booking) when the agency already has another read in flight or
        the process has :data:`MAX_BULK_READS`; the :func:`booking_error` Problem when Booking cannot answer."""
        key: _Key = (tenant_id, currency, fmt, source_id or "")
        hit = self._cached(key)
        if hit is not None:
            return hit
        running = self._inflight.get(key)
        if running is not None:
            return await asyncio.shield(running)
        if tenant_id in self._tenants or len(self._inflight) >= self._max_reads:
            raise tariff_busy()
        future: asyncio.Future[TariffFile] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        self._tenants.add(tenant_id)
        try:
            result = await self._fetch(tenant_id, currency, fmt, today, source_id)
        except BaseException as exc:
            # The asks that joined this read get the same Problem; a cancelled read (the caller went away) tells them
            # Booking did not answer rather than cancelling them.
            shared = exc if isinstance(exc, ServiceError) else booking_error(BookingUnavailable("booking_cancelled"))
            future.set_exception(shared)
            future.exception()  # retrieved here, so an ask nobody joined logs no "never retrieved" warning
            raise
        else:
            future.set_result(result)
            self._store(key, result)
            return result
        finally:
            del self._inflight[key]
            self._tenants.discard(tenant_id)

    async def _fetch(
        self, tenant_id: uuid.UUID, currency: str, fmt: str, today: dt.date, source_id: str | None
    ) -> TariffFile:
        if fmt == "csv":
            body, name = await csv_file(self._client, tenant_id, currency=currency, source_id=source_id, today=today)
            return TariffFile(body, CSV_MEDIA, name)
        view_ = await read(self._client, tenant_id, currency=currency, source_id=source_id)
        return TariffFile(encode(view_), JSON_MEDIA)

    def _cached(self, key: _Key) -> TariffFile | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if self._clock() >= entry[0]:
            self._drop(key)
            return None
        self._cache.move_to_end(key)
        return entry[1]

    def _store(self, key: _Key, result: TariffFile) -> None:
        size = len(result.body)
        if size > self._max_bytes // 2:
            return  # one answer never takes most of the budget: it is read again next time
        self._drop(key)
        now = self._clock()
        for old in [k for k, (expires, _) in self._cache.items() if now >= expires]:
            self._drop(old)
        while self._cache and self._bytes + size > self._max_bytes:
            self._drop(next(iter(self._cache)))
        self._cache[key] = (now + self._ttl_s, result)
        self._bytes += size

    def _drop(self, key: _Key) -> None:
        entry = self._cache.pop(key, None)
        if entry is not None:
            self._bytes -= len(entry[1].body)
