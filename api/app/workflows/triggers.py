"""Where workflow runs start and stop (AA-959; design v3 M10, Fix B8 Undo, Fix C3 "cancel stops runs").

Called inside the transaction of the change that triggers them, so a rolled-back change starts nothing:

- :func:`on_stage_enter`: a trip entered a stage (:mod:`app.stages` moves, the date rule, a new inquiry). Keyed
  ``stage:<history id>``. A member's move starts the run after the Undo window (``next_at`` = now + :data:`UNDO_DELAY`):
  Undo of that move stops it before its first step (:func:`stop_for_trigger`, Fix B8).
- :func:`on_trip_event`: a trip event (``crm.metric_event``, :mod:`app.proposals.events`). Keyed ``event:<event id>``.
- :func:`on_message`: the AA-958 seam (:mod:`app.messaging.events`): ``message.received`` / ``message.sent`` on a
  trip. Keyed ``event:<message id>``; :func:`install` subscribes it.
- :func:`apply_manual`: a member applies a manual rule (a task list, R9) to trips. Keyed ``manual:<request id>:<trip>``.
- Date and special-date triggers start in the hourly tick (:mod:`app.workflows.tick`).

A run that starts is a ``crm.workflow_run`` row (``ON CONFLICT DO NOTHING`` on the trigger key: card E, nothing starts
twice) and one ``crm_workflow_run`` job, due when the run is (:mod:`app.workflows.runner`). Conditions are checked
here (the trip as it is now); a trip that does not match starts nothing.

Stops: :func:`stop_trip_runs` (the trip cancel's hook, :data:`app.cancellation.CANCEL_HOOKS`; dry run = the count the
cancel dialog shows) and :func:`stop_for_trigger` (Undo). A run also stops itself when its trip is lost, trashed or
cancelling, or its client archived (:mod:`app.workflows.engine`).

A failure to start a run never fails the change that triggered it: each start runs in a savepoint, and an error is
logged with its class only.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg

from app.jobs import queue as jobs
from app.jobs.kinds import WORKFLOW_RUN
from app.tenancy import AnyContext
from app.workflows import facts as wf_facts
from app.workflows import model

log = logging.getLogger(__name__)

UNDO_DELAY = 20  # seconds: the stage Undo window (app.stages.UNDO_SECONDS, 15 s) plus a margin
RUN_ATTEMPTS = 5

RULES_SQL = """
SELECT workflow_rule_id, version, trigger_kind, trigger_value, conditions
  FROM crm.workflow_rule
 WHERE status = 'active' AND deleted_at IS NULL AND trigger_kind = $1 AND trigger_value = $2
 ORDER BY created_at, workflow_rule_id
 LIMIT 50
"""
START_SQL = """
INSERT INTO crm.workflow_run (workflow_rule_id, rule_version, trip_id, traveler_id, trigger_key, next_at)
VALUES ($1, $2, $3, $4, $5, now() + make_interval(secs => $6))
ON CONFLICT (tenant_id, workflow_rule_id, trigger_key) DO NOTHING
RETURNING workflow_run_id, next_at
"""
STOP_TRIP_SQL = """
UPDATE crm.workflow_run SET status = 'stopped', stop_reason = $2, next_at = NULL, finished_at = now()
 WHERE trip_id = $1 AND status = 'active'
"""
COUNT_TRIP_SQL = "SELECT count(*)::int FROM crm.workflow_run WHERE trip_id = $1 AND status = 'active'"
STOP_KEY_SQL = """
UPDATE crm.workflow_run SET status = 'stopped', stop_reason = $3, next_at = NULL, finished_at = now()
 WHERE trip_id = $1 AND trigger_key = $2 AND status = 'active'
