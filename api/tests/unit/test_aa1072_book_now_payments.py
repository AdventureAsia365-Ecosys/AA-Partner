"""AA-1072: the Python branches of "Book now" (``app.intake.booking``, ``app.intake.booking_settings``), the VNPay
protocol (``app.payments.vnpay``), the agency's own gateway (``app.payments.gateway``), the pay-online flag and card
window (``app.payments.claims``) and the gateway payment paths of ``app.payments.service``, over a
:class:`ScriptedConn` and the in-memory Booking (synthetic data, invented names). The SQL, RLS and the whole flow run
on PostgreSQL in tests/integration/test_aa1072_book_now_api.py."""

from __future__ import annotations

import datetime as dt
import json
import urllib.parse
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.booking.products import ProductCache
from app.errors import Conflict, Forbidden, Gone, Invalid, NotFound, RateLimited, ServiceError, Unavailable
from app.intake import booking, booking_settings
from app.payments import claims, gateway, providers, vnpay
from app.payments import service as payments
from app.proposals import aa_options, pricing
from app.settings import Settings
from app.tenancy import PaymentSessionRef
from tests.aa_fakes import FakeBooking, booking_client
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, intake_form, system_ctx, tenant_ctx

TEST = Settings(environment="test", db_ssl="disable")
PROD = SimpleNamespace(environment="prod", messaging_hooks_origin=lambda: "https://client.example.test")
NOW = dt.datetime(2031, 3, 1, 9, 0, tzinfo=dt.UTC)
SECRET = "an-invented-test-secret-0001"
REF = "a1b2c3d4e5f6a7b8c9d0e1f2"
INVOICE = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000d2")
SESSION = uuid.UUID("00000000-0000-4000-8000-0000000000d3")
ACCOUNT = uuid.UUID("00000000-0000-4000-8000-0000000000d4")
CREDENTIAL = uuid.UUID("00000000-0000-4000-8000-0000000000d5")
BANDS = pricing.Settings(child_age_max=11, infant_age_max=1, discount_owner_limit_pct=Decimal("10"))


@contextmanager
def _raises(cls: type[ServiceError], code: str) -> Iterator[None]:
    with pytest.raises(cls) as err:
        yield
    assert err.value.code == code, err.value.code


class SecretVault(FakeVault):
    """FakeVault + the integration secret and the address index."""

    def __init__(self, secret: dict[str, str] | None = None) -> None:
        super().__init__()
        self.secret = secret or {"hash_secret": SECRET, "mode": "stub"}
        self.sealed: list[str] = []

    async def encrypt_secret(self, credential_id: Any, value: str) -> bytes:
        self.sealed.append(value)
        return b"sealed-" + value.encode()

    async def decrypt_secret(self, credential_id: Any, blob: bytes) -> str:
        return json.dumps(self.secret)

    async def address_index(self, table: str, column: str, value: str) -> bytes:
        return b"h" * 32


# ── vnpay: the protocol ───────────────────────────────────────────────────────────────────────────


def _request(**over: Any) -> vnpay.PayRequest:
    base: dict[str, Any] = dict(
        tmn_code="TESTSTUB1",
        secret=SECRET,
        txn_ref=REF,
        amount=Decimal("3600000"),
        order_info="Invoice INV-000001 deposit",
        return_url="https://client.example.test/c/p/paid",
        client_ip="198.51.100.7",
        locale="vn",
        created=NOW,
        expires=NOW + dt.timedelta(minutes=15),
    )
    base.update(over)
    return vnpay.PayRequest(**base)


def test_the_pay_url_is_signed_over_the_sorted_fields() -> None:
    url = vnpay.pay_url(vnpay.PAY_URLS["sandbox"], _request())
    fields = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith(vnpay.PAY_URLS["sandbox"] + "?")
    assert fields["vnp_Amount"] == "360000000" and fields["vnp_CurrCode"] == "VND" and fields["vnp_Locale"] == "vn"
    assert fields["vnp_CreateDate"] == "20310301160000"  # GMT+7
    assert vnpay.verify(fields, SECRET)
    assert not vnpay.verify(fields, "another-secret-of-16")
    assert not vnpay.verify({**fields, "vnp_Amount": "1"}, SECRET)
    assert not vnpay.verify({k: v for k, v in fields.items() if k != "vnp_SecureHash"}, SECRET)
    assert not vnpay.verify(fields, "")
    en = dict(
        urllib.parse.parse_qsl(urllib.parse.urlsplit(vnpay.pay_url("x", _request(locale="en", client_ip=""))).query)
    )
    assert en["vnp_Locale"] == "en" and en["vnp_IpAddr"] == "127.0.0.1"


def test_order_info_is_ascii_and_never_empty() -> None:
    assert vnpay.order_info("INV-000002", "deposit") == "Invoice INV-000002 deposit"
    assert vnpay.order_info(None, "đặt cọc!") == "Invoice  dat coc"
    assert vnpay.order_info(None, "") == "Invoice"
    assert vnpay.ipn_answer("97") == {"RspCode": "97", "Message": "Invalid signature"}
    assert vnpay.ipn_answer("xx")["Message"] == vnpay.MESSAGES[vnpay.OTHER]


def test_a_notice_is_read_and_checked() -> None:
    ok = vnpay.notice_fields(
        tmn_code="TESTSTUB1",
        txn_ref=REF,
        amount=Decimal("3600000"),
        succeeded=True,
        transaction_no="14012345",
        when=NOW,
        secret=SECRET,
    )
    assert vnpay.verify(ok, SECRET)
    notice = vnpay.read_notice(ok)
    assert notice.succeeded and notice.amount == Decimal("3600000") and notice.transaction_no == "14012345"
    assert notice.tmn_code == "TESTSTUB1" and notice.txn_ref == REF
    failed = vnpay.read_notice(
        vnpay.notice_fields(
            tmn_code="T", txn_ref=REF, amount=Decimal(1), succeeded=False, transaction_no="1", when=NOW, secret=SECRET
        )
    )
    assert not failed.succeeded and failed.response_code == "24" and failed.transaction_no is None
    for bad in (
        {"vnp_TxnRef": "short"},
        {"vnp_Amount": "12a"},
        {"vnp_ResponseCode": "!!"},
        {"vnp_TransactionNo": "has space"},
    ):
        with pytest.raises(vnpay.NoticeInvalid):
            vnpay.read_notice({**ok, **bad})


# ── booking: the answers and the price ────────────────────────────────────────────────────────────


