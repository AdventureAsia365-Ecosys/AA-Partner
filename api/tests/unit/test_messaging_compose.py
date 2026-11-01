"""AA-958 the composer (scripted SQL): card R, WhatsApp free text with the window closed is 409 ``template_required``
while a template goes; the other refusals in order (unmatched, assistant, no connection / sender, no phone, opted out,
template not approved, variables); a marketing template needs a marketing yes and is queued as marketing; a send
stores the message sealed with its delivery row and queues one dispatch row with the Undo delay; a resend after Undo
reuses the author's draft; what the composer may offer per channel."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.messaging import compose
from tests.unit.fakes import ScriptedConn
from tests.unit.messaging_fakes import AGENCY_WA, CLIENT_PHONE, CONTENT_SID, TextVault, connection_row
from tests.unit.slice_fakes import MEMBER, enc, tenant_ctx

MSG = uuid.UUID("00000000-0000-4000-8000-00000000b001")
CONV = uuid.UUID("00000000-0000-4000-8000-00000000b002")
CLIENT = uuid.UUID("00000000-0000-4000-8000-00000000b003")
TRIP = uuid.UUID("00000000-0000-4000-8000-00000000b004")
NOW = dt.datetime.now(dt.UTC)
TEMPLATE = {
    "content_sid": CONTENT_SID,
    "name": "deposit_reminder",
    "language": "en",
    "category": "UTILITY",
    "body": "Hi {{1}}, the deposit for {{2}} is due.",
    "variables": ["1", "2"],
}
PROMO = TEMPLATE | {"content_sid": "HX" + "b2" * 16, "name": "autumn_offer", "category": "MARKETING"}


def _conn(**over: Any) -> ScriptedConn:
    values: dict[str, Any] = {
        "conversation": {"conversation_id": CONV, "trip_id": TRIP, "traveler_id": CLIENT, "channel": "whatsapp"},
        "owner": MEMBER,
        "connection": connection_row(),
        "phone": enc("traveler", "phone_enc", CLIENT_PHONE),
        "window": NOW + dt.timedelta(hours=2),
        "opted_out": False,
        "template": TEMPLATE,
        "existing": None,
        "marketing": None,
    } | over
    conn = ScriptedConn()
    conn.on("FROM crm.conversation WHERE conversation_id", values["conversation"])
    conn.on("FROM crm.trip WHERE trip_id", {"owner_member_id": values["owner"]})
    conn.on("FROM crm.messaging_connection c", values["connection"])
    conn.on("SELECT phone_enc FROM crm.traveler", values["phone"])
    conn.on("SELECT max(wa_window_expires_at)", values["window"])
    conn.on("SELECT EXISTS (SELECT 1 FROM crm.conversation", values["opted_out"])
    conn.on("FROM crm.channel_template WHERE content_sid", values["template"])
    conn.on("FROM crm.channel_template WHERE approval", [TEMPLATE, PROMO])
    conn.on("FROM crm.consent c WHERE c.traveler_id", values["marketing"])
    conn.on("FROM crm.message WHERE message_id = $1 FOR UPDATE", values["existing"])
    conn.on("SELECT archived_at, deleted_at FROM crm.traveler", {"archived_at": None, "deleted_at": None})
    conn.on("INSERT INTO crm.dispatch_queue", {"dispatch_id": uuid.uuid4(), "not_before": NOW})
    conn.on("FROM crm.trip_traveler WHERE trip_id", CLIENT)
    conn.on("WHERE channel = $1 AND deleted_at IS NULL AND trip_id", CONV)
    return conn


async def _send(conn: ScriptedConn, role: str = "agent", **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "conversation_id": CONV,
        "trip_id": None,
        "channel": None,
        "body": "See you at 7 tomorrow",
        "content_sid": None,
        "variables": None,
    } | over
    return await compose.send(tenant_ctx(conn, role=role), TextVault(), MSG, **args)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_free_text_inside_the_window_is_queued_with_undo() -> None:
    conn = _conn()
    out = await _send(conn)
    assert out["status"] == "queued" and out["channel"] == "whatsapp" and out["conversation_id"] == str(CONV)
    (insert,) = conn.sql("INSERT INTO crm.message (message_id")
    assert insert[:4] == (MSG, CONV, "whatsapp", MEMBER)
    assert bytes(insert[4]).decode().endswith("See you at 7 tomorrow")
    assert bytes(insert[5]).decode().endswith(AGENCY_WA) and json.loads(bytes(insert[6]).decode().split(":", 2)[2]) == [
        CLIENT_PHONE
    ]
    assert conn.sql("INSERT INTO crm.message_delivery")[0] == (MSG, "whatsapp", None, None)
    assert conn.sql("SET status = 'queued'")[0] == (MSG,)
    (dispatch,) = conn.sql("INSERT INTO crm.dispatch_queue")
    assert dispatch[0] == CLIENT and dispatch[2] == "whatsapp" and dispatch[4] == "user_action" and dispatch[6] == 10
    assert dispatch[3] == "transactional"


@pytest.mark.parametrize("window", [None, NOW - dt.timedelta(minutes=1)])
@pytest.mark.asyncio
async def test_card_r_free_text_outside_the_window_needs_a_template(window: Any) -> None:
    conn = _conn(window=window)
    with pytest.raises(Conflict) as caught:
        await _send(conn)
    assert caught.value.code == "template_required" and conn.sql("INSERT INTO crm.message") == []
    out = await _send(_conn(window=window), body=None, content_sid=CONTENT_SID, variables={"1": "Lan", "2": "Hoi An"})
    assert out["status"] == "queued"


@pytest.mark.asyncio
async def test_a_template_is_rendered_and_its_variables_sealed() -> None:
    conn = _conn(window=None)
    await _send(conn, body=None, content_sid=CONTENT_SID, variables={"1": " Lan ", "2": "Hoi An", "x": "ignored"})
    (insert,) = conn.sql("INSERT INTO crm.message (message_id")
    assert bytes(insert[4]).decode().endswith("Hi Lan, the deposit for Hoi An is due.")
    (delivery,) = conn.sql("INSERT INTO crm.message_delivery")
    assert delivery[2] == CONTENT_SID and json.loads(bytes(delivery[3]).decode().split(":", 2)[2]) == {
        "1": "Lan",
        "2": "Hoi An",
    }


@pytest.mark.asyncio
async def test_sms_has_no_window_and_a_length_cap() -> None:
    conv = {"conversation_id": CONV, "trip_id": None, "traveler_id": CLIENT, "channel": "sms"}
    assert (await _send(_conn(conversation=conv, window=None)))["channel"] == "sms"
    with pytest.raises(Invalid) as caught:
        await _send(_conn(conversation=conv), body="x" * 1601)
    assert caught.value.code == "body_too_long"
    with pytest.raises(Invalid) as caught:
        await _send(_conn(conversation=conv), body="   ")
    assert caught.value.code == "body_required"


@pytest.mark.parametrize(
    ("over", "send_over", "error", "code"),
    [
        ({"conversation": None}, {}, NotFound, "conversation_not_found"),
        (
            {"conversation": {"conversation_id": CONV, "trip_id": None, "traveler_id": CLIENT, "channel": "email"}},
            {},
            Invalid,
            "channel_mismatch",
        ),
        ({}, {"channel": "sms"}, Invalid, "channel_mismatch"),
        (
            {"conversation": {"conversation_id": CONV, "trip_id": None, "traveler_id": None, "channel": "sms"}},
            {},
            Conflict,
            "conversation_unmatched",
        ),
        ({"connection": None}, {}, Conflict, "channel_not_connected"),
        ({"connection": connection_row(credential_status="disconnected")}, {}, Conflict, "channel_not_connected"),
        ({"connection": connection_row(whatsapp_sender=None)}, {}, Conflict, "channel_not_connected"),
        ({"phone": None}, {}, Conflict, "no_client_phone"),
        ({"phone": enc("traveler", "phone_enc", "call me")}, {}, Conflict, "no_client_phone"),
        ({"opted_out": True}, {}, Conflict, "client_opted_out"),
        ({"template": None}, {"content_sid": CONTENT_SID, "body": None}, Conflict, "template_not_approved"),
        ({}, {"content_sid": CONTENT_SID, "variables": {"1": "Lan"}}, Invalid, "template_variables_missing"),
        ({}, {"content_sid": CONTENT_SID, "variables": {"1": "Lan", "2": "a\nb"}},
         Invalid, "invalid_template_variable"),
        ({"existing": {"message_id": MSG, "status": "sent", "author_member_id": MEMBER, "conversation_id": CONV}}, {},
         Conflict, "message_id_taken"),
    ],
)  # fmt: skip
@pytest.mark.asyncio
async def test_refusals(over: dict[str, Any], send_over: dict[str, Any], error: type, code: str) -> None:
    conn = _conn(**over)
    with pytest.raises(error) as caught:
        await _send(conn, **send_over)
    assert caught.value.code == code
    assert conn.sql("INSERT INTO crm.dispatch_queue") == []


@pytest.mark.asyncio
async def test_an_assistant_sends_only_on_an_assigned_trip() -> None:
    with pytest.raises(Forbidden):
        await _send(_conn(owner=uuid.uuid4()), role="assistant")
    assert (await _send(_conn(), role="assistant"))["status"] == "queued"


@pytest.mark.asyncio
async def test_a_resend_after_undo_reuses_the_draft() -> None:
    draft = {"message_id": MSG, "status": "draft_autosaved", "author_member_id": MEMBER, "conversation_id": CONV}
    conn = _conn(existing=draft)
    await _send(conn)
    assert conn.sql("INSERT INTO crm.message (message_id") == []
    assert conn.sql("UPDATE crm.message SET body_enc")[0][0] == MSG


@pytest.mark.asyncio
async def test_a_trip_target_writes_to_its_lead_on_the_channel() -> None:
    conn = _conn().on("WHERE channel = $1 AND deleted_at IS NULL AND trip_id", None)
    conn.on("INSERT INTO crm.conversation", CONV)
    out = await _send(conn, conversation_id=None, trip_id=TRIP, channel="whatsapp")
    assert conn.sql("INSERT INTO crm.conversation")[0] == (CLIENT, TRIP, "whatsapp") and out["status"] == "queued"
    with pytest.raises(Invalid):
        await _send(_conn(), conversation_id=None, trip_id=TRIP, channel=None)
    with pytest.raises(NotFound):
        await _send(_conn().on("FROM crm.trip WHERE trip_id", None), conversation_id=None, trip_id=TRIP, channel="sms")
    no_lead = _conn().on("FROM crm.trip_traveler WHERE trip_id", None)
    with pytest.raises(Conflict):
        await _send(no_lead, conversation_id=None, trip_id=TRIP, channel="sms")


@pytest.mark.asyncio
async def test_compose_state_on_a_conversation_and_a_trip() -> None:
    vault = TextVault()
    out = await compose.compose_state(tenant_ctx(_conn()), vault, conversation_id=CONV, trip_id=None)  # type: ignore[arg-type]
    assert out["channels"]["whatsapp"]["available"] is True and out["channels"]["whatsapp"]["window_until"]
    assert out["channels"]["sms"] == {"available": False, "reason": "channel_mismatch"}
    assert out["templates"][0]["variables"] == ["1", "2"] and out["traveler_id"] == str(CLIENT)
    trip = await compose.compose_state(tenant_ctx(_conn(opted_out=True)), vault, conversation_id=None, trip_id=TRIP)  # type: ignore[arg-type]
    assert trip["channels"]["sms"]["reason"] == "client_opted_out"
    nothing = await compose.compose_state(
        tenant_ctx(_conn(connection=None, phone=None)),
        vault,
        conversation_id=None,
        trip_id=TRIP,  # type: ignore[arg-type]
    )
    assert nothing["channels"]["sms"]["reason"] == "channel_not_connected" and nothing["templates"] == []
    no_phone = await compose.compose_state(tenant_ctx(_conn(phone=None)), vault, conversation_id=None, trip_id=TRIP)  # type: ignore[arg-type]
    assert no_phone["channels"]["whatsapp"]["reason"] == "no_client_phone"
    unmatched = {"conversation_id": CONV, "trip_id": None, "traveler_id": None, "channel": "sms"}
    out = await compose.compose_state(
        tenant_ctx(_conn(conversation=unmatched)), vault, conversation_id=CONV, trip_id=None
    )  # type: ignore[arg-type]
    assert out["channels"]["sms"]["reason"] == "conversation_unmatched"
    with pytest.raises(NotFound):
        await compose.compose_state(tenant_ctx(_conn(conversation=None)), vault, conversation_id=CONV, trip_id=None)  # type: ignore[arg-type]
    with pytest.raises(NotFound):
        await compose.compose_state(
            tenant_ctx(_conn().on("FROM crm.trip WHERE trip_id", None)),
            vault,
            conversation_id=None,
            trip_id=TRIP,  # type: ignore[arg-type]
        )
    with pytest.raises(Invalid):
        await compose.compose_state(tenant_ctx(_conn()), vault, conversation_id=None, trip_id=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("category", ["MARKETING", None, "promo"])
@pytest.mark.asyncio
async def test_a_marketing_template_needs_marketing_consent(category: Any) -> None:
    """A template that is not a service one (utility / authentication) is marketing: refused without a current
    marketing yes, and queued with purpose marketing so the dispatcher checks the consent again at send time."""
    promo = PROMO | {"category": category}
    for consent in (None, False):
        conn = _conn(window=None, template=promo, marketing=consent)
        with pytest.raises(Conflict) as caught:
            await _send(conn, body=None, content_sid=PROMO["content_sid"], variables={"1": "Lan", "2": "Hoi An"})
        assert caught.value.code == "no_marketing_consent" and conn.sql("INSERT INTO crm.dispatch_queue") == []
    conn = _conn(window=None, template=promo, marketing=True)
    await _send(conn, body=None, content_sid=PROMO["content_sid"], variables={"1": "Lan", "2": "Hoi An"})
    (dispatch,) = conn.sql("INSERT INTO crm.dispatch_queue")
    assert dispatch[3] == "marketing"


@pytest.mark.parametrize("category", ["UTILITY", "authentication"])
@pytest.mark.asyncio
async def test_a_service_template_is_transactional_without_consent(category: str) -> None:
    conn = _conn(window=None, template=TEMPLATE | {"category": category}, marketing=False)
    await _send(conn, body=None, content_sid=CONTENT_SID, variables={"1": "Lan", "2": "Hoi An"})
    assert conn.sql("INSERT INTO crm.dispatch_queue")[0][3] == "transactional"
    assert conn.sql("FROM crm.consent") == []


@pytest.mark.asyncio
async def test_compose_state_marks_marketing_templates_without_consent() -> None:
    vault = TextVault()
    out = await compose.compose_state(tenant_ctx(_conn()), vault, conversation_id=CONV, trip_id=None)  # type: ignore[arg-type]
    service, promo = out["templates"]
    assert (service["marketing"], service["available"], service["reason"]) == (False, True, None)
    assert (promo["marketing"], promo["available"], promo["reason"]) == (True, False, "no_marketing_consent")
    agreed = await compose.compose_state(tenant_ctx(_conn(marketing=True)), vault, conversation_id=CONV, trip_id=None)  # type: ignore[arg-type]
    assert all(t["available"] for t in agreed["templates"])
