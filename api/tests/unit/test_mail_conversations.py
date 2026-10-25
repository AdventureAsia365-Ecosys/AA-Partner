"""AA-945 conversations, drafts, send / Undo, saved replies, mailbox settings and the OAuth state (scripted SQL)."""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any

import asyncpg
import pytest

from app.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from app.mail import conversations, mailbox, oauth
from app.mail.providers import Rejected
from app.mail.providers.oauth_mail import GOOGLE, OAuthMailProvider
from app.settings import Settings
from tests.unit.fakes import ScriptedConn
from tests.unit.mail_fakes import MailVault
from tests.unit.slice_fakes import MEMBER, OTHER_MEMBER, enc, tenant_ctx

CONV = uuid.UUID("00000000-0000-4000-8000-00000000e001")
TRIP = uuid.UUID("00000000-0000-4000-8000-00000000e002")
CLIENT = uuid.UUID("00000000-0000-4000-8000-00000000e003")
DRAFT = uuid.UUID("00000000-0000-4000-8000-00000000e004")
MAILBOX = uuid.UUID("00000000-0000-4000-8000-00000000e005")
NOW = __import__("datetime").datetime(2026, 10, 25, tzinfo=__import__("datetime").UTC)


def _conv(**over: Any) -> dict[str, Any]:
    return {
        "conversation_id": CONV,
        "trip_id": TRIP,
        "traveler_id": CLIENT,
        "channel": "email",
        "last_message_at": NOW,
    } | over


@pytest.mark.asyncio
async def test_lists_carry_ids_and_counts_only() -> None:
    row = _conv() | {"message_count": 3, "held_count": 1, "last_direction": "in"}
    conn = ScriptedConn().on("FROM crm.conversation c WHERE c.deleted_at IS NULL", [row])
    ctx = tenant_ctx(conn)
    out = await conversations.list_conversations(ctx, trip_id=TRIP, limit=500)
    assert out["conversations"][0]["held_count"] == 1 and not out["conversations"][0]["unmatched"]
    assert conn.calls[-1][1][3] == conversations.MAX_LIST
    assert ctx.audit_events[0].action == "list_view"


@pytest.mark.asyncio
async def test_a_thread_is_decrypted_and_viewed_once() -> None:
    message = {
        "message_id": DRAFT,
        "direction": "in",
        "status": "received",
        "sender_kind": None,
        "author_member_id": None,
        "subject_enc": enc("message", "subject_enc", "Hoi An"),
        "body_enc": enc("message", "body_enc", "Hello"),
        "body_html_enc": None,
        "from_enc": enc("message", "from_enc", "lan@example.test"),
        "to_enc": enc("message", "to_enc", json.dumps(["a@b.test"])),
        "attachments": [],
        "attachment_notes": ["too_large"],
        "failure_code": None,
        "imported": False,
        "match_method": "sender",
        "occurred_at": NOW,
        "sent_at": None,
        "updated_at": NOW,
    }
    conn = (
        ScriptedConn()
        .on("FROM crm.conversation WHERE conversation_id", _conv())
        .on("FROM crm.message WHERE", [message])
    )
    ctx = tenant_ctx(conn)
    out = await conversations.conversation_messages(ctx, MailVault(), CONV)
    (m,) = out["messages"]
    assert (m["subject"], m["text"], m["from"], m["to"], m["html"]) == (
        "Hoi An",
        "Hello",
        "lan@example.test",
        ["a@b.test"],
        None,
    )
    assert [e.action for e in ctx.audit_events] == ["view"]
    with pytest.raises(NotFound):
        await conversations.conversation_messages(tenant_ctx(), MailVault(), CONV)