def _press(**over: Any) -> dict[str, Any]:
    base = {
        "tour": "T-77",
        "departure_id": "d1",
        "adults": 2,
        "child_ages": [7, 1],
        "name": "Ilse Varga",
        "email": "ilse@example.test",
        "phone": "+84 912 000 222",
        "consent_contact": True,
        "client_submission_id": str(uuid.uuid4()),
    }
    base.update(over)
    return base


def test_the_party_and_the_answers_are_checked() -> None:
    assert booking.read_party({"adults": 2, "child_ages": [3]}) == booking.Party(2, (3,))
    for bad in (
        {"adults": 0},
        {"adults": 21},
        {"adults": True},
        {"adults": 2, "child_ages": [18]},
        {"adults": 2, "child_ages": "7"},
        {"adults": 2, "child_ages": list(range(11))},
    ):
        with _raises(Invalid, "invalid_party"):
            booking.read_party(bad)
    b = booking.read_booking(_press(message="  A quiet cabin  ", consent_marketing=True))
    assert b.party == booking.Party(2, (7, 1)) and b.email == "ilse@example.test" and b.consent_marketing
    assert b.message == "A quiet cabin" and b.phone and b.phone.startswith("+84")
    with _raises(Invalid, "consent_required"):
        booking.read_booking(_press(consent_contact=False))
    with _raises(Invalid, "invalid_submission"):
        booking.read_booking([1])
    with pytest.raises(Invalid) as err:
        booking.read_booking(
            _press(
                name="",
                email="nope",
                phone="12",
                consent_marketing="yes",
                client_submission_id="x",
                tour="",
                departure_id="bad id",
            )
        )
    assert err.value.extra["fields"] == ["name", "email", "phone", "consent_marketing", "client_submission_id", "tour"]
    with pytest.raises(Invalid) as err:
        booking.read_booking(_press(email="nope"))
    assert err.value.extra["fields"] == ["email"]
    assert [booking.Party(n, ()).party_type for n in (1, 2, 4, 9)] == ["solo", "couple", "friends", "group"]
    assert booking.Party(2, (5,)).party_type == "family"
    assert json.loads(booking.Party(2, (5,)).as_json()) == {"adults": 2, "child_ages": [5]}


def test_the_grid_rounds_up_to_the_currency_unit_and_infants_are_free() -> None:
    rows = booking.price_grid(Decimal("10000000"), booking.Party(2, (7, 1)), BANDS, "VND")
    assert rows == [
        {"pax_type": "adult", "unit_sell": Decimal("3333334.00"), "unit_net": None, "qty": 2},
        {"pax_type": "child", "unit_sell": Decimal("3333334.00"), "unit_net": None, "qty": 1},
        {"pax_type": "infant", "unit_sell": Decimal("0.00"), "unit_net": None, "qty": 1},
    ]
    assert booking.grid_total(rows) == Decimal("10000002.00")
    usd = booking.price_grid(Decimal("1000"), booking.Party(3, ()), BANDS, "USD")
    assert usd[0]["unit_sell"] == Decimal("333.34") and booking.grid_total(usd) == Decimal("1000.02")


def test_the_retail_total_is_the_agency_suggestion() -> None:
    quote = aa_options.AaQuote("q1", Decimal("1000"), "available", NOW, None)
    rctx = {"markups": {"trip_plan": Decimal("20")}, "rules": [], "today": NOW.date()}
    total = booking.retail_total(
        quote, kind="trip_plan", source_id="T-77", party=booking.Party(2, ()), rctx=rctx, service_date=NOW.date()
    )
    assert total == Decimal("1200.00")
    nothing = {"markups": {}, "rules": [], "today": NOW.date()}
    assert (
        booking.retail_total(
            quote, kind="trip_plan", source_id="T-77", party=booking.Party(2, ()), rctx=nothing, service_date=NOW.date()
        )
        is None
    )


# ── booking: widget, price, submit over a scripted connection ─────────────────────────────────────


AGENCY = {
    "public_booking_enabled": True,
    "base_currency": "VND",
    "timezone": "Asia/Ho_Chi_Minh",
    "display_name": "Lotus Lane Travel",
    "consent_text": None,
    "agency_privacy_notice_url": None,
}
TOUR = {
    "source_id": "T-100",
    "title": "Lantern bay overnight cruise",
    "country_code": "VN",
    "duration_days": 2,
    "served_state": "served",
}


def _fake() -> FakeBooking:
    return FakeBooking(b2b_price="10000000.00", from_price=None, departure_start="2031-05-01")


@pytest.fixture
def agency(monkeypatch: pytest.MonkeyPatch) -> ScriptedConn:
    conn = ScriptedConn().on("public_booking_enabled", AGENCY).on("FROM crm.aa_catalog_tour", TOUR)
    monkeypatch.setattr(booking, "intake_tx", fake_tx(system_ctx(conn), intake_form()))
    monkeypatch.setattr(booking, "Vault", lambda keys, ctx: SecretVault())

    async def bands(ctx: Any) -> pricing.Settings:
        return BANDS

    async def rctx(ctx: Any) -> dict[str, Any]:
        return {"markups": {"trip_plan": Decimal("20")}, "rules": [], "today": dt.date(2031, 3, 1)}

    monkeypatch.setattr(booking.pricing, "settings", bands)
    monkeypatch.setattr(booking, "_rctx", rctx)
    monkeypatch.setattr(booking.pricing, "today_in", lambda tz: dt.date(2031, 3, 1))
    return conn


