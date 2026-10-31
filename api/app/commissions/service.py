"""Commission tracking, the agency side (AA-957, card CRM-27; design v3 M13, F10.2, R17, R21, A-12 Commissions, the
trip's Bookings tab). Routes in :mod:`app.routers.commissions`; rows and receipts in crm-035.

- **Rows** come from ``crm.commission_sync(trip)`` (SECURITY DEFINER): one expected row per confirmed agency line with
  a positive expected commission, cancelled when the line is cancelled or its commission set to 0, mirrored into
  ``crm.trip_commitment`` (the delete block). :func:`sync` runs after every confirm, money change, line cancel, trip
  cancel, and from the hourly date rule (:func:`sweep`).
- **Expected** = the head of the line's ``commission_expected`` chain (frozen at the confirmation day, R17); never
  copied until the receipt.
- **Overdue** = expected / claimed and due before today in the agency timezone; derived on every read. The date rule
  opens one owner task ``commission_overdue`` per trip, once per row (:func:`alert_overdue`).
- **Claim** (expected -> claimed, the agency's reference), **withdraw claim** (claimed -> expected), **receive**
  (``crm.commission_receive``: the receipt day's rate, ``short_amount`` in the original currency only,
  ``fx_difference_base``), **write off** (a reason code and a note), **due date**.

Margin field (§4.6): the routes are owner / agent only (``VIEW_MARGIN`` / ``PAYMENT_MARK_RECEIVED``), the database
returns no row to the assistant (crm-035 ``commission_margin_roles``), and every key here names a commission, so the
serializer would strip it anywhere else. Every function takes the open context and never opens one.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound
from app.ledger import fx
from app.tenancy import AnyContext, TenantContext

STATUSES = ("expected", "claimed", "received", "written_off", "cancelled")
FILTERS = ("open", "overdue", "received", "written_off", "cancelled", "all")
WRITE_OFF_REASONS = ("supplier_refused", "too_small", "other")
OVERDUE_TASK = "commission_overdue"
MAX_LIST = 500
MAX_SWEEP = 500
MAX_AMOUNT = Decimal("999999999999.99")
REF = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")
CENT = Decimal("0.01")

SYNC_SQL = "SELECT crm.commission_sync($1)"
RECEIVE_SQL = "SELECT crm.commission_receive($1, $2, $3, $4, $5)"
_COLUMNS = """
SELECT c.commission_id, c.trip_id, c.booking_line_id, c.status, c.due_on, c.claimed_at, c.claim_ref,
       c.received_amount, c.received_currency, c.received_on, c.received_ref, c.base_currency,
       c.received_amount_base, c.fx_rate, c.fx_rate_date, c.fx_source, c.stale_days, c.short_amount,
       c.fx_difference_base, c.close_reason, c.note, c.keep_on_cancel, c.decided_by, c.created_at, c.updated_at,
       l.title AS line_title, l.status AS line_status, l.service_from, l.service_to, s.agency_supplier_id,
       s.name AS supplier_name, t.title AS trip_title, t.stage AS trip_stage,
       m.amount AS expected_amount, m.currency AS expected_currency, m.amount_base AS expected_amount_base,
       m.base_currency AS expected_base_currency
  FROM crm.commission c
  JOIN crm.booking_line l ON l.booking_line_id = c.booking_line_id
  JOIN crm.agency_supplier s ON s.agency_supplier_id = l.agency_supplier_id
  JOIN crm.trip t ON t.trip_id = c.trip_id AND t.deleted_at IS NULL
  LEFT JOIN crm.v_booking_line_money_current m ON m.booking_line_id = c.booking_line_id
       AND m.kind = 'commission_expected'
"""
ONE_SQL = _COLUMNS + " WHERE c.commission_id = $1\n"
LOCK_SQL = "SELECT commission_id, trip_id, status FROM crm.commission WHERE commission_id = $1 FOR UPDATE"
LIST_SQL = (
    _COLUMNS
    + """ WHERE ($1::uuid IS NULL OR c.trip_id = $1)
   AND ($2::text[] IS NULL OR c.status = ANY($2::text[]))
   AND (NOT $3::boolean OR (c.status IN ('expected', 'claimed') AND c.due_on < $4))
 ORDER BY c.due_on, c.created_at, c.commission_id
 LIMIT $5
