"""AA-1152 (crm-086 / crm-087; S285 B2 option C, PR-29 T6): the agency's rounded prices, the "AA add-on" drawer and
the planner price publish. Scripted connections and fake Booking / TripPlanner answers; invented places and names
only. Real SQL (the columns, the CHECK) is in tests/integration/test_aa1152_planner_prices_db.py."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal as D
from typing import Any

import asyncpg
import pytest

from app.booking import addons as booking_addons
from app.booking import contract
from app.errors import Conflict, Invalid
from app.jobs.queue import Job
from app.proposals import aa_addons, retail, rounding
from app.tripplanner import leads, planner, prices
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, tenant_ctx

SV = "sv:7d9f8b00-0000-4000-8000-0000000000c2"
SV2 = "sv:7d9f8b00-0000-4000-8000-0000000000c3"
PLACE = "d1520000-0000-4000-8000-0000000000d1"
DAY = dt.date(2027, 3, 2)


def _addon_json(**over: Any) -> dict[str, Any]:
    body = {
        "component_id": SV,
        "offer_id": "a1520000-0000-4000-8000-0000000000a1",
        "kind": "activity",
        "title": "Evening lantern walk",
        "unit": "person",
        "duration_minutes": 180,
        "max_group_size": 12,
        "country_code": "VN",
        "min_pax": 2,
        "cutoff_hours": 24,
        "operating_weekdays": [2, 4, 6],
        "start_times": ["17:30"],
        "inventory_kind": "on_request",
        "featured": True,
        "first_date": DAY.isoformat(),
        "places": [{"destination_id": PLACE, "role": "operates", "name": "Lantern Bay", "country_code": "VN"}],
        "b2b_price_per_person": {"amount": "34.24", "currency": "USD"},
        "rrp_per_person": {"amount": "45.00", "currency": "USD"},
    }
    body.update(over)
    return body


def _page(*items: dict[str, Any], currency: str = "USD") -> dict[str, Any]:
    return {"currency": currency, "priced_for_pax": 2, "date": None, "truncated": False, "items": list(items)}


# ── Rounding (B2 option C) ───────────────────────────────────────────────────────────────────────────────────────


def test_round_up_per_traveller_then_times_the_party() -> None:
    assert rounding.round_up(D("41.09"), D("1")) == D("42.00")
    assert rounding.round_up(D("41.09"), D("5")) == D("45.00")
    assert rounding.round_up(D("1038000"), D("10000")) == D("1040000.00")
    assert rounding.round_up(D("42"), D("1")) == D("42.00")  # an exact step stays
    assert rounding.round_up(D("10.001"), D("0")) == D("10.01")
    # 2 travellers at 82.18 total: 41.09 each -> 42 each -> 84, never 83 from rounding the total.
    assert rounding.suggested_total(D("82.18"), 2, D("1")) == D("84.00")
    assert rounding.suggested_total(D("10"), 0, D("1")) == D("10.00")


def test_from_price_markup_else_rrp_then_rules_then_one_round_up() -> None:
    # The advisory's kayak: B2B 34.24 x 1.20 = 41.09 -> 42 (step 1); the 10 % peak rule on top: 45.20 -> 46.
    assert rounding.from_price(D("34.24"), D("45"), D("20"), D("1")) == (D("42.00"), "agency_markup")
    assert rounding.from_price(D("34.24"), D("45"), D("20"), D("1"), D("10")) == (D("46.00"), "agency_markup")
    assert rounding.from_price(D("34.24"), D("45"), None, D("5")) == (D("45.00"), "aa_rrp")
    assert rounding.from_price(None, None, D("20"), D("1")) is None
    assert rounding.from_price(D("0"), None, D("20"), D("1")) is None


def test_an_add_on_without_markup_takes_its_rrp_like_a_tour() -> None:
    assert retail.suggest("activity", b2b_price=D("68.48"), from_price=D("45"), party=2, agency_markups={}) == (
        D("90.00"),
        retail.BASIS_RRP,
    )


@pytest.mark.asyncio
async def test_settings_read_merge_and_refusals() -> None:
    row = {"price_round_step": '{"USD": "5"}', "show_planner_prices": True, "base_currency": "VND"}
    conn = ScriptedConn().on("SELECT price_round_step, show_planner_prices", row)
    ctx = tenant_ctx(conn)
    view = await rounding.read(ctx)
    by_cur = {c["currency"]: c for c in view["currencies"]}
    assert view["base_currency"] == "VND" and view["show_planner_prices"] is True
    assert by_cur["USD"] == {
        "currency": "USD",
        "step": "5",
        "chosen": True,
        "default": "1",
        "choices": ["1", "5", "10"],
    }
    assert by_cur["VND"]["step"] == "10000" and by_cur["VND"]["chosen"] is False
    out = await rounding.update(ctx, {"steps": {"USD": None, "VND": "50000"}, "show_planner_prices": False})
    [upsert] = conn.sql("INSERT INTO crm.tenant_settings (price_round_step, show_planner_prices)")
    assert json.loads(upsert[0]) == {"VND": "50000"} and upsert[1] is False
    assert out["show_planner_prices"] is False
    assert [e.action for e in ctx.audit_events] == ["update"]
    with pytest.raises(Invalid) as bad:
        await rounding.update(ctx, {"steps": {"USD": "3"}})
    assert bad.value.code == "invalid_round_step"
    # A currency AA lists no steps for offers its default step only (1 whole unit).
    with pytest.raises(Invalid):
        await rounding.update(ctx, {"steps": {"EUR": "5"}})
    assert (await rounding.update(ctx, {"steps": {"EUR": "1"}}))["currencies"]


@pytest.mark.asyncio
async def test_settings_before_crm_086() -> None:
    conn = ScriptedConn().on("SELECT price_round_step", asyncpg.UndefinedColumnError("no column"))
    conn.on("SELECT base_currency FROM crm.tenant_settings", "USD")
    current = await rounding.settings(tenant_ctx(conn))
    assert current == rounding.PriceSettings(base_currency="USD") and current.step("VND") == D("10000")
    conn.on("INSERT INTO crm.tenant_settings (price_round_step", asyncpg.UndefinedColumnError("no column"))
    with pytest.raises(Invalid) as early:
        await rounding.update(tenant_ctx(conn), {"show_planner_prices": False})
    assert early.value.code == "planner_prices_not_ready"


# ── Booking GET /add-ons (contract 1.11) ─────────────────────────────────────────────────────────────────────────


def test_addon_page_is_read_field_by_field() -> None:
    [one] = booking_addons.page(_page(_addon_json(extra="dropped")), "USD")
    assert one.component_id == SV and one.title == "Evening lantern walk" and one.min_pax == 2
    assert one.places[0].name == "Lantern Bay" and one.b2b_per_person == D("34.24") and one.rrp_per_person == D("45")
    [unpriced] = booking_addons.page(_page(_addon_json(b2b_price_per_person=None, rrp_per_person=None)), "USD")
    assert unpriced.b2b_per_person is None
    for broken in (
        _page(_addon_json(component_id="tp:abc")),
        _page(_addon_json(kind="spa")),
        _page(_addon_json(operating_weekdays=[8])),
        _page(_addon_json(rrp_per_person={"amount": "45.00", "currency": "EUR"})),
        _page(_addon_json(), currency="EUR"),
        {"items": "x", "currency": "USD"},
    ):
        with pytest.raises(contract.ContractError):
            booking_addons.page(broken, "USD")


class FakeBooking:
    def __init__(self, page: dict[str, Any] | Exception, tariff: dict[str, Any] | None = None) -> None:
        self.page, self.tariff_body, self.asked = page, tariff, []

    async def addons(self, tenant_id: uuid.UUID, query: dict[str, str]) -> dict[str, Any]:
        self.asked.append(query)
        if isinstance(self.page, Exception):
            raise self.page
        return self.page

    async def tariff(self, tenant_id: uuid.UUID, *, currency: str, source_id: str | None = None) -> dict[str, Any]:
        assert self.tariff_body is not None
        return self.tariff_body


@pytest.mark.asyncio
async def test_fetch_asks_one_day_for_the_party() -> None:
    client = FakeBooking(_page(_addon_json()))
    await booking_addons.load(client, TENANT, currency="USD", pax=3, on=DAY)  # type: ignore[arg-type]
    assert client.asked == [{"currency": "USD", "pax": "3", "date": DAY.isoformat()}]


# ── The builder's "AA add-on" drawer ─────────────────────────────────────────────────────────────────────────────


def _day(**over: Any) -> dict[str, Any]:
    base = {
        "date": DAY,
        "party": 2,
        "currency": "USD",
        "place": "Lantern Bay old town",
        "tour_source_id": "SYN-TOUR-0042",
        "rctx": {"markups": {"activity": D("20")}, "rules": [], "today": DAY, "round_step": D("1")},
        "sees_margin": True,
    }
    base.update(over)
    return base


def test_listing_puts_the_day_place_first_with_the_agency_price() -> None:
    other = booking_addons.addon(
        _addon_json(component_id=SV2, title="A rice field ride", places=[
            {"destination_id": PLACE, "role": "operates", "name": "Kelp Island", "country_code": "VN"}]),
        "USD",
    )  # fmt: skip
    walk = booking_addons.addon(_addon_json(), "USD")
    out = aa_addons.listing([other, walk], _day())
    assert [i["component_id"] for i in out["items"]] == [SV, SV2] and out["items"][0]["at_day_place"] is True
    assert out["items"][0]["suggested_per_person"] == "42.00" and out["items"][0]["suggested_basis"] == "agency_markup"
    assert out["items"][0]["b2b_per_person"] == "34.24" and out["needs_tour"] is False and out["date"] == "2027-03-02"
    # An assistant sees neither AA's price nor a suggestion made from it; the RRP one stays.
    hidden = aa_addons.listing([walk], _day(sees_margin=False))["items"][0]
    assert hidden["b2b_per_person"] is None and hidden["suggested_per_person"] is None
    rrp = aa_addons.listing([walk], _day(sees_margin=False, rctx={**_day()["rctx"], "markups": {}}))["items"][0]
    assert rrp["suggested_per_person"] == "45.00" and rrp["suggested_basis"] == "aa_rrp"
    assert aa_addons.listing([], _day(tour_source_id=None, date=None)) == {
        "date": None, "party": 2, "currency": "USD", "place": "Lantern Bay old town", "needs_tour": True, "items": [],
    }  # fmt: skip


def test_place_matching() -> None:
    walk = booking_addons.addon(_addon_json(), "USD")
    assert aa_addons.matches("lantern bay", walk) and aa_addons.matches("Lantern Bay, night market", walk)
    assert not aa_addons.matches("Harbour Hill", walk) and not aa_addons.matches(None, walk)


@pytest.mark.asyncio
async def test_offered_maps_booking_refusals() -> None:
    from app.booking.client import BookingRefusal, BookingUnavailable

    with pytest.raises(Conflict) as partner:
        await aa_addons.offered(FakeBooking(BookingRefusal(403, "forbidden")), TENANT, _day())  # type: ignore[arg-type]
    assert partner.value.code == "aa_partner_not_active"
    with pytest.raises(Invalid):
        await aa_addons.offered(FakeBooking(BookingRefusal(422, "invalid")), TENANT, _day())  # type: ignore[arg-type]
    with pytest.raises(Exception) as down:
        await aa_addons.offered(FakeBooking(BookingUnavailable("booking_timeout")), TENANT, _day())  # type: ignore[arg-type]
    assert getattr(down.value, "code", None) == "booking_unavailable"


@pytest.mark.asyncio
async def test_add_on_needs_an_aa_tour_and_takes_booking_s_name(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Conflict) as no_tour:
        await aa_addons.plan(tenant_ctx(ScriptedConn()), uuid.uuid4(), 1, uuid.uuid4(), SV, "activity")
    assert no_tour.value.code == "aa_addon_needs_tour"

    async def fake_plan(ctx: Any, proposal_id: Any, seq: Any, block_id: Any, body: dict[str, Any]) -> dict[str, Any]:
        return {"title": "Lantern Coast Explorer", "component_id": body["component_id"], "currency": "USD",
                "party": 2, "service_date": DAY.isoformat(), "tour_source_id": body["tour_source_id"]}  # fmt: skip

    monkeypatch.setattr(aa_addons.builder, "aa_plan", fake_plan)
    conn = ScriptedConn().on("SELECT o.aa_tour_source_id", "SYN-TOUR-0042")
    plan = await aa_addons.plan(tenant_ctx(conn), uuid.uuid4(), 1, uuid.uuid4(), SV, "activity")
    assert plan["addon"] is True and plan["tour_source_id"] == "SYN-TOUR-0042"

    async def fake_quote(client: Any, tenant_id: Any, the_plan: Any) -> Any:
        return aa_addons.aa_options.AaQuote(
            "q1", D("68.48"), "on_request", dt.datetime(2027, 1, 1, tzinfo=dt.UTC), None
        )

    monkeypatch.setattr(aa_addons.aa_options, "quote", fake_quote)
    named, answer = await aa_addons.quote(FakeBooking(_page(_addon_json())), TENANT, plan)  # type: ignore[arg-type]
    assert named["title"] == "Evening lantern walk" and answer.from_price == D("45")
    with pytest.raises(Conflict) as gone:
        await aa_addons.quote(FakeBooking(_page()), TENANT, plan)  # type: ignore[arg-type]
    assert gone.value.code == "aa_addon_not_offered"


# ── The planner price publish (tripplanner.agency_prices.v1) ─────────────────────────────────────────────────────


def _tariff_item(source_id: str, pax_min: int, pax_max: int | None, pp: str, rrp: str | None) -> dict[str, Any]:
    return {
        "source_id": source_id, "title": "Lantern Coast Explorer", "country": "Vietnam", "duration_days": 6,
        "season": "low", "valid_from": "2027-01-01", "valid_to": "2027-04-30", "pax_min": pax_min, "pax_max": pax_max,
        "priced_for_pax": pax_min, "priced_for_date": "2027-01-01", "price": pp, "price_per_person": pp,
        "agency_discount_pct": "0", "rrp_per_person": rrp, "rrp_currency": "USD" if rrp else None,
    }  # fmt: skip


def test_tour_bases_take_the_bands_of_two() -> None:
    items = [
        contract.TariffItem(**{**_tariff_item("SYN-A", 1, 1, "1500.00", "2000.00"),
                               "valid_from": DAY, "valid_to": DAY, "priced_for_date": DAY, "price": D("1500"),
                               "price_per_person": D("1500"), "agency_discount_pct": D("0"),
                               "rrp_per_person": D("2000")}),
        contract.TariffItem(**{**_tariff_item("SYN-A", 2, 3, "1043.48", "1305.00"),
                               "valid_from": DAY, "valid_to": DAY, "priced_for_date": DAY, "price": D("2086.96"),
                               "price_per_person": D("1043.48"), "agency_discount_pct": D("0"),
                               "rrp_per_person": D("1305")}),
    ]  # fmt: skip
    tariff = contract.Tariff("USD", DAY, "x", "T1", D("1"), DAY, False, tuple(items))
    assert prices.tour_bases(tariff) == {"SYN-A": (D("1043.48"), D("1305"))}


def test_price_list_is_the_final_numbers_only() -> None:
    settings = rounding.PriceSettings({"USD": D("5")}, True, "USD")
    walk = booking_addons.addon(_addon_json(), "USD")
    unpriced = booking_addons.addon(
        _addon_json(component_id=SV2, b2b_price_per_person=None, rrp_per_person=None), "USD"
    )
    now = dt.datetime(2027, 2, 1, 8, tzinfo=dt.UTC)
    body = prices.price_list(
        settings, {"trip_plan": D("18")}, {"SYN-A": (D("1043.48"), D("1305"))}, [walk, unpriced], now
    )
    # 1,043.48 x 1.18 = 1,231.31 -> 1,235 (step 5); the add-on has no markup: its RRP 45 -> 45.
    assert body == {
        "show_prices": True, "currency": "USD", "computed_at": now.isoformat(),
        "tours": [{"source_id": "SYN-A", "from_per_person": "1235"}],
        "addons": [{"component_id": SV, "from_per_person": "45"}],
    }  # fmt: skip
    text = json.dumps(body)
    assert "b2b" not in text and "markup" not in text and "rrp" not in text
    off = prices.price_list(
        rounding.PriceSettings(show_planner_prices=False), {}, {"SYN-A": (D("1"), None)}, [walk], now
    )
    assert off["show_prices"] is False and off["tours"] == [] and off["addons"] == []


class FakeTokens:
    async def token(self, scope: str) -> str:
        return "token"

    def invalidate(self, scope: str) -> None:
        pass


class FakeTripPlanner:
    def __init__(self, status: int = 200) -> None:
        self.status, self.sent = status, []
        self.base_url, self.tokens = "https://planner.example.test", FakeTokens()

    async def transport(self, method: str, url: str, headers: dict, data: bytes | None, _timeout: float) -> Any:
        self.sent.append((method, url, json.loads(data) if data else None))
        body = {"tenant_id": str(TENANT), "show_prices": True, "currency": "USD", "computed_at": "x", "tour_count": 1,
                "addon_count": 1} if self.status == 200 else {"error": "unknown_tenant"}  # fmt: skip

        class Resp:
            status = self.status

        Resp.body = json.dumps(body).encode()  # type: ignore[attr-defined]
        return Resp


@pytest.mark.asyncio
async def test_publish_one_agency(monkeypatch: pytest.MonkeyPatch) -> None:
    tariff = {"currency": "USD", "priced_on": "2027-02-01", "basis": "x", "tier_code": "T1", "fx": {}, "items": [],
              "truncated": False}  # fmt: skip
    monkeypatch.setattr(prices.contract, "tariff", lambda data, currency: contract.Tariff(
        "USD", DAY, "x", "T1", D("1"), DAY, False, ()))  # fmt: skip

    class Ctx:
        conn = ScriptedConn()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def tx(*_: Any, **__: Any) -> Any:
        yield Ctx()

    async def routed(ctx: Any) -> bool:
        return True

    async def settings(ctx: Any) -> rounding.PriceSettings:
        return rounding.PriceSettings({}, True, "USD")

    async def markups(ctx: Any) -> dict[str, D]:
        return {"activity": D("20")}

    monkeypatch.setattr(prices, "system_tx", tx)
    monkeypatch.setattr(prices.leads, "routed_here", routed)
    monkeypatch.setattr(prices.rounding, "settings", settings)
    monkeypatch.setattr(prices.retail, "markups", markups)
    tp = FakeTripPlanner()
    booking = FakeBooking(_page(_addon_json()), tariff)
    now = dt.datetime(2027, 2, 1, tzinfo=dt.UTC)
    handler = prices.PricePublish(tp, booking, now=lambda: now)  # type: ignore[arg-type]
    result = await handler(object(), Job("j1", prices.TP_PRICE_PUBLISH, {"tenant_id": str(TENANT)}, 1, 1))  # type: ignore[arg-type]
    assert result["status"] == "ok" and result["published_count"] == 1
    [(method, url, body)] = tp.sent
    assert method == "PUT" and url.endswith(f"/trip/m2m/v1/tenants/{TENANT}/planner/prices")
    assert body["addons"] == [{"component_id": SV, "from_per_person": "42"}] and booking.asked[0]["pax"] == "2"
    refused = prices.PricePublish(FakeTripPlanner(404), booking, now=lambda: now)  # type: ignore[arg-type]
    out = await refused(object(), Job("j2", prices.TP_PRICE_PUBLISH, {"tenant_id": str(TENANT)}, 1, 1))  # type: ignore[arg-type]
    assert out["refused_unknown_tenant_count"] == 1 and out["status"] == "ok"
    assert (await prices.PricePublish(None, None)(object(), Job("j3", "k", {}, 1, 1)))["status"] == "not_configured"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_hourly_schedule_and_owner_nudge() -> None:
    now = dt.datetime(2027, 2, 1, 8, 30, tzinfo=dt.UTC)
    pool = ScriptedConn().on("INSERT INTO crm.job", "job-1")
    hook = prices.PricePublishSchedule(lambda: now)
    assert await hook(pool) == "job-1" and await hook(pool) is None  # once per hour slot
    assert prices.schedule_key(now) == prices.schedule_key(now + dt.timedelta(minutes=20))
    await prices.enqueue_agency(pool, TENANT)
    keys = [args[2] for args in pool.sql("INSERT INTO crm.job")]
    assert keys[-1].startswith(f"crm_tp_price_publish:{TENANT}:")


# ── The lead (lead.submitted.v1 1.2) ─────────────────────────────────────────────────────────────────────────────


def test_ticked_add_ons_reach_the_agent_with_their_code() -> None:
    lead = {"trip": {"days": 3, "countries": ["Vietnam"], "message": None, "tours": [], "day_plan": [
        {"day": 1, "stops": ["Lantern Bay"], "addons": [{"component_id": SV, "title": "Evening lantern walk"}]},
        {"day": 2, "stops": ["Kelp Island"]},
    ]}}  # fmt: skip
    text = leads.message_text(lead)
    assert f"Day 1 AA add-on: Evening lantern walk ({SV})" in text and "Day 2: Kelp Island" in text


def test_planner_refusal_type_is_shared() -> None:
    assert planner.PlannerRefused(404, "unknown_tenant").code == "unknown_tenant"


# ── Review fixes: hidden lists, the add-on RRP on refresh, rounding per paying traveller ─────────────────────────────


def _publish_fakes(monkeypatch: pytest.MonkeyPatch, *, routed: bool) -> None:
    from contextlib import asynccontextmanager

    class Ctx:
        conn = ScriptedConn()

    @asynccontextmanager
    async def tx(*_: Any, **__: Any) -> Any:
        yield Ctx()

    async def is_routed(ctx: Any) -> bool:
        return routed

    async def settings(ctx: Any) -> rounding.PriceSettings:
        return rounding.PriceSettings({}, True, "VND")

    async def markups(ctx: Any) -> dict[str, D]:
        return {}

    monkeypatch.setattr(prices, "system_tx", tx)
    monkeypatch.setattr(prices.leads, "routed_here", is_routed)
    monkeypatch.setattr(prices.rounding, "settings", settings)
    monkeypatch.setattr(prices.retail, "markups", markups)


class RefusingBooking(FakeBooking):
    def __init__(self, code: str) -> None:
        super().__init__(_page())
        self.code = code

    async def tariff(self, tenant_id: uuid.UUID, *, currency: str, source_id: str | None = None) -> dict[str, Any]:
        from app.booking.client import BookingRefusal

        raise BookingRefusal(403, self.code)


@pytest.mark.asyncio
async def test_an_agency_no_longer_served_gets_a_hidden_list_once(monkeypatch: pytest.MonkeyPatch) -> None:
    now = dt.datetime(2027, 2, 1, tzinfo=dt.UTC)
    hidden_body = {"show_prices": False, "currency": "VND", "computed_at": now.isoformat(), "tours": [], "addons": []}
    _publish_fakes(monkeypatch, routed=False)
    tp, hidden = FakeTripPlanner(), set()
    booking = FakeBooking(_page())
    assert await prices.publish_agency(object(), booking, tp, TENANT, now, hidden) == "hidden_not_routed"  # type: ignore[arg-type]
    assert [b for _, _, b in tp.sent] == [hidden_body] and hidden == {TENANT}
    assert await prices.publish_agency(object(), booking, tp, TENANT, now, hidden) == "skipped_hidden"  # type: ignore[arg-type]
    assert len(tp.sent) == 1  # the hourly sweep does not call TripPlanner again
    # No planner at TripPlanner (unknown_tenant): nothing to hide, marked so the sweep stops asking.
    nobody, none_hidden = FakeTripPlanner(404), set()
    assert await prices.publish_agency(object(), booking, nobody, TENANT, now, none_hidden) == "skipped_no_planner"  # type: ignore[arg-type]
    assert none_hidden == {TENANT}
    # AA stopped selling to the agency: Booking refuses, the list is hidden; another refusal is an error.
    _publish_fakes(monkeypatch, routed=True)
    tp2, hidden2 = FakeTripPlanner(), set()
    out = await prices.publish_agency(object(), RefusingBooking("unknown_partner"), tp2, TENANT, now, hidden2)  # type: ignore[arg-type]
    assert out == "hidden_not_partner" and [b for _, _, b in tp2.sent] == [hidden_body]
    from app.booking.client import BookingRefusal

    with pytest.raises(BookingRefusal):
        await prices.publish_agency(object(), RefusingBooking("invalid"), FakeTripPlanner(), TENANT, now, set())  # type: ignore[arg-type]
    # Routed and priced again: published, and the mark is cleared so a later stop hides it again.
    tariff = {"currency": "VND", "priced_on": "2027-02-01", "basis": "x", "tier_code": "T1", "fx": {}, "items": [],
              "truncated": False}  # fmt: skip
    monkeypatch.setattr(prices.contract, "tariff", lambda data, currency: contract.Tariff(
        "VND", DAY, "x", "T1", D("1"), DAY, False, ()))  # fmt: skip
    again = await prices.publish_agency(object(), FakeBooking(_page(currency="VND"), tariff), tp2, TENANT, now, hidden2)  # type: ignore[arg-type]
    assert again == "published" and TENANT not in hidden2


@pytest.mark.asyncio
async def test_a_locked_or_deleted_crm_agency_is_hidden_by_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.errors import Forbidden

    async def gone(*_: Any, **__: Any) -> str:
        raise Forbidden("agency_locked")

    monkeypatch.setattr(prices, "publish_agency", gone)
    tp = FakeTripPlanner()
    now = dt.datetime(2027, 2, 1, tzinfo=dt.UTC)
    handler = prices.PricePublish(tp, FakeBooking(_page()), now=lambda: now)  # type: ignore[arg-type]
    job = Job("j1", prices.TP_PRICE_PUBLISH, {"tenant_id": str(TENANT)}, 1, 1)
    out = await handler(object(), job)  # type: ignore[arg-type]
    assert out["hidden_agency_gone_count"] == 1 and tp.sent[0][2]["show_prices"] is False
    assert (await handler(object(), job))["skipped_hidden_count"] == 1 and len(tp.sent) == 1  # type: ignore[arg-type]
    down = prices.PricePublish(FakeTripPlanner(503), FakeBooking(_page()), now=lambda: now)  # type: ignore[arg-type]
    assert (await down(object(), job))["skipped_agency_count"] == 1  # type: ignore[arg-type]


def _refresh_plan(**over: Any) -> dict[str, Any]:
    plan = {"option_id": str(uuid.uuid4()), "component_id": SV, "component_kind": "activity", "currency": "USD",
            "party": 2, "service_date": DAY.isoformat(), "from_price": "45.00", "sell": "90.00"}  # fmt: skip
    plan.update(over)
    return plan


def _answer(from_price: D | None = None, quote_id: str | None = "q2") -> Any:
    return aa_addons.aa_options.AaQuote(quote_id, D("68.48"), "on_request", dt.datetime(2027, 1, 1, tzinfo=dt.UTC),
                                        from_price)  # fmt: skip


@pytest.mark.asyncio
async def test_refresh_keeps_an_add_on_s_rrp_and_follows_aa_s_new_one() -> None:
    addon, tour = _refresh_plan(), _refresh_plan(component_kind="trip_plan", component_id="tp:1", from_price=None)
    gone = _refresh_plan(component_id=SV2)
    client = FakeBooking(_page(_addon_json(rrp_per_person={"amount": "48.00", "currency": "USD"})))
    out = await aa_addons.refresh_rrp(client, TENANT, [(addon, _answer()), (tour, _answer()), (gone, _answer())])  # type: ignore[arg-type]
    assert [a.from_price for _, a in out] == [D("48.00"), None, None]
    assert len(client.asked) == 1  # one add-on list per day and party
    down = FakeBooking(contract.ContractError("bad"))
    kept = await aa_addons.refresh_rrp(down, TENANT, [(addon, _answer())])  # type: ignore[arg-type]
    assert kept[0][1].from_price is None  # Booking failed: the stored RRP is kept by apply_refresh (below)


@pytest.mark.asyncio
async def test_apply_refresh_keeps_the_stored_rrp_of_an_add_on() -> None:
    from app.proposals import builder
    from tests.unit.slice_fakes import FakeVault
    from tests.unit.test_builder_service import P, _conn

    addon, tour = _refresh_plan(), _refresh_plan(component_kind="trip_plan", component_id="tp:1", from_price="200.00")
    conn = _conn()
    await builder.apply_refresh(tenant_ctx(conn, role="agent"), FakeVault(), P, 5,  # type: ignore[arg-type]
                                [(addon, _answer()), (tour, _answer())])  # fmt: skip
    rows = conn.sql("UPDATE crm.option SET net_amount = $2, availability = $3")
    assert rows[0][5] == D("45.00")  # the add-on keeps its RRP (Booking gave none again)
    assert rows[1][5] is None  # a tour's "from" price comes from price parity only (none answered here)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("currency", "rrp", "unit", "sell"),
    [("USD", "35.00", "53.00", "106.00"), ("VND", "350000.00", "530000.00", "1060000.00")],
)
async def test_the_suggested_grid_with_an_infant_is_on_the_step(currency: str, rrp: str, unit: str, sell: str) -> None:
    from app.proposals import builder
    from tests.unit.test_builder_service import O2, P, _conn, _options, _proposal, _run

    options = _options()
    options[1] = {**options[1], "aa_from_price": D(rrp)}
    proposal = _proposal(currency=currency, party_child_ages=[1],
                         room_config='[{"type": "double", "adults": 2, "children": 1}]')  # fmt: skip
    # RRP x party of 3 (the infant counted, AA's RRP rule) = 105 / 1,050,000; shared by the 2 paying travellers and
    # rounded up per traveller to the step (USD 1, VND 10,000), then times them.
    conn = _conn(proposal=proposal, options=options)
    conn.on("SELECT p.discount_pct FROM crm.proposal p", None)
    conn.on("FROM crm.option_price WHERE proposal_id", [])
    await _run(builder.set_option_prices, P, 5, O2, None, use_suggested=True, conn=conn)
    rows = [(a[2], a[3], a[5]) for a in conn.sql("INSERT INTO crm.option_price")]
    assert (str(rows[0][0]), str(rows[0][1]), rows[0][2]) == ("adult", unit, 2)
    assert (str(rows[1][0]), rows[1][1], rows[1][2]) == ("infant", D("0.00"), 1)
    assert conn.sql("UPDATE crm.option SET sell_amount = $2")[0][1] == D(sell)
