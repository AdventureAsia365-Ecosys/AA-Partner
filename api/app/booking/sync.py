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
urgent task for the trip owner; ``booking.cancelled`` -> cancelled; ``departure.incident`` /
``departure.incident_cleared`` -> ``crm.incident_flag`` (AA-961, :func:`apply_incident`); the other ``departure.*`` ->
recorded ``informational``; an unknown type -> ``unknown_type``. A feed failure backs off like a request.

AA-956: a confirm / release answer and the line events ``line.confirmed`` / ``line.declined`` / ``booking.cancelled``
queue one ``crm_ops_release_sync`` nudge for the trip (:func:`app.ops_release.service.nudge`, ids only), so the
Module 8 ops release is created or withdrawn within about a minute.
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

from app import cancellation, stages
from app.audit import MAX_OBJECT_IDS
from app.audit_export import registry
from app.booking import contract, proposal_lines, rehold
from app.booking.client import BookingClient, BookingRefusal, BookingUnavailable
from app.errors import Forbidden, NotFound
from app.jobs.queue import Job
from app.ops_release import service as ops_release
from app.payments import claims
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
# AA-956: the feed events that change which AA lines of a trip are confirmed (the ops release follows within a minute).
OPS_RELEASE_EVENTS = frozenset({"line.confirmed", "line.declined", "booking.cancelled"})
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
         WHERE m.booking_line_id = l.booking_line_id AND m.kind = 'sell' AND m.currency = l.aa_currency) AS sell_amount,
       (SELECT x.reason_code FROM crm.trip_cancellation x WHERE x.trip_id = l.trip_id) AS cancel_reason
  FROM crm.booking_line l JOIN crm.trip t ON t.trip_id = l.trip_id
 WHERE l.booking_line_id = $1
"""
# AA-957 (C12): the lines of one Booking booking in the trip (a cancel answer closes them all).
BOOKING_LINES_SQL = """
SELECT booking_line_id, bk_hold_id FROM crm.booking_line
 WHERE trip_id = $1 AND bk_booking_id = $2 AND supplier_kind = 'aa' AND status <> 'cancelled' AND deleted_at IS NULL
 ORDER BY booking_line_id
   FOR UPDATE
"""
FEE_SQL = "SELECT crm.booking_line_aa_cancel_fee($1, $2, $3, $4)"
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
# AA-961 (crm-027): an AA incident on a departure. One open flag per departure (a second incident while one is open
# keeps the first); a replayed event never opens a second (raised_event_id is unique).
INCIDENT_OPEN_SQL = """
INSERT INTO crm.incident_flag (departure_id, incident_code, raised_at, raised_event_id)
VALUES ($1, $2, COALESCE($3, now()), $4)
ON CONFLICT DO NOTHING
RETURNING incident_flag_id
"""
INCIDENT_CLEAR_SQL = """
UPDATE crm.incident_flag
   SET status = 'cleared', cleared_at = GREATEST(COALESCE($2, now()), raised_at), cleared_event_id = $3
 WHERE departure_id = $1 AND status = 'open' AND deleted_at IS NULL
RETURNING incident_flag_id
"""
# The agency's live trips with a line on the departure (an agency without one has nothing to show or do), and a
# trip the stage job completed today (its return day, the agency's timezone): the travellers may still be there.
INCIDENT_TRIPS_SQL = """
SELECT DISTINCT t.trip_id
  FROM crm.booking_line l JOIN crm.trip t ON t.trip_id = l.trip_id
 WHERE l.departure_id = $1 AND l.status <> 'cancelled' AND l.deleted_at IS NULL
   AND t.deleted_at IS NULL
   AND (t.stage IN ('booked', 'pre_departure', 'travelling')
        OR (t.stage = 'completed' AND t.end_date >= (now() AT TIME ZONE coalesce(
                (SELECT timezone FROM crm.tenant_settings LIMIT 1), 'Asia/Ho_Chi_Minh'))::date))
 ORDER BY t.trip_id
