"""The trip stage engine (card CRM-15, design v3 §2.7 F2, §3.3 group D, §3.5): moves, gates, overrides, Undo, bulk
moves, cancellation and ``waiting_on``. Every function takes the open context and never opens one; the routes are in
:mod:`app.routers.pipeline`, the hourly date rule in :mod:`app.stages_job`.

The database is the judge (crm-012 ``crm.enforce_stage_transition``); this module answers first with a useful 409:

- **Edges** (:data:`ALLOWED_MOVES`, the crm-012 rows): ``lost`` only before ``booked``; ``cancelled`` only through the
  cancel flow (:func:`cancel`, from booked / pre_departure / travelling); the generic move refuses it with 409
  ``use_cancel_flow``. Anything else is 409 ``transition_not_allowed``.
- **Gates** (:data:`GATES`): qualifying -> proposal needs Stage 2 at least 80 % or an override with a logged reason,
  else 409 ``stage2_incomplete`` ``{s2, missing}``; proposal -> booked needs a paid deposit of the accepted proposal
  and booked -> pre_departure needs Stage 3 complete (``s3`` = 100: every traveller's Stage 3 details and signed
  waiver, :mod:`app.portal.progress`) since CRM-18 (AA-946), else 409 ``gate_unmet`` unless an override
  (``manual_booking`` / ``manual_stage3``) is given. An override sets ``app.gate_override`` for the one UPDATE and is
  written to ``trip_stage_history`` (``override_reason_code``) and the audit.
- **Undo** (Fix B8): every move returns its history id; :func:`undo` within :data:`UNDO_SECONDS` reverts it (the DB
  checks the same rule with ``app.stage_undo``) and cancels what the move queued in the same transaction: dispatch
  rows (``cancelled`` / ``undone``), signed links (``revoked``) and the tasks it opened (``skipped``). Later than that:
  409 ``undo_expired``. A cancel is never undone.
- **Bulk** (:func:`bulk_move`): a dry run lists eligible and blocked trips and a ``preview_hash``; the run needs the
  same hash (409 ``preview_stale``), writes one history row per trip with one ``batch_id``, and returns one batch undo.
- **waiting_on** (:func:`derive_waiting_on`): derived on every move; cards that know more register a hook
  (:func:`register_waiting_on_hook`, the contract in ``docs/implementation-notes/AA-943.md``). A member may still set it
  by hand (``PATCH /trips/{id}``).

``last_activity_at`` is bumped on every move (and by notes, tasks and submissions, :func:`touch`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound
from app.intake import completeness
from app.permissions import Action, decide
from app.tenancy import AnyContext, TenantContext
from app.vault import Vault

STAGES = (
    "inquiry", "qualifying", "proposal", "booked", "pre_departure", "travelling", "completed", "lost", "cancelled",
)  # fmt: skip
ALLOWED_MOVES: dict[str, tuple[str, ...]] = {
    "inquiry": ("qualifying", "lost"),
    "qualifying": ("inquiry", "proposal", "lost"),
    "proposal": ("qualifying", "booked", "lost"),
    "booked": ("pre_departure", "travelling", "cancelled"),
    "pre_departure": ("travelling", "cancelled"),
    "travelling": ("completed", "cancelled"),
    "completed": (),
    "lost": ("inquiry",),
    "cancelled": (),
}
GATES: dict[tuple[str, str], str] = {
    ("qualifying", "proposal"): "stage2",
    ("proposal", "booked"): "booking",
    ("booked", "pre_departure"): "stage3",
}
# The gate of the one gated edge into each stage (a bulk move checks its override reason against it up front).
TARGET_GATES: dict[str, str] = {to: gate for (_from, to), gate in GATES.items()}
# The reasons an override may carry, per gate (crm-012 trip_stage_history_override_chk).
OVERRIDE_REASONS: dict[str, tuple[str, ...]] = {
    "stage2": ("client_in_hurry", "repeat_client", "info_by_phone", "other"),
    "booking": ("manual_booking",),
    "stage3": ("manual_stage3",),
}
CANCEL_REASONS = (
    "client_request",
    "client_illness",
    "travel_advisory",
    "force_majeure",
    "supplier_failure",
    "aa_cancelled",
    "payment_not_received",
    "duplicate",
    "other",
)
CANCELLABLE = ("booked", "pre_departure", "travelling")
UNDO_SECONDS = 10
MAX_BULK = 200
QUIET_DAYS = 3
WAITING_ON = ("agency", "client", "aa", "supplier", "none")

# DB SQLSTATEs of crm-012.
GATE_SQLSTATE = "CRM19"
UNDO_SQLSTATE = "CRM20"

LOCK_SQL = """
SELECT trip_id, stage, owner_member_id, intake_completeness, lost_reason_code, waiting_on
  FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL FOR UPDATE
