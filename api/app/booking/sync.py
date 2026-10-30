"""``crm_booking_sync`` (AA-947, card CRM-19): the AA outage queue and the Booking event feed.

Runs every minute (Recurring, deploy-gate and DB Migrate idle exempt) for every active agency, and at once for one
agency when a member asks for something (a nudge row, payload ``tenant_id``). Actor ``job:crm_booking_sync`` (the
crm-014 line guard lets a job write AA lines; crm-017 ``crm.booking_line_aa_money`` lets it write their money).

**Requests** (``crm.aa_request``, at most :data:`MAX_REQUESTS` per agency and run):

1. claim due pending rows with a lease (``FOR UPDATE SKIP LOCKED``; a run never sends a request another run holds);
2. call Booking **outside** any transaction (:func:`perform`): quote / requote (and the "from" price of a trip plan),
   hold (a ``price_changed`` answer re-quotes once and the hold is tried again at the new price), confirm, release,
   refresh (``GET /holds/{id}``). Holds and confirms carry ``Idempotency-Key: crm-<kind>:<request id>[:v<version>]``,
   the same on every retry, so Booking answers a retry with the hold / booking it already made (card E: no
   duplicates);
3. apply the answer in one transaction (:func:`apply`): the line (sub-status, quote, hold, booking, the money with the
   quote's FX lock), ``crm.trip_commitment`` (the delete block: held / confirmed / expired / released / declined), the
   request (done / failed with a code), ``waiting_on``. A transient failure (Booking down, a timeout, a 5xx, a 401)
   keeps the request pending with a backoff (:func:`backoff_seconds`): the line reads "AA pending" with the next try.
   After :data:`MAX_ATTEMPTS` the request fails ``gave_up``. ``apply`` decides from the line and trip as they are under
   the line lock, not as claimed: a hold that lands after its line was released keeps only its hold id and queues the
   release that gives the seats back.

**Accepted proposals** (AA-952, :mod:`app.booking.proposal_lines`, before the requests): the AA lines of an accepted
builder version are written from its hold set (``GET /holds/{id}``, contract 1.2: net = each line's ``b2b_price``),
and once its deposit is paid one ``confirm`` request is queued for the set. A confirm, a refusal of it and a release
act on the whole Booking hold, so :func:`apply` writes them on **every** line of the set (the lines sharing the hold
id in the trip), not only on the line the request names; a refused confirm reads ``failed`` (or ``expired`` /
``sold_out`` / ``not_served``) with Booking's code and opens an urgent task; the trip's stage never moves back.

A quote / requote / hold / refresh whose trip is Lost or cancelled is dropped (``trip_closed``) without calling Booking
(the Lost move held it back by the Undo window first, so an Undo leaves it to run). When Booking gives no answer at all
(:data:`OUTAGE_CLASSES`) the rest of the agency's batch goes back to the queue unsent and the feed waits: one timeout
per agency and run, so an outage never holds the worker for minutes.

**Feed** (``GET /events``, agencies with a ``crm.booking_feed_state`` row): up to :data:`FEED_MAX_PAGES` pages; each
event in its own transaction, recorded once in ``crm.inbox_events`` (a replayed page applies nothing twice); the
cursor moves only from the value this run read (compare and set). ``hold.expired`` -> expired; ``hold.extended`` -> a
refresh request; ``line.confirmed`` -> confirmed; ``line.declined`` -> cancelled / declined (the Lost hint) and an
urgent task for the trip owner; ``booking.cancelled`` -> cancelled; ``departure.*`` -> recorded ``informational``
(the incident banner is a later card); an unknown type -> ``unknown_type``. A feed failure backs off like a request.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import asyncpg

from app import stages
from app.audit_export import registry
from app.booking import contract, proposal_lines
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.errors import Forbidden, NotFound
from app.jobs.queue import Job
from app.tenancy import SystemContext, system_tx

log = logging.getLogger(__name__)

ACTOR = "job:crm_booking_sync"
LEASE_S = 120
MAX_REQUESTS = 20
MAX_ATTEMPTS = 96
BACKOFF_BASE_S = 30
BACKOFF_MAX_S = 900
FEED_PAGE = 200
FEED_MAX_PAGES = 5
STUCK_FAILURES = 10
DECLINE_TASK = "aa_line_declined"
CLOSED_STAGES = ("lost", "cancelled")
# Requests that make no sense once the trip is closed: dropped (``trip_closed``) without calling Booking. A Lost or a
# cancel holds them back by the Undo window first (app.booking.aa_lines.close_trip), so an Undo leaves them to run.
DROP_ON_CLOSE = ("quote", "requote", "hold", "refresh")
# Booking (or the token pool) gave no answer at all: the rest of the batch is not sent this run (card E: one timeout per
# agency and run, never one per request, so an outage does not hold the one-job-at-a-time worker for minutes).
OUTAGE_CLASSES = frozenset({"booking_timeout", "booking_unreachable", "booking_unavailable", "token_unavailable"})

CLAIM_SQL = """
UPDATE crm.aa_request r SET lease_until = now() + make_interval(secs => $2), attempts = r.attempts + 1
 WHERE r.aa_request_id IN (
        SELECT q.aa_request_id FROM crm.aa_request q
         WHERE q.status = 'pending' AND q.deleted_at IS NULL AND q.next_attempt_at <= now()
           AND (q.lease_until IS NULL OR q.lease_until < now())
         ORDER BY q.next_attempt_at, q.created_at
         LIMIT $1
           FOR UPDATE SKIP LOCKED)
