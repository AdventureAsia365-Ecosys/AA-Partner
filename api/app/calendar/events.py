"""Calendar events of an agency (AA-961, card CRM-30, design R15), read from the records that already carry a date.

=====================  ==================================================================================
kind                   source
=====================  ==================================================================================
``departure``          ``crm.trip.start_date`` of a booked, pre-departure or travelling trip
``return``             ``crm.trip.end_date`` of the same trips
``payment_due``        ``crm.payment_schedule.due_on`` of a ``due`` deposit / balance row (crm-016)
``invoice_due``        ``crm.invoice.due_on`` of an ``open`` / ``payment_claimed`` invoice (crm-024)
``signing_deadline``   start date - 21 days of a trip with a ``pending`` signature (crm-025; R29 "waiver missing
                       at T-21")
``task``               ``crm.task.due_at`` of an open task
``call``               ``crm.meeting_slot.starts_at`` of a requested / confirmed call (crm-012, R15)
=====================  ==================================================================================

Scope ``mine``: the member's own trips (``owner_member_id``), tasks assigned to them (or unassigned on their trips) and
calls on their trips or clients; ``agency``: everything. Ids, codes, dates, amounts and trip titles for the in-app
calendar; :mod:`app.calendar.ics` leaves titles and amounts out of the feed that leaves the CRM.

Every function takes the open context (a member's or the feed's) and never opens one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.errors import Invalid
from app.tenancy import SystemContext, TenantContext

KINDS = ("departure", "return", "payment_due", "invoice_due", "signing_deadline", "task", "call")
SIGNING_LEAD_DAYS = 21
MAX_SPAN_DAYS = 400
MAX_EVENTS = 2000
LIVE_STAGES = ("booked", "pre_departure", "travelling")
DEFAULT_TZ = "Asia/Ho_Chi_Minh"
TZ_SQL = "SELECT coalesce((SELECT timezone FROM crm.tenant_settings LIMIT 1), $1)"

TRIP_DATES_SQL = """
SELECT t.trip_id, t.title, t.stage, t.start_date, t.end_date, t.owner_member_id
  FROM crm.trip t
 WHERE t.deleted_at IS NULL AND t.stage = ANY($1::text[])
   AND ((t.start_date BETWEEN $2 AND $3) OR (t.end_date BETWEEN $2 AND $3))
   AND ($4::uuid IS NULL OR t.owner_member_id = $4)
 ORDER BY t.start_date, t.trip_id
 LIMIT $5
"""
SCHEDULE_SQL = """
SELECT s.schedule_id, s.trip_id, s.kind, s.amount, s.currency, s.due_on, t.title
  FROM crm.payment_schedule s JOIN crm.trip t ON t.trip_id = s.trip_id AND t.deleted_at IS NULL
 WHERE s.status = 'due' AND s.deleted_at IS NULL AND s.due_on BETWEEN $1 AND $2
   AND t.stage NOT IN ('lost', 'cancelled')
   AND ($3::uuid IS NULL OR t.owner_member_id = $3)
 ORDER BY s.due_on, s.schedule_id
 LIMIT $4
"""
INVOICE_SQL = """
SELECT i.invoice_id, i.trip_id, i.number, i.kind, i.total_amount, i.currency, i.due_on, t.title
  FROM crm.invoice i JOIN crm.trip t ON t.trip_id = i.trip_id AND t.deleted_at IS NULL
 WHERE i.status IN ('open', 'payment_claimed') AND i.deleted_at IS NULL AND i.due_on BETWEEN $1 AND $2
   AND ($3::uuid IS NULL OR t.owner_member_id = $3)
 ORDER BY i.due_on, i.invoice_id
 LIMIT $4
"""
SIGNING_SQL = """
SELECT t.trip_id, t.title, t.start_date, count(*) AS pending
  FROM crm.signature_request r JOIN crm.trip t ON t.trip_id = r.trip_id AND t.deleted_at IS NULL
 WHERE r.status = 'pending' AND r.deleted_at IS NULL AND t.stage = ANY($1::text[]) AND t.start_date IS NOT NULL
   AND t.start_date - $5::int BETWEEN $2 AND $3
   AND ($4::uuid IS NULL OR t.owner_member_id = $4)
 GROUP BY t.trip_id, t.title, t.start_date
 ORDER BY t.start_date, t.trip_id
 LIMIT $6
"""
TASK_SQL = """
SELECT k.task_id, k.trip_id, k.kind, k.priority, k.due_at, t.title
  FROM crm.task k LEFT JOIN crm.trip t ON t.trip_id = k.trip_id
 WHERE k.status = 'open' AND k.deleted_at IS NULL AND k.due_at IS NOT NULL
   AND k.due_at >= $1 AND k.due_at < $2
   AND (k.trip_id IS NULL OR t.deleted_at IS NULL)
   AND ($3::uuid IS NULL OR k.assignee_member_id = $3 OR (k.assignee_member_id IS NULL AND t.owner_member_id = $3))
 ORDER BY k.due_at, k.task_id
 LIMIT $4
"""
CALL_SQL = """
SELECT s.slot_id, s.trip_id, s.starts_at, s.duration_min, s.status, t.title
  FROM crm.meeting_slot s
  LEFT JOIN crm.trip t ON t.trip_id = s.trip_id
  LEFT JOIN crm.traveler tr ON tr.traveler_id = s.traveler_id
 WHERE s.status IN ('requested', 'confirmed') AND s.deleted_at IS NULL
   AND s.starts_at >= $1 AND s.starts_at < $2
   AND (s.trip_id IS NULL OR t.deleted_at IS NULL)
   AND ($3::uuid IS NULL OR t.owner_member_id = $3 OR (s.trip_id IS NULL AND tr.owner_member_id = $3))
 ORDER BY s.starts_at, s.slot_id
 LIMIT $4
