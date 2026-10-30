"""The AA lines of an accepted proposal and their confirm after the deposit (AA-952, card CRM-23, milestone C-M3
"confirmed"; design v3 F6.4 / F6.6, §3.5, §5.2).

The client accepts a builder version under one live Booking hold set (AA-950: ``crm.proposal.accepted_hold_id``, one
multi-line quote of every chosen AA option, ``line_ref`` = the option id). From there:

1. **Lines** (:func:`due_sets`, :func:`write_lines`): the Booking sync (actor ``job:crm_booking_sync``; members and the
   client link never write AA lines, crm-014 CRM24) reads the hold from Booking (``GET /holds/{id}``, contract 1.2:
   the held quote's ``price_hash``, FX lock and each line's ``b2b_price``) and writes one AA line per held option:
   **the line id is the option id**, so Booking's ``line_ref`` names the line in every later answer (confirm, feed
   events) without a mapping table. Money: cost = the line's ``b2b_price`` (net from Booking, the quote's FX lock,
   ``crm.booking_line_aa_money``), sell = the option's sell (what the agency priced, the builder's margin rules). A
   hold Booking already lets go (expired, released) gives lines ``expired`` and an urgent task for the trip owner
   (re-quote); a hold Booking does not know gives lines ``failed`` (``hold_not_found``) and the task. Once per
   proposal: a proposal with any line of its options is never written again.
2. **Confirm** (:func:`queue_confirms`): once the accepted proposal's deposit is paid and the trip is booked (or
   later), one ``confirm`` request (``crm.aa_request``) for the whole set, on its first line, only while its hold is
   still live and no confirm of that set was ever queued (a confirm Booking refused is not sent again by itself: the
   line reads ``failed`` with the code, the agent decides). :mod:`app.booking.sync` sends it with a stable
   Idempotency-Key and writes Booking's answer on **every** line of the set (one confirm books the whole hold).

Every function here runs in the caller's transaction (``system_tx`` of the sync job). Nothing personal is sent to
Booking: the hold id only.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from app import stages
from app.booking import contract
from app.jobs import queue
from app.jobs.kinds import BOOKING_SYNC
from app.proposals import builder, client_page, rules
from app.tenancy import AnyContext

MAX_SETS = 10
CONFIRM_STAGES = ("booked", "pre_departure", "travelling")
EXPIRED_TASK = "aa_hold_expired"
FAILED_TASK = "aa_lines_failed"
CONFIRM_FAILED_TASK = "aa_confirm_failed"

# Accepted builder versions with a hold whose options have no AA line yet (open trips only: a Lost or cancelled trip
# takes no AA line, its hold runs out at Booking).
DUE_SQL = """
SELECT p.proposal_id, p.trip_id, p.version, p.currency, p.accepted_hold_id, p.starts_on, p.room_config,
       t.party_adults, t.party_child_ages
  FROM crm.proposal p JOIN crm.trip t ON t.trip_id = p.trip_id AND t.deleted_at IS NULL
 WHERE p.status = 'accepted' AND p.kind = 'builder' AND p.accepted_hold_id IS NOT NULL AND p.deleted_at IS NULL
   AND t.stage NOT IN ('lost', 'cancelled', 'completed')
   AND NOT EXISTS (SELECT 1 FROM crm.booking_line l JOIN crm.option o ON o.option_id = l.booking_line_id
                    WHERE o.proposal_id = p.proposal_id)
 ORDER BY p.accepted_at, p.proposal_id
 LIMIT $1
"""
# The same proposal again under its row lock (two sync runs never write one set twice).
LOCK_SQL = """
SELECT p.proposal_id, p.trip_id, p.version, p.currency, p.accepted_hold_id, p.starts_on, p.room_config,
       t.party_adults, t.party_child_ages
  FROM crm.proposal p JOIN crm.trip t ON t.trip_id = p.trip_id AND t.deleted_at IS NULL
 WHERE p.proposal_id = $1 AND p.status = 'accepted' AND p.kind = 'builder' AND p.accepted_hold_id IS NOT NULL
   AND p.deleted_at IS NULL AND t.stage NOT IN ('lost', 'cancelled', 'completed')
   AND NOT EXISTS (SELECT 1 FROM crm.booking_line l JOIN crm.option o ON o.option_id = l.booking_line_id
                    WHERE o.proposal_id = p.proposal_id)
   FOR UPDATE OF p