RETURNING r.aa_request_id, r.booking_line_id, r.kind, r.attempts, r.sell_amount, r.result_code
"""
LINE_SQL = """
SELECT l.booking_line_id, l.trip_id, l.status, l.aa_sub_status, l.aa_tour_source_id, l.aa_component_id,
       l.aa_component_kind, l.aa_currency, l.aa_pax, l.departure_id, l.service_from, l.bk_quote_id, l.aa_price_hash,
       l.aa_quote_version, l.bk_hold_id, l.bk_booking_id, l.from_price, t.stage AS trip_stage,
       t.deleted_at AS trip_deleted_at,
       (SELECT m.amount FROM crm.v_booking_line_money_current m
         WHERE m.booking_line_id = l.booking_line_id AND m.kind = 'sell' AND m.currency = l.aa_currency) AS sell_amount
  FROM crm.booking_line l JOIN crm.trip t ON t.trip_id = l.trip_id
 WHERE l.booking_line_id = $1
"""
REQUEST_STATUS_SQL = "SELECT status FROM crm.aa_request WHERE aa_request_id = $1 FOR UPDATE"
LOCK_LINE_SQL = "SELECT 1 FROM crm.booking_line WHERE booking_line_id = $1 FOR UPDATE"
RETRY_SQL = """
UPDATE crm.aa_request SET lease_until = NULL, last_error_class = $2, next_attempt_at = now() + make_interval(secs => $3)
 WHERE aa_request_id = $1 AND status = 'pending'
"""
FINISH_SQL = """
UPDATE crm.aa_request SET status = $2, result_code = $3, finished_at = now(), lease_until = NULL
 WHERE aa_request_id = $1 AND status = 'pending'
"""
AGAIN_SQL = """
UPDATE crm.aa_request SET lease_until = NULL, result_code = $2, next_attempt_at = now()
 WHERE aa_request_id = $1 AND status = 'pending'
"""
QUOTED_SQL = """
UPDATE crm.booking_line
   SET aa_sub_status = $2, bk_quote_id = $3, aa_price_hash = $4, quote_valid_until = $5, aa_quote_version = $6,
       departure_id = COALESCE($7, departure_id), from_price = $8, below_from_price = $9, aa_result_code = $10
 WHERE booking_line_id = $1
"""
SUB_SQL = "UPDATE crm.booking_line SET aa_sub_status = $2, aa_result_code = $3 WHERE booking_line_id = $1"
HELD_SQL = """
UPDATE crm.booking_line SET aa_sub_status = 'held', bk_hold_id = $2, hold_expires_at = $3, aa_result_code = NULL
 WHERE booking_line_id = $1
"""
ORPHAN_HOLD_SQL = """
UPDATE crm.booking_line SET bk_hold_id = $2, hold_expires_at = $3 WHERE booking_line_id = $1
"""
HOLD_STATE_SQL = """
UPDATE crm.booking_line SET aa_sub_status = $2, hold_expires_at = $3 WHERE booking_line_id = $1
"""
BOOKED_SQL = """
UPDATE crm.booking_line
   SET bk_booking_id = $2, confirmation_no = $3, status = $4, aa_sub_status = $5, confirm_class = $6,
       aa_result_code = NULL
 WHERE booking_line_id = $1
"""
CLOSE_LINE_SQL = """
UPDATE crm.booking_line SET status = 'cancelled', aa_sub_status = $2, aa_decline_code = $3, aa_result_code = $4
 WHERE booking_line_id = $1 AND status <> 'cancelled'
"""
CONFIRM_LINE_SQL = """
UPDATE crm.booking_line SET status = 'confirmed', aa_sub_status = NULL, confirm_class = 'confirmed'
 WHERE booking_line_id = $1 AND status = 'planning'
