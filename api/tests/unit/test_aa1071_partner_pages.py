"""AA-1071 :mod:`app.partners.public` without a database (the link context replaced): the intake form's checks in the
public form's order, the view's e-mail code (the agency mailbox or the Dev echo), the code check, the signed-in view
(no traveller name), the partner's code mail at send time, the dispatcher's partner gate and the 24 h code purge.
The SQL runs on PostgreSQL in tests/integration/test_aa1071_partners_api.py."""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.dispatch import worker
from app.errors import Conflict, Forbidden, Invalid, RateLimited, Unavailable
from app.mail.sender import MailSender
from app.partners import public
from app.portal.codes import CODE_MASK
from app.settings import Settings
from app.tenancy import PartnerLink, SystemContext
from tests.unit.fakes import ScriptedConn
from tests.unit.mail_fakes import FakeProvider, MailVault
from tests.unit.slice_fakes import TENANT, FakeVault, enc, system_ctx
from tests.unit.test_mail_send import DISPATCH, MESSAGE, _conn, _message, _runtime

PARTNER = uuid.UUID("00000000-0000-4000-8000-0000000071c1")
LINK = uuid.UUID("00000000-0000-4000-8000-0000000071c2")
FORM = uuid.UUID("00000000-0000-4000-8000-0000000071c3")
TOKEN = "B" * 43
ECHO = Settings(environment="test", db_ssl="disable", portal_otp_dev_echo=True)
MAILBOX_ONLY = Settings(environment="test", db_ssl="disable")
GUARDED = Settings(environment="test", db_ssl="disable", intake_page_origins=("https://go.agency.example",))


def _link(purpose: str = "view") -> PartnerLink:
    return PartnerLink(
        link_id=LINK, tenant_id=TENANT, sales_partner_id=PARTNER, purpose=purpose,
        intake_form_id=FORM if purpose == "intake" else None, expires_at=dt.datetime(2027, 6, 1, tzinfo=dt.UTC),
    )  # fmt: skip


def _patch(monkeypatch: pytest.MonkeyPatch, conn: ScriptedConn, purpose: str = "view") -> SystemContext:
    ctx = SystemContext(conn=conn, tenant_id=TENANT, actor=f"sales_partner_link:{LINK}")  # type: ignore[arg-type]

    @asynccontextmanager
    async def tx(_pool: Any, token_hash: str, *, purpose: str):  # noqa: ANN202
        assert token_hash == public.hash_hex(TOKEN)
        yield ctx, _link(purpose)

    monkeypatch.setattr(public, "partner_link_tx", tx)
    monkeypatch.setattr(public, "Vault", lambda keys, ctx: FakeVault())
    return ctx


def _view_conn() -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("SELECT name_enc, contact_email_enc FROM crm.sales_partner", {
        "name_enc": enc("sales_partner", "name_enc", "Blue Kite"),
        "contact_email_enc": enc("sales_partner", "contact_email_enc", "desk@bluekite.example"),
    })  # fmt: skip
    conn.on("SELECT display_name FROM crm.tenant", "Lotus Trails")
    return conn


# ── intake ───────────────────────────────────────────────────────────────────────────────────────


def _intake_conn() -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM crm.intake_form WHERE intake_form_id", {
        "intake_form_id": FORM, "form_definition_id": uuid.uuid4(), "allowed_origins": [],
        "honeypot_field": "website", "privacy_notice_url": None,
    })  # fmt: skip
    conn.on("FROM crm.form_definition d", {
        "schema": '{"fields": ["name"]}', "display_name": "Lotus Trails", "default_locale": "en",
        "agency_privacy_notice_url": "https://lotus.example/privacy", "consent_text": None,
        "data_request_contact": None,
    })  # fmt: skip
    return conn


