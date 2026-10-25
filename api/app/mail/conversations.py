"""Conversations, messages, drafts, sending and saved replies (AA-945; design v3 M5 "saved replies, notes, autosaved
drafts"; Fix B8 Undo; R24 held_reconnect).

- **Lists** carry ids, counts and dates only (design §4.4: no contact data in lists), one ``list_view`` audit event.
- **A conversation's messages** are decrypted for the agent (subject, text, HTML for the sandboxed iframe, From / To):
  one ``view`` audit event with the fields. A member sees their own drafts only.
- **Link** (the Unmatched inbox): a conversation without a client gets one (and optionally a trip); a link set from
  NULL is never refused by the hold rule, an existing link is not changed here (409 ``already_linked``).
- **Drafts** (``draft_autosaved``): ``PUT /messages/drafts/{id}`` with an id the composer chose, so the 3-second
  autosave is idempotent; only the author edits or discards a draft (crm-015 lets crm_app delete drafts only).
- **Send**: the draft is sealed and ``queued``, then one dispatch row (``origin user_action``: 10 s Undo delay) with
  ``message_id``. Needs a client on the conversation (409 ``conversation_unmatched``) and a sending mailbox (the
  member's own, else the agency's; none -> 409 ``mailbox_not_connected``). A disconnected mailbox is accepted: the
  message is held at send time (R24). The assistant sends only on a trip assigned to them (``MESSAGE_SEND`` ASSIGNED).
- **Undo** within the delay: the dispatch row is cancelled and the message goes back to a draft.
- **Saved replies**: agency-wide ones (owner: ``WORKFLOW_EDIT``, the agency's templates) and each member's own.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import asyncpg

from app.dispatch import queue as dispatch
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.mail import threads
from app.permissions import Action, require
from app.tenancy import TenantContext
from app.vault import Vault

PURPOSE = "mail"
MAX_SUBJECT = 300
MAX_BODY = 100_000
MAX_LIST = 100

LIST_SQL = """
SELECT c.conversation_id, c.trip_id, c.traveler_id, c.channel, c.last_message_at,
       (SELECT count(*) FROM crm.message m WHERE m.conversation_id = c.conversation_id
           AND m.status <> 'draft_autosaved' AND m.deleted_at IS NULL)::int AS message_count,
       (SELECT count(*) FROM crm.message m WHERE m.conversation_id = c.conversation_id
           AND m.status = 'held_reconnect')::int AS held_count,
       (SELECT m.direction FROM crm.message m WHERE m.conversation_id = c.conversation_id
           AND m.status <> 'draft_autosaved' ORDER BY m.occurred_at DESC LIMIT 1) AS last_direction
  FROM crm.conversation c
 WHERE c.deleted_at IS NULL
   AND ($1::uuid IS NULL OR c.trip_id = $1)
   AND ($2::uuid IS NULL OR c.traveler_id = $2)
   AND (NOT $3 OR (c.trip_id IS NULL AND c.traveler_id IS NULL))
 ORDER BY c.last_message_at DESC, c.conversation_id
 LIMIT $4
"""
CONVERSATION_SQL = """
SELECT conversation_id, trip_id, traveler_id, channel, last_message_at FROM crm.conversation
 WHERE conversation_id = $1 AND deleted_at IS NULL
"""
MESSAGES_SQL = """
SELECT message_id, direction, status, sender_kind, author_member_id, subject_enc, body_enc, body_html_enc, from_enc,
       to_enc, attachments, attachment_notes, failure_code, imported, match_method, occurred_at, sent_at, updated_at
  FROM crm.message
 WHERE conversation_id = $1 AND deleted_at IS NULL
   AND (status <> 'draft_autosaved' OR author_member_id = $2)
 ORDER BY occurred_at, message_id
 LIMIT 500
"""
LINK_SQL = """
UPDATE crm.conversation SET traveler_id = coalesce(traveler_id, $2), trip_id = coalesce(trip_id, $3)
 WHERE conversation_id = $1
