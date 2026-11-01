"""The SMS / WhatsApp sender behind the dispatcher gate (AA-958; the crm-010 ``Sender`` of :mod:`app.dispatch.worker`)
and the channel router the process registers.

For one dispatch row of channel ``sms`` / ``whatsapp`` whose gate passed (inside the dispatcher's ``system_tx``):

1. the row's message: an outbound ``queued`` message of a conversation with a client, else ``no_message``;
2. the agency's connection: none, or its credential ``disconnected`` -> the message ``failed``
   (``channel_disconnected``) and the row dropped ``send_failed``: errata S247 §2 "SMS / WhatsApp blocked with a
   clear reason"; nothing is ever sent another way (no AA account exists). No sender for the channel ->
   ``channel_not_configured``;
3. the client's number (``crm.traveler.phone_enc``, E.164): none or not E.164 -> ``no_client_phone``;
4. WhatsApp: free text only inside the 24-hour window (:func:`app.messaging.state.window_open`), else
   ``window_closed`` (the API refuses it first with 409 ``template_required``; this catches a window that closed
   during the Undo delay or a retry). A template must still be ``approved`` in ``crm.channel_template``, else
   ``template_not_approved``;
5. the credential is decrypted here (:func:`load_secret`: ``Vault.decrypt_secret`` is lint-allowed in this module), the
   provider sends; :class:`AuthRevoked` -> the credential ``disconnected`` + one owners' alert, the message
   ``channel_disconnected``; :class:`Rejected` -> the message ``failed`` with the code (``recipient_opted_out`` also
   marks the conversation opted out); anything else -> the job is retried;
6. sent -> the message ``sent`` with the provider's SID (``external_id``; status callbacks find it by it), the delivery
   row's first state, a ``message.sent`` event (:mod:`app.messaging.events`).

Nothing here logs a number or a text.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.dispatch.worker import Sender
from app.messaging import events, state
from app.messaging.providers import AuthRevoked, Rejected, TextOut
from app.tenancy import SystemContext
from app.vault import Vault
from app.vault.blind_index import NormalisationError, normalise_phone

log = logging.getLogger(__name__)

PURPOSE = "mail"
RANKS = {"queued": 1, "sending": 2, "sent": 3, "undelivered": 4, "failed": 4, "delivered": 5, "read": 6}
# Twilio's message states -> ours (accepted / scheduled are queued; canceled never left: failed).
STATUS_OF = {
    "accepted": "queued",
    "scheduled": "queued",
    "queued": "queued",
    "sending": "sending",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "undelivered": "undelivered",
    "failed": "failed",
    "canceled": "failed",
}

MESSAGE_SQL = """
SELECT m.message_id, m.status, m.direction, m.channel, m.body_enc, m.conversation_id, c.traveler_id, c.trip_id,
       d.content_sid, d.variables_enc
  FROM crm.message m
  JOIN crm.conversation c ON c.conversation_id = m.conversation_id
  LEFT JOIN crm.message_delivery d ON d.message_id = m.message_id
 WHERE m.message_id = $1
   FOR UPDATE OF m
"""
PHONE_SQL = "SELECT phone_enc FROM crm.traveler WHERE traveler_id = $1"
TEMPLATE_OK_SQL = """
SELECT 1 FROM crm.channel_template WHERE content_sid = $1 AND approval = 'approved' AND deleted_at IS NULL
"""
FAIL_SQL = "UPDATE crm.message SET status = 'failed', failure_code = $2 WHERE message_id = $1 AND status = 'queued'"
SENT_SQL = """
UPDATE crm.message SET status = 'sent', sent_at = now(), occurred_at = now(), external_id = $2 WHERE message_id = $1
"""
DELIVERY_SQL = """
INSERT INTO crm.message_delivery (message_id, channel, provider_status, status_rank, status_at)
VALUES ($1, $2, $3, $4, now())
ON CONFLICT (message_id) DO UPDATE
   SET provider_status = EXCLUDED.provider_status, status_rank = EXCLUDED.status_rank, status_at = now()
 WHERE crm.message_delivery.status_rank < EXCLUDED.status_rank
"""
TOUCH_SQL = "UPDATE crm.conversation SET last_message_at = now() WHERE conversation_id = $1"
OPT_OUT_SQL = """
UPDATE crm.conversation SET opted_out_at = coalesce(opted_out_at, now())
 WHERE channel = $3 AND deleted_at IS NULL AND (conversation_id = $1 OR ($2::uuid IS NOT NULL AND traveler_id = $2))
