"""``crm_dispatch``: gate one queued message at send time, then hand it to the sender (AA-942; lane §2.8).

The gate, in order (first match wins; the row ends ``dropped`` with the code as ``gate_result``):

1. the traveller is in the trash -> ``traveler_trashed``; the trip of the message is in the trash -> ``trip_trashed``;
2. the traveller is **archived** -> ``archived`` (R23, card R "an archived contact's queued message is dropped");
3. a marketing message without a current marketing consent (latest ``crm.consent`` row of purpose ``marketing``) ->
   ``no_consent``.

A row that passes goes to the sender: ``sent``; with no sender configured (until CRM-17 / CRM-28) -> ``held`` /
``no_sender``. A row that is no longer ``queued`` (undone, already decided) is skipped. One audit event per decision
(action ``dispatch``; ``denied`` with the gate code for a drop). The job result is ids and codes only.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol

import asyncpg

from app.errors import Forbidden, NotFound
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.tenancy import SystemContext, system_tx

log = logging.getLogger(__name__)
ACTOR = "job:crm_dispatch"

ROW_SQL = """
SELECT d.dispatch_id, d.status, d.channel, d.purpose, d.message_ref, d.traveler_id, d.trip_id,
       t.archived_at, t.deleted_at AS traveler_deleted_at, tr.deleted_at AS trip_deleted_at,
       (SELECT c.value FROM crm.consent c WHERE c.traveler_id = d.traveler_id AND c.purpose = 'marketing'
         ORDER BY c.at DESC, c.created_at DESC, c.consent_id DESC LIMIT 1) AS marketing_ok
  FROM crm.dispatch_queue d
  JOIN crm.traveler t ON t.traveler_id = d.traveler_id
  LEFT JOIN crm.trip tr ON tr.trip_id = d.trip_id
 WHERE d.dispatch_id = $1
   FOR UPDATE OF d
"""
DECIDE_SQL = """
UPDATE crm.dispatch_queue SET status = $2, gate_result = $3, decided_at = now(), attempts = attempts + 1
 WHERE dispatch_id = $1
"""


class Sender(Protocol):
    async def send(
        self, ctx: SystemContext, dispatch_id: uuid.UUID, channel: str, message_ref: uuid.UUID | None
    ) -> None:
        """Deliver the message (CRM-17 / CRM-28). Raise to retry the job."""
        ...


def gate(row: Any) -> str | None:
    """The drop code for a queued row, or None when it may be sent."""
    if row["traveler_deleted_at"] is not None:
        return "traveler_trashed"
    if row["trip_deleted_at"] is not None:
        return "trip_trashed"
    if row["archived_at"] is not None:
        return "archived"
    if row["purpose"] == "marketing" and row["marketing_ok"] is not True:
        return "no_consent"
    return None


def build_handler(sender: Sender | None = None) -> Handler:
    async def dispatch(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        try:
            tenant_id = uuid.UUID(str(job.payload["tenant_id"]))
            dispatch_id = uuid.UUID(str(job.payload["dispatch_id"]))
        except (KeyError, ValueError):
            raise PermanentError("bad_payload") from None
        try:
            async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                row = await ctx.conn.fetchrow(ROW_SQL, dispatch_id)
                if row is None or row["status"] != "queued":
                    return {"dispatch_id": str(dispatch_id), "status": "skipped"}
                drop = gate(row)
                if drop is not None:
                    await ctx.conn.execute(DECIDE_SQL, dispatch_id, "dropped", drop)
                    ctx.audit("dispatch", "denied", "dispatch_queue", str(dispatch_id), reason=drop)
                    log.info("dispatch_dropped reason=%s", drop)
                    return {"dispatch_id": str(dispatch_id), "status": "dropped", "reason_code": drop}
                if sender is None:
                    await ctx.conn.execute(DECIDE_SQL, dispatch_id, "held", "no_sender")
                    ctx.audit("dispatch", "allowed", "dispatch_queue", str(dispatch_id), reason="no_sender")
                    return {"dispatch_id": str(dispatch_id), "status": "held", "reason_code": "no_sender"}
                await sender.send(ctx, dispatch_id, row["channel"], row["message_ref"])
                await ctx.conn.execute(DECIDE_SQL, dispatch_id, "sent", "sent")
                ctx.audit("dispatch", "allowed", "dispatch_queue", str(dispatch_id), reason="sent")
                return {"dispatch_id": str(dispatch_id), "status": "sent"}
        except (Forbidden, NotFound):
            # A locked, offboarding or unknown agency sends nothing; its queue is erased with it.
            return {"dispatch_id": str(dispatch_id), "status": "skipped", "reason_code": "tenant_inactive"}

    return dispatch