"""


def window(start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
    if end < start:
        raise Invalid("invalid_range", "the end is before the start")
    if (end - start).days > MAX_SPAN_DAYS:
        raise Invalid("invalid_range", f"at most {MAX_SPAN_DAYS} days")
    return start, end


def _id(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def _money(amount: Any, currency: str) -> dict[str, str]:
    return {"amount": f"{amount:.2f}", "currency": currency}


async def events(
    ctx: TenantContext | SystemContext, *, start: dt.date, end: dt.date, member_id: uuid.UUID | None, tz: dt.tzinfo
) -> list[dict[str, Any]]:
    """Every event between ``start`` and ``end`` (both included), sorted by date; ``member_id`` None = the agency.
    Timed events (tasks, calls) use ``tz`` for their day."""
    first, last = window(start, end)
    since = dt.datetime.combine(first, dt.time.min, tz)
    until = dt.datetime.combine(last + dt.timedelta(days=1), dt.time.min, tz)
    out: list[dict[str, Any]] = []
    for t in await ctx.conn.fetch(TRIP_DATES_SQL, list(LIVE_STAGES), first, last, member_id, MAX_EVENTS):
        base = {"trip_id": str(t["trip_id"]), "title": t["title"], "stage": t["stage"], "all_day": True}
        if t["start_date"] is not None and first <= t["start_date"] <= last:
            out.append({**base, "kind": "departure", "id": f"departure-{t['trip_id']}", "date": t["start_date"]})
        if t["end_date"] is not None and first <= t["end_date"] <= last:
            out.append({**base, "kind": "return", "id": f"return-{t['trip_id']}", "date": t["end_date"]})
    for s in await ctx.conn.fetch(SCHEDULE_SQL, first, last, member_id, MAX_EVENTS):
        out.append(
            {
                "kind": "payment_due",
                "id": f"payment-{s['schedule_id']}",
                "date": s["due_on"],
                "all_day": True,
                "trip_id": str(s["trip_id"]),
                "title": s["title"],
                "payment_kind": s["kind"],
                "amount": _money(s["amount"], s["currency"]),
            }
        )
    for i in await ctx.conn.fetch(INVOICE_SQL, first, last, member_id, MAX_EVENTS):
        out.append(
            {
                "kind": "invoice_due",
                "id": f"invoice-{i['invoice_id']}",
                "date": i["due_on"],
                "all_day": True,
                "trip_id": str(i["trip_id"]),
                "title": i["title"],
                "invoice_number": i["number"],
                "amount": _money(i["total_amount"], i["currency"]),
            }
        )
    signing = (list(LIVE_STAGES), first, last, member_id, SIGNING_LEAD_DAYS, MAX_EVENTS)
    for g in await ctx.conn.fetch(SIGNING_SQL, *signing):
        out.append(
            {
                "kind": "signing_deadline",
                "id": f"signing-{g['trip_id']}",
                "date": g["start_date"] - dt.timedelta(days=SIGNING_LEAD_DAYS),
                "all_day": True,
                "trip_id": str(g["trip_id"]),
                "title": g["title"],
                "pending_signatures": int(g["pending"]),
            }
        )
    for k in await ctx.conn.fetch(TASK_SQL, since, until, member_id, MAX_EVENTS):
        at = k["due_at"].astimezone(tz)
        out.append(
            {
                "kind": "task",
                "id": f"task-{k['task_id']}",
                "date": at.date(),
                "starts_at": at,
                "all_day": False,
                "trip_id": _id(k["trip_id"]),
                "title": k["title"],
                "task_id": str(k["task_id"]),
                "task_kind": k["kind"],
                "priority": k["priority"],
            }
        )
    for c in await ctx.conn.fetch(CALL_SQL, since, until, member_id, MAX_EVENTS):
        at = c["starts_at"].astimezone(tz)
        out.append(
            {
                "kind": "call",
                "id": f"call-{c['slot_id']}",
                "date": at.date(),
                "starts_at": at,
                "ends_at": at + dt.timedelta(minutes=int(c["duration_min"])),
                "all_day": False,
                "trip_id": _id(c["trip_id"]),
                "title": c["title"],
                "call_status": c["status"],
            }
        )
    out.sort(key=lambda e: (e["date"], e.get("starts_at") or dt.datetime.min.replace(tzinfo=tz), e["id"]))
    return out[:MAX_EVENTS]


def as_json(event: dict[str, Any]) -> dict[str, Any]:
    """An event for the API response (dates and times as ISO strings)."""
    out = dict(event)
    for key in ("date", "starts_at", "ends_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    return out


def zone(name: str | None) -> ZoneInfo:
    """The agency's timezone (``tenant_settings.timezone``); the default when unset or unknown."""
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


async def agency_calendar(
    ctx: TenantContext, *, start: dt.date | None, end: dt.date | None, mine: bool, now: dt.datetime | None = None
) -> dict[str, Any]:
    """The in-app calendar (A-09): default this month and the next two; ``mine`` = the member's own records."""
    tz = zone(await ctx.conn.fetchval(TZ_SQL, DEFAULT_TZ))
    today = (now or dt.datetime.now(dt.UTC)).astimezone(tz).date()
    first = start or today.replace(day=1)
    last = end or (first + dt.timedelta(days=92))
    items = await events(ctx, start=first, end=last, member_id=ctx.member_id if mine else None, tz=tz)
    return {
        "from": first.isoformat(),
        "to": last.isoformat(),
        "today": today.isoformat(),
        "timezone": str(tz),
        "scope": "mine" if mine else "agency",
        "events": [as_json(e) for e in items],
        "truncated": len(items) >= MAX_EVENTS,
    }
