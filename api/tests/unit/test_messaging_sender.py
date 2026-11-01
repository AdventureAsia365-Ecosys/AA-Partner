"""AA-958 SMS / WhatsApp sender behind the dispatcher (scripted SQL, fake provider): sent through the agency's own
account with the delivery row and the ``message.sent`` event; a template with its sealed variables; the window closed
during the Undo delay; no connection / no sender / no phone / a template no longer approved; a revoked token
disconnects and alerts once; a refusal fails the message (STOP marks the conversation opted out); the channel router;
the message events seam."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest

from app.messaging import events, state
from app.messaging import sender as sender_module
from app.messaging.providers import AuthRevoked, Rejected, Transient
from app.messaging.sender import ChannelSender, TextSender
from tests.unit.fakes import ScriptedConn
from tests.unit.messaging_fakes import (
    AGENCY_SMS,
    AGENCY_WA,
    CLIENT_PHONE,
    CONTENT_SID,
    CREDENTIAL,
    HOOK_ID,
    FakeTextProvider,
    TextVault,
    connection_row,
    runtime,
)
from tests.unit.slice_fakes import enc, system_ctx

MESSAGE = uuid.UUID("00000000-0000-4000-8000-00000000f001")
DISPATCH = uuid.UUID("00000000-0000-4000-8000-00000000f002")
CONV = uuid.UUID("00000000-0000-4000-8000-00000000f003")
CLIENT = uuid.UUID("00000000-0000-4000-8000-00000000f004")
TRIP = uuid.UUID("00000000-0000-4000-8000-00000000f005")
FUTURE = dt.datetime.now(dt.UTC) + dt.timedelta(hours=3)
PAST = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)


@pytest.fixture(autouse=True)
def fake_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sender_module, "Vault", lambda keys, ctx: TextVault())


def _message(**over: Any) -> dict[str, Any]:
    return {
        "message_id": MESSAGE,
        "status": "queued",
        "direction": "out",
        "channel": "whatsapp",
        "body_enc": enc("message", "body_enc", "Your pickup is at 7"),
        "conversation_id": CONV,
        "traveler_id": CLIENT,
        "trip_id": TRIP,
        "content_sid": None,
        "variables_enc": None,
    } | over


def _conn(message: Any = None, connection: Any = "default", window: Any = FUTURE) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM crm.message m JOIN crm.conversation c", _message() if message is None else message)
    conn.on("FROM crm.messaging_connection c", connection_row() if connection == "default" else connection)
    conn.on("SELECT phone_enc FROM crm.traveler", enc("traveler", "phone_enc", CLIENT_PHONE))
    conn.on("SELECT max(wa_window_expires_at)", window)
    conn.on("FROM crm.channel_template WHERE content_sid", 1)
    conn.on("SELECT owner_member_id FROM crm.trip", None)
    conn.on("INSERT INTO crm.metric_event", uuid.uuid4())
    conn.on("RETURNING integration_credential_id", CREDENTIAL)
    conn.on("SELECT cognito_iss, cognito_sub FROM crm.member", [])
    return conn


@pytest.mark.asyncio
async def test_free_text_whatsapp_inside_the_window_is_sent() -> None:
    provider = FakeTextProvider()
    conn = _conn()
    seen: list[events.MessageEvent] = []

    async def listener(_ctx: Any, event: events.MessageEvent) -> None:
        seen.append(event)

    remove = events.subscribe(listener)
    try:
        out = await TextSender(runtime(provider)).send(system_ctx(conn), DISPATCH, "whatsapp", MESSAGE)
    finally:
        remove()
    assert out == "sent"
    ((secret, sent),) = provider.sent
    assert secret["auth_token"] and sent.to == CLIENT_PHONE and sent.sender == AGENCY_WA
    assert sent.body == "Your pickup is at 7" and sent.content_sid is None and sent.reference == str(MESSAGE)
    assert sent.status_callback == f"https://go.crm.example.test/crm/public/v1/hooks/twilio/{HOOK_ID}/status"
    assert conn.sql("SET status = 'sent'")[0] == (MESSAGE, "SM" + "5" * 32)
    assert conn.sql("INSERT INTO crm.message_delivery")[0] == (MESSAGE, "whatsapp", "queued", 1)
    (event,) = conn.sql("INSERT INTO crm.metric_event")
    assert event[0] == TRIP and event[2] == "message.sent" and json.loads(event[3])["channel"] == "whatsapp"
    assert [e.kind for e in seen] == ["sent"]


@pytest.mark.asyncio
async def test_a_template_goes_with_its_variables_and_no_window_check() -> None:
    provider = FakeTextProvider(status="weird")
    variables = enc("message_delivery", "variables_enc", json.dumps({"1": "Lan"}))
    conn = _conn(message=_message(content_sid=CONTENT_SID, variables_enc=variables, trip_id=None), window=PAST)
    assert await TextSender(runtime(provider)).send(system_ctx(conn), DISPATCH, "whatsapp", MESSAGE) == "sent"
    ((_, sent),) = provider.sent
    assert (sent.content_sid, sent.variables, sent.body) == (CONTENT_SID, {"1": "Lan"}, None)
    assert conn.sql("INSERT INTO crm.message_delivery")[0][2] == "queued"  # an unknown state counts as queued
    assert conn.sql("INSERT INTO crm.metric_event") == []  # no trip: no trip event


@pytest.mark.asyncio
async def test_sms_has_no_window() -> None:
    provider = FakeTextProvider()
    conn = _conn(message=_message(channel="sms"), window=PAST)
    assert await TextSender(runtime(provider)).send(system_ctx(conn), DISPATCH, "sms", MESSAGE) == "sent"
    assert provider.sent[0][1].sender == AGENCY_SMS


@pytest.mark.parametrize(
    ("message", "connection", "window", "code"),
    [
        (None, "default", PAST, "window_closed"),
        (None, "default", None, "window_closed"),
        (None, None, FUTURE, "channel_disconnected"),
        (None, connection_row(credential_status="disconnected"), FUTURE, "channel_disconnected"),
        (None, connection_row(provider="other"), FUTURE, "channel_disconnected"),
        (None, connection_row(whatsapp_sender=None), FUTURE, "channel_not_configured"),
    ],
)
@pytest.mark.asyncio
async def test_refused_before_the_provider(message: Any, connection: Any, window: Any, code: str) -> None:
    provider = FakeTextProvider()
    conn = _conn(message=message, connection=connection, window=window)
    assert await TextSender(runtime(provider)).send(system_ctx(conn), DISPATCH, "whatsapp", MESSAGE) == "send_failed"
    assert provider.sent == [] and conn.sql("SET status = 'failed'")[0] == (MESSAGE, code)


@pytest.mark.asyncio
async def test_no_phone_and_unapproved_template() -> None:
    conn = _conn().on("SELECT phone_enc FROM crm.traveler", None)
    assert await TextSender(runtime()).send(system_ctx(conn), DISPATCH, "whatsapp", MESSAGE) == "send_failed"
    assert conn.sql("SET status = 'failed'")[0][1] == "no_client_phone"
    conn = _conn().on("SELECT phone_enc FROM crm.traveler", enc("traveler", "phone_enc", "0912 not a number"))
    assert await TextSender(runtime()).send(system_ctx(conn), DISPATCH, "whatsapp", MESSAGE) == "send_failed"
    conn = _conn(message=_message(content_sid=CONTENT_SID)).on("FROM crm.channel_template WHERE content_sid", None)
    assert await TextSender(runtime()).send(system_ctx(conn), DISPATCH, "whatsapp", MESSAGE) == "send_failed"
    assert conn.sql("SET status = 'failed'")[0][1] == "template_not_approved"


@pytest.mark.parametrize(
    "message",
    [
        _message(direction="in"),
        _message(status="sent"),
        _message(traveler_id=None),
        _message(channel="sms"),
    ],
)
@pytest.mark.asyncio
async def test_nothing_to_send(message: dict[str, Any]) -> None:
    conn = _conn(message=message)
    assert await TextSender(runtime()).send(system_ctx(conn), DISPATCH, "whatsapp", MESSAGE) == "no_message"
    assert await TextSender(runtime()).send(system_ctx(_conn()), DISPATCH, "email", MESSAGE) == "no_message"
    assert await TextSender(runtime()).send(system_ctx(_conn()), DISPATCH, "whatsapp", None) == "no_message"
    conn = ScriptedConn().on("FROM crm.message m JOIN crm.conversation c", None)
    assert await TextSender(runtime()).send(system_ctx(conn), DISPATCH, "whatsapp", MESSAGE) == "no_message"


@pytest.mark.asyncio
async def test_a_revoked_token_disconnects_once_and_alerts_the_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Any] = []

    async def alert(ctx: Any, rt: Any) -> int:
        alerts.append(ctx)
        return 1

    monkeypatch.setattr(state, "alert_owners", alert)
    provider = FakeTextProvider(error=AuthRevoked("auth_revoked"))
    conn = _conn()
    ctx = system_ctx(conn)
    assert await TextSender(runtime(provider)).send(ctx, DISPATCH, "whatsapp", MESSAGE) == "send_failed"
    assert conn.sql("SET status = 'failed'")[0][1] == "channel_disconnected"
    assert conn.sql("SET status = 'disconnected'")[0] == (CREDENTIAL, "auth_revoked")
    assert len(alerts) == 1 and [e.action for e in ctx.audit_events] == ["messaging_disconnected"]
    conn = _conn().on("RETURNING integration_credential_id", None)  # already disconnected: no second alert
    assert await TextSender(runtime(provider)).send(system_ctx(conn), DISPATCH, "whatsapp", MESSAGE) == "send_failed"
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_refusals_fail_the_message_and_stop_marks_the_conversation() -> None:
    conn = _conn()
    provider = FakeTextProvider(error=Rejected("recipient_opted_out"))
    assert await TextSender(runtime(provider)).send(system_ctx(conn), DISPATCH, "whatsapp", MESSAGE) == "send_failed"
    assert conn.sql("SET opted_out_at")[0] == (CONV, CLIENT, "whatsapp")
    conn = _conn()
    provider = FakeTextProvider(error=Rejected("invalid_number"))
    assert await TextSender(runtime(provider)).send(system_ctx(conn), DISPATCH, "whatsapp", MESSAGE) == "send_failed"
    assert conn.sql("SET opted_out_at") == [] and conn.sql("SET status = 'failed'")[0][1] == "invalid_number"
    with pytest.raises(Transient):
        await TextSender(runtime(FakeTextProvider(error=Transient("unreachable")))).send(
            system_ctx(_conn()), DISPATCH, "whatsapp", MESSAGE
        )


@pytest.mark.asyncio
async def test_no_keys_means_no_message() -> None:
    rt = runtime()
    rt.keys = None
    assert await TextSender(rt).send(system_ctx(_conn()), DISPATCH, "whatsapp", MESSAGE) == "no_message"


class Recorder:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def send(self, ctx: Any, dispatch_id: uuid.UUID, channel: str, message_id: uuid.UUID | None) -> str:
        self.calls.append(channel)
        return self.answer


@pytest.mark.asyncio
async def test_the_channel_router() -> None:
    mail, text = Recorder("sent"), Recorder("held_reconnect")
    router = ChannelSender(mail=mail, text=text)
    ctx = system_ctx()
    assert await router.send(ctx, DISPATCH, "email", MESSAGE) == "sent"
    assert await router.send(ctx, DISPATCH, "sms", MESSAGE) == "held_reconnect"
    assert await router.send(ctx, DISPATCH, "whatsapp", MESSAGE) == "held_reconnect"
    assert await router.send(ctx, DISPATCH, "fax", MESSAGE) == "no_message"
    assert (mail.calls, text.calls) == (["email"], ["sms", "whatsapp"])
    assert await ChannelSender(mail=None, text=None).send(ctx, DISPATCH, "email", MESSAGE) == "no_message"


@pytest.mark.asyncio
async def test_events_record_a_bell_for_received_and_listeners_can_leave() -> None:
    conn = ScriptedConn().on("SELECT owner_member_id FROM crm.trip", uuid.uuid4())
    conn.on("INSERT INTO crm.metric_event", uuid.uuid4())
    seen: list[str] = []

    async def listener(_ctx: Any, event: events.MessageEvent) -> None:
        seen.append(event.kind)

    remove = events.subscribe(listener)
    event = events.MessageEvent("received", "sms", MESSAGE, CONV, TRIP, CLIENT, "sender")
    await events.emit(system_ctx(conn), event)
    remove()
    remove()  # removing twice is harmless
    await events.emit(system_ctx(conn), event)
    assert seen == ["received"]
    first = conn.sql("INSERT INTO crm.metric_event")[0]
    assert first[2] == "message.received" and first[4] is not None  # the trip owner's bell
    assert json.loads(first[3]) == {
        "channel": "sms",
        "conversation_id": str(CONV),
        "message_id": str(MESSAGE),
        "match_method": "sender",
    }