"""
TRAVELER_SQL = "SELECT 1 FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL"
TRIP_SQL = "SELECT owner_member_id FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
LEAD_SQL = "SELECT traveler_id FROM crm.trip_traveler WHERE trip_id = $1 AND role = 'lead' LIMIT 1"
DRAFT_SQL = """
SELECT message_id, conversation_id, status, author_member_id, channel_account_id FROM crm.message
 WHERE message_id = $1 FOR UPDATE
"""
INSERT_DRAFT_SQL = """
INSERT INTO crm.message (message_id, conversation_id, direction, status, sender_kind, channel_account_id,
                         author_member_id, subject_enc, body_enc, match_method)
VALUES ($1, $2, 'out', 'draft_autosaved', 'agency_mailbox', $3, $4, $5, $6, 'compose')
RETURNING updated_at
"""
UPDATE_DRAFT_SQL = """
UPDATE crm.message SET subject_enc = $2, body_enc = $3, channel_account_id = coalesce(channel_account_id, $4)
 WHERE message_id = $1
RETURNING updated_at
"""
DELETE_DRAFT_SQL = "DELETE FROM crm.message WHERE message_id = $1"
QUEUE_SQL = "UPDATE crm.message SET status = 'queued', occurred_at = now() WHERE message_id = $1"
BACK_TO_DRAFT_SQL = """
UPDATE crm.message SET status = 'draft_autosaved', failure_code = NULL WHERE message_id = $1
   AND status IN ('queued', 'held_reconnect', 'failed')
"""
DISPATCH_OF_SQL = """
SELECT dispatch_id FROM crm.dispatch_queue WHERE message_id = $1 AND status = 'queued' ORDER BY created_at DESC LIMIT 1
"""
SENDING_MAILBOX_SQL = """
SELECT channel_account_id FROM crm.channel_account
 WHERE deleted_at IS NULL AND (member_id = $1 OR member_id IS NULL)
 ORDER BY member_id NULLS LAST
 LIMIT 1
"""
REPLIES_SQL = """
SELECT saved_reply_id, member_id, locale, title, body_enc, updated_at FROM crm.saved_reply
 WHERE deleted_at IS NULL AND (member_id IS NULL OR member_id = $1)
 ORDER BY member_id NULLS FIRST, title
 LIMIT 200
"""
REPLY_SQL = "SELECT saved_reply_id, member_id FROM crm.saved_reply WHERE saved_reply_id = $1 AND deleted_at IS NULL"
INSERT_REPLY_SQL = """
INSERT INTO crm.saved_reply (saved_reply_id, member_id, locale, title, body_enc) VALUES ($1, $2, $3, $4, $5)
"""
UPDATE_REPLY_SQL = """
UPDATE crm.saved_reply SET locale = coalesce($2, locale), title = coalesce($3, title), body_enc = coalesce($4, body_enc)
 WHERE saved_reply_id = $1
