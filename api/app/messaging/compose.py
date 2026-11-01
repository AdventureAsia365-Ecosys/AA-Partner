"""Sending an SMS / WhatsApp message to a client (AA-958; design v3 M9, WF-11 "WhatsApp window open 21 h left
(outside: templates)"; card R "free text outside the window -> 409 template_required").

- :func:`compose_state` answers what the composer may offer on a conversation or a trip: per channel whether it can
  send (connection, sender, the client's number, opt-out), the WhatsApp window, and the approved templates.
- :func:`send` stores the message (``crm.message`` channel ``sms`` / ``whatsapp``, text under the mail DEK, From = the
  agency's sender, To = the client's number) with its delivery row (``crm.message_delivery``: the template and its
  variables, sealed) and queues one dispatch row (``origin user_action``: the 10-second Undo of AA-942; Undo / Edit
  are the AA-945 message routes, which work for every channel). The id is chosen by the composer, so a retried POST
  and a resend after Undo use the same row (the author's draft).

Refusals, in order: 404 / 422 for a bad target; 409 ``conversation_unmatched`` (link the client first);
403 for an assistant on a trip not assigned to them; 409 ``channel_not_connected`` (no connection, a disconnected one,
no sender for the channel); 409 ``no_client_phone`` (none or not E.164); 409 ``client_opted_out``; 409
``template_not_approved``; 409 ``no_marketing_consent`` (a marketing template to a client whose latest marketing
consent is not a yes); 422 ``template_variables_missing`` / ``invalid_template_variable``; WhatsApp free text with
the window closed (or never opened): **409 ``template_required``**; 422 ``body_required`` / ``body_too_long``. An
archived client: 409 ``traveler_archived`` (:func:`app.dispatch.queue.enqueue_message`).

**Marketing templates.** A template whose WhatsApp category is not ``UTILITY`` / ``AUTHENTICATION`` (``MARKETING``, or
none reported) is a marketing message: it is queued with ``purpose marketing``, so the dispatcher's consent gate
(``no_consent`` without a current ``crm.consent`` marketing yes) applies at send time too, and the composer offers it
as unavailable (``no_marketing_consent``) to a client without that consent. Free text and service templates stay
``transactional``.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import asyncpg

from app.dispatch import queue as dispatch
from app.errors import Conflict, Invalid, NotFound
from app.messaging import state
from app.messaging.providers import placeholders, render
from app.permissions import Action, require
from app.tenancy import TenantContext
from app.vault import Vault
from app.vault.blind_index import NormalisationError, normalise_phone

PURPOSE = "mail"
MAX_BODY = {"sms": 1600, "whatsapp": 4096}
MAX_VARIABLE = 1024
_BAD_VARIABLE = re.compile(r"[\r\n\t]| {5,}")

CONVERSATION_SQL = """
SELECT conversation_id, trip_id, traveler_id, channel FROM crm.conversation
 WHERE conversation_id = $1 AND deleted_at IS NULL
"""
TRIP_SQL = "SELECT owner_member_id FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
LEAD_SQL = "SELECT traveler_id FROM crm.trip_traveler WHERE trip_id = $1 AND role = 'lead' LIMIT 1"
CHANNEL_CONVERSATION_SQL = """
SELECT conversation_id FROM crm.conversation
 WHERE channel = $1 AND deleted_at IS NULL AND trip_id IS NOT DISTINCT FROM $2 AND traveler_id IS NOT DISTINCT FROM $3
 ORDER BY last_message_at DESC, conversation_id
 LIMIT 1
"""
NEW_CONVERSATION_SQL = """
INSERT INTO crm.conversation (traveler_id, trip_id, channel) VALUES ($1, $2, $3) RETURNING conversation_id
"""
PHONE_SQL = "SELECT phone_enc FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL"
TEMPLATE_SQL = """
SELECT content_sid, name, language, category, body, variables FROM crm.channel_template
 WHERE content_sid = $1 AND approval = 'approved' AND deleted_at IS NULL
"""
APPROVED_SQL = """
SELECT content_sid, name, language, category, body, variables FROM crm.channel_template
 WHERE approval = 'approved' AND deleted_at IS NULL
 ORDER BY name, language
 LIMIT 200
