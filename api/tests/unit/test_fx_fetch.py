"""AA-944 ``crm_fx_fetch``: the parsers on recorded bodies (ExchangeRate-API, ECB, fawazahmed0), the 1 % cross-check,
the synthetic provider, the daily due-now schedule and the payload guard. No network: every fetch is a fake."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

import pytest

from app.fx import fetch
from app.jobs.kinds import DEPLOY_GATE_EXEMPT, FX_FETCH
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job

DAY = dt.date(2026, 10, 24)
JOB = Job("j1", FX_FETCH, {"purpose": "daily_schedule"}, 1, 4)

ER_API = json.dumps(
    {
        "result": "success",
        "provider": "https://www.exchangerate-api.com",
        "time_last_update_unix": 1792800001,
        "base_code": "USD",
        "rates": {"USD": 1, "VND": 25412.5, "EUR": 0.921, "THB": 36.4, "XAU": 0.0004, "BAD": "x"},
    }
).encode()
ECB = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <gesmes:subject>Reference rates</gesmes:subject>
  <Cube><Cube time="2026-10-23">
    <Cube currency="USD" rate="1.0857"/><Cube currency="JPY" rate="162.9"/><Cube currency="ISK" rate="150.1"/>
  </Cube></Cube>
</gesmes:Envelope>"""
FAWAZ = json.dumps({"date": "2026-10-24", "usd": {"vnd": 25400.0, "eur": 0.95, "thb": 36.41, "btc": 0.00001}}).encode()


def test_parse_er_api_keeps_the_known_currencies_against_usd() -> None:
    rows = fetch.parse_er_api(ER_API)
    assert [(r.base, r.quote, r.source) for r in rows] == [
        ("USD", "EUR", "exchangerate_api"),
        ("USD", "THB", "exchangerate_api"),
        ("USD", "VND", "exchangerate_api"),
    ]
    assert rows[2].rate == Decimal("25412.5") and rows[0].rate_date == dt.date(2026, 10, 24)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        json.dumps({"result": "error"}).encode(),
        json.dumps({"result": "success", "base_code": "USD"}).encode(),
    ],
)
def test_parse_er_api_refuses_a_body_it_cannot_read(body: bytes) -> None:
    with pytest.raises(fetch.FxFetchError, match="fx_bad_body"):
        fetch.parse_er_api(body)


def test_parse_ecb_reads_the_reference_day_against_eur() -> None:
    rows = fetch.parse_ecb(ECB)
    assert [(r.rate_date, r.base, r.quote, r.rate) for r in rows] == [
        (dt.date(2026, 10, 23), "EUR", "JPY", Decimal("162.9")),
        (dt.date(2026, 10, 23), "EUR", "USD", Decimal("1.0857")),
    ]
    for body in (b"<broken", b"<Envelope/>"):
        with pytest.raises(fetch.FxFetchError):
            fetch.parse_ecb(body)


def test_parse_fawaz_upper_cases_and_filters() -> None:
    rows = fetch.parse_fawaz(FAWAZ)
    assert [(r.quote, r.source) for r in rows] == [
        ("EUR", "fawazahmed0"),
        ("THB", "fawazahmed0"),
        ("VND", "fawazahmed0"),
    ]
    with pytest.raises(fetch.FxFetchError):
        fetch.parse_fawaz(b"{}")


def test_the_cross_check_flags_a_gap_above_one_percent() -> None:
    # EUR: 0.921 vs 0.95 = 3.1 % -> flagged; VND 0.05 % and THB 0.03 % pass.
    assert fetch.cross_check(fetch.parse_er_api(ER_API), fetch.parse_fawaz(FAWAZ)) == ["EUR"]


def test_live_rows_count_failed_sources_and_fail_only_when_all_fail() -> None:
    bodies = {fetch.ER_API_URL: ER_API, fetch.ECB_URL: ECB}

    def get(url: str) -> bytes:
        if url not in bodies:
            raise fetch.FxFetchError("fx_rate_limited")
        return bodies[url]

    rows, counts = fetch.live_rows(get)
    assert counts == {"failed_count": 1, "flagged_count": 0}
    assert {r.source for r in rows} == {"exchangerate_api", "ecb"}

    def down(url: str) -> bytes:
        raise fetch.FxFetchError("fx_unreachable")

    with pytest.raises(fetch.FxFetchError, match="fx_all_sources_failed"):
        fetch.live_rows(down)