"""
DELETE_REPLY_SQL = "DELETE FROM crm.saved_reply WHERE saved_reply_id = $1"


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _text(value: str | None, limit: int, code: str) -> str:
    text = (value or "").replace("\x00", "")
    if len(text) > limit:
        raise Invalid(code, f"at most {limit} characters")
    return text


# ── lists and reads ──────────────────────────────────────────────────────────────────────────────


async def list_conversations(
    ctx: TenantContext,
    *,
    trip_id: uuid.UUID | None = None,
    traveler_id: uuid.UUID | None = None,
    unmatched: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    rows = await ctx.conn.fetch(LIST_SQL, trip_id, traveler_id, unmatched, max(1, min(limit, MAX_LIST)))
    items = [
        {
            "conversation_id": str(r["conversation_id"]),
            "trip_id": str(r["trip_id"]) if r["trip_id"] else None,
            "traveler_id": str(r["traveler_id"]) if r["traveler_id"] else None,
            "channel": r["channel"],
            "last_message_at": _iso(r["last_message_at"]),
            "message_count": r["message_count"],
            "held_count": r["held_count"],
            "last_direction": r["last_direction"],
            "unmatched": r["trip_id"] is None and r["traveler_id"] is None,
        }
        for r in rows
    ]
    ctx.audit("list_view", "allowed", "conversation", object_ids=tuple(i["conversation_id"] for i in items))
    return {"conversations": items}


async def _conversation(ctx: TenantContext, conversation_id: uuid.UUID) -> Any:
    row = await ctx.conn.fetchrow(CONVERSATION_SQL, conversation_id)
    if row is None:
        raise NotFound("conversation_not_found")
    return row


async def _open(vault: Vault, message_id: uuid.UUID, column: str, blob: bytes | None) -> str | None:
    if blob is None:
        return None
    return await vault.decrypt("message", column, message_id, blob, purpose=PURPOSE)


async def conversation_messages(ctx: TenantContext, vault: Vault, conversation_id: uuid.UUID) -> dict[str, Any]:
    conv = await _conversation(ctx, conversation_id)
    out = []
    for r in await ctx.conn.fetch(MESSAGES_SQL, conversation_id, ctx.member_id):
        mid = r["message_id"]
        to_raw = await _open(vault, mid, "to_enc", r["to_enc"])
        out.append(
            {
                "message_id": str(mid),
                "direction": r["direction"],
                "status": r["status"],
                "sender_kind": r["sender_kind"],
                "author_member_id": str(r["author_member_id"]) if r["author_member_id"] else None,
                "subject": await _open(vault, mid, "subject_enc", r["subject_enc"]),
                "text": await _open(vault, mid, "body_enc", r["body_enc"]),
                "html": await _open(vault, mid, "body_html_enc", r["body_html_enc"]),
                "from": await _open(vault, mid, "from_enc", r["from_enc"]),
                "to": json.loads(to_raw) if to_raw else [],
                "attachments": [str(a) for a in r["attachments"] or []],
                "attachment_notes": list(r["attachment_notes"] or []),
                "failure_code": r["failure_code"],
                "imported": r["imported"],
                "match_method": r["match_method"],
                "occurred_at": _iso(r["occurred_at"]),
                "sent_at": _iso(r["sent_at"]),
                "updated_at": _iso(r["updated_at"]),
            }
        )
    ctx.audit(
        "view",
        "allowed",
        "conversation",
        str(conversation_id),
        fields=("subject", "body", "from", "to"),
        object_ids=tuple(m["message_id"] for m in out),
    )
    return {
        "conversation_id": str(conversation_id),
        "trip_id": str(conv["trip_id"]) if conv["trip_id"] else None,
        "traveler_id": str(conv["traveler_id"]) if conv["traveler_id"] else None,
        "messages": out,
    }


async def link(
    ctx: TenantContext, conversation_id: uuid.UUID, traveler_id: uuid.UUID, trip_id: uuid.UUID | None
) -> dict[str, Any]:
    conv = await _conversation(ctx, conversation_id)
    if conv["traveler_id"] is not None and conv["traveler_id"] != traveler_id:
        raise Conflict("already_linked", "this conversation already belongs to a client")
    if conv["trip_id"] is not None and trip_id is not None and conv["trip_id"] != trip_id:
        raise Conflict("already_linked", "this conversation already belongs to a trip")
    if not await ctx.conn.fetchval(TRAVELER_SQL, traveler_id):
        raise NotFound("traveler_not_found")
    if trip_id is not None and await ctx.conn.fetchrow(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    await ctx.conn.execute(LINK_SQL, conversation_id, traveler_id, trip_id)
    ctx.audit("conversation_link", "allowed", "conversation", str(conversation_id))
    return {
        "conversation_id": str(conversation_id),
        "traveler_id": str(traveler_id),
        "trip_id": str(trip_id) if trip_id else None,
    }


# ── drafts, send, undo ───────────────────────────────────────────────────────────────────────────


async def _target(ctx: TenantContext, conversation_id: uuid.UUID | None, trip_id: uuid.UUID | None) -> uuid.UUID:
    if conversation_id is not None:
        return (await _conversation(ctx, conversation_id))["conversation_id"]
    if trip_id is None:
        raise Invalid("target_required", "a conversation or a trip")
    if await ctx.conn.fetchrow(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    lead = await ctx.conn.fetchval(LEAD_SQL, trip_id)
    if lead is None:
        raise Conflict("no_lead_traveller", "the trip has no lead traveller to write to")
    return await threads.conversation_for(ctx, trip_id, lead)


async def save_draft(
    ctx: TenantContext,
    vault: Vault,
    draft_id: uuid.UUID,
    *,
    conversation_id: uuid.UUID | None,
    trip_id: uuid.UUID | None,
    subject: str | None,
    body: str | None,
) -> dict[str, Any]:
    subject_text = _text(subject, MAX_SUBJECT, "subject_too_long")
    body_text = _text(body, MAX_BODY, "body_too_long")
    subject_enc = (
        await vault.encrypt("message", "subject_enc", draft_id, subject_text, purpose=PURPOSE) if subject_text else None
    )
    body_enc = await vault.encrypt("message", "body_enc", draft_id, body_text, purpose=PURPOSE) if body_text else None
    mailbox = await ctx.conn.fetchval(SENDING_MAILBOX_SQL, ctx.member_id)
    row = await ctx.conn.fetchrow(DRAFT_SQL, draft_id)
    if row is None:
        target = await _target(ctx, conversation_id, trip_id)
        try:
            async with ctx.conn.transaction():
                updated = await ctx.conn.fetchval(
                    INSERT_DRAFT_SQL, draft_id, target, mailbox, ctx.member_id, subject_enc, body_enc
                )
        except asyncpg.UniqueViolationError:
            raise Conflict("draft_id_taken") from None
        return {
            "message_id": str(draft_id),
            "conversation_id": str(target),
            "status": "draft_autosaved",
            "updated_at": _iso(updated),
        }
    if row["author_member_id"] != ctx.member_id or row["status"] != "draft_autosaved":
        raise Conflict("not_a_draft", "only the author's own draft is edited")
    updated = await ctx.conn.fetchval(UPDATE_DRAFT_SQL, draft_id, subject_enc, body_enc, mailbox)
    return {
        "message_id": str(draft_id),
        "conversation_id": str(row["conversation_id"]),
        "status": "draft_autosaved",
        "updated_at": _iso(updated),
    }


async def discard_draft(ctx: TenantContext, draft_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(DRAFT_SQL, draft_id)
    if row is None:
        raise NotFound("draft_not_found")
    if row["author_member_id"] != ctx.member_id or row["status"] != "draft_autosaved":
        raise Conflict("not_a_draft", "only the author's own draft is discarded")
    await ctx.conn.execute(DELETE_DRAFT_SQL, draft_id)
    return {"message_id": str(draft_id), "status": "discarded"}


async def send(
    ctx: TenantContext,
    vault: Vault,
    message_id: uuid.UUID,
    *,
    conversation_id: uuid.UUID | None,
    trip_id: uuid.UUID | None,
    subject: str | None,
    body: str | None,
) -> dict[str, Any]:
    if not (body or "").strip():
        raise Invalid("body_required", "write a message first")
    draft = await save_draft(
        ctx, vault, message_id, conversation_id=conversation_id, trip_id=trip_id, subject=subject, body=body
    )
    conv = await _conversation(ctx, uuid.UUID(draft["conversation_id"]))
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
    row = await ctx.conn.fetchrow(DRAFT_SQL, message_id)
    if row["channel_account_id"] is None:
        raise Conflict("mailbox_not_connected", "connect a mailbox in Settings > Mailbox first")
    await ctx.conn.execute(QUEUE_SQL, message_id)
    queued = await dispatch.enqueue_message(
        ctx,
        traveler_id=conv["traveler_id"],
        channel="email",
        purpose="transactional",
        origin="user_action",
        trip_id=conv["trip_id"],
        message_id=message_id,
    )
    ctx.audit("message_send", "allowed", "message", str(message_id))
    return {
        "message_id": str(message_id),
        "conversation_id": str(conv["conversation_id"]),
        "status": "queued",
        "dispatch_id": queued["dispatch_id"],
        "undo_until": queued["not_before"],
    }


async def undo(ctx: TenantContext, message_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(DRAFT_SQL, message_id)
    if row is None:
        raise NotFound("message_not_found")
    if row["author_member_id"] != ctx.member_id:
        raise Forbidden("not_the_author", "only the member who sent it can undo it")
    dispatch_id = await ctx.conn.fetchval(DISPATCH_OF_SQL, message_id)
    if dispatch_id is None or row["status"] != "queued":
        raise Conflict("already_dispatched", "the message has left the queue")
    await dispatch.undo(ctx, dispatch_id)
    await ctx.conn.execute(BACK_TO_DRAFT_SQL, message_id)
    ctx.audit("message_undo", "allowed", "message", str(message_id))
    return {"message_id": str(message_id), "status": "draft_autosaved"}


async def back_to_draft(ctx: TenantContext, message_id: uuid.UUID) -> dict[str, Any]:
    """A held or failed message back into the composer (to edit, or to give up on it)."""
    row = await ctx.conn.fetchrow(DRAFT_SQL, message_id)
    if row is None:
        raise NotFound("message_not_found")
    if row["author_member_id"] != ctx.member_id or row["status"] not in ("held_reconnect", "failed"):
        raise Conflict("not_held", "only the author's held or failed message goes back to a draft")
    await ctx.conn.execute(BACK_TO_DRAFT_SQL, message_id)
    return {"message_id": str(message_id), "status": "draft_autosaved"}


# ── saved replies ────────────────────────────────────────────────────────────────────────────────


async def saved_replies(ctx: TenantContext, vault: Vault) -> dict[str, Any]:
    items = []
    for r in await ctx.conn.fetch(REPLIES_SQL, ctx.member_id):
        items.append(
            {
                "saved_reply_id": str(r["saved_reply_id"]),
                "scope": "agency" if r["member_id"] is None else "own",
                "locale": r["locale"],
                "title": r["title"],
                "body": await vault.decrypt(
                    "saved_reply", "body_enc", r["saved_reply_id"], r["body_enc"], purpose=PURPOSE
                ),
                "updated_at": _iso(r["updated_at"]),
            }
        )
    return {"saved_replies": items}


def _reply_scope(ctx: TenantContext, scope: str) -> uuid.UUID | None:
    if scope == "agency":
        require(ctx, Action.WORKFLOW_EDIT, object_kind="saved_reply")
        return None
    return ctx.member_id


async def create_reply(
    ctx: TenantContext, vault: Vault, *, scope: str, locale: str, title: str, body: str
) -> dict[str, Any]:
    member = _reply_scope(ctx, scope)
    reply_id = uuid.uuid4()
    blob = await vault.encrypt(
        "saved_reply", "body_enc", reply_id, _text(body, 20_000, "body_too_long"), purpose=PURPOSE
    )
    await ctx.conn.execute(INSERT_REPLY_SQL, reply_id, member, locale, title.strip(), blob)
    ctx.audit("saved_reply_create", "allowed", "saved_reply", str(reply_id))
    return {"saved_reply_id": str(reply_id), "scope": scope}


async def _own_reply(ctx: TenantContext, reply_id: uuid.UUID) -> Any:
    row = await ctx.conn.fetchrow(REPLY_SQL, reply_id)
    if row is None:
        raise NotFound("saved_reply_not_found")
    if row["member_id"] is None:
        require(ctx, Action.WORKFLOW_EDIT, object_kind="saved_reply", object_id=str(reply_id))
    elif row["member_id"] != ctx.member_id:
        raise NotFound("saved_reply_not_found")
    return row


async def update_reply(
    ctx: TenantContext, vault: Vault, reply_id: uuid.UUID, *, locale: str | None, title: str | None, body: str | None
) -> dict[str, Any]:
    await _own_reply(ctx, reply_id)
    blob = (
        await vault.encrypt("saved_reply", "body_enc", reply_id, _text(body, 20_000, "body_too_long"), purpose=PURPOSE)
        if body is not None
        else None
    )
    await ctx.conn.execute(UPDATE_REPLY_SQL, reply_id, locale, title.strip() if title else None, blob)
    return {"saved_reply_id": str(reply_id)}


async def delete_reply(ctx: TenantContext, reply_id: uuid.UUID) -> dict[str, Any]:
    await _own_reply(ctx, reply_id)
    await ctx.conn.execute(DELETE_REPLY_SQL, reply_id)
    ctx.audit("saved_reply_delete", "allowed", "saved_reply", str(reply_id))
    return {"saved_reply_id": str(reply_id), "status": "deleted"}