"""
# The client's latest marketing consent (the dispatcher's own rule, app.dispatch.worker): true, false or none.
MARKETING_SQL = """
SELECT c.value FROM crm.consent c WHERE c.traveler_id = $1 AND c.purpose = 'marketing'
 ORDER BY c.at DESC, c.created_at DESC, c.consent_id DESC
 LIMIT 1
"""
ROW_SQL = """
SELECT message_id, status, author_member_id, conversation_id FROM crm.message WHERE message_id = $1 FOR UPDATE
"""
INSERT_SQL = """
INSERT INTO crm.message (message_id, conversation_id, direction, status, channel, sender_kind, author_member_id,
                         body_enc, from_enc, to_enc, match_method)
VALUES ($1, $2, 'out', 'draft_autosaved', $3, 'agency_mailbox', $4, $5, $6, $7, 'compose')
"""
UPDATE_DRAFT_SQL = "UPDATE crm.message SET body_enc = $2, from_enc = $3, to_enc = $4 WHERE message_id = $1"
DELIVERY_SQL = """
INSERT INTO crm.message_delivery (message_id, channel, content_sid, variables_enc) VALUES ($1, $2, $3, $4)
ON CONFLICT (message_id) DO UPDATE SET content_sid = EXCLUDED.content_sid, variables_enc = EXCLUDED.variables_enc
"""
QUEUE_SQL = "UPDATE crm.message SET status = 'queued', occurred_at = now() WHERE message_id = $1"


SERVICE_CATEGORIES = frozenset({"UTILITY", "AUTHENTICATION"})


def is_marketing(category: str | None) -> bool:
    """A template is a marketing message unless its category says service (utility / authentication)."""
    return (category or "").strip().upper() not in SERVICE_CATEGORIES


async def marketing_consent(ctx: TenantContext, traveler_id: uuid.UUID) -> bool:
    return await ctx.conn.fetchval(MARKETING_SQL, traveler_id) is True


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def conversation_for(
    ctx: TenantContext, channel: str, trip_id: uuid.UUID | None, traveler_id: uuid.UUID
) -> uuid.UUID:
    """The latest conversation of exactly this (channel, trip, client), created when there is none."""
    found = await ctx.conn.fetchval(CHANNEL_CONVERSATION_SQL, channel, trip_id, traveler_id)
    if found is not None:
        return found
    return await ctx.conn.fetchval(NEW_CONVERSATION_SQL, traveler_id, trip_id, channel)


async def _target(
    ctx: TenantContext, *, conversation_id: uuid.UUID | None, trip_id: uuid.UUID | None, channel: str | None
) -> Any:
    """The conversation row to write on (an existing one, or the trip lead's on ``channel``)."""
    if conversation_id is not None:
        row = await ctx.conn.fetchrow(CONVERSATION_SQL, conversation_id)
        if row is None:
            raise NotFound("conversation_not_found")
        if row["channel"] not in state.CHANNELS or (channel is not None and channel != row["channel"]):
            raise Invalid("channel_mismatch", "this conversation is on another channel")
        return row
    if trip_id is None or channel not in state.CHANNELS:
        raise Invalid("target_required", "a conversation, or a trip and a channel (sms, whatsapp)")
    if await ctx.conn.fetchrow(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    lead = await ctx.conn.fetchval(LEAD_SQL, trip_id)
    if lead is None:
        raise Conflict("no_lead_traveller", "the trip has no lead traveller to write to")
    return await ctx.conn.fetchrow(CONVERSATION_SQL, await conversation_for(ctx, channel, trip_id, lead))


async def _client_phone(ctx: TenantContext, vault: Vault, traveler_id: uuid.UUID) -> str | None:
    blob = await ctx.conn.fetchval(PHONE_SQL, traveler_id)
    if blob is None:
        return None
    try:
        return normalise_phone(await vault.decrypt("traveler", "phone_enc", traveler_id, blob))
    except NormalisationError:
        return None


def _template_out(r: Any, consent: bool) -> dict[str, Any]:
    marketing = is_marketing(r["category"])
    blocked = marketing and not consent
    return {
        "content_sid": r["content_sid"],
        "name": r["name"],
        "language": r["language"],
        "body": r["body"],
        "variables": list(r["variables"] or []) or list(placeholders(r["body"])),
        "marketing": marketing,
        "available": not blocked,
        "reason": "no_marketing_consent" if blocked else None,
    }


async def compose_state(
    ctx: TenantContext, vault: Vault, *, conversation_id: uuid.UUID | None, trip_id: uuid.UUID | None
) -> dict[str, Any]:
    """What the composer may offer. ``conversation_id`` (a text conversation) or ``trip_id`` (its lead)."""
    traveler_id: uuid.UUID | None
    if conversation_id is not None:
        row = await ctx.conn.fetchrow(CONVERSATION_SQL, conversation_id)
        if row is None:
            raise NotFound("conversation_not_found")
        traveler_id, conv_ids = row["traveler_id"], {row["channel"]: row["conversation_id"]}
    elif trip_id is not None:
        if await ctx.conn.fetchrow(TRIP_SQL, trip_id) is None:
            raise NotFound("trip_not_found")
        traveler_id = await ctx.conn.fetchval(LEAD_SQL, trip_id)
        conv_ids = {}
        if traveler_id is not None:
            for channel in state.CHANNELS:
                found = await ctx.conn.fetchval(CHANNEL_CONVERSATION_SQL, channel, trip_id, traveler_id)
                if found is not None:
                    conv_ids[channel] = found
    else:
        raise Invalid("target_required", "a conversation or a trip")
    conn = await state.connection(ctx)
    has_phone = traveler_id is not None and await _client_phone(ctx, vault, traveler_id) is not None
    channels: dict[str, Any] = {}
    for channel in state.CHANNELS:
        reason = None
        if traveler_id is None:
            reason = "conversation_unmatched"
        elif conn is None or conn["credential_status"] == "disconnected" or not state.sender_for(conn, channel):
            reason = "channel_not_connected"
        elif not has_phone:
            reason = "no_client_phone"
        elif await state.opted_out(ctx, conv_ids.get(channel, uuid.UUID(int=0)), traveler_id, channel):
            reason = "client_opted_out"
        entry: dict[str, Any] = {"available": reason is None, "reason": reason}
        if conversation_id is not None and channel not in conv_ids:
            entry = {"available": False, "reason": "channel_mismatch"}
        if channel == "whatsapp":
            until = await state.window_until(ctx, conv_ids.get(channel, uuid.UUID(int=0)), traveler_id)
            entry["window_until"] = _iso(until)
        channels[channel] = entry
    templates: list[dict[str, Any]] = []
    if conn is not None:
        consent = traveler_id is not None and await marketing_consent(ctx, traveler_id)
        templates = [_template_out(r, consent) for r in await ctx.conn.fetch(APPROVED_SQL)]
    return {"traveler_id": str(traveler_id) if traveler_id else None, "channels": channels, "templates": templates}


def _variables(template: Any, variables: dict[str, str] | None) -> dict[str, str]:
    names = list(template["variables"] or []) or list(placeholders(template["body"]))
    given = {str(k): str(v) for k, v in (variables or {}).items()}
    missing = [n for n in names if not given.get(n, "").strip()]
    if missing:
        raise Invalid("template_variables_missing", ", ".join(missing))
    out = {}
    for name in names:
        value = given[name].strip()
        if len(value) > MAX_VARIABLE or _BAD_VARIABLE.search(value):
            raise Invalid("invalid_template_variable", f"{name}: one line, at most {MAX_VARIABLE} characters")
        out[name] = value
    return out


async def send(
    ctx: TenantContext,
    vault: Vault,
    message_id: uuid.UUID,
    *,
    conversation_id: uuid.UUID | None,
    trip_id: uuid.UUID | None,
    channel: str | None,
    body: str | None,
    content_sid: str | None,
    variables: dict[str, str] | None,
) -> dict[str, Any]:
    conv = await _target(ctx, conversation_id=conversation_id, trip_id=trip_id, channel=channel)
    channel = conv["channel"]
    if conv["traveler_id"] is None:
        raise Conflict("conversation_unmatched", "link the conversation to a client first")
    owner = (await ctx.conn.fetchrow(TRIP_SQL, conv["trip_id"]))["owner_member_id"] if conv["trip_id"] else None
    require(
        ctx,
        Action.MESSAGE_SEND,
        object_kind="conversation",
        object_id=str(conv["conversation_id"]),
        assigned=owner is not None and owner == ctx.member_id,
    )
    connection = await state.connection(ctx)
    sender = state.sender_for(connection, channel) if connection is not None else None
    if connection is None or connection["credential_status"] == "disconnected" or not sender:
        raise Conflict("channel_not_connected", "connect SMS / WhatsApp in Settings > Channels first")
    to = await _client_phone(ctx, vault, conv["traveler_id"])
    if to is None:
        raise Conflict("no_client_phone", "add the client's mobile number with its country code (+…)")
    if await state.opted_out(ctx, conv["conversation_id"], conv["traveler_id"], channel):
        raise Conflict("client_opted_out", "the client asked not to get messages on this channel")
    variables_enc: bytes | None = None
    purpose = "transactional"
    if content_sid:
        template = await ctx.conn.fetchrow(TEMPLATE_SQL, content_sid)
        if template is None:
            raise Conflict("template_not_approved", "only an approved template can be sent")
        if is_marketing(template["category"]):
            # The dispatcher checks the consent again at send time (purpose marketing -> no_consent).
            purpose = "marketing"
            if not await marketing_consent(ctx, conv["traveler_id"]):
                raise Conflict("no_marketing_consent", "the client has not agreed to marketing messages")
        values = _variables(template, variables)
        text = render(template["body"], values)
        if values:
            variables_enc = await vault.encrypt(
                "message_delivery", "variables_enc", message_id, json.dumps(values), purpose=PURPOSE
            )
    else:
        text = (body or "").replace("\x00", "").strip()
        if not text:
            raise Invalid("body_required", "write a message first")
        if len(text) > MAX_BODY[channel]:
            raise Invalid("body_too_long", f"at most {MAX_BODY[channel]} characters")
        if channel == "whatsapp" and not await state.window_open(ctx, conv["conversation_id"], conv["traveler_id"]):
            raise Conflict(
                "template_required",
                "the client has not written on WhatsApp in the last 24 hours: send an approved template",
            )
    body_enc = await vault.encrypt("message", "body_enc", message_id, text, purpose=PURPOSE)
    from_enc = await vault.encrypt("message", "from_enc", message_id, sender, purpose=PURPOSE)
    to_enc = await vault.encrypt("message", "to_enc", message_id, json.dumps([to]), purpose=PURPOSE)
    existing = await ctx.conn.fetchrow(ROW_SQL, message_id)
    if existing is None:
        try:
            async with ctx.conn.transaction():
                await ctx.conn.execute(
                    INSERT_SQL, message_id, conv["conversation_id"], channel, ctx.member_id, body_enc, from_enc, to_enc
                )
        except asyncpg.UniqueViolationError:
            raise Conflict("message_id_taken") from None
    elif (
        existing["author_member_id"] != ctx.member_id
        or existing["status"] != "draft_autosaved"
        or existing["conversation_id"] != conv["conversation_id"]
    ):
        raise Conflict("message_id_taken", "only the author's own draft on this conversation is sent again")
    else:
        await ctx.conn.execute(UPDATE_DRAFT_SQL, message_id, body_enc, from_enc, to_enc)
    await ctx.conn.execute(DELIVERY_SQL, message_id, channel, content_sid or None, variables_enc)
    await ctx.conn.execute(QUEUE_SQL, message_id)
    queued = await dispatch.enqueue_message(
        ctx,
        traveler_id=conv["traveler_id"],
        channel=channel,
        purpose=purpose,
        origin="user_action",
        trip_id=conv["trip_id"],
        message_id=message_id,
    )
    ctx.audit("message_send", "allowed", "message", str(message_id), reason=f"{channel}_{purpose}")
    return {
        "message_id": str(message_id),
        "conversation_id": str(conv["conversation_id"]),
        "channel": channel,
        "status": "queued",
        "dispatch_id": queued["dispatch_id"],
        "undo_until": queued["not_before"],
    }