"""


def _conditions(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw or {})


def _rows(status: str) -> int:
    tail = str(status).split()[-1] if status else ""
    return int(tail) if tail.isdigit() else 0


async def start(
    ctx: AnyContext,
    rule: Any,
    *,
    trip_id: uuid.UUID | None,
    traveler_id: uuid.UUID | None,
    key: str,
    delay_s: float = 0,
) -> uuid.UUID | None:
    """One run of ``rule`` for the subject, unless the key already started one. Queues its first job."""
    row = await ctx.conn.fetchrow(
        START_SQL, rule["workflow_rule_id"], rule["version"], trip_id, traveler_id, key, delay_s
    )
    if row is None:
        return None
    run_id = row["workflow_run_id"]
    await jobs.enqueue(
        ctx.conn,
        WORKFLOW_RUN,
        {"tenant_id": str(ctx.tenant_id), "workflow_run_id": str(run_id)},
        idempotency_key=f"{WORKFLOW_RUN}:{run_id}:0",
        delay_seconds=delay_s,
        max_attempts=RUN_ATTEMPTS,
        created_by="workflow",
    )
    ctx.audit("workflow_start", "allowed", "workflow_run", str(run_id), reason=key.split(":", 1)[0])
    return run_id


async def _start_matching(
    ctx: AnyContext, kind: str, value: str, trip_id: uuid.UUID, key: str, delay_s: float = 0
) -> list[uuid.UUID]:
    rules = await ctx.conn.fetch(RULES_SQL, kind, value)
    if not rules:
        return []
    facts = await wf_facts.trip(ctx, trip_id)
    if facts is None or model.stop_reason(facts) not in (None, "trip_lost" if value == "lost" else None):
        return []
    started: list[uuid.UUID] = []
    for rule in rules:
        if model.matches(_conditions(rule["conditions"]), facts) is not None:
            continue
        run_id = await start(ctx, rule, trip_id=trip_id, traveler_id=None, key=key, delay_s=delay_s)
        if run_id is not None:
            started.append(run_id)
    return started


async def _safely(ctx: AnyContext, what: str, kind: str, value: str, trip_id: uuid.UUID, key: str, delay: float) -> int:
    try:
        async with ctx.conn.transaction():
            return len(await _start_matching(ctx, kind, value, trip_id, key, delay))
    except (asyncpg.PostgresError, ValueError) as exc:
        log.error("workflow_start_failed trigger=%s error_class=%s", what, type(exc).__name__)
        return 0


async def on_stage_enter(
    ctx: AnyContext, trip_id: uuid.UUID, stage: str, history_id: uuid.UUID, *, user_action: bool = False
) -> int:
    """A trip entered ``stage`` (the history row of the move). Returns how many runs started."""
    if stage == "cancelled":
        return 0
    delay = UNDO_DELAY if user_action else 0
    return await _safely(ctx, "stage_enter", "stage_enter", stage, trip_id, f"stage:{history_id}", delay)


async def on_trip_event(ctx: AnyContext, trip_id: uuid.UUID, name: str, event_id: uuid.UUID) -> int:
    """A trip event was recorded. Message events come through :func:`on_message` (the AA-958 seam) instead."""
    if name not in model.EVENTS or name.startswith("message."):
        return 0
    return await _safely(ctx, "event", "event", name, trip_id, f"event:{event_id}", 0)


async def on_message(ctx: AnyContext, event: Any) -> None:
    """The AA-958 listener (:class:`app.messaging.events.MessageEvent`): a text message arrived or left on a trip."""
    if event.trip_id is None:
        return
    name = "message.received" if event.kind == "received" else "message.sent"
    await _safely(ctx, "message", "event", name, event.trip_id, f"event:{event.message_id}", 0)


async def stop_trip_runs(ctx: AnyContext, trip_id: uuid.UUID, dry_run: bool, *, reason: str = "trip_cancelled") -> int:
    """Every active run of the trip stops (the trip cancel, Fix C3). Dry run: how many would."""
    if dry_run:
        return int(await ctx.conn.fetchval(COUNT_TRIP_SQL, trip_id))
    return _rows(await ctx.conn.execute(STOP_TRIP_SQL, trip_id, reason))


async def stop_for_trigger(ctx: AnyContext, trip_id: uuid.UUID, key: str, reason: str = "undone") -> int:
    """The runs a trigger started stop (Undo of the stage move that started them)."""
    return _rows(await ctx.conn.execute(STOP_KEY_SQL, trip_id, key, reason))


async def _cancel_hook(ctx: Any, trip_id: uuid.UUID, dry_run: bool) -> int:
    return await stop_trip_runs(ctx, trip_id, dry_run)


_INSTALLED: list[bool] = []


def install() -> None:
    """Register the trip-cancel hook and the AA-958 message listener (once per process; app.main calls it)."""
    if _INSTALLED:
        return
    from app import cancellation
    from app.messaging import events as message_events

    cancellation.register_cancel_hook("workflows", _cancel_hook)
    message_events.subscribe(on_message)
    _INSTALLED.append(True)
