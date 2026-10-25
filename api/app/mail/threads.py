"""Thread an ingested mail into a conversation (AA-945; lane §6.5 step 3; card E).

In order, first match wins:

1. **reply_header**: an In-Reply-To / References hash equals the Message-ID hash of a stored message of the agency ->
   that message's conversation;
2. **trip_token**: ``[ref:<token>]`` in the subject (or ``local+<token>@``) is a live trip's ``mail_token`` (card E:
   a forwarded thread without ``References``) -> that trip's conversation with the client;
3. **sender**: the From address's blind index (the traveller e-mail index) names exactly one live client -> their most
   recent open trip, if any, and their conversation;
4. **unmatched**: an Unmatched conversation (no client, no trip) from the same sender, else a new one.

An agency's own outgoing mail copied to the forward-in address (a reply an agent wrote in Gmail and BCC'd) is matched
the same way, by its headers or token, else by the first recipient that is a client.

Ids and codes only leave this module; the database lookups use hashes and blind indexes, never an address in clear.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.tenancy import AnyContext

OPEN_STAGES = ("inquiry", "qualifying", "proposal", "booked", "pre_departure", "travelling")

BY_HEADER_SQL = """
SELECT c.conversation_id, c.trip_id, c.traveler_id
  FROM crm.message m JOIN crm.conversation c ON c.conversation_id = m.conversation_id
 WHERE m.rfc_message_id_hash = ANY($1::bytea[]) AND c.deleted_at IS NULL AND m.deleted_at IS NULL
 ORDER BY m.occurred_at DESC
 LIMIT 1
"""
TRIP_BY_MAIL_REF_SQL = "SELECT trip_id FROM crm.trip WHERE mail_token = $1 AND deleted_at IS NULL"
TRAVELER_BY_BIDX_SQL = """
SELECT traveler_id FROM crm.traveler WHERE email_bidx = $1 AND deleted_at IS NULL ORDER BY created_at LIMIT 2
"""
ON_TRIP_SQL = "SELECT 1 FROM crm.trip_traveler WHERE trip_id = $1 AND traveler_id = $2"
OPEN_TRIP_SQL = """
SELECT t.trip_id FROM crm.trip_traveler tt JOIN crm.trip t ON t.trip_id = tt.trip_id
 WHERE tt.traveler_id = $1 AND t.deleted_at IS NULL AND t.stage = ANY($2::text[])
 ORDER BY t.updated_at DESC, t.trip_id
 LIMIT 1
"""
CONVERSATION_SQL = """
SELECT conversation_id FROM crm.conversation
 WHERE channel = 'email' AND deleted_at IS NULL
   AND trip_id IS NOT DISTINCT FROM $1 AND traveler_id IS NOT DISTINCT FROM $2
 ORDER BY last_message_at DESC, conversation_id
 LIMIT 1
"""
UNMATCHED_SQL = """
SELECT c.conversation_id FROM crm.conversation c
 WHERE c.trip_id IS NULL AND c.traveler_id IS NULL AND c.channel = 'email' AND c.deleted_at IS NULL
   AND EXISTS (SELECT 1 FROM crm.message m WHERE m.conversation_id = c.conversation_id AND m.from_bidx = $1)
 ORDER BY c.last_message_at DESC
 LIMIT 1
"""
NEW_CONVERSATION_SQL = """
INSERT INTO crm.conversation (traveler_id, trip_id, channel) VALUES ($1, $2, 'email') RETURNING conversation_id
"""


@dataclass(frozen=True)
class Thread:
    conversation_id: uuid.UUID
    trip_id: uuid.UUID | None
    traveler_id: uuid.UUID | None
    match_method: str


async def _traveler(ctx: AnyContext, bidx: bytes | None) -> uuid.UUID | None:
    if bidx is None:
        return None
    rows = await ctx.conn.fetch(TRAVELER_BY_BIDX_SQL, bidx)
    return rows[0]["traveler_id"] if len(rows) == 1 else None  # two clients share it: do not guess


async def conversation_for(ctx: AnyContext, trip_id: uuid.UUID | None, traveler_id: uuid.UUID | None) -> uuid.UUID:
    """The latest e-mail conversation of exactly this (trip, client) pair, created when there is none."""
    found = await ctx.conn.fetchval(CONVERSATION_SQL, trip_id, traveler_id)
    if found is not None:
        return found
    return await ctx.conn.fetchval(NEW_CONVERSATION_SQL, traveler_id, trip_id)


async def thread(
    ctx: AnyContext,
    *,
    reference_hashes: list[bytes],
    trip_token: str | None,
    client_bidxs: list[bytes],
    sender_bidx: bytes | None,
) -> Thread:
    """``client_bidxs``: the blind indexes of the addresses that may be the client (the sender of an inbound mail,
    the recipients of the agency's own copied mail); ``sender_bidx`` groups Unmatched mail by sender."""
    if reference_hashes:
        row: Any = await ctx.conn.fetchrow(BY_HEADER_SQL, reference_hashes)
        if row is not None:
            return Thread(row["conversation_id"], row["trip_id"], row["traveler_id"], "reply_header")
    traveler_id = None
    for bidx in client_bidxs:
        traveler_id = await _traveler(ctx, bidx)
        if traveler_id is not None:
            break
    if trip_token:
        trip_id = await ctx.conn.fetchval(TRIP_BY_MAIL_REF_SQL, trip_token)
        if trip_id is not None:
            on_trip = traveler_id is not None and await ctx.conn.fetchval(ON_TRIP_SQL, trip_id, traveler_id)
            client = traveler_id if on_trip else None
            return Thread(await conversation_for(ctx, trip_id, client), trip_id, client, "trip_token")
    if traveler_id is not None:
        trip_id = await ctx.conn.fetchval(OPEN_TRIP_SQL, traveler_id, list(OPEN_STAGES))
        return Thread(await conversation_for(ctx, trip_id, traveler_id), trip_id, traveler_id, "sender")
    if sender_bidx is not None:
        found = await ctx.conn.fetchval(UNMATCHED_SQL, sender_bidx)
        if found is not None:
            return Thread(found, None, None, "unmatched")
    return Thread(await ctx.conn.fetchval(NEW_CONVERSATION_SQL, None, None), None, None, "unmatched")
