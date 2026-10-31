"""Trip events and the agency bell (card CRM-21b, AA-950; design v3 R4, Fix A3, A-00 bell; migration crm-021).

One ``crm.metric_event`` row per event of a trip (``proposal.viewed``, ``proposal.option_chosen``, ``proposal.rehold``
...). ``detail`` holds ids, codes, counts and product titles only (never a name, a contact or free text). An event
with ``bell=True`` is addressed to the trip owner (``bell_member_id``): it is an item of their bell until they mark
it seen. The bell bundles at read time: unseen events of one (proposal, name) are one line with a count, so several
swaps of one client are one "Client changed their choices (3)" line, not three.

- :func:`record`: one event (every context; the client page writes through ``client_link_tx``).
- :func:`bell`: the caller's unseen items, grouped (``TRIP_VIEW``).
- :func:`mark_seen`: mark the caller's items seen (all, or the listed groups).
- :func:`nudge_due`: R4, the hourly run: a sent version the client viewed 2 days ago without choosing anything or
  accepting gives the trip owner one follow-up task and a bell item, once per version.

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.tenancy import AnyContext, TenantContext

VIEWED = "proposal.viewed"
OPTION_CHOSEN = "proposal.option_chosen"
CHANGE_REQUESTED = "proposal.change_requested"
HOLD_PLACED = "proposal.hold_placed"
REHOLD = "proposal.rehold"
RECHECK_REFUSED = "proposal.recheck_refused"
OPTION_SOLD_OUT = "proposal.option_sold_out"
HOLD_FAILED = "proposal.hold_failed"
BOOKING_UNAVAILABLE = "proposal.booking_unavailable"
ACCEPTED = "proposal.accepted"
NUDGE_DUE = "proposal.nudge_due"
TERMS_REACCEPTED = "proposal.terms_reaccepted"  # AA-955: the client accepted the agency's newer terms version
# AA-952 slice 4 (C31, Fix A8, U8): the client says they paid; an invoice is overdue (agent first); the money of a late
# payment re-held the AA items, or could not (paid_unfulfillable: a refund request waits).
PAYMENT_CLAIMED = "invoice.payment_claimed"
INVOICE_OVERDUE = "invoice.overdue"
INVOICE_REHELD = "invoice.reheld"
INVOICE_UNFULFILLABLE = "invoice.unfulfillable"
NAMES = (
    VIEWED, OPTION_CHOSEN, CHANGE_REQUESTED, HOLD_PLACED, REHOLD, RECHECK_REFUSED, OPTION_SOLD_OUT, HOLD_FAILED,
    BOOKING_UNAVAILABLE, ACCEPTED, NUDGE_DUE, TERMS_REACCEPTED, PAYMENT_CLAIMED, INVOICE_OVERDUE, INVOICE_REHELD,
    INVOICE_UNFULFILLABLE,
)  # fmt: skip
NUDGE_AFTER_DAYS = 2
MAX_BELL = 50
MAX_DETAIL = 3500

OWNER_SQL = "SELECT owner_member_id FROM crm.trip WHERE trip_id = $1"
INSERT_SQL = """
INSERT INTO crm.metric_event (trip_id, proposal_id, name, detail, bell_member_id)
VALUES ($1, $2, $3, $4::jsonb, $5)
RETURNING metric_event_id
"""
RECENT_SQL = """
SELECT EXISTS (SELECT 1 FROM crm.metric_event WHERE proposal_id = $1 AND name = $2
                  AND created_at > now() - make_interval(secs => $3))
"""
BELL_SQL = """
SELECT e.proposal_id, e.trip_id, e.name, count(*)::int AS n, max(e.created_at) AS last_at,
       (array_agg(e.detail ORDER BY e.created_at DESC))[1] AS detail, t.title AS trip_title, p.version
  FROM crm.metric_event e
  JOIN crm.trip t ON t.trip_id = e.trip_id AND t.deleted_at IS NULL
  LEFT JOIN crm.proposal p ON p.proposal_id = e.proposal_id
 WHERE e.bell_member_id = $1 AND e.seen_at IS NULL AND e.deleted_at IS NULL
 GROUP BY e.proposal_id, e.trip_id, e.name, t.title, p.version
 ORDER BY max(e.created_at) DESC
 LIMIT $2
"""
SEEN_ALL_SQL = """
UPDATE crm.metric_event SET seen_at = now() WHERE bell_member_id = $1 AND seen_at IS NULL
"""
SEEN_TRIP_SQL = """
UPDATE crm.metric_event SET seen_at = now() WHERE bell_member_id = $1 AND seen_at IS NULL AND trip_id = ANY($2::uuid[])
"""
TRIP_EVENTS_SQL = """
SELECT name, detail, created_at FROM crm.metric_event
 WHERE proposal_id = $1 AND name = ANY($2::text[]) ORDER BY created_at DESC LIMIT 20