"""
MOVE_SQL = """
UPDATE crm.trip
   SET stage = $2, stage_entered_at = now(), lost_reason_code = $3, cancel_reason_code = $4, waiting_on = $5,
       last_activity_at = now()
 WHERE trip_id = $1
"""
HISTORY_SQL = """
INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code, override, override_reason_code,
                                    undo_of, batch_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING history_id, at
"""
LAST_MOVE_SQL = """
SELECT history_id, from_stage, to_stage, at, created_by, undo_of, batch_id FROM crm.trip_stage_history
 WHERE trip_id = $1 ORDER BY at DESC, created_at DESC LIMIT 2
"""
LAST_LOST_REASON_SQL = """
SELECT reason_code FROM crm.trip_stage_history
 WHERE trip_id = $1 AND to_stage = 'lost' AND reason_code LIKE 'lost\\_%' ORDER BY at DESC LIMIT 1
"""
BATCH_SQL = """
SELECT h.trip_id, h.history_id FROM crm.trip_stage_history h
 WHERE h.batch_id = $1 AND h.undo_of IS NULL ORDER BY h.trip_id
"""
UNDO_DISPATCH_SQL = """
UPDATE crm.dispatch_queue SET status = 'cancelled', gate_result = 'undone', decided_at = now()
 WHERE trip_id = $1 AND created_at = $2 AND origin = 'user_action' AND status IN ('queued', 'held')
"""
UNDO_LINKS_SQL = """
UPDATE crm.client_link SET status = 'revoked', revoked_at = now()
 WHERE trip_id = $1 AND created_at = $2 AND status IN ('sent', 'opened')
"""
UNDO_TASKS_SQL = """
UPDATE crm.task SET status = 'skipped', done_at = now(), done_by = $3
 WHERE trip_id = $1 AND created_at = $2 AND status = 'open'
"""
TOUCH_SQL = "UPDATE crm.trip SET last_activity_at = now() WHERE trip_id = $1"
SETTINGS_SQL = "SELECT stage2_auto_send FROM crm.tenant_settings"
OPEN_LINK_SQL = """
SELECT EXISTS (SELECT 1 FROM crm.client_link
                WHERE trip_id = $1 AND purpose = 'stage2_form' AND status IN ('sent', 'opened')
                  AND expires_at > now() AND deleted_at IS NULL)
"""
# AA-946 (crm-016): the booking gate's fact, the same predicate as crm.enforce_stage_transition.
DEPOSIT_PAID_SQL = """
SELECT EXISTS (SELECT 1 FROM crm.payment_schedule s
                 JOIN crm.proposal p ON p.proposal_id = s.proposal_id
                WHERE s.trip_id = $1 AND s.kind = 'deposit' AND s.status = 'paid' AND s.deleted_at IS NULL
                  AND p.status = 'accepted' AND p.deleted_at IS NULL)
"""
# AA-946 review (S251): the portal lock (card R) = crm-016 crm.portal_unlocked: a paid deposit of the accepted proposal,
# or a manual_booking override into Booked (the deposit was taken outside the CRM) that was not undone. The database
# guard crm.guard_traveler_access (CRM32) reads the same function.
PORTAL_UNLOCKED_SQL = "SELECT crm.portal_unlocked($1)"
AA_LINE_OPEN_SQL = """
SELECT EXISTS (SELECT 1 FROM crm.trip_commitment
                WHERE trip_id = $1 AND kind = 'aa_line' AND line_status IN ('requested', 'held') AND deleted_at IS NULL)
"""
BULK_TRIPS_SQL = """
SELECT t.trip_id, t.stage, t.intake_completeness,
       EXISTS (SELECT 1 FROM crm.payment_schedule s
                 JOIN crm.proposal p ON p.proposal_id = s.proposal_id
                WHERE s.trip_id = t.trip_id AND s.kind = 'deposit' AND s.status = 'paid' AND s.deleted_at IS NULL
                  AND p.status = 'accepted' AND p.deleted_at IS NULL) AS deposit_paid
  FROM crm.trip t
 WHERE t.deleted_at IS NULL AND (t.trip_id = ANY($1::uuid[]) OR ($1::uuid[] IS NULL AND t.stage = $2))
 ORDER BY t.trip_id
 LIMIT $3