"""
MONEY_SQL = "SELECT crm.booking_line_aa_money($1, $2, $3, $4, $5, $6)"
COMMIT_SQL = """
INSERT INTO crm.trip_commitment (trip_id, kind, line_status, source_ref, checked_at)
VALUES ($1, 'aa_line', $2, $3, now())
ON CONFLICT (tenant_id, kind, source_ref) DO UPDATE SET line_status = EXCLUDED.line_status, checked_at = now()
"""
REFRESH_SQL = """
INSERT INTO crm.aa_request (trip_id, booking_line_id, kind) VALUES ($1, $2, $3)
ON CONFLICT (tenant_id, booking_line_id, kind) WHERE status = 'pending' AND deleted_at IS NULL DO NOTHING
RETURNING aa_request_id
"""
# AA-952: the other lines of the same Booking hold set in the trip (an accepted proposal's lines share one hold).
SET_LINES_SQL = """
SELECT booking_line_id FROM crm.booking_line
 WHERE trip_id = $1 AND bk_hold_id = $2 AND booking_line_id <> $3 AND supplier_kind = 'aa' AND status = 'planning'
   AND deleted_at IS NULL
 ORDER BY booking_line_id
   FOR UPDATE
"""
CANCEL_OTHERS_SQL = """
UPDATE crm.aa_request SET status = 'cancelled', finished_at = now(), result_code = 'released', lease_until = NULL
 WHERE booking_line_id = $1 AND status = 'pending' AND aa_request_id <> $2
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
SELECT $1, $2, 'urgent', t.owner_member_id, 'created', now() + interval '1 day' FROM crm.trip t WHERE t.trip_id = $1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""
EVENT_LINES_SQL = """
SELECT l.booking_line_id, l.trip_id, l.status, l.aa_sub_status, l.bk_hold_id
  FROM crm.booking_line l
 WHERE l.supplier_kind = 'aa'
   AND (($1::uuid IS NOT NULL AND l.booking_line_id = $1)
        OR ($1::uuid IS NULL AND $2::text IS NOT NULL AND l.bk_hold_id = $2)
        OR ($1::uuid IS NULL AND $3::text IS NOT NULL AND l.bk_booking_id = $3))
 ORDER BY l.booking_line_id
   FOR UPDATE
"""
SEEN_SQL = """
SELECT 1 FROM crm.inbox_events WHERE source = 'booking' AND contract = 'booking.b2b.v1' AND idempotency_key = $1
"""
INBOX_SQL = """
INSERT INTO crm.inbox_events (source, contract, idempotency_key, event_type, outcome, reason_code)
VALUES ('booking', 'booking.b2b.v1', $1, $2, $3, $4)
ON CONFLICT (tenant_id, source, contract, idempotency_key) DO NOTHING
RETURNING id
"""
FEED_LOAD_SQL = "SELECT feed_cursor, failures, last_error_at FROM crm.booking_feed_state"
FEED_ADVANCE_SQL = """
UPDATE crm.booking_feed_state SET feed_cursor = $2, failures = 0, last_error_class = NULL, last_ok_at = now()
 WHERE feed_cursor IS NOT DISTINCT FROM $1
RETURNING 1
"""
FEED_OK_SQL = "UPDATE crm.booking_feed_state SET failures = 0, last_error_class = NULL, last_ok_at = now()"
FEED_FAIL_SQL = """
UPDATE crm.booking_feed_state
   SET failures = least(failures + 1, 1000000), last_error_class = $1, last_error_at = now()
RETURNING failures
"""
# A release calls Booking for a hold in these sub-statuses ("released": a hold that landed after its line was released;
# apply() keeps its id and queues the release).
RELEASABLE = frozenset({"held", "pending", "quoted", "failed", "sold_out", "not_served", "released"})
HOLD_STATUS = {"active": "held", "confirmed": "confirmed", "expired": "expired", "released": "released"}
# A line Booking booked: (status, aa_sub_status, confirm_class) for BOOKED_SQL.
BOOKED_ARGS = {"confirmed": ("confirmed", None, "confirmed"), "on_request": ("planning", "on_request", "aa_on_request")}


def backoff_seconds(attempts: int) -> int:
    """Wait after the ``attempts``-th failed try: 30 s, 60 s, 120 s, ... capped at 15 minutes."""
    return min(BACKOFF_BASE_S * 2 ** min(max(attempts - 1, 0), 10), BACKOFF_MAX_S)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else (value or {})


def proposal_ref(line_id: uuid.UUID) -> str:
    """Until CRM-21b ties holds to a proposal version, each AA line is its own Booking hold set."""
    return f"crm-line:{line_id}"


