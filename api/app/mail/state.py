"""Mailbox state changes shared by the sender, the health check and the mailbox routes (AA-945; errata S247 §2, R24).

- :func:`mark_disconnected`: the mailbox and its credential go ``disconnected`` with the failure class; True when it
  was not disconnected yet (the caller then alerts the owners once, not on every held message).
- :func:`mark_connected`: back to ``connected`` (a passing check, a reconnect); its ``held_reconnect`` messages and
  their dispatch rows go back to the queue (:func:`requeue_mailbox`).
- :func:`alert_owners`: the AA system mail "mailbox disconnected" to the agency's active owners, best effort, through
  AA's SES (errata §3 item 3). The client's own message never takes this path.

Runtime: :class:`MailRuntime` holds what the mail paths need from the process (key provider, the send-as providers,
the AA system mailer and the partner directory for the owners' addresses, the partner web origin for links).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.dispatch import queue as dispatch
from app.members.directory import PartnerDirectory
from app.members.mail import Mailer, mailbox_disconnected_message
from app.members.recovery import mail_owners
from app.tenancy import AnyContext
from app.vault import KeyProvider

log = logging.getLogger(__name__)

DISCONNECT_MAILBOX_SQL = """
UPDATE crm.channel_account SET status = 'disconnected', last_token_check_at = now()
 WHERE channel_account_id = $1 AND status <> 'disconnected'
RETURNING channel_account_id
"""
DISCONNECT_CREDENTIAL_SQL = """
UPDATE crm.integration_credential SET status = 'disconnected', failure_class = $2, last_checked_at = now()
 WHERE integration_credential_id = $1
"""
CONNECT_MAILBOX_SQL = """
UPDATE crm.channel_account SET status = 'connected', last_token_check_at = now()
 WHERE channel_account_id = $1 AND credential_id IS NOT NULL
RETURNING channel_account_id
"""
CONNECT_CREDENTIAL_SQL = """
UPDATE crm.integration_credential
   SET status = 'connected', failure_class = NULL, last_checked_at = now(), last_ok_at = now()
 WHERE integration_credential_id = $1
"""
HELD_MESSAGES_SQL = """
UPDATE crm.message SET status = 'queued', failure_code = NULL
 WHERE channel_account_id = $1 AND status = 'held_reconnect'
RETURNING message_id
"""
HELD_DISPATCH_SQL = """
SELECT dispatch_id FROM crm.dispatch_queue
 WHERE message_id = ANY($1::uuid[]) AND status = 'held' AND gate_result IN ('held_reconnect', 'no_sender')
"""
OWNERS_SQL = """
SELECT cognito_iss, cognito_sub FROM crm.member WHERE role = 'owner' AND status = 'active' AND deleted_at IS NULL
"""
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
FAILURE_CLASSES = frozenset({"auth_revoked", "permission_missing", "vendor_rejected", "rate_limited", "unreachable"})


@dataclass
class MailRuntime:
    keys: KeyProvider | None
    providers: dict[str, Any] = field(default_factory=dict)  # channel_account.kind -> app.mail.providers.MailProvider
    mailer: Mailer | None = None
    directory: PartnerDirectory | None = None
    web_origin: str = ""


async def mark_disconnected(
    ctx: AnyContext, channel_account_id: uuid.UUID, credential_id: uuid.UUID | None, code: str
) -> bool:
    failure = code if code in FAILURE_CLASSES else "auth_revoked"
    newly = await ctx.conn.fetchval(DISCONNECT_MAILBOX_SQL, channel_account_id) is not None
    if credential_id is not None:
        await ctx.conn.execute(DISCONNECT_CREDENTIAL_SQL, credential_id, failure)
    if newly:
        ctx.audit("mailbox_disconnected", "allowed", "channel_account", str(channel_account_id), reason=failure)
    return newly


async def requeue_mailbox(ctx: AnyContext, channel_account_id: uuid.UUID) -> int:
    held = [r["message_id"] for r in await ctx.conn.fetch(HELD_MESSAGES_SQL, channel_account_id)]
    if not held:
        return 0
    rows = [r["dispatch_id"] for r in await ctx.conn.fetch(HELD_DISPATCH_SQL, held)]
    return await dispatch.requeue(ctx, rows, reason="mailbox_reconnected")


async def mark_connected(ctx: AnyContext, channel_account_id: uuid.UUID, credential_id: uuid.UUID) -> int:
    """Connected again; returns how many held messages went back to the queue."""
    await ctx.conn.execute(CONNECT_CREDENTIAL_SQL, credential_id)
    await ctx.conn.fetchval(CONNECT_MAILBOX_SQL, channel_account_id)
    return await requeue_mailbox(ctx, channel_account_id)


async def alert_owners(ctx: AnyContext, runtime: MailRuntime) -> int:
    owners = await ctx.conn.fetch(OWNERS_SQL)
    agency = await ctx.conn.fetchval(AGENCY_SQL, ctx.tenant_id) or "your agency"
    origin = runtime.web_origin
    return await mail_owners(
        owners,
        runtime.directory,
        runtime.mailer,
        lambda email: mailbox_disconnected_message(email, agency=agency, link=origin),
        tenant_id=ctx.tenant_id,
        tag="mailbox_alert",
    )
