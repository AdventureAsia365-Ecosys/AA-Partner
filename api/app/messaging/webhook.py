"""Twilio webhooks of an agency (AA-958; design §5.4 "a per-tenant hook URL; the HMAC is checked before anything is
stored"; card R "invalid signature -> 403, dropped"; card E "out-of-order status callbacks keep the latest state";
spec §11.2 "unknown number -> Unmatched, never a random trip").

``POST /crm/public/v1/hooks/twilio/<hook_id>`` (an inbound message) and ``.../<hook_id>/status`` (a status callback),
form-encoded. :func:`handle`, in one :func:`app.tenancy.messaging_hook_tx`:

1. the hook id finds the agency's live connection (else 404 ``hook_not_found``);
2. the credential is opened (:func:`app.messaging.sender.load_secret`) and ``X-Twilio-Signature`` is checked against
   the URL the CRM gave the agency and the posted parameters (:mod:`app.messaging.signature`); a missing or wrong
   signature, or an ``AccountSid`` that is not the agency's: **403 ``invalid_signature``**, nothing stored (one
   count-only log line);
3. **inbound** (:func:`inbound`): channel from ``From`` (``whatsapp:`` prefix or SMS); deduplicated by ``MessageSid``
   (``crm.message.external_id``: Twilio retries); threaded, first match wins:

   a. the sender's number (E.164) is the phone blind index of exactly one live client -> that client's most recent
      conversation on this channel (any trip; ``sender``), else a new one on their most recent open trip;
   b. no client, or two clients share the number -> the Unmatched conversation of this number on this channel
      (``from_bidx`` of an earlier message), else a new Unmatched one (``unmatched``);

   the text is stored under the mail DEK, the number in ``from_enc`` + ``from_bidx`` (the traveller phone index, so a
   later link or client can be found by it); media are not downloaded (``attachment_notes`` ``media_not_kept``).
   WhatsApp: the conversation's window becomes now + 24 h. ``STOP`` (and Twilio's ``OptOutType``) marks every
   conversation of this client / number on the channel opted out, ``START`` clears it. A ``message.received`` event
   (:mod:`app.messaging.events`: the trip owner's bell, the workflow seam);
4. **status** (:func:`status`): the outbound message with that SID gets the reported state only when its rank is
   higher than the stored one (``crm.message_delivery``), so a late ``delivered`` never overwrites ``read``; error
   21610 (the client replied STOP to Twilio) marks the conversation opted out.

The answer is an empty TwiML ``<Response/>`` (no auto-reply). No number or text is logged.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.errors import Forbidden
from app.messaging import events, signature, state
from app.messaging.sender import RANKS, STATUS_OF, load_secret
from app.tenancy import SystemContext, messaging_hook_tx
from app.vault import Vault
from app.vault.blind_index import NormalisationError, normalise_phone

log = logging.getLogger(__name__)

PURPOSE = "mail"
MAX_TEXT = 10_000
STOP_WORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "OPT-OUT"})
START_WORDS = frozenset({"START", "UNSTOP", "SUBSCRIBE", "OPTIN", "OPT-IN"})
OPEN_STAGES = ("inquiry", "qualifying", "proposal", "booked", "pre_departure", "travelling")
_SID = ("SM", "MM")

DUPLICATE_SQL = "SELECT 1 FROM crm.message WHERE channel = $1 AND external_id = $2"
TRAVELER_SQL = """
SELECT traveler_id FROM crm.traveler WHERE phone_bidx = $1 AND deleted_at IS NULL ORDER BY created_at LIMIT 2
"""
LATEST_SQL = """
SELECT conversation_id, trip_id FROM crm.conversation
 WHERE channel = $1 AND traveler_id = $2 AND deleted_at IS NULL
 ORDER BY last_message_at DESC, conversation_id
 LIMIT 1
"""
OPEN_TRIP_SQL = """
SELECT t.trip_id FROM crm.trip_traveler tt JOIN crm.trip t ON t.trip_id = tt.trip_id
 WHERE tt.traveler_id = $1 AND t.deleted_at IS NULL AND t.stage = ANY($2::text[])
 ORDER BY t.updated_at DESC, t.trip_id
 LIMIT 1
"""
UNMATCHED_SQL = """
SELECT c.conversation_id FROM crm.conversation c
 WHERE c.trip_id IS NULL AND c.traveler_id IS NULL AND c.channel = $1 AND c.deleted_at IS NULL
   AND EXISTS (SELECT 1 FROM crm.message m WHERE m.conversation_id = c.conversation_id AND m.from_bidx = $2)
 ORDER BY c.last_message_at DESC
 LIMIT 1