# ── 1. what Booking answered ─────────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    """The answer of one request, as values (no DB here). ``retry``: a transient error class (stay pending);
    ``code``: the result code of a final answer; ``again``: re-run the request now (a hold after its re-quote);
    ``cancelled``: the request is dropped, Booking was not called (its trip or line closed)."""

    quote: contract.QuoteLine | None = None
    from_price: Decimal | None = None
    hold: contract.Hold | None = None
    booking: contract.Booking | None = None
    booking_lines: dict[str, str] = field(default_factory=dict)  # AA-952: every line_ref of the confirm answer
    released: bool = False
    code: str | None = None
    failed: bool = False
    retry: str | None = None
    again: bool = False
    cancelled: bool = False


@dataclass(frozen=True)
class Work:
    request: dict[str, Any]
    line: dict[str, Any]

    @property
    def line_id(self) -> uuid.UUID:
        return self.line["booking_line_id"]


def quote_body(line: dict[str, Any], version: int) -> dict[str, Any]:
    pax = _json(line["aa_pax"])
    item: dict[str, Any] = {
        "line_ref": str(line["booking_line_id"]),
        "component_id": line["aa_component_id"],
        "service_date": line["service_from"].isoformat(),
        "pax": {
            "adult": int(pax.get("adult", 1)),
            "child": int(pax.get("child", 0)),
            "infant": int(pax.get("infant", 0)),
        },
    }
    if line["departure_id"]:
        item["departure_id"] = line["departure_id"]
    if pax.get("nights"):
        item["nights"] = int(pax["nights"])
    if pax.get("rooms"):
        item["rooms"] = pax["rooms"]
    return {
        "proposal_ref": proposal_ref(line["booking_line_id"]),
        "proposal_version": version,
        "currency": line["aa_currency"],
        "lines": [item],
    }


async def _quote(client: BookingClient, tenant_id: uuid.UUID, line: dict[str, Any]) -> Outcome:
    version = int(line["aa_quote_version"] or 0) + 1
    try:
        data = await client.quote(tenant_id, quote_body(line, version))
    except BookingRefusal as exc:
        return Outcome(code=exc.code, failed=True)
    out = Outcome(quote=contract.quote_line(data, str(line["booking_line_id"])), code="quoted")
    if out.quote.availability == "sold_out":
        out.code = "sold_out"
    if line["aa_component_kind"] == "trip_plan":
        try:  # the "from" price is a warning: its absence never fails the quote
            parity = await client.price_parity(tenant_id, line["aa_tour_source_id"], line["aa_currency"])
            out.from_price = contract.from_price(parity, line["aa_currency"])
        except (BookingUnavailable, BookingRefusal, contract.ContractError):
            out.from_price = None
    return out


async def _hold(client: BookingClient, tenant_id: uuid.UUID, w: Work) -> Outcome:
    line = w.line
    version = int(line["aa_quote_version"] or 1)
    body = {
        "quote_id": line["bk_quote_id"],
        "price_hash": line["aa_price_hash"],
        "proposal_ref": proposal_ref(w.line_id),
        "proposal_version": version,
    }
    key = f"crm-hold:{w.request['aa_request_id']}:v{version}"
    try:
        data = await client.hold(tenant_id, body, idempotency_key=key)
    except BookingRefusal as exc:
        if exc.code == "price_changed" and w.request.get("result_code") != "requoted":
            # The quote moved (or expired): re-quote, store it, then the hold runs again at the new price.
            out = await _quote(client, tenant_id, line)
            if out.quote is not None and out.code == "quoted":
                out.again = True
                out.code = "requoted"
            return out
        return Outcome(code=exc.code, failed=True)
    return Outcome(hold=contract.hold(data, str(w.line_id)), code="held")


async def _confirm(client: BookingClient, tenant_id: uuid.UUID, w: Work) -> Outcome:
    key = f"crm-confirm:{w.request['aa_request_id']}"
    try:
        data = await client.confirm(
            tenant_id, w.line["bk_hold_id"], {"price_hash": w.line["aa_price_hash"]}, idempotency_key=key
        )
    except BookingRefusal as exc:
        return Outcome(code=exc.code, failed=True)
    booked = contract.booking(data, str(w.line_id))
    code = "confirmed" if booked.line_status == "confirmed" else booked.line_status
    return Outcome(booking=booked, booking_lines=contract.booking_lines(data), code=code)


async def _release(client: BookingClient, tenant_id: uuid.UUID, w: Work) -> Outcome:
    hold_id = w.line["bk_hold_id"]
    if hold_id and w.line["aa_sub_status"] in RELEASABLE:
        try:
            await client.release(tenant_id, hold_id)
        except BookingRefusal as exc:
            if exc.code != "not_found":
                return Outcome(code="hold_confirmed" if exc.code == "invalid" else exc.code, failed=True)
    return Outcome(released=True, code="released")


async def _refresh(client: BookingClient, tenant_id: uuid.UUID, w: Work) -> Outcome:
    if not w.line["bk_hold_id"]:
        return Outcome(code="no_hold")
    try:
        data = await client.get_hold(tenant_id, w.line["bk_hold_id"])
    except BookingRefusal as exc:
        return Outcome(code=exc.code, failed=True)
    return Outcome(hold=contract.hold(data, str(w.line_id)), code="refreshed")


async def perform(client: BookingClient, tenant_id: uuid.UUID, w: Work) -> Outcome:
    """Call Booking for one request; never raises for a Booking failure (it becomes ``retry`` or a final ``code``)."""
    kind = w.request["kind"]
    try:
        if kind in ("quote", "requote"):
            return await _quote(client, tenant_id, w.line)
        if kind == "hold":
            return await _hold(client, tenant_id, w)
        if kind == "confirm":
            return await _confirm(client, tenant_id, w)
        if kind == "release":
            return await _release(client, tenant_id, w)
        return await _refresh(client, tenant_id, w)
    except BookingUnavailable as exc:
        return Outcome(retry=exc.error_class)
    except contract.ContractError:
        return Outcome(retry="booking_contract")


# ── 2. applying it ───────────────────────────────────────────────────────────────────────────────

# A final refusal -> the line's sub-status (None = unchanged).
REFUSED_SUB = {
    "sold_out": "sold_out",
    "tour_not_served": "not_served",
    "hold_expired": "expired",
    "unknown_partner": "failed",
    "forbidden": "failed",
}


def below_from_price(sell: Decimal | None, pax: dict[str, Any], from_price: Decimal | None) -> bool | None:
    """The agency's sell per person (adults + children) below AA's public "from" price (warn, never block)."""
    people = int(pax.get("adult", 0) or 0) + int(pax.get("child", 0) or 0)
    if sell is None or from_price is None or people <= 0:
        return None
    return sell / people < from_price


async def _money(ctx: SystemContext, w: Work, q: contract.QuoteLine, sell: Decimal | None) -> None:
    await ctx.conn.fetchval(MONEY_SQL, w.line_id, "cost", q.b2b_price, q.currency, q.rate_per_usd, q.rate_date)
    if sell is not None:
        await ctx.conn.fetchval(MONEY_SQL, w.line_id, "sell", sell, q.currency, q.rate_per_usd, q.rate_date)


async def _apply_quote(ctx: SystemContext, w: Work, out: Outcome) -> None:
    q = out.quote
    assert q is not None  # noqa: S101 — the caller checked
    request_sell = w.request.get("sell_amount")
    sell = Decimal(request_sell) if request_sell is not None else w.line["sell_amount"]
    sub = "sold_out" if q.availability == "sold_out" else "quoted"
    await ctx.conn.execute(
        QUOTED_SQL,
        w.line_id,
        sub,
        q.quote_id,
        q.price_hash,
        q.valid_until,
        int(w.line["aa_quote_version"] or 0) + 1,
        q.departure_id,
        out.from_price,
        below_from_price(sell, _json(w.line["aa_pax"]), out.from_price),
        "sold_out" if sub == "sold_out" else None,
    )
    await _money(ctx, w, q, sell)


async def apply(ctx: SystemContext, w: Work, out: Outcome) -> str:
    """Store one answer; returns ``retry`` / ``again`` / the request's final status.

    Decides from the line and trip as they are under the line lock (``cur``), never from the claim-time snapshot in
    ``w``: while the call was on its way another run may have released the line (and cancelled this request) or the
    trip may have closed. ``w.line`` still gives what was sent (the quote version, pax, the sell at claim)."""
    request_id, line_id = w.request["aa_request_id"], w.line_id
    status = await ctx.conn.fetchval(REQUEST_STATUS_SQL, request_id)
    await ctx.conn.execute(LOCK_LINE_SQL, line_id)
    cur = dict(await ctx.conn.fetchrow(LINE_SQL, line_id))
    trip_id = cur["trip_id"]
    kind = w.request["kind"]
    line_closed = cur["status"] == "cancelled"
    if out.cancelled or (line_closed and out.quote is not None and out.hold is None):
        # Dropped before the call (closed trip), or a quote answer for a line that closed meanwhile: nothing to store.
        code = out.code if out.cancelled else "line_closed"
        if status == "pending":
            await ctx.conn.execute(FINISH_SQL, request_id, "cancelled", code)
        ctx.audit("aa_sync", "allowed", "booking_line", str(line_id), reason=f"{kind}_{code}"[:40])
        return "cancelled"
    if out.retry is not None:
        if w.request["attempts"] >= MAX_ATTEMPTS:
            await ctx.conn.execute(FINISH_SQL, request_id, "failed", "gave_up")
            if cur["aa_sub_status"] == "pending":
                await ctx.conn.execute(SUB_SQL, line_id, "failed", "gave_up")
            return "failed"
        await ctx.conn.execute(RETRY_SQL, request_id, out.retry, backoff_seconds(w.request["attempts"]))
        return "retry"
    if out.quote is not None:
        await _apply_quote(ctx, w, out)
    if out.again:
        await ctx.conn.execute(AGAIN_SQL, request_id, "requoted")
        return "again"
    # AA-952: a confirm or a release acts on the whole Booking hold: the other lines of its set follow.
    siblings: list[uuid.UUID] = []
    if kind in ("confirm", "release") and cur["bk_hold_id"] and not line_closed:
        siblings = [r["booking_line_id"] for r in await ctx.conn.fetch(SET_LINES_SQL, trip_id, cur["bk_hold_id"],
                                                                          line_id)]  # fmt: skip
    if out.failed:
        sub = REFUSED_SUB.get(out.code or "")
        if sub is None and (cur["aa_sub_status"] == "pending" or kind == "confirm"):
            sub = "failed"  # a first quote Booking refused ("Ask AA"), or a confirm it refused (AA-952)
        if sub is not None:
            await ctx.conn.execute(SUB_SQL, line_id, sub, out.code)
        else:
            await ctx.conn.execute(
                "UPDATE crm.booking_line SET aa_result_code = $2 WHERE booking_line_id = $1", line_id, out.code
            )
        if sub == "expired" and cur["bk_hold_id"]:
            await ctx.conn.execute(COMMIT_SQL, trip_id, "expired", cur["bk_hold_id"])
        if kind == "confirm":
            for other in siblings:
                await ctx.conn.execute(SUB_SQL, other, sub, out.code)
                ctx.audit("aa_sync", "allowed", "booking_line", str(other), reason=f"confirm_{out.code}"[:40])
            await ctx.conn.execute(TASK_SQL, trip_id, proposal_lines.CONFIRM_FAILED_TASK)
    if out.hold is not None:
        line_sub = HOLD_STATUS[out.hold.status]
        orphan = kind == "hold" and (line_closed or status == "cancelled")
        if orphan:
            # The line was released (or its request cancelled) while the hold was on its way: the line stays closed,
            # only the hold id is kept so the release below can give the seats back. Booking holds them until then,
            # so the commitment says held (the release turns it to released).
            await ctx.conn.execute(ORPHAN_HOLD_SQL, line_id, out.hold.hold_id, out.hold.expires_at)
        elif kind == "hold":
            await ctx.conn.execute(HELD_SQL, line_id, out.hold.hold_id, out.hold.expires_at)
        elif line_sub != "confirmed" and not line_closed:
            await ctx.conn.execute(HOLD_STATE_SQL, line_id, line_sub, out.hold.expires_at)
        await ctx.conn.execute(COMMIT_SQL, trip_id, line_sub, out.hold.hold_id)
        if kind == "hold" and (orphan or cur["trip_stage"] in CLOSED_STAGES):
            # The line or the trip closed while the hold was on its way: give the seats back.
            await ctx.conn.fetchval(REFRESH_SQL, trip_id, line_id, "release")
    if out.booking is not None:
        b = out.booking
        args = (*BOOKED_ARGS["confirmed" if b.line_status == "confirmed" else "on_request"],
                "confirmed" if b.line_status == "confirmed" else "requested")  # fmt: skip
        await ctx.conn.execute(BOOKED_SQL, line_id, b.booking_id, b.trip_case_code, *args[:3])
        await ctx.conn.execute(COMMIT_SQL, trip_id, args[3], b.hold_id)
        for other in siblings:
            status = out.booking_lines.get(str(other))
            if status not in ("confirmed", "on_request"):
                continue  # not in the answer (or declined at once: the feed's line.declined closes it)
            booked = BOOKED_ARGS[status]
            await ctx.conn.execute(BOOKED_SQL, other, b.booking_id, b.trip_case_code, *booked)
            ctx.audit("aa_sync", "allowed", "booking_line", str(other), reason=f"confirm_{status}")
    if out.released:
        await ctx.conn.execute(CLOSE_LINE_SQL, line_id, "released", None, None)
        await ctx.conn.execute(CANCEL_OTHERS_SQL, line_id, request_id)
        for other in siblings:  # Booking let the whole set go (AA-952): every line of it is released
            await ctx.conn.execute(CLOSE_LINE_SQL, other, "released", None, None)
            await ctx.conn.execute(CANCEL_OTHERS_SQL, other, request_id)
            ctx.audit("aa_sync", "allowed", "booking_line", str(other), reason="release_released")
        if cur["bk_hold_id"]:
            await ctx.conn.execute(COMMIT_SQL, trip_id, "released", cur["bk_hold_id"])
    final = "failed" if out.failed else "done"
    if status == "pending":
        await ctx.conn.execute(FINISH_SQL, request_id, final, out.code)
    ctx.audit("aa_sync", "allowed", "booking_line", str(line_id), reason=f"{kind}_{out.code or final}"[:40])
    if cur["trip_deleted_at"] is None:
        await stages.refresh_waiting_on(ctx, trip_id)
    return final


# ── 3. the feed ──────────────────────────────────────────────────────────────────────────────────


async def apply_event(ctx: SystemContext, ev: contract.Event) -> tuple[str, str]:
    """``(outcome, reason)`` of one known event, its changes made in ``ctx``'s transaction."""
    if not ev.known:
        return "ignored", "unknown_type"
    if ev.type.startswith("departure."):
        return "ignored", "informational"
    try:
        line_ref = uuid.UUID(ev.line_ref) if ev.line_ref else None
    except ValueError:
        line_ref = None
    rows = await ctx.conn.fetch(EVENT_LINES_SQL, line_ref, ev.hold_id, ev.booking_id)
    if not rows:
        return "ignored", "unknown_line"
    trips = set()
    for row in rows:
        line_id, trip_id, hold_id = row["booking_line_id"], row["trip_id"], row["bk_hold_id"] or ev.hold_id
        trips.add(trip_id)
        if ev.type == "hold.expired":
            if row["status"] == "planning" and row["aa_sub_status"] == "held":
                await ctx.conn.execute(SUB_SQL, line_id, "expired", "hold_expired")
            if hold_id:
                await ctx.conn.execute(COMMIT_SQL, trip_id, "expired", hold_id)
        elif ev.type == "hold.extended":
            await ctx.conn.fetchval(REFRESH_SQL, trip_id, line_id, "refresh")
        elif ev.type == "line.confirmed":
            await ctx.conn.execute(CONFIRM_LINE_SQL, line_id)
            if hold_id:
                await ctx.conn.execute(COMMIT_SQL, trip_id, "confirmed", hold_id)
        elif ev.type == "line.declined":
            await ctx.conn.execute(CLOSE_LINE_SQL, line_id, "declined", ev.decline_code, "declined")
            if hold_id:
                await ctx.conn.execute(COMMIT_SQL, trip_id, "declined", hold_id)
            await ctx.conn.execute(TASK_SQL, trip_id, DECLINE_TASK)
        else:  # booking.cancelled
            await ctx.conn.execute(CLOSE_LINE_SQL, line_id, None, None, "booking_cancelled")
            if hold_id:
                await ctx.conn.execute(COMMIT_SQL, trip_id, "cancelled", hold_id)
        ctx.audit("aa_sync", "allowed", "booking_line", str(line_id), reason=ev.type.replace(".", "_"))
    for trip_id in trips:
        await stages.refresh_waiting_on(ctx, trip_id)
    return "applied", ev.type.replace(".", "_")