"""
CANCEL_EFFECTS_SQL = """
SELECT (SELECT count(*) FROM crm.dispatch_queue
         WHERE trip_id = $1 AND status IN ('queued', 'held') AND deleted_at IS NULL)::int AS dispatch,
       (SELECT count(*) FROM crm.task WHERE trip_id = $1 AND status = 'open' AND deleted_at IS NULL)::int AS tasks,
       (SELECT count(*) FROM crm.meeting_slot
         WHERE trip_id = $1 AND status IN ('requested', 'confirmed') AND starts_at > now()
           AND deleted_at IS NULL)::int AS slots,
       (SELECT count(*) FROM crm.client_link
         WHERE trip_id = $1 AND status IN ('sent', 'opened', 'submitted') AND deleted_at IS NULL)::int AS links
"""
CANCEL_DISPATCH_SQL = """
UPDATE crm.dispatch_queue SET status = 'cancelled', gate_result = 'undone', decided_at = now()
 WHERE trip_id = $1 AND status IN ('queued', 'held') AND deleted_at IS NULL
"""
CANCEL_TASKS_SQL = """
UPDATE crm.task SET status = 'skipped', done_at = now(), done_by = $2
 WHERE trip_id = $1 AND status = 'open' AND deleted_at IS NULL
"""
CANCEL_SLOTS_SQL = """
UPDATE crm.meeting_slot SET status = 'cancelled'
 WHERE trip_id = $1 AND status IN ('requested', 'confirmed') AND starts_at > now() AND deleted_at IS NULL
"""
CANCEL_LINKS_SQL = """
UPDATE crm.client_link SET status = 'revoked', revoked_at = now()
 WHERE trip_id = $1 AND status IN ('sent', 'opened', 'submitted') AND deleted_at IS NULL
