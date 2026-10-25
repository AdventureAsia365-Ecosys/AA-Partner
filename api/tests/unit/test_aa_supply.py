"""AA-947 (card CRM-19) without a database: what Booking's answers become (``app.booking.sync.perform``), the line view
and the Lost hint, the product look-up, the restore re-check, the catalog client, the request parameters, the routes
(permissions, 503 when not configured) and the settings. The database side: tests/integration/test_aa_lines_api.py
and test_crm017_schema.py."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.booking import aa_lines, products, sync, views
from app.booking.checker import BookingChecker
from app.booking.sync import BookingSync, Outcome, Work
from app.catalog.client import CatalogClient, CatalogUnavailable, summary
from app.catalog.sync import CatalogSync, HourlyCatalogSync, backoff_seconds
from app.errors import Conflict, Invalid, NotFound, Unavailable
from app.jobs.queue import Job
from app.lifecycle import booking as seam
from app.main import create_app
from app.routers import aa as router
from app.settings import SettingsError, load_settings
from tests.aa_fakes import FakeBooking, FakeCis, booking_client, catalog_config, tour_body
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, fake_tx, tenant_ctx

LINE_ID = uuid.UUID("0f8fad5b-d9cb-469f-a165-70867728950e")


def _line(**over: Any) -> dict[str, Any]:
    line = {
        "booking_line_id": LINE_ID, "trip_id": uuid.uuid4(), "status": "planning", "aa_sub_status": "quoted",
        "aa_tour_source_id": "T-100", "aa_component_id": "tp:1", "aa_component_kind": "trip_plan",
        "aa_currency": "USD", "aa_pax": '{"adult": 2, "child": 1, "nights": 2, "rooms": [{"occupancy": "double"}]}',
        "departure_id": "d1", "service_from": dt.date(2026, 12, 1), "bk_quote_id": "q1", "aa_price_hash": "a" * 64,
        "aa_quote_version": 1, "bk_hold_id": None, "bk_booking_id": None, "from_price": None,
        "trip_stage": "booked", "trip_deleted_at": None, "sell_amount": None,
    }  # fmt: skip
    return {**line, **over}


def _work(kind: str, **over: Any) -> Work:
    return Work({"aa_request_id": uuid.uuid4(), "kind": kind, "attempts": 1, "sell_amount": None, "result_code": None},
                _line(**over))  # fmt: skip


# ── perform: Booking's answers as values ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_perform_quote_hold_confirm_release_refresh() -> None:
    fake = FakeBooking()
    client = booking_client(fake)
    quoted = await sync.perform(client, TENANT, _work("quote"))
    assert quoted.code == "quoted" and quoted.quote.b2b_price == Decimal("1000.00")
    assert quoted.from_price == Decimal("800.00")
    body = sync.quote_body(_line(), 2)
    assert body["proposal_ref"] == f"crm-line:{LINE_ID}" and body["proposal_version"] == 2
    assert body["lines"][0]["nights"] == 2 and body["lines"][0]["departure_id"] == "d1"
    held = await sync.perform(client, TENANT, _work("hold"))
    assert held.hold.hold_id == "h1" and held.code == "held"
    booked = await sync.perform(client, TENANT, _work("confirm", bk_hold_id="h1"))
    assert booked.booking.trip_case_code == "TC-0001" and booked.code == "on_request"
    refreshed = await sync.perform(client, TENANT, _work("refresh", bk_hold_id="h1"))
    assert refreshed.hold.status == "confirmed"
    assert (await sync.perform(client, TENANT, _work("refresh"))).code == "no_hold"
    refused = await sync.perform(client, TENANT, _work("release", bk_hold_id="h1", aa_sub_status="held"))
    assert refused.failed and refused.code == "hold_confirmed"
    gone = await sync.perform(client, TENANT, _work("release", bk_hold_id="h9", aa_sub_status="held"))
    assert gone.released  # a hold Booking no longer knows is released already
    assert (await sync.perform(client, TENANT, _work("release"))).released  # nothing held: no call
    missing = await sync.perform(client, TENANT, _work("refresh", bk_hold_id="h9"))
    assert missing.failed and missing.code == "not_found"


@pytest.mark.asyncio
async def test_perform_failures() -> None:
    fake = FakeBooking(down=True)
    assert (await sync.perform(booking_client(fake), TENANT, _work("quote"))).retry == "booking_timeout"
    fake = FakeBooking(availability="sold_out", from_price=None)
    out = await sync.perform(booking_client(fake), TENANT, _work("requote", aa_component_kind="hotel_room"))
    assert out.code == "sold_out" and out.from_price is None and fake.calls["GET price-parity"] == 0
    fake = FakeBooking(hold_refusal="price_changed")
    out = await sync.perform(booking_client(fake), TENANT, _work("hold"))
    assert out.again and out.code == "requoted" and out.quote is not None
    work = _work("hold")
    work.request["result_code"] = "requoted"
    out = await sync.perform(booking_client(fake), TENANT, work)
    assert out.failed and out.code == "price_changed"
    fake = FakeBooking(hold_refusal="rehold_limit")
    assert (await sync.perform(booking_client(fake), TENANT, _work("hold"))).code == "rehold_limit"

    class Broken(FakeBooking):
        async def __call__(self, method: str, url: str, *a: Any) -> Any:
            answer = await super().__call__(method, url, *a)
            if "/quotes" in url:
                return type(answer)(201, b'{"quote_id": 5}')
            return answer

    assert (await sync.perform(booking_client(Broken()), TENANT, _work("quote"))).retry == "booking_contract"
    fake = FakeBooking()
    fake.holds["h1"] = {}
    out = await sync.perform(booking_client(fake), TENANT, _work("confirm", bk_hold_id="h2"))
    assert out.failed and out.code == "not_found"


def test_backoff_and_the_from_price_flag() -> None:
    assert [sync.backoff_seconds(n) for n in (0, 1, 2, 3, 20)] == [30, 30, 60, 120, 900]
    pax = {"adult": 2, "child": 0}
    assert sync.below_from_price(Decimal("1500"), pax, Decimal("800")) is True
    assert sync.below_from_price(Decimal("1700"), pax, Decimal("800")) is False
    assert sync.below_from_price(None, pax, Decimal("800")) is None
    assert sync.below_from_price(Decimal("1"), {"adult": 0}, Decimal("800")) is None
    assert Outcome().retry is None


@pytest.mark.asyncio
async def test_the_sync_without_booking_is_not_configured() -> None:
    job = Job("j", "crm_booking_sync", {}, 1, 3)
    handler = BookingSync(None)
    assert await handler(object(), job) == {"status": "not_configured"}  # type: ignore[arg-type]
    assert await handler(object(), job) == {"status": "not_configured"}  # type: ignore[arg-type]


# ── the line view and the Lost hint ──────────────────────────────────────────────────────────────


def _row(**over: Any) -> dict[str, Any]:
    row = {
        "supplier_kind": "aa", "status": "planning", "aa_sub_status": "quoted", "aa_pending": None,
        "bk_booking_id": None, "bk_hold_id": None, "aa_tour_source_id": "T-1", "aa_component_kind": "trip_plan",
        "aa_currency": "USD", "aa_pax": '{"adult": 2}', "quote_valid_until": None, "hold_expires_at": None,
        "from_price": Decimal("800.00"), "below_from_price": True, "aa_decline_code": None, "aa_result_code": None,
        "aa_catalog_state": "retired",
    }  # fmt: skip
    return {**row, **over}


def test_actions_follow_the_line_and_the_trip() -> None:
    assert views.actions(_row(), "proposal") == ["hold", "release", "requote"]
    assert views.actions(_row(aa_sub_status="held"), "proposal") == ["release"]
    assert views.actions(_row(aa_sub_status="held"), "booked") == ["confirm", "release"]
    assert views.actions(_row(aa_sub_status="on_request", bk_booking_id="b1"), "booked") == []
    assert views.actions(_row(aa_pending='{"kind": "hold"}'), "proposal") == []
    assert views.actions(_row(), "lost") == ["release"]
    assert views.actions(_row(supplier_kind="agency"), "proposal") == []


def test_the_aa_block_and_the_lost_hint() -> None:
    block = views.aa_block(_row(aa_pending='{"kind": "quote", "attempts": 2}'), "proposal")
    assert block["pending"] == {"kind": "quote", "attempts": 2} and block["no_longer_offered"]
    assert block["from_price"] == {"amount": "800.00", "currency": "USD"} and block["pax"] == {"adult": 2}
    assert views.aa_block(_row(supplier_kind="agency"), None) is None
    assert views.aa_block(_row(from_price=None, aa_catalog_state=None), None)["from_price"] is None
    agency = _row(supplier_kind="agency")
    assert views.lost_reason_hint([agency, _row()]) is None
    assert views.lost_reason_hint([_row(aa_sub_status="sold_out")]) == "aa_sold_out"
    assert views.lost_reason_hint([_row(aa_result_code="sold_out")]) == "aa_sold_out"
    assert views.lost_reason_hint([_row(aa_sub_status="declined", aa_decline_code="sold_out")]) == "aa_sold_out"
    assert views.lost_reason_hint([_row(aa_sub_status="declined", aa_decline_code="other")]) == "aa_on_request_declined"


# ── the request parameters ───────────────────────────────────────────────────────────────────────


def _req(**over: Any) -> dict[str, Any]:
    body = {"tour_source_id": "T-1", "component_id": "rt:2", "component_kind": "hotel_room", "nights": 2,
            "service_date": "2026-12-01", "pax": {"adult": 2}, "currency": "VND", "sell_amount": "1,5",
            "rooms": [{"occupancy": "double"}]}  # fmt: skip
    return {**body, **over}


def test_request_parameters_are_checked() -> None:
    ok = aa_lines.parse_request(_req(sell_amount="1500.555", service_date=dt.date(2026, 12, 1)))
    assert ok["sell_amount"] == Decimal("1500.56") and ok["rooms"] == [{"occupancy": "double", "count": 1}]
    assert ok["child"] == 0 and ok["infant"] == 0
    assert aa_lines.parse_request(_req(sell_amount=None, rooms=[]))["rooms"] is None
    for bad, code in (
        ({"tour_source_id": "\x00"}, "invalid_tour_source_id"),
        ({"component_id": "a b"}, "invalid_component_id"),
        ({"component_kind": "boat"}, "invalid_component_kind"),
        ({"departure_id": 5}, "invalid_departure_id"),
        ({"service_date": "tomorrow"}, "invalid_service_date"),
        ({"currency": "usd"}, "invalid_currency"),
        ({"nights": None}, "invalid_nights"),
        ({"nights": True}, "invalid_nights"),
        ({"sell_amount": "-1"}, "invalid_sell_amount"),
        ({"sell_amount": "abc"}, "invalid_sell_amount"),
        ({"rooms": "x"}, "invalid_rooms"),
        ({"rooms": [{"occupancy": "dorm"}]}, "invalid_rooms"),
        ({"pax": {"adult": 0}}, "invalid_pax"),
    ):
        with pytest.raises(Invalid) as err:
            aa_lines.parse_request(_req(**bad))
        assert err.value.code == code, bad


# ── the product look-up and the restore re-check ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_product_lookup_and_its_cache() -> None:
    fake = FakeBooking()
    client = booking_client(fake)
    now = [0.0]
    cache = products.ProductCache(client, clock=lambda: now[0])
    out = await products.lookup(cache, client, TENANT, "T-100", "USD", today=dt.date(2026, 10, 25))
    assert [c["component_id"] for c in out["components"]] == ["tp:1", "rt:2"]
    assert out["departures"][0]["departure_id"] == "d1" and out["from_price"]["amount"] == "800.00"
    await products.lookup(cache, client, TENANT, "T-100", None, today=dt.date(2026, 10, 25))
    assert fake.calls["GET products"] == 1
    now[0] = 1000.0
    with pytest.raises(NotFound):
        await products.lookup(cache, client, TENANT, "T-404", None, today=dt.date(2026, 10, 25))
    assert fake.calls["GET products"] == 2
    fake.from_price = None
    fake.down = False
    assert (await products.lookup(cache, client, TENANT, "T-100", "USD", today=dt.date(2026, 10, 25)))[
        "from_price"
    ] is None
    fake.down = True
    cache2 = products.ProductCache(client)
    with pytest.raises(Unavailable):
        await products.lookup(cache2, client, TENANT, "T-100", None, today=dt.date(2026, 10, 25))
    assert isinstance(products._booking_error(sync.BookingRefusal(403, "unknown_partner")), Conflict)


@pytest.mark.asyncio
async def test_the_restore_recheck_asks_booking() -> None:
    fake = FakeBooking()
    client = booking_client(fake)
    await client.hold(TENANT, {"quote_id": "q", "price_hash": "a" * 64, "proposal_ref": "crm-line:L1",
                               "proposal_version": 1}, idempotency_key="k" * 10)  # fmt: skip
    checker = BookingChecker(client)
    assert await checker.line_statuses(TENANT, ["h1", "h404"]) == {"h1": "held"}
    fake.down = True
    with pytest.raises(seam.BookingUnavailable):
        await checker.line_statuses(TENANT, ["h1"])
    fake.down = False
    fake.holds["h2"] = {"x": 1}  # a broken answer
    with pytest.raises(seam.BookingUnavailable):
        await checker.line_statuses(TENANT, ["h2"])


# ── the catalog client and schedule ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_catalog_client_reads_liveness() -> None:
    cis = FakeCis(tours={"T-1": tour_body("T-1")}, retired={"T-2"}, paused={"T-3"})
    client = CatalogClient(catalog_config(cis))
    items, cursor = await client.list_tours(None)
    assert [i.source_id for i in items] == ["T-1"] and cursor is None
    served = await client.get_tour("T-1")
    assert served.state == "served" and served.etag == '"sha256:1"'
    assert (await client.get_tour("T-1", served.etag)).state == "not_modified"
    assert (await client.get_tour("T-2")).reason == "source_deleted"
    assert (await client.get_tour("T-3")).state == "paused"
    assert (await client.get_tour("T-9")).state == "unknown"
    cis.down = True
    with pytest.raises(CatalogUnavailable) as err:
        await client.list_tours("5")
    assert err.value.error_class == "cis_unavailable"
    with pytest.raises(CatalogUnavailable):
        summary({"source_system": "cis", "source_id": "T", "title": "", "country_code": "VN"})
    assert [backoff_seconds(n) for n in (0, 1, 2, 10)] == [0, 300, 600, 21600]
    assert await CatalogSync(None)(object(), Job("j", "crm_catalog_sync", {}, 1, 3)) == {  # type: ignore[arg-type]
        "status": "not_configured"
    }


@pytest.mark.asyncio
async def test_the_hourly_catalog_schedule_queues_once_per_hour() -> None:
    conn = ScriptedConn().on("INSERT INTO crm.job", [{"id": "j1"}])
    hook = HourlyCatalogSync(lambda: dt.datetime(2026, 10, 25, 8, 30, tzinfo=dt.UTC))
    assert await hook(conn) is not None  # type: ignore[arg-type]
    assert await hook(conn) is None  # type: ignore[arg-type]


# ── routes ───────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str, *, booking: Any = None) -> tuple:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), booking_client=booking)
    ctx = tenant_ctx(ScriptedConn(), role=role)

    def tx(*a: Any, **kw: Any) -> Any:
        margin.current_member_role.set(role)
        return fake_tx(ctx)(*a, **kw)

    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", tx)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []

    async def request_line(ctx: Any, trip_id: Any, body: Any) -> dict[str, Any]:
        out.append(("request_line", body))
        return {"booking_line_id": str(LINE_ID), "money": {"cost": {"amount": "1"}, "sell": {"amount": "2"}}}

    async def ask(ctx: Any, line_id: Any, action: str, body: Any) -> dict[str, Any]:
        out.append((action, body))
        return {"ok": action}

    async def search(ctx: Any, q: Any, country: Any, limit: int) -> list[dict[str, Any]]:
        out.append(("search", (q, country, limit)))
        return [{"source_id": "T-1"}]

    async def tour(ctx: Any, source_id: str) -> dict[str, Any]:
        return {"source_id": source_id}

    monkeypatch.setattr(aa_lines, "request_line", request_line)
    monkeypatch.setattr(aa_lines, "ask", ask)
    monkeypatch.setattr(router.catalog, "search", search)
    monkeypatch.setattr(router.catalog, "tour", tour)
    return out


BODY = {"tour_source_id": "T-1", "component_id": "tp:1", "component_kind": "trip_plan", "service_date": "2026-12-01",
        "pax": {"adult": 2}, "currency": "USD"}  # fmt: skip


def test_routes_reach_their_helpers(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, headers = _client(pools, monkeypatch, "agent")
    r = client.get("/crm/v1/aa/catalog?q=%20mekong%20&country=VN", headers=headers)
    assert r.status_code == 200 and r.json() == {"tours": [{"source_id": "T-1"}]}
    assert calls[-1] == ("search", ("mekong", "VN", 50))
    r = client.post(f"/crm/v1/trips/{uuid.uuid4()}/aa-lines", headers=headers, json={**BODY, "sell_amount": "1500"})
    assert r.status_code == 201 and calls[-1][0] == "request_line"
    r = client.post(f"/crm/v1/lines/{LINE_ID}/aa/hold", headers=headers)
    assert r.status_code == 200 and calls[-1] == ("hold", None)
    r = client.post(f"/crm/v1/lines/{LINE_ID}/aa/requote", headers=headers, json={"sell_amount": "1600"})
    assert r.status_code == 200 and calls[-1] == ("requote", {"sell_amount": Decimal("1600")})
    assert client.post(f"/crm/v1/lines/{LINE_ID}/aa/edit", headers=headers).status_code == 422
    assert client.post(f"/crm/v1/trips/{uuid.uuid4()}/aa-lines", headers=headers,
                       json={**BODY, "title": "typed"}).status_code == 422  # fmt: skip


def test_the_assistant_sets_no_sell(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, headers = _client(pools, monkeypatch, "assistant")
    r = client.post(f"/crm/v1/trips/{uuid.uuid4()}/aa-lines", headers=headers, json={**BODY, "sell_amount": "1500"})
    assert r.status_code == 403
    r = client.post(f"/crm/v1/trips/{uuid.uuid4()}/aa-lines", headers=headers, json=BODY)
    assert r.status_code == 201 and "cost" not in r.json()["money"]  # the margin serializer
    assert client.post(f"/crm/v1/lines/{LINE_ID}/aa/requote", headers=headers,
                       json={"sell_amount": "1"}).status_code == 403  # fmt: skip


def test_products_need_booking(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, headers = _client(pools, monkeypatch, "agent")
    r = client.get("/crm/v1/aa/products/T-100", headers=headers)
    assert r.status_code == 503 and r.json()["detail"] == "booking_not_configured"
    fake = FakeBooking()
    client, headers = _client(pools, monkeypatch, "agent", booking=booking_client(fake))
    r = client.get("/crm/v1/aa/products/T-100?currency=USD", headers=headers)
    assert r.status_code == 200 and r.json()["tour"] == {"source_id": "T-100"}
    assert r.json()["from_price"] == {"amount": "800.00", "currency": "USD"}


# ── settings ─────────────────────────────────────────────────────────────────────────────────────


def test_booking_api_url_is_an_origin() -> None:
    assert load_settings(
        {"ENVIRONMENT": "test", "BOOKING_API_URL": "https://Api-Booking.example.test/"}
    ).booking_api_url == ("https://api-booking.example.test")
    assert load_settings({"ENVIRONMENT": "local", "BOOKING_API_URL": "http://localhost:8001"}).booking_api_url
    for bad in ("https://api-booking.example.test/booking", "http://api-booking.example.test"):
        with pytest.raises(SettingsError):
            load_settings({"ENVIRONMENT": "dev", "BOOKING_API_URL": bad})
