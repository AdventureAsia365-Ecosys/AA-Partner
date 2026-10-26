"""AA-947 test doubles: an in-memory AA-Booking ``booking.b2b.v1`` and a CIS ``catalog.http.v1`` behind the
:mod:`app.cis.http` transport shape, plus the aa-m2m token endpoint. Synthetic data only (no real product, price or
person). Shared by the unit and the integration tests."""

from __future__ import annotations

import json
import urllib.parse
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.booking.client import BookingClient, BookingConfig
from app.catalog.client import CatalogConfig
from app.cis.http import HttpError, HttpResponse
from app.cis.m2m import M2mCredentials, TokenProvider


def _in_two_days() -> str:
    """A quote / hold expiry relative to now: a fixed date made every AA item stale once it passed (S252)."""
    return (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0).isoformat()


TOKEN_URL = "https://m2m.local.test/oauth2/token"
BOOKING_URL = "https://booking.local.test"
CIS_URL = "https://cis.local.test"
HASH = "a" * 64


def _resp(status: int, body: Any = None, headers: Mapping[str, str] | None = None) -> HttpResponse:
    raw = b"" if body is None else json.dumps(body).encode()
    return HttpResponse(status, raw, dict(headers or {}))


@dataclass
class FakeBooking:
    """Booking as the CRM sees it. ``down``: every call fails (no HTTP answer). ``lose_next_answer``: the next write
    is done in Booking but its answer is lost (a timeout after the commit), so the CRM must retry with the same
    Idempotency-Key and get the same hold / booking back. ``on_hold``: awaited once while a new hold call is on its
    way (after Booking made the hold, before the CRM gets the answer), to race another sync run against it. Calls
    made while ``down`` count under ``calls["down"]``."""

    down: bool = False
    lose_next_answer: bool = False
    b2b_price: str = "1000.00"
    currency_rate: str = "1"
    from_price: str | None = "800.00"
    availability: str = "available"
    per_night: bool = False  # b2b_price is a night's price: a quote line costs it times its nights (1 when absent)
    hold_refusal: str | None = None
    # AA-950: a quote answers every line (the hold set of a proposal version quotes all its AA items at once);
    # ``line_availability`` overrides ``availability`` per line_ref; ``hold_expires_in`` sets a new hold's expiry.
    line_availability: dict[str, str] = field(default_factory=dict)
    hold_expires_in: timedelta | None = None
    confirm_line_status: str = "on_request"
    calls: Counter[str] = field(default_factory=Counter)
    holds: dict[str, dict[str, Any]] = field(default_factory=dict)
    hold_keys: dict[str, str] = field(default_factory=dict)
    bookings: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    tenants: list[str] = field(default_factory=list)
    quotes: int = 0
    on_hold: Callable[[], Awaitable[None]] | None = None

    async def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout_s: float
    ) -> HttpResponse:
        if url == TOKEN_URL:
            return _resp(200, {"access_token": "tok", "token_type": "Bearer", "expires_in": 3600})
        if self.down:
            self.calls["down"] += 1
            raise HttpError("timeout")
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path.removeprefix("/booking/b2b/v1")
        query = dict(urllib.parse.parse_qsl(parsed.query))
        data = json.loads(body) if body else None
        self.tenants.append(headers.get("X-AA-Tenant-Id", ""))
        key = f"{method} {path.split('/')[1] if '/' in path else path}"
        self.calls[key] += 1
        answer = self._route(method, path, query, data, headers.get("Idempotency-Key"))
        if self.on_hold is not None and method == "POST" and path == "/holds":
            hook, self.on_hold = self.on_hold, None
            await hook()
        if self.lose_next_answer and method != "GET":
            self.lose_next_answer = False
            raise HttpError("timeout")
        return answer

    def _route(self, method: str, path: str, query: dict[str, str], data: Any, key: str | None) -> HttpResponse:
        if method == "POST" and path == "/quotes":
            self.quotes += 1
            answered = []
            for line in data["lines"]:
                price = self.b2b_price
                if self.per_night:
                    price = str(Decimal(self.b2b_price) * int(line.get("nights") or 1))
                answered.append({"line_ref": line["line_ref"],
                                 "b2b_price": {"amount": price, "currency": data["currency"]}, "unit": "per_line",
                                 "availability": self.line_availability.get(line["line_ref"], self.availability),
                                 "departure_id": None})  # fmt: skip
            total = str(sum(Decimal(a["b2b_price"]["amount"]) for a in answered))
            return _resp(201, {
                "quote_id": f"q{self.quotes}",
                "currency": data["currency"],
                "fx": {"base_currency": "USD", "quote_currency": data["currency"], "rate_per_usd": self.currency_rate,
                       "rate_date": "2026-10-25", "source": "test"},
                "valid_until": _in_two_days(),
                "price_hash": HASH[:-1] + str(self.quotes % 10),
                "lines": answered,
                "total_b2b_price": {"amount": total, "currency": data["currency"]},
                "x_future_field": True,
            })  # fmt: skip
        if method == "GET" and path == "/price-parity":
            amount = {"amount": self.from_price, "currency": query["currency"]} if self.from_price else None
            return _resp(200, {"tour": {"source_system": "cis", "source_id": query["source_id"]},
                               "currency": query["currency"], "from_price": amount,
                               "basis": "per_person_2pax_low_season"})  # fmt: skip
        if method == "POST" and path == "/holds":
            if self.hold_refusal:
                return _resp(409 if self.hold_refusal == "price_changed" else 422,
                             {"error": self.hold_refusal, "message": "refused"})  # fmt: skip
            if key in self.hold_keys:
                return _resp(201, self.holds[self.hold_keys[key]])
            hold_id = f"h{len(self.holds) + 1}"
            self.hold_keys[key or ""] = hold_id
            self.holds[hold_id] = {
                "hold_id": hold_id, "quote_id": data["quote_id"], "status": "active",
                "expires_at": _in_two_days() if self.hold_expires_in is None else (
                    datetime.now(UTC) + self.hold_expires_in).replace(microsecond=0).isoformat(),
                "extended_count": 0,
                "lines": [{"line_ref": data["proposal_ref"].removeprefix("crm-line:"), "line_status": "available"}],
            }  # fmt: skip
            return _resp(201, self.holds[hold_id])
        if path.startswith("/holds/"):
            parts = path.split("/")
            hold = self.holds.get(parts[2])
            if hold is None:
                return _resp(404, {"error": "not_found", "message": "unknown hold"})
            if method == "GET":
                return _resp(200, hold)
            if method == "DELETE":
                if hold["status"] == "confirmed":
                    return _resp(422, {"error": "invalid", "message": "confirmed"})
                hold["status"] = "released"
                return _resp(204)
            if method == "POST" and parts[-1] == "confirm":
                booking = self.bookings.get(hold["hold_id"])
                if booking is None:
                    hold["status"] = "confirmed"
                    booking = {
                        "booking_id": f"b-{hold['hold_id']}", "hold_id": hold["hold_id"],
                        "trip_case_code": f"TC-{len(self.bookings) + 1:04d}",
                        "lines": [{"line_ref": hold["lines"][0]["line_ref"], "status": self.confirm_line_status}],
                    }  # fmt: skip
                    self.bookings[hold["hold_id"]] = booking
                return _resp(201, booking)
        if method == "GET" and path == "/events":
            after = int(query.get("after", "0") or 0)
            items = [e for e in self.events if int(e["cursor"]) > after][: int(query.get("limit", "200"))]
            return _resp(200, {"items": items, "next_cursor": items[-1]["cursor"] if items else str(after)})
        if method == "GET" and path == "/products":
            return _resp(200, {"items": [{
                "tour": {"source_system": "cis", "source_id": "T-100"}, "trip_plan_id": "p1", "sellable": True,
                "has_fixed_departures": True, "updated_at": "2026-10-20T00:00:00+00:00",
                "components": [{"component_id": "tp:1", "kind": "trip_plan", "day_no": None},
                               {"component_id": "rt:2", "kind": "hotel_room", "day_no": 1}],
            }], "next_cursor": None})  # fmt: skip
        if method == "GET" and path == "/departures":
            return _resp(200, {"items": [{
                "departure_id": "d1", "tour": {"source_system": "cis", "source_id": query["source_id"]},
                "start_date": "2026-12-01", "capacity": 10, "held": 0, "sold": 0, "available": 10,
                "status": "scheduled", "auto_confirm": True,
            }]})  # fmt: skip
        return _resp(404, {"error": "not_found", "message": "no route"})

    def add_event(self, type_: str, **fields: Any) -> dict[str, Any]:
        event = {"event_id": f"e{len(self.events) + 1}", "cursor": str(len(self.events) + 1), "type": type_,
                 "occurred_at": "2026-10-25T00:00:00+00:00", **fields}  # fmt: skip
        self.events.append(event)
        return event


