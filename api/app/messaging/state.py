"""Shared state of the text-message paths (AA-958): the runtime, the webhook URLs, the connection read, the WhatsApp
window, the opt-out and the connection's failure handling.

- :class:`MessagingRuntime`: what the paths need from the process: the key provider, the providers (``twilio``, and
  ``sandbox`` on dev / test / local), the webhook origin, and the AA system mailer + partner directory for the
  "connection stopped working" alert to the owners (errata S247 §3 item 3).
- **The 24-hour window** (design §5.4): a WhatsApp free-text message is allowed only while the client wrote on
  WhatsApp in the last 24 hours, on this conversation or any other WhatsApp conversation of the same client
  (:func:`window_open`); outside it only an approved template is sent.
- **Opt-out**: a client who texted STOP on a channel gets nothing on that channel, whatever the conversation
  (:func:`opted_out`).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.members.directory import PartnerDirectory
from app.members.mail import Mailer, messaging_disconnected_message
from app.members.recovery import mail_owners
from app.tenancy import AnyContext
from app.vault import KeyProvider

log = logging.getLogger(__name__)

CHANNELS = ("sms", "whatsapp")
HOOK_PATH = "/crm/public/v1/hooks/twilio/"
WINDOW = dt.timedelta(hours=24)
FAILURE_CLASSES = frozenset({"auth_revoked", "permission_missing", "vendor_rejected", "rate_limited", "unreachable"})

CONNECTION_SQL = """
SELECT c.messaging_connection_id, c.provider, c.credential_id, c.hook_id, c.sms_sender, c.whatsapp_sender,
       c.last_inbound_at, c.last_status_at, c.created_at,
       ic.account_ref, ic.secret_enc, ic.status AS credential_status, ic.failure_class, ic.last_checked_at,
       ic.last_ok_at, ic.last_test_at, ic.last_test_result
  FROM crm.messaging_connection c
  JOIN crm.integration_credential ic ON ic.integration_credential_id = c.credential_id
 WHERE c.deleted_at IS NULL
 LIMIT 1
"""
WINDOW_SQL = """
SELECT max(wa_window_expires_at) FROM crm.conversation
 WHERE channel = 'whatsapp' AND deleted_at IS NULL
   AND (conversation_id = $1 OR ($2::uuid IS NOT NULL AND traveler_id = $2))
"""
OPTED_OUT_SQL = """
SELECT EXISTS (SELECT 1 FROM crm.conversation
                WHERE channel = $3 AND deleted_at IS NULL AND opted_out_at IS NOT NULL
                  AND (conversation_id = $1 OR ($2::uuid IS NOT NULL AND traveler_id = $2)))
"""
DISCONNECT_SQL = """
UPDATE crm.integration_credential SET status = 'disconnected', failure_class = $2, last_checked_at = now()
 WHERE integration_credential_id = $1 AND status <> 'disconnected'
RETURNING integration_credential_id
"""
ATTENTION_SQL = """
UPDATE crm.integration_credential SET status = 'needs_attention', failure_class = $2, last_checked_at = now()
 WHERE integration_credential_id = $1 AND status = 'connected'
"""
CONNECTED_SQL = """
UPDATE crm.integration_credential
   SET status = 'connected', failure_class = NULL, last_checked_at = now(), last_ok_at = now()
 WHERE integration_credential_id = $1
"""
OWNERS_SQL = """
SELECT cognito_iss, cognito_sub FROM crm.member WHERE role = 'owner' AND status = 'active' AND deleted_at IS NULL
"""
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"


@dataclass
class MessagingRuntime:
    keys: KeyProvider | None
    providers: dict[str, Any] = field(default_factory=dict)  # messaging_connection.provider -> TextProvider
    hooks_origin: str = ""
    mailer: Mailer | None = None
    directory: PartnerDirectory | None = None
    web_origin: str = ""

    def hook_url(self, hook_id: str) -> str:
        return f"{self.hooks_origin}{HOOK_PATH}{hook_id}" if self.hooks_origin else ""

    def status_url(self, hook_id: str) -> str:
        return f"{self.hook_url(hook_id)}/status" if self.hooks_origin else ""


async def connection(ctx: AnyContext) -> Any:
    """The agency's live connection joined with its credential, or None."""
    return await ctx.conn.fetchrow(CONNECTION_SQL)


def inbound_check(last_test_at: dt.datetime | None, last_inbound_at: dt.datetime | None) -> str | None:
    """The webhook check of the last Test: ``ok`` once a message arrived after it, ``pending`` until then, None
    before any test (Settings > Channels)."""
    if last_test_at is None:
        return None
    return "ok" if last_inbound_at is not None and last_inbound_at >= last_test_at else "pending"


def sender_for(row: Any, channel: str) -> str | None:
    return row["whatsapp_sender"] if channel == "whatsapp" else row["sms_sender"]


async def window_until(ctx: AnyContext, conversation_id: uuid.UUID, traveler_id: uuid.UUID | None) -> Any:
    return await ctx.conn.fetchval(WINDOW_SQL, conversation_id, traveler_id)


async def window_open(ctx: AnyContext, conversation_id: uuid.UUID, traveler_id: uuid.UUID | None) -> bool:
    until = await window_until(ctx, conversation_id, traveler_id)
    return until is not None and until > dt.datetime.now(dt.UTC)


async def opted_out(ctx: AnyContext, conversation_id: uuid.UUID, traveler_id: uuid.UUID | None, channel: str) -> bool:
    return bool(await ctx.conn.fetchval(OPTED_OUT_SQL, conversation_id, traveler_id, channel))


async def mark_disconnected(ctx: AnyContext, credential_id: uuid.UUID, code: str) -> bool:
    """The connection's credential goes ``disconnected``; True when it newly did (alert the owners once)."""
    failure = code if code in FAILURE_CLASSES else "auth_revoked"
    newly = await ctx.conn.fetchval(DISCONNECT_SQL, credential_id, failure) is not None
    if newly:
        ctx.audit("messaging_disconnected", "allowed", "integration_credential", str(credential_id), reason=failure)
    return newly


async def mark_attention(ctx: AnyContext, credential_id: uuid.UUID, code: str) -> None:
    await ctx.conn.execute(ATTENTION_SQL, credential_id, code if code in FAILURE_CLASSES else "vendor_rejected")


async def mark_connected(ctx: AnyContext, credential_id: uuid.UUID) -> None:
    await ctx.conn.execute(CONNECTED_SQL, credential_id)


async def alert_owners(ctx: AnyContext, runtime: MessagingRuntime) -> int:
    """AA system mail to the active owners: the SMS / WhatsApp connection stopped working. Best effort."""
    owners = await ctx.conn.fetch(OWNERS_SQL)
    agency = await ctx.conn.fetchval(AGENCY_SQL, ctx.tenant_id) or "your agency"
    origin = runtime.web_origin
    return await mail_owners(
        owners,
        runtime.directory,
        runtime.mailer,
        lambda email: messaging_disconnected_message(email, agency=agency, link=origin),
        tenant_id=ctx.tenant_id,
        tag="messaging_alert",
    )