"""
)
# Totals in today's base currency: open expected (frozen at confirmation), overdue, received this far (receipt day).
TOTALS_SQL = """
SELECT
  coalesce(sum(m.amount_base) FILTER (WHERE c.status IN ('expected', 'claimed') AND m.base_currency = $1), 0)
      AS open_base,
  coalesce(sum(m.amount_base) FILTER (WHERE c.status IN ('expected', 'claimed') AND c.due_on < $2
                                       AND m.base_currency = $1), 0) AS overdue_base,
  count(*) FILTER (WHERE c.status IN ('expected', 'claimed') AND c.due_on < $2) AS overdue_count,
  count(*) FILTER (WHERE c.status IN ('expected', 'claimed')) AS open_count,
  coalesce(sum(c.received_amount_base) FILTER (WHERE c.status = 'received' AND c.base_currency = $1), 0)
      AS received_base,
  coalesce(sum(m.amount_base) FILTER (WHERE c.status = 'received' AND m.base_currency = $1), 0)
      AS received_expected_base,
  count(*) FILTER (WHERE c.status IN ('expected', 'claimed') AND m.base_currency IS DISTINCT FROM $1)
      AS open_other_base
  FROM crm.commission c
  JOIN crm.trip t ON t.trip_id = c.trip_id AND t.deleted_at IS NULL
  LEFT JOIN crm.v_booking_line_money_current m ON m.booking_line_id = c.booking_line_id
       AND m.kind = 'commission_expected'
"""
CLAIM_SQL = """
UPDATE crm.commission SET status = 'claimed', claimed_at = now(), claim_ref = $2, decided_by = $3
 WHERE commission_id = $1
"""
UNCLAIM_SQL = "UPDATE crm.commission SET status = 'expected', decided_by = $2 WHERE commission_id = $1"
WRITE_OFF_SQL = """
UPDATE crm.commission SET status = 'written_off', close_reason = $2, note = $3, decided_by = $4 WHERE commission_id = $1
"""
DUE_SQL = "UPDATE crm.commission SET due_on = $2, overdue_alerted_at = NULL WHERE commission_id = $1"
# The date rule: every trip with a confirmed agency line whose commission row is missing or out of date (a positive
# expected commission without a live row; an expected row whose line is gone or whose commission is now 0).
SWEEP_SQL = """
SELECT DISTINCT l.trip_id
  FROM crm.booking_line l
  JOIN crm.trip t ON t.trip_id = l.trip_id AND t.deleted_at IS NULL
 WHERE l.supplier_kind = 'agency'
   AND ((l.status = 'confirmed' AND l.deleted_at IS NULL AND t.stage NOT IN ('lost', 'cancelled')
         AND EXISTS (SELECT 1 FROM crm.v_booking_line_money_current m
                      WHERE m.booking_line_id = l.booking_line_id AND m.kind = 'commission_expected' AND m.amount > 0)
         AND NOT EXISTS (SELECT 1 FROM crm.commission c
                          WHERE c.booking_line_id = l.booking_line_id AND c.status <> 'cancelled'))
        OR EXISTS (SELECT 1 FROM crm.commission c
                    WHERE c.booking_line_id = l.booking_line_id AND c.status = 'expected' AND NOT c.keep_on_cancel
                      AND (l.status = 'cancelled' OR l.deleted_at IS NOT NULL
                           OR NOT EXISTS (SELECT 1 FROM crm.v_booking_line_money_current m
                                           WHERE m.booking_line_id = l.booking_line_id
                                             AND m.kind = 'commission_expected' AND m.amount > 0))))
 LIMIT $1
"""
OVERDUE_SQL = """
SELECT c.commission_id, c.trip_id FROM crm.commission c
  JOIN crm.trip t ON t.trip_id = c.trip_id AND t.deleted_at IS NULL
 WHERE c.status IN ('expected', 'claimed') AND c.due_on < $1 AND c.overdue_alerted_at IS NULL
 ORDER BY c.due_on, c.commission_id
 LIMIT $2
   FOR UPDATE OF c SKIP LOCKED