@pytest.mark.asyncio
async def test_link_an_unmatched_conversation() -> None:
    conn = ScriptedConn().on("FROM crm.conversation WHERE conversation_id", _conv(traveler_id=None, trip_id=None))
    conn.on("FROM crm.traveler WHERE traveler_id", 1).on("FROM crm.trip WHERE trip_id", {"owner_member_id": MEMBER})
    out = await conversations.link(tenant_ctx(conn), CONV, CLIENT, TRIP)
    assert out["traveler_id"] == str(CLIENT) and conn.sql("UPDATE crm.conversation SET traveler_id")
    linked = ScriptedConn().on("FROM crm.conversation WHERE conversation_id", _conv(traveler_id=uuid.uuid4()))
    with pytest.raises(Conflict):
        await conversations.link(tenant_ctx(linked), CONV, CLIENT, None)
    with pytest.raises(Conflict):
        await conversations.link(
            tenant_ctx(ScriptedConn().on("FROM crm.conversation WHERE", _conv())), CONV, CLIENT, uuid.uuid4()
        )
    missing = ScriptedConn().on("FROM crm.conversation WHERE conversation_id", _conv(traveler_id=None))
    with pytest.raises(NotFound):
        await conversations.link(tenant_ctx(missing), CONV, CLIENT, None)


def _draft_conn(draft: Any = None, *, mailbox_id: Any = MAILBOX, owner: Any = MEMBER) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM crm.conversation WHERE conversation_id", _conv())
    conn.on("FROM crm.channel_account WHERE deleted_at IS NULL", mailbox_id)
    conn.on("FROM crm.message WHERE message_id = $1 FOR UPDATE", draft)
    conn.on("INSERT INTO crm.message", NOW)
    conn.on("UPDATE crm.message SET subject_enc", NOW)
    conn.on("FROM crm.trip WHERE trip_id", {"owner_member_id": owner})
    conn.on("FROM crm.trip_traveler WHERE trip_id", CLIENT)
    conn.on("SELECT conversation_id FROM crm.conversation WHERE channel", CONV)
    conn.on("FROM crm.traveler WHERE traveler_id", {"archived_at": None, "deleted_at": None})
    conn.on("INSERT INTO crm.dispatch_queue", {"dispatch_id": uuid.uuid4(), "not_before": NOW})
    return conn


def _draft_row(**over: Any) -> dict[str, Any]:
    return {
        "message_id": DRAFT,
        "conversation_id": CONV,
        "status": "draft_autosaved",
        "author_member_id": MEMBER,
        "channel_account_id": MAILBOX,
    } | over


@pytest.mark.asyncio
async def test_draft_autosave_is_idempotent_and_owned() -> None:
    conn = _draft_conn()
    out = await conversations.save_draft(
        tenant_ctx(conn), MailVault(), DRAFT, conversation_id=None, trip_id=TRIP, subject="Plan", body="Dear Lan"
    )
    assert out["status"] == "draft_autosaved" and out["conversation_id"] == str(CONV)
    (ins,) = conn.sql("INSERT INTO crm.message")
    assert ins[2] == MAILBOX and ins[4].startswith(b"enc:message.subject_enc:Plan")
    again = _draft_conn(_draft_row())
    await conversations.save_draft(
        tenant_ctx(again), MailVault(), DRAFT, conversation_id=None, trip_id=None, subject="Plan 2", body="Dear Lan"
    )
    assert again.sql("UPDATE crm.message SET subject_enc")
    others = _draft_conn(_draft_row(author_member_id=OTHER_MEMBER))
    with pytest.raises(Conflict):
        await conversations.save_draft(
            tenant_ctx(others), MailVault(), DRAFT, conversation_id=None, trip_id=None, subject="x", body="y"
        )
    with pytest.raises(Invalid):
        await conversations.save_draft(
            tenant_ctx(_draft_conn()), MailVault(), DRAFT, conversation_id=None, trip_id=None, subject="x", body="y"
        )
    with pytest.raises(Invalid):
        await conversations.save_draft(
            tenant_ctx(_draft_conn()),
            MailVault(),
            DRAFT,
            conversation_id=CONV,
            trip_id=None,
            subject="x" * 400,
            body="y",
        )
    assert (await conversations.discard_draft(tenant_ctx(_draft_conn(_draft_row())), DRAFT))["status"] == "discarded"
    with pytest.raises(NotFound):
        await conversations.discard_draft(tenant_ctx(_draft_conn()), DRAFT)
    with pytest.raises(Conflict):
        await conversations.discard_draft(tenant_ctx(_draft_conn(_draft_row(status="sent"))), DRAFT)


