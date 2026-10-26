"""The e-mail sender behind the dispatcher gate (AA-945; the crm-010 ``Sender`` of :mod:`app.dispatch.worker`).

For one dispatch row whose gate passed (inside the dispatcher's ``system_tx``):

1. the row's message (``crm.dispatch_queue.message_id``): an outbound ``queued`` message, else ``no_message``;
2. its sending mailbox (``message.channel_account_id``): missing, disconnected or without a credential ->
   ``held_reconnect`` (the message too, failure ``mailbox_disconnected``). **Never another path**: no fallback to AA's
   SES (errata S247 §1 removed ``allow_platform_sender``; card R);
3. the credential is decrypted here (``Vault.decrypt_secret``: lint-allowed in this module and :mod:`app.mail.health`
   only), the client's address and the message text under their DEKs, the MIME message is built
   (:mod:`app.mail.compose`: trip tag, In-Reply-To the client's latest mail, References ending with our own
   Message-ID) and the provider sends it;
4. :class:`~app.mail.providers.AuthRevoked` -> the message ``held_reconnect``, the mailbox and credential
   ``disconnected`` with the failure class, one alert to the owners through AA's SES (once, when it newly
   disconnects); :class:`~app.mail.providers.Rejected` -> the message ``failed`` and ``send_failed`` (dropped);
   anything else -> the job is retried;
5. sent -> ``sent`` with the Message-ID hash (and the id itself under the mail DEK) and the provider's id; a refresh
   token the provider rotated is sealed again in place (:func:`store_rotated`).

A portal code mail (AA-946 review S251: an ``otp_challenge`` row names the message) is stored with the code masked
(:data:`app.portal.codes.CODE_MASK`); the code is opened from ``otp_challenge.code_enc`` here, put into the mail and
cleared once sent. A code that is no longer pending or has expired -> the message ``failed`` (``code_expired``).

Nothing here logs an address, a subject or a body.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.mail import compose
from app.mail.parse import id_hash
from app.mail.providers import AuthRevoked, Rejected
from app.mail.state import MailRuntime, alert_owners, mark_disconnected
from app.portal.codes import CODE_MASK
from app.tenancy import AnyContext, SystemContext
from app.vault import Vault

log = logging.getLogger(__name__)

MESSAGE_SQL = """
SELECT m.message_id, m.status, m.direction, m.channel_account_id, m.subject_enc, m.body_enc, m.conversation_id,
       c.traveler_id, c.trip_id, t.mail_token
  FROM crm.message m
  JOIN crm.conversation c ON c.conversation_id = m.conversation_id
  LEFT JOIN crm.trip t ON t.trip_id = c.trip_id
 WHERE m.message_id = $1
   FOR UPDATE OF m
"""
MAILBOX_SQL = """
SELECT a.channel_account_id, a.kind, a.status, a.address_enc, a.credential_id, ic.secret_enc
  FROM crm.channel_account a
  LEFT JOIN crm.integration_credential ic ON ic.integration_credential_id = a.credential_id
 WHERE a.channel_account_id = $1 AND a.deleted_at IS NULL
"""
TRAVELER_SQL = "SELECT email_enc FROM crm.traveler WHERE traveler_id = $1"
REPLY_TO_SQL = """
SELECT message_id, rfc_message_id_enc FROM crm.message
 WHERE conversation_id = $1 AND direction = 'in' AND rfc_message_id_enc IS NOT NULL AND deleted_at IS NULL
 ORDER BY occurred_at DESC LIMIT 1
"""
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
HOLD_SQL = """
UPDATE crm.message SET status = 'held_reconnect', failure_code = $2 WHERE message_id = $1 AND status = 'queued'
"""
FAIL_SQL = "UPDATE crm.message SET status = 'failed', failure_code = $2 WHERE message_id = $1 AND status = 'queued'"
SENT_SQL = """
UPDATE crm.message
   SET status = 'sent', sent_at = now(), occurred_at = now(), rfc_message_id_hash = $2, rfc_message_id_enc = $3,
       external_id = $4
 WHERE message_id = $1
"""
TOUCH_SQL = "UPDATE crm.conversation SET last_message_at = now() WHERE conversation_id = $1"
CREDENTIAL_OK_SQL = """
UPDATE crm.integration_credential SET last_ok_at = now(), last_checked_at = now() WHERE integration_credential_id = $1
"""
ROTATE_SQL = "UPDATE crm.integration_credential SET secret_enc = $2 WHERE integration_credential_id = $1"
CODE_SQL = """
SELECT otp_id, code_enc, status <> 'pending' OR expires_at <= now() AS expired FROM crm.otp_challenge
 WHERE message_id = $1
   FOR UPDATE