def test_live_rows_fail_when_only_the_cross_check_source_answers() -> None:
    """fawazahmed0 alone gives crm.fx_resolve nothing to use: the run fails (and alerts) instead of passing."""

    def only_fawaz(url: str) -> bytes:
        if url != fetch.FAWAZ_URL:
            raise fetch.FxFetchError("fx_unreachable")
        return FAWAZ

    with pytest.raises(fetch.FxFetchError, match="fx_no_usable_source"):
        fetch.live_rows(only_fawaz)

    def ecb_only(url: str) -> bytes:
        if url != fetch.ECB_URL:
            raise fetch.FxFetchError("fx_unreachable")
        return ECB

    rows, counts = fetch.live_rows(ecb_only)
    assert counts["failed_count"] == 2 and {r.source for r in rows} == {"ecb"}


def test_synthetic_rows_are_deterministic_and_include_vnd() -> None:
    rows = fetch.synthetic_rows(DAY)
    assert rows == fetch.synthetic_rows(DAY)
    assert {r.quote for r in rows} >= {"VND", "EUR", "THB"} and {r.base for r in rows} == {"USD"}
    assert {r.source for r in rows} == {"synthetic"}


class FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append(args)
        return f"INSERT 0 {len(args[0])}"


@pytest.mark.asyncio
async def test_the_synthetic_handler_stores_counts_only() -> None:
    pool = FakePool()
    result = await fetch.build_handler("synthetic", today=lambda: DAY)(pool, JOB)  # type: ignore[arg-type]
    assert result["mode"] == "synthetic" and result["inserted_count"] == result["rate_count"] > 10
    check_payload(result, what="result")
    dates, bases, quotes, sources, rates = pool.calls[0]
    assert set(dates) == {DAY} and "VND" in quotes and set(sources) == {"synthetic"}


@pytest.mark.asyncio
async def test_the_live_handler_uses_the_fetcher() -> None:
    pool = FakePool()
    bodies = {fetch.ER_API_URL: ER_API, fetch.ECB_URL: ECB, fetch.FAWAZ_URL: FAWAZ}
    result = await fetch.build_handler("live", get=bodies.__getitem__)(pool, JOB)  # type: ignore[arg-type]
    assert result["mode"] == "live" and result["flagged_count"] == 1 and result["failed_count"] == 0
    check_payload(result, what="result")


@pytest.mark.asyncio
async def test_store_skips_an_empty_set() -> None:
    assert await fetch.store(FakePool(), []) == 0  # type: ignore[arg-type]


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown FX provider"):
        fetch.build_handler("ecb")


@pytest.mark.asyncio
async def test_the_daily_schedule_queues_due_now_once_per_day(monkeypatch: pytest.MonkeyPatch) -> None:
    queued: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def enqueue(_pool: Any, kind: str, payload: dict[str, Any], **kw: Any) -> str:
        queued.append((kind, payload, kw))
        return "job-1"

    async def prune(*_: Any) -> int:
        return 0

    monkeypatch.setattr(fetch.queue, "enqueue", enqueue)
    monkeypatch.setattr(fetch.queue, "prune", prune)
    clock = iter(
        [
            dt.datetime(2026, 10, 24, 0, 30, tzinfo=dt.UTC),
            dt.datetime(2026, 10, 24, 1, 5, tzinfo=dt.UTC),
            dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC),
        ]
    )
    hook = fetch.DailyFxSchedule(utc_hour=1, now=lambda: next(clock))
    assert await hook(object()) is None  # before 01:00 UTC  # type: ignore[arg-type]
    assert await hook(object()) == "job-1"  # type: ignore[arg-type]
    assert await hook(object()) is None  # done today  # type: ignore[arg-type]
    (kind, payload, kw) = queued[0]
    assert kind == FX_FETCH and kw["idempotency_key"] == "crm_fx_fetch:2026-10-24"
    check_payload(payload)
    assert FX_FETCH not in DEPLOY_GATE_EXEMPT
    with pytest.raises(ValueError):
        fetch.DailyFxSchedule(utc_hour=24)


def test_http_get_maps_errors_to_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def rate_limited(*_: Any, **__: Any) -> Any:
        raise urllib.error.HTTPError(fetch.ER_API_URL, 429, "Too Many Requests", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(fetch.urllib.request, "urlopen", rate_limited)
    with pytest.raises(fetch.FxFetchError, match="fx_rate_limited"):
        fetch._http_get(fetch.ER_API_URL)

    def unreachable(*_: Any, **__: Any) -> Any:
        raise urllib.error.URLError("down")

    monkeypatch.setattr(fetch.urllib.request, "urlopen", unreachable)
    with pytest.raises(fetch.FxFetchError, match="fx_unreachable"):
        fetch._http_get(fetch.ER_API_URL)

    class Big:
        def __enter__(self) -> Big:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def read(self, n: int) -> bytes:
            return b"x" * n

    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda *a, **k: Big())
    with pytest.raises(fetch.FxFetchError, match="fx_body_too_large"):
        fetch._http_get(fetch.ER_API_URL)