"""
INCIDENT_TASK = "aa_incident"
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
    cancellation: contract.Cancellation | None = None  # AA-957: Booking cancelled the booking (the fee)
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


async def _cancel(client: BookingClient, tenant_id: uuid.UUID, w: Work) -> Outcome:
    """AA-957 (C12): ``POST /bookings/{id}/cancel`` (not a dry run) with the trip's reason; ``Idempotency-Key
    crm-cancel:<request id>`` (Booking also answers a repeat with the stored cancellation)."""
    booking_id = w.line["bk_booking_id"]
    if not booking_id:
        return Outcome(code="not_found", failed=True)
    reason = cancellation.BOOKING_REASON.get(w.line.get("cancel_reason") or "", "other")
    try:
        data = await client.cancel_booking(
            tenant_id, booking_id, reason, dry_run=False, idempotency_key=f"crm-cancel:{w.request['aa_request_id']}"
        )
    except BookingRefusal as exc:
        return Outcome(code=exc.code, failed=True)
    result = contract.cancellation(data)
    if result.cancelled_at is None:
        raise contract.ContractError("cancelled_at is null on a cancel that is not a dry run")
    return Outcome(cancellation=result, code="cancelled")


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
        if kind == "cancel":
            return await _cancel(client, tenant_id, w)
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
    if kind == "cancel":
        return await _apply_cancel(ctx, w, out, status, cur)
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
            # Not `status`: that name holds this request's own status, read for the FINISH below.
            line_status = out.booking_lines.get(str(other))
            if line_status not in ("confirmed", "on_request"):
                continue  # not in the answer (or declined at once: the feed's line.declined closes it)
            booked = BOOKED_ARGS[line_status]
            await ctx.conn.execute(BOOKED_SQL, other, b.booking_id, b.trip_case_code, *booked)
            ctx.audit("aa_sync", "allowed", "booking_line", str(other), reason=f"confirm_{line_status}")
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
    if out.booking is not None or out.released:
        await ops_release.nudge(ctx, trip_id, f"aa_{kind}")  # AA-956: a confirmed line releases the trip to AA ops
    return final


async def _apply_cancel(ctx: SystemContext, w: Work, out: Outcome, status: str | None, cur: dict[str, Any]) -> str:
    """AA-957 (C12): Booking cancelled the booking (or says there is nothing left to cancel: ``not_found`` /
    ``invalid``): every line of the booking is cancelled, the fee becomes a ``cancellation_cost`` on this line, the
    commitments read cancelled, and a trip whose cancellation waited for it moves to ``cancelled``. Any other refusal
    fails the request and opens the owner task ``aa_cancel_failed`` (the trip stays "cancellation pending"; pressing
    Cancel again asks Booking again)."""
    request_id, line_id, trip_id = w.request["aa_request_id"], w.line_id, cur["trip_id"]
    booking_id = cur["bk_booking_id"]
    done = out.cancellation is not None or (out.failed and out.code in cancellation.CANCEL_DONE_CODES)
    if done:
        result = "cancelled_by_agency" if out.cancellation is not None else (out.code or "cancelled")
        for r in await ctx.conn.fetch(BOOKING_LINES_SQL, trip_id, booking_id) if booking_id else []:
            await ctx.conn.execute(CLOSE_LINE_SQL, r["booking_line_id"], None, None, result)
            if r["bk_hold_id"]:
                await ctx.conn.execute(COMMIT_SQL, trip_id, "cancelled", r["bk_hold_id"])
            ctx.audit("aa_sync", "allowed", "booking_line", str(r["booking_line_id"]), reason=f"cancel_{result}"[:40])
        c = out.cancellation
        if c is not None and c.fee > 0:
            await ctx.conn.fetchval(FEE_SQL, line_id, c.fee, c.currency, (c.cancelled_at or _utc_now()).date())
        final, code = "done", out.code
    else:
        final, code = "failed", out.code
        await ctx.conn.execute(TASK_SQL, trip_id, cancellation.CANCEL_FAILED_TASK)
    if status == "pending":
        await ctx.conn.execute(FINISH_SQL, request_id, final, code)
    ctx.audit("aa_sync", "allowed", "booking_line", str(line_id), reason=f"cancel_{code or final}"[:40])
    if done:
        await ops_release.nudge(ctx, trip_id, "aa_cancel")
        await cancellation.finish_if_done(ctx, trip_id)
    if cur["trip_deleted_at"] is None:
        await stages.refresh_waiting_on(ctx, trip_id)
    return final


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ── 3. the feed ──────────────────────────────────────────────────────────────────────────────────


async def apply_event(ctx: SystemContext, ev: contract.Event) -> tuple[str, str]:
    """``(outcome, reason)`` of one known event, its changes made in ``ctx``'s transaction."""
    if not ev.known:
        return "ignored", "unknown_type"
    if ev.type in ("departure.incident", "departure.incident_cleared"):
        return await apply_incident(ctx, ev)
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
            if ev.hold_id and row["bk_hold_id"] and row["bk_hold_id"] != ev.hold_id:
                # AA-952 slice 4: a late event of a hold the line left (a late deposit re-held it, app.booking.rehold):
                # the old hold's commitment reads expired, the line stays on its new hold.
                await ctx.conn.execute(COMMIT_SQL, trip_id, "expired", ev.hold_id)
                continue
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
        if ev.type == "booking.cancelled":
            await cancellation.finish_if_done(ctx, trip_id)  # AA-957: a pending trip cancel may be complete now
        await stages.refresh_waiting_on(ctx, trip_id)
        if ev.type in OPS_RELEASE_EVENTS:
            await ops_release.nudge(ctx, trip_id, ev.type.replace(".", "_"))
    return "applied", ev.type.replace(".", "_")