@pytest.mark.asyncio
async def test_send_queues_one_dispatch_row_with_the_undo_delay() -> None:
    conn = _draft_conn(_draft_row())
    ctx = tenant_ctx(conn, role="assistant")
    out = await conversations.send(ctx, MailVault(), DRAFT, conversation_id=CONV, trip_id=None, subject="s", body="b")
    assert out["status"] == "queued" and conn.sql("UPDATE crm.message SET status = 'queued'")
    (row,) = conn.sql("INSERT INTO crm.dispatch_queue")
    assert row[-1] == DRAFT and row[-2] == 10  # message_id, Undo delay
    # The assistant on someone else's trip: refused (MESSAGE_SEND is ASSIGNED for the assistant).
    with pytest.raises(Forbidden):
        await conversations.send(
            tenant_ctx(_draft_conn(_draft_row(), owner=OTHER_MEMBER), role="assistant"),
            MailVault(),
            DRAFT,
            conversation_id=CONV,
            trip_id=None,
            subject="s",
            body="b",
        )
    with pytest.raises(Invalid):
        await conversations.send(
            tenant_ctx(conn), MailVault(), DRAFT, conversation_id=CONV, trip_id=None, subject="", body="  "
        )
    unmatched = _draft_conn(_draft_row()).on("FROM crm.conversation WHERE conversation_id", _conv(traveler_id=None))
    with pytest.raises(Conflict, match="link"):
        await conversations.send(
            tenant_ctx(unmatched), MailVault(), DRAFT, conversation_id=CONV, trip_id=None, subject="s", body="b"
        )
    no_box = _draft_conn(_draft_row(channel_account_id=None), mailbox_id=None)
    with pytest.raises(Conflict, match="Mailbox"):
        await conversations.send(
            tenant_ctx(no_box), MailVault(), DRAFT, conversation_id=CONV, trip_id=None, subject="s", body="b"
        )


@pytest.mark.asyncio
async def test_undo_and_back_to_draft() -> None:
    conn = _draft_conn(_draft_row(status="queued"))
    conn.on("SELECT dispatch_id FROM crm.dispatch_queue WHERE message_id", uuid.uuid4())
    conn.on("UPDATE crm.dispatch_queue SET status = 'cancelled'", uuid.uuid4())
    assert (await conversations.undo(tenant_ctx(conn), DRAFT))["status"] == "draft_autosaved"
    with pytest.raises(Conflict):
        await conversations.undo(tenant_ctx(_draft_conn(_draft_row(status="sent"))), DRAFT)
    with pytest.raises(Forbidden):
        await conversations.undo(tenant_ctx(_draft_conn(_draft_row(author_member_id=OTHER_MEMBER))), DRAFT)
    with pytest.raises(NotFound):
        await conversations.undo(tenant_ctx(_draft_conn()), DRAFT)
    held = _draft_conn(_draft_row(status="held_reconnect"))
    assert (await conversations.back_to_draft(tenant_ctx(held), DRAFT))["status"] == "draft_autosaved"
    with pytest.raises(Conflict):
        await conversations.back_to_draft(tenant_ctx(_draft_conn(_draft_row(status="sent"))), DRAFT)
    with pytest.raises(NotFound):
        await conversations.back_to_draft(tenant_ctx(_draft_conn()), DRAFT)


@pytest.mark.asyncio
async def test_saved_replies() -> None:
    row = {
        "saved_reply_id": DRAFT,
        "member_id": None,
        "locale": "vi",
        "title": "Visa",
        "body_enc": enc("saved_reply", "body_enc", "Xin chào"),
        "updated_at": NOW,
    }
    conn = ScriptedConn().on("FROM crm.saved_reply WHERE deleted_at IS NULL", [row])
    assert (await conversations.saved_replies(tenant_ctx(conn), MailVault()))["saved_replies"][0]["body"] == "Xin chào"
    await conversations.create_reply(tenant_ctx(conn), MailVault(), scope="own", locale="en", title=" T ", body="B")
    assert conn.sql("INSERT INTO crm.saved_reply")[0][1] == MEMBER
    with pytest.raises(Forbidden):
        await conversations.create_reply(
            tenant_ctx(conn, role="agent"), MailVault(), scope="agency", locale="en", title="T", body="B"
        )
    agency = ScriptedConn().on(
        "FROM crm.saved_reply WHERE saved_reply_id", {"saved_reply_id": DRAFT, "member_id": None}
    )
    await conversations.update_reply(tenant_ctx(agency), MailVault(), DRAFT, locale=None, title="New", body="Body")
    with pytest.raises(Forbidden):
        await conversations.delete_reply(tenant_ctx(agency, role="agent"), DRAFT)
    others = ScriptedConn().on(
        "FROM crm.saved_reply WHERE saved_reply_id", {"saved_reply_id": DRAFT, "member_id": OTHER_MEMBER}
    )
    with pytest.raises(NotFound):
        await conversations.delete_reply(tenant_ctx(others), DRAFT)
    with pytest.raises(NotFound):
        await conversations.delete_reply(tenant_ctx(), DRAFT)
    assert (await conversations.delete_reply(tenant_ctx(agency), DRAFT))["status"] == "deleted"