"""
ALERTED_SQL = "UPDATE crm.commission SET overdue_alerted_at = now() WHERE commission_id = ANY($1::uuid[])"
OWNER_SQL = """
SELECT member_id FROM crm.member WHERE role = 'owner' AND status = 'active' ORDER BY created_at, member_id LIMIT 1
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
VALUES ($1, $2, 'normal', $3, 'created', now() + interval '1 day')
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""
# The supplier task of a cancel (app.cancellation): done once no confirmed agency line is left on the trip.
CLOSE_SUPPLIER_TASKS_SQL = """
UPDATE crm.task k SET status = 'done', done_at = now(), done_by = $2
 WHERE k.kind = $1 AND k.status = 'open' AND k.deleted_at IS NULL AND k.trip_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM crm.booking_line l
                    WHERE l.trip_id = k.trip_id AND l.supplier_kind = 'agency' AND l.status = 'confirmed'
                      AND l.deleted_at IS NULL)
RETURNING k.task_id
"""


def _money(value: Any) -> str | None:
    return None if value is None else str(Decimal(value).quantize(CENT))


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def commission_view(r: Any, today: dt.date) -> dict[str, Any]:
    open_ = r["status"] in ("expected", "claimed")
    out: dict[str, Any] = {
        "commission_id": str(r["commission_id"]),
        "trip_id": str(r["trip_id"]),
        "trip_title": r["trip_title"],
        "trip_stage": r["trip_stage"],
        "booking_line_id": str(r["booking_line_id"]),
        "line_title": r["line_title"],
        "line_status": r["line_status"],
        "service_from": _iso(r["service_from"]),
        "service_to": _iso(r["service_to"]),
        "agency_supplier_id": str(r["agency_supplier_id"]),
        "supplier_name": r["supplier_name"],
        "status": r["status"],
        "due_on": _iso(r["due_on"]),
        "overdue": open_ and r["due_on"] < today,
        "days_overdue": (today - r["due_on"]).days if open_ and r["due_on"] < today else 0,
        "claimed_at": _iso(r["claimed_at"]),
        "claim_ref": r["claim_ref"],
        "close_reason": r["close_reason"],
        "note": r["note"],
        "keep_on_cancel": r["keep_on_cancel"],
        "commission_expected": None
        if r["expected_amount"] is None
        else {
            "amount": _money(r["expected_amount"]),
            "currency": r["expected_currency"],
            "amount_base": _money(r["expected_amount_base"]),
            "base_currency": r["expected_base_currency"],
        },
        "commission_received": None
        if r["received_amount"] is None
        else {
            "amount": _money(r["received_amount"]),
            "currency": r["received_currency"],
            "received_on": _iso(r["received_on"]),
            "reference": r["received_ref"],
            "amount_base": _money(r["received_amount_base"]),
            "base_currency": r["base_currency"],
            "fx": {
                "rate": str(r["fx_rate"]),
                "rate_date": _iso(r["fx_rate_date"]),
                "source": r["fx_source"],
                "basis": "receipt_date",
                "stale_days": r["stale_days"],
            },
            "short_amount": _money(r["short_amount"]),
            "fx_difference_base": _money(r["fx_difference_base"]),
        },
        "updated_at": _iso(r["updated_at"]),
    }
    if r["fx_source"] == "platform_daily" and r["fx_rate"] is not None and r["fx_rate"] != 1:
        out["commission_received"]["fx"]["attribution"] = fx.ATTRIBUTION
    return out


async def sync(ctx: AnyContext, trip_id: uuid.UUID) -> dict[str, int]:
    """``crm.commission_sync`` for one trip (counts only; safe for every role and actor)."""
    raw = await ctx.conn.fetchval(SYNC_SQL, trip_id)
    return json.loads(raw) if isinstance(raw, str) else dict(raw or {})


async def _one(ctx: AnyContext, commission_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ONE_SQL, commission_id)
    if row is None:
        raise NotFound("commission_not_found")
    return commission_view(row, await fx.agency_today(ctx))


async def get(ctx: TenantContext, commission_id: uuid.UUID) -> dict[str, Any]:
    return await _one(ctx, commission_id)