"""
DURATION_SQL = "SELECT duration_days FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = $1"
SUPPLIER_SQL = "SELECT crm.ensure_aa_supplier()"
INSERT_SQL = """
INSERT INTO crm.booking_line (booking_line_id, trip_id, supplier_kind, agency_supplier_id, departure_id, status,
                              aa_sub_status, title, service_from, service_to, aa_tour_source_id, aa_component_id,
                              aa_component_kind, aa_currency, aa_pax, bk_quote_id, aa_price_hash, aa_quote_version,
                              bk_hold_id, hold_expires_at, aa_result_code)
VALUES ($1, $2, 'aa', $3, $4, 'planning', $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14, $15, $16, $17, $18, $19)
ON CONFLICT (booking_line_id) DO NOTHING
RETURNING booking_line_id
"""
MONEY_SQL = "SELECT crm.booking_line_aa_money($1, $2, $3, $4, $5, $6)"
COMMIT_SQL = """
INSERT INTO crm.trip_commitment (trip_id, kind, line_status, source_ref, checked_at)
VALUES ($1, 'aa_line', $2, $3, now())
ON CONFLICT (tenant_id, kind, source_ref) DO UPDATE SET line_status = EXCLUDED.line_status, checked_at = now()
"""
FEED_SQL = "INSERT INTO crm.booking_feed_state DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING"
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
SELECT $1, $2, 'urgent', t.owner_member_id, 'created', now() + interval '1 day' FROM crm.trip t WHERE t.trip_id = $1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""
# The first line of every set of an accepted proposal whose deposit is paid, the trip booked or later, the hold still
# live, and no confirm of the set ever queued.
CONFIRM_DUE_SQL = """
SELECT DISTINCT ON (l.bk_hold_id) l.booking_line_id, l.trip_id, l.bk_hold_id, p.proposal_id
  FROM crm.booking_line l
  JOIN crm.option o ON o.option_id = l.booking_line_id
  JOIN crm.proposal p ON p.proposal_id = o.proposal_id AND p.status = 'accepted' AND p.deleted_at IS NULL
                     AND p.accepted_hold_id = l.bk_hold_id
  JOIN crm.trip t ON t.trip_id = l.trip_id AND t.deleted_at IS NULL
 WHERE l.supplier_kind = 'aa' AND l.status = 'planning' AND l.aa_sub_status = 'held' AND l.deleted_at IS NULL
   AND l.hold_expires_at > now() AND t.stage = ANY($1::text[])
   AND EXISTS (SELECT 1 FROM crm.payment_schedule s
                WHERE s.proposal_id = p.proposal_id AND s.kind = 'deposit' AND s.status = 'paid'
                  AND s.deleted_at IS NULL)
   AND NOT EXISTS (SELECT 1 FROM crm.aa_request r JOIN crm.booking_line x ON x.booking_line_id = r.booking_line_id
                    WHERE x.trip_id = l.trip_id AND x.bk_hold_id = l.bk_hold_id AND r.kind = 'confirm')
 ORDER BY l.bk_hold_id, l.booking_line_id
