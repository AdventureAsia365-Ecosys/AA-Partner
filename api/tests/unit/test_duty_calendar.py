"""AA-961 (CRM-30) without a database: where a trip is on a day, the bulk reveal rules (card R: a reason above the
threshold, the hourly limit), the ICS rendering (no names, titles or money; escaping and folding), the Booking
incident event parsing, and every duty / calendar route reaching its helper (the services run on PostgreSQL in
tests/integration/test_duty_calendar_api.py)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.booking import contract
from app.calendar import events, feeds, ics
from app.calendar import public as calendar_public
from app.duty import service
from app.errors import Invalid, NotFound, RateLimited
from app.main import create_app
from app.observability import reveal_meter
from app.routers import calendar as calendar_router
from app.routers import duty as duty_router
from app.routers import public as public_router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, TENANT, FakeVault, enc, fake_tx, tenant_ctx

ID = "00000000-0000-4000-8000-0000000000e1"
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000aa")


# ── where a trip is ──────────────────────────────────────────────────────────────────────────────


def _trip(**over: Any) -> dict[str, Any]:
    base = {
        "trip_id": TRIP,
        "start_date": dt.date(2026, 11, 10),
        "end_date": dt.date(2026, 11, 20),
        "destinations": ["laos", "vietnam"],
    }
    base.update(over)
    return base


def _line(**over: Any) -> dict[str, Any]:
    base = {
        "booking_line_id": uuid.uuid4(),
        "title": "Ha Giang loop",
        "supplier_kind": "aa",
        "departure_id": "dep-1",
        "service_from": dt.date(2026, 11, 12),
        "service_to": dt.date(2026, 11, 15),
        "country_code": "VN",
    }
    base.update(over)
    return base


def test_where_on_prefers_the_running_aa_line_then_the_plan_then_the_destination() -> None:
    plan = {3: {"place": "Ha Giang"}, 1: {"place": "Vientiane"}}
    region, place, day_no, running = service.where_on(_trip(), dt.date(2026, 11, 12), [_line()], plan)
    assert (region, place, day_no, len(running)) == ("VN", "Ha Giang", 3, 1)
    # Before the line runs: the first known destination, the plan's place.
    region, place, day_no, running = service.where_on(_trip(), dt.date(2026, 11, 10), [_line()], plan)
    assert (region, place, day_no, running) == ("LA", "Vientiane", 1, [])
    # A one-day line (no service_to) runs that day only; an unknown destination is "unknown".
    one_day = _line(service_to=None, country_code=None)
    region, _, _, running = service.where_on(_trip(destinations=["not_sure"]), dt.date(2026, 11, 12), [one_day], {})
    assert region == service.UNKNOWN_REGION and len(running) == 1
    # Before the start or after the end there is no day number.
    assert service.where_on(_trip(), dt.date(2026, 11, 9), [], {})[2] is None
    assert service.where_on(_trip(), dt.date(2026, 11, 21), [], {})[2] is None


def _bulk_ctx(n: int) -> tuple[Any, list[uuid.UUID]]:
    ids = [uuid.uuid4() for _ in range(n)]
    rows = [
        {
            "traveler_id": i,
            "display_name_enc": enc("traveler", "display_name_enc", f"Traveller {k}"),
            "phone_enc": enc("traveler", "phone_enc", "+84900000000") if k % 2 == 0 else None,
            "email_enc": None,
            "emergency_contact_enc": None,
        }
        for k, i in enumerate(ids)
    ]
    conn = ScriptedConn().on("FROM crm.traveler WHERE traveler_id = ANY", rows)
    return tenant_ctx(conn, role="assistant"), ids


@pytest.fixture(autouse=True)
def _meter() -> Any:
    reveal_meter.METER.reset()
    yield
    reveal_meter.METER.reset()


@pytest.mark.asyncio
async def test_bulk_reveal_up_to_the_threshold_needs_no_reason_and_logs_one_event_per_traveller() -> None:
    ctx, ids = _bulk_ctx(service.REASON_THRESHOLD)
    out = await service.bulk_reveal(ctx, FakeVault(), ids, ["display_name", "phone"], None)
    assert out["reason"] == "duty_of_care" and len(out["travellers"]) == service.REASON_THRESHOLD
    assert out["travellers"][0]["display_name"] == "Traveller 0" and out["travellers"][1]["phone"] is None
    events_ = [e for e in ctx.audit_events if e.action == "reveal"]
    assert len(events_) == len(ids) and {e.reason for e in events_} == {"duty_of_care"}
    assert events_[0].fields == ("display_name", "phone")


@pytest.mark.asyncio
async def test_r_bulk_reveal_above_the_threshold_needs_a_reason() -> None:
    ctx, ids = _bulk_ctx(service.REASON_THRESHOLD + 1)
    with pytest.raises(Invalid) as err:
        await service.bulk_reveal(ctx, FakeVault(), ids, ["phone"], None)
    assert err.value.code == "reason_required" and err.value.extra["threshold"] == service.REASON_THRESHOLD
    with pytest.raises(Invalid):
        await service.bulk_reveal(ctx, FakeVault(), ids, ["phone"], "other")  # not a bulk reason
    assert ctx.audit_events == []  # nothing opened
    out = await service.bulk_reveal(ctx, FakeVault(), ids, ["phone"], "incident_response")
    assert len(out["travellers"]) == len(ids)


@pytest.mark.asyncio
async def test_r_bulk_reveal_is_rate_limited_per_member_per_hour() -> None:
    reveal_meter.METER.record(TENANT, MEMBER, service.HOURLY_LIMIT - 2)
    ctx, ids = _bulk_ctx(3)
    with pytest.raises(RateLimited) as err:
        await service.bulk_reveal(ctx, FakeVault(), ids, ["phone"], None)
    assert err.value.code == "reveal_rate_limited" and err.value.retry_after_s == 3600
    denied = [e for e in ctx.audit_events if e.outcome == "denied"]
    assert len(denied) == 1 and denied[0].reason == "rate_limited" and len(denied[0].object_ids) == 3
    # Two still fit.
    ctx, ids = _bulk_ctx(2)
    assert len((await service.bulk_reveal(ctx, FakeVault(), ids, ["phone"], None))["travellers"]) == 2


@pytest.mark.asyncio
async def test_bulk_reveal_refuses_unknown_travellers_and_fields() -> None:
    ctx, ids = _bulk_ctx(2)
    with pytest.raises(NotFound):
        await service.bulk_reveal(ctx, FakeVault(), [*ids, uuid.uuid4()], ["phone"], None)
    with pytest.raises(Invalid):
        await service.bulk_reveal(ctx, FakeVault(), ids, ["passport_no"], None)
    with pytest.raises(Invalid):
        await service.bulk_reveal(ctx, FakeVault(), [uuid.uuid4() for _ in range(service.MAX_BULK + 1)], ["phone"], "x")
    assert service.needs_sensitive(["phone", "emergency_contact"]) and not service.needs_sensitive(["email"])


# ── ICS ──────────────────────────────────────────────────────────────────────────────────────────


NOW = dt.datetime(2026, 11, 1, 3, 0, tzinfo=dt.UTC)


def _events() -> list[dict[str, Any]]:
    tz = dt.timezone(dt.timedelta(hours=7))
    return [
        {
            "kind": "departure",
            "id": f"departure-{TRIP}",
            "date": dt.date(2026, 11, 10),
            "all_day": True,
            "trip_id": str(TRIP),
            "title": "Nguyen family, Ha Giang",
        },
        {
            "kind": "payment_due",
            "id": "payment-1",
            "date": dt.date(2026, 11, 5),
            "all_day": True,
            "trip_id": str(TRIP),
            "title": "Nguyen family, Ha Giang",
            "payment_kind": "balance",
            "amount": {"amount": "1250.00", "currency": "USD"},
        },
        {
            "kind": "call",
            "id": "call-1",
            "date": dt.date(2026, 11, 3),
            "starts_at": dt.datetime(2026, 11, 3, 9, 30, tzinfo=tz),
            "ends_at": dt.datetime(2026, 11, 3, 10, 0, tzinfo=tz),
            "all_day": False,
            "trip_id": None,
            "title": None,
        },
        {
            "kind": "task",
            "id": "task-1",
            "date": dt.date(2026, 11, 4),
            "starts_at": dt.datetime(2026, 11, 4, 8, 0, tzinfo=tz),
            "all_day": False,
            "trip_id": str(TRIP),
            "title": "Nguyen family, Ha Giang",
            "task_kind": "aa_incident",
        },
    ]


def test_ics_carries_no_title_name_or_money_and_is_valid_rfc5545() -> None:
    text = ics.render(_events(), calendar_name="Lotus Travel, CRM", web_origin="https://partner.example", now=NOW)
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "Nguyen" not in text and "Ha Giang" not in text and "1250" not in text
    assert "X-WR-CALNAME:Lotus Travel\\, CRM" in text
    assert f"SUMMARY:Departure · trip {str(TRIP)[:8]}" in text and "Balance due" in text
    assert "DTSTART;VALUE=DATE:20261110\r\nDTEND;VALUE=DATE:20261111" in text
    assert "DTSTART:20261103T023000Z\r\nDTEND:20261103T030000Z" in text  # the call, in UTC
    assert "DTSTART:20261104T010000Z\r\nDTEND:20261104T011500Z" in text  # a task lasts 15 minutes
    assert "Task due: aa incident" in text
    assert f"UID:departure-{TRIP}@crm" in text and f"URL:https://partner.example/pipeline/{TRIP}" in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))


def test_ics_escapes_and_folds_on_character_boundaries() -> None:
    assert ics.escape("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"
    long = "SUMMARY:" + "Đà Lạt " * 20
    folded = ics.fold(long)
    assert all(len(p.encode()) <= 75 for p in folded.split("\r\n"))
    assert folded.replace("\r\n ", "") == long


# ── Booking incident events ─────────────────────────────────────────────────────────────────────


def test_incident_events_read_the_code_and_the_time_tolerantly() -> None:
    ev = contract.event(
        {
            "event_id": "ev_1",
            "type": "departure.incident",
            "departure_id": "dep-1",
            "incident_code": "road",
            "occurred_at": "2026-11-15T01:00:00Z",
        }  # fmt: skip
    )
    assert ev.incident_code == "road" and ev.occurred_at == dt.datetime(2026, 11, 15, 1, tzinfo=dt.UTC)
    odd = contract.event({"event_id": "ev_2", "type": "departure.incident", "incident_code": "guest ill"})
    assert odd.incident_code is None and odd.occurred_at is None and odd.departure_id is None


# ── calendar window ─────────────────────────────────────────────────────────────────────────────


def test_calendar_window_and_zone() -> None:
    with pytest.raises(Invalid):
        events.window(dt.date(2026, 11, 2), dt.date(2026, 11, 1))
    with pytest.raises(Invalid):
        events.window(dt.date(2026, 1, 1), dt.date(2027, 3, 1))
    assert str(events.zone("Not/AZone")) == events.DEFAULT_TZ and str(events.zone("Asia/Bangkok")) == "Asia/Bangkok"
    assert feeds.feed_url("https://p.example", "tok") == "https://p.example/crm/public/v1/calendar/tok.ics"


# ── routes ───────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> tuple[TestClient, dict[str, str]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    ctx = tenant_ctx(ScriptedConn(), role=role)

    def tx(*a: Any, **kw: Any) -> Any:
        margin.current_member_role.set(role)
        return fake_tx(ctx)(*a, **kw)

    for module in (duty_router, calendar_router):
        monkeypatch.setattr(module, "request_pool", _pool)
        monkeypatch.setattr(module, "tenant_tx", tx)
    monkeypatch.setattr(duty_router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(duty_router, "require_keys", lambda keys: keys)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple, dict]]:
    out: list[tuple[str, tuple, dict]] = []

    def make(n: str) -> Any:
        async def fn(*args: Any, **kwargs: Any) -> Any:
            out.append((n, args, kwargs))
            return {"ok": n}

        return fn

    for name in ("travelling", "reach", "bulk_reveal", "trip_incidents"):
        monkeypatch.setattr(service, name, make(name))
    for name in ("status", "create", "revoke", "team"):
        monkeypatch.setattr(feeds, name, make(f"feed_{name}"))
    monkeypatch.setattr(events, "agency_calendar", make("agency_calendar"))
    return out


def test_every_route_reaches_its_helper(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "assistant")
    requests = [
        ("GET", "/crm/v1/duty/travelling?from=2026-11-10&days=7", None, 200),
        ("GET", "/crm/v1/duty/reach?region=VN&on=2026-11-12", None, 200),
        ("POST", "/crm/v1/duty/reveal", {"traveler_ids": [ID], "fields": ["display_name", "emergency_contact"]}, 200),
        ("GET", f"/crm/v1/trips/{ID}/incidents", None, 200),
        ("GET", "/crm/v1/calendar?from=2026-11-01&to=2026-11-30&scope=agency", None, 200),
        ("GET", "/crm/v1/calendar/feed", None, 200),
        ("POST", "/crm/v1/calendar/feed", {"scope": "mine"}, 201),
        ("POST", f"/crm/v1/calendar/feeds/{ID}/revoke", None, 200),
        ("GET", "/crm/v1/calendar/feeds", None, 200),
    ]
    with client:
        for method, path, body, status in requests:
            res = client.request(method, path, headers=h, json=body)
            assert res.status_code == status, (method, path, res.text)
        bad = [
            ("GET", "/crm/v1/duty/travelling?days=40"),
            ("GET", "/crm/v1/duty/reach?region=vietnam"),
            ("GET", "/crm/v1/calendar?scope=everyone"),
        ]
        for method, path in bad:
            assert client.request(method, path, headers=h).status_code == 422, path
        res = client.post("/crm/v1/duty/reveal", headers=h, json={"traveler_ids": [ID], "fields": ["passport_no"]})
        assert res.status_code == 422
    by_name = {n: (a, kw) for n, a, kw in calls}
    assert by_name["travelling"][1] == {"start": dt.date(2026, 11, 10), "days": 7}
    assert by_name["bulk_reveal"][0][3] == ["display_name", "emergency_contact"]
    assert by_name["agency_calendar"][1]["mine"] is False
    assert by_name["feed_create"][1]["scope"] == "mine" and by_name["feed_create"][1]["origin"].startswith("https://")


def test_public_feed_route_serves_text_calendar(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    seen: list[str] = []

    async def feed(pool: Any, token: str, *, web_origin: str) -> str:
        seen.append(token)
        if token.startswith("x"):
            raise NotFound("feed_not_found")
        return "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"

    monkeypatch.setattr(public_router, "request_pool", _pool)
    monkeypatch.setattr(calendar_public, "feed", feed)
    token = "a" * 43
    with TestClient(app) as client:
        res = client.get(f"/crm/public/v1/calendar/{token}.ics")
        assert res.status_code == 200 and res.headers["content-type"].startswith("text/calendar")
        assert res.headers["cache-control"] == "private, max-age=300" and res.text.startswith("BEGIN:VCALENDAR")
        assert client.get(f"/crm/public/v1/calendar/{'x' * 43}.ics").status_code == 404
        assert client.get("/crm/public/v1/calendar/short.ics").status_code == 422
    assert seen == [token, "x" * 43]