async def listing(ctx: TenantContext, *, status: str | None, trip_id: uuid.UUID | None) -> dict[str, Any]:
    """A-12 (and the trip's Bookings tab with ``trip_id``): commissions by due date; ``status`` open (default:
    expected + claimed), overdue, received, written_off, cancelled or all. Totals in the base currency."""
    flt = status or "open"
    if flt not in FILTERS:
        raise Invalid("invalid_status", ", ".join(FILTERS))
    statuses = {
        "open": ["expected", "claimed"],
        "overdue": ["expected", "claimed"],
        "received": ["received"],
        "written_off": ["written_off"],
        "cancelled": ["cancelled"],
        "all": None,
    }[flt]
    today = await fx.agency_today(ctx)
    rows = await ctx.conn.fetch(LIST_SQL, trip_id, statuses, flt == "overdue", today, MAX_LIST)
    settings = await fx.settings(ctx)
    base = settings["base_currency"]
    t = await ctx.conn.fetchrow(TOTALS_SQL, base, today)
    ctx.audit("list_view", "allowed", "commission", None,
              object_ids=tuple(str(r["commission_id"]) for r in rows[:20]))  # fmt: skip
    return {
        "status": flt,
        "today": today.isoformat(),
        "base_currency": base,
        "commissions": [commission_view(r, today) for r in rows],
        "commission_totals": {
            "open_count": int(t["open_count"]),
            "overdue_count": int(t["overdue_count"]),
            "open_base": _money(t["open_base"]),
            "overdue_base": _money(t["overdue_base"]),
            "received_base": _money(t["received_base"]),
            "received_expected_base": _money(t["received_expected_base"]),
            "open_other_base": int(t["open_other_base"]),
        },
    }


def _ref(value: Any, field: str, *, required: bool) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise Invalid(f"invalid_{field}", "1 to 80 characters on one line")
        return None
    if not isinstance(value, str) or not REF.match(value.strip()):
        raise Invalid(f"invalid_{field}", "1 to 80 characters on one line")
    return value.strip()


async def _lock(ctx: TenantContext, commission_id: uuid.UUID, allowed: tuple[str, ...]) -> Any:
    row = await ctx.conn.fetchrow(LOCK_SQL, commission_id)
    if row is None:
        raise NotFound("commission_not_found")
    if row["status"] not in allowed:
        closed = row["status"] in ("received", "written_off", "cancelled")
        code = "commission_closed" if closed else "commission_not_claimed"
        raise Conflict(code, f"a {row['status']} commission", status=row["status"])
    return row


async def _after(ctx: TenantContext, row: Any, action: str, reason: str | None = None) -> dict[str, Any]:
    await sync(ctx, row["trip_id"])
    ctx.audit(action, "allowed", "commission", str(row["commission_id"]), reason=reason)
    return await _one(ctx, row["commission_id"])


async def claim(ctx: TenantContext, commission_id: uuid.UUID, reference: Any) -> dict[str, Any]:
    row = await _lock(ctx, commission_id, ("expected",))
    await ctx.conn.execute(CLAIM_SQL, commission_id, _ref(reference, "reference", required=False),
                           f"member:{ctx.member_id}")  # fmt: skip
    return await _after(ctx, row, "commission_claim")


async def unclaim(ctx: TenantContext, commission_id: uuid.UUID) -> dict[str, Any]:
    row = await _lock(ctx, commission_id, ("claimed",))
    await ctx.conn.execute(UNCLAIM_SQL, commission_id, f"member:{ctx.member_id}")
    return await _after(ctx, row, "commission_unclaim")