@pytest.mark.asyncio
async def test_the_widget_lists_open_departures(agency: ScriptedConn) -> None:
    fake = _fake()
    client = booking_client(fake)
    w = await booking.widget(None, ProductCache(client), client, "k" * 22, tour="T-100", page_id=None)  # type: ignore[arg-type]
    assert w["tour"] == "T-100" and w["currency"] == "VND" and w["agency_name"] == "Lotus Lane Travel"
    assert w["departures"] == [{"departure_id": "d1", "start_date": "2031-05-01", "seats_left": 9}]
    with _raises(Unavailable, "booking_unavailable"):
        await booking.widget(None, None, None, "k" * 22, tour="T-100", page_id=None)  # type: ignore[arg-type]
    fake.down = True
    with _raises(Unavailable, "booking_unavailable"):
        await booking.widget(
            None, ProductCache(booking_client(fake)), booking_client(fake), "k" * 22, tour="T-100", page_id=None
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_widget_refuses_an_agency_without_book_now_or_an_unknown_tour(agency: ScriptedConn) -> None:
    client = booking_client(_fake())
    agency.on("public_booking_enabled", {**AGENCY, "public_booking_enabled": False})
    with _raises(NotFound, "booking_not_offered"):
        await booking.widget(None, ProductCache(client), client, "k" * 22, tour="T-100", page_id=None)  # type: ignore[arg-type]
    agency.on("public_booking_enabled", AGENCY).on("FROM crm.aa_catalog_tour", None)
    with _raises(NotFound, "tour_not_bookable"):
        await booking.widget(None, ProductCache(client), client, "k" * 22, tour=None, page_id="P-1")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_offer_needs_a_sold_whole_tour_with_departures() -> None:
    fake = _fake()
    client = booking_client(fake)
    with _raises(NotFound, "tour_not_bookable"):
        await booking.offer(ProductCache(client), client, TENANT, "T-other", dt.date(2031, 3, 1))
    found = await booking.offer(ProductCache(client), client, TENANT, "T-100", dt.date(2031, 3, 1))
    assert found.component_id == "tp:1" and list(found.departures) == ["d1"]
    late = await booking.offer(ProductCache(client), client, TENANT, "T-100", dt.date(2031, 5, 1))
    assert late.departures == {}  # a departure on or before today is not offered
    assert booking._booking_error(Unavailable("x")).code == "booking_unavailable"


@pytest.mark.asyncio
async def test_the_price_is_the_retail_grid(agency: ScriptedConn) -> None:
    fake = _fake()
    client = booking_client(fake)
    out = await booking.price(
        None,
        ProductCache(client),
        client,
        "k" * 22,  # type: ignore[arg-type]
        {"tour": "T-100", "departure_id": "d1", "adults": 2, "child_ages": [7]},
    )
    assert out["total"] == "12000000.00" and out["currency"] == "VND" and out["availability"] == "available"
    with _raises(Conflict, "departure_not_available"):
        await booking.price(
            None,
            ProductCache(client),
            client,
            "k" * 22,  # type: ignore[arg-type]
            {"tour": "T-100", "departure_id": "d2", "adults": 2},
        )
    with _raises(Invalid, "invalid_submission"):
        await booking.price(None, None, None, "k" * 22, [1])  # type: ignore[arg-type]
    with _raises(Unavailable, "booking_unavailable"):
        await booking.price(None, None, None, "k" * 22, {"tour": "T-100", "departure_id": "d1", "adults": 2})  # type: ignore[arg-type]
    fake.availability = "sold_out"
    with _raises(Conflict, "aa_sold_out"):
        await booking.price(
            None,
            ProductCache(client),
            client,
            "k" * 22,  # type: ignore[arg-type]
            {"tour": "T-100", "departure_id": "d1", "adults": 2},
        )


@pytest.mark.asyncio
async def test_the_quote_refusals_read_as_booking_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refused(*_: Any) -> Any:
        raise Invalid("aa_quote_refused", "no", aa_code="x")

    monkeypatch.setattr(booking.aa_options, "quote", refused)
    with _raises(Conflict, "aa_quote_refused"):
        await booking._quote(None, TENANT, {})  # type: ignore[arg-type]

    async def down(*_: Any) -> Any:
        raise Unavailable("booking_unavailable")

    monkeypatch.setattr(booking.aa_options, "quote", down)
    with _raises(Unavailable, "booking_unavailable"):
        await booking._quote(None, TENANT, {})  # type: ignore[arg-type]


async def _submit(conn: ScriptedConn, fake: FakeBooking | None, body: dict[str, Any], **kw: Any) -> dict[str, Any]:
    client = booking_client(fake) if fake is not None else None
    cache = ProductCache(client) if client is not None else None
    return await booking.submit(
        None,
        None,
        kw.pop("settings", TEST),
        cache,
        client,
        "k" * 22,
        body,  # type: ignore[arg-type]
        origin=kw.pop("origin", None),
        client_ip="198.51.100.7",
    )


@pytest.mark.asyncio
async def test_submit_checks_origin_honeypot_and_repeats(agency: ScriptedConn) -> None:
    dev = Settings(environment="dev", intake_page_origins=("https://go-dev.example.test",))
    with _raises(Forbidden, "origin_not_allowed"):
        await _submit(agency, _fake(), _press(), settings=dev, origin="https://evil.example.test")
    with _raises(Unavailable, "intake_not_configured"):
        await _submit(agency, _fake(), _press(), settings=Settings(environment="dev"))
    assert await _submit(agency, _fake(), _press(website="spam")) == {"status": "received"}
    agency.on("FROM crm.public_booking_attempt WHERE client_submission_id", {"status": "created"})
    assert await _submit(agency, _fake(), _press()) == {"status": "already_created"}
    agency.on("FROM crm.public_booking_attempt WHERE client_submission_id", {"status": "received"})
    with _raises(Conflict, "booking_in_progress"):
        await _submit(agency, _fake(), _press())
    agency.on(
        "FROM crm.public_booking_attempt WHERE client_submission_id",
        {"status": "refused", "refusal_code": "aa_sold_out"},
    )
    with _raises(Conflict, "aa_sold_out"):
        await _submit(agency, _fake(), _press())
    assert not agency.sql("INSERT INTO crm.public_booking_attempt")


@pytest.mark.asyncio
async def test_submit_caps_one_address(agency: ScriptedConn) -> None:
    agency.on("SELECT count(*) FROM crm.public_booking_attempt", booking.MAX_PER_ADDRESS_HOUR)
    with _raises(RateLimited, "booking_rate_limited"):
        await _submit(agency, _fake(), _press())
    assert not agency.sql("INSERT INTO crm.public_booking_attempt")


@pytest.mark.asyncio
async def test_submit_records_a_refusal_or_a_failure(agency: ScriptedConn) -> None:
    agency.on("SELECT count(*) FROM crm.public_booking_attempt", 0)
    fake = _fake()
    fake.availability = "sold_out"
    with _raises(Conflict, "aa_sold_out"):
        await _submit(agency, fake, _press())
    assert [a[1:] for a in agency.sql("UPDATE crm.public_booking_attempt SET status = $2")] == [
        ("refused", "aa_sold_out")
    ]
    with _raises(Unavailable, "booking_unavailable"):
        await _submit(agency, None, _press())
    assert agency.sql("UPDATE crm.public_booking_attempt SET status = $2")[-1][1:] == ("failed", "booking_unavailable")
    with _raises(Conflict, "departure_not_available"):
        await _submit(agency, _fake(), _press(departure_id="d9"))


def _rrp_only(monkeypatch: pytest.MonkeyPatch) -> FakeBooking:
    """An agency without a markup (the retail is AA's RRP of 4,000,000 a head) on a tour whose B2B price is
    10,000,000 whatever the party: one adult would be sold below cost, three adults are not."""

    async def rctx(ctx: Any) -> dict[str, Any]:
        return {"markups": {}, "rules": [], "today": dt.date(2031, 3, 1)}

    monkeypatch.setattr(booking, "_rctx", rctx)
    return FakeBooking(b2b_price="10000000.00", from_price="4000000.00", departure_start="2031-05-01")


def test_a_retail_below_the_b2b_price_is_on_request() -> None:
    quote = aa_options.AaQuote("q1", Decimal("10000000"), "available", NOW, Decimal("4000000"))
    with _raises(Conflict, booking.PRICE_ON_REQUEST):
        booking.check_not_below_cost(Decimal("9999999.99"), quote)
    booking.check_not_below_cost(Decimal("10000000.00"), quote)  # at cost: the gate's margin is not negative
    booking.check_not_below_cost(Decimal("1.00"), aa_options.AaQuote("q2", None, "available", NOW, None))


@pytest.mark.asyncio
async def test_one_adult_below_cost_is_priced_on_request_not_an_error(
    agency: ScriptedConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S277 live check: a 1-adult price check stopped at the owner's discount gate (409 discount_needs_approval,
    below_cost, "Something went wrong" on the page). The public path refuses with its own code instead."""
    fake = _rrp_only(monkeypatch)
    client = booking_client(fake)
    one = {"tour": "T-100", "departure_id": "d1", "adults": 1, "child_ages": []}
    with _raises(Conflict, booking.PRICE_ON_REQUEST):
        await booking.price(None, ProductCache(client), client, "k" * 22, one)  # type: ignore[arg-type]
    three = await booking.price(None, ProductCache(client), client, "k" * 22, {**one, "adults": 3})  # type: ignore[arg-type]
    assert three["total"] == "12000000.00" and three["lines"] == [{"pax_type": "adult", "qty": 3, "unit": "4000000.00"}]


@pytest.mark.asyncio
async def test_a_one_adult_booking_below_cost_is_refused_without_using_an_attempt(
    agency: ScriptedConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _creating(agency, monkeypatch)
    fake = _rrp_only(monkeypatch)
    with _raises(Conflict, booking.PRICE_ON_REQUEST):
        await _submit(agency, fake, _press(tour="T-100", adults=1, child_ages=[]))
    assert seen == ["start"]  # never published (the owner gate is never reached), nothing sent
    assert not agency.sql("INSERT INTO crm.trip (") and not agency.sql("INSERT INTO crm.traveler (")
    assert agency.sql("UPDATE crm.public_booking_attempt SET status = $2")[-1][1:] == ("refused", "price_on_request")
    # ... and such a refusal is not one of the address's attempts an hour.
    assert booking.PRICE_ON_REQUEST in booking.UNCOUNTED_REFUSALS
    assert "refusal_code = ANY($2::text[])" in " ".join(booking.ADDRESS_COUNT_SQL.split())
    out = await _submit(agency, fake, _press(tour="T-100", adults=3, child_ages=[]))
    assert out["status"] == "created" and out["total"] == "12000000.00"


def _creating(agency: ScriptedConn, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    agency.on("SELECT count(*) FROM crm.public_booking_attempt", 0)
    agency.on(
        "FROM crm.public_booking_attempt WHERE client_submission_id",
        lambda *a: None if not seen else {"status": "received", "attempt_id": uuid.uuid4()},
    )
    agency.on("SELECT edit_seq FROM crm.proposal", 3).on("INSERT INTO crm.trip_stage_history", uuid.uuid4())

    async def publish(ctx: Any, vault: Any, proposal_id: Any, seq: int, **_: Any) -> dict:
        seen.append(f"publish:{seq}")
        return {}

    async def send(ctx: Any, vault: Any, proposal_id: Any) -> dict:
        seen.append("send")
        return {"link_path": "/c/p/tok"}

    async def noop(*_: Any, **__: Any) -> Any:
        return None

    async def started(ctx: Any, form: Any, vault: Any, answers: Any, *, client_ip: str) -> Any:
        seen.append("start")
        return booking.Started(uuid.uuid4(), TENANT, TOUR, "VND", dt.date(2031, 3, 1))

    monkeypatch.setattr(booking, "_start", started)
    monkeypatch.setattr(booking.builder, "publish", publish)
    monkeypatch.setattr(booking.proposal_service, "send", send)
    monkeypatch.setattr(booking.triggers, "on_stage_enter", noop)
    monkeypatch.setattr(booking.esign_terms, "current", noop)
    monkeypatch.setattr(booking.builder, "_template", noop)
    monkeypatch.setattr(booking.builder, "_write_plan", noop)
    monkeypatch.setattr(booking.pricing, "replace", noop)
    return seen


@pytest.mark.asyncio
async def test_submit_creates_the_trip_and_sends_the_proposal(
    agency: ScriptedConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _creating(agency, monkeypatch)
    out = await _submit(agency, _fake(), _press(message="A quiet cabin", consent_marketing=True))
    assert out == {"status": "created", "link_path": "/c/p/tok", "currency": "VND", "total": "12000000.00"}
    assert seen == ["start", "publish:3", "send"]
    trip = agency.sql("INSERT INTO crm.trip (")[0]
    assert trip[2:5] == (dt.date(2031, 5, 1), dt.date(2031, 5, 2), 1) and trip[5] == ["vietnam"]
    assert trip[6:9] == (2, [7, 1], "family")
    assert agency.sql("INSERT INTO crm.traveler (")  # a new client (no e-mail or phone match)
    assert [n[3] for n in agency.sql("INSERT INTO crm.note")] == ["inquiry_message"]  # no duplicate note
    option = agency.sql("INSERT INTO crm.option (")[0]
    assert option[5] == "aa" and option[9] == Decimal("12000000.00") and option[16:19] == ("tp:1", "trip_plan", "d1")
    assert agency.sql("SET status = 'created'")


@pytest.mark.asyncio
async def test_submit_never_binds_the_visitor_to_a_known_client_and_rolls_a_failure_back(
    agency: ScriptedConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding: the visitor's e-mail is not verified and the answer hands them a live client link, so an e-mail
    (or phone) an existing client has makes a new client, never reuses or unarchives the existing one; the trip gets
    an internal note for the agent to merge, and the answer is the same as for an unknown e-mail."""
    _creating(agency, monkeypatch)
    known = {"traveler_id": uuid.uuid4(), "profile_token_enc": None}
    agency.on("WHERE email_bidx = $1", known).on("WHERE phone_bidx = $1", known).on("AS known", True)
    agency.on("SET archived_at = NULL", uuid.uuid4())
    out = await _submit(agency, _fake(), _press(message=None))
    assert out == {"status": "created", "link_path": "/c/p/tok", "currency": "VND", "total": "12000000.00"}
    created = agency.sql("INSERT INTO crm.traveler (")
    assert len(created) == 1 and created[0][0] != known["traveler_id"]
    lead = agency.sql("INSERT INTO crm.trip_traveler")[0]
    assert known["traveler_id"] not in lead and created[0][0] in lead
    assert not agency.sql("SET archived_at = NULL") and not agency.sql("WHERE email_bidx = $1 AND deleted_at")
    assert [n[3] for n in agency.sql("INSERT INTO crm.note")] == ["internal"]
    assert agency.sql("AS known")[0][1] is not None  # the phone is matched too

    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(booking.proposal_service, "send", broken)
    with _raises(Conflict, "booking_failed"):
        await _submit(agency, _fake(), _press())
    assert agency.sql("UPDATE crm.public_booking_attempt SET status = $2")[-1][1:] == ("failed", "booking_failed")

    async def unpublishable(*_: Any, **__: Any) -> Any:
        raise Invalid("not_publishable", "x")

    monkeypatch.setattr(booking.builder, "publish", unpublishable)
    with _raises(Invalid, "not_publishable"):
        await _submit(agency, _fake(), _press())

    async def no_price(*_: Any, **__: Any) -> Any:
        return {"markups": {}, "rules": [], "today": dt.date(2031, 3, 1)}

    monkeypatch.setattr(booking, "_rctx", no_price)
    with _raises(Conflict, "no_retail_price"):
        await _submit(agency, _fake(), _press())
    assert agency.sql("UPDATE crm.public_booking_attempt SET status = $2")[-1][1:] == ("refused", "no_retail_price")


@pytest.mark.asyncio
async def test_create_answers_already_created_for_a_finished_attempt(agency: ScriptedConn) -> None:
    agency.on("FROM crm.public_booking_attempt WHERE client_submission_id", {"status": "created"})
    started = booking.Started(uuid.uuid4(), TENANT, TOUR, "VND", dt.date(2031, 3, 1))
    out = await booking._create(
        system_ctx(agency),
        SecretVault(),
        started,
        booking.read_booking(_press()),  # type: ignore[arg-type]
        plan={},
        quote=None,
        departure=None,
        owner=None,
        form_id=uuid.uuid4(),
    )  # type: ignore[arg-type]
    assert out == {"status": "already_created"}


@pytest.mark.asyncio
async def test_start_writes_one_attempt(agency: ScriptedConn) -> None:
    agency.on("SELECT count(*) FROM crm.public_booking_attempt", 0)
    out = await booking._start(
        system_ctx(agency),
        intake_form(),
        SecretVault(),
        booking.read_booking(_press()),  # type: ignore[arg-type]
        client_ip="198.51.100.7",
    )
    assert isinstance(out, booking.Started) and out.currency == "VND"
    assert agency.sql("DELETE FROM crm.public_booking_attempt")[0] == (booking.RETENTION_DAYS,)
    row = agency.sql("INSERT INTO crm.public_booking_attempt")[0]
    assert row[2:4] == ("T-100", "d1") and row[7] == b"h" * 32


@pytest.mark.asyncio
async def test_the_online_booking_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = (
        ScriptedConn()
        .on("SELECT public_booking_enabled", True)
        .on("FROM crm.intake_form", [{"label": "Website", "public_key": "k" * 22}])
    )
    out = await booking_settings.read(tenant_ctx(conn), TEST)
    assert out["enabled"] and out["forms"] == [{"label": "Website", "public_key": "k" * 22}]
    assert out["page_base"].endswith("/trip/")
    await booking_settings.update(tenant_ctx(conn), TEST, False)
    assert conn.sql("INSERT INTO crm.tenant_settings") == [(False,)]
    with _raises(Invalid, "invalid_enabled"):
        await booking_settings.update(tenant_ctx(conn), TEST, "yes")
    assert (await booking_settings.read(tenant_ctx(conn), SimpleNamespace(messaging_hooks_origin=lambda: "")))[  # type: ignore[arg-type]
        "page_base"
    ] is None


# ── the gateway ───────────────────────────────────────────────────────────────────────────────────


def test_connect_values_are_checked() -> None:
    ok = gateway.check_connect(
        tmn_code=" TESTSTUB1 ", hash_secret=SECRET, mode="sandbox", display_name=None, environment="prod"
    )
    assert ok == {"tmn_code": "TESTSTUB1", "hash_secret": SECRET, "mode": "sandbox", "display_name": "Online payment"}
    for kw, code in (
        ({"mode": "other"}, "invalid_mode"),
        ({"mode": "stub"}, "mode_not_allowed"),
        ({"tmn_code": "a b"}, "invalid_tmn_code"),
        ({"hash_secret": "short"}, "invalid_hash_secret"),
        ({"hash_secret": "has a space in it ok"}, "invalid_hash_secret"),
        ({"display_name": "x" * 81}, "invalid_display_name"),
    ):
        args = {
            "tmn_code": "TESTSTUB1",
            "hash_secret": SECRET,
            "mode": "live",
            "display_name": None,
            "environment": "prod",
            **kw,
        }
        with _raises(Invalid, code):
            gateway.check_connect(**args)
    assert gateway.ipn_url(TEST).endswith(gateway.IPN_PATH)
    assert gateway.ipn_url(SimpleNamespace(messaging_hooks_origin=lambda: "")) is None  # type: ignore[arg-type]


LAST_ACCOUNT = "WHERE provider = 'vn_gateway' AND deleted_at IS NULL ORDER BY"


@pytest.mark.asyncio
async def test_connect_seals_the_secret_and_answers_without_it() -> None:
    conn = ScriptedConn().on("FROM crm.payment_provider_account WHERE deleted_at IS NULL", [])
    vault = SecretVault()
    out = await gateway.connect(
        tenant_ctx(conn), vault, TEST, {"tmn_code": "TESTSTUB1", "hash_secret": SECRET, "mode": "stub"}
    )
    assert out["vn_gateway"]["mode"] == "stub" and SECRET not in json.dumps(out)
    assert json.loads(vault.sealed[0]) == {"hash_secret": SECRET, "mode": "stub"}
    assert conn.sql("INSERT INTO crm.integration_credential") and conn.sql("INSERT INTO crm.payment_provider_account")
    conn.on(
        "WHERE provider = 'vn_gateway' AND member_id IS NULL",
        {"integration_credential_id": CREDENTIAL, "secret_enc": b"x", "account_ref": "TESTSTUB1"},
    )
    conn.on(LAST_ACCOUNT, {"account_id": ACCOUNT, "status": "active"})
    conn.on("WHERE provider = 'vn_gateway' AND status = 'active'", ACCOUNT)
    await gateway.connect(
        tenant_ctx(conn), vault, TEST, {"tmn_code": "TESTSTUB2", "hash_secret": SECRET, "mode": "live"}
    )
    assert conn.sql("UPDATE crm.integration_credential")[0][1] == "TESTSTUB2"
    assert conn.sql("UPDATE crm.payment_provider_account SET display_name")[0] == (
        ACCOUNT,
        "Online payment",
        "TESTSTUB2",
    )
    assert len(conn.sql("INSERT INTO crm.payment_provider_account")) == 1  # the active account is updated in place
    status = await gateway.status(tenant_ctx(conn), vault, TEST)
    assert status == {"connected": True, "mode": "stub", "ipn_url": gateway.ipn_url(TEST)}
    assert (await gateway.status(tenant_ctx(ScriptedConn()), vault, TEST))["connected"] is False


@pytest.mark.asyncio
async def test_after_a_disconnect_the_gateway_reads_not_connected_and_a_reconnect_reuses_the_account() -> None:
    """S277 live check: the disconnect left the credential (webhooks of started sessions still verify), so the status
    still said "Connected", and every new connect added another account row."""
    vault = SecretVault()
    conn = ScriptedConn().on(
        "WHERE provider = 'vn_gateway' AND member_id IS NULL",
        {"integration_credential_id": CREDENTIAL, "secret_enc": b"x", "account_ref": "TESTSTUB1"},
    )
    # The credential is there, no account is active: not connected, no mode (and the secret is never opened).
    status = await gateway.status(tenant_ctx(conn), vault, TEST)
    assert status == {"connected": False, "mode": None, "ipn_url": gateway.ipn_url(TEST)}
    conn.on(LAST_ACCOUNT, {"account_id": ACCOUNT, "status": "disconnected"})
    ctx = tenant_ctx(conn)
    await gateway.connect(ctx, vault, TEST, {"tmn_code": "TESTSTUB3", "hash_secret": SECRET, "mode": "stub"})
    assert not conn.sql("INSERT INTO crm.payment_provider_account")
    assert conn.sql("SET status = 'active', disconnected_at = NULL") == [(ACCOUNT, "Online payment", "TESTSTUB3")]
    assert ctx.audit_events[-1].fields == ("status", "external_account_id", "charges_enabled")


def _invoice_row(**over: Any) -> dict[str, Any]:
    row = {"invoice_id": INVOICE, "trip_id": TRIP, "currency": "VND", "number": "INV-000003", "kind": "deposit"}
    row.update(over)
    return row


def _gateway_conn(**over: Any) -> ScriptedConn:
    conn = ScriptedConn().on(
        "WHERE provider = 'vn_gateway' AND member_id IS NULL",
        {"integration_credential_id": CREDENTIAL, "secret_enc": b"x", "account_ref": "TESTSTUB1"},
    )
    conn.on("SELECT i.total_amount - coalesce", over.get("open", Decimal("3600000.00")))
    return conn


@pytest.mark.asyncio
async def test_a_session_charges_the_open_amount_in_dong() -> None:
    conn = _gateway_conn()
    account = {"account_id": ACCOUNT, "provider": "vn_gateway"}
    out = await gateway.start_session(
        system_ctx(conn),
        SecretVault(),
        TEST,
        _invoice_row(),
        account,  # type: ignore[arg-type]
        client_ip="198.51.100.7",
        locale="vi",
        now=NOW,
    )
    fields = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(out["redirect_url"]).query))
    assert gateway.STUB_PATH in out["redirect_url"] and fields["vnp_Amount"] == "360000000"
    assert fields["vnp_Locale"] == "vn" and fields["vnp_ReturnUrl"].endswith(gateway.RETURN_PATH)
    session = conn.sql("INSERT INTO crm.payment_session")[0]
    assert session[6:8] == (Decimal("3600000"), "VND") and session[4] == "vn_gateway"
    live = SecretVault({"hash_secret": SECRET, "mode": "live"})
    out = await gateway.start_session(
        system_ctx(conn),
        live,
        PROD,
        _invoice_row(),
        account,  # type: ignore[arg-type]
        client_ip="",
        locale="en",
        now=NOW,
    )
    assert out["redirect_url"].startswith(vnpay.PAY_URLS["live"])