@pytest.mark.asyncio
async def test_the_intake_form_and_its_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _intake_conn()
    _patch(monkeypatch, conn, "intake")

    async def offered(ctx: Any) -> dict[str, Any]:
        return {"call_slots": []}

    async def stored(*a: Any, **kw: Any) -> uuid.UUID:
        stored.calls.append(kw)  # type: ignore[attr-defined]
        return uuid.uuid4()

    stored.calls = []  # type: ignore[attr-defined]
    monkeypatch.setattr(public.slots, "offered", offered)
    monkeypatch.setattr(public, "store_submission", stored)
    page = await public.read_form(object(), TOKEN)  # type: ignore[arg-type]
    assert page["fields"] == ["name"] and page["privacy_notice_url"] == "https://lotus.example/privacy"
    assert not await public.submit(object(), object(), ECHO, TOKEN, {"website": "bot"}, origin=None)  # type: ignore[arg-type]
    with pytest.raises(Invalid) as err:
        await public.submit(object(), object(), ECHO, TOKEN, {"name": "Lan"}, origin=None)  # type: ignore[arg-type]
    assert err.value.code == "consent_required"
    good = {"destinations": ["laos"], "adults": 1, "budget_band": "under_1000", "name": "An", "email": "an@x.test",
            "consent_contact": True, "client_submission_id": str(uuid.uuid4())}  # fmt: skip
    with pytest.raises(RateLimited):
        await public.submit(object(), object(), ECHO, TOKEN, good, origin=None, form_quota=lambda *_, **__: False)  # type: ignore[arg-type]
    assert await public.submit(object(), object(), ECHO, TOKEN, good, origin=None, form_quota=lambda *_, **__: True)  # type: ignore[arg-type]
    assert stored.calls[-1]["sales_partner_id"] == PARTNER  # type: ignore[attr-defined]
    assert stored.calls[-1]["idempotency_key"].startswith(f"partner:{LINK.hex[:12]}:")  # type: ignore[attr-defined]
    assert conn.sql("SET use_count = use_count + 1") == [(LINK,)]
    with pytest.raises(Forbidden):
        await public.submit(object(), object(), GUARDED, TOKEN, good, origin="https://evil.example")  # type: ignore[arg-type]
    with pytest.raises(Unavailable):
        await public.submit(
            object(),
            object(),
            SimpleNamespace(intake_origin_check=True, intake_page_origins=()),
            TOKEN,
            good,
            origin=None,
        )  # type: ignore[arg-type]
    conn.on("FROM crm.intake_form WHERE intake_form_id", None)
    with pytest.raises(Unavailable):
        await public.read_form(object(), TOKEN)  # type: ignore[arg-type]


