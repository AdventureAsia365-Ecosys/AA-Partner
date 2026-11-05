"""AA-1030 S270 review: the agency tariff's bounded bulk reads (:class:`app.booking.tariff.TariffReader`).

At most ``max_reads`` Booking tariff reads in flight per process and one per agency (429 ``aa_tariff_busy`` with
``Retry-After``, Booking never asked), the same ask joins the read in flight, and a finished answer is kept a short
while within a byte budget. Invented agencies and tours only.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from typing import Any

import pytest

from app.booking import tariff as aa_tariff
from app.booking.client import BookingRefusal, BookingUnavailable
from app.errors import Conflict, RateLimited, UnavailableRetry
from tests.aa_fakes import TARIFF_TOUR, tariff_body, tariff_csv

TODAY = dt.date(2031, 1, 5)
AGENCY_A = uuid.UUID("a1000000-0000-4000-8000-00000000a030")
AGENCY_B = uuid.UUID("b1000000-0000-4000-8000-00000000b030")
AGENCY_C = uuid.UUID("c1000000-0000-4000-8000-00000000c030")


class GatedBooking:
    """A Booking client whose tariff reads wait for :attr:`release` (every call counted, ``fail`` raised if set)."""

    def __init__(self, *, open_: bool = False) -> None:
        self.calls: list[tuple[str, uuid.UUID, str, str | None]] = []
        self.release = asyncio.Event()
        if open_:
            self.release.set()
        self.fail: Exception | None = None

    async def _wait(self) -> None:
        await self.release.wait()
        if self.fail is not None:
            raise self.fail

    async def tariff(self, tenant_id: uuid.UUID, *, currency: str, source_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("json", tenant_id, currency, source_id))
        await self._wait()
        return tariff_body(currency, source_id=source_id)

    async def tariff_csv(
        self, tenant_id: uuid.UUID, *, currency: str, source_id: str | None = None
    ) -> tuple[bytes, str | None]:
        self.calls.append(("csv", tenant_id, currency, source_id))
        await self._wait()
        return tariff_csv(currency), f"invented-{currency.lower()}.csv"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _reader(booking: GatedBooking, **kw: Any) -> aa_tariff.TariffReader:
    return aa_tariff.TariffReader(booking, **kw)  # type: ignore[arg-type]  # the duck-typed fake client


def _get(reader: aa_tariff.TariffReader, tenant: uuid.UUID, currency: str = "USD", fmt: str = "json", **kw: Any):
    return reader.get(tenant, currency=currency, fmt=fmt, today=TODAY, **kw)  # type: ignore[arg-type]


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def _busy(results: list[Any]) -> list[Any]:
    return [r for r in results if isinstance(r, RateLimited) and r.code == aa_tariff.BUSY]


@pytest.mark.asyncio
async def test_one_agency_gets_one_read_in_flight_and_the_rest_429_without_reaching_booking() -> None:
    booking = GatedBooking()
    reader = _reader(booking, max_reads=2)
    first = asyncio.create_task(_get(reader, AGENCY_A, "USD"))
    await _settle()
    # N + 1 parallel asks of one agency (other currencies and the CSV): only the first reaches Booking.
    others = [_get(reader, AGENCY_A, c) for c in ("EUR", "GBP", "AUD")] + [_get(reader, AGENCY_A, "USD", "csv")]
    results = await asyncio.gather(*others, return_exceptions=True)
    assert len(_busy(results)) == 4
    assert all(r.status_code == 429 and r.retry_after_s == aa_tariff.RETRY_AFTER_BUSY_S for r in _busy(results))
    assert len(booking.calls) == 1 and reader.in_flight() == 1
    booking.release.set()
    out = await first
    assert out.media_type == aa_tariff.JSON_MEDIA and json.loads(out.body)["currency"] == "USD"
    assert reader.in_flight() == 0
    # The agency's slot is free again once its read finished.
    assert json.loads((await _get(reader, AGENCY_A, "EUR")).body)["currency"] == "EUR"


@pytest.mark.asyncio
async def test_the_process_runs_at_most_max_reads_and_a_calm_agency_waits_for_none_of_them() -> None:
    booking = GatedBooking()
    reader = _reader(booking, max_reads=2)
    running = [asyncio.create_task(_get(reader, t)) for t in (AGENCY_A, AGENCY_B)]
    await _settle()
    with pytest.raises(RateLimited) as err:
        await _get(reader, AGENCY_C)
    assert (err.value.status_code, err.value.code) == (429, "aa_tariff_busy")
    assert len(booking.calls) == 2 and reader.in_flight() == 2
    booking.release.set()
    await asyncio.gather(*running)
    await _get(reader, AGENCY_C)
    assert len(booking.calls) == 3


@pytest.mark.asyncio
async def test_the_same_ask_in_flight_is_joined_not_sent_again() -> None:
    booking = GatedBooking()
    reader = _reader(booking)
    asks = [asyncio.create_task(_get(reader, AGENCY_A, "SGD", "csv")) for _ in range(5)]
    await _settle()
    booking.release.set()
    files = await asyncio.gather(*asks)
    assert len(booking.calls) == 1
    assert {f.body for f in files} == {tariff_csv("SGD")}
    assert {(f.media_type, f.filename) for f in files} == {(aa_tariff.CSV_MEDIA, "invented-sgd.csv")}


@pytest.mark.asyncio
async def test_a_finished_answer_is_kept_for_the_ttl_per_agency_currency_format_and_tour() -> None:
    booking = GatedBooking(open_=True)
    clock = Clock()
    reader = _reader(booking, ttl_s=60, clock=clock)
    first = await _get(reader, AGENCY_A, "EUR")
    assert await _get(reader, AGENCY_A, "EUR") is first
    assert len(booking.calls) == 1
    # Each part of the ask is its own entry: another agency, currency, format or tour reads Booking.
    await _get(reader, AGENCY_B, "EUR")
    await _get(reader, AGENCY_A, "GBP")
    await _get(reader, AGENCY_A, "EUR", "csv")
    await _get(reader, AGENCY_A, "EUR", source_id=TARIFF_TOUR)
    assert len(booking.calls) == 5
    clock.now += 59
    await _get(reader, AGENCY_A, "EUR")
    assert len(booking.calls) == 5
    clock.now += 1
    again = await _get(reader, AGENCY_A, "EUR")
    assert again is not first and len(booking.calls) == 6


@pytest.mark.asyncio
async def test_the_json_answer_is_the_view_encoded_like_fastapi() -> None:
    reader = _reader(GatedBooking(open_=True))
    out = await _get(reader, AGENCY_A, "AUD")
    view = json.loads(out.body)
    assert view["tier_code"] == "silver" and len(view["items"]) == 2
    assert view["items"][0]["b2b_price_per_person"] == {"amount": "388.00", "currency": "AUD"}
    assert out.body == aa_tariff.encode(view) and b": " not in out.body and out.filename is None


@pytest.mark.asyncio
async def test_the_cache_stays_within_its_byte_budget() -> None:
    booking = GatedBooking(open_=True)
    one = len((await _get(_reader(GatedBooking(open_=True)), AGENCY_A, "USD", "csv")).body)
    reader = _reader(booking, max_bytes=one * 2 + 1)
    for currency in ("USD", "EUR", "GBP"):
        await _get(reader, AGENCY_A, currency, "csv")
    assert reader.cached_bytes() <= one * 2 + 1
    # The oldest entry went first; the two newest stay.
    await _get(reader, AGENCY_A, "GBP", "csv")
    await _get(reader, AGENCY_A, "EUR", "csv")
    assert len(booking.calls) == 3
    await _get(reader, AGENCY_A, "USD", "csv")
    assert len(booking.calls) == 4


@pytest.mark.asyncio
async def test_an_answer_bigger_than_half_the_budget_is_not_kept() -> None:
    booking = GatedBooking(open_=True)
    reader = _reader(booking, max_bytes=100)
    await _get(reader, AGENCY_A)
    await _get(reader, AGENCY_A)
    assert len(booking.calls) == 2 and reader.cached_bytes() == 0


@pytest.mark.asyncio
async def test_expired_entries_are_dropped_when_a_new_one_is_stored() -> None:
    clock = Clock()
    reader = _reader(GatedBooking(open_=True), ttl_s=60, clock=clock)
    await _get(reader, AGENCY_A, "USD", "csv")
    size = reader.cached_bytes()
    clock.now += 61
    await _get(reader, AGENCY_B, "USD", "csv")
    assert reader.cached_bytes() == size  # the expired entry of agency A is gone


@pytest.mark.parametrize(
    ("fail", "kind", "code"),
    [
        (BookingRefusal(403, "unknown_partner"), Conflict, "aa_partner_not_active"),
        (BookingUnavailable("booking_timeout"), UnavailableRetry, "booking_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_a_failed_read_is_shared_with_its_joiners_never_kept_and_frees_its_slots(
    fail: Exception, kind: type, code: str
) -> None:
    booking = GatedBooking()
    booking.fail = fail
    reader = _reader(booking, max_reads=1)
    asks = [asyncio.create_task(_get(reader, AGENCY_A, "VND")) for _ in range(3)]
    await _settle()
    booking.release.set()
    results = await asyncio.gather(*asks, return_exceptions=True)
    assert all(isinstance(r, kind) and r.code == code for r in results)
    assert len(booking.calls) == 1 and reader.in_flight() == 0 and reader.cached_bytes() == 0
    booking.fail = None
    await _get(reader, AGENCY_B, "VND")  # the process slot and agency A's are both free again
    await _get(reader, AGENCY_A, "VND")
    assert len(booking.calls) == 3


@pytest.mark.asyncio
async def test_a_cancelled_read_tells_its_joiners_booking_did_not_answer() -> None:
    booking = GatedBooking()
    reader = _reader(booking)
    leader = asyncio.create_task(_get(reader, AGENCY_A))
    await _settle()
    joiner = asyncio.create_task(_get(reader, AGENCY_A))
    await _settle()
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    with pytest.raises(UnavailableRetry) as err:
        await joiner
    assert err.value.code == "booking_unavailable" and reader.in_flight() == 0
    booking.release.set()
    await _get(reader, AGENCY_A)
    assert len(booking.calls) == 2


def test_the_reader_needs_at_least_one_read() -> None:
    with pytest.raises(ValueError, match="max_reads"):
        aa_tariff.TariffReader(object(), max_reads=0)  # type: ignore[arg-type]


def test_the_defaults_bound_a_whole_tariff() -> None:
    assert aa_tariff.MAX_BULK_READS == 2 and aa_tariff.CACHE_TTL_S == 60
    assert aa_tariff.CACHE_MAX_BYTES == 24 * 1024 * 1024 and aa_tariff.RETRY_AFTER_BUSY_S == 5
    assert aa_tariff.tariff_busy().retry_after_s == 5
