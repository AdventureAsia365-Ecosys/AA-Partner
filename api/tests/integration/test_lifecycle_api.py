"""AA-946 (card CRM-18) end to end on PostgreSQL 15 with the RDS role shape: the thin full lifecycle as crm_app through
``tenant_tx()`` and the signed-link contexts, with real field encryption.

- The happy path: terms + waiver published -> text proposal drafted, sent (price hash, link once) -> the client
  accepts on the link (typed name; a wrong price hash is 409 ``price_changed``) -> deposit + balance rows -> "mark
  paid" moves the trip to Booked (reason ``deposit_paid``) and invites every traveller -> each traveller signs in with
  an e-mail code (a wrong code counts its attempt), sends Stage 3 and signs the waiver with a fresh code -> s3 = 100
  -> booked -> pre_departure without an override.
- **R** (card): Stage 3 before the deposit -> 403 ``stage3_locked`` (and the signing the same).
- **E** (card): a travelling trip without a return date stays Travelling and gets one ``return_date_missing`` task;
  the date rule moves a booked trip with Stage 3 missing as ``date_rule_gate_unmet`` + an urgent task, one with
  Stage 3 complete as ``date_rule``; the T-21 tick and the 24-hour code purge.
- Code delivery through the agency's own mailbox (a queued ``crm.message`` + dispatch row), never without one; the
  stored message shows the code masked and the code waits sealed on its ``otp_challenge`` row (review S251); the real
  sender (``MailSender`` through the dispatcher, a fake provider) puts the code into the mail and clears the sealed
  copy, and a code superseded or past its 10 minutes is never sent (``code_expired``; the hourly run expires it).
- Re-review S251: the signature keeps ``code_delivery`` and the canonical evidence record, re-verifiable after the
  code purge; a signing code is refused while signing is locked.
- Review S251: a trip booked with the ``manual_booking`` override opens the portal without a CRM deposit; a deposit
  row is never void (409 ``deposit_not_voidable``).
- The gates in the API: proposal -> booked without the deposit is 409 ``gate_unmet``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app import stages, stages_job
from app.dispatch.worker import build_handler as build_dispatch
from app.errors import Conflict, Forbidden, Invalid
from app.jobs.queue import Job
from app.mail import mailbox as mailboxes
from app.mail.sender import MailSender
from app.mail.state import MailRuntime
from app.permissions import Action
from app.portal import access, progress
from app.portal import public as portal
from app.portal.codes import CODE_MASK
from app.proposals import agreements, service
from app.proposals import public as proposal_page
from app.settings import Settings
from app.tenancy import system_tx, tenant_tx
from app.travellers import service as travellers
from app.vault import Vault
from tests.integration.test_crm005_intake import _agency, _keys
from tests.unit.mail_fakes import FakeProvider

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ECHO = Settings(environment="test", db_ssl="disable", portal_otp_dev_echo=True)
PLAIN = Settings(environment="test", db_ssl="disable")
STAGE3 = {
    "given_name": "Grace",
    "family_name": "Lee",
    "dob": "1984-03-02",
    "nationality": "AU",
    "passport_no": "PA1234567",
    "passport_expiry": "2031-01-01",
    "passport_country": "AU",
    "emergency_contact": "Tom Lee +61 400 000 000",
}


def _token(path: str) -> str:
    return path.rsplit("/", 1)[1]


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    tenant_id, people = await _agency(pool, "owner", "agent", "assistant")
    who = {role: people[i][0] for i, role in enumerate(("owner", "agent", "assistant"))}
    members = {role: people[i][1] for i, role in enumerate(("owner", "agent", "assistant"))}
    keys = _keys()
    trip = uuid.uuid4()
    async with tenant_tx(pool, who["agent"], tenant_id) as ctx:
        vault = Vault(keys, ctx)
        lead = uuid.UUID(
            (
                await travellers.create_traveler(
                    ctx, vault, {"display_name": "Grace Lee", "email": "grace@example.test"}
                )
            )["traveler_id"]
        )
        await ctx.conn.execute(
            "INSERT INTO crm.trip (trip_id, title, owner_member_id, start_date, end_date, intake_completeness) "
            "VALUES ($1, 'Laos 10 nights', $2, current_date + 90, current_date + 100, "
            '\'{"s1": 100, "s2": 100, "s3": 0}\')',
            trip,
            members["agent"],
        )
        await ctx.conn.execute(
            "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, 'lead')", trip, lead
        )
        added = await travellers.add_to_trip(
            ctx, vault, trip, new={"display_name": "Tom Lee", "email": "tom@example.test"}
        )
        companion = uuid.UUID(added["traveler_id"])
        await stages.move(ctx, trip, "qualifying")
        await stages.move(ctx, trip, "proposal")
    async with tenant_tx(pool, who["owner"], tenant_id) as ctx:
        terms = await agreements.create(ctx, kind="booking_terms", version_label="v2026-10", body_text="Our terms.")
        waiver = await agreements.create(ctx, kind="waiver", version_label="v1", body_text="I travel at my own risk.")
        await agreements.publish(ctx, uuid.UUID(terms["doc_id"]))
        await agreements.publish(ctx, uuid.UUID(waiver["doc_id"]))
    return {
        "tenant": tenant_id,
        "who": who,
        "members": members,
        "keys": keys,
        "trip": trip,
        "lead": lead,
        "companion": companion,
        "terms": uuid.UUID(terms["doc_id"]),
        "waiver": uuid.UUID(waiver["doc_id"]),
    }


async def _proposal(pool: asyncpg.Pool, s: dict[str, Any], *, total: str = "1000", deposit: str = "300") -> dict:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        draft = await service.create_draft(
            ctx,
            Vault(s["keys"], ctx),
            s["trip"],
            body="Day 1 Luang Prabang. Day 2 Kuang Si.",
            currency="USD",
            total=Decimal(total),
            deposit=Decimal(deposit),
            balance_due_on=dt.date.today() + dt.timedelta(days=60) if deposit != total else None,
            terms_doc_id=s["terms"],
        )
        return await service.send(ctx, Vault(s["keys"], ctx), uuid.UUID(draft["proposal_id"]))


async def _accept(pool: asyncpg.Pool, s: dict[str, Any], sent: dict[str, Any]) -> dict[str, Any]:
    token = _token(sent["link_path"])
    page = await proposal_page.read(pool, s["keys"], token)
    return await proposal_page.accept(
        pool,
        s["keys"],
        PLAIN,
        token,
        {
            "full_name": "Grace Lee",
            "accept_terms": True,
            "price_hash": page["price_hash"],
            "terms_doc_id": page["terms"]["doc_id"],
        },
        origin=None,
    )


async def _schedule(pool: asyncpg.Pool, s: dict[str, Any]) -> dict[str, Any]:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        return await service.trip_overview(ctx, Vault(s["keys"], ctx), s["trip"])


async def _sign_in(pool: asyncpg.Pool, s: dict[str, Any], token: str) -> str:
    sent = await portal.request_code(pool, s["keys"], ECHO, token, purpose="sign_in", session=None, origin=None)
    session, answer = await portal.verify_code(pool, ECHO, token, sent["dev_code"], origin=None)
    assert session is not None and answer["signed_in"]
    return session


async def _sign(pool: asyncpg.Pool, s: dict[str, Any], token: str, session: str, name: str) -> tuple[bool, dict]:
    code = await portal.request_code(pool, s["keys"], ECHO, token, purpose="sign", session=session, origin=None)
    home = await portal.home(pool, token, session)
    return await portal.sign(
        pool,
        s["keys"],
        ECHO,
        token,
        session,
        {
            "code": code["dev_code"],
            "full_name": name,
            "agree": True,
            "doc_id": home["waiver"]["doc_id"],
            "doc_sha256": home["waiver"]["body_sha256"],
        },
        origin=None,
    )


async def test_the_thin_lifecycle_end_to_end(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    sent = await _proposal(app_pool, s)
    assert sent["status"] == "sent" and sent["link_path"].startswith("/c/p/") and len(sent["price_hash"]) == 64

    # The client page shows deposit and balance apart; a stale price hash is refused.
    token = _token(sent["link_path"])
    page = await proposal_page.read(app_pool, s["keys"], token)
    assert (page["total_amount"], page["deposit_amount"], page["balance_amount"]) == ("1000.00", "300.00", "700.00")
    assert page["body"].startswith("Day 1") and page["terms"]["version_label"] == "v2026-10"
    with pytest.raises(Conflict) as err:
        await proposal_page.accept(
            app_pool,
            s["keys"],
            PLAIN,
            token,
            {"full_name": "Grace Lee", "accept_terms": True, "price_hash": "0" * 64, "terms_doc_id": str(s["terms"])},
            origin=None,
        )
    assert err.value.code == "price_changed"
    with pytest.raises(Invalid):
        await proposal_page.accept(
            app_pool, s["keys"], PLAIN, token, {"full_name": " ", "price_hash": page["price_hash"]}, origin=None
        )
    accepted = await _accept(app_pool, s, sent)
    assert accepted["status"] == "accepted"
    assert (await _accept(app_pool, s, sent))["status"] == "already_accepted"  # a second device, a double click

    overview = await _schedule(app_pool, s)
    assert [(r["kind"], r["amount"], r["status"]) for r in overview["schedule"]] == [
        ("deposit", "300.00", "due"),
        ("balance", "700.00", "due"),
    ]
    assert overview["proposals"][0]["status"] == "accepted" and not overview["deposit_paid"]

    # The API gate: proposal -> booked without the deposit is 409 gate_unmet.
    with pytest.raises(Conflict) as err:
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
            await stages.move(ctx, s["trip"], "booked", vault=Vault(s["keys"], ctx))
    assert err.value.code == "gate_unmet" and err.value.extra["gate"] == "booking"

    # Portal before the deposit: sign-in works, Stage 3 and signing are locked (card R).
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        early = await access.invite(ctx, s["trip"], s["lead"])
    lead_token = _token(early["link_path"])
    assert (await portal.home(app_pool, lead_token, None)) == {"agency_name": "Sen Vàng Travel", "signed_in": False}
    code = await portal.request_code(
        app_pool, s["keys"], ECHO, lead_token, purpose="sign_in", session=None, origin=None
    )
    wrong = "000000" if code["dev_code"] != "000000" else "111111"
    session, refusal = await portal.verify_code(app_pool, ECHO, lead_token, wrong, origin=None)
    assert session is None and refusal == {"detail": "code_invalid", "attempts_left": 4}  # the attempt committed
    session, answer = await portal.verify_code(app_pool, ECHO, lead_token, code["dev_code"], origin=None)
    assert session and answer["signed_in"]
    home = await portal.home(app_pool, lead_token, session)
    assert home["stage3"]["locked"] and home["role"] == "lead" and home["payments"][0]["status"] == "due"
    with pytest.raises(Forbidden) as err:
        await portal.submit_stage3(app_pool, s["keys"], ECHO, lead_token, session, STAGE3, origin=None)
    assert err.value.code == "stage3_locked" and err.value.status_code == 403
    with pytest.raises(Forbidden) as err:
        await _sign(app_pool, s, lead_token, session, "Grace Lee")
    assert err.value.code == "stage3_locked"
    with pytest.raises(Forbidden) as err:  # no session: sign in first
        await portal.submit_stage3(app_pool, s["keys"], ECHO, lead_token, "not-a-session", STAGE3, origin=None)
    assert err.value.code == "portal_sign_in_required"

    # Mark the deposit paid: Booked, everyone invited (the lead's link re-issued), the delete block holds.
    deposit = next(r for r in overview["schedule"] if r["kind"] == "deposit")
    with pytest.raises(Forbidden):
        async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"], action=Action.PAYMENT_MARK_RECEIVED) as ctx:
            pass
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        paid = await service.mark_paid(
            ctx, Vault(s["keys"], ctx), uuid.UUID(deposit["schedule_id"]), received_on=None, method="bank_transfer"
        )
        assert await ctx.conn.fetchval("SELECT crm.trip_delete_blockers($1)", s["trip"]) == ["has_money"]
        history = await ctx.conn.fetchval(
            "SELECT reason_code FROM crm.trip_stage_history WHERE trip_id = $1 AND to_stage = 'booked'", s["trip"]
        )
    assert paid["trip_moved"]["stage"] == "booked" and history == "deposit_paid"
    assert paid["trip_moved"]["waiting_on"] == "client"  # Stage 3 not done yet (hook portal_stage3)
    links = {i["traveler_id"]: _token(i["link_path"]) for i in paid["portal"]["invited"]}
    assert set(links) == {str(s["lead"]), str(s["companion"])} and paid["portal"]["skipped"] == []
    with pytest.raises(Conflict) as err:
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
            await service.mark_paid(
                ctx, Vault(s["keys"], ctx), uuid.UUID(deposit["schedule_id"]), received_on=None, method="cash"
            )
    assert err.value.code == "schedule_not_due"

    # The old lead link was re-issued: the new one works, the old one is gone.
    lead_token, companion_token = links[str(s["lead"])], links[str(s["companion"])]
    with pytest.raises(Exception) as gone:
        await portal.home(app_pool, _token(early["link_path"]), session)
    assert getattr(gone.value, "code", None) == "link_not_found"

    # Each traveller: sign in, Stage 3, sign the waiver.
    lead_session = await _sign_in(app_pool, s, lead_token)
    done = await portal.submit_stage3(app_pool, s["keys"], ECHO, lead_token, lead_session, STAGE3, origin=None)
    assert done == {"status": "received", "missing": []}
    with pytest.raises(Invalid) as err:
        await portal.submit_stage3(
            app_pool, s["keys"], ECHO, lead_token, lead_session, {"dob": "2999-01-01"}, origin=None
        )
    assert err.value.extra["fields"] == ["dob"]
    signed, answer = await _sign(app_pool, s, lead_token, lead_session, "Grace Lee")
    assert signed and answer["status"] == "signed"
    # Re-review S251: the signature keeps the delivery and the exact hashed record (no PII), sealed; after the code
    # row is purged the hash still re-verifies and the delivery is still known.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        await ctx.conn.execute("DELETE FROM crm.otp_challenge WHERE traveler_id = $1 AND purpose = 'sign'", s["lead"])
        evidence = await ctx.conn.fetchrow(
            "SELECT otp_id, code_delivery, evidence_record, evidence_sha256 FROM crm.signature_request "
            "WHERE traveler_id = $1 AND status = 'signed'",
            s["lead"],
        )
    record = bytes(evidence["evidence_record"])
    assert evidence["otp_id"] is None and evidence["code_delivery"] == "dev_echo"
    assert hashlib.sha256(record).digest() == bytes(evidence["evidence_sha256"])
    parsed = json.loads(record)
    assert parsed["code_delivery"] == "dev_echo" and parsed["traveler_id"] == str(s["lead"])
    assert parsed["typed_name_sha256"] == hashlib.sha256(b"Grace Lee").hexdigest() and b"Grace" not in record
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        enc = await ctx.conn.fetchval("SELECT passport_no_enc FROM crm.traveler WHERE traveler_id = $1", s["lead"])
        assert b"PA1234567" not in bytes(enc)
        assert (await progress.compute(ctx, s["trip"])).s3 == 50
        blockers = await ctx.conn.fetchval("SELECT crm.trip_delete_blockers($1)", s["trip"])
        assert set(blockers) == {"has_money", "has_signature"}

    # The companion sees no payments and no party; signing with a stale document hash is refused.
    companion_session = await _sign_in(app_pool, s, companion_token)
    companion_home = await portal.home(app_pool, companion_token, companion_session)
    assert "payments" not in companion_home and "party" not in companion_home and not companion_home["stage3"]["locked"]
    await portal.submit_stage3(app_pool, s["keys"], ECHO, companion_token, companion_session,
                               {**STAGE3, "given_name": "Tom", "passport_no": "PA7654321"}, origin=None)  # fmt: skip
    code = await portal.request_code(
        app_pool, s["keys"], ECHO, companion_token, purpose="sign", session=companion_session, origin=None
    )
    with pytest.raises(Conflict) as err:
        await portal.sign(app_pool, s["keys"], ECHO, companion_token, companion_session,
                          {"code": code["dev_code"], "full_name": "Tom Lee", "agree": True,
                           "doc_id": str(s["waiver"]), "doc_sha256": "0" * 64}, origin=None)  # fmt: skip
    assert err.value.code == "document_changed"
    signed, _ = await _sign(app_pool, s, companion_token, companion_session, "Tom Lee")
    assert signed

    # s3 = 100 opens booked -> pre_departure without an override; the lead's home shows the party's progress.
    lead_home = await portal.home(app_pool, lead_token, lead_session)
    assert [(p["role"], p["details_done"], p["waiver_signed"]) for p in lead_home["party"]] == [
        ("lead", True, True),
        ("companion", True, True),
    ]
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        assert await ctx.conn.fetchval("SELECT waiting_on FROM crm.trip WHERE trip_id = $1", s["trip"]) != "client"
        moved = await stages.move(ctx, s["trip"], "pre_departure", vault=Vault(s["keys"], ctx))
    assert moved["stage"] == "pre_departure" and moved["override_reason_code"] is None

    # Sign out ends the session.
    await portal.sign_out(app_pool, lead_token, lead_session)
    assert (await portal.home(app_pool, lead_token, lead_session))["signed_in"] is False


async def test_codes_go_through_the_agencys_mailbox_or_not_at_all(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        token = _token((await access.invite(ctx, s["trip"], s["lead"]))["link_path"])
    with pytest.raises(Conflict) as err:
        await portal.request_code(app_pool, s["keys"], PLAIN, token, purpose="sign_in", session=None, origin=None)
    assert err.value.code == "code_delivery_unavailable"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.otp_challenge") == 0  # nothing stored
        await ctx.conn.execute(
            "INSERT INTO crm.channel_account (member_id, kind, address_enc, address_bidx, status) "
            "VALUES (NULL, 'smtp', $1, $2, 'needs_attention')",
            b"x" * 40,
            b"y" * 16,
        )
    sent = await portal.request_code(app_pool, s["keys"], PLAIN, token, purpose="sign_in", session=None, origin=None)
    assert sent == {"sent": True, "to_hint": "g***@e******.test", "expires_in_s": 600}
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        row = await ctx.conn.fetchrow(
            "SELECT m.status, m.sender_kind, q.origin, q.message_id = m.message_id AS linked FROM crm.message m "
            "JOIN crm.dispatch_queue q ON q.message_id = m.message_id"
        )
    assert dict(row) == {"status": "queued", "sender_kind": "agency_mailbox", "origin": "system", "linked": True}
    # Review S251: nobody at the agency reads the code in the stored mail; it waits sealed for the sender only.
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        vault = Vault(s["keys"], ctx)
        mail = await ctx.conn.fetchrow("SELECT message_id, body_enc FROM crm.message")
        body = await vault.decrypt("message", "body_enc", mail["message_id"], mail["body_enc"], purpose="mail")
        otp = await ctx.conn.fetchrow("SELECT otp_id, message_id, code_enc, delivery FROM crm.otp_challenge")
        code = await vault.decrypt("otp_challenge", "code_enc", otp["otp_id"], otp["code_enc"])
    assert CODE_MASK in body and code not in body and len(code) == 6
    assert otp["message_id"] == mail["message_id"] and otp["delivery"] == "mailbox"
    session, _ = await portal.verify_code(app_pool, PLAIN, token, code, origin=None)
    assert session is not None
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        left = await ctx.conn.fetchval("SELECT code_enc FROM crm.otp_challenge WHERE otp_id = $1", otp["otp_id"])
    assert left is None
    # A signing code needs a session; five codes an hour per traveller at most.
    with pytest.raises(Forbidden):
        await portal.request_code(app_pool, s["keys"], PLAIN, token, purpose="sign", session=None, origin=None)
    for _ in range(4):
        await portal.request_code(app_pool, s["keys"], PLAIN, token, purpose="sign_in", session=None, origin=None)
    with pytest.raises(Exception) as err:
        await portal.request_code(app_pool, s["keys"], PLAIN, token, purpose="sign_in", session=None, origin=None)
    assert getattr(err.value, "code", None) == "too_many_codes"


async def _dispatch_code_mail(pool: asyncpg.Pool, s: dict[str, Any], otp_id: uuid.UUID, provider: FakeProvider) -> dict:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        dispatch_id = await ctx.conn.fetchval(
            "SELECT q.dispatch_id FROM crm.dispatch_queue q JOIN crm.otp_challenge o ON o.message_id = q.message_id "
            "WHERE o.otp_id = $1",
            otp_id,
        )
    job = Job("j", "crm_dispatch", {"tenant_id": str(s["tenant"]), "dispatch_id": str(dispatch_id)}, 1, 5)
    return await build_dispatch(MailSender(MailRuntime(keys=s["keys"], providers={"gmail": provider})))(pool, job)


async def _codes(pool: asyncpg.Pool, s: dict[str, Any]) -> list[Any]:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        return await ctx.conn.fetch(
            "SELECT o.otp_id, o.status, o.code_enc, o.code_hash, m.status AS mail_status, m.body_enc, m.message_id "
            "FROM crm.otp_challenge o JOIN crm.message m ON m.message_id = o.message_id ORDER BY o.created_at"
        )


async def test_the_mail_sender_fills_in_the_code_and_clears_it(app_pool: asyncpg.Pool) -> None:
    """Review nit S251: MailSender on PostgreSQL with the real vault for a queued code mail."""
    s = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"], action=Action.OWN_MAILBOX_CONNECT) as ctx:
        await mailboxes.connect(
            ctx,
            Vault(s["keys"], ctx),
            scope="own",
            provider="google",
            secret={"refresh_token": "rt", "address": "agent@senvang.example"},
        )
        token = _token((await access.invite(ctx, s["trip"], s["lead"]))["link_path"])
    sent = await portal.request_code(app_pool, s["keys"], PLAIN, token, purpose="sign_in", session=None, origin=None)
    assert sent["sent"] is True
    (first,) = await _codes(app_pool, s)
    provider = FakeProvider()
    assert (await _dispatch_code_mail(app_pool, s, first["otp_id"], provider))["status"] == "sent"
    raw = provider.sent[-1][0].decode()
    assert CODE_MASK not in raw
    stored_hash = bytes(first["code_hash"])
    code = next(c for c in re.findall(r"\b\d{6}\b", raw) if portal.code_hash(first["otp_id"], c) == stored_hash)
    (after,) = await _codes(app_pool, s)
    assert after["mail_status"] == "sent" and after["code_enc"] is None and after["status"] == "pending"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        body = await Vault(s["keys"], ctx).decrypt(
            "message", "body_enc", after["message_id"], after["body_enc"], purpose="mail"
        )
    assert CODE_MASK in body and code not in body  # the stored mail still shows the code masked
    session, _ = await portal.verify_code(app_pool, PLAIN, token, code, origin=None)
    assert session is not None

    # A superseded code is never sent: the next request expires it (and clears its sealed copy) before its mail goes.
    await portal.request_code(app_pool, s["keys"], PLAIN, token, purpose="sign_in", session=None, origin=None)
    await portal.request_code(app_pool, s["keys"], PLAIN, token, purpose="sign_in", session=None, origin=None)
    _, superseded, latest = await _codes(app_pool, s)
    assert superseded["status"] == "expired" and superseded["code_enc"] is None
    held = len(provider.sent)
    answer = await _dispatch_code_mail(app_pool, s, superseded["otp_id"], provider)
    assert answer.get("status") != "sent" and len(provider.sent) == held
    assert (await _codes(app_pool, s))[1]["mail_status"] == "failed"

    # A code whose 10 minutes ran out while its mail waited: the hourly run expires it and drops the sealed copy;
    # its mail then fails as code_expired.
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(s["tenant"]))
        await conn.execute(
            "UPDATE crm.otp_challenge SET created_at = now() - interval '20 minutes', "
            "expires_at = now() - interval '10 minutes' WHERE otp_id = $1",
            latest["otp_id"],
        )
    counts = await _date_rule(app_pool, s["tenant"])
    assert counts["codes_expired"] == 1
    *_, stale = await _codes(app_pool, s)
    assert stale["status"] == "expired" and stale["code_enc"] is None
    answer = await _dispatch_code_mail(app_pool, s, latest["otp_id"], provider)
    assert answer.get("status") != "sent" and len(provider.sent) == held


async def test_a_signing_code_waits_for_the_unlock(app_pool: asyncpg.Pool) -> None:
    """Re-review S251: a sign code before the portal unlocks spends neither a mail nor one of the hourly codes."""
    s = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        token = _token((await access.invite(ctx, s["trip"], s["lead"]))["link_path"])
    session = await _sign_in(app_pool, s, token)
    with pytest.raises(Forbidden) as err:
        await portal.request_code(app_pool, s["keys"], ECHO, token, purpose="sign", session=session, origin=None)
    assert err.value.code == "stage3_locked"
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.otp_challenge WHERE purpose = 'sign'") == 0


async def test_five_wrong_codes_fail_the_code(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        token = _token((await access.invite(ctx, s["trip"], s["lead"]))["link_path"])
    code = (await portal.request_code(app_pool, s["keys"], ECHO, token, purpose="sign_in", session=None, origin=None))[
        "dev_code"
    ]
    wrong = "000000" if code != "000000" else "111111"
    for left in (4, 3, 2, 1, 0):
        _, refusal = await portal.verify_code(app_pool, ECHO, token, wrong, origin=None)
        assert refusal["attempts_left"] == left
    session, refusal = await portal.verify_code(app_pool, ECHO, token, code, origin=None)
    assert session is None and refusal["detail"] == "code_invalid"  # the code failed: the right one is no use now


async def test_proposal_rules(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        vault = Vault(s["keys"], ctx)
        for kwargs, code in (
            ({"total": Decimal("100"), "deposit": Decimal("200"), "balance_due_on": None}, "deposit_above_total"),
            ({"total": Decimal("100"), "deposit": Decimal("50"), "balance_due_on": None}, "balance_due_on_required"),
            (
                {"total": Decimal("100"), "deposit": Decimal("100"), "balance_due_on": dt.date.today()},
                "balance_due_on_not_allowed",
            ),
        ):
            with pytest.raises(Invalid) as err:
                await service.create_draft(ctx, vault, s["trip"], body="x", currency="USD", terms_doc_id=None, **kwargs)
            assert err.value.code == code
    first = await _proposal(app_pool, s)
    second = await _proposal(app_pool, s, total="900", deposit="900")
    assert second["superseded"] == first["proposal_id"]
    with pytest.raises(Exception) as gone:  # the superseded version's link is revoked
        await proposal_page.read(app_pool, s["keys"], _token(first["link_path"]))
    assert getattr(gone.value, "code", None) == "link_not_found"
    await _accept(app_pool, s, second)
    overview = await _schedule(app_pool, s)
    assert [r["kind"] for r in overview["schedule"]] == ["deposit"]  # paid in full by the deposit
    with pytest.raises(Conflict) as err:  # review S251: a deposit row is never void
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
            await service.void(ctx, uuid.UUID(overview["schedule"][0]["schedule_id"]))
    assert err.value.code == "deposit_not_voidable"
    with pytest.raises(Conflict) as err:
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
            await service.create_draft(ctx, Vault(s["keys"], ctx), s["trip"], body="v3", currency="USD",
                                       total=Decimal("1"), deposit=Decimal("1"), balance_due_on=None,
                                       terms_doc_id=None)  # fmt: skip
    assert err.value.code == "proposal_already_accepted"


async def test_a_manual_booking_opens_the_portal(app_pool: asyncpg.Pool) -> None:
    """Review S251: the deposit was taken outside the CRM and the trip booked with the manual_booking override (no
    proposal at all): Stage 3 and the waiver open (the undone override: test_crm016_schema)."""
    s = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        token = _token((await access.invite(ctx, s["trip"], s["lead"]))["link_path"])
    session = await _sign_in(app_pool, s, token)
    assert (await portal.home(app_pool, token, session))["stage3"]["locked"]
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        vault = Vault(s["keys"], ctx)
        moved = await stages.move(ctx, s["trip"], "booked", vault=vault, override_reason="manual_booking")
    assert moved["override_reason_code"] == "manual_booking"
    assert not (await portal.home(app_pool, token, session))["stage3"]["locked"]
    assert (await _schedule(app_pool, s))["portal_unlocked"]
    done = await portal.submit_stage3(app_pool, s["keys"], ECHO, token, session, STAGE3, origin=None)
    assert done["status"] == "received"
    signed, _ = await _sign(app_pool, s, token, session, "Grace Lee")
    assert signed


async def test_a_published_agreement_never_changes(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:
        with pytest.raises(Conflict) as err:
            await agreements.update(ctx, s["terms"], {"body_text": "changed"})
        assert err.value.code == "agreement_not_draft"
        v2 = await agreements.create(ctx, kind="booking_terms", version_label="v2026-11", body_text="New terms.")
        await agreements.publish(ctx, uuid.UUID(v2["doc_id"]))
        listed = {d["version_label"]: d["status"] for d in (await agreements.list_docs(ctx))["agreements"]}
    assert listed["v2026-10"] == "retired" and listed["v2026-11"] == "published"


async def _date_rule(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> dict[str, int]:
    async with system_tx(pool, tenant_id, actor=stages_job.ACTOR) as ctx:
        return dict(await stages_job.run_agency(ctx))


async def _trip(pool: asyncpg.Pool, s: dict[str, Any], stage: str, start: int, end: int | None, s3: int) -> uuid.UUID:
    trip = uuid.uuid4()
    async with system_tx(pool, s["tenant"], actor="seed:test") as ctx:
        await ctx.conn.execute(
            "INSERT INTO crm.trip (trip_id, title, stage, start_date, end_date, intake_completeness) "
            "VALUES ($1, 'Dated', $2, current_date + $3::int, current_date + $4::int, "
            "jsonb_build_object('s1', 100, 's2', 100, 's3', $5::int))",
            trip,
            stage,
            start,
            end,
            s3,
        )
        await ctx.conn.execute(
            "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, 'lead')", trip, s["lead"]
        )
    return trip


async def test_e_the_date_ticks(app_pool: asyncpg.Pool) -> None:
    s = await _setup(app_pool)
    no_return = await _trip(app_pool, s, "travelling", -3, None, 0)
    missing = await _trip(app_pool, s, "booked", -1, 9, 0)
    t21 = await _trip(app_pool, s, "booked", 20, 30, 0)
    # A booked trip whose lead finished Stage 3 and signed: the lead of the main trip has done both below.
    complete = await _trip(app_pool, s, "booked", 0, 9, 0)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        vault = Vault(s["keys"], ctx)
        await travellers.update_traveler(ctx, vault, s["lead"], STAGE3)
        access_id = uuid.UUID((await access.invite(ctx, complete, s["lead"]))["access_id"])
        await ctx.conn.execute(
            "UPDATE crm.signature_request SET status = 'signed', signed_at = now(), signed_name_enc = $2, "
            "doc_sha256 = $3, evidence_sha256 = $4, evidence_record = $5, code_delivery = 'mailbox' "
            "WHERE access_id = $1",
            access_id,
            b"z" * 40,
            b"h" * 32,
            hashlib.sha256(b"{}").digest(),
            b"{}",
        )
        await ctx.conn.execute(
            "INSERT INTO crm.otp_challenge (access_id, trip_id, traveler_id, purpose, code_hash, expires_at, "
            "created_at) VALUES ($1, $2, $3, 'sign_in', $4, now() - interval '25 hours' + interval '5 minutes', "
            "now() - interval '25 hours')",
            access_id,
            complete,
            s["lead"],
            b"c" * 32,
        )
    counts = await _date_rule(app_pool, s["tenant"])
    assert counts["booked_to_travelling"] == 2 and counts["urgent_tasks"] == 1
    assert counts["return_date_missing"] >= 1 and counts["stage3_due_t21"] == 1 and counts["codes_purged"] == 1
    assert counts["codes_expired"] == 1  # the old pending code expired first (code_enc cleared), then purged
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:

        async def stage_and_reason(trip: uuid.UUID) -> tuple[str, str | None]:
            return (
                await ctx.conn.fetchval("SELECT stage FROM crm.trip WHERE trip_id = $1", trip),
                await ctx.conn.fetchval(
                    "SELECT reason_code FROM crm.trip_stage_history WHERE trip_id = $1 ORDER BY at DESC LIMIT 1", trip
                ),
            )

        async def tasks(trip: uuid.UUID) -> list[tuple[str, str]]:
            rows = await ctx.conn.fetch("SELECT kind, priority FROM crm.task WHERE trip_id = $1 ORDER BY kind", trip)
            return [(r["kind"], r["priority"]) for r in rows]

        # E (card): no return date -> stays Travelling + one task.
        assert await stage_and_reason(no_return) == ("travelling", None)
        assert await tasks(no_return) == [("return_date_missing", "normal")]
        assert await stage_and_reason(missing) == ("travelling", "date_rule_gate_unmet")
        assert ("date_rule_gate_unmet", "urgent") in await tasks(missing)
        assert await stage_and_reason(complete) == ("travelling", "date_rule")
        assert await tasks(complete) == []
        assert await tasks(t21) == [("stage3_due_t21", "normal")]
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.otp_challenge") == 0
    # The ticks run once: a second pass adds nothing.
    again = await _date_rule(app_pool, s["tenant"])
    assert again.get("return_date_missing", 0) == 0 and again.get("stage3_due_t21", 0) == 0
