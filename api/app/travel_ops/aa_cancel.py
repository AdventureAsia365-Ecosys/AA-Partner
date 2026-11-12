"""Adventure Asia cancelled a departure (AA-1070 item crm-13; crm-060 ``crm.aa_cancel_case``): the guided flow for every
agency trip on it.

- **The feed** (:func:`open_cases`, called by :func:`app.travel_ops.departures.apply_event` when Booking says
  ``departure.cancelled``): every live trip of the agency (not lost, cancelled, completed or being cancelled) with a
  live AA line on the departure (``crm.booking_line``, not cancelled) or an AA option on it in a live builder version
  (draft, published, sent, accepted) gets one case (unique per trip and departure: a replayed page or a repeated event
  opens nothing new), one urgent task ``aa_departure_cancelled`` for the trip owner and the workflow trip event
  ``departure.aa_cancelled`` (the AA library's opt-in template mails the lead traveller from the agency mailbox). AA
  never messages travellers. One ``aa_sync`` audit event per departure naming the trips; no money anywhere.
- **The flag** (:func:`flagged`): a case still open, or re-quoted onto a new trip while this one is not cancelled yet,
  marks the trip "AA cancelled": the pipeline filter ``issue=aa_cancelled``, the card badge, the trip page card.
- **(a) Re-quote** (:func:`requote`): the same AA tour on another departure the agent picks (a Booking departure of
  the tour: ``GET /aa/products/{source_id}``), as a NEW builder version prefilled from the trip's latest version with
  that tour (the accepted one first): the AA options on the cancelled departure move to the new one, their quote is
  cleared ("Refresh AA prices" quotes it again) and day 1 moves so the tour's day falls on the new start date. A trip
  before booking (no accepted version) gets the version itself (or its working copy, when it has one); a booked trip
  cannot take a second accepted version (one per trip, crm-016), so the re-quote opens a **new trip** in Proposal
  (same client, party, owner, destinations; the dates moved) with the version, and the card then offers (b) for the
  old trip. Nothing is sent to the client: the agent publishes and sends as usual. Idempotent: a case re-quoted
  already answers with what it made.
- **(b) Cancel** (:func:`forced_decision`, used by :func:`app.stages.cancel`): cancelling a flagged trip with the
  reason ``aa_cancelled`` forces the traveller charge to 0 (decision ``waived``, reason
  :data:`WAIVER_REASON`, never the owner's call: AA cancelled, Booking charges the agency no fee), so every payment
  is prefilled for a full refund as a ``proposed`` refund request (approved under §4.6 as usual, never sent by
  itself). The run closes the case (:func:`close_on_cancel`).

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from typing import Any

from app.errors import Conflict, Invalid, NotFound
from app.tenancy import AnyContext, TenantContext
from app.vault import Vault

log = logging.getLogger(__name__)

TASK = "aa_departure_cancelled"
EVENT_NAME = "departure.aa_cancelled"
CANCEL_REASON = "aa_cancelled"  # the trip cancel reason (crm-012) Booking maps to departure_cancelled
WAIVER_REASON = "aa_departure_cancelled"
REBOOK_REASON = "aa_rebook"  # crm.trip_stage_history reason of the new trip a re-quote opens
CLOSED_STAGES = ("lost", "cancelled", "completed")
PRE_BOOKING = ("inquiry", "qualifying", "proposal")
BOOKED = ("booked", "pre_departure", "travelling")
MAX_TRIPS = 1000  # trips of one agency on one departure (seats bound it far below this)
_REF = re.compile(r"^[A-Za-z0-9:_.-]{1,64}$")
_EVENT_NS = uuid.UUID("5d8f1f4e-2b71-4f0a-9a43-1c7e0d6b2a60")

AFFECTED_SQL = """
SELECT t.trip_id FROM crm.trip t
 WHERE t.deleted_at IS NULL AND t.stage <> ALL($2::text[])
   AND NOT EXISTS (SELECT 1 FROM crm.trip_cancellation x WHERE x.trip_id = t.trip_id)
   AND (EXISTS (SELECT 1 FROM crm.booking_line l
                 WHERE l.trip_id = t.trip_id AND l.supplier_kind = 'aa' AND l.departure_id = $1
                   AND l.status <> 'cancelled' AND l.deleted_at IS NULL)
        OR EXISTS (SELECT 1 FROM crm.option o
                     JOIN crm.proposal p ON p.proposal_id = o.proposal_id AND p.deleted_at IS NULL
                                        AND p.status IN ('draft', 'published', 'sent', 'accepted')
                    WHERE o.trip_id = t.trip_id AND o.source = 'aa' AND o.aa_departure_id = $1
                      AND o.deleted_at IS NULL))
 ORDER BY t.trip_id
 LIMIT $3