"""
NEW_CONVERSATION_SQL = """
INSERT INTO crm.conversation (traveler_id, trip_id, channel) VALUES ($1, $2, $3) RETURNING conversation_id
"""
INSERT_SQL = """
INSERT INTO crm.message (message_id, conversation_id, direction, status, channel, body_enc, from_enc, from_bidx, to_enc,
                         external_id, match_method, attachment_notes)
VALUES ($1, $2, 'in', 'received', $3, $4, $5, $6, $7, $8, $9, $10::text[])
ON CONFLICT (tenant_id, channel, external_id) DO NOTHING
RETURNING message_id
"""
TOUCH_SQL = """
UPDATE crm.conversation
   SET last_message_at = now(),
       wa_window_expires_at = CASE WHEN channel = 'whatsapp' THEN now() + make_interval(hours => 24)
                                   ELSE wa_window_expires_at END
 WHERE conversation_id = $1
"""
OPT_SQL = """
UPDATE crm.conversation SET opted_out_at = CASE WHEN $4 THEN coalesce(opted_out_at, now()) ELSE NULL END
 WHERE channel = $3 AND deleted_at IS NULL
   AND (conversation_id = $1 OR ($2::uuid IS NOT NULL AND traveler_id = $2))
"""
LAST_INBOUND_SQL = "UPDATE crm.messaging_connection SET last_inbound_at = now() WHERE messaging_connection_id = $1"
LAST_STATUS_SQL = "UPDATE crm.messaging_connection SET last_status_at = now() WHERE messaging_connection_id = $1"
STATUS_SQL = """
UPDATE crm.message_delivery d
   SET provider_status = $2, status_rank = $3, error_code = $4, status_at = now()
  FROM crm.message m
 WHERE m.message_id = d.message_id AND m.external_id = $1 AND m.direction = 'out' AND d.status_rank < $3
RETURNING d.message_id
"""
MESSAGE_BY_SID_SQL = """
SELECT m.message_id, m.channel, m.conversation_id, c.traveler_id FROM crm.message m
  JOIN crm.conversation c ON c.conversation_id = m.conversation_id
 WHERE m.external_id = $1 AND m.direction = 'out'
