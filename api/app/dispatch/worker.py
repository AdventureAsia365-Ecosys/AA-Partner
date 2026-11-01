"""``crm_dispatch``: gate one queued message at send time, then hand it to the sender (AA-942; lane §2.8).

The gate, in order (first match wins; the row ends ``dropped`` with the code as ``gate_result``):

1. the traveller is in the trash -> ``traveler_trashed``; the trip of the message is in the trash -> ``trip_trashed``;
2. the traveller is **archived** -> ``archived`` (R23, card R "an archived contact's queued message is dropped");
3. a marketing message without a current marketing consent (latest ``crm.consent`` row of purpose ``marketing``) ->
   ``no_consent``; AA-958: an SMS / WhatsApp message to a client who texted STOP on that channel (any of their
   conversations on it has ``opted_out_at``) -> ``no_consent``.

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

**The send marker (AA-993 G4b, crm-023, design S252 Q9).** The provider APIs take no idempotency key, so a run stopped
between the provider call and the commit of its decision (a deploy releasing the job, a crash) must never call the
provider again. The handler works in two transactions:

1. **open**: lock the row, gate it, and insert one ``crm.dispatch_attempt`` row (``job_attempt``); commit. Nothing is
   sent yet.
2. **send**: lock the row again (an Undo or a gate change in between closes the attempt ``not_sent``), call the sender,
   and in the same transaction decide the row and close the attempt with the sender's code. A sender that raises
   closes the attempt ``retry`` before the job is retried (as before G4b: the sender said it failed).

A run that finds an **open** attempt (a previous run committed step 1 and never its decision) does not call the
sender: the row is held ``send_uncertain`` and its message marked ``failed`` / ``send_uncertain``, so a person checks
the agency's Sent folder before sending again. A run cancelled before it called the sender (the worker's shutdown
cancel) closes its attempt ``not_sent`` in its own short transaction, so the re-claimed job sends normally. With the
marker, ``crm_dispatch`` is no longer a deploy-blocking kind (:data:`app.jobs.kinds.DEPLOY_BLOCKING_KINDS`).
"""

from __future__ import annotations

import asyncio
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
         ORDER BY c.at DESC, c.created_at DESC, c.consent_id DESC LIMIT 1) AS marketing_ok,
       (d.channel IN ('sms', 'whatsapp') AND EXISTS (
            SELECT 1 FROM crm.conversation cv
             WHERE cv.traveler_id = d.traveler_id AND cv.channel = d.channel AND cv.opted_out_at IS NOT NULL
               AND cv.deleted_at IS NULL)) AS text_opted_out
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
# AA-993 G4b (crm-023): the send marker. One open attempt per dispatch row (dispatch_attempt_open_uq).
OPEN_ATTEMPT_SQL = "SELECT attempt_id FROM crm.dispatch_attempt WHERE dispatch_id = $1 AND decided_at IS NULL"
INSERT_ATTEMPT_SQL = "INSERT INTO crm.dispatch_attempt (dispatch_id, job_attempt) VALUES ($1, $2) RETURNING attempt_id"
CLOSE_ATTEMPT_SQL = """
UPDATE crm.dispatch_attempt SET decided_at = now(), outcome = $2 WHERE attempt_id = $1 AND decided_at IS NULL
"""
UNCERTAIN = "send_uncertain"
# The sender's answer -> (dispatch status, gate_result).
OUTCOMES = {
    "sent": ("sent", "sent"),
    "held_reconnect": ("held", "held_reconnect"),
    "no_message": ("held", "no_message"),
    "send_failed": ("dropped", "send_failed"),
}


