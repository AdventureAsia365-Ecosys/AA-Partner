"""Queue an outbound message for a traveller and undo it (AA-942; lane §2.8, design §3.5, Fix B8).

A message caused by a user action waits ``UNDO_SECONDS`` (10 s) before the dispatcher may send it, so Undo can cancel
it; workflow and system messages have no delay. Queueing for an archived or trashed client is refused at once (409);
a client archived while the message waits is caught at send time by :mod:`app.dispatch.worker`.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.errors import Conflict, Invalid, NotFound
from app.jobs import queue as jobs
from app.jobs.kinds import DISPATCH
from app.tenancy import AnyContext

CHANNELS = ("email", "sms", "whatsapp")
PURPOSES = ("transactional", "marketing")
ORIGINS = ("user_action", "workflow", "system")
UNDO_SECONDS = 10
MAX_ATTEMPTS = 5

TRAVELER_SQL = "SELECT archived_at, deleted_at FROM crm.traveler WHERE traveler_id = $1"
INSERT_SQL = """
INSERT INTO crm.dispatch_queue (traveler_id, trip_id, channel, purpose, origin, message_ref, not_before)
VALUES ($1, $2, $3, $4, $5, $6, now() + make_interval(secs => $7))
RETURNING dispatch_id, not_before
"""
UNDO_SQL = """
UPDATE crm.dispatch_queue SET status = 'cancelled', gate_result = 'undone', decided_at = now()
 WHERE dispatch_id = $1 AND status = 'queued'
RETURNING dispatch_id
"""


async def enqueue_message(
    ctx: AnyContext,
    *,
    traveler_id: uuid.UUID,
    channel: str,
    purpose: str,
    origin: str,
    trip_id: uuid.UUID | None = None,
    message_ref: uuid.UUID | None = None,
) -> dict[str, Any]:
    if channel not in CHANNELS or purpose not in PURPOSES or origin not in ORIGINS:
        raise Invalid("invalid_dispatch", "channel, purpose or origin")
    row = await ctx.conn.fetchrow(TRAVELER_SQL, traveler_id)
    if row is None or row["deleted_at"] is not None:
        raise NotFound("traveler_not_found")
    if row["archived_at"] is not None:
        raise Conflict("traveler_archived", "unarchive the client before messaging them")
    delay = UNDO_SECONDS if origin == "user_action" else 0
    queued = await ctx.conn.fetchrow(INSERT_SQL, traveler_id, trip_id, channel, purpose, origin, message_ref, delay)
    await jobs.enqueue(
        ctx.conn,
        DISPATCH,
        {"tenant_id": str(ctx.tenant_id), "dispatch_id": str(queued["dispatch_id"])},
        idempotency_key=f"{DISPATCH}:{queued['dispatch_id']}",
        delay_seconds=delay,
        max_attempts=MAX_ATTEMPTS,
        created_by="dispatch",
    )
    return {"dispatch_id": str(queued["dispatch_id"]), "not_before": queued["not_before"].isoformat()}


async def undo(ctx: AnyContext, dispatch_id: uuid.UUID) -> dict[str, Any]:
    """Cancel a message that has not been sent yet ("Undo until sent")."""
    if await ctx.conn.fetchval(UNDO_SQL, dispatch_id) is None:
        raise Conflict("already_dispatched", "the message has left the queue")
    return {"dispatch_id": str(dispatch_id), "status": "cancelled"}