@pytest.mark.asyncio
async def test_a_session_is_refused_when_the_gateway_cannot_take_it() -> None:
    account = {"account_id": ACCOUNT, "provider": "vn_gateway"}
    with _raises(Conflict, "currency_not_supported"):
        await gateway.start_session(
            system_ctx(_gateway_conn()),
            SecretVault(),
            TEST,
            _invoice_row(currency="USD"),  # type: ignore[arg-type]
            account,
            client_ip="",
            locale="en",
            now=NOW,
        )
    with _raises(Conflict, "provider_not_available"):
        await gateway.start_session(
            system_ctx(_gateway_conn()),
            SecretVault(),
            TEST,
            _invoice_row(),  # type: ignore[arg-type]
            {"account_id": ACCOUNT, "provider": "stripe"},
            client_ip="",
            locale="en",
            now=NOW,
        )
    with _raises(Conflict, "provider_not_available"):
        await gateway.start_session(
            system_ctx(ScriptedConn()),
            SecretVault(),
            TEST,
            _invoice_row(),
            account,  # type: ignore[arg-type]
            client_ip="",
            locale="en",
            now=NOW,
        )
    with _raises(Conflict, "provider_not_available"):  # a stub account on prod never sends anyone to the simulator
        await gateway.start_session(
            system_ctx(_gateway_conn()),
            SecretVault(),
            PROD,
            _invoice_row(),
            account,  # type: ignore[arg-type]
            client_ip="",
            locale="en",
            now=NOW,
        )
    with _raises(Conflict, "provider_not_available"):
        await gateway.start_session(
            system_ctx(_gateway_conn()),
            SecretVault(),  # type: ignore[arg-type]
            SimpleNamespace(environment="test", messaging_hooks_origin=lambda: ""),
            _invoice_row(),
            account,
            client_ip="",
            locale="en",
            now=NOW,
        )
    with _raises(Conflict, "invoice_not_payable"):
        await gateway.start_session(
            system_ctx(_gateway_conn(open=Decimal(0))),
            SecretVault(),
            TEST,
            _invoice_row(),  # type: ignore[arg-type]
            account,
            client_ip="",
            locale="en",
            now=NOW,
        )