"""


# ── waiting_on ────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TripFacts:
    """What a waiting_on hook may read without a query of its own."""

    trip_id: uuid.UUID
    stage: str
    stage2_link_open: bool


WaitingOnHook = Callable[[AnyContext, TripFacts], Awaitable[str | None]]
# (priority, name, hook): run in ascending priority for booked / pre_departure only; the first non-None answer wins.
WAITING_ON_HOOKS: list[tuple[int, str, WaitingOnHook]] = []
HOOK_STAGES = ("booked", "pre_departure")


def register_waiting_on_hook(priority: int, name: str, hook: WaitingOnHook) -> None:
    """A card that knows what a booked trip waits on adds its rule here (AA-944: agency supplier lines). Registering a
    name again replaces it (module reloads in tests)."""
    WAITING_ON_HOOKS[:] = [h for h in WAITING_ON_HOOKS if h[1] != name]
    WAITING_ON_HOOKS.append((priority, name, hook))
    WAITING_ON_HOOKS.sort(key=lambda h: (h[0], h[1]))


async def derive_waiting_on(ctx: AnyContext, trip_id: uuid.UUID, stage: str) -> str:
    """The base rule (Fix B1): inquiry -> agency; qualifying -> client while a planning form is out, else agency;
    proposal -> client; booked / pre_departure -> a hook's answer, else aa while an AA line is not confirmed, else
    none; travelling / completed / lost / cancelled -> none."""
    if stage == "inquiry":
        return "agency"
    if stage == "proposal":
        return "client"
    link_open = bool(await ctx.conn.fetchval(OPEN_LINK_SQL, trip_id)) if stage == "qualifying" else False
    if stage == "qualifying":
        return "client" if link_open else "agency"
    if stage in HOOK_STAGES:
        facts = TripFacts(trip_id=trip_id, stage=stage, stage2_link_open=link_open)
        for _priority, _name, hook in WAITING_ON_HOOKS:
            answer = await hook(ctx, facts)
            if answer is not None:
                if answer not in WAITING_ON:
                    raise ValueError(f"waiting_on hook {_name} answered {answer!r}")
                return answer
        return "aa" if await ctx.conn.fetchval(AA_LINE_OPEN_SQL, trip_id) else "none"
    return "none"


async def refresh_waiting_on(ctx: AnyContext, trip_id: uuid.UUID) -> str | None:
    """Derive again and store when it changed (a card whose facts changed calls this). None for an unknown trip."""
    stage = await ctx.conn.fetchval("SELECT stage FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL", trip_id)
    if stage is None:
        return None
    value = await derive_waiting_on(ctx, trip_id, stage)
    await ctx.conn.execute(
        "UPDATE crm.trip SET waiting_on = $2 WHERE trip_id = $1 AND waiting_on IS DISTINCT FROM $2", trip_id, value
    )
    return value


async def touch(ctx: AnyContext, trip_id: uuid.UUID) -> None:
    """Bump ``last_activity_at`` (the board's quiet flag) after a note, task change or submission."""
    await ctx.conn.execute(TOUCH_SQL, trip_id)


# ── moves ─────────────────────────────────────────────────────────────────────────────────────────


def allowed_moves(stage: str) -> list[str]:
    """The moves the stage menu offers (cancel is its own flow)."""
    return [s for s in ALLOWED_MOVES.get(stage, ()) if s != "cancelled"]


def _s2(raw: Any) -> int:
    return _stored(raw, "s2")


def _stored(raw: Any, key: str) -> int:
    value = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    return int(value.get(key, 0))


STAGE3_GATE = 100


def undo_view(history_id: uuid.UUID, at: dt.datetime) -> dict[str, Any]:
    return {"history_id": str(history_id), "undo_until": (at + dt.timedelta(seconds=UNDO_SECONDS)).isoformat()}


async def _check_gate(
    ctx: AnyContext,
    vault: Vault | None,
    trip: Any,
    to: str,
    override_reason: str | None,
    *,
    override_if_needed: bool = False,
) -> tuple[str | None, str | None]:
    """(gate to override, reason) for this move, or (None, None) when no gate stands; raises the 409.
    ``override_if_needed`` (a bulk move): an override on a Stage 2 gate that is already met is ignored (and not
    logged) instead of answering 422 ``override_not_needed``."""
    gate = GATES.get((trip["stage"], to))
    if gate is None:
        if override_reason is not None:
            raise Invalid("override_not_needed", "this move has no gate")
        return None, None
    if gate == "stage2":
        done = await completeness.refresh(ctx, vault, trip["trip_id"], actor=_actor(ctx)) if vault else None
        s2 = done.s2 if done is not None else _s2(trip["intake_completeness"])
        if s2 >= completeness.STAGE2_GATE:
            if override_reason is not None and not override_if_needed:
                raise Invalid("override_not_needed", "Stage 2 is complete enough")
            return None, None
        if override_reason is None:
            raise Conflict(
                "stage2_incomplete",
                f"Stage 2 is {s2} %; {completeness.STAGE2_GATE} % or an override reason is needed",
                s2=s2,
                missing=list(done.missing_s2) if done is not None else [],
                override_reasons=list(OVERRIDE_REASONS[gate]),
            )
    else:
        if await _gate_met(ctx, vault, trip, gate):
            if override_reason is not None and not override_if_needed:
                raise Invalid("override_not_needed", f"the {gate} gate is met")
            return None, None
        if override_reason is None:
            raise Conflict("gate_unmet", f"the {gate} gate needs an override", gate=gate,
                           override_reasons=list(OVERRIDE_REASONS[gate]))  # fmt: skip
    if override_reason not in OVERRIDE_REASONS[gate]:
        raise Invalid("invalid_override_reason", ", ".join(OVERRIDE_REASONS[gate]), gate=gate)
    return gate, override_reason


async def _gate_met(ctx: AnyContext, vault: Vault | None, trip: Any, gate: str) -> bool:
    """CRM-18 (AA-946): the booking gate is a paid deposit of the accepted proposal; the Stage 3 gate is s3 = 100
    (recomputed when a vault is at hand, else the stored value)."""
    if gate == "booking":
        return bool(await ctx.conn.fetchval(DEPOSIT_PAID_SQL, trip["trip_id"]))
    if vault is not None:
        from app.portal import progress

        done = await progress.refresh(ctx, trip["trip_id"])
        return done.s3 >= STAGE3_GATE
    return _stored(trip["intake_completeness"], "s3") >= STAGE3_GATE


def _actor(ctx: AnyContext) -> str:
    member = getattr(ctx, "member_id", None)
    return f"member:{member}" if member is not None else str(getattr(ctx, "actor", "system"))


def _db_refusal(exc: asyncpg.PostgresError, stage: str) -> Conflict:
    code = getattr(exc, "sqlstate", None)
    if code == GATE_SQLSTATE:
        return Conflict("gate_unmet", "the database refused the gate", detail_code=str(exc).removeprefix("crm: "))
    if code == UNDO_SQLSTATE:
        return Conflict("undo_expired", "this move can no longer be undone")
    return Conflict("transition_not_allowed", allowed=allowed_moves(stage))


async def move(
    ctx: AnyContext,
    trip_id: uuid.UUID,
    to: str,
    *,
    lost_reason: str | None = None,
    override_reason: str | None = None,
    vault: Vault | None = None,
    batch_id: uuid.UUID | None = None,
    reopen: bool = False,
    override_if_needed: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """One move of one trip (the stage menu, a bulk move, the date rule). ``vault`` lets the Stage 2 gate recompute
    completeness (and name what is missing); without it the stored percentage decides. ``reopen``: the legacy Slice 1
    "undo" flag of the web (lost -> inquiry), kept as reason ``undo`` until the web uses :func:`undo`. ``reason``: the
    history code of a move a system rule makes (``deposit_paid``, AA-946) instead of ``agent_move``."""
    from app.pipeline import LOST_REASONS

    if to not in STAGES:
        raise Invalid("invalid_stage", ", ".join(STAGES))
    if to == "cancelled":
        raise Conflict("use_cancel_flow", "cancel a trip with POST /trips/{id}/cancel (dry run first)")
    trip = await ctx.conn.fetchrow(LOCK_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    stage = trip["stage"]
    if stage == to:
        raise Conflict("already_in_stage")
    if to == "lost":
        if lost_reason not in LOST_REASONS:
            raise Invalid("lost_reason_required", "pick one of the fixed reasons", reasons=list(LOST_REASONS))
    elif lost_reason is not None:
        raise Invalid("lost_reason_not_allowed", "a reason goes with lost only")
    if to not in ALLOWED_MOVES.get(stage, ()):
        raise Conflict("transition_not_allowed", allowed=allowed_moves(stage))
    gate, override = await _check_gate(ctx, vault, trip, to, override_reason, override_if_needed=override_if_needed)
    waiting_on = await derive_waiting_on(ctx, trip_id, to)
    try:
        async with ctx.conn.transaction():
            if gate is not None:
                await ctx.conn.execute("SELECT set_config('app.gate_override', $1, true)", gate)
            await ctx.conn.execute(MOVE_SQL, trip_id, to, lost_reason if to == "lost" else None, None, waiting_on)
            if gate is not None:
                await ctx.conn.execute("SELECT set_config('app.gate_override', '', true)")
    except asyncpg.PostgresError as exc:
        if isinstance(exc, asyncpg.CheckViolationError) or getattr(exc, "sqlstate", None) == GATE_SQLSTATE:
            raise _db_refusal(exc, stage) from None
        raise
    if reason is None:
        reason = "undo" if reopen else (f"lost_{lost_reason}"[:40] if to == "lost" else "agent_move")
        if batch_id is not None and to != "lost":
            reason = "bulk_move"
    history = await ctx.conn.fetchrow(
        HISTORY_SQL, trip_id, stage, to, reason, override is not None, override, None, batch_id
    )
    audit_reason = f"override_{override}" if override is not None else reason
    ctx.audit("stage_change", "allowed", "trip", str(trip_id), reason=audit_reason[:40])
    effects = await _enter_effects(ctx, vault, trip_id, stage, to, owner_member_id=trip["owner_member_id"])
    if to == "lost":
        # AA-947: a lost trip gives its AA holds back (queued after the Undo window; Undo cancels them).
        from app.booking import aa_lines

        effects["aa_releases_queued"] = await aa_lines.close_trip(ctx, trip_id)
    if effects.get("stage2_sent"):
        waiting_on = await refresh_waiting_on(ctx, trip_id) or waiting_on
    return {
        "trip_id": str(trip_id),
        "from_stage": stage,
        "stage": to,
        "waiting_on": waiting_on,
        "override_reason_code": override,
        "allowed_moves": allowed_moves(to),
        "undo": undo_view(history["history_id"], history["at"]),
        **effects,
    }


async def _enter_effects(
    ctx: AnyContext,
    vault: Vault | None,
    trip_id: uuid.UUID,
    from_stage: str,
    to: str,
    *,
    owner_member_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """What entering a stage starts (in the move's transaction, so Undo takes it back): entering Qualifying opens the
    planning task below 80 % and, with Stage 2 auto-send on, sends the planning form (card E).

    The auto-send never fails the move: it runs in a savepoint, and a trip whose form cannot go out (no lead, the lead
    archived, ...) moves anyway with ``stage2_sent: null`` and ``stage2_skipped: <code>``. It also follows the message
    rule of the member who moves (``MESSAGE_SEND``: an assistant only for the trips assigned to them), as the explicit
    ``POST /trips/{id}/stage2/send`` does; a member the rule refuses moves the trip without the send
    (``stage2_skipped``: the rule's reason). A move without a member (a job) acts for the agency."""
    if to != "qualifying" or vault is None:
        return {}
    done = await completeness.refresh(ctx, vault, trip_id, actor=_actor(ctx))
    out: dict[str, Any] = {"completeness": done.view() if done is not None else None}
    if from_stage == "inquiry" and await ctx.conn.fetchval(SETTINGS_SQL) and done is not None and done.s2 < 100:
        from app.intake import stage2

        member = getattr(ctx, "member_id", None)
        if member is not None:
            rule = decide(getattr(ctx, "member_role", None) or "", Action.MESSAGE_SEND,
                          assigned=owner_member_id == member)  # fmt: skip
            if not rule.allowed:
                return {**out, "stage2_sent": None, "stage2_skipped": rule.reason}
        try:
            async with ctx.conn.transaction():
                out["stage2_sent"] = await stage2.send_link(ctx, vault, trip_id, auto=True)
        except (Conflict, NotFound) as exc:
            out["stage2_sent"] = None
            out["stage2_skipped"] = exc.code
    return out


def _rows(status: str) -> int:
    """The row count of an asyncpg command tag (``UPDATE 3``)."""
    tail = str(status).split()[-1] if status else ""
    return int(tail) if tail.isdigit() else 0


async def undo(ctx: TenantContext, trip_id: uuid.UUID, history_id: uuid.UUID) -> dict[str, Any]:
    """Take back the trip's latest move within :data:`UNDO_SECONDS`, made by this member (409 ``undo_expired``
    otherwise), and cancel what it queued."""
    trip = await ctx.conn.fetchrow(LOCK_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    return await _undo_one(ctx, trip, history_id, batch=False)


async def _undo_one(ctx: TenantContext, trip: Any, history_id: uuid.UUID, *, batch: bool) -> dict[str, Any]:
    trip_id = trip["trip_id"]
    rows = await ctx.conn.fetch(LAST_MOVE_SQL, trip_id)
    last = rows[0] if rows else None
    now = await ctx.conn.fetchval("SELECT now()")
    if (
        last is None
        or last["history_id"] != history_id
        or last["undo_of"] is not None
        or last["from_stage"] is None
        or last["to_stage"] != trip["stage"]
        or last["to_stage"] == "cancelled"
        or last["created_by"] != f"member:{ctx.member_id}"
        or now - last["at"] > dt.timedelta(seconds=UNDO_SECONDS)
        or (len(rows) > 1 and rows[1]["at"] == last["at"])
    ):
        raise Conflict("undo_expired", "this move can no longer be undone")
    back = last["from_stage"]
    lost_reason = None
    if back == "lost":
        code = await ctx.conn.fetchval(LAST_LOST_REASON_SQL, trip_id)
        lost_reason = code.removeprefix("lost_") if code else "other"
    waiting_on = await derive_waiting_on(ctx, trip_id, back)
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute("SELECT set_config('app.stage_undo', $1, true)", str(history_id))
            await ctx.conn.execute(MOVE_SQL, trip_id, back, lost_reason, None, waiting_on)
            await ctx.conn.execute("SELECT set_config('app.stage_undo', '', true)")
    except asyncpg.PostgresError as exc:
        if getattr(exc, "sqlstate", None) in (UNDO_SQLSTATE, GATE_SQLSTATE) or isinstance(
            exc, asyncpg.CheckViolationError
        ):
            raise Conflict("undo_expired", "this move can no longer be undone") from None
        raise
    await ctx.conn.execute(
        HISTORY_SQL, trip_id, trip["stage"], back, "undo", False, None, history_id, last["batch_id"] if batch else None
    )
    cancelled = _rows(await ctx.conn.execute(UNDO_DISPATCH_SQL, trip_id, last["at"]))
    revoked = _rows(await ctx.conn.execute(UNDO_LINKS_SQL, trip_id, last["at"]))
    if last["to_stage"] == "lost":
        from app.booking import aa_lines  # AA-947: the AA releases the Lost move queued

        await aa_lines.undo_close(ctx, trip_id, last["at"])
    # Only entering Qualifying opens tasks (_enter_effects). A planning task the Stage 2 gate's completeness refresh
    # opened on another move belongs to the trip's earlier stage and stays.
    skipped = (
        _rows(await ctx.conn.execute(UNDO_TASKS_SQL, trip_id, last["at"], f"member:{ctx.member_id}"))
        if last["to_stage"] == "qualifying"
        else 0
    )
    ctx.audit("stage_change", "allowed", "trip", str(trip_id), reason="undo")
    return {
        "trip_id": str(trip_id),
        "stage": back,
        "waiting_on": waiting_on,
        "allowed_moves": allowed_moves(back),
        "dispatch_cancelled": cancelled,
        "links_revoked": revoked,
        "tasks_skipped": skipped,
    }


# ── bulk ──────────────────────────────────────────────────────────────────────────────────────────


def preview_hash(to: str, eligible: list[str], override_reason: str | None, lost_reason: str | None) -> str:
    raw = "|".join([to, override_reason or "", lost_reason or "", *sorted(eligible)])
    return hashlib.sha256(raw.encode()).hexdigest()


def _blocked_code(row: Any, to: str, override_reason: str | None) -> str | None:
    stage = row["stage"]
    if stage == to:
        return "already_in_stage"
    if to not in ALLOWED_MOVES.get(stage, ()):
        return "transition_not_allowed"
    gate = GATES.get((stage, to))
    if gate == "stage2" and _s2(row["intake_completeness"]) < completeness.STAGE2_GATE and override_reason is None:
        return "stage2_incomplete"
    met = {
        "booking": bool(row["deposit_paid"]),
        "stage3": _stored(row["intake_completeness"], "s3") >= STAGE3_GATE,
    }
    if gate in met and not met[gate] and override_reason is None:
        return "gate_unmet"
    return None


async def bulk_move(
    ctx: TenantContext,
    *,
    to: str,
    trip_ids: list[uuid.UUID] | None,
    from_stage: str | None,
    lost_reason: str | None,
    override_reason: str | None,
    dry_run: bool,
    expected_hash: str | None,
    vault: Vault | None = None,
) -> dict[str, Any]:
    """Preview (``dry_run``) or run a move of many trips. The run needs the preview's hash: the same eligible trips,
    target and reasons (409 ``preview_stale`` otherwise)."""
    if to == "cancelled":
        raise Conflict("use_cancel_flow", "cancel trips one by one, with their dry run")
    if to not in STAGES:
        raise Invalid("invalid_stage", ", ".join(STAGES))
    if (trip_ids is None) == (from_stage is None):
        raise Invalid("trip_ids_or_from_stage", "name the trips or the column, not both")
    if trip_ids is not None and not 1 <= len(trip_ids) <= MAX_BULK:
        raise Invalid("too_many_trips", f"1 to {MAX_BULK} trips")
    if from_stage is not None and from_stage not in STAGES:
        raise Invalid("invalid_stage", ", ".join(STAGES))
    _check_bulk_reasons(to, lost_reason, override_reason)
    rows = await ctx.conn.fetch(BULK_TRIPS_SQL, trip_ids, from_stage, MAX_BULK + 1)
    if len(rows) > MAX_BULK:
        raise Invalid("too_many_trips", f"at most {MAX_BULK} trips per bulk move; filter the column first")
    found = {r["trip_id"] for r in rows}
    eligible: list[str] = []
    blocked: list[dict[str, str]] = [
        {"trip_id": str(t), "code": "trip_not_found"} for t in (trip_ids or []) if t not in found
    ]
    for r in rows:
        code = _blocked_code(r, to, override_reason)
        if code is None:
            eligible.append(str(r["trip_id"]))
        else:
            blocked.append({"trip_id": str(r["trip_id"]), "code": code})
    digest = preview_hash(to, eligible, override_reason, lost_reason)
    out: dict[str, Any] = {"to": to, "eligible": eligible, "blocked": blocked, "preview_hash": digest}
    if dry_run:
        return out
    if expected_hash != digest:
        raise Conflict("preview_stale", "the trips changed since the preview; preview again", preview_hash=digest)
    if not eligible:
        raise Conflict("nothing_to_move")
    batch_id = uuid.uuid4()
    for trip_id in eligible:
        await move(
            ctx,
            uuid.UUID(trip_id),
            to,
            lost_reason=lost_reason if to == "lost" else None,
            override_reason=override_reason if (_stage_of(rows, trip_id), to) in GATES else None,
            vault=vault,
            batch_id=batch_id,
            override_if_needed=True,
        )
    ctx.audit("bulk_stage_change", "allowed", "pipeline", str(batch_id), reason=to, object_ids=tuple(eligible))
    now = await ctx.conn.fetchval("SELECT now()")
    return {**out, "moved": len(eligible), "batch_id": str(batch_id), "undo": undo_view(batch_id, now)}


def _check_bulk_reasons(to: str, lost_reason: str | None, override_reason: str | None) -> None:
    """The reasons of a bulk move are checked before the preview, so a preview never lists trips the run refuses.
    An override reason must fit the gate of the one gated edge into ``to``; on a run it is applied only to the trips
    whose gate still stands (a trip already at 80 % moves without it)."""
    from app.pipeline import LOST_REASONS

    if to == "lost":
        if lost_reason not in LOST_REASONS:
            raise Invalid("lost_reason_required", "pick one of the fixed reasons", reasons=list(LOST_REASONS))
    elif lost_reason is not None:
        raise Invalid("lost_reason_not_allowed", "a reason goes with lost only")
    if override_reason is None:
        return
    gate = TARGET_GATES.get(to)
    if gate is None:
        raise Invalid("override_not_needed", "no move into this stage has a gate")
    if override_reason not in OVERRIDE_REASONS[gate]:
        raise Invalid("invalid_override_reason", ", ".join(OVERRIDE_REASONS[gate]), gate=gate)


def _stage_of(rows: list[Any], trip_id: str) -> str:
    return next(r["stage"] for r in rows if str(r["trip_id"]) == trip_id)


async def bulk_undo(ctx: TenantContext, batch_id: uuid.UUID) -> dict[str, Any]:
    """Undo every move of one bulk run (all or nothing: one expired trip fails the whole undo)."""
    rows = await ctx.conn.fetch(BATCH_SQL, batch_id)
    if not rows:
        raise NotFound("batch_not_found")
    results = []
    for r in rows:
        trip = await ctx.conn.fetchrow(LOCK_SQL, r["trip_id"])
        if trip is None:
            raise Conflict("undo_expired", "a trip of the batch is gone")
        results.append(await _undo_one(ctx, trip, r["history_id"], batch=True))
    return {
        "batch_id": str(batch_id),
        "undone": len(results),
        "dispatch_cancelled": sum(r["dispatch_cancelled"] for r in results),
    }


# ── cancel (C12) ──────────────────────────────────────────────────────────────────────────────────


async def cancel(ctx: TenantContext, trip_id: uuid.UUID, *, reason: str | None, dry_run: bool) -> dict[str, Any]:
    """Cancel a booked, pre-departure or travelling trip. The dry run lists today's effects and changes nothing; the
    run applies them in this transaction. Later cards (Booking release, invoices, refunds, commission, workflows) add
    their effect rows here."""
    if reason not in CANCEL_REASONS:
        raise Invalid("cancel_reason_required", "pick one of the fixed reasons", reasons=list(CANCEL_REASONS))
    trip = await ctx.conn.fetchrow(LOCK_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    if trip["stage"] not in CANCELLABLE:
        raise Conflict("transition_not_allowed", "only a booked, pre-departure or travelling trip is cancelled",
                       allowed=allowed_moves(trip["stage"]))  # fmt: skip
    from app.booking import aa_lines

    counts = await ctx.conn.fetchrow(CANCEL_EFFECTS_SQL, trip_id)
    aa = await aa_lines.close_effects(ctx, trip_id)
    effects = [
        {"kind": "dispatch_cancelled", "count": counts["dispatch"]},
        {"kind": "tasks_skipped", "count": counts["tasks"]},
        {"kind": "call_slots_cancelled", "count": counts["slots"]},
        {"kind": "client_links_revoked", "count": counts["links"]},
        # AA-947: AA holds are released; lines AA already booked wait for the cancellation card (CRM-27, the fee).
        {"kind": "aa_holds_released", "count": aa["held"]},
        {"kind": "aa_bookings_kept", "count": aa["booked"]},
    ]
    out: dict[str, Any] = {
        "trip_id": str(trip_id),
        "from_stage": trip["stage"],
        "cancel_reason_code": reason,
        "dry_run": dry_run,
        "effects": effects,
    }
    if dry_run:
        return out
    actor = f"member:{ctx.member_id}"
    await ctx.conn.execute(CANCEL_DISPATCH_SQL, trip_id)
    await ctx.conn.execute(CANCEL_TASKS_SQL, trip_id, actor)
    await ctx.conn.execute(CANCEL_SLOTS_SQL, trip_id)
    await ctx.conn.execute(CANCEL_LINKS_SQL, trip_id)
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(MOVE_SQL, trip_id, "cancelled", None, reason, "none")
    except asyncpg.CheckViolationError:
        raise Conflict("transition_not_allowed", allowed=allowed_moves(trip["stage"])) from None
    await ctx.conn.execute(
        HISTORY_SQL, trip_id, trip["stage"], "cancelled", f"cancel_{reason}", False, None, None, None
    )
    await aa_lines.close_trip(ctx, trip_id)
    ctx.audit("trip_cancel", "allowed", "trip", str(trip_id), reason=reason)
    return {**out, "stage": "cancelled"}
