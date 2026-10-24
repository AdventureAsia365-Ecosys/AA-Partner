"""``crm_fx_fetch`` (AA-944, design v3 R17 §3.1 item 5): the daily platform FX rates in ``crm.fx_rate_platform``.

One run per UTC day, queued due now by :class:`DailyFxSchedule` (idempotency key ``crm_fx_fetch:<date>``), never
queued ahead, so it is not deploy-gate exempt and blocks a deploy only while it really runs. ``FX_PROVIDER``
(``app.settings``):

- ``synthetic`` (default on dev / test / local): :data:`SYNTHETIC_USD`, a fixed table quoted against USD (VND
  included), source ``synthetic``. No network. Every Dev confirm freezes these.
- ``live``: free sources, no key, no new AWS resource (the task's NAT egress):
  * ExchangeRate-API open access (``open.er-api.com``, USD base: covers VND and the other Asian currencies ECB lacks),
    source ``exchangerate_api``. Its terms require the attribution "Rates By Exchange Rate API" wherever such a rate
    is shown (``app.ledger.fx.ATTRIBUTION``) and forbid redistribution: the CRM never exposes a rates feed;
  * ECB euro reference rates (EUR base), source ``ecb``;
  * fawazahmed0 currency-api (CC0, jsDelivr), source ``fawazahmed0``: stored as a cross-check only
    (``crm.fx_resolve`` never uses it). A USD cross more than :data:`CROSS_CHECK_GAP` (1 %) away from ExchangeRate-API
    is logged (``crm_fx_cross_check_gap``, currency codes only) and counted in ``flagged_count``.
  A 429 or a network error fails the attempt; the worker retries with backoff (``MAX_ATTEMPTS``). One source failing
  does not stop the others: the run fails only when no source answered.

Rows are written with ``ON CONFLICT DO NOTHING`` (a rerun of the day changes nothing) — this module is the only writer
of ``crm.fx_rate_platform`` (``tests/unit/test_code_rules.py``). Results are counts only (payload guard).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET  # noqa: S405 — parses the ECB file we fetched, no external entities (stdlib)
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app.jobs import queue
from app.jobs.kinds import FX_FETCH
from app.jobs.queue import Job
from app.jobs.worker import Handler

log = logging.getLogger(__name__)

SCHEDULER = "scheduler:crm-fx-fetch"
MAX_ATTEMPTS = 4
HISTORY_DAYS = 30
FETCH_TIMEOUT_S = 10.0
MAX_BYTES = 1_048_576
CROSS_CHECK_GAP = Decimal("0.01")

ER_API_URL = "https://open.er-api.com/v6/latest/USD"
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FAWAZ_URL = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json"
ECB_NS = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"

# The currencies the CRM's agencies trade in (Asia and the usual client currencies). Others are skipped.
CURRENCIES = frozenset(
    {
        "AUD", "CAD", "CHF", "CNY", "EUR", "GBP", "HKD", "IDR", "INR", "JPY", "KHR", "KRW", "LAK", "MMK", "MYR",
        "NZD", "PHP", "SGD", "THB", "TWD", "USD", "VND",
    }
)  # fmt: skip
# Dev: one fixed rate per currency against USD (1 USD = rate quote), roughly 2026 levels; deterministic.
SYNTHETIC_USD: dict[str, Decimal] = {
    "AUD": Decimal("1.52"),
    "CAD": Decimal("1.37"),
    "CHF": Decimal("0.88"),
    "CNY": Decimal("7.20"),
    "EUR": Decimal("0.92"),
    "GBP": Decimal("0.79"),
    "HKD": Decimal("7.80"),
    "IDR": Decimal("15800"),
    "INR": Decimal("83.5"),
    "JPY": Decimal("150"),
    "KHR": Decimal("4100"),
    "KRW": Decimal("1350"),
    "LAK": Decimal("21000"),
    "MMK": Decimal("2100"),
    "MYR": Decimal("4.70"),
    "NZD": Decimal("1.66"),
    "PHP": Decimal("56"),
    "SGD": Decimal("1.35"),
    "THB": Decimal("36.5"),
    "TWD": Decimal("32"),
    "VND": Decimal("25400"),
}

INSERT_SQL = """
INSERT INTO crm.fx_rate_platform (rate_date, base, quote, source, rate)
SELECT * FROM unnest($1::date[], $2::text[], $3::text[], $4::text[], $5::numeric[])
ON CONFLICT DO NOTHING
"""


@dataclass(frozen=True)
class RateRow:
    rate_date: dt.date
    base: str
    quote: str
    source: str
    rate: Decimal


class FxFetchError(RuntimeError):
    """A source did not answer (network, HTTP status, a 429 rate limit, a body we cannot read). Codes only."""


Fetcher = Callable[[str], bytes]


def _http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "aa-crm-fx-fetch/1", "Accept": "*/*"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:  # noqa: S310 — fixed https URLs
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise FxFetchError("fx_rate_limited" if exc.code == 429 else f"fx_http_{exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise FxFetchError("fx_unreachable") from None
    if len(body) > MAX_BYTES:
        raise FxFetchError("fx_body_too_large")
    return body


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def synthetic_rows(day: dt.date) -> list[RateRow]:
    return [RateRow(day, "USD", quote, "synthetic", rate) for quote, rate in sorted(SYNTHETIC_USD.items())]


def parse_er_api(body: bytes) -> list[RateRow]:
    """ExchangeRate-API open access ``/v6/latest/USD``: ``{result, base_code, time_last_update_unix, rates}``."""
    try:
        data = json.loads(body)
    except ValueError:
        raise FxFetchError("fx_bad_body") from None
    if not isinstance(data, dict) or data.get("result") != "success" or data.get("base_code") != "USD":
        raise FxFetchError("fx_bad_body")
    try:
        day = dt.datetime.fromtimestamp(int(data["time_last_update_unix"]), dt.UTC).date()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise FxFetchError("fx_bad_body") from None
    rows = []
    for quote, value in (data.get("rates") or {}).items():
        rate = _decimal(value)
        if quote in CURRENCIES and quote != "USD" and rate is not None:
            rows.append(RateRow(day, "USD", quote, "exchangerate_api", rate))
    return sorted(rows, key=lambda r: r.quote)


def parse_ecb(body: bytes) -> list[RateRow]:
    """The ECB daily file: ``<Cube time="YYYY-MM-DD"><Cube currency="USD" rate="1.08"/>…`` (EUR base)."""
    try:
        root = ET.fromstring(body)  # noqa: S314 — a fixed ECB URL; the stdlib parser expands no external entities
    except ET.ParseError:
        raise FxFetchError("fx_bad_body") from None
    rows = []
    for day_cube in root.iter(f"{ECB_NS}Cube"):
        stamp = day_cube.get("time")
        if not stamp:
            continue
        try:
            day = dt.date.fromisoformat(stamp)
        except ValueError:
            raise FxFetchError("fx_bad_body") from None
        for cube in day_cube:
            quote, rate = cube.get("currency"), _decimal(cube.get("rate"))
            if quote in CURRENCIES and quote != "EUR" and rate is not None:
                rows.append(RateRow(day, "EUR", str(quote), "ecb", rate))
    if not rows:
        raise FxFetchError("fx_bad_body")
    return sorted(rows, key=lambda r: r.quote)


def parse_fawaz(body: bytes) -> list[RateRow]:
    """fawazahmed0 currency-api ``currencies/usd.json``: ``{date, usd: {vnd: 25400.1, …}}`` (lower-case codes)."""
    try:
        data = json.loads(body)
        day = dt.date.fromisoformat(str(data["date"]))
        rates = data["usd"]
    except (ValueError, KeyError, TypeError):
        raise FxFetchError("fx_bad_body") from None
    rows = []
    for code, value in (rates or {}).items():
        quote, rate = str(code).upper(), _decimal(value)
        if quote in CURRENCIES and quote != "USD" and rate is not None:
            rows.append(RateRow(day, "USD", quote, "fawazahmed0", rate))
    return sorted(rows, key=lambda r: r.quote)


def cross_check(primary: list[RateRow], check: list[RateRow]) -> list[str]:
    """Quotes where the check source is more than :data:`CROSS_CHECK_GAP` away from the primary (USD base)."""
    theirs = {r.quote: r.rate for r in check}
    flagged = []
    for r in primary:
        other = theirs.get(r.quote)
        if other is not None and abs(other - r.rate) / r.rate > CROSS_CHECK_GAP:
            flagged.append(r.quote)
    return flagged


def live_rows(get: Fetcher = _http_get) -> tuple[list[RateRow], dict[str, int]]:
    """Every live source; a failing one is counted (``failed_count``), the run fails only when all three fail."""
    rows: list[RateRow] = []
    counts = {"failed_count": 0, "flagged_count": 0}
    er: list[RateRow] = []
    fawaz: list[RateRow] = []
    sources = ((ER_API_URL, parse_er_api, "er"), (ECB_URL, parse_ecb, "ecb"), (FAWAZ_URL, parse_fawaz, "fw"))
    for url, parse, sink in sources:
        try:
            got = parse(get(url))
        except FxFetchError as exc:
            counts["failed_count"] += 1
            log.warning("crm_fx_source_failed source=%s reason=%s", sink, exc)
            continue
        rows.extend(got)
        if sink == "er":
            er = got
        elif sink == "fw":
            fawaz = got
    if counts["failed_count"] == 3:
        raise FxFetchError("fx_all_sources_failed")
    flagged = cross_check(er, fawaz)
    if flagged:
        log.warning("crm_fx_cross_check_gap quotes=%s", ",".join(flagged))
    counts["flagged_count"] = len(flagged)
    return rows, counts


async def store(pool: asyncpg.Pool, rows: list[RateRow]) -> int:
    if not rows:
        return 0
    status = await pool.execute(
        INSERT_SQL,
        [r.rate_date for r in rows],
        [r.base for r in rows],
        [r.quote for r in rows],
        [r.source for r in rows],
        [r.rate for r in rows],
    )
    return int(status.split()[-1])


def build_handler(provider: str, *, today: Callable[[], dt.date] | None = None, get: Fetcher | None = None) -> Handler:
    """The ``crm_fx_fetch`` handler for ``FX_PROVIDER`` (``synthetic`` or ``live``)."""
    if provider not in ("synthetic", "live"):
        raise ValueError(f"unknown FX provider {provider!r}")
    clock = today or (lambda: dt.datetime.now(dt.UTC).date())

    async def fx_fetch(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        counts = {"failed_count": 0, "flagged_count": 0}
        if provider == "synthetic":
            rows = synthetic_rows(clock())
        else:
            rows, counts = await asyncio.to_thread(live_rows, get or _http_get)
        inserted = await store(pool, rows)
        return {"mode": provider, "rate_count": len(rows), "inserted_count": inserted, **counts}

    return fx_fetch


def schedule_key(day: dt.date) -> str:
    return f"{FX_FETCH}:{day.isoformat()}"


class DailyFxSchedule:
    """Worker maintenance hook: one ``crm_fx_fetch`` run per UTC day, due now, on the first maintenance pass of the
    day from ``utc_hour`` (default 0: ExchangeRate-API refreshes once a day around midnight UTC); never queued ahead
    (the ``DailyLifecycleSchedule`` pattern)."""

    def __init__(self, utc_hour: int = 0, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        if not 0 <= utc_hour <= 23:
            raise ValueError(f"utc_hour must be 0..23, got {utc_hour}")
        self._utc_hour = utc_hour
        self._now = now
        self._done_day: dt.date | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        now = self._now().astimezone(dt.UTC)
        today = now.date()
        if now.hour < self._utc_hour or self._done_day == today:
            return None
        job_id = await queue.enqueue(
            pool,
            FX_FETCH,
            {"purpose": "daily_schedule"},
            idempotency_key=schedule_key(today),
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, FX_FETCH, HISTORY_DAYS)
        self._done_day = today
        return job_id