def _ipn(**over: Any) -> dict[str, str]:
    kw: dict[str, Any] = dict(
        tmn_code="TESTSTUB1",
        txn_ref=REF,
        amount=Decimal("3600000"),
        succeeded=True,
        transaction_no="14012345",
        when=NOW,
        secret=SECRET,
    )
    kw.update(over)
    return vnpay.notice_fields(**kw)


SESSION_ROW = {
    "session_id": SESSION,
    "invoice_id": INVOICE,
    "trip_id": TRIP,
    "amount": Decimal("3600000"),
    "currency": "VND",
    "status": "open",
    "provider": "vn_gateway",
    "txn_ref": REF,
    "expires_at": NOW,
    "number": "INV-000003",
    "kind": "deposit",
}


@pytest.fixture
def hook(monkeypatch: pytest.MonkeyPatch) -> ScriptedConn:
    conn = _gateway_conn().on("FROM crm.payment_session s JOIN crm.invoice", SESSION_ROW)

    @asynccontextmanager
    async def tx(pool: Any, ref: str) -> Any:
        if ref != REF:
            raise NotFound("session_not_found")
        yield system_ctx(conn), PaymentSessionRef(SESSION, TENANT)

    monkeypatch.setattr(gateway, "payment_session_tx", tx)
    monkeypatch.setattr(gateway, "Vault", lambda keys, ctx: SecretVault())
    return conn


