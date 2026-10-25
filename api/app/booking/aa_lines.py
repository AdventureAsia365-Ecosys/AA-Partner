"""What a member may ask Booking for, for a trip's Adventure Asia lines (AA-947, card CRM-19).

Every ask is a row of the outage queue ``crm.aa_request`` (crm-017); the Booking call itself is made by the job
``crm_booking_sync`` (:mod:`app.booking.sync`), which this module nudges right away so a healthy Booking answers in
seconds, and which retries without duplicates while Booking is down (card E: the line reads "AA pending"). The line
stays read-only for members (card R): they never type its title, price or status.

- :func:`request_line`: a new AA line from a quote: ``crm.aa_line_request()`` (crm-017 definer) creates the pending
  line (title from the catalog cache) and the quote request in one call.
- :func:`ask`: hold, confirm (trip booked or later: after the deposit), release, re-quote of one AA line; one live
  request per (line, kind) (409 ``aa_request_pending``). Confirm books with AA, so it needs ``aa_confirm`` (owner and
  agent; the assistant gets 403 ``role_forbidden``).
- :func:`close_trip`: Lost or a cancelled trip releases its AA holds (queued after the stage Undo window; Undo cancels
  them, :func:`undo_close`) and holds the requests still waiting back by the same window (an Undo leaves them to run;
  otherwise the sync drops them).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app.booking import refusal, views
from app.errors import Conflict, Invalid, NotFound
from app.jobs import queue
from app.jobs.kinds import BOOKING_SYNC
from app.ledger import lines as ledger_lines
from app.permissions import Action, require
from app.tenancy import AnyContext, TenantContext

ACTIONS = ("hold", "confirm", "release", "requote")
COMPONENT_KINDS = ("trip_plan", "hotel_room", "transfer", "guide", "activity", "entrance", "meal")
OCCUPANCIES = ("single", "double", "twin", "triple", "family")
_SOURCE_ID = re.compile(r"^[^\x00-\x1f]{1,128}$")
_ID = re.compile(r"^[A-Za-z0-9:_.-]{1,64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
MAX_AMOUNT = Decimal("999999999999.99")
# A release queued by a stage move waits the Undo window (app.stages.UNDO_SECONDS = 10) plus a margin.
RELEASE_DELAY_S = 15
NUDGE_ATTEMPTS = 3

REQUEST_SQL = "SELECT crm.aa_line_request($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13)"
LINE_SQL = """
SELECT l.booking_line_id, l.trip_id, l.supplier_kind, l.status, l.aa_sub_status, l.bk_hold_id, l.bk_booking_id,
       l.bk_quote_id, t.stage AS trip_stage,
       EXISTS (SELECT 1 FROM crm.aa_request r WHERE r.booking_line_id = l.booking_line_id AND r.status = 'pending'
                  AND r.deleted_at IS NULL) AS aa_pending
  FROM crm.booking_line l JOIN crm.trip t ON t.trip_id = l.trip_id AND t.deleted_at IS NULL
 WHERE l.booking_line_id = $1 AND l.deleted_at IS NULL
"""
ASK_SQL = """
INSERT INTO crm.aa_request (trip_id, booking_line_id, kind, sell_amount, next_attempt_at)
VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5))
ON CONFLICT (tenant_id, booking_line_id, kind) WHERE status = 'pending' AND deleted_at IS NULL DO NOTHING
RETURNING aa_request_id
"""
# Lost / cancel: the requests that would act on a closed trip wait out the Undo window like the release (not the ones
# a run holds right now: their answer is applied, and a hold that lands on a closed trip is released by the sync).
# They are not cancelled here: an Undo leaves them pending and they run as asked; once the window is over the sync
# drops the ones whose trip is still closed (``trip_closed``, Booking not called: app.booking.sync).
CLOSE_DEFER_SQL = """
UPDATE crm.aa_request SET next_attempt_at = greatest(next_attempt_at, now() + make_interval(secs => $2))
 WHERE trip_id = $1 AND status = 'pending' AND deleted_at IS NULL AND kind IN ('quote', 'requote', 'hold', 'refresh')
   AND (lease_until IS NULL OR lease_until < now())
"""
CLOSE_LINES_SQL = """
SELECT l.booking_line_id FROM crm.booking_line l
 WHERE l.trip_id = $1 AND l.supplier_kind = 'aa' AND l.status = 'planning' AND l.bk_booking_id IS NULL
   AND l.deleted_at IS NULL
"""
CLOSE_COUNT_SQL = """
SELECT count(*) FILTER (WHERE l.aa_sub_status = 'held') AS held,
       count(*) FILTER (WHERE l.bk_booking_id IS NOT NULL AND l.status <> 'cancelled') AS booked
  FROM crm.booking_line l
 WHERE l.trip_id = $1 AND l.supplier_kind = 'aa' AND l.deleted_at IS NULL
