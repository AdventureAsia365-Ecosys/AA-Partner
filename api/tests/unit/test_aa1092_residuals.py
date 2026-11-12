"""AA-1092 residual items (S278 pre-PII review, follow-up lane): Book now price checks per agency, one quota per form.

- The limiter takes a per-call ceiling (the agency's own setting) and keeps keys apart.
- ``booking.price`` takes one of the agency's price checks before anything goes to Booking: past the agency's
  ``public_price_per_minute`` it is 429 ``agency_rate_limited`` (no Booking call); a missing value uses the default.
- The route answers 429 ``agency_rate_limited`` with ``Retry-After``, keyed on the agency (two addresses share it).
- Settings > Online booking reads and changes the ceiling (bounds, audit, owner only through the route class).
- The public inquiry form and its sales partner links charge one bucket per form (the form's id): together they get
  the form's hourly quota once, not twice.
All names are invented.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.booking.products import ProductCache
from app.errors import Invalid, RateLimited
from app.intake import booking, booking_settings
from app.intake import public as intake_public
from app.main import create_app
from app.partners import public as partner_public
from app.proposals import pricing
from app.ratelimit import FixedWindowLimiter
from app.routers import partner_pages as partner_router
from app.routers import public as public_router
from app.settings import Settings
from tests.aa_fakes import FakeBooking, booking_client
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, fake_tx, intake_form, system_ctx, tenant_ctx

TEST = Settings(environment="test", db_ssl="disable")
KEY = "k" * 22
LINK = "L" * 43
FORM_ID = uuid.UUID("5f0c0000-0000-4000-8000-0000000a1092")
BODY = {"tour": "T-100", "departure_id": "d1", "adults": 2, "child_ages": [7]}
AGENCY = {
    "public_booking_enabled": True,
    "base_currency": "VND",
    "timezone": "Asia/Ho_Chi_Minh",
    "display_name": "Copperleaf Journeys",
    "consent_text": None,
    "agency_privacy_notice_url": None,
    "public_price_per_minute": 2,
}
TOUR = {
    "source_id": "T-100",
    "title": "Misty ridge two day walk",
    "country_code": "VN",
    "duration_days": 2,
    "served_state": "served",
}


@pytest.fixture
def pools() -> LocalPools:
    return LocalPools()


# ── the limiter ──────────────────────────────────────────────────────────────────────────────────


def test_a_per_call_ceiling_replaces_the_limiter_default_per_key() -> None:
    limiter = FixedWindowLimiter(100, clock=lambda: 0.0)
    assert [limiter.allow("price:a", 2) for _ in range(3)] == [True, True, False]
    assert limiter.allow("price:b", 2) is True  # another agency has its own count
    assert all(limiter.allow("price:c", 0) for _ in range(5))  # 0 = no ceiling
    assert limiter.allow("other") is True  # no per-call value: the limiter's own


# ── booking.price ────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def agency(monkeypatch: pytest.MonkeyPatch) -> ScriptedConn:
    conn = ScriptedConn().on("public_booking_enabled", AGENCY).on("FROM crm.aa_catalog_tour", TOUR)
    monkeypatch.setattr(booking, "intake_tx", fake_tx(system_ctx(conn), intake_form()))

    async def bands(ctx: Any) -> pricing.Settings:
        return pricing.Settings(child_age_max=11, infant_age_max=1, discount_owner_limit_pct=Decimal("10"))

    async def rctx(ctx: Any) -> dict[str, Any]:
        return {"markups": {"trip_plan": Decimal("20")}, "rules": [], "today": dt.date(2031, 3, 1)}

    monkeypatch.setattr(booking.pricing, "settings", bands)
    monkeypatch.setattr(booking, "_rctx", rctx)
    return conn


def _fake() -> FakeBooking:
    return FakeBooking(b2b_price="10000000.00", from_price=None, departure_start="2031-05-01")


@pytest.mark.asyncio
async def test_the_agency_ceiling_is_taken_before_booking_is_asked(agency: ScriptedConn) -> None:
    limiter = FixedWindowLimiter(0, clock=lambda: 0.0)
    asked: list[tuple[uuid.UUID, int]] = []

    def quota(tenant_id: uuid.UUID, ceiling: int) -> bool:
        asked.append((tenant_id, ceiling))
        return limiter.allow(f"price:{tenant_id}", ceiling)

    fake = _fake()
    client = booking_client(fake)
    for _ in range(2):
        out = await booking.price(None, ProductCache(client), client, KEY, BODY, agency_quota=quota)  # type: ignore[arg-type]
        assert out["total"] == "12000000.00"
    fake.down = True  # a third check must not reach Booking at all
    with pytest.raises(RateLimited) as err:
        await booking.price(None, ProductCache(client), client, KEY, BODY, agency_quota=quota, quota_window_s=60)  # type: ignore[arg-type]
    assert (err.value.code, err.value.status_code, err.value.retry_after_s) == ("agency_rate_limited", 429, 60)
    assert {ceiling for _, ceiling in asked} == {2}


@pytest.mark.asyncio
async def test_without_a_value_the_default_ceiling_applies(agency: ScriptedConn) -> None:
    agency.on("public_booking_enabled", {**AGENCY, "public_price_per_minute": None})
    seen: list[int] = []
    client = booking_client(_fake())
    await booking.price(
        None, ProductCache(client), client, KEY, BODY, agency_quota=lambda _t, c: seen.append(c) or True
    )  # type: ignore[arg-type]
    assert seen == [booking_settings.DEFAULT_PRICE_PER_AGENCY_MINUTE]


# ── the route ────────────────────────────────────────────────────────────────────────────────────


async def _pool(request: Any) -> Any:
    return object()


def test_the_route_answers_429_agency_rate_limited_per_agency(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = pools.settings(rate_limit_booking_price_per_minute=0)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(public_router, "request_pool", _pool)

    async def price(pool: Any, cache: Any, client: Any, key: str, raw: Any, **kw: Any) -> dict[str, str]:
        if not kw["agency_quota"](TENANT, 2):
            raise RateLimited("agency_rate_limited", kw["quota_window_s"])
        return {"total": "1.00"}

    monkeypatch.setattr(booking, "price", price)
    url = f"/crm/public/v1/book/{KEY}/price"
    with TestClient(app) as c:
        first = c.post(url, json=BODY, headers={"X-Forwarded-For": "198.51.100.7"})
        second = c.post(url, json=BODY, headers={"X-Forwarded-For": "198.51.100.8"})
        third = c.post(url, json=BODY, headers={"X-Forwarded-For": "198.51.100.9"})
    assert (first.status_code, second.status_code) == (200, 200)
    assert third.status_code == 429 and third.headers["retry-after"] == "60"
    assert third.json()["detail"] == "agency_rate_limited"


# ── Settings > Online booking ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_owner_changes_the_ceiling_within_bounds() -> None:
    conn = (
        ScriptedConn()
        .on("SELECT public_booking_enabled", {"public_booking_enabled": True, "public_price_per_minute": 90})
        .on("FROM crm.intake_form", [])
    )
    ctx = tenant_ctx(conn)
    out = await booking_settings.update(ctx, TEST, None, 90)
    assert conn.sql("SET public_price_per_minute") == [(90,)] and conn.sql("SET public_booking_enabled") == []
    assert out["price_checks_per_minute"] == 90
    assert out["price_checks_bounds"] == {"min": 10, "max": 600, "default": 60}
    for bad in (9, 601, True, "60", 60.5):
        with pytest.raises(Invalid) as err:
            await booking_settings.update(ctx, TEST, None, bad)
        assert err.value.code == "invalid_price_checks_per_minute"
    with pytest.raises(Invalid) as err:
        await booking_settings.update(ctx, TEST, None, None)
    assert err.value.code == "nothing_to_change"


@pytest.mark.asyncio
async def test_without_a_settings_row_the_default_is_shown() -> None:
    conn = ScriptedConn().on("SELECT public_booking_enabled", None).on("FROM crm.intake_form", [])
    out = await booking_settings.read(tenant_ctx(conn), TEST)
    assert (out["enabled"], out["price_checks_per_minute"]) == (False, 60)


# ── one hourly quota per form ────────────────────────────────────────────────────────────────────


def test_public_form_and_partner_link_share_one_quota(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = pools.settings(rate_limit_intake_per_minute=0, rate_limit_portal_write_per_minute=0,
                              rate_limit_intake_form_per_hour=2)  # fmt: skip
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(public_router, "request_pool", _pool)
    monkeypatch.setattr(partner_router, "request_pool", _pool)

    async def public_submit(pool: Any, keys: Any, settings: Any, key: str, raw: Any, **kw: Any) -> Any:
        if not kw["form_quota"](FORM_ID):
            raise RateLimited("form_rate_limited", kw["quota_window_s"])
        return intake_public.Submitted(stored=True)

    async def partner_submit(pool: Any, keys: Any, settings: Any, token: str, raw: Any, **kw: Any) -> bool:
        if not kw["form_quota"](FORM_ID):
            raise RateLimited("form_rate_limited", kw["quota_window_s"])
        return True

    monkeypatch.setattr(intake_public, "submit", public_submit)
    monkeypatch.setattr(partner_public, "submit", partner_submit)
    with TestClient(app) as c:
        a = c.post(f"/crm/public/v1/forms/{KEY}/submissions", json={})
        b = c.post(f"/crm/public/v1/r/{LINK}/submissions", json={})
        over_public = c.post(f"/crm/public/v1/forms/{KEY}/submissions", json={})
        over_link = c.post(f"/crm/public/v1/r/{LINK}/submissions", json={})
    assert (a.status_code, b.status_code) == (202, 202)
    for res in (over_public, over_link):
        assert res.status_code == 429 and res.json()["detail"] == "form_rate_limited"
        assert res.headers["retry-after"] == "3600"


def test_the_bucket_is_the_form_id() -> None:
    assert intake_public.form_bucket(FORM_ID) == f"form:{FORM_ID}"