# ── view ─────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_code_goes_out_from_the_agency_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _view_conn()
    conn.on("FROM crm.channel_account", uuid.uuid4())
    queued: list[dict[str, Any]] = []

    async def enqueue(ctx: Any, **kw: Any) -> dict[str, Any]:
        queued.append(kw)
        return {}

    monkeypatch.setattr(public.dispatch, "enqueue_partner_message", enqueue)
    _patch(monkeypatch, conn)
    out = await public.request_code(object(), object(), MAILBOX_ONLY, TOKEN, origin=None)  # type: ignore[arg-type]
    assert out == {"sent": True, "to_hint": "d***@b*******.example", "expires_in_s": 600}
    assert conn.sql("INSERT INTO crm.conversation") and queued[0]["sales_partner_id"] == PARTNER
    body = conn.sql("INSERT INTO crm.message")[0][4]
    assert CODE_MASK.encode() in body  # the stored mail never shows the code
    assert conn.sql("SET message_id = $2, code_enc = $3")
    conn.on("FROM crm.sales_partner_otp WHERE link_id = $1 AND created_at", 5)
    with pytest.raises(RateLimited):
        await public.request_code(object(), object(), MAILBOX_ONLY, TOKEN, origin=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_no_mailbox_no_code_unless_dev_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _view_conn()
    _patch(monkeypatch, conn)
    with pytest.raises(Conflict) as err:
        await public.request_code(object(), object(), MAILBOX_ONLY, TOKEN, origin=None)  # type: ignore[arg-type]
    assert err.value.code == "code_delivery_unavailable"
    echoed = await public.request_code(object(), object(), ECHO, TOKEN, origin=None)  # type: ignore[arg-type]
    assert echoed["sent"] is False and len(echoed["dev_code"]) == 6
    conn.on("SELECT name_enc, contact_email_enc FROM crm.sales_partner", {"name_enc": b"x", "contact_email_enc": None})
    with pytest.raises(Conflict) as err:
        await public.request_code(object(), object(), ECHO, TOKEN, origin=None)  # type: ignore[arg-type]
    assert err.value.code == "no_email_on_file"
    with pytest.raises(Forbidden):
        await public.request_code(object(), object(), GUARDED, TOKEN, origin="https://evil.example")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_code_check(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _view_conn()
    _patch(monkeypatch, conn)
    otp = uuid.uuid4()
    assert await public.verify_code(object(), ECHO, TOKEN, "123456", origin=None) == (  # type: ignore[arg-type]
        None, {"detail": "code_invalid", "attempts_left": 0},
    )  # fmt: skip
    conn.on("WHERE link_id = $1 AND status = 'pending'", {"otp_id": otp, "code_hash": public.code_hash(otp, "246810"),
                                                          "expired": True})  # fmt: skip
    assert (await public.verify_code(object(), ECHO, TOKEN, "246810", origin=None))[1]["detail"] == "code_expired"  # type: ignore[arg-type]
    conn.on("WHERE link_id = $1 AND status = 'pending'", {"otp_id": otp, "code_hash": public.code_hash(otp, "246810"),
                                                          "expired": False})  # fmt: skip
    conn.on("RETURNING attempts", 2)
    assert (await public.verify_code(object(), ECHO, TOKEN, "12", origin=None))[1] == {  # type: ignore[arg-type]
        "detail": "code_invalid", "attempts_left": 3,
    }  # fmt: skip
    session, ok = await public.verify_code(object(), ECHO, TOKEN, "246810", origin=None)  # type: ignore[arg-type]
    assert session and ok == {"signed_in": True}
    assert conn.sql("SET status = 'verified'")[0][1] == public.session_hash(session)


@pytest.mark.asyncio
async def test_the_view_shows_trips_without_names(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _view_conn()
    _patch(monkeypatch, conn)
    assert (await public.home(object(), object(), TOKEN, None))["signed_in"] is False  # type: ignore[arg-type]
    conn.on("FROM crm.sales_partner_otp WHERE link_id = $1 AND session_hash", uuid.uuid4())
    trip = uuid.uuid4()
    rows = [
        {"trip_id": trip, "stage": "pre_departure", "start_date": dt.date(2027, 4, 2), "month": None, "party_adults": 2,
         "children": 1, "created_at": dt.datetime(2026, 11, 3, tzinfo=dt.UTC), "amount": Decimal("120"),
         "currency": "USD", "commission_status": "approved", "due_on": dt.date(2027, 5, 1), "paid_on": None,
         "amount_base": Decimal("120"), "base_currency": "USD"},
        {"trip_id": uuid.uuid4(), "stage": "inquiry", "start_date": None, "month": dt.date(2027, 6, 1),
         "party_adults": 1, "children": 0, "created_at": dt.datetime(2026, 11, 4, tzinfo=dt.UTC), "amount": None,
         "currency": None, "commission_status": None, "due_on": None, "paid_on": None, "amount_base": None,
         "base_currency": None},
        {"trip_id": uuid.uuid4(), "stage": "completed", "start_date": None, "month": None, "party_adults": 2,
         "children": 0, "created_at": dt.datetime(2026, 1, 4, tzinfo=dt.UTC), "amount": Decimal("80"),
         "currency": "USD", "commission_status": "paid", "due_on": None, "paid_on": dt.date(2026, 6, 1),
         "amount_base": Decimal("80"), "base_currency": "USD"},
    ]  # fmt: skip
    conn.on("FROM crm.trip t LEFT JOIN LATERAL", rows)
    view = await public.home(object(), object(), TOKEN, "session")  # type: ignore[arg-type]
    assert view["partner_name"] == "Blue Kite" and view["agency"] == "Lotus Trails"
    first, second, third = view["trips"]
    assert first == {"ref": "T-" + trip.hex[:8].upper(), "stage": "booked", "start_month": "2027-04", "travellers": 3,
                     "received_on": "2026-11-03", "commission": {"amount": "120.00", "currency": "USD",
                     "status": "approved", "due_on": "2027-05-01", "paid_on": None}}  # fmt: skip
    assert second["stage"] == "enquiry" and second["start_month"] == "2027-06" and second["commission"] is None
    assert third["commission"]["paid_on"] == "2026-06-01" and third["start_month"] is None
    assert view["totals"] == [{"currency": "USD", "open": "120.00", "paid": "80.00"}]
    await public.sign_out(object(), TOKEN, "session")  # type: ignore[arg-type]
    assert conn.sql("SET status = 'expired' WHERE link_id = $1 AND session_hash")


# ── the code mail, the dispatcher's gate and the purge ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_partner_code_mail_goes_to_the_partner_at_send_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.mail import sender as sender_module

    monkeypatch.setattr(sender_module, "Vault", lambda keys, ctx: MailVault())
    otp = uuid.uuid4()
    masked = enc("message", "body_enc", f"Your code to open your partner page is {CODE_MASK}.")
    provider = FakeProvider()
    runtime, _, _ = _runtime(provider)
    conn = _conn(_message(body_enc=masked, traveler_id=None, sales_partner_id=PARTNER))
    conn.on(
        "FROM crm.sales_partner WHERE sales_partner_id", enc("sales_partner", "contact_email_enc", "desk@kite.example")
    )
    conn.on("FROM crm.sales_partner_otp", {"otp_id": otp, "code_enc": enc("sales_partner_otp", "code_enc", "135790"),
                                           "expired": False})  # fmt: skip
    assert await MailSender(runtime).send(system_ctx(conn), DISPATCH, "email", MESSAGE) == "sent"
    ((raw, _, recipients),) = provider.sent
    text = email.message_from_bytes(raw, policy=email.policy.default).get_body(("plain",)).get_content()
    assert "is 135790." in text and recipients == ["desk@kite.example"]
    assert conn.sql("UPDATE crm.sales_partner_otp SET code_enc = NULL") == [(otp,)]
    gone = _conn(_message(traveler_id=None, sales_partner_id=PARTNER))
    gone.on("FROM crm.sales_partner WHERE sales_partner_id", None)
    assert await MailSender(runtime).send(system_ctx(gone), DISPATCH, "email", MESSAGE) == "send_failed"
    assert gone.sql("SET status = 'failed'") == [(MESSAGE, "no_partner_address")]


def test_the_dispatcher_drops_a_mail_to_an_inactive_partner() -> None:
    row = {"sales_partner_id": PARTNER, "partner_active": True, "traveler_deleted_at": None}
    assert worker.gate(row) is None
    assert worker.gate({**row, "partner_active": False}) == "partner_inactive"
    assert worker.gate({**row, "partner_active": None}) == "partner_inactive"