def tour_body(source_id: str = "T-100", title: str = "Mekong Delta 3 days", digest: str = "sha256:1") -> dict:
    return {
        "source_system": "cis", "source_id": source_id, "title": title, "summary": None, "country_code": "VN",
        "duration_days": 3, "content_hash": digest, "days": [], "inclusions": [], "exclusions": [],
        "facets": {"activities": []}, "photos": [],
    }  # fmt: skip


@dataclass
class FakeCis:
    """CIS catalog.http.v1: ``tours`` = source_id -> body; ``retired`` / ``paused`` keys answer 410 / 404."""

    tours: dict[str, dict[str, Any]] = field(default_factory=dict)
    retired: set[str] = field(default_factory=set)
    paused: set[str] = field(default_factory=set)
    down: bool = False
    calls: Counter[str] = field(default_factory=Counter)

    async def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout_s: float
    ) -> HttpResponse:
        if url == TOKEN_URL:
            return _resp(200, {"access_token": "tok", "token_type": "Bearer", "expires_in": 3600})
        if self.down:
            return _resp(503, {"error": "unavailable", "message": "down"})
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path.removeprefix("/catalog/v1")
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if path == "/tours":
            self.calls["list"] += 1
            keys = sorted(self.tours)
            start = int(query.get("cursor", "0") or 0)
            page = keys[start : start + int(query.get("limit", "50"))]
            nxt = str(start + len(page)) if start + len(page) < len(keys) else None
            keep = ("source_system", "source_id", "title", "country_code", "duration_days", "content_hash")
            items = [{k: self.tours[s][k] for k in keep} | {"updated_at": "2026-10-20T00:00:00+00:00"} for s in page]
            return _resp(200, {"items": items, "next_cursor": nxt})
        source_id = urllib.parse.unquote(path.removeprefix("/tours/cis/"))
        self.calls["get"] += 1
        if source_id in self.retired:
            return _resp(410, {"error": "gone", "message": "retired", "reason": "source_deleted"})
        if source_id in self.paused:
            return _resp(404, {"error": "not_found", "message": "paused", "reason": "master_inactive"})
        tour = self.tours.get(source_id)
        if tour is None:
            return _resp(404, {"error": "not_found", "message": "unknown"})
        if headers.get("If-None-Match") == f'"{tour["content_hash"]}"':
            return _resp(304)
        return _resp(200, tour, {"ETag": f'"{tour["content_hash"]}"'})


def tokens(transport: Any) -> TokenProvider:
    return TokenProvider(M2mCredentials("crmclient1", "secret", TOKEN_URL), transport=transport)


def booking_client(fake: FakeBooking) -> BookingClient:
    return BookingClient(BookingConfig(base_url=BOOKING_URL, tokens=tokens(fake), transport=fake))


def catalog_config(fake: FakeCis) -> CatalogConfig:
    return CatalogConfig(base_url=CIS_URL, tokens=tokens(fake), transport=fake)


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def dec(value: str) -> Decimal:
    return Decimal(value)