"""
OPEN_SQL = """
INSERT INTO crm.aa_cancel_case (trip_id, departure_id, opened_event_id, opened_at) VALUES ($1, $2, $3, $4)
ON CONFLICT (tenant_id, trip_id, departure_id) DO NOTHING
RETURNING aa_cancel_case_id
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
SELECT $1, $2, 'urgent', t.owner_member_id, 'created', now() + interval '1 day' FROM crm.trip t WHERE t.trip_id = $1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""
# The flag: an open case, or one re-quoted onto a new trip (the old trip still waits for its cancel). The same
# condition in FLAGGED_SQL, ACTIVE_SQL and CLOSE_SQL (and crm-060's aa_cancel_case_active_idx).
FLAGGED_SQL = """
SELECT DISTINCT c.trip_id FROM crm.aa_cancel_case c
 WHERE c.trip_id = ANY($1::uuid[]) AND c.deleted_at IS NULL
   AND (c.resolution IS NULL OR (c.resolution = 'requoted' AND c.rebook_trip_id IS NOT NULL))
"""
ACTIVE_SQL = """
SELECT c.aa_cancel_case_id FROM crm.aa_cancel_case c
 WHERE c.trip_id = $1 AND c.deleted_at IS NULL
   AND (c.resolution IS NULL OR (c.resolution = 'requoted' AND c.rebook_trip_id IS NOT NULL))
 ORDER BY c.opened_at LIMIT 1
"""
CASES_SQL = """
SELECT c.aa_cancel_case_id, c.departure_id, c.resolution, c.opened_at, c.resolved_at, c.proposal_id,
       c.rebook_trip_id,
       (SELECT v.version FROM crm.proposal v WHERE v.proposal_id = c.proposal_id) AS proposal_version
  FROM crm.aa_cancel_case c
 WHERE c.trip_id = $1 AND c.deleted_at IS NULL
 ORDER BY c.opened_at, c.aa_cancel_case_id
"""
REBOOKED_FROM_SQL = """
SELECT c.trip_id FROM crm.aa_cancel_case c WHERE c.rebook_trip_id = $1 AND c.deleted_at IS NULL LIMIT 1
"""
CASE_LOCK_SQL = """
SELECT c.aa_cancel_case_id, c.trip_id, c.departure_id, c.resolution, c.proposal_id, c.rebook_trip_id
  FROM crm.aa_cancel_case c
 WHERE c.aa_cancel_case_id = $1 AND c.trip_id = $2 AND c.deleted_at IS NULL
   FOR UPDATE
"""
# What was on the departure: the AA line first (its tour), else the option of the latest live version.
TOUR_SQL = """
SELECT x.tour_source_id, x.component_id, x.component_kind, x.title, x.service_from FROM (
  SELECT l.aa_tour_source_id AS tour_source_id, l.aa_component_id AS component_id,
         l.aa_component_kind AS component_kind, l.title, l.service_from, 0 AS rank, 0 AS version
    FROM crm.booking_line l
   WHERE l.trip_id = $1 AND l.departure_id = $2 AND l.supplier_kind = 'aa' AND l.deleted_at IS NULL
  UNION ALL
  SELECT o.aa_tour_source_id, o.aa_component_id, o.aa_component_kind, o.title, NULL, 1, p.version
    FROM crm.option o JOIN crm.proposal p ON p.proposal_id = o.proposal_id AND p.deleted_at IS NULL
   WHERE o.trip_id = $1 AND o.aa_departure_id = $2 AND o.source = 'aa' AND o.deleted_at IS NULL
) x
 ORDER BY x.rank, x.version DESC, x.service_from NULLS LAST
 LIMIT 1
"""
TRIP_SQL = "SELECT trip_id, stage, start_date, end_date FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
# The version to start from: the trip's builder version with the tour on the departure, the accepted one first.
SOURCE_SQL = """
SELECT p.proposal_id, p.status, p.version FROM crm.proposal p
 WHERE p.trip_id = $1 AND p.kind = 'builder' AND p.deleted_at IS NULL
   AND p.status IN ('accepted', 'sent', 'published', 'draft')
   AND EXISTS (SELECT 1 FROM crm.option o WHERE o.proposal_id = p.proposal_id AND o.source = 'aa'
                  AND o.aa_departure_id = $2 AND o.deleted_at IS NULL)
 ORDER BY p.status = 'accepted' DESC, p.version DESC
 LIMIT 1
"""
ACCEPTED_SQL = """
SELECT EXISTS (SELECT 1 FROM crm.proposal WHERE trip_id = $1 AND status = 'accepted' AND deleted_at IS NULL) AS has
"""
DRAFT_SQL = """
SELECT proposal_id FROM crm.proposal WHERE trip_id = $1 AND kind = 'builder' AND status = 'draft' AND deleted_at IS NULL
"""
DEPARTURE_STATE_SQL = """
SELECT departure_status FROM crm.aa_departure_state WHERE departure_id = $1 AND deleted_at IS NULL
"""
FIRST_DAY_SQL = """
SELECT min(d.day_no) FROM crm.option o
  JOIN crm.option_block b ON b.option_block_id = o.option_block_id
  JOIN crm.proposal_day d ON d.proposal_day_id = b.proposal_day_id
 WHERE o.proposal_id = $1 AND o.source = 'aa' AND o.aa_departure_id = $2 AND o.deleted_at IS NULL
"""
SWAP_SQL = """
UPDATE crm.option SET aa_departure_id = $3, aa_quote_id = NULL, aa_quote_valid_until = NULL,
       availability_checked_at = NULL, below_from_price = NULL
 WHERE proposal_id = $1 AND source = 'aa' AND aa_departure_id = $2 AND deleted_at IS NULL
RETURNING option_id
"""
STARTS_ON_SQL = "UPDATE crm.proposal SET starts_on = $2 WHERE proposal_id = $1 AND status = 'draft'"
RESOLVE_SQL = """
UPDATE crm.aa_cancel_case
   SET resolution = 'requoted', resolved_at = now(), resolved_by = $2, proposal_id = $3, rebook_trip_id = $4
 WHERE aa_cancel_case_id = $1 AND resolution IS NULL
"""
CLOSE_SQL = """
UPDATE crm.aa_cancel_case c
   SET resolution = 'cancelled', resolved_at = now(), resolved_by = $2
 WHERE c.trip_id = $1 AND c.deleted_at IS NULL
   AND (c.resolution IS NULL OR (c.resolution = 'requoted' AND c.rebook_trip_id IS NOT NULL))
RETURNING c.aa_cancel_case_id
"""
CLOSE_TASK_SQL = """
UPDATE crm.task SET status = 'done', done_at = now(), done_by = $3
 WHERE trip_id = $1 AND kind = $2 AND status = 'open' AND deleted_at IS NULL
"""
NEW_TRIP_SQL = """
INSERT INTO crm.trip (trip_id, title, stage, waiting_on, start_date, end_date, nights, destinations, party_adults,
                      party_child_ages, party_type, budget_band, trip_style, owner_member_id, lead_source,
                      sales_partner_id, intake_completeness)
SELECT $2, t.title, 'proposal', 'agency', $3, $4, t.nights, t.destinations, t.party_adults, t.party_child_ages,
       t.party_type, t.budget_band, t.trip_style, t.owner_member_id, t.lead_source, t.sales_partner_id,
       t.intake_completeness
  FROM crm.trip t WHERE t.trip_id = $1 AND t.deleted_at IS NULL
"""
NEW_HISTORY_SQL = """
INSERT INTO crm.trip_stage_history (trip_id, from_stage, to_stage, reason_code)
VALUES ($1, NULL, 'proposal', 'aa_rebook')
"""
COPY_TRAVELLERS_SQL = """
INSERT INTO crm.trip_traveler (trip_id, traveler_id, role, portal_access)
SELECT $2, traveler_id, role, portal_access FROM crm.trip_traveler WHERE trip_id = $1 AND deleted_at IS NULL
"""
COPY_PARTY_SQL = """
INSERT INTO crm.trip_party (trip_id, traveler_token, role)
SELECT $2, traveler_token, role FROM crm.trip_party WHERE trip_id = $1 AND deleted_at IS NULL
"""


