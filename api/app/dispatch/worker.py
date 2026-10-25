"""``crm_dispatch``: gate one queued message at send time, then hand it to the sender (AA-942; lane §2.8).

The gate, in order (first match wins; the row ends ``dropped`` with the code as ``gate_result``):

1. the traveller is in the trash -> ``traveler_trashed``; the trip of the message is in the trash -> ``trip_trashed``;
2. the traveller is **archived** -> ``archived`` (R23, card R "an archived contact's queued message is dropped");
3. a marketing message without a current marketing consent (latest ``crm.consent`` row of purpose ``marketing``) ->
   ``no_consent``.

A row that passes goes to the sender: ``sent``; with no sender configured -> ``held`` / ``no_sender``. AA-945
(CRM-17): the e-mail sender (:mod:`app.mail.sender`) answers a gate code: ``sent``; ``held_reconnect`` (the agency's
mailbox is disconnected: held, re-queued on reconnect, never sent another way, card R); ``no_message`` (nothing to
send: a row from before CRM-17, held); ``send_failed`` (the provider refused it for good: dropped). A dropped row's
message (``crm.dispatch_queue.message_id``, crm-015) is marked ``failed`` with the code. When the sender keeps raising
(a provider that stays unreachable) and the job's **last attempt** fails too, the row is dropped ``send_failed`` and its
message marked ``failed`` (``retries_exhausted``), so neither stays ``queued`` with no way out: the author sees the
failed chip and "Edit and send again". A row that is no longer
``queued`` (undone, already decided) is skipped. One audit event per decision (action ``dispatch``; ``denied`` with the
gate code for a drop). The job result is ids and codes only.
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
SELECT d.dispatch_id, d.status, d.channel, d.purpose, d.message_ref, d.message_id, d.traveler_id, d.trip_id,
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
# AA-945: the message of a dropped row does not stay queued.
MESSAGE_FAILED_SQL = """
UPDATE crm.message SET status = 'failed', failure_code = $2 WHERE message_id = $1 AND status = 'queued'
"""
# The sender's answer -> (dispatch status, gate_result).
OUTCOMES = {
    "sent": ("sent", "sent"),
    "held_reconnect": ("held", "held_reconnect"),
    "no_message": ("held", "no_message"),
    "send_failed": ("dropped", "send_failed"),
}


class Sender(Protocol):
    async def send(self, ctx: SystemContext, dispatch_id: uuid.UUID, channel: str, message_id: uuid.UUID | None) -> str:
        """Deliver the row's message (CRM-17 e-mail; CRM-28 later). Returns a key of :data:`OUTCOMES`; raise to
        retry the job."""
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
                    if row["message_id"] is not None:
                        await ctx.conn.execute(MESSAGE_FAILED_SQL, row["message_id"], drop)
                    ctx.audit("dispatch", "denied", "dispatch_queue", str(dispatch_id), reason=drop)
                    log.info("dispatch_dropped reason=%s", drop)
                    return {"dispatch_id": str(dispatch_id), "status": "dropped", "reason_code": drop}
                if sender is None:
                    await ctx.conn.execute(DECIDE_SQL, dispatch_id, "held", "no_sender")
                    ctx.audit("dispatch", "allowed", "dispatch_queue", str(dispatch_id), reason="no_sender")
                    return {"dispatch_id": str(dispatch_id), "status": "held", "reason_code": "no_sender"}
                try:
                    async with ctx.conn.transaction():  # a savepoint: the sender's partial writes roll back
                        outcome = await sender.send(ctx, dispatch_id, row["channel"], row["message_id"])
                except (Forbidden, NotFound):
                    raise
                except Exception:
                    if job.attempt < job.max_attempts:
                        raise  # retried with backoff
                    log.warning("dispatch_retries_exhausted")
                    outcome = "send_failed"
                    if row["message_id"] is not None:
                        await ctx.conn.execute(MESSAGE_FAILED_SQL, row["message_id"], "retries_exhausted")
                status, code = OUTCOMES.get(outcome, ("held", "no_message"))
                await ctx.conn.execute(DECIDE_SQL, dispatch_id, status, code)
                ctx.audit(
                    "dispatch",
                    "denied" if status == "dropped" else "allowed",
                    "dispatch_queue",
                    str(dispatch_id),
                    reason=code,
                )
                if status == "sent":
                    return {"dispatch_id": str(dispatch_id), "status": "sent"}
                return {"dispatch_id": str(dispatch_id), "status": status, "reason_code": code}
        except (Forbidden, NotFound):
            # A locked, offboarding or unknown agency sends nothing; its queue is erased with it.
            return {"dispatch_id": str(dispatch_id), "status": "skipped", "reason_code": "tenant_inactive"}

    return dispatch