@pytest.mark.asyncio
async def test_mailbox_connect_view_disconnect_and_ingest_address() -> None:
    conn = ScriptedConn()
    ctx = tenant_ctx(conn)
    out = await mailbox.connect(
        ctx,
        MailVault(),
        scope="own",
        provider="google",
        secret={"refresh_token": "rt", "address": "Hello@Sunshine.Example"},
    )
    assert out["status"] == "connected"
    (cred,) = conn.sql("INSERT INTO crm.integration_credential")
    assert cred[1:3] == ("google", MEMBER) and cred[3].startswith(b"secret:")
    (box,) = conn.sql("INSERT INTO crm.channel_account")
    assert box[2] == "gmail" and box[4] == b"e:hello@sunshine.example"  # the traveller e-mail blind index
    assert conn.sql("DELETE FROM crm.integration_credential")[0][1] == ["microsoft", "smtp"]
    again = ScriptedConn().on("SELECT integration_credential_id FROM", uuid.uuid4())
    again.on("SELECT channel_account_id, credential_id FROM", {"channel_account_id": MAILBOX, "credential_id": None})
    await mailbox.connect(
        tenant_ctx(again),
        MailVault(),
        scope="agency",
        provider="smtp",
        secret={"address": "a@b.test", "host": "h", "port": 587},
    )
    assert again.sql("UPDATE crm.channel_account SET kind")[0][1] == "smtp"
    clash = ScriptedConn().on("INSERT INTO crm.channel_account", _pg("CRM30"))
    with pytest.raises(Conflict, match="mismatch|mailbox"):
        await mailbox.connect(
            tenant_ctx(clash),
            MailVault(),
            scope="own",
            provider="google",
            secret={"refresh_token": "rt", "address": "a@b.test"},
        )
    view_row = {
        "channel_account_id": MAILBOX,
        "member_id": None,
        "kind": "smtp",
        "address_enc": enc("channel_account", "address_enc", "a@b.test"),
        "status": "disconnected",
        "last_token_check_at": NOW,
        "credential_status": None,
        "failure_class": "auth_revoked",
        "last_ok_at": None,
        "last_test_at": None,
        "last_test_result": None,
    }
    view_conn = ScriptedConn().on("FROM crm.channel_account a LEFT JOIN", [view_row])
    view_conn.on("SELECT channel_account_id, count(*)", [{"channel_account_id": MAILBOX, "n": 2}])
    view = await mailbox.view(tenant_ctx(view_conn), MailVault(), Settings(environment="test", db_ssl="disable"))
    assert view["mailboxes"][0] | {} == view["mailboxes"][0] and view["mailboxes"][0]["held_count"] == 2
    assert view["providers"] == {"google": False, "microsoft": False, "smtp": True}
    box_conn = ScriptedConn().on(
        "SELECT channel_account_id, credential_id FROM", {"channel_account_id": MAILBOX, "credential_id": uuid.uuid4()}
    )
    assert (await mailbox.disconnect(tenant_ctx(box_conn), "own"))["status"] == "disconnected"
    assert box_conn.sql("DELETE FROM crm.integration_credential")
    assert await mailbox.mailbox_id(tenant_ctx(box_conn), "own") == MAILBOX
    with pytest.raises(NotFound):
        await mailbox.disconnect(tenant_ctx(), "own")
    with pytest.raises(NotFound):
        await mailbox.mailbox_id(tenant_ctx(), "agency")
    settings = Settings(environment="test", db_ssl="disable", crm_ingest_domain="crm-in.example.test")
    created = {
        "ingest_address_id": MAILBOX,
        "local_part": "sunshine-travel-ab12cd",
        "forwarding_confirmation_enc": enc("ingest_address", "x", "123456789"),
        "forwarding_code_at": NOW,
    }
    rows = iter([None, created])
    ing = ScriptedConn().on("FROM crm.ingest_address WHERE active", lambda: next(rows))
    ing.on("SELECT display_name FROM crm.tenant", "Sunshine Travel Đà Lạt")
    out = await mailbox.ingest_address(tenant_ctx(ing), MailVault(), settings)
    assert out["address"] == "sunshine-travel-ab12cd@crm-in.example.test" and out["forwarding_code"] == "123456789"
    assert ing.sql("INSERT INTO crm.ingest_address")[0][0].startswith("sunshine-travel-da-lat-")
    assert mailbox.slug("!!!") == "agency"