"""
UNDO_CLOSE_SQL = """
UPDATE crm.aa_request SET status = 'cancelled', finished_at = now(), result_code = 'undone'
 WHERE trip_id = $1 AND kind = 'release' AND status = 'pending' AND created_at >= $2
   AND (lease_until IS NULL OR lease_until < now())
"""


# ── validation ───────────────────────────────────────────────────────────────────────────────────


def _int(value: Any, field: str, low: int, high: int, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Invalid(f"invalid_{field}", f"a whole number from {low} to {high}")
    return value


def _amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Invalid("invalid_sell_amount", "a number from 0, two decimals") from None
    if not number.is_finite() or number < 0 or number > MAX_AMOUNT:
        raise Invalid("invalid_sell_amount", "a number from 0, two decimals")
    return number.quantize(Decimal("0.01"))


def _rooms(value: Any) -> list[dict[str, Any]] | None:
    if value is None or value == []:
        return None
    if not isinstance(value, list) or len(value) > 30:
        raise Invalid("invalid_rooms", "a list of {occupancy, count}")
    out = []
    for room in value:
        if not isinstance(room, dict) or room.get("occupancy") not in OCCUPANCIES:
            raise Invalid("invalid_rooms", "occupancy single, double, twin, triple or family")
        out.append({"occupancy": room["occupancy"], "count": _int(room.get("count"), "rooms", 1, 30, default=1)})
    return out


def parse_request(body: dict[str, Any]) -> dict[str, Any]:
    """The checked parameters of a new AA line (counts and ids only)."""
    source_id = body.get("tour_source_id")
    if not isinstance(source_id, str) or not _SOURCE_ID.match(source_id):
        raise Invalid("invalid_tour_source_id", "an AA tour of the catalog")
    component_id = body.get("component_id")
    if not isinstance(component_id, str) or not _ID.match(component_id):
        raise Invalid("invalid_component_id", "a component of the AA product")
    kind = body.get("component_kind")
    if kind not in COMPONENT_KINDS:
        raise Invalid("invalid_component_kind", ", ".join(COMPONENT_KINDS))
    departure = body.get("departure_id")
    if departure is not None and (not isinstance(departure, str) or not _ID.match(departure)):
        raise Invalid("invalid_departure_id", "a departure of the AA product")
    try:
        service_date = (
            body["service_date"]
            if isinstance(body.get("service_date"), dt.date)
            else dt.date.fromisoformat(str(body.get("service_date")))
        )
    except ValueError:
        raise Invalid("invalid_service_date", "a date, YYYY-MM-DD") from None
    pax = body.get("pax") if isinstance(body.get("pax"), dict) else {}
    currency = body.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.match(currency):
        raise Invalid("invalid_currency", "an ISO currency, e.g. USD or VND")
    nights = _int(body.get("nights"), "nights", 1, 60)
    if kind == "hotel_room" and nights is None:
        raise Invalid("invalid_nights", "a hotel room needs its nights")
    return {
        "tour_source_id": source_id,
        "component_id": component_id,
        "component_kind": kind,
        "departure_id": departure,
        "service_date": service_date,
        "nights": nights,
        "adult": _int(pax.get("adult"), "pax", 1, 60),
        "child": _int(pax.get("child"), "pax", 0, 60, default=0),
        "infant": _int(pax.get("infant"), "pax", 0, 20, default=0),
        "rooms": _rooms(body.get("rooms")),
        "currency": currency,
        "sell_amount": _amount(body.get("sell_amount")),
    }


def _db(exc: asyncpg.PostgresError) -> Exception:
    mapped = refusal(exc)
    return mapped if mapped is not None else exc


async def nudge(ctx: AnyContext, request_id: uuid.UUID, *, delay_s: float = 0) -> None:
    """Queue a one-agency ``crm_booking_sync`` run for this request (the minute run would send it anyway)."""
    await queue.enqueue(
        ctx.conn,
        BOOKING_SYNC,
        {"tenant_id": str(ctx.tenant_id), "aa_request_id": str(request_id)},
        idempotency_key=f"{BOOKING_SYNC}:nudge:{request_id}",
        delay_seconds=delay_s,
        max_attempts=NUDGE_ATTEMPTS,
        created_by=getattr(ctx, "actor", None) or f"member:{getattr(ctx, 'member_id', '')}",
    )


# ── member asks ──────────────────────────────────────────────────────────────────────────────────


async def request_line(ctx: TenantContext, trip_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
    p = parse_request(body)
    if p["adult"] is None:
        raise Invalid("invalid_pax", "at least one adult")
    try:
        async with ctx.conn.transaction():
            line_id = await ctx.conn.fetchval(
                REQUEST_SQL,
                trip_id,
                p["tour_source_id"],
                p["component_id"],
                p["component_kind"],
                p["departure_id"],
                p["service_date"],
                p["nights"],
                p["adult"],
                p["child"],
                p["infant"],
                json.dumps(p["rooms"]) if p["rooms"] is not None else None,
                p["currency"],
                p["sell_amount"],
            )
    except asyncpg.PostgresError as exc:
        if getattr(exc, "sqlstate", None) == "CRM13":
            raise NotFound("trip_not_found") from None
        raise _db(exc) from None
    request_id = await ctx.conn.fetchval(
        "SELECT aa_request_id FROM crm.aa_request WHERE booking_line_id = $1 AND kind = 'quote'", line_id
    )
    await nudge(ctx, request_id)
    ctx.audit("aa_request", "allowed", "booking_line", str(line_id), reason="quote")
    return await ledger_lines.line(ctx, line_id)


async def ask(ctx: TenantContext, line_id: uuid.UUID, action: str, body: dict[str, Any] | None = None) -> dict:
    """One of :data:`ACTIONS` for an AA line; the rules of :func:`app.booking.views.actions`, worded as 409s."""
    if action not in ACTIONS:
        raise Invalid("invalid_action", ", ".join(ACTIONS))
    if action == "confirm":  # a confirm creates a real AA booking: owner and agent only (403 for the assistant)
        require(ctx, Action.AA_CONFIRM, object_kind="booking_line", object_id=str(line_id))
    row = await ctx.conn.fetchrow(LINE_SQL, line_id)
    if row is None:
        raise NotFound("line_not_found")
    if row["supplier_kind"] != "aa":
        raise Conflict("not_an_aa_line", "this is one of your own supplier lines")
    if row["aa_pending"]:
        raise Conflict("aa_request_pending", "AA has not answered the last request for this line yet")
    if action not in views.actions({**dict(row), "aa_pending": None}, row["trip_stage"]):
        if action == "confirm" and row["aa_sub_status"] == "held":
            raise Conflict("deposit_required", "confirm with AA once the trip is booked (deposit received)")
        if action == "release" and row["bk_booking_id"] is not None:
            raise Conflict("use_cancel_flow", "AA booked this line; cancel the trip to cancel it")
        raise Conflict("aa_action_not_allowed", f"{action} is not possible for this line now",
                       sub_status=row["aa_sub_status"])  # fmt: skip
    sell = _amount((body or {}).get("sell_amount")) if action == "requote" else None
    try:
        async with ctx.conn.transaction():
            request_id = await ctx.conn.fetchval(ASK_SQL, row["trip_id"], line_id, action, sell, 0)
    except asyncpg.PostgresError as exc:
        raise _db(exc) from None
    if request_id is None:
        raise Conflict("aa_request_pending", "AA has not answered the last request for this line yet")
    await nudge(ctx, request_id)
    ctx.audit("aa_request", "allowed", "booking_line", str(line_id), reason=action)
    return await ledger_lines.line(ctx, line_id)


# ── Lost / cancel ────────────────────────────────────────────────────────────────────────────────


async def close_effects(ctx: AnyContext, trip_id: uuid.UUID) -> dict[str, int]:
    """What closing the trip does to its AA lines (the cancel dry run lists it): holds released; lines AA already
    booked are left to the cancellation card (CRM-27: Booking's fee)."""
    row = await ctx.conn.fetchrow(CLOSE_COUNT_SQL, trip_id)
    return {"held": int(row["held"] or 0), "booked": int(row["booked"] or 0)}


async def close_trip(ctx: AnyContext, trip_id: uuid.UUID) -> int:
    """Lost or cancelled: hold the waiting requests back and queue one release per line Booking has not booked, all
    sent after :data:`RELEASE_DELAY_S` (so a stage Undo cancels the releases first and the other requests then run as
    asked; without an Undo the sync drops them, ``trip_closed``). Returns the releases queued."""
    await ctx.conn.execute(CLOSE_DEFER_SQL, trip_id, RELEASE_DELAY_S)
    queued = 0
    for row in await ctx.conn.fetch(CLOSE_LINES_SQL, trip_id):
        request_id = await ctx.conn.fetchval(ASK_SQL, trip_id, row["booking_line_id"], "release", None, RELEASE_DELAY_S)
        if request_id is not None:
            queued += 1
            await nudge(ctx, request_id, delay_s=RELEASE_DELAY_S + 1)
    return queued


async def undo_close(ctx: AnyContext, trip_id: uuid.UUID, since: dt.datetime) -> int:
    """A stage Undo of a Lost move: the releases that move queued are cancelled (not yet sent)."""
    status = await ctx.conn.execute(UNDO_CLOSE_SQL, trip_id, since)
    return int(status.split()[-1]) if status and status.split()[-1].isdigit() else 0