def _amount(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Invalid("invalid_amount", "a positive amount, at most two decimals") from None
    if not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT or amount != amount.quantize(CENT):
        raise Invalid("invalid_amount", "a positive amount, at most two decimals")
    return amount


def _day(value: Any, field: str) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise Invalid(f"invalid_{field}", "a date (YYYY-MM-DD)") from None


async def receive(ctx: TenantContext, commission_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
    """The supplier paid: the amount, its currency, the day it arrived (today or earlier; frozen at that day's rate,
    R17 E) and the agency's own reference (never card or account numbers)."""
    row = await _lock(ctx, commission_id, ("expected", "claimed"))
    amount = _amount(body.get("amount"))
    currency = fx.currency(body.get("currency"))
    today = await fx.agency_today(ctx)
    day = _day(body.get("received_on") or today.isoformat(), "received_on")
    if day > today:
        raise Invalid("invalid_received_on", "today or earlier")
    reference = _ref(body.get("reference"), "reference", required=False)
    try:
        async with ctx.conn.transaction():
            await ctx.conn.fetchval(RECEIVE_SQL, commission_id, amount, currency, day, reference)
    except asyncpg.PostgresError as exc:
        if getattr(exc, "sqlstate", None) == "CRM25":
            raise Conflict("fx_rate_missing", f"no {currency} rate within 30 days of {day}: add one in Settings > "
                           "Payments", currency=currency) from None  # fmt: skip
        raise
    return await _after(ctx, row, "commission_receive")


async def write_off(ctx: TenantContext, commission_id: uuid.UUID, reason: Any, note: Any) -> dict[str, Any]:
    if reason not in WRITE_OFF_REASONS:
        raise Invalid("invalid_reason", ", ".join(WRITE_OFF_REASONS))
    text = None
    bad = not isinstance(note, str) or len(note.strip()) > 500 or re.search(r"[\x00-\x1f\x7f]", note) is not None
    if note is not None and bad:
        raise Invalid("invalid_note", "at most 500 characters on one line")
    if isinstance(note, str) and note.strip():
        text = note.strip()
    row = await _lock(ctx, commission_id, ("expected", "claimed"))
    await ctx.conn.execute(WRITE_OFF_SQL, commission_id, reason, text, f"member:{ctx.member_id}")
    return await _after(ctx, row, "commission_write_off", reason)


async def set_due(ctx: TenantContext, commission_id: uuid.UUID, due_on: Any) -> dict[str, Any]:
    row = await _lock(ctx, commission_id, ("expected", "claimed"))
    await ctx.conn.execute(DUE_SQL, commission_id, _day(due_on, "due_on"))
    return await _after(ctx, row, "update", "due_on")


async def for_lines(ctx: TenantContext, trip_id: uuid.UUID) -> dict[uuid.UUID, dict[str, Any]]:
    """The live commission row of each line of a trip (the Bookings tab), for owner / agent."""
    rows = await ctx.conn.fetch(LIST_SQL, trip_id, None, False, None, MAX_LIST)
    if not rows:
        return {}
    today = await fx.agency_today(ctx)
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for r in rows:
        if r["status"] != "cancelled" or r["booking_line_id"] not in out:
            v = commission_view(r, today)
            out[r["booking_line_id"]] = {
                "commission_id": v["commission_id"],
                "status": v["status"],
                "due_on": v["due_on"],
                "overdue": v["overdue"],
            }
    return out


# ── the hourly date rule (app.stages_job) ─────────────────────────────────────────────────────────


async def sweep(ctx: AnyContext) -> int:
    """Sync every trip whose commission rows are missing or out of date (a catch-all for any path that changed a line
    without a sync). Returns the trips synced."""
    trips = [r["trip_id"] for r in await ctx.conn.fetch(SWEEP_SQL, MAX_SWEEP)]
    for trip_id in trips:
        await sync(ctx, trip_id)
    return len(trips)


async def alert_overdue(ctx: AnyContext, today: dt.date) -> int:
    """One owner task ``commission_overdue`` per trip with a commission past its due date, once per row (agent first:
    the CRM never writes to the supplier). Returns the rows alerted."""
    rows = await ctx.conn.fetch(OVERDUE_SQL, today, MAX_SWEEP)
    if not rows:
        return 0
    owner = await ctx.conn.fetchval(OWNER_SQL)
    for trip_id in dict.fromkeys(r["trip_id"] for r in rows):
        await ctx.conn.execute(TASK_SQL, trip_id, OVERDUE_TASK, owner)
    await ctx.conn.execute(ALERTED_SQL, [r["commission_id"] for r in rows])
    return len(rows)


async def close_supplier_tasks(ctx: AnyContext, actor: str) -> int:
    from app.cancellation import SUPPLIER_TASK

    return len(await ctx.conn.fetch(CLOSE_SUPPLIER_TASKS_SQL, SUPPLIER_TASK, actor))