"""


@dataclass(frozen=True)
class HookRequest:
    hook_id: str
    kind: str  # inbound | status
    params: list[tuple[str, str]]
    signature: str | None


def _param(params: list[tuple[str, str]], name: str) -> str:
    return next((v for k, v in params if k == name), "")


def _sid(value: str) -> str | None:
    value = value.strip()
    if len(value) == 34 and value[:2] in _SID and all(c in "0123456789abcdef" for c in value[2:]):
        return value
    return None


def _number(value: str) -> tuple[str, str]:
    """(channel, the number without the whatsapp: prefix)."""
    value = value.strip()
    if value.lower().startswith("whatsapp:"):
        return "whatsapp", value[9:].strip()
    return "sms", value


def opt_word(params: list[tuple[str, str]], text: str) -> bool | None:
    """True = opt out, False = opt back in, None = neither."""
    kind = _param(params, "OptOutType").strip().upper()
    if kind == "STOP":
        return True
    if kind == "START":
        return False
    word = " ".join(text.split()).upper()
    if word in STOP_WORDS:
        return True
    if word in START_WORDS:
        return False
    return None


async def handle(pool: asyncpg.Pool, runtime: state.MessagingRuntime, request: HookRequest) -> str:
    """Verify, then process one webhook. Returns a code for the log (``stored``, ``duplicate``, ``status_applied``
    ...). Raises :class:`Forbidden` (403) for a bad signature and :class:`NotFound` (404) for an unknown hook."""
    async with messaging_hook_tx(pool, request.hook_id) as (ctx, hook):
        conn = await state.connection(ctx)
        if conn is None or runtime.keys is None or conn["messaging_connection_id"] != hook.messaging_connection_id:
            raise Forbidden("invalid_signature")
        vault = Vault(runtime.keys, ctx)
        secret = await load_secret(vault, conn["credential_id"], conn["secret_enc"])
        url = runtime.hook_url(request.hook_id) + ("/status" if request.kind == "status" else "")
        if (
            not url
            or not signature.valid(str(secret.get("auth_token", "")), url, request.params, request.signature)
            or _param(request.params, "AccountSid") != secret.get("account_sid")
        ):
            log.warning("messaging_hook_refused reason=invalid_signature")
            raise Forbidden("invalid_signature")
        if request.kind == "status":
            return await status(ctx, conn, request.params)
        return await inbound(ctx, vault, conn, request.params)


async def _thread(
    ctx: SystemContext, channel: str, bidx: bytes | None
) -> tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None, str]:
    """(conversation, trip, client, match_method) for a message from the number with this blind index."""
    if bidx is not None:
        rows = await ctx.conn.fetch(TRAVELER_SQL, bidx)
        if len(rows) == 1:
            traveler_id = rows[0]["traveler_id"]
            latest = await ctx.conn.fetchrow(LATEST_SQL, channel, traveler_id)
            if latest is not None:
                return latest["conversation_id"], latest["trip_id"], traveler_id, "sender"
            trip_id = await ctx.conn.fetchval(OPEN_TRIP_SQL, traveler_id, list(OPEN_STAGES))
            conversation_id = await ctx.conn.fetchval(NEW_CONVERSATION_SQL, traveler_id, trip_id, channel)
            return conversation_id, trip_id, traveler_id, "sender"
        found = await ctx.conn.fetchval(UNMATCHED_SQL, channel, bidx)
        if found is not None:
            return found, None, None, "unmatched"
    return await ctx.conn.fetchval(NEW_CONVERSATION_SQL, None, None, channel), None, None, "unmatched"


async def inbound(ctx: SystemContext, vault: Vault, conn: Any, params: list[tuple[str, str]]) -> str:
    sid = _sid(_param(params, "MessageSid") or _param(params, "SmsMessageSid"))
    if sid is None:
        return "ignored"
    channel, number = _number(_param(params, "From"))
    if await ctx.conn.fetchval(DUPLICATE_SQL, channel, sid):
        return "duplicate"
    try:
        e164: str | None = normalise_phone(number)
    except NormalisationError:
        e164 = None  # a short code or a sender name: stored, never matched
    bidx = await vault.phone_index("traveler", "phone", e164) if e164 else None
    conversation_id, trip_id, traveler_id, method = await _thread(ctx, channel, bidx)
    text = _param(params, "Body").replace("\x00", "")[:MAX_TEXT]
    _, agency_number = _number(_param(params, "To"))
    message_id = uuid.uuid4()
    try:
        media = int(_param(params, "NumMedia") or "0")
    except ValueError:
        media = 0
    stored = await ctx.conn.fetchval(
        INSERT_SQL,
        message_id,
        conversation_id,
        channel,
        await vault.encrypt("message", "body_enc", message_id, text, purpose=PURPOSE) if text else None,
        await vault.encrypt("message", "from_enc", message_id, e164 or number[:64], purpose=PURPOSE),
        bidx,
        await vault.encrypt("message", "to_enc", message_id, json.dumps([agency_number[:32]]), purpose=PURPOSE),
        sid,
        method,
        ["media_not_kept"] if media > 0 else [],
    )
    if stored is None:
        return "duplicate"
    await ctx.conn.execute(TOUCH_SQL, conversation_id)
    opt = opt_word(params, text)
    if opt is not None:
        await ctx.conn.execute(OPT_SQL, conversation_id, traveler_id, channel, opt)
        ctx.audit("messaging_opt_out" if opt else "messaging_opt_in", "allowed", "conversation", str(conversation_id))
    await ctx.conn.execute(LAST_INBOUND_SQL, conn["messaging_connection_id"])
    ctx.audit("message_received", "allowed", "message", str(message_id), reason=method)
    await events.emit(
        ctx,
        events.MessageEvent(
            kind=events.RECEIVED,
            channel=channel,
            message_id=message_id,
            conversation_id=conversation_id,
            trip_id=trip_id,
            traveler_id=traveler_id,
            match_method=method,
        ),
    )
    return "stored"


async def status(ctx: SystemContext, conn: Any, params: list[tuple[str, str]]) -> str:
    sid = _sid(_param(params, "MessageSid") or _param(params, "SmsSid"))
    reported = STATUS_OF.get(_param(params, "MessageStatus").strip().lower())
    await ctx.conn.execute(LAST_STATUS_SQL, conn["messaging_connection_id"])
    if sid is None or reported is None:
        return "ignored"
    code = _param(params, "ErrorCode").strip()
    code = code if code.isdigit() and len(code) <= 6 else None
    applied = await ctx.conn.fetchval(STATUS_SQL, sid, reported, RANKS[reported], code)
    if code == "21610":
        row = await ctx.conn.fetchrow(MESSAGE_BY_SID_SQL, sid)
        if row is not None:
            await ctx.conn.execute(OPT_SQL, row["conversation_id"], row["traveler_id"], row["channel"], True)
    return "status_applied" if applied else "status_kept"