"""
NUDGE_SQL = """
SELECT p.proposal_id, p.trip_id, p.version
  FROM crm.proposal p JOIN crm.trip t ON t.trip_id = p.trip_id AND t.deleted_at IS NULL AND t.stage = 'proposal'
 WHERE p.status = 'sent' AND p.nudged_at IS NULL AND p.first_viewed_at IS NOT NULL AND p.deleted_at IS NULL
   AND p.first_viewed_at <= now() - make_interval(days => $1)
   AND NOT EXISTS (SELECT 1 FROM crm.selection s WHERE s.proposal_id = p.proposal_id AND s.chosen_by = 'client')
 ORDER BY p.first_viewed_at
 LIMIT $2
   FOR UPDATE OF p SKIP LOCKED
"""
NUDGED_SQL = "UPDATE crm.proposal SET nudged_at = now() WHERE proposal_id = $1"
NUDGE_TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
SELECT $1, 'proposal_follow_up', 'normal', t.owner_member_id, 'created', now() + interval '1 day'
  FROM crm.trip t WHERE t.trip_id = $1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""


def _detail(detail: dict[str, Any] | None) -> str:
    text = json.dumps(detail or {}, separators=(",", ":"), default=str)
    if len(text.encode()) > MAX_DETAIL:  # a long diff is cut to its counts (the CHECK caps the column at 4000 bytes)
        keep = {k: v for k, v in (detail or {}).items() if isinstance(v, (int, str, bool)) and len(str(v)) <= 200}
        text = json.dumps({**keep, "truncated": True}, separators=(",", ":"), default=str)
    return text


async def record(
    ctx: AnyContext,
    trip_id: uuid.UUID,
    name: str,
    *,
    proposal_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
    bell: bool = False,
) -> uuid.UUID:
    """One event; ``bell`` addresses it to the trip owner (none when the trip has no owner)."""
    if name not in NAMES:
        raise ValueError(f"unknown event {name}")
    member = await ctx.conn.fetchval(OWNER_SQL, trip_id) if bell else None
    return await ctx.conn.fetchval(INSERT_SQL, trip_id, proposal_id, name, _detail(detail), member)


async def recent(ctx: AnyContext, proposal_id: uuid.UUID, name: str, seconds: int) -> bool:
    """An event of this name for this proposal in the last ``seconds`` (dedupe of repeated page reads / outages)."""
    return bool(await ctx.conn.fetchval(RECENT_SQL, proposal_id, name, seconds))


async def bell(ctx: TenantContext) -> dict[str, Any]:
    rows = await ctx.conn.fetch(BELL_SQL, ctx.member_id, MAX_BELL)
    items = [
        {
            "trip_id": str(r["trip_id"]),
            "trip_title": r["trip_title"],
            "proposal_id": str(r["proposal_id"]) if r["proposal_id"] else None,
            "version": r["version"],
            "name": r["name"],
            "count": r["n"],
            "last_at": r["last_at"].isoformat(),
            "detail": json.loads(r["detail"]) if isinstance(r["detail"], str) else r["detail"],
        }
        for r in rows
    ]
    ctx.audit("view", "allowed", "member", str(ctx.member_id), fields=("bell",))
    return {"items": items, "unseen": sum(i["count"] for i in items)}


async def mark_seen(ctx: TenantContext, trip_ids: list[uuid.UUID] | None) -> dict[str, Any]:
    if trip_ids:
        result = await ctx.conn.execute(SEEN_TRIP_SQL, ctx.member_id, trip_ids)
    else:
        result = await ctx.conn.execute(SEEN_ALL_SQL, ctx.member_id)
    return {"seen": int(result.split()[-1]) if result else 0}


async def proposal_events(ctx: AnyContext, proposal_id: uuid.UUID, names: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        {
            "name": r["name"],
            "at": r["created_at"].isoformat(),
            "detail": json.loads(r["detail"]) if isinstance(r["detail"], str) else r["detail"],
        }
        for r in await ctx.conn.fetch(TRIP_EVENTS_SQL, proposal_id, list(names))
    ]


async def nudge_due(ctx: AnyContext, *, limit: int = 200) -> int:
    """R4 (hourly run): viewed :data:`NUDGE_AFTER_DAYS` days ago, nothing chosen, not accepted -> one follow-up task
    for the trip owner and a bell item; ``nudged_at`` is set so it happens once per version."""
    done = 0
    for row in await ctx.conn.fetch(NUDGE_SQL, NUDGE_AFTER_DAYS, limit):
        await ctx.conn.execute(NUDGED_SQL, row["proposal_id"])
        await ctx.conn.execute(NUDGE_TASK_SQL, row["trip_id"])
        await record(ctx, row["trip_id"], NUDGE_DUE, proposal_id=row["proposal_id"],
                     detail={"version": row["version"], "days": NUDGE_AFTER_DAYS}, bell=True)  # fmt: skip
        ctx.audit("proposal_nudge", "allowed", "proposal", str(row["proposal_id"]))
        done += 1
    return done