def _pg(sqlstate: str) -> asyncpg.PostgresError:
    exc = asyncpg.PostgresError("refused")
    exc.sqlstate = sqlstate  # type: ignore[attr-defined]
    return exc


def _oauth_settings(**over: Any) -> Settings:
    base: dict[str, Any] = dict(
        environment="test",
        db_ssl="disable",
        mail_oauth_redirect_base="https://partner.test",
        mail_oauth=(("google_client_id", "gid"), ("google_client_secret", "gsecret")),
    )
    return Settings(**(base | over))


class _Exchange:
    async def exchange_code(self, code: str, verifier: str, redirect: str) -> dict[str, str]:
        assert redirect == "https://partner.test/connect/mail/callback" and len(verifier) > 40
        return {"refresh_token": "rt", "address": "a@b.test"}


@pytest.mark.asyncio
async def test_oauth_state_is_sealed_bound_and_expires() -> None:
    ctx = tenant_ctx()
    vault = MailVault()
    started = await oauth.start(ctx, vault, _oauth_settings(), "google", "own")
    url = started["authorize_url"]
    assert url.startswith(GOOGLE.authorize_url) and "code_challenge_method=S256" in url and "gmail.send" in url
    state = url.split("state=")[1].split("&")[0]
    done = await oauth.complete(ctx, vault, _oauth_settings(), {"gmail": _Exchange()}, "code", state)
    assert (done.provider, done.scope, done.secret["address"]) == ("google", "own", "a@b.test")
    # Another member's state does not pass.
    with pytest.raises(Invalid):
        await oauth.complete(
            tenant_ctx(member=OTHER_MEMBER), vault, _oauth_settings(), {"gmail": _Exchange()}, "c", state
        )
    payload = {"p": "google", "s": "own", "m": str(MEMBER), "e": int(time.time()) - 1, "v": "v"}
    expired = base64.urlsafe_b64encode(f"enc:oauth_state.state:{json.dumps(payload)}".encode()).decode()
    with pytest.raises(Conflict, match="again"):
        await oauth.complete(ctx, vault, _oauth_settings(), {"gmail": _Exchange()}, "c", expired)
    with pytest.raises(Invalid):
        await oauth.complete(ctx, vault, _oauth_settings(), {}, "", state)
    with pytest.raises(Unavailable):
        await oauth.start(ctx, vault, _oauth_settings(mail_oauth=()), "google", "own")
    with pytest.raises(Invalid):
        await oauth.start(ctx, vault, _oauth_settings(), "yahoo", "own")
    with pytest.raises(Unavailable):
        oauth.redirect_uri(_oauth_settings(mail_oauth_redirect_base=""))

    class Refusing:
        async def exchange_code(self, *_: Any) -> Any:
            raise Rejected("oauth_code_rejected")

    with pytest.raises(Conflict):
        await oauth.complete(ctx, vault, _oauth_settings(), {"gmail": Refusing()}, "c", state)
    assert isinstance(OAuthMailProvider(GOOGLE, None, None), OAuthMailProvider)  # type: ignore[arg-type]