@pytest.mark.asyncio
async def test_the_ipn_checks_the_signature_merchant_amount_and_state(hook: ScriptedConn) -> None:
    async def answer(fields: dict[str, str]) -> str:
        return (await gateway.handle_ipn(None, None, fields))["RspCode"]  # type: ignore[arg-type]

    assert await answer({}) == vnpay.NOT_FOUND
    assert await answer({"vnp_TxnRef": REF}) == vnpay.OTHER
    assert await answer(_ipn(txn_ref="f" * 24)) == vnpay.NOT_FOUND
    assert await answer(_ipn(secret="a-forged-secret-of-someone")) == vnpay.BAD_SIGNATURE
    assert await answer(_ipn(tmn_code="OTHERMERCH")) == vnpay.BAD_SIGNATURE
    assert await answer(_ipn(amount=Decimal("1"))) == vnpay.BAD_AMOUNT
    hook.on("FROM crm.payment_session s JOIN crm.invoice", {**SESSION_ROW, "status": "paid"})
    assert await answer(_ipn()) == vnpay.ALREADY
    hook.on("FROM crm.payment_session s JOIN crm.invoice", None)
    assert await answer(_ipn()) == vnpay.NOT_FOUND
    hook.on("FROM crm.payment_session s JOIN crm.invoice", SESSION_ROW)
    assert await answer(_ipn(succeeded=False)) == vnpay.CONFIRMED
    assert hook.sql("UPDATE crm.payment_session SET status = $2")[-1][1:3] == ("failed", "24")
    assert not hook.sql("INSERT INTO crm.payment")