"""
CONFIRM_SQL = """
INSERT INTO crm.aa_request (trip_id, booking_line_id, kind) VALUES ($1, $2, 'confirm')
ON CONFLICT (tenant_id, booking_line_id, kind) WHERE status = 'pending' AND deleted_at IS NULL DO NOTHING
RETURNING aa_request_id
"""


async def nudge(ctx: AnyContext, key: str) -> None:
    """Ask the Booking sync to run for this agency now (the minute run would do it anyway); once per ``key``."""
    actor = getattr(ctx, "actor", None) or f"member:{getattr(ctx, 'member_id', '')}"
    await queue.enqueue(ctx.conn, BOOKING_SYNC, {"tenant_id": str(ctx.tenant_id)},
                        idempotency_key=f"{BOOKING_SYNC}:{key}", max_attempts=3, created_by=actor)  # fmt: skip


async def due_sets(ctx: AnyContext, limit: int = MAX_SETS) -> list[dict[str, Any]]:
    return [dict(r) for r in await ctx.conn.fetch(DUE_SQL, limit)]


def line_state(held: contract.HeldSet | None, option_id: str) -> tuple[str, str | None]:
    """``(aa_sub_status, aa_result_code)`` of a new line from Booking's hold answer (None = Booking has no such hold).
    A confirmed hold reads ``held``: the confirm the deposit queues gets its booking back (Booking answers a confirmed
    hold with its booking)."""
    if held is None:
        return "failed", "hold_not_found"
    if option_id not in held.lines:
        return "failed", "not_in_hold"
    if held.status in ("active", "confirmed"):
        return "held", None
    return "expired", "hold_expired" if held.status == "expired" else "hold_released"


async def write_lines(ctx: AnyContext, proposal_id: uuid.UUID, held: contract.HeldSet | None) -> dict[str, Any] | None:
    """Step 1 in one transaction: the AA lines of the accepted proposal from Booking's hold answer. None when the
    proposal no longer needs them (another run wrote them, the trip closed, the proposal changed)."""
    row = await ctx.conn.fetchrow(LOCK_SQL, proposal_id)
    if row is None:
        return None
    s = await builder._structure(ctx, proposal_id)  # type: ignore[arg-type]
    picks = [dict(r) for r in await ctx.conn.fetch(builder.PICKS_SQL, proposal_id)]
    options = builder._option_map(s)
    aa_ids = client_page.aa_chosen(client_page.effective(s, picks)["choice"], options)
    days = {d["proposal_day_id"]: d for d in s["days"]}
    blocks = {b["option_block_id"]: b for b in s["blocks"]}
    pax = rules.room_pax(builder._jsonb(row["room_config"]), int(row["party_adults"]), row["party_child_ages"] or [])
    supplier = await ctx.conn.fetchval(SUPPLIER_SQL)
    hold_id = row["accepted_hold_id"]
    states: list[str] = []
    for oid in aa_ids:
        o = options[oid]
        day_no = days[blocks[o["option_block_id"]]["proposal_day_id"]]["day_no"]
        start = builder._day_date(row["starts_on"], day_no)
        if o["aa_nights"]:
            end = start + dt.timedelta(days=int(o["aa_nights"])) if start else None
        elif o["aa_component_kind"] == "trip_plan" and start is not None:
            duration = await ctx.conn.fetchval(DURATION_SQL, o["aa_tour_source_id"])
            end = start + dt.timedelta(days=max(int(duration or 1), 1) - 1)
        else:
            end = start
        sub, code = line_state(held, oid)
        counts = {"adult": int(pax["adult"]), "child": int(pax["child"]), "infant": int(pax["infant"])}
        if o["aa_nights"]:
            counts["nights"] = int(o["aa_nights"])
        if pax.get("rooms"):
            counts["rooms"] = pax["rooms"]
        made = await ctx.conn.fetchval(
            INSERT_SQL, uuid.UUID(oid), row["trip_id"], supplier, o["aa_departure_id"], sub, o["title"][:200], start,
            end, o["aa_tour_source_id"], o["aa_component_id"], o["aa_component_kind"], row["currency"],
            json.dumps(counts), held.quote_id if held else None, held.price_hash if held else None,
            int(row["version"]), hold_id, held.expires_at if held else None, code,
        )  # fmt: skip
        if made is None:
            continue
        states.append(sub)
        line = held.lines.get(oid) if held else None
        if held is not None and line is not None:
            # Net from Booking (the held quote's line price, its FX lock); the sell is the agency's (the option).
            await ctx.conn.fetchval(MONEY_SQL, made, "cost", line.b2b_price, held.currency, held.rate_per_usd,
                                    held.rate_date)  # fmt: skip
            await ctx.conn.fetchval(MONEY_SQL, made, "sell", rules.money(o["sell_amount"]), held.currency,
                                    held.rate_per_usd, held.rate_date)  # fmt: skip
        ctx.audit("aa_sync", "allowed", "booking_line", str(made), reason=f"proposal_line_{sub}")
    if not states:
        return None
    await ctx.conn.execute(FEED_SQL)
    if held is not None:
        commitment = "held" if "held" in states else ("released" if held.status == "released" else "expired")
        await ctx.conn.execute(COMMIT_SQL, row["trip_id"], commitment, hold_id)
    if "expired" in states:
        await ctx.conn.execute(TASK_SQL, row["trip_id"], EXPIRED_TASK)
    if "failed" in states:
        await ctx.conn.execute(TASK_SQL, row["trip_id"], FAILED_TASK)
    outcome = "held" if set(states) == {"held"} else ("expired" if "expired" in states else "failed")
    ctx.audit("aa_lines_from_proposal", "allowed", "proposal", str(proposal_id), reason=outcome)
    await stages.refresh_waiting_on(ctx, row["trip_id"])
    return {"proposal_id": str(proposal_id), "lines": len(states), "outcome": outcome}


async def queue_confirms(ctx: AnyContext) -> list[uuid.UUID]:
    """Step 2: one confirm request per set due (see the module text); returns the requests queued."""
    queued = []
    for row in await ctx.conn.fetch(CONFIRM_DUE_SQL, list(CONFIRM_STAGES)):
        request_id = await ctx.conn.fetchval(CONFIRM_SQL, row["trip_id"], row["booking_line_id"])
        if request_id is not None:
            queued.append(request_id)
            ctx.audit("aa_request", "allowed", "booking_line", str(row["booking_line_id"]), reason="confirm_deposit")
    return queued