class _Replayed(Exception):
    pass


async def process_event(pool: asyncpg.Pool, tenant_id: uuid.UUID, item: Any, counts: Counter[str]) -> None:
    key = contract.event_id(item)
    if key is None:
        counts["unrecorded"] += 1  # nothing to key it on: logged, the cursor moves past it
        log.warning("crm_booking_event_unrecorded reason=no_event_id")
        return
    try:
        ev: contract.Event | None = contract.event(item)
    except contract.ContractError:
        ev = None
    try:
        async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
            if await ctx.conn.fetchval(SEEN_SQL, key) is not None:
                raise _Replayed
            if ev is None:
                outcome, reason, kind = "rejected", "contract", None
            else:
                outcome, reason = await apply_event(ctx, ev)
                kind = ev.type
            if await ctx.conn.fetchval(INBOX_SQL, key, kind, outcome, reason) is None:
                raise _Replayed
    except _Replayed:
        counts["replayed"] += 1
        return
    counts[f"events_{outcome}"] += 1


# ── the job ──────────────────────────────────────────────────────────────────────────────────────


@dataclass
class _Feed:
    cursor: str | None
    failures: int
    last_error_at: dt.datetime | None


@dataclass
class BookingSync:
    client: BookingClient | None
    now: Callable[[], dt.datetime] = field(default=lambda: dt.datetime.now(dt.UTC))
    _warned: bool = False

    async def __call__(self, pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        if self.client is None:
            if not self._warned:
                log.warning("crm_booking_sync not_configured (BOOKING_API_URL / M2M_CLIENT)")
                self._warned = True
            return {"status": "not_configured"}
        only = job.payload.get("tenant_id")
        tenants = [uuid.UUID(str(only))] if only else [a.tenant_id for a in await registry.agencies(pool)]
        counts: Counter[str] = Counter()
        for tenant_id in tenants:
            try:
                await self.run_agency(pool, tenant_id, counts, feed=not only)
            except (Forbidden, NotFound):
                counts["skipped"] += 1  # locked, offboarding or gone
            except Exception as exc:  # noqa: BLE001 — one agency's error must not stop every later agency
                counts["agency_failed"] += 1
                log.error("crm_booking_sync_failed tenant_id=%s error_class=%s", tenant_id, type(exc).__name__)
        return {"status": "ok", **{f"{k}_count": int(v) for k, v in sorted(counts.items())}}

    async def proposal_sets(self, pool: asyncpg.Pool, tenant_id: uuid.UUID, counts: Counter[str]) -> str | None:
        """AA-952: the AA lines of accepted proposals (Booking's hold read outside any transaction), then the confirms
        their paid deposits are due. Returns an outage class when Booking gave no answer (the run stops asking)."""
        async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
            due = await proposal_lines.due_sets(ctx)
        for row in due:
            held: contract.HeldSet | None
            try:
                answer = await self.client.get_hold(tenant_id, row["accepted_hold_id"])  # type: ignore[union-attr]
                held = contract.held_set(answer)
            except BookingRefusal as exc:
                if exc.code != "not_found":
                    counts["proposal_lines_refused"] += 1  # unknown_partner, forbidden: tried again next run
                    continue
                held = None  # Booking has no such hold: the lines read failed, the agent is told
            except BookingUnavailable as exc:
                counts["proposal_lines_retry"] += 1
                if exc.error_class in OUTAGE_CLASSES:
                    return exc.error_class
                continue
            except contract.ContractError:
                # Booking not on booking.b2b.v1 1.2 yet (no price_hash / fx / line prices): nothing is written.
                counts["proposal_lines_retry"] += 1
                log.warning("crm_proposal_lines_contract tenant_id=%s", tenant_id)
                continue
            async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                made = await proposal_lines.write_lines(ctx, row["proposal_id"], held)
            if made is not None:
                counts[f"proposal_lines_{made['outcome']}"] += 1
        async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
            counts["confirms_queued"] += len(await proposal_lines.queue_confirms(ctx))
        return None

    async def run_agency(self, pool: asyncpg.Pool, tenant_id: uuid.UUID, counts: Counter[str], *, feed: bool) -> None:
        sent = 0
        outage: str | None = await self.proposal_sets(pool, tenant_id, counts)
        while sent < MAX_REQUESTS:
            async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                claimed = [dict(r) for r in await ctx.conn.fetch(CLAIM_SQL, MAX_REQUESTS - sent, LEASE_S)]
                work = []
                for req in claimed:
                    line = await ctx.conn.fetchrow(LINE_SQL, req["booking_line_id"])
                    work.append(Work(req, dict(line)))
            if not work:
                break
            again = False
            for w in work:
                sent += 1
                if w.request["kind"] in DROP_ON_CLOSE and w.line["trip_stage"] in CLOSED_STAGES:
                    outcome = Outcome(code="trip_closed", cancelled=True)
                elif outage is not None:
                    outcome = Outcome(retry=outage)  # not sent: Booking just gave no answer to this run
                else:
                    outcome = await perform(self.client, tenant_id, w)  # type: ignore[arg-type]
                    if outcome.retry in OUTAGE_CLASSES:
                        outage = outcome.retry
                async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                    result = await apply(ctx, w, outcome)
                counts[f"requests_{result}"] += 1
                again = again or result == "again"
            if outage is not None or (not again and len(work) < MAX_REQUESTS):
                break
        if outage is not None:
            counts["outage"] += 1
            return  # the feed waits for the next run too
        if feed:
            await self.pull_feed(pool, tenant_id, counts)

    async def _feed_state(self, pool: asyncpg.Pool, tenant_id: uuid.UUID) -> _Feed | None:
        async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
            row = await ctx.conn.fetchrow(FEED_LOAD_SQL)
        return _Feed(row["feed_cursor"], row["failures"], row["last_error_at"]) if row is not None else None

    async def pull_feed(self, pool: asyncpg.Pool, tenant_id: uuid.UUID, counts: Counter[str]) -> None:
        state = await self._feed_state(pool, tenant_id)
        if state is None:
            return  # no AA line was ever asked for: nothing to pull
        if state.failures and state.last_error_at is not None:
            if self.now() < state.last_error_at + dt.timedelta(seconds=backoff_seconds(state.failures)):
                counts["feed_backoff"] += 1
                return
        cursor = state.cursor
        try:
            for _ in range(FEED_MAX_PAGES):
                data = await self.client.events(tenant_id, after=cursor, limit=FEED_PAGE)  # type: ignore[union-attr]
                items, next_cursor = contract.event_page(data)
                counts["feed_pages"] += 1
                for item in items:
                    await process_event(pool, tenant_id, item, counts)
                async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                    if next_cursor != cursor:
                        if await ctx.conn.fetchval(FEED_ADVANCE_SQL, cursor, next_cursor) is None:
                            return  # another run moved it first
                    else:
                        await ctx.conn.execute(FEED_OK_SQL)
                cursor = next_cursor
                if len(items) < FEED_PAGE:
                    return
        except (BookingUnavailable, BookingRefusal, contract.ContractError) as exc:
            error = getattr(exc, "error_class", None) or (
                f"booking_{exc.code}" if isinstance(exc, BookingRefusal) else "booking_contract"
            )
            async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                failures = int(await ctx.conn.fetchval(FEED_FAIL_SQL, error[:60]) or 0)
            counts["feed_failed"] += 1
            log.warning("crm_booking_feed_failed tenant_id=%s error=%s failures=%d", tenant_id, error, failures)
            if failures >= STUCK_FAILURES:
                log.error("crm_booking_feed_stuck tenant_id=%s error=%s failures=%d", tenant_id, error, failures)
