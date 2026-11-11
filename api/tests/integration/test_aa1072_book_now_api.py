"""AA-1072 end to end on PostgreSQL with the RDS role shape, against the in-memory Booking of ``tests/aa_fakes.py``
(synthetic agency, invented names only):

- "Book now" (item crm-02): off by default (404 ``booking_not_offered``); the widget lists the tour's open fixed
  departures for the agency whose form key the page carries; the live price is the agency's retail on the Booking quote
  (owner markup), whole dong per head; a booking makes the traveller, a trip at Proposal (``web_booking``), a sent
  one-option day-by-day proposal and its client link; a repeated press is the same attempt; a honeypot hit stores
  nothing; a booking with an existing client's e-mail makes a new client (never binds the visitor to the existing
  one) with an internal note to merge; a sold-out departure is refused and recorded.
- The client holds and accepts on the existing page; the deposit invoice of a web booking is issued at the accept.
- Online payment (item crm-03): without a gateway the pay box offers bank details only and the card button is 409
  ``card_not_offered``; with the agency's own VNPay account (stub mode) the button answers a signed gateway URL for the
  invoice's open amount; the stub's Pay sends a signed IPN that records the payment once (method online, the gateway's
  number), pays the invoice and settles the deposit row (trip Booked). The IPN refuses a bad signature (97), an unknown
  reference (01), a wrong amount (04), a repeat (02). Money that cannot settle (the invoice paid meanwhile) is still
  recorded, the session goes to review and the trip owner gets an urgent ``payment_review`` task.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.booking.products import ProductCache
from app.errors import Conflict, NotFound
from app.intake import booking, booking_settings
from app.intake import forms as intake_forms
from app.payments import gateway, vnpay
from app.payments import service as payments
from app.proposals import public as page
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.aa_fakes import FakeBooking, booking_client
from tests.integration.conftest import OWNER
from tests.integration.test_client_proposal_api import PLAIN, _raw
from tests.integration.test_crm005_intake import _agency, _keys

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TOUR = "T-1072"


AGENCIES: list[uuid.UUID] = []


@pytest_asyncio.fixture(autouse=True)
async def _leave_no_trace(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> AsyncIterator[None]:
    """After each test: the agencies made here are locked (an accepted hold set of an active agency would be picked up
    by another file's Booking sync run, which asks Booking for every active agency), and the tour's row of the shared
    catalog cache (a platform table) leaves (the catalog sync tests count every cached tour)."""
    yield
    while AGENCIES:
        tenant_id = AGENCIES.pop()
        async with app_pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            await conn.execute("UPDATE crm.tenant SET status = 'locked'")
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute("DELETE FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = $1", TOUR)
    finally:
        await admin.execute("RESET ROLE")


BANK = "Account 0000 7777 at the agency's bank"


async def _setup(pool: asyncpg.Pool, *, enabled: bool = True) -> dict[str, Any]:
    tenant_id, people = await _agency(pool, "owner", "agent")
    AGENCIES.append(tenant_id)
    who = {"owner": people[0][0], "agent": people[1][0]}
    keys = _keys()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state) "
            "VALUES ($1, 'Lantern bay overnight cruise', 'VN', 2, 'served') "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served'",
            TOUR,
        )
    async with tenant_tx(pool, who["owner"], tenant_id) as ctx:
        await ctx.conn.execute(
            "INSERT INTO crm.tenant_settings (tenant_id, base_currency, retail_markup_pct) "
            "VALUES ($1, 'VND', '{\"trip_plan\": 20}'::jsonb) ON CONFLICT (tenant_id) DO UPDATE "
            "SET base_currency = 'VND', retail_markup_pct = '{\"trip_plan\": 20}'::jsonb",
            tenant_id,
        )
        await booking_settings.update(ctx, PLAIN, enabled)
        await payments.save_manual(ctx, display_name="Bank transfer", instructions=BANK)
        form = await intake_forms.create_form(ctx, label="Website", default_owner_member_id=people[1][1])
        honeypot = await ctx.conn.fetchval(
            "SELECT honeypot_field FROM crm.intake_form WHERE public_key = $1", form.public_key
        )
    fake = FakeBooking(
        b2b_price="10000000.00",
        from_price=None,
        product_source_id=TOUR,
        departure_start=(dt.date.today() + dt.timedelta(days=60)).isoformat(),
    )
    return {
        "tenant": tenant_id,
        "who": who,
        "owner_member": people[0][1],
        "keys": keys,
        "key": form.public_key,
        "honeypot": honeypot,
        "fake": fake,
        "cache": ProductCache(booking_client(fake)),
    }


def _press(s: dict[str, Any], **over: Any) -> dict[str, Any]:
    body = {
        "tour": TOUR,
        "departure_id": "d1",
        "adults": 2,
        "child_ages": [7],
        "name": "Mira Solenne",
        "email": "mira.solenne@example.test",
        "phone": "+84 912 000 111",
        "message": "We would like a quiet cabin.",
        "consent_contact": True,
        "consent_marketing": False,
        "client_submission_id": str(uuid.uuid4()),
    }
    body.update(over)
    return body


async def _submit(pool: asyncpg.Pool, s: dict[str, Any], body: dict[str, Any], ip: str = "198.51.100.7") -> dict:
    return await booking.submit(
        pool, s["keys"], PLAIN, s["cache"], booking_client(s["fake"]), s["key"], body, origin=None, client_ip=ip
    )


def _token(path: str) -> str:
    return path.rsplit("/", 1)[1]


async def _book_and_accept(pool: asyncpg.Pool, s: dict[str, Any]) -> dict[str, Any]:
    out = await _submit(pool, s, _press(s))
    assert out["status"] == "created", out
    s["token"] = _token(out["link_path"])
    status, held = await page.hold(pool, s["keys"], PLAIN, booking_client(s["fake"]), s["token"], origin=None)
    assert status == 200 and held["outcome"] == "held", held
    p = held["page"]
    body = {
        "full_name": "Mira Solenne",
        "accept_terms": True,
        "price_hash": p["price_hash"],
        "terms_doc_id": None,
        "selection_hash": p["selection_hash"],
        "rooms_confirmed": True,
    }
    assert (await page.accept(pool, s["keys"], PLAIN, s["token"], body, origin=None))["status"] == "accepted"
    return await page.read(pool, s["keys"], s["token"])


async def _connect_stub(pool: asyncpg.Pool, s: dict[str, Any]) -> dict[str, Any]:
    async with tenant_tx(pool, s["who"]["owner"], s["tenant"]) as ctx:
        return await gateway.connect(
            ctx,
            Vault(s["keys"], ctx),
            PLAIN,
            {
                "tmn_code": "TESTSTUB1",
                "hash_secret": "an-invented-test-secret-0001",
                "mode": "stub",
                "display_name": "Pay online",
            },
        )


async def _card(pool: asyncpg.Pool, s: dict[str, Any], invoice_id: str) -> dict[str, Any]:
    return await page.card_payment(
        pool, s["keys"], PLAIN, s["token"], invoice_id, origin=None, client_ip="198.51.100.7"
    )


def _fields(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


# ── Book now ──────────────────────────────────────────────────────────────────────────────────────


async def test_book_now_is_off_by_default_and_the_widget_names_only_bookable_tours(app_pool: asyncpg.Pool) -> None:
    off = await _setup(app_pool, enabled=False)
    with pytest.raises(NotFound) as err:
        await booking.widget(app_pool, off["cache"], booking_client(off["fake"]), off["key"], tour=TOUR, page_id=None)
    assert err.value.code == "booking_not_offered"
    s = await _setup(app_pool)
    w = await booking.widget(app_pool, s["cache"], booking_client(s["fake"]), s["key"], tour=None, page_id=TOUR)
    assert w["tour"] == TOUR and w["currency"] == "VND" and w["honeypot_field"] == s["honeypot"]
    assert [d["departure_id"] for d in w["departures"]] == ["d1"] and w["agency_name"]
    assert "b2b" not in str(w) and "net" not in w
    with pytest.raises(NotFound) as err:
        await booking.widget(app_pool, s["cache"], booking_client(s["fake"]), s["key"], tour="T-unknown", page_id=None)
    assert err.value.code == "tour_not_bookable"
    with pytest.raises(NotFound) as err:  # an unknown form key reads like any unknown form
        await booking.widget(app_pool, s["cache"], booking_client(s["fake"]), "x" * 22, tour=TOUR, page_id=None)
    assert err.value.code == "form_not_found"


async def test_the_live_price_is_the_agency_retail_in_whole_dong(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    out = await booking.price(
        app_pool,
        s["cache"],
        booking_client(s["fake"]),
        s["key"],
        {"tour": TOUR, "departure_id": "d1", "adults": 2, "child_ages": [7]},
    )
    # 10,000,000 B2B + the owner's 20 % = 12,000,000 for three heads.
    assert out["currency"] == "VND" and out["total"] == "12000000.00"
    assert out["lines"] == [
        {"pax_type": "adult", "qty": 2, "unit": "4000000.00"},
        {"pax_type": "child", "qty": 1, "unit": "4000000.00"},
    ]
    odd = await booking.price(
        app_pool,
        s["cache"],
        booking_client(s["fake"]),
        s["key"],
        {"tour": TOUR, "departure_id": "d1", "adults": 7, "child_ages": []},
    )
    assert Decimal(odd["total"]) == Decimal(odd["total"]).to_integral_value() and Decimal(odd["total"]) >= 12000000
    with pytest.raises(Conflict) as err:
        await booking.price(
            app_pool, s["cache"], booking_client(s["fake"]), s["key"], {"tour": TOUR, "departure_id": "d9", "adults": 2}
        )
    assert err.value.code == "departure_not_available"


async def test_a_booking_makes_the_trip_the_sent_proposal_and_the_link(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    press = _press(s)
    out = await _submit(app_pool, s, press)
    assert out["status"] == "created" and out["link_path"].startswith("/c/p/") and out["total"] == "12000000.00"
    trip = (
        await _raw(
            app_pool,
            s,
            "SELECT trip_id, stage, lead_source, start_date, end_date, party_adults, "
            "party_child_ages, destinations, waiting_on FROM crm.trip",
        )
    )[0]
    assert trip["stage"] == "proposal" and trip["lead_source"] == "web_booking" and trip["destinations"] == ["vietnam"]
    assert trip["party_adults"] == 2 and list(trip["party_child_ages"]) == [7]
    assert trip["end_date"] - trip["start_date"] == dt.timedelta(days=1)
    prop = (
        await _raw(
            app_pool,
            s,
            "SELECT status, kind, total_amount, currency FROM crm.proposal WHERE trip_id = $1",
            trip["trip_id"],
        )
    )[0]
    assert (prop["status"], prop["kind"], prop["currency"].strip()) == ("sent", "builder", "VND")
    assert prop["total_amount"] == Decimal("12000000.00")
    history = await _raw(
        app_pool,
        s,
        "SELECT from_stage, to_stage, reason_code FROM crm.trip_stage_history WHERE trip_id = $1",
        trip["trip_id"],
    )
    assert [(h["from_stage"], h["to_stage"], h["reason_code"]) for h in history] == [(None, "proposal", "web_booking")]
    attempt = (await _raw(app_pool, s, "SELECT status, trip_id, quoted_total, party FROM crm.public_booking_attempt"))[
        0
    ]
    assert attempt["status"] == "created" and attempt["trip_id"] == trip["trip_id"]
    assert attempt["quoted_total"] == Decimal("12000000.00")
    # A repeated press is the same attempt (no second trip, no second link).
    assert await _submit(app_pool, s, press) == {"status": "already_created"}
    # A honeypot hit is answered as if stored and stores nothing.
    bot = await _submit(app_pool, s, _press(s, **{s["honeypot"]: "x"}))
    assert bot == {"status": "received"}
    counts = (
        await _raw(
            app_pool,
            s,
            "SELECT (SELECT count(*) FROM crm.traveler) AS travellers, "
            "(SELECT count(*) FROM crm.trip) AS trips, "
            "(SELECT count(*) FROM crm.public_booking_attempt) AS attempts",
        )
    )[0]
    assert (counts["travellers"], counts["trips"], counts["attempts"]) == (1, 1, 1)


async def test_an_existing_clients_email_never_binds_the_visitor_to_that_client(app_pool: asyncpg.Pool) -> None:
    """Review finding: nobody checks the e-mail a visitor types, and the answer hands the visitor a live client link.
    A booking with an existing client's e-mail (here an archived one) makes a new client: the link, the lead and the
    terms the visitor could accept belong to the new client; the existing client stays as it was (still archived, no
    trip, no link) and the agent gets an internal note to merge the two once the client confirms. The answer has the
    same shape as for an unknown e-mail."""
    s = await _setup(app_pool)
    first = await _submit(app_pool, s, _press(s))
    known = (await _raw(app_pool, s, "SELECT traveler_id FROM crm.traveler"))[0]["traveler_id"]
    await _raw(
        app_pool,
        s,
        "UPDATE crm.traveler SET archived_at = now(), archive_reason = 'agent', archived_by = 'seed:test' "
        "WHERE traveler_id = $1 RETURNING traveler_id",
        known,
    )
    out = await _submit(app_pool, s, _press(s, name="Dorin Vask", phone=None))
    assert sorted(out) == sorted(first) and out["status"] == "created" and out["link_path"] != first["link_path"]
    rows = await _raw(
        app_pool,
        s,
        "SELECT t.traveler_id, t.archived_at IS NOT NULL AS archived, "
        "(SELECT count(*) FROM crm.trip_traveler tt WHERE tt.traveler_id = t.traveler_id) AS trips, "
        "(SELECT count(*) FROM crm.client_link l WHERE l.traveler_id = t.traveler_id) AS links "
        "FROM crm.traveler t ORDER BY t.created_at, t.traveler_id",
    )
    assert len(rows) == 2
    old = next(r for r in rows if r["traveler_id"] == known)
    new = next(r for r in rows if r["traveler_id"] != known)
    assert (old["archived"], old["trips"], old["links"]) == (True, 1, 1)  # only its own first booking
    assert (new["archived"], new["trips"], new["links"]) == (False, 1, 1)
    trip = (
        await _raw(
            app_pool,
            s,
            "SELECT tt.trip_id FROM crm.trip_traveler tt WHERE tt.traveler_id = $1 AND tt.role = 'lead'",
            new["traveler_id"],
        )
    )[0]["trip_id"]
    notes = await _raw(app_pool, s, "SELECT kind, traveler_id FROM crm.note WHERE trip_id = $1", trip)
    assert sorted(n["kind"] for n in notes) == ["inquiry_message", "internal"]
    assert {n["traveler_id"] for n in notes} == {new["traveler_id"]}
    first_trip_notes = await _raw(
        app_pool, s, "SELECT kind FROM crm.note WHERE kind = 'internal' AND trip_id <> $1", trip
    )
    assert first_trip_notes == []  # the first booking (an unknown e-mail then) has no duplicate note
    # The visitor's link opens the new client's trip, not the existing client's.
    held = await page.read(app_pool, s["keys"], _token(out["link_path"]))
    lead = await _raw(
        app_pool,
        s,
        "SELECT l.traveler_id FROM crm.client_link l JOIN crm.proposal p ON p.link_id = l.link_id WHERE p.trip_id = $1",
        trip,
    )
    assert held and [r["traveler_id"] for r in lead] == [new["traveler_id"]]


async def test_a_sold_out_departure_is_refused_and_recorded(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    s["fake"].availability = "sold_out"
    with pytest.raises(Conflict) as err:
        await _submit(app_pool, s, _press(s))
    assert err.value.code == "aa_sold_out"
    row = (await _raw(app_pool, s, "SELECT status, refusal_code, trip_id FROM crm.public_booking_attempt"))[0]
    assert (row["status"], row["refusal_code"], row["trip_id"]) == ("refused", "aa_sold_out", None)
    assert (await _raw(app_pool, s, "SELECT count(*) AS n FROM crm.trip"))[0]["n"] == 0


async def test_one_address_gets_five_attempts_an_hour(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    s["fake"].availability = "sold_out"  # cheap refusals still count as attempts
    for _ in range(booking.MAX_PER_ADDRESS_HOUR):
        with pytest.raises(Conflict):
            await _submit(app_pool, s, _press(s), ip="203.0.113.9")
    with pytest.raises(Exception) as err:
        await _submit(app_pool, s, _press(s), ip="203.0.113.9")
    assert getattr(err.value, "code", None) == "booking_rate_limited"
    stored = await _raw(app_pool, s, "SELECT ip_hash FROM crm.public_booking_attempt LIMIT 1")
    assert b"203.0.113.9" not in bytes(stored[0]["ip_hash"])  # an HMAC, never the address


# ── online payment through the agency's own gateway ──────────────────────────────────────────────


async def test_web_booking_accept_issues_the_deposit_and_the_stub_gateway_pays_it(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    p = await _book_and_accept(app_pool, s)
    deposit = next(i for i in p["payments"] if i["kind"] == "deposit")
    assert deposit["pay_window"]["state"] == "open" and deposit["pay_by"]["instructions"] == BANK
    assert deposit["pay_online"] is False
    with pytest.raises(Conflict) as err:
        await _card(app_pool, s, deposit["invoice_id"])
    assert err.value.code == "card_not_offered"

    connected = await _connect_stub(app_pool, s)
    assert connected["vn_gateway"]["mode"] == "stub" and connected["vn_gateway"]["ipn_url"].endswith(gateway.IPN_PATH)
    assert "hash_secret" not in str(connected) and "an-invented-test-secret" not in str(connected)
    p = await page.read(app_pool, s["keys"], s["token"])
    deposit = next(i for i in p["payments"] if i["kind"] == "deposit")
    assert deposit["pay_online"] is True
    started = await _card(app_pool, s, deposit["invoice_id"])
    fields = _fields(started["redirect_url"])
    assert gateway.STUB_PATH in started["redirect_url"]
    assert Decimal(fields["vnp_Amount"]) / 100 == Decimal(deposit["balance_amount"])
    assert vnpay.verify(fields, "an-invented-test-secret-0001") and fields["vnp_TmnCode"] == "TESTSTUB1"
    html_page = await gateway.stub_page(app_pool, s["keys"], PLAIN, fields)
    assert "Test payment gateway" in html_page and "No real money moves" in html_page

    back = await gateway.stub_pay(app_pool, s["keys"], PLAIN, fields["vnp_TxnRef"], "paid")
    assert back.endswith(gateway.RETURN_PATH)
    inv = (
        await _raw(
            app_pool, s, "SELECT status FROM crm.invoice WHERE invoice_id = $1", uuid.UUID(deposit["invoice_id"])
        )
    )[0]
    assert inv["status"] == "paid"
    pay = await _raw(app_pool, s, "SELECT method, provider, provider_ref, amount FROM crm.payment")
    assert [(r["method"], r["provider"]) for r in pay] == [("online", "vn_gateway")] and pay[0]["provider_ref"]
    session = (await _raw(app_pool, s, "SELECT status, payment_id, response_code FROM crm.payment_session"))[0]
    assert session["status"] == "paid" and session["payment_id"] is not None and session["response_code"] == "00"
    trip = (await _raw(app_pool, s, "SELECT stage FROM crm.trip"))[0]
    assert trip["stage"] == "booked"

    # The same IPN again: already confirmed, no second payment.
    signed = vnpay.notice_fields(
        tmn_code="TESTSTUB1",
        txn_ref=fields["vnp_TxnRef"],
        amount=Decimal(deposit["balance_amount"]),
        succeeded=True,
        transaction_no="99887766",
        when=dt.datetime.now(dt.UTC),
        secret="an-invented-test-secret-0001",
    )
    assert (await gateway.handle_ipn(app_pool, s["keys"], signed))["RspCode"] == vnpay.ALREADY
    assert len(await _raw(app_pool, s, "SELECT payment_id FROM crm.payment")) == 1


async def test_the_ipn_refuses_forgeries_and_records_money_it_cannot_settle(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    p = await _book_and_accept(app_pool, s)
    await _connect_stub(app_pool, s)
    deposit = next(i for i in p["payments"] if i["kind"] == "deposit")
    first = _fields((await _card(app_pool, s, deposit["invoice_id"]))["redirect_url"])
    second = _fields((await _card(app_pool, s, deposit["invoice_id"]))["redirect_url"])
    amount = Decimal(deposit["balance_amount"])
    now = dt.datetime.now(dt.UTC)

    def ipn(
        ref: str,
        *,
        value: Decimal = amount,
        key: str = "an-invented-test-secret-0001",
        ok: bool = True,
        number: str = "11223344",
    ) -> dict[str, str]:
        return vnpay.notice_fields(
            tmn_code="TESTSTUB1", txn_ref=ref, amount=value, succeeded=ok, transaction_no=number, when=now, secret=key
        )

    async def answer(fields: dict[str, str]) -> str:
        return (await gateway.handle_ipn(app_pool, s["keys"], fields))["RspCode"]

    assert await answer(ipn(first["vnp_TxnRef"], key="a-forged-secret-of-someone")) == vnpay.BAD_SIGNATURE
    assert await answer(ipn("0" * 24)) == vnpay.NOT_FOUND
    assert await answer(ipn(first["vnp_TxnRef"], value=amount - 1)) == vnpay.BAD_AMOUNT
    assert await answer({"vnp_TxnRef": first["vnp_TxnRef"]}) == vnpay.OTHER
    assert await _raw(app_pool, s, "SELECT payment_id FROM crm.payment") == []
    # A cancelled payment closes its session, nothing is recorded.
    assert await answer(ipn(first["vnp_TxnRef"], ok=False)) == vnpay.CONFIRMED
    # The second session pays the invoice.
    assert await answer(ipn(second["vnp_TxnRef"], number="55667788")) == vnpay.CONFIRMED
    # Money for the first (failed) session arrives late: the invoice is paid already, so it goes to review.
    assert await answer(ipn(first["vnp_TxnRef"], number="55667799")) == vnpay.CONFIRMED
    sessions = await _raw(app_pool, s, "SELECT txn_ref, status FROM crm.payment_session")
    assert {r["txn_ref"]: r["status"] for r in sessions} == {
        first["vnp_TxnRef"]: "review",
        second["vnp_TxnRef"]: "paid",
    }
    pays = await _raw(app_pool, s, "SELECT schedule_id, provider_ref FROM crm.payment ORDER BY provider_ref")
    assert [(r["provider_ref"], r["schedule_id"] is None) for r in pays] == [("55667788", False), ("55667799", True)]
    tasks = await _raw(app_pool, s, "SELECT priority, status FROM crm.task WHERE kind = 'payment_review'")
    assert [(t["priority"], t["status"]) for t in tasks] == [("urgent", "open")]


async def test_stub_mode_is_dev_only_and_never_simulates_a_real_merchant(app_pool: asyncpg.Pool) -> None:
    from types import SimpleNamespace

    from app.errors import Invalid

    with pytest.raises(Invalid) as err:
        gateway.check_connect(
            tmn_code="TESTSTUB1", hash_secret="x" * 20, mode="stub", display_name=None, environment="prod"
        )
    assert err.value.code == "mode_not_allowed"
    s = await _setup(app_pool)
    await _book_and_accept(app_pool, s)
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        await gateway.connect(
            ctx,
            Vault(s["keys"], ctx),
            PLAIN,
            {"tmn_code": "SANDBOX01", "hash_secret": "an-invented-test-secret-0002", "mode": "sandbox"},
        )
    p = await page.read(app_pool, s["keys"], s["token"])
    deposit = next(i for i in p["payments"] if i["kind"] == "deposit")
    started = await _card(app_pool, s, deposit["invoice_id"])
    assert started["redirect_url"].startswith(vnpay.PAY_URLS["sandbox"])
    ref = _fields(started["redirect_url"])["vnp_TxnRef"]
    with pytest.raises(NotFound):
        await gateway.stub_pay(app_pool, s["keys"], PLAIN, ref, "paid")
    with pytest.raises(NotFound):
        prod = SimpleNamespace(environment="prod", messaging_hooks_origin=lambda: "https://client.example.test")
        await gateway.stub_pay(app_pool, s["keys"], prod, ref, "paid")  # type: ignore[arg-type]


async def test_the_purge_keeps_held_attempts(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    out = await _submit(app_pool, s, _press(s))
    assert out["status"] == "created"
    s["fake"].availability = "sold_out"
    with pytest.raises(Conflict):
        await _submit(app_pool, s, _press(s))
    trip = (await _raw(app_pool, s, "SELECT trip_id FROM crm.trip"))[0]["trip_id"]
    await _raw(app_pool, s, "UPDATE crm.public_booking_attempt SET created_at = now() - interval '91 days'")
    await _raw(
        app_pool, s, "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) VALUES ('trip', $1, 'dispute')", trip
    )
    rows = await _raw(app_pool, s, booking.PURGE_SQL, booking.RETENTION_DAYS)
    assert rows == []
    left = await _raw(app_pool, s, "SELECT status FROM crm.public_booking_attempt")
    assert [r["status"] for r in left] == ["created"]  # the refused one left; the held trip's stays
