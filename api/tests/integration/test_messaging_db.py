"""AA-958 (card CRM-28) end to end on PostgreSQL 15 with the RDS role shape, real field encryption and the sandbox
provider (no network): the Done-when "two-way WhatsApp on a trip timeline".

- Connect (credential sealed, hook id, template sync job); an inbound WhatsApp from the trip lead's number, signed with
  the agency's token, lands on the trip (``sender``), opens the 24-hour window and rings the trip owner's bell; the
  agent's reply is queued, sent through the sandbox by the dispatcher and shows on the same thread.
- **R**: a webhook with a wrong signature is 403 and stores nothing; free text with the window closed is 409
  ``template_required`` and an approved template goes instead.
- **E**: status callbacks out of order keep the latest state (read stays read after a late delivered).
- An unknown number lands in Unmatched, never on a trip; STOP blocks a queued message at the dispatcher
  (``no_consent``) and new sends (409); ``messaging_hook_tx`` sees only its agency; a disconnect deletes the
  credential, the connection and its templates (cascade) and keeps the messages.
- A marketing template is refused (409 ``no_marketing_consent``) without a current marketing yes, and a queued one
  whose consent was withdrawn before it left ends ``no_consent`` at the dispatcher.
- A known client with no conversation on the channel and no open trip: the conversation is listed for the Inbox
  (``no_trip``), never lost.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import asyncpg
import pytest

from app.dispatch.worker import build_handler as build_dispatch
from app.errors import Conflict, Forbidden, NotFound
from app.jobs.queue import Job
from app.mail import conversations
from app.messaging import compose, connection, health, signature, webhook
from app.messaging.providers import SandboxProvider
from app.messaging.sender import ChannelSender, TextSender
from app.messaging.state import MessagingRuntime
from app.permissions import Action
from app.tenancy import messaging_hook_tx, tenant_tx
from app.vault import Vault
from tests.integration.test_crm005_intake import _keys
from tests.integration.test_crm010_lifecycle import _inquiry

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ORIGIN = "https://go.crm.example.test"
ACCOUNT_SID = "AC" + "0123456789abcdef" * 2
TOKEN = "dev-sandbox-" + "t0k3n" * 4
CLIENT_PHONE = "+61491570110"
STRANGER = "+61491570199"
AGENCY_WA = "+61491570156"


async def _sql(pool: asyncpg.Pool, tenant_id: uuid.UUID, sql: str, *args: Any) -> list[Any]:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        return await conn.fetch(sql, *args)


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    keys = _keys()
    d = await _inquiry(pool, keys, "owner")
    runtime = MessagingRuntime(keys=keys, providers={"sandbox": SandboxProvider()}, hooks_origin=ORIGIN)
    d |= {"keys": keys, "runtime": runtime}
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.INTEGRATIONS_MANAGE) as ctx:
        vault = Vault(keys, ctx)
        await ctx.conn.execute(
            "UPDATE crm.traveler SET phone_enc = $2, phone_bidx = $3 WHERE traveler_id = $1",
            d["lead"],
            await vault.encrypt("traveler", "phone_enc", d["lead"], CLIENT_PHONE),
            await vault.phone_index("traveler", "phone", CLIENT_PHONE),
        )
        out = await connection.connect(
            ctx,
            vault,
            runtime,
            provider="sandbox",
            secret={"account_sid": ACCOUNT_SID, "auth_token": TOKEN},
            sms_sender="Sunshine",
            whatsapp_sender=AGENCY_WA,
        )
    d["hook"] = out["hook_url"].rsplit("/", 1)[1]
    d["url"] = out["hook_url"]
    return d


def _inbound(d: dict[str, Any], *, sender: str = CLIENT_PHONE, body: str = "Is the kayak OK for a 7 year old?",
             token: str = TOKEN) -> webhook.HookRequest:  # fmt: skip
    params = [
        ("MessageSid", "SM" + uuid.uuid4().hex),
        ("AccountSid", ACCOUNT_SID),
        ("From", f"whatsapp:{sender}"),
        ("To", f"whatsapp:{AGENCY_WA}"),
        ("Body", body),
        ("NumMedia", "0"),
    ]
    return webhook.HookRequest(d["hook"], "inbound", params, signature.sign(token, d["url"], params))


def _status(d: dict[str, Any], sid: str, state: str) -> webhook.HookRequest:
    params = [("MessageSid", sid), ("AccountSid", ACCOUNT_SID), ("MessageStatus", state)]
    return webhook.HookRequest(d["hook"], "status", params, signature.sign(TOKEN, d["url"] + "/status", params))


async def _send(pool: asyncpg.Pool, d: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    async with tenant_tx(pool, d["owner"], d["tenant"], action=Action.MESSAGE_SEND) as ctx:
        args: dict[str, Any] = {
            "conversation_id": None,
            "trip_id": d["trip"],
            "channel": "whatsapp",
            "body": "Yes, from 6 years.",
            "content_sid": None,
            "variables": None,
        } | kwargs
        return await compose.send(ctx, Vault(d["keys"], ctx), uuid.uuid4(), **args)


async def _dispatch(pool: asyncpg.Pool, d: dict[str, Any], dispatch_id: str) -> dict[str, Any]:
    job = Job("j", "crm_dispatch", {"tenant_id": str(d["tenant"]), "dispatch_id": dispatch_id}, 1, 5)
    return await build_dispatch(ChannelSender(mail=None, text=TextSender(d["runtime"])))(pool, job)


async def test_two_way_whatsapp_on_the_trip_timeline(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    assert await webhook.handle(app_pool, d["runtime"], _inbound(d)) == "stored"
    (conv,) = await _sql(
        app_pool,
        d["tenant"],
        "SELECT conversation_id, trip_id, traveler_id, wa_window_expires_at FROM crm.conversation",
    )
    assert (conv["trip_id"], conv["traveler_id"]) == (d["trip"], d["lead"])
    assert conv["wa_window_expires_at"] > dt.datetime.now(dt.UTC) + dt.timedelta(hours=23)
    (bell,) = await _sql(
        app_pool, d["tenant"], "SELECT name, bell_member_id FROM crm.metric_event WHERE name LIKE 'message.%'"
    )
    assert bell["name"] == "message.received" and bell["bell_member_id"] is not None

    sent = await _send(app_pool, d)
    assert sent["conversation_id"] == str(conv["conversation_id"])
    assert (await _dispatch(app_pool, d, sent["dispatch_id"]))["status"] == "sent"
    (out,) = await _sql(
        app_pool, d["tenant"],
        "SELECT m.status, m.external_id, d.provider_status, d.status_rank FROM crm.message m "
        "JOIN crm.message_delivery d ON d.message_id = m.message_id WHERE m.direction = 'out'",
    )  # fmt: skip
    assert out["status"] == "sent" and out["external_id"].startswith("SM") and out["provider_status"] == "queued"

    # Card E: read, then a late delivered: read stays.
    assert await webhook.handle(app_pool, d["runtime"], _status(d, out["external_id"], "read")) == "status_applied"
    assert await webhook.handle(app_pool, d["runtime"], _status(d, out["external_id"], "delivered")) == "status_kept"
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.TRIP_VIEW) as ctx:
        thread = await conversations.conversation_messages(ctx, Vault(d["keys"], ctx), conv["conversation_id"])
    assert [m["direction"] for m in thread["messages"]] == ["in", "out"]
    assert thread["messages"][0]["text"] == "Is the kayak OK for a 7 year old?"
    assert thread["messages"][0]["from"] == CLIENT_PHONE and thread["messages"][1]["to"] == [CLIENT_PHONE]
    assert thread["messages"][1]["delivery"]["status"] == "read" and thread["window_until"]
    # Twilio retries the same inbound: stored once.
    again = _inbound(d)
    assert await webhook.handle(app_pool, d["runtime"], again) == "stored"
    assert await webhook.handle(app_pool, d["runtime"], again) == "duplicate"


async def test_card_r_bad_signature_and_template_required(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    with pytest.raises(Forbidden):
        await webhook.handle(app_pool, d["runtime"], _inbound(d, token="not-the-agency-token-123"))
    assert await _sql(app_pool, d["tenant"], "SELECT 1 FROM crm.message") == []
    with pytest.raises(Conflict) as caught:
        await _send(app_pool, d)  # no inbound yet: the window never opened
    assert caught.value.code == "template_required"
    # The template sync job reads the sandbox account's templates; an approved one goes outside the window.
    job = Job("j", "crm_messaging_templates", {"tenant_id": str(d["tenant"])}, 1, 3)
    assert (await health.build_templates_handler(d["runtime"])(app_pool, job))["approved_count"] == 2
    (template,) = await _sql(
        app_pool,
        d["tenant"],
        "SELECT content_sid FROM crm.channel_template WHERE approval = 'approved' AND language = 'en'",
    )
    sent = await _send(
        app_pool, d, body=None, content_sid=template["content_sid"], variables={"1": "Hoa", "2": "Hoi An"}
    )
    assert (await _dispatch(app_pool, d, sent["dispatch_id"]))["status"] == "sent"
    (row,) = await _sql(app_pool, d["tenant"], "SELECT content_sid, variables_enc FROM crm.message_delivery")
    assert row["content_sid"] == template["content_sid"] and row["variables_enc"] is not None


async def test_unknown_number_stop_and_isolation(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    other = await _setup(app_pool)
    assert (
        await webhook.handle(app_pool, d["runtime"], _inbound(d, sender=STRANGER, body="Hi, saw your tour")) == "stored"
    )
    (unmatched,) = await _sql(app_pool, d["tenant"], "SELECT trip_id, traveler_id FROM crm.conversation")
    assert (unmatched["trip_id"], unmatched["traveler_id"]) == (None, None)
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.TRIP_VIEW) as ctx:
        assert len((await conversations.list_conversations(ctx, unmatched=True))["conversations"]) == 1

    # The client writes (window open), the agent queues a reply, then the client texts STOP before it leaves.
    await webhook.handle(app_pool, d["runtime"], _inbound(d))
    queued = await _send(app_pool, d)
    await webhook.handle(app_pool, d["runtime"], _inbound(d, body="STOP"))
    assert (await _dispatch(app_pool, d, queued["dispatch_id"]))["reason_code"] == "no_consent"
    with pytest.raises(Conflict) as caught:
        await _send(app_pool, d)
    assert caught.value.code == "client_opted_out"
    await webhook.handle(app_pool, d["runtime"], _inbound(d, body="START"))
    assert (await _send(app_pool, d))["status"] == "queued"

    # The hook context sees only its own agency; an unknown hook is 404.
    async with messaging_hook_tx(app_pool, other["hook"]) as (ctx, hook):
        assert hook.tenant_id == other["tenant"]
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.message") == 0
        assert await ctx.conn.fetchval("SELECT count(*) FROM crm.messaging_connection") == 1
    with pytest.raises(NotFound):
        async with messaging_hook_tx(app_pool, "0" * 32):
            pass  # pragma: no cover


async def test_disconnect_deletes_credential_connection_and_templates(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    job = Job("j", "crm_messaging_templates", {"tenant_id": str(d["tenant"])}, 1, 3)
    await health.build_templates_handler(d["runtime"])(app_pool, job)
    await webhook.handle(app_pool, d["runtime"], _inbound(d))
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.INTEGRATIONS_MANAGE) as ctx:
        view = await connection.view(ctx, d["runtime"])
        assert view["status"] == "connected" and len(view["templates"]) == 3
        await connection.disconnect(ctx)
    for table in ("integration_credential", "messaging_connection", "channel_template"):
        assert await _sql(app_pool, d["tenant"], f"SELECT 1 FROM crm.{table}") == []  # noqa: S608 — fixed names
    assert len(await _sql(app_pool, d["tenant"], "SELECT 1 FROM crm.message")) == 1
    with pytest.raises(NotFound):
        await webhook.handle(app_pool, d["runtime"], _inbound(d))  # the hook is gone with the connection
    with pytest.raises(Conflict) as caught:
        await _send(app_pool, d)
    assert caught.value.code == "channel_not_connected"


async def _consent(pool: asyncpg.Pool, d: dict[str, Any], value: bool) -> None:
    await _sql(
        pool,
        d["tenant"],
        "INSERT INTO crm.consent (traveler_id, purpose, value, source) VALUES ($1, 'marketing', $2, 'agent_recorded')",
        d["lead"],
        value,
    )


async def test_a_marketing_template_follows_the_marketing_consent(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    job = Job("j", "crm_messaging_templates", {"tenant_id": str(d["tenant"])}, 1, 3)
    await health.build_templates_handler(d["runtime"])(app_pool, job)
    # The sandbox's marketing template is pending; the agency's Meta review approves it.
    (promo,) = await _sql(
        app_pool,
        d["tenant"],
        "UPDATE crm.channel_template SET approval = 'approved' WHERE category = 'MARKETING' RETURNING content_sid",
    )
    args = {"body": None, "content_sid": promo["content_sid"], "variables": {"1": "Hoa"}}
    await _consent(app_pool, d, False)
    with pytest.raises(Conflict) as caught:
        await _send(app_pool, d, **args)
    assert caught.value.code == "no_marketing_consent"
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.MESSAGE_SEND) as ctx:
        offered = await compose.compose_state(ctx, Vault(d["keys"], ctx), conversation_id=None, trip_id=d["trip"])
    assert {t["content_sid"]: t["available"] for t in offered["templates"]}[promo["content_sid"]] is False

    await _consent(app_pool, d, True)
    queued = await _send(app_pool, d, **args)
    (row,) = await _sql(app_pool, d["tenant"], "SELECT purpose FROM crm.dispatch_queue WHERE dispatch_id = $1",
                        uuid.UUID(queued["dispatch_id"]))  # fmt: skip
    assert row["purpose"] == "marketing"
    await _consent(app_pool, d, False)  # withdrawn during the Undo delay
    assert (await _dispatch(app_pool, d, queued["dispatch_id"]))["reason_code"] == "no_consent"


async def test_a_known_client_without_an_open_trip_is_listed_for_the_inbox(app_pool: asyncpg.Pool) -> None:
    d = await _setup(app_pool)
    lost = "UPDATE crm.trip SET stage = 'lost', lost_reason_code = 'postponed' WHERE trip_id = $1"
    await _sql(app_pool, d["tenant"], lost, d["trip"])  # the client's only trip is closed
    assert await webhook.handle(app_pool, d["runtime"], _inbound(d)) == "stored"
    (conv,) = await _sql(app_pool, d["tenant"], "SELECT conversation_id, trip_id, traveler_id FROM crm.conversation")
    assert (conv["trip_id"], conv["traveler_id"]) == (None, d["lead"])
    async with tenant_tx(app_pool, d["owner"], d["tenant"], action=Action.TRIP_VIEW) as ctx:
        assert (await conversations.list_conversations(ctx, unmatched=True))["conversations"] == []
        (listed,) = (await conversations.list_conversations(ctx, no_trip=True))["conversations"]
        assert listed["conversation_id"] == str(conv["conversation_id"]) and listed["traveler_id"] == str(d["lead"])
        # Link to trip (the Inbox action): the conversation moves to that trip and leaves the list.
        await conversations.link(ctx, conv["conversation_id"], d["lead"], d["trip"])
        assert (await conversations.list_conversations(ctx, no_trip=True))["conversations"] == []