async def apply_incident(ctx: SystemContext, ev: contract.Event) -> tuple[str, str]:
    """``departure.incident`` opens a ``crm.incident_flag`` for the departure and one urgent ``aa_incident`` task per
    affected booked / pre-departure / travelling trip, or one completed today (its return day): the agent decides whom
    to reach, AA never messages travellers (design F9.3). ``departure.incident_cleared`` clears the open flag (the
    tasks stay for the agent to close). An event without a departure id or a known code is recorded as
    ``incident_incomplete``."""
    if ev.departure_id is None or (ev.type == "departure.incident" and ev.incident_code is None):
        return "ignored", "incident_incomplete"
    if ev.type == "departure.incident_cleared":
        cleared = await ctx.conn.fetchval(INCIDENT_CLEAR_SQL, ev.departure_id, ev.occurred_at, ev.event_id)
        if cleared is None:
            return "ignored", "incident_not_open"
        ctx.audit("aa_sync", "allowed", "incident_flag", str(cleared), reason="departure_incident_cleared")
        return "applied", "departure_incident_cleared"
    flag = await ctx.conn.fetchval(INCIDENT_OPEN_SQL, ev.departure_id, ev.incident_code, ev.occurred_at, ev.event_id)
    if flag is None:
        return "ignored", "incident_already_open"
    trips = [r["trip_id"] for r in await ctx.conn.fetch(INCIDENT_TRIPS_SQL, ev.departure_id)]
    for trip_id in trips:
        await ctx.conn.execute(TASK_SQL, trip_id, INCIDENT_TASK)
    ctx.audit(
        "aa_sync",
        "allowed",
        "incident_flag",
        str(flag),
        reason="departure_incident",
        object_ids=tuple(str(t) for t in trips[:MAX_OBJECT_IDS]),
    )
    return "applied", "departure_incident"


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
            try:
                async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                    made = await proposal_lines.write_lines(ctx, row["proposal_id"], held)
            except (Forbidden, NotFound):
                raise  # the agency is locked or gone: the whole run skips it
            except Exception as exc:  # noqa: BLE001 — one proposal (e.g. no FX rate yet) must not block the agency
                counts["proposal_lines_error"] += 1
                log.error("crm_proposal_lines_failed tenant_id=%s error_class=%s", tenant_id, type(exc).__name__)
                continue
            if made is not None:
                counts[f"proposal_lines_{made['outcome']}"] += 1
        try:
            async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                counts["confirms_queued"] += len(await proposal_lines.queue_confirms(ctx))
        except (Forbidden, NotFound):
            raise
        except Exception as exc:  # noqa: BLE001 — the hand-asked requests and the feed still run
            counts["confirms_error"] += 1
            log.error("crm_proposal_confirms_failed tenant_id=%s error_class=%s", tenant_id, type(exc).__name__)
        return None

    async def invoice_holds(self, pool: asyncpg.Pool, tenant_id: uuid.UUID, counts: Counter[str]) -> str | None:
        """AA-952 slice 4 (:mod:`app.booking.rehold`, :mod:`app.payments.claims`): the claimed invoices whose one
        hold extension got no answer, then the late deposits that wait for their AA hold to be placed again. Returns
        an outage class when Booking gave no answer (the run stops asking)."""
        client = self.client
        assert client is not None  # noqa: S101 — __call__ returns early without a client
        async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
            extends = await claims.extend_due(ctx)
            invoices = await rehold.due(ctx, self.now())
        for invoice_id, hold_id in extends:
            ext = await claims.extend(client, tenant_id, invoice_id, hold_id)
            if ext.code == claims.UNAVAILABLE:
                counts["extend_retry"] += 1
                return "booking_unavailable"
            async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                await claims.finish_claim(ctx, invoice_id, hold_id, ext)
            counts[f"extend_{ext.code}"] += 1
        for invoice_id in invoices:
            try:
                async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                    p = await rehold.plan(ctx, invoice_id)
                if p is None:
                    counts["rehold_waiting"] += 1
                    continue

                async def remember(quote_id: str, price_hash: str, invoice_id: uuid.UUID = invoice_id) -> bool:
                    async with system_tx(pool, tenant_id, actor=ACTOR) as rctx:
                        return await rehold.remember(rctx, invoice_id, quote_id, price_hash)

                asked = await rehold.ask(client, p, remember, self.now())
                async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                    result = await rehold.apply(ctx, invoice_id, asked)
            except (Forbidden, NotFound):
                raise
            except Exception as exc:  # noqa: BLE001 — one invoice must not block the agency's other work
                counts["rehold_error"] += 1
                log.error("crm_rehold_failed tenant_id=%s error_class=%s", tenant_id, type(exc).__name__)
                continue
            counts[f"rehold_{result or asked.outcome}"] += 1
            if asked.outcome == "retry" and asked.code == "booking_unavailable":
                return "booking_unavailable"
        return None

    async def run_agency(self, pool: asyncpg.Pool, tenant_id: uuid.UUID, counts: Counter[str], *, feed: bool) -> None:
        sent = 0
        outage: str | None = await self.proposal_sets(pool, tenant_id, counts)
        if outage is None:
            outage = await self.invoice_holds(pool, tenant_id, counts)
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