"""
CREDENTIAL_OK_SQL = """
UPDATE crm.integration_credential SET last_ok_at = now(), last_checked_at = now() WHERE integration_credential_id = $1
"""


async def load_secret(vault: Vault, credential_id: uuid.UUID, blob: bytes) -> dict[str, Any]:
    """The decrypted Twilio credential JSON (never returned to a route, never logged)."""
    value = json.loads(await vault.decrypt_secret(credential_id, bytes(blob)))
    return value if isinstance(value, dict) else {}


class TextSender:
    """The ``Sender`` for channels ``sms`` and ``whatsapp``."""

    def __init__(self, runtime: state.MessagingRuntime) -> None:
        self.runtime = runtime

    async def _fail(self, ctx: SystemContext, message_id: uuid.UUID, code: str) -> str:
        await ctx.conn.execute(FAIL_SQL, message_id, code)
        log.info("text_send_failed reason=%s", code)
        return "send_failed"

    async def send(self, ctx: SystemContext, dispatch_id: uuid.UUID, channel: str, message_id: uuid.UUID | None) -> str:
        if channel not in state.CHANNELS or message_id is None or self.runtime.keys is None:
            return "no_message"
        row = await ctx.conn.fetchrow(MESSAGE_SQL, message_id)
        if (
            row is None
            or row["direction"] != "out"
            or row["status"] != "queued"
            or row["traveler_id"] is None
            or row["channel"] != channel
        ):
            return "no_message"
        conn = await state.connection(ctx)
        provider = self.runtime.providers.get(conn["provider"]) if conn else None
        if conn is None or provider is None or conn["credential_status"] == "disconnected":
            return await self._fail(ctx, message_id, "channel_disconnected")
        sender = state.sender_for(conn, channel)
        if sender is None:
            return await self._fail(ctx, message_id, "channel_not_configured")
        vault = Vault(self.runtime.keys, ctx)
        phone_enc = await ctx.conn.fetchval(PHONE_SQL, row["traveler_id"])
        try:
            to = normalise_phone(await vault.decrypt("traveler", "phone_enc", row["traveler_id"], phone_enc))
        except (NormalisationError, TypeError):
            return await self._fail(ctx, message_id, "no_client_phone")
        content_sid = row["content_sid"]
        variables: dict[str, str] | None = None
        body: str | None = None
        if content_sid:
            if not await ctx.conn.fetchval(TEMPLATE_OK_SQL, content_sid):
                return await self._fail(ctx, message_id, "template_not_approved")
            if row["variables_enc"] is not None:
                variables = json.loads(
                    await vault.decrypt(
                        "message_delivery", "variables_enc", message_id, row["variables_enc"], purpose=PURPOSE
                    )
                )
        else:
            if channel == "whatsapp" and not await state.window_open(ctx, row["conversation_id"], row["traveler_id"]):
                return await self._fail(ctx, message_id, "window_closed")
            body = (
                await vault.decrypt("message", "body_enc", message_id, row["body_enc"], purpose=PURPOSE)
                if row["body_enc"]
                else ""
            )
        out = TextOut(
            channel=channel,
            to=to,
            sender=sender,
            status_callback=self.runtime.status_url(conn["hook_id"]),
            reference=str(message_id),
            body=body,
            content_sid=content_sid,
            variables=variables,
        )
        secret = await load_secret(vault, conn["credential_id"], conn["secret_enc"])
        try:
            sent = await provider.send(secret, out)
        except AuthRevoked as exc:
            if await state.mark_disconnected(ctx, conn["credential_id"], exc.code):
                await state.alert_owners(ctx, self.runtime)
            return await self._fail(ctx, message_id, "channel_disconnected")
        except Rejected as exc:
            if exc.code == "recipient_opted_out":
                await ctx.conn.execute(OPT_OUT_SQL, row["conversation_id"], row["traveler_id"], channel)
            return await self._fail(ctx, message_id, exc.code)
        await ctx.conn.execute(SENT_SQL, message_id, sent.sid)
        status = STATUS_OF.get(sent.status or "", "queued")
        await ctx.conn.execute(DELIVERY_SQL, message_id, channel, status, RANKS[status])
        await ctx.conn.execute(TOUCH_SQL, row["conversation_id"])
        await ctx.conn.execute(CREDENTIAL_OK_SQL, conn["credential_id"])
        await events.emit(
            ctx,
            events.MessageEvent(
                kind=events.SENT,
                channel=channel,
                message_id=message_id,
                conversation_id=row["conversation_id"],
                trip_id=row["trip_id"],
                traveler_id=row["traveler_id"],
            ),
        )
        return "sent"


class ChannelSender:
    """The one ``Sender`` the dispatcher gets: e-mail to the AA-945 mail sender, SMS / WhatsApp to :class:`TextSender`.
    A channel without its sender answers ``no_message`` (the row is held), as before CRM-28."""

    def __init__(self, mail: Sender | None, text: Sender | None) -> None:
        self.mail = mail
        self.text = text

    async def send(self, ctx: SystemContext, dispatch_id: uuid.UUID, channel: str, message_id: uuid.UUID | None) -> str:
        target = self.mail if channel == "email" else self.text if channel in state.CHANNELS else None
        if target is None:
            return "no_message"
        return await target.send(ctx, dispatch_id, channel, message_id)