def _ref(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _REF.match(value):
        raise Invalid(code, "1 to 64 letters, digits or : _ . -")
    return value


def event_id(trip_id: uuid.UUID, departure_id: str) -> uuid.UUID:
    """The workflow event id of one trip hit by one cancelled departure: a rule never starts twice for it."""
    return uuid.uuid5(_EVENT_NS, f"{trip_id}:{departure_id}")


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


# ── the feed ─────────────────────────────────────────────────────────────────────────────────────


async def open_cases(ctx: AnyContext, departure_id: str, event: str, occurred_at: dt.datetime) -> list[uuid.UUID]:
    """``departure.cancelled``: one case, one owner task and one workflow event per affected trip (new cases only).
    Returns the trips that got a new case."""
    from app.workflows import triggers

    rows = await ctx.conn.fetch(AFFECTED_SQL, departure_id, list(CLOSED_STAGES), MAX_TRIPS)
    if len(rows) >= MAX_TRIPS:
        log.warning("aa_cancel_trips_capped limit=%s", MAX_TRIPS)
    opened: list[uuid.UUID] = []
    for r in rows:
        case_id = await ctx.conn.fetchval(OPEN_SQL, r["trip_id"], departure_id, event, occurred_at)
        if case_id is None:
            continue  # replayed or repeated: the trip has its case (and had its task) already
        await ctx.conn.execute(TASK_SQL, r["trip_id"], TASK)
        await triggers.on_trip_event(ctx, r["trip_id"], EVENT_NAME, event_id(r["trip_id"], departure_id))
        opened.append(r["trip_id"])
    if opened:
        ctx.audit("aa_sync", "allowed", "departure", departure_id, reason="departure_cancelled_cases",
                  object_ids=tuple(str(t) for t in opened[:200]))  # fmt: skip
    return opened


# ── reads ────────────────────────────────────────────────────────────────────────────────────────


async def flagged(ctx: AnyContext, trip_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """The trips among ``trip_ids`` marked "AA cancelled"."""
    if not trip_ids:
        return set()
    return {r["trip_id"] for r in await ctx.conn.fetch(FLAGGED_SQL, trip_ids)}


async def active_case(ctx: AnyContext, trip_id: uuid.UUID) -> uuid.UUID | None:
    return await ctx.conn.fetchval(ACTIVE_SQL, trip_id)


def requote_mode(stage: str, has_accepted: bool) -> str | None:
    """``version`` (a new version of this trip), ``new_trip`` (a booked trip: a new trip with the version) or None."""
    if stage in PRE_BOOKING and not has_accepted:
        return "version"
    if stage in BOOKED or (stage in PRE_BOOKING and has_accepted):
        return "new_trip"
    return None


async def trip_view(ctx: AnyContext, trip_id: uuid.UUID, stage: str) -> dict[str, Any]:
    """The trip page's guided card: the cases (the tour that was on the departure, what the agent may do now) and the
    case that made this trip (a re-quote of a booked trip)."""
    has_accepted = bool(await ctx.conn.fetchval(ACCEPTED_SQL, trip_id))
    mode = requote_mode(stage, has_accepted)
    cases = []
    for r in await ctx.conn.fetch(CASES_SQL, trip_id):
        tour = await ctx.conn.fetchrow(TOUR_SQL, trip_id, r["departure_id"])
        flagged_now = r["resolution"] is None or (r["resolution"] == "requoted" and r["rebook_trip_id"] is not None)
        cases.append(
            {
                "case_id": str(r["aa_cancel_case_id"]),
                "departure_id": r["departure_id"],
                "resolution": r["resolution"],
                "opened_at": _iso(r["opened_at"]),
                "resolved_at": _iso(r["resolved_at"]),
                "active": flagged_now and stage not in CLOSED_STAGES,
                "tour": None
                if tour is None
                else {
                    "tour_source_id": tour["tour_source_id"],
                    "component_id": tour["component_id"],
                    "component_kind": tour["component_kind"],
                    "title": tour["title"],
                    "service_from": _iso(tour["service_from"]),
                },
                "proposal_id": str(r["proposal_id"]) if r["proposal_id"] else None,
                "proposal_version": r["proposal_version"],
                "rebook_trip_id": str(r["rebook_trip_id"]) if r["rebook_trip_id"] else None,
                "can_requote": r["resolution"] is None and mode is not None and tour is not None,
                "requote_mode": mode,
                "can_cancel": flagged_now and stage in BOOKED,
            }
        )
    rebooked_from = await ctx.conn.fetchval(REBOOKED_FROM_SQL, trip_id)
    return {"cases": cases, "rebooked_from": str(rebooked_from) if rebooked_from else None}


# ── (a) re-quote ─────────────────────────────────────────────────────────────────────────────────


async def _new_trip(ctx: TenantContext, trip: Any, start: dt.date) -> uuid.UUID:
    """A trip in Proposal for the same client and party, the dates moved to ``start`` (same length)."""
    new_id = uuid.uuid4()
    end = None
    if trip["start_date"] is not None and trip["end_date"] is not None:
        end = start + (trip["end_date"] - trip["start_date"])
    await ctx.conn.execute(NEW_TRIP_SQL, trip["trip_id"], new_id, start, end)
    await ctx.conn.execute(NEW_HISTORY_SQL, new_id)
    await ctx.conn.execute(COPY_TRAVELLERS_SQL, trip["trip_id"], new_id)
    await ctx.conn.execute(COPY_PARTY_SQL, trip["trip_id"], new_id)
    ctx.audit("create", "allowed", "trip", str(new_id), reason=REBOOK_REASON, object_ids=(str(trip["trip_id"]),))
    return new_id


async def requote(
    ctx: TenantContext,
    vault: Vault,
    trip_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    departure_id: Any,
    start_date: Any,
) -> dict[str, Any]:
    """(a): the same AA tour on ``departure_id`` (starting ``start_date``) as a new builder version. Idempotent."""
    from app.proposals import builder

    case = await ctx.conn.fetchrow(CASE_LOCK_SQL, case_id, trip_id)
    if case is None:
        raise NotFound("aa_cancel_case_not_found")
    if case["resolution"] == "requoted":
        return _requoted(case["proposal_id"], case["rebook_trip_id"], trip_id)
    if case["resolution"] is not None:
        raise Conflict("aa_cancel_case_closed", "this trip was cancelled")
    new_dep = _ref(departure_id, "invalid_departure_id")
    if new_dep == case["departure_id"]:
        raise Invalid("same_departure", "pick another departure than the one AA cancelled")
    if not isinstance(start_date, dt.date):
        try:
            start_date = dt.date.fromisoformat(str(start_date))
        except ValueError:
            raise Invalid("invalid_start_date", "a date, YYYY-MM-DD") from None
    if await ctx.conn.fetchval(DEPARTURE_STATE_SQL, new_dep) == "cancelled":
        raise Conflict("departure_cancelled", "AA cancelled that departure too")
    trip = await ctx.conn.fetchrow(TRIP_SQL, trip_id)
    if trip is None:
        raise NotFound("trip_not_found")
    mode = requote_mode(trip["stage"], bool(await ctx.conn.fetchval(ACCEPTED_SQL, trip_id)))
    if mode is None:
        raise Conflict("trip_not_open_for_proposal", "a lost, cancelled or completed trip takes no new version")
    source = await ctx.conn.fetchrow(SOURCE_SQL, trip_id, case["departure_id"])
    if source is None:
        raise Conflict("no_builder_version", "no builder version of this trip has the AA tour; add it to a new one")
    rebook_trip = None
    if mode == "version":
        target = await ctx.conn.fetchval(DRAFT_SQL, trip_id)
        if target is None:
            made = await builder.create(ctx, vault, trip_id, currency=None, from_proposal_id=source["proposal_id"])
            target = uuid.UUID(made["proposal_id"])
    else:
        rebook_trip = await _new_trip(ctx, trip, start_date)
        target = await builder.rebook_copy(ctx, vault, rebook_trip, source["proposal_id"], trip_id)
    first_day = await ctx.conn.fetchval(FIRST_DAY_SQL, target, case["departure_id"])
    moved = await ctx.conn.fetch(SWAP_SQL, target, case["departure_id"], new_dep)
    if not moved:
        # The working copy has no option on the departure (the agent removed it): it is theirs, never overwritten.
        raise Conflict("builder_draft_exists", "this trip has a working copy without that AA tour; finish it first",
                       proposal_id=str(target))  # fmt: skip
    starts_on = start_date - dt.timedelta(days=max(int(first_day or 1), 1) - 1)
    await ctx.conn.execute(STARTS_ON_SQL, target, starts_on)
    await builder.touch_draft(ctx, target)
    await ctx.conn.execute(RESOLVE_SQL, case_id, f"member:{ctx.member_id}", target, rebook_trip)
    ctx.audit("aa_cancel_case", "allowed", "aa_cancel_case", str(case_id), reason=f"requoted_{mode}",
              object_ids=tuple(str(x) for x in (target, rebook_trip) if x is not None))  # fmt: skip
    return _requoted(target, rebook_trip, trip_id)


def _requoted(proposal_id: Any, rebook_trip_id: Any, trip_id: uuid.UUID) -> dict[str, Any]:
    return {
        "proposal_id": str(proposal_id),
        "trip_id": str(rebook_trip_id or trip_id),
        "mode": "new_trip" if rebook_trip_id else "version",
    }


# ── (b) cancel ───────────────────────────────────────────────────────────────────────────────────


async def forced_decision(ctx: AnyContext, trip_id: uuid.UUID, reason: str | None) -> Any:
    """The charge decision of a cancel with reason ``aa_cancelled`` on a flagged trip: waived, forced (None when the
    trip is not flagged or the reason is another one: the dialog's own decision stands)."""
    if reason != CANCEL_REASON or await active_case(ctx, trip_id) is None:
        return None
    from app.cancel_policy import rules

    return rules.Decision("waive", None, WAIVER_REASON, forced=True)


async def close_on_cancel(ctx: AnyContext, trip_id: uuid.UUID, actor: str) -> int:
    """The trip cancel run: its flagged cases read ``cancelled`` and the owner task is done."""
    closed = [r["aa_cancel_case_id"] for r in await ctx.conn.fetch(CLOSE_SQL, trip_id, actor)]
    if closed:
        await ctx.conn.execute(CLOSE_TASK_SQL, trip_id, TASK, actor)
        ctx.audit("aa_cancel_case", "allowed", "trip", str(trip_id), reason="cancelled",
                  object_ids=tuple(str(c) for c in closed))  # fmt: skip
    return len(closed)


__all__ = [
    "CANCEL_REASON",
    "EVENT_NAME",
    "TASK",
    "WAIVER_REASON",
    "active_case",
    "close_on_cancel",
    "flagged",
    "forced_decision",
    "open_cases",
    "requote",
    "requote_mode",
    "trip_view",
]
