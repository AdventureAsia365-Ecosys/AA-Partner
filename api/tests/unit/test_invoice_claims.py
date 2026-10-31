"""AA-952 slice 4: the Python branches of the pay window, the claim, Booking's one hold extension, the re-hold of a late
deposit and the overdue reminders over a :class:`ScriptedConn` and the in-memory Booking (synthetic data). The SQL,
RLS, the status moves and the whole flow run on PostgreSQL in tests/integration/test_invoice_claims_api.py."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest

from app.booking import contract, rehold
from app.errors import Conflict, Gone, NotFound, ServiceError
from app.payments import claims, reminders
from tests.aa_fakes import FakeBooking, booking_client
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, system_ctx, tenant_ctx

NOW = dt.datetime(2026, 10, 31, 9, 0, tzinfo=dt.UTC)
INVOICE = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000c2")
PROPOSAL = uuid.UUID("00000000-0000-4000-8000-0000000000c3")
SCHEDULE = uuid.UUID("00000000-0000-4000-8000-0000000000c4")
OPTION = "00000000-0000-4000-8000-0000000000c5"
HASH = "a" * 64


@contextmanager
def _raises(cls: type[ServiceError], code: str) -> Iterator[None]:
    with pytest.raises(cls) as err:
        yield
    assert err.value.code == code, err.value.code


def _row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "invoice_id": INVOICE, "trip_id": TRIP, "kind": "deposit", "status": "open", "number": "INV-000007",
        "currency": "USD", "total_amount": Decimal("300"), "due_on": dt.date(2026, 11, 2), "proposal_id": PROPOSAL,
        "schedule_id": SCHEDULE, "claimed_at": None, "hold_extended_at": None, "hold_extend_code": None,
        "rehold_requested_at": None, "rehold_code": None, "proposal_kind": "builder", "accepted_hold_id": "h1",
        "hold_id": "h1", "hold_expires_at": NOW + dt.timedelta(hours=10), "paid_amount": Decimal(0),
    }  # fmt: skip
    row.update(over)
    return row


# ── the pay window ────────────────────────────────────────────────────────────────────────────────


def test_pay_window_states() -> None:
    until = (NOW + dt.timedelta(hours=10)).isoformat()
    assert claims.pay_window(_row(), NOW) == {"state": "open", "until": until}
    assert claims.pay_window(_row(hold_expires_at=NOW - dt.timedelta(seconds=1)), NOW)["state"] == "expired"
    assert claims.pay_window(_row(hold_expires_at=None), NOW)["state"] == "expired"
    assert claims.pay_window(_row(status="payment_claimed"), NOW) == {"state": "claimed", "until": until}
    assert claims.pay_window(_row(rehold_requested_at=NOW), NOW) == {"state": "settling", "until": None}
    assert claims.pay_window(_row(status="paid"), NOW)["state"] == "paid"
    assert claims.pay_window(_row(status="paid_unfulfillable"), NOW)["state"] == "paid"
    assert claims.pay_window(_row(status="void"), NOW)["state"] == "closed"
    # No AA hold behind it (a text proposal, a balance, a service fee): no window, open until paid.
    for over in ({"proposal_kind": "text"}, {"kind": "balance"}, {"accepted_hold_id": None},
                 {"schedule_id": None, "kind": "service_fee"}):  # fmt: skip
        assert claims.pay_window(_row(hold_expires_at=NOW - dt.timedelta(days=3), **over), NOW) == {
            "state": "open",
            "until": None,
        }


def test_client_invoice_shows_bank_details_only_while_payable() -> None:
    pay_by = {"display_name": "Bank transfer", "instructions": "Account 0000 5555"}
    shown = claims._client_invoice(_row(paid_amount=Decimal("100")), NOW, pay_by)
    assert shown["pay_by"] == {"name": "Bank transfer", "instructions": "Account 0000 5555"} and shown["can_claim"]
    assert shown["balance_amount"] == "200.00" and shown["total_amount"] == "300.00"
    gone = claims._client_invoice(_row(hold_expires_at=NOW - dt.timedelta(minutes=1)), NOW, pay_by)
    assert gone["pay_by"] is None and not gone["can_claim"] and gone["pay_window"]["state"] == "expired"
    assert claims._client_invoice(_row(), NOW, None)["pay_by"] is None  # no method saved: nothing to print


def test_agency_view() -> None:
    view = claims.agency_view(_row(status="payment_claimed", claimed_at=NOW, rehold_requested_at=NOW,
                                   rehold_code="reheld"), NOW)  # fmt: skip
    assert view["claimed_at"] == NOW.isoformat() and view["rehold"] == {"requested_at": NOW.isoformat(),
                                                                       "code": "reheld"}  # fmt: skip
    assert claims.agency_view(_row(), NOW)["rehold"] is None


# ── the claim ─────────────────────────────────────────────────────────────────────────────────────


def _claim_conn(row: dict[str, Any] | None) -> ScriptedConn:
    return ScriptedConn().on("FOR UPDATE OF i", row).on("FROM crm.trip WHERE trip_id", None)


@pytest.mark.asyncio
async def test_claim_refusals_and_repeat() -> None:
    for row in (None, _row(proposal_id=uuid.uuid4()), _row(kind="service_fee"), _row(number=None)):
        with _raises(NotFound, "invoice_not_found"):
            await claims.claim(system_ctx(_claim_conn(row)), PROPOSAL, INVOICE, NOW)
    conn = _claim_conn(_row(hold_expires_at=NOW - dt.timedelta(minutes=1)))
    ctx = system_ctx(conn)
    with _raises(Gone, "pay_link_expired"):
        await claims.claim(ctx, PROPOSAL, INVOICE, NOW)
    assert [e.reason for e in ctx.audit_events if e.outcome == "denied"] == ["pay_link_expired"]
    assert not conn.sql("SET status = 'payment_claimed'")
    with _raises(Conflict, "invoice_not_payable"):
        await claims.claim(system_ctx(_claim_conn(_row(status="paid"))), PROPOSAL, INVOICE, NOW)
    for state in ({"status": "payment_claimed"}, {"rehold_requested_at": NOW}):
        conn = _claim_conn(_row(**state))
        made = await claims.claim(system_ctx(conn), PROPOSAL, INVOICE, NOW)
        assert not made.new and made.hold_id is None and not conn.sql("INSERT INTO crm.task")


@pytest.mark.asyncio
async def test_claim_with_and_without_a_hold_to_extend() -> None:
    conn = _claim_conn(_row())
    made = await claims.claim(system_ctx(conn), PROPOSAL, INVOICE, NOW)
    assert made.new and made.hold_id == "h1"
    assert conn.sql("SET status = 'payment_claimed'") == [(INVOICE,)]
    task = conn.sql("INSERT INTO crm.task")[0]
    assert task[1:] == (claims.VERIFY_TASK, "hold_expiry", NOW + dt.timedelta(hours=10))
    assert conn.sql("INSERT INTO crm.metric_event")[0][2] == "invoice.payment_claimed"
    # Extended already (a claim after "not received"): no second extension.
    again = await claims.claim(system_ctx(_claim_conn(_row(hold_extended_at=NOW))), PROPOSAL, INVOICE, NOW)
    assert again.new and again.hold_id is None
    # A text proposal's deposit: the agent gets two days, no Booking.
    conn = _claim_conn(_row(proposal_kind="text"))
    made = await claims.claim(system_ctx(conn), PROPOSAL, INVOICE, NOW)
    assert made.hold_id is None and conn.sql("INSERT INTO crm.task")[0][2:] == ("created", NOW + dt.timedelta(days=2))
    # The proposal lost its hold columns (never after accept, defensive): nothing to extend, said so.
    conn = _claim_conn(_row(hold_id=None))
    assert (await claims.claim(system_ctx(conn), PROPOSAL, INVOICE, NOW)).hold_id is None
    assert conn.sql("SET hold_extend_code = $2")[0] == (INVOICE, claims.NOT_NEEDED)


@pytest.mark.asyncio
async def test_extend_maps_booking_answers() -> None:
    assert (await claims.extend(None, TENANT, INVOICE, "h1")).code == claims.UNAVAILABLE
    fake = FakeBooking()
    fake.holds["h1"] = {"hold_id": "h1", "quote_id": "q1", "status": "active", "extended_count": 0, "lines": [],
                        "expires_at": (NOW.replace(year=2030)).isoformat()}  # fmt: skip
    ext = await claims.extend(booking_client(fake), TENANT, INVOICE, "h1")
    assert ext.code == "extended" and ext.expires_at == NOW.replace(year=2030) + dt.timedelta(hours=72)
    assert fake.extend_keys == [claims.extend_key(INVOICE)]
    assert (await claims.extend(booking_client(fake), TENANT, INVOICE, "h1")).code == "extended"  # same key
    assert (await claims.extend(booking_client(fake), TENANT, uuid.uuid4(), "h1")).code == "extend_limit"
    assert (await claims.extend(booking_client(fake), TENANT, INVOICE, "nope")).code == "not_found"
    fake.holds["h2"] = dict(fake.holds["h1"], hold_id="h2", status="confirmed", extended_count=0)
    assert (await claims.extend(booking_client(fake), TENANT, INVOICE, "h2")).code == "invalid"
    fake.down = True
    assert (await claims.extend(booking_client(fake), TENANT, INVOICE, "h1")).code == claims.UNAVAILABLE


@pytest.mark.asyncio
async def test_finish_claim() -> None:
    later = NOW + dt.timedelta(hours=82)
    conn = _claim_conn(_row(status="payment_claimed"))
    out = await claims.finish_claim(system_ctx(conn), INVOICE, "h1", claims.Extension("extended", later))
    assert out == {"code": "extended", "until": later.isoformat()}
    assert conn.sql("SET hold_extended_at = now()") and conn.sql("UPDATE crm.proposal SET hold_expires_at")[0] == (
        PROPOSAL,
        "h1",
        later,
    )
    assert conn.sql("UPDATE crm.task SET due_at")[0][2] == later and conn.sql("INSERT INTO crm.job")
    conn = _claim_conn(_row(status="payment_claimed"))
    out = await claims.finish_claim(system_ctx(conn), INVOICE, "h1", claims.Extension("extend_limit"))
    assert out == {"code": "extend_limit", "until": None} and conn.sql("SET hold_extend_code")[0][1] == "extend_limit"
    done = await claims.finish_claim(system_ctx(_claim_conn(_row(hold_extended_at=NOW))), INVOICE, "h1",
                                     claims.Extension("extended", later))  # fmt: skip
    assert done == {"code": "extended", "until": None}
    gone = await claims.finish_claim(system_ctx(_claim_conn(None)), INVOICE, "h1", claims.Extension("invalid"))
    assert gone == {"code": "invalid", "until": None}


@pytest.mark.asyncio
async def test_reject_claim_and_close_verify() -> None:
    with _raises(NotFound, "invoice_not_found"):
        await claims.reject_claim(tenant_ctx(_claim_conn(None)), INVOICE)
    with _raises(Conflict, "invoice_not_claimed"):
        await claims.reject_claim(tenant_ctx(_claim_conn(_row())), INVOICE)
    with _raises(Conflict, "invoice_settling"):
        await claims.reject_claim(tenant_ctx(_claim_conn(_row(status="payment_claimed", rehold_requested_at=NOW))),
                                  INVOICE)  # fmt: skip
    conn = _claim_conn(_row(status="payment_claimed")).on("status = 'payment_claimed' AND deleted_at IS NULL", False)
    assert await claims.reject_claim(tenant_ctx(conn), INVOICE) == TRIP
    assert conn.sql("SET status = 'open'") and conn.sql("UPDATE crm.task SET status = $3")[0][2] == "skipped"
    # Another invoice of the trip is still claimed: its verify task stays open.
    conn = _claim_conn(_row(status="payment_claimed")).on("status = 'payment_claimed' AND deleted_at IS NULL", True)
    await claims.reject_claim(tenant_ctx(conn), INVOICE)
    assert not conn.sql("UPDATE crm.task SET status = $3")
    conn = ScriptedConn().on("status = 'payment_claimed' AND deleted_at IS NULL", False)
    await claims.close_verify(system_ctx(conn), TRIP, INVOICE, "job:x")
    assert conn.sql("UPDATE crm.task SET status = $3")[0][2:] == ("done", "job:x")


@pytest.mark.asyncio
async def test_card_payment_never_starts_a_session_past_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr(claims.providers, "provider", lambda name: asked.append(name))
    with _raises(Gone, "pay_link_expired"):
        await claims.card_payment(system_ctx(_claim_conn(_row(hold_expires_at=NOW - dt.timedelta(minutes=1)))),
                                  PROPOSAL, INVOICE, NOW)  # fmt: skip
    assert asked == []
    with _raises(Conflict, "invoice_not_payable"):
        await claims.card_payment(system_ctx(_claim_conn(_row(status="payment_claimed"))), PROPOSAL, INVOICE, NOW)
    with _raises(Conflict, "card_not_offered"):
        await claims.card_payment(system_ctx(_claim_conn(_row())), PROPOSAL, INVOICE, NOW)
    conn = _claim_conn(_row()).on("charges_enabled", {"account_id": uuid.uuid4(), "provider": "stripe"})
    with _raises(Conflict, "provider_not_available"):
        await claims.card_payment(system_ctx(conn), PROPOSAL, INVOICE, NOW)
    assert asked == ["stripe"]


@pytest.mark.asyncio
async def test_extend_due_and_client_invoices() -> None:
    conn = ScriptedConn().on("hold_extend_code = $1", [{"invoice_id": INVOICE, "hold_id": "h1"}])
    assert await claims.extend_due(system_ctx(conn)) == [(INVOICE, "h1")]
    assert await claims.client_invoices(system_ctx(ScriptedConn()), PROPOSAL, NOW) == []
    conn = ScriptedConn().on("i.kind = ANY($2::text[])", [_row()]).on("provider = 'manual'", None)
    assert [i["invoice_id"] for i in await claims.client_invoices(system_ctx(conn), PROPOSAL, NOW)] == [str(INVOICE)]


# ── terms first (S261 live finding F1: AA-955 x AA-952) ────────────────────────────────────────────


TERMS_DOC = uuid.UUID("00000000-0000-4000-8000-0000000000c6")
CURRENT_TERMS = "kind = 'booking_terms' AND status = 'published'"


def _terms_pending(conn: ScriptedConn, *, accepted: bool = False, deposit_paid: bool = False) -> ScriptedConn:
    """The agency published booking terms the trip has not accepted (crm.terms_acceptance), deposit unpaid."""
    conn.on(CURRENT_TERMS, {"doc_id": TERMS_DOC, "version_label": "v2026-11", "body_text": "Terms",
                            "body_sha256": b"\x03" * 32, "published_at": NOW})  # fmt: skip
    conn.on("AS deposit_paid", {"proposal_id": PROPOSAL, "price_hash": b"\x04" * 32, "link_id": None,
                                "accepted": accepted, "deposit_paid": deposit_paid})  # fmt: skip
    return conn


def test_pay_window_terms_first() -> None:
    until = (NOW + dt.timedelta(hours=10)).isoformat()
    assert claims.pay_window(_row(), NOW, terms_first=True) == {"state": "terms", "until": until}
    # A claim, a re-hold, an expired window or a settled invoice keep their own state.
    assert claims.pay_window(_row(status="payment_claimed"), NOW, terms_first=True)["state"] == "claimed"
    assert claims.pay_window(_row(rehold_requested_at=NOW), NOW, terms_first=True)["state"] == "settling"
    expired = _row(hold_expires_at=NOW - dt.timedelta(seconds=1))
    assert claims.pay_window(expired, NOW, terms_first=True)["state"] == "expired"
    assert claims.pay_window(_row(status="paid"), NOW, terms_first=True)["state"] == "paid"
    pay_by = {"display_name": "Bank transfer", "instructions": "Account 0000 5555"}
    shown = claims._client_invoice(_row(), NOW, pay_by, terms_first=True)
    assert shown["pay_window"]["state"] == "terms" and shown["pay_by"] is None and not shown["can_claim"]


@pytest.mark.asyncio
async def test_client_invoices_hide_the_pay_details_until_the_terms_are_accepted() -> None:
    rows = [_row(), _row(invoice_id=uuid.uuid4(), kind="balance", number="INV-000008")]
    pay_by = {"display_name": "Bank transfer", "instructions": "Account 0000 5555"}

    def listed(**terms: bool) -> ScriptedConn:
        conn = ScriptedConn().on("i.kind = ANY($2::text[])", rows).on("provider = 'manual'", pay_by)
        return _terms_pending(conn, **terms)

    deposit, balance = await claims.client_invoices(system_ctx(listed()), PROPOSAL, NOW)
    assert deposit["pay_window"]["state"] == "terms" and deposit["pay_by"] is None and not deposit["can_claim"]
    assert balance["pay_window"]["state"] == "open" and balance["can_claim"]  # the rule is the deposit's only
    for done in ({"accepted": True}, {"deposit_paid": True}):
        deposit, _ = await claims.client_invoices(system_ctx(listed(**done)), PROPOSAL, NOW)
        assert deposit["pay_window"]["state"] == "open" and deposit["can_claim"] and deposit["pay_by"] is not None
    # No deposit among the invoices: the terms are not even read.
    conn = ScriptedConn().on("i.kind = ANY($2::text[])", rows[1:]).on("provider = 'manual'", pay_by)
    await claims.client_invoices(system_ctx(_terms_pending(conn)), PROPOSAL, NOW)
    assert not conn.sql(CURRENT_TERMS)


@pytest.mark.asyncio
async def test_claim_waits_for_the_terms_with_nothing_written() -> None:
    conn = _terms_pending(_claim_conn(_row()))
    ctx = system_ctx(conn)
    with pytest.raises(Conflict) as err:
        await claims.claim(ctx, PROPOSAL, INVOICE, NOW)
    assert err.value.code == "terms_reaccept_required" and err.value.extra == {"version_label": "v2026-11"}
    assert [(e.action, e.reason) for e in ctx.audit_events if e.outcome == "denied"] == [
        ("invoice_claim", "terms_reaccept_required")
    ]
    # No claim, no verify task, no bell, no commitment move, nothing to extend.
    for fragment in ("SET status = 'payment_claimed'", "INSERT INTO crm.task", "INSERT INTO crm.metric_event",
                     "INSERT INTO crm.trip_commitment", "SET hold_extend_code"):  # fmt: skip
        assert not conn.sql(fragment), fragment
    # Accepted since: the claim goes through as before.
    made = await claims.claim(system_ctx(_terms_pending(_claim_conn(_row()), accepted=True)), PROPOSAL, INVOICE, NOW)
    assert made.new and made.hold_id == "h1"
    # A balance invoice is not held by the deposit's rule; a claimed invoice answers as before.
    conn = _terms_pending(_claim_conn(_row(kind="balance")))
    assert (await claims.claim(system_ctx(conn), PROPOSAL, INVOICE, NOW)).new and not conn.sql(CURRENT_TERMS)
    again = await claims.claim(system_ctx(_terms_pending(_claim_conn(_row(status="payment_claimed")))), PROPOSAL,
                               INVOICE, NOW)  # fmt: skip
    assert not again.new and again.hold_id is None


@pytest.mark.asyncio
async def test_card_payment_waits_for_the_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr(claims.providers, "provider", lambda name: asked.append(name))
    conn = _terms_pending(_claim_conn(_row())).on("charges_enabled", {"account_id": uuid.uuid4(), "provider": "stripe"})
    ctx = system_ctx(conn)
    with _raises(Conflict, "terms_reaccept_required"):
        await claims.card_payment(ctx, PROPOSAL, INVOICE, NOW)
    assert asked == [] and [e.reason for e in ctx.audit_events if e.outcome == "denied"] == ["terms_reaccept_required"]


# ── the re-hold ───────────────────────────────────────────────────────────────────────────────────


def _plan(**over: Any) -> rehold.Plan:
    base: dict[str, Any] = {"invoice_id": INVOICE, "tenant_id": TENANT, "hold_id": "h1", "aa_ids": [OPTION],
                            "body": {"proposal_ref": f"crm-proposal:{PROPOSAL}", "proposal_version": 1,
                                     "currency": "USD", "lines": [{"line_ref": OPTION, "component_id": "tp:1",
                                                                   "service_date": "2026-12-01",
                                                                   "pax": {"adult": 2, "child": 0, "infant": 0}}]},
                            "quote_id": None, "price_hash": None, "requested_at": NOW}  # fmt: skip
    base.update(over)
    return rehold.Plan(**base)


def _old_hold(fake: FakeBooking, *, live: bool = False, price: str = "300.00") -> None:
    expires = NOW.replace(year=2030) if live else NOW - dt.timedelta(hours=1)
    fake.holds["h1"] = {"hold_id": "h1", "quote_id": "q0", "status": "active", "extended_count": 1,
                        "expires_at": expires.isoformat(), "price_hash": HASH, "currency": "USD",
                        "fx": {"rate_per_usd": "1", "rate_date": "2026-10-25"},
                        "lines": [{"line_ref": OPTION, "line_status": "available",
                                   "b2b_price": {"amount": price, "currency": "USD"}}]}  # fmt: skip


async def _yes(_quote: str, _hash: str) -> bool:
    return True


@pytest.mark.asyncio
async def test_ask_holds_again_at_the_same_net() -> None:
    fake = FakeBooking(b2b_price="300.00")
    _old_hold(fake)
    asked = await rehold.ask(booking_client(fake), _plan(), _yes, NOW)
    assert (asked.outcome, asked.code) == ("held", "reheld") and asked.held is not None
    assert fake.hold_keys[f"crm-invoice:{INVOICE}:rehold"] == asked.held.hold_id != "h1"
    # Booking still holds it (the CRM's expiry was stale): nothing new is asked.
    fake = FakeBooking()
    _old_hold(fake, live=True)
    asked = await rehold.ask(booking_client(fake), _plan(), _yes, NOW)
    assert (asked.outcome, asked.code) == ("held", "hold_live") and fake.quotes == 0


@pytest.mark.asyncio
async def test_ask_ends_or_waits() -> None:
    assert (await rehold.ask(booking_client(FakeBooking()), _plan(aa_ids=[]), _yes, NOW)).code == "no_aa_items"
    assert (await rehold.ask(booking_client(FakeBooking()), _plan(), _yes, NOW)).code == "not_found"
    fake = FakeBooking(b2b_price="310.00")  # a higher net: the agency's cost never rises behind its back
    _old_hold(fake)
    assert (await rehold.ask(booking_client(fake), _plan(), _yes, NOW)).code == "price_higher"
    fake = FakeBooking(b2b_price="300.00", availability="sold_out")
    _old_hold(fake)
    assert (await rehold.ask(booking_client(fake), _plan(), _yes, NOW)).code == "sold_out"
    fake = FakeBooking(b2b_price="300.00", hold_refusal="rehold_limit")
    _old_hold(fake)
    assert (await rehold.ask(booking_client(fake), _plan(), _yes, NOW)).outcome == "wait"
    late = _plan(requested_at=NOW - rehold.LIMIT_WAIT)
    assert (await rehold.ask(booking_client(fake), late, _yes, NOW)).outcome == "ended"
    fake = FakeBooking(b2b_price="300.00", hold_refusal="tour_not_served")
    _old_hold(fake)
    assert (await rehold.ask(booking_client(fake), _plan(), _yes, NOW)).code == "tour_not_served"
    fake = FakeBooking(b2b_price="300.00")
    _old_hold(fake)

    async def no(_quote: str, _hash: str) -> bool:
        return False

    assert (await rehold.ask(booking_client(fake), _plan(), no, NOW)).outcome == "retry"
    fake.down = True
    assert (await rehold.ask(booking_client(fake), _plan(), _yes, NOW)).code == "booking_unavailable"


@pytest.mark.asyncio
async def test_ask_replays_the_written_quote() -> None:
    fake = FakeBooking(b2b_price="300.00")
    _old_hold(fake)
    quoted = contract.quote_line(await booking_client(fake).quote(TENANT, _plan().body), OPTION)
    asked = await rehold.ask(booking_client(fake), _plan(quote_id=quoted.quote_id, price_hash=quoted.price_hash),
                             _yes, NOW)  # fmt: skip
    assert asked.code == "reheld" and fake.quotes == 1  # no second quote
    fake.lose_next_hold_answer = True
    other = _plan(invoice_id=uuid.uuid4(), quote_id=quoted.quote_id, price_hash=quoted.price_hash)
    assert (await rehold.ask(booking_client(fake), other, _yes, NOW)).outcome == "retry"


def _apply_conn(row: dict[str, Any] | None) -> ScriptedConn:
    conn = ScriptedConn().on("FOR UPDATE OF i", row)
    conn.on("status = 'payment_claimed' AND deleted_at IS NULL", False)
    conn.on("FROM crm.payment_schedule s", None)
    return conn


@pytest.mark.asyncio
async def test_apply_outcomes() -> None:
    row = {"invoice_id": INVOICE, "trip_id": TRIP, "proposal_id": PROPOSAL, "number": "INV-000007", "kind": "deposit",
           "schedule_id": SCHEDULE}  # fmt: skip
    assert await rehold.apply(system_ctx(_apply_conn(None)), INVOICE, rehold.Asked("held", "reheld")) is None
    assert await rehold.apply(system_ctx(_apply_conn(row)), INVOICE, rehold.Asked("retry", "busy")) is None
    conn = _apply_conn(row)
    assert await rehold.apply(system_ctx(conn), INVOICE, rehold.Asked("wait", "rehold_limit")) is None
    assert conn.sql("SET rehold_code = $2")[0] == (INVOICE, "rehold_limit")

    fake = FakeBooking()
    _old_hold(fake, live=True)
    held = contract.held_set(fake.holds["h1"])
    conn = _apply_conn(row)
    assert await rehold.apply(system_ctx(conn), INVOICE, rehold.Asked("held", "hold_live", held)) == "paid"
    assert conn.sql("UPDATE crm.proposal SET hold_expires_at = $3")[0][1] == "h1"
    assert conn.sql("SET status = $2, paid_at = now()")[0] == (INVOICE, "paid", "hold_live")
    assert conn.sql("SELECT EXISTS ( SELECT 1 FROM crm.invoice i")[0][:2] == (TRIP, INVOICE)
    assert conn.sql("UPDATE crm.task SET status = 'done'")[0][:2] == (TRIP, reminders.TASK)  # nothing left to chase

    conn = _apply_conn(row).on("RETURNING booking_line_id", [{"booking_line_id": uuid.UUID(OPTION)}])
    assert await rehold.apply(system_ctx(conn), INVOICE, rehold.Asked("held", "reheld", held)) == "paid"
    assert conn.sql("hold_count = hold_count + 1") and conn.sql("crm.booking_line_aa_money")[0][1:3] == (
        "cost",
        Decimal("300.00"),
    )

    pays = [{"payment_id": uuid.uuid4(), "trip_id": TRIP, "amount": Decimal("100"), "currency": "USD",
             "left_amount": Decimal("100")},
            {"payment_id": uuid.uuid4(), "trip_id": TRIP, "amount": Decimal("200"), "currency": "USD",
             "left_amount": Decimal("0")}]  # fmt: skip
    conn = _apply_conn(row).on("AS left_amount", pays)
    assert await rehold.apply(system_ctx(conn), INVOICE, rehold.Asked("ended", "sold_out")) == "paid_unfulfillable"
    asks = conn.sql("INSERT INTO crm.refund_request")
    assert len(asks) == 1 and asks[0][4] == Decimal("100") and "(sold_out)" in asks[0][6]
    assert conn.sql("SET status = $2, paid_at = now()")[0] == (INVOICE, "paid_unfulfillable", "sold_out")
    assert conn.sql("INSERT INTO crm.task")[0][1] == rehold.UNFULFILLABLE_TASK
    assert conn.sql("UPDATE crm.task SET status = 'done'")[0][:2] == (TRIP, reminders.TASK)


# ── reminders ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overdue_reminders() -> None:
    today = dt.date(2026, 11, 6)
    conn = ScriptedConn().on("FOR UPDATE OF i SKIP LOCKED", [_row(hold_expires_at=NOW - dt.timedelta(days=1))])
    conn.on("RETURNING reminder_count", 2)
    assert await reminders.overdue(system_ctx(conn), today, NOW) == 1
    args = conn.sql("FOR UPDATE OF i SKIP LOCKED")[0]
    assert args == (today, reminders.MAX_REMINDERS, reminders.EVERY_DAYS, reminders.BATCH)
    event = conn.sql("INSERT INTO crm.metric_event")[0]
    assert event[2] == "invoice.overdue"
    assert '"days":4' in event[3] and '"reminder":2' in event[3] and '"pay_window_closed":true' in event[3]
    assert conn.sql("INSERT INTO crm.task")[0][1] == reminders.TASK
    assert await reminders.overdue(system_ctx(ScriptedConn()), today) == 0


@pytest.mark.asyncio
async def test_the_reminder_task_closes_when_nothing_is_left_to_chase() -> None:
    conn = ScriptedConn()
    await reminders.settled(system_ctx(conn), TRIP, INVOICE, "member:m1")
    assert conn.sql("UPDATE crm.task SET status = 'done'") == [(TRIP, reminders.TASK, "member:m1")]
    conn = ScriptedConn().on("SELECT EXISTS ( SELECT 1 FROM crm.invoice i", True)  # another invoice still overdue
    await reminders.settled(system_ctx(conn), TRIP, INVOICE, "member:m1")
    assert conn.sql("UPDATE crm.task") == []

    today = dt.date(2026, 11, 6)
    conn = ScriptedConn().on("UPDATE crm.task k", [{"task_id": uuid.uuid4()}, {"task_id": uuid.uuid4()}])
    ctx = system_ctx(conn)
    assert await reminders.close_settled(ctx, today) == 2
    assert conn.sql("UPDATE crm.task k")[0] == (reminders.TASK, ctx.actor, today)
    assert await reminders.close_settled(system_ctx(ScriptedConn()), today) == 0