@pytest.mark.asyncio
async def test_the_ipn_records_the_payment_or_holds_it_for_review(
    hook: ScriptedConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple[Any, ...]] = []

    async def paid(
        ctx: Any, vault: Any, invoice_id: Any, *, amount: Decimal, provider_name: str, reference: str
    ) -> dict:
        recorded.append((invoice_id, amount, provider_name, reference))
        return {"payment_id": str(uuid.uuid4())}

    monkeypatch.setattr(gateway.payments, "record_gateway_payment", paid)
    assert (await gateway.handle_ipn(None, None, _ipn()))["RspCode"] == vnpay.CONFIRMED  # type: ignore[arg-type]
    assert recorded == [(INVOICE, Decimal("3600000"), "vn_gateway", "14012345")]
    assert hook.sql("UPDATE crm.payment_session SET status = $2")[-1][1:4] == ("paid", "00", "14012345")

    async def refused(*_: Any, **__: Any) -> dict:
        raise Conflict("invoice_not_payable")

    async def unsettled(ctx: Any, invoice_id: Any, **kw: Any) -> uuid.UUID:
        recorded.append(("review", kw["reason"]))
        return uuid.uuid4()

    monkeypatch.setattr(gateway.payments, "record_gateway_payment", refused)
    monkeypatch.setattr(gateway.payments, "record_unsettled_payment", unsettled)
    assert (await gateway.handle_ipn(None, None, _ipn()))["RspCode"] == vnpay.CONFIRMED  # type: ignore[arg-type]
    assert recorded[-1] == ("review", "invoice_not_payable")
    assert hook.sql("UPDATE crm.payment_session SET status = $2")[-1][1] == "review"
    # Late money for a failed session goes to review as well.
    hook.on("FROM crm.payment_session s JOIN crm.invoice", {**SESSION_ROW, "status": "failed"})
    assert (await gateway.handle_ipn(None, None, _ipn()))["RspCode"] == vnpay.CONFIRMED  # type: ignore[arg-type]
    assert recorded[-1] == ("review", "session_closed")

    import asyncpg

    async def twice(*_: Any, **__: Any) -> dict:
        raise asyncpg.UniqueViolationError("payment_provider_ref_uq")

    hook.on("FROM crm.payment_session s JOIN crm.invoice", SESSION_ROW)
    monkeypatch.setattr(gateway.payments, "record_gateway_payment", twice)
    assert (await gateway.handle_ipn(None, None, _ipn()))["RspCode"] == vnpay.ALREADY  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_stub_gateway_is_dev_only_and_signs_with_the_agency_secret(
    hook: ScriptedConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = vnpay.pay_url("https://client.example.test" + gateway.STUB_PATH, _request())
    fields = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    page = await gateway.stub_page(None, None, TEST, fields)  # type: ignore[arg-type]
    assert "3,600,000 VND" in page and 'value="paid"' in page and "Content-Security-Policy" in page
    with _raises(NotFound, "session_not_found"):
        await gateway.stub_page(None, None, TEST, {**fields, "vnp_Amount": "1"})  # type: ignore[arg-type]
    with _raises(NotFound, "session_not_found"):
        await gateway.stub_page(None, None, PROD, fields)  # type: ignore[arg-type]
    hook.on("FROM crm.payment_session s JOIN crm.invoice", {**SESSION_ROW, "status": "paid"})
    assert "This payment is closed" in await gateway.stub_page(None, None, TEST, fields)  # type: ignore[arg-type]
    hook.on("FROM crm.payment_session s JOIN crm.invoice", None)
    with _raises(NotFound, "session_not_found"):
        await gateway.stub_page(None, None, TEST, fields)  # type: ignore[arg-type]

    sent: list[dict[str, str]] = []

    async def ipn(pool: Any, keys: Any, f: dict[str, str]) -> dict[str, str]:
        sent.append(f)
        return vnpay.ipn_answer(vnpay.CONFIRMED)

    hook.on("FROM crm.payment_session s JOIN crm.invoice", SESSION_ROW)
    monkeypatch.setattr(gateway, "handle_ipn", ipn)
    back = await gateway.stub_pay(None, None, TEST, REF, "paid")  # type: ignore[arg-type]
    assert back.endswith(gateway.RETURN_PATH) and vnpay.verify(sent[0], SECRET) and sent[0]["vnp_ResponseCode"] == "00"
    await gateway.stub_pay(None, None, TEST, REF, "cancel")  # type: ignore[arg-type]
    assert sent[1]["vnp_ResponseCode"] == "24"
    monkeypatch.setattr(gateway, "Vault", lambda keys, ctx: SecretVault({"hash_secret": SECRET, "mode": "live"}))
    with _raises(NotFound, "session_not_found"):  # never simulate a real merchant
        await gateway.stub_pay(None, None, TEST, REF, "paid")  # type: ignore[arg-type]


# ── claims: the card window and the pay-online flag ──────────────────────────────────────────────


def _client_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "invoice_id": INVOICE,
        "trip_id": TRIP,
        "kind": "deposit",
        "status": "open",
        "number": "INV-000003",
        "currency": "VND",
        "total_amount": Decimal("3600000"),
        "due_on": dt.date(2031, 3, 2),
        "proposal_id": uuid.UUID(int=7),
        "schedule_id": uuid.UUID(int=8),
        "claimed_at": None,
        "hold_extended_at": None,
        "hold_extend_code": None,
        "rehold_requested_at": None,
        "rehold_code": None,
        "proposal_kind": "text",
        "accepted_hold_id": None,
        "hold_id": None,
        "hold_expires_at": None,
        "paid_amount": Decimal(0),
        "seq": 1,
    }
    row.update(over)
    return row