"""
CODE_SENT_SQL = "UPDATE crm.otp_challenge SET code_enc = NULL WHERE otp_id = $1"
PURPOSE = "mail"


async def load_secret(vault: Vault, credential_id: uuid.UUID, blob: bytes) -> dict[str, Any]:
    """The decrypted credential JSON (never returned to a route, never logged)."""
    value = json.loads(await vault.decrypt_secret(credential_id, bytes(blob)))
    return value if isinstance(value, dict) else {}


async def store_rotated(
    ctx: AnyContext, vault: Vault, credential_id: uuid.UUID, rotated: dict[str, Any] | None
) -> bool:
    """Seal and store the credential the provider rotated (a new refresh token). True when one was stored."""
    if not rotated:
        return False
    await ctx.conn.execute(ROTATE_SQL, credential_id, await vault.encrypt_secret(credential_id, json.dumps(rotated)))
    return True


class MailSender:
    """The ``Sender`` for channel ``email`` (crm-010 dispatcher)."""

    def __init__(self, runtime: MailRuntime) -> None:
        self.runtime = runtime

    async def send(self, ctx: SystemContext, dispatch_id: uuid.UUID, channel: str, message_id: uuid.UUID | None) -> str:
        if channel != "email" or message_id is None or self.runtime.keys is None:
            return "no_message"
        row = await ctx.conn.fetchrow(MESSAGE_SQL, message_id)
        if row is None or row["direction"] != "out" or row["status"] != "queued" or row["traveler_id"] is None:
            return "no_message"
        mailbox = await ctx.conn.fetchrow(MAILBOX_SQL, row["channel_account_id"]) if row["channel_account_id"] else None
        provider = self.runtime.providers.get(mailbox["kind"]) if mailbox else None
        if (
            mailbox is None
            or provider is None
            or mailbox["status"] == "disconnected"
            or mailbox["credential_id"] is None
            or mailbox["secret_enc"] is None
        ):
            await ctx.conn.execute(HOLD_SQL, message_id, "mailbox_disconnected")
            return "held_reconnect"
        vault = Vault(self.runtime.keys, ctx)
        to_enc = await ctx.conn.fetchval(TRAVELER_SQL, row["traveler_id"])
        if to_enc is None:
            await ctx.conn.execute(FAIL_SQL, message_id, "no_client_address")
            return "send_failed"
        to_address = await vault.decrypt("traveler", "email_enc", row["traveler_id"], to_enc)
        from_address = await vault.decrypt(
            "channel_account", "address_enc", mailbox["channel_account_id"], mailbox["address_enc"], purpose=PURPOSE
        )
        subject = (
            await vault.decrypt("message", "subject_enc", message_id, row["subject_enc"], purpose=PURPOSE)
            if row["subject_enc"]
            else ""
        )
        text = (
            await vault.decrypt("message", "body_enc", message_id, row["body_enc"], purpose=PURPOSE)
            if row["body_enc"]
            else ""
        )
        code = await ctx.conn.fetchrow(CODE_SQL, message_id)
        if code is not None:
            if code["expired"] or code["code_enc"] is None:
                await ctx.conn.execute(FAIL_SQL, message_id, "code_expired")
                return "send_failed"
            text = text.replace(
                CODE_MASK, await vault.decrypt("otp_challenge", "code_enc", code["otp_id"], code["code_enc"])
            )
        reply = await ctx.conn.fetchrow(REPLY_TO_SQL, row["conversation_id"])
        in_reply_to = (
            await vault.decrypt(
                "message", "rfc_message_id_enc", reply["message_id"], reply["rfc_message_id_enc"], purpose=PURPOSE
            )
            if reply
            else None
        )
        built = compose.build(
            from_address=from_address,
            from_name=await ctx.conn.fetchval(AGENCY_SQL, ctx.tenant_id),
            to_address=to_address,
            subject=subject,
            text=text,
            mail_token=row["mail_token"],
            in_reply_to=in_reply_to,
        )
        secret = await load_secret(vault, mailbox["credential_id"], mailbox["secret_enc"])
        try:
            result = await provider.send(built.raw, secret, [to_address])
        except AuthRevoked as exc:
            await ctx.conn.execute(HOLD_SQL, message_id, exc.code)
            if await mark_disconnected(ctx, mailbox["channel_account_id"], mailbox["credential_id"], exc.code):
                await alert_owners(ctx, self.runtime)
            log.info("mail_send_held reason=%s", exc.code)
            return "held_reconnect"
        except Rejected as exc:
            await ctx.conn.execute(FAIL_SQL, message_id, exc.code)
            log.info("mail_send_rejected reason=%s", exc.code)
            return "send_failed"
        sealed_id = await vault.encrypt("message", "rfc_message_id_enc", message_id, built.message_id, purpose=PURPOSE)
        await ctx.conn.execute(SENT_SQL, message_id, id_hash(built.message_id), sealed_id, result.external_id)
        await ctx.conn.execute(TOUCH_SQL, row["conversation_id"])
        await ctx.conn.execute(CREDENTIAL_OK_SQL, mailbox["credential_id"])
        if code is not None:
            await ctx.conn.execute(CODE_SENT_SQL, code["otp_id"])
        await store_rotated(ctx, vault, mailbox["credential_id"], result.rotated_secret)
        return "sent"