class Sender(Protocol):
    async def send(self, ctx: SystemContext, dispatch_id: uuid.UUID, channel: str, message_id: uuid.UUID | None) -> str:
        """Deliver the row's message (CRM-17 e-mail; CRM-28 SMS / WhatsApp through
        :class:`app.messaging.sender.ChannelSender`). Returns a key of :data:`OUTCOMES`; raise to
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
    if row.get("text_opted_out"):  # AA-958: the client texted STOP on this SMS / WhatsApp channel
        return "no_consent"
    return None


def _result(dispatch_id: uuid.UUID, status: str, code: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"dispatch_id": str(dispatch_id), "status": status}
    if code is not None:
        out["reason_code"] = code
    return out


async def _drop(ctx: SystemContext, row: Any, dispatch_id: uuid.UUID, code: str) -> dict[str, Any]:
    await ctx.conn.execute(DECIDE_SQL, dispatch_id, "dropped", code)
    if row["message_id"] is not None:
        await ctx.conn.execute(MESSAGE_FAILED_SQL, row["message_id"], code)
    ctx.audit("dispatch", "denied", "dispatch_queue", str(dispatch_id), reason=code)
    log.info("dispatch_dropped reason=%s", code)
    return _result(dispatch_id, "dropped", code)


async def _open(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, dispatch_id: uuid.UUID, job: Job, sender: Sender | None
) -> uuid.UUID | dict[str, Any]:
    """Transaction 1: the gate, then the committed attempt. Returns the attempt id, or the job result when the row is
    decided here (skipped, dropped, held no_sender, held send_uncertain)."""
    async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
        row = await ctx.conn.fetchrow(ROW_SQL, dispatch_id)
        if row is None or row["status"] != "queued":
            return _result(dispatch_id, "skipped")
        stale = await ctx.conn.fetchval(OPEN_ATTEMPT_SQL, dispatch_id)
        if stale is not None:
            # A previous run committed its attempt and never its decision: the provider may have the message.
            await ctx.conn.execute(CLOSE_ATTEMPT_SQL, stale, UNCERTAIN)
            await ctx.conn.execute(DECIDE_SQL, dispatch_id, "held", UNCERTAIN)
            if row["message_id"] is not None:
                await ctx.conn.execute(MESSAGE_FAILED_SQL, row["message_id"], UNCERTAIN)
            ctx.audit("dispatch", "allowed", "dispatch_queue", str(dispatch_id), reason=UNCERTAIN)
            log.warning("dispatch_send_uncertain")
            return _result(dispatch_id, "held", UNCERTAIN)
        drop = gate(row)
        if drop is not None:
            return await _drop(ctx, row, dispatch_id, drop)
        if sender is None:
            await ctx.conn.execute(DECIDE_SQL, dispatch_id, "held", "no_sender")
            ctx.audit("dispatch", "allowed", "dispatch_queue", str(dispatch_id), reason="no_sender")
            return _result(dispatch_id, "held", "no_sender")
        attempt_id: uuid.UUID = await ctx.conn.fetchval(INSERT_ATTEMPT_SQL, dispatch_id, job.attempt)
        return attempt_id


async def _close_alone(pool: asyncpg.Pool, tenant_id: uuid.UUID, attempt_id: uuid.UUID, outcome: str) -> None:
    """Close an attempt in a transaction of its own (a run cancelled before it called the sender). Best effort: an
    attempt left open only makes the next run hold the row send_uncertain, never send it twice."""
    try:
        async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
            await ctx.conn.execute(CLOSE_ATTEMPT_SQL, attempt_id, outcome)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("dispatch_attempt_close_failed outcome=%s error=%s", outcome, type(exc).__name__)


async def _send(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, dispatch_id: uuid.UUID, attempt_id: uuid.UUID, job: Job, sender: Sender
) -> dict[str, Any]:
    """Transaction 2: lock the row again, call the sender, decide the row and close the attempt together."""
    called = False
    retry: Exception | None = None
    try:
        async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
            row = await ctx.conn.fetchrow(ROW_SQL, dispatch_id)
            if row is None or row["status"] != "queued":  # undone while unlocked
                await ctx.conn.execute(CLOSE_ATTEMPT_SQL, attempt_id, "not_sent")
                return _result(dispatch_id, "skipped")
            drop = gate(row)
            if drop is not None:  # archived, trashed or consent withdrawn while unlocked
                await ctx.conn.execute(CLOSE_ATTEMPT_SQL, attempt_id, "not_sent")
                return await _drop(ctx, row, dispatch_id, drop)
            called = True
            try:
                async with ctx.conn.transaction():  # a savepoint: the sender's partial writes roll back
                    outcome = await sender.send(ctx, dispatch_id, row["channel"], row["message_id"])
            except (Forbidden, NotFound):
                raise
            except Exception as exc:  # noqa: BLE001 — re-raised after the commit below, or the last attempt fails
                if job.attempt < job.max_attempts:
                    # Retried with backoff; the sender raised, so this attempt is closed and the next run sends.
                    await ctx.conn.execute(CLOSE_ATTEMPT_SQL, attempt_id, "retry")
                    retry = exc
                else:
                    log.warning("dispatch_retries_exhausted")
                    outcome = "send_failed"
                    if row["message_id"] is not None:
                        await ctx.conn.execute(MESSAGE_FAILED_SQL, row["message_id"], "retries_exhausted")
            if retry is None:
                status, code = OUTCOMES.get(outcome, ("held", "no_message"))
                await ctx.conn.execute(DECIDE_SQL, dispatch_id, status, code)
                await ctx.conn.execute(CLOSE_ATTEMPT_SQL, attempt_id, code)
                ctx.audit(
                    "dispatch",
                    "denied" if status == "dropped" else "allowed",
                    "dispatch_queue",
                    str(dispatch_id),
                    reason=code,
                )
                result = _result(dispatch_id, "sent") if status == "sent" else _result(dispatch_id, status, code)
    except asyncio.CancelledError:
        # The worker's shutdown cancel. Before the sender was called nothing left: close the attempt so the re-claimed
        # job sends. After it, the attempt stays open and the re-run holds the row send_uncertain.
        if not called:
            await asyncio.shield(_close_alone(pool, tenant_id, attempt_id, "not_sent"))
        raise
    if retry is not None:
        raise retry
    return result


def build_handler(sender: Sender | None = None) -> Handler:
    async def dispatch(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        try:
            tenant_id = uuid.UUID(str(job.payload["tenant_id"]))
            dispatch_id = uuid.UUID(str(job.payload["dispatch_id"]))
        except (KeyError, ValueError):
            raise PermanentError("bad_payload") from None
        try:
            opened = await _open(pool, tenant_id, dispatch_id, job, sender)
            if isinstance(opened, dict):
                return opened
            if sender is None:  # pragma: no cover — _open decides the row when there is no sender
                raise PermanentError("no_sender")
            return await _send(pool, tenant_id, dispatch_id, opened, job, sender)
        except (Forbidden, NotFound):
            # A locked, offboarding or unknown agency sends nothing; its queue is erased with it.
            return _result(dispatch_id, "skipped", "tenant_inactive")

    return dispatch