def test_pay_online_needs_an_open_window_and_the_gateway_currency() -> None:
    assert claims._client_invoice(_client_row(), NOW, None, online="VND")["pay_online"] is True
    assert claims._client_invoice(_client_row(), NOW, None, online=None)["pay_online"] is False
    assert claims._client_invoice(_client_row(currency="USD"), NOW, None, online="VND")["pay_online"] is False
    assert claims._client_invoice(_client_row(status="paid"), NOW, None, online="VND")["pay_online"] is False


@pytest.mark.asyncio
async def test_the_card_window_and_the_online_currency() -> None:
    conn = ScriptedConn().on("FOR UPDATE OF i", _client_row()).on("charges_enabled AND provider <> 'manual'", None)
    proposal = uuid.UUID(int=7)
    with _raises(Conflict, "card_not_offered"):
        await claims.card_window(system_ctx(conn), proposal, INVOICE, NOW)
    assert await claims.online_currency(system_ctx(conn)) is None
    conn.on("charges_enabled AND provider <> 'manual'", {"account_id": ACCOUNT, "provider": "vn_gateway"})
    row, account = await claims.card_window(system_ctx(conn), proposal, INVOICE, NOW)
    assert row["invoice_id"] == INVOICE and account["provider"] == "vn_gateway"
    assert await claims.online_currency(system_ctx(conn)) == "VND"
    conn.on("charges_enabled AND provider <> 'manual'", {"account_id": ACCOUNT, "provider": "stripe"})
    with _raises(Conflict, "provider_not_available"):
        await claims.card_window(system_ctx(conn), proposal, INVOICE, NOW)
    assert await claims.online_currency(system_ctx(conn)) is None
    conn.on("FOR UPDATE OF i", _client_row(status="paid"))
    with _raises(Conflict, "invoice_not_payable"):
        await claims.card_window(system_ctx(conn), proposal, INVOICE, NOW)
    conn.on(
        "FOR UPDATE OF i",
        _client_row(proposal_kind="builder", accepted_hold_id="h1", hold_expires_at=NOW - dt.timedelta(minutes=1)),
    )
    with _raises(Gone, "pay_link_expired"):
        await claims.card_window(system_ctx(conn), proposal, INVOICE, NOW)


# ── payments: the gateway paths and the provider ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_vn_gateway_provider_records_a_refund_reference() -> None:
    impl = providers.provider("vn_gateway")
    assert impl.takes_cards and impl.needs_external_account and impl.currency == "VND"
    assert (await impl.refund(amount=Decimal(1), currency="VND", original_ref="1", reference="R9")).provider_ref == "R9"
    assert {p["provider"]: p["available"] for p in providers.available()}["vn_gateway"] is True
    with _raises(Conflict, "use_gateway_settings"):
        await payments.connect(tenant_ctx(), "vn_gateway")
    with _raises(Conflict, "provider_not_available"):
        await payments.connect(tenant_ctx(), "stripe")


@pytest.mark.asyncio
async def test_unsettled_money_is_recorded_with_a_review_task() -> None:
    conn = ScriptedConn().on(
        "FROM crm.invoice WHERE invoice_id = $1", {"invoice_id": INVOICE, "trip_id": TRIP, "currency": "VND"}
    )
    conn.on("AT TIME ZONE", NOW.date())
    payment_id = await payments.record_unsettled_payment(
        system_ctx(conn),
        INVOICE,
        amount=Decimal("3600000"),
        provider_name="vn_gateway",
        reference="14012345",
        reason="invoice_not_payable",
    )
    insert = conn.sql("INSERT INTO crm.payment")[0]
    assert insert[0] == payment_id and insert[2] is None and insert[6] == "online" and insert[8] == "vn_gateway"
    assert conn.sql("INSERT INTO crm.task")[0] == (TRIP, payments.REVIEW_TASK)
    with _raises(NotFound, "invoice_not_found"):
        await payments.record_unsettled_payment(
            system_ctx(ScriptedConn()),
            INVOICE,
            amount=Decimal(1),
            provider_name="vn_gateway",
            reference="1",
            reason="x",
        )
    assert payments.ctx_actor(system_ctx()) == "job:crm_intake_process"
    assert payments.ctx_actor(tenant_ctx()).startswith("member:")
