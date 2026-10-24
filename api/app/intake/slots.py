"""Call slots on the inquiry form (R15, card CRM-15; design v3 C-01 "optional call slot", group G ``meeting_slot``).

The owner sets weekly windows, a slot length and a horizon (Settings > Forms, ``tenant_settings.call_slot_rules``,
crm-012) in the agency's timezone. The public form shows the free starts (:func:`offered`: ISO UTC strings, no names,
no other client's data: only "free or not"); the submission names one; ``crm_intake_process`` books it as a
``crm.meeting_slot`` (``requested``, source ``form``) and opens a ``call_scheduled`` task due at the slot. Two
inquiries racing for one slot: the partial UNIQUE of crm-012 lets one in, the other inquiry is still accepted and gets
a ``call_slot_taken`` task instead (:func:`book`). Calendar sync is AA-961 (CRM-30).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from app.tenancy import AnyContext

LEAD_TIME = timedelta(hours=2)  # no slot sooner than this: the agency needs time to see it
MAX_OFFERED = 200
SLOT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_TZ = "Asia/Ho_Chi_Minh"

RULES_SQL = "SELECT timezone, call_slot_rules FROM crm.tenant_settings"
TAKEN_SQL = """
SELECT starts_at FROM crm.meeting_slot
 WHERE status IN ('requested', 'confirmed') AND deleted_at IS NULL AND starts_at >= $1 AND starts_at <= $2
"""
BOOK_SQL = """
INSERT INTO crm.meeting_slot (slot_id, trip_id, traveler_id, starts_at, duration_min, status, source,
                              form_submission_id)
VALUES ($1, $2, $3, $4, $5, 'requested', 'form', $6)
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, assignee_member_id, due_anchor, due_at, priority)
VALUES ($1, $2, $3, 'created', $4, $5)
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""


def _clock(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def free_starts(
    rules: dict[str, Any], tz: str, taken: set[datetime], now: datetime, *, limit: int = MAX_OFFERED
) -> list[datetime]:
    """The free slot starts (UTC) from ``now`` + the lead time to the end of the horizon, in the windows' local time
    (daylight saving handled by the zone). ``rules`` = ``{}`` offers nothing."""
    if not rules or not rules.get("windows"):
        return []
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return []
    length = timedelta(minutes=int(rules["duration_min"]))
    earliest = now + LEAD_TIME
    today = now.astimezone(zone).date()
    out: list[datetime] = []
    for offset in range(int(rules["horizon_days"])):
        day: date = today + timedelta(days=offset)
        for window in sorted(rules["windows"], key=lambda w: w["start"]):
            if int(window["dow"]) != day.isoweekday():
                continue
            start = datetime.combine(day, _clock(window["start"]), zone)
            end = datetime.combine(day, _clock(window["end"]), zone)
            while start + length <= end:
                utc = start.astimezone(UTC)
                if utc >= earliest and utc not in taken:
                    out.append(utc)
                    if len(out) >= limit:
                        return out
                start += length
    return out


def slot_text(value: datetime) -> str:
    return value.astimezone(UTC).strftime(SLOT_FORMAT)


async def _rules(ctx: AnyContext) -> tuple[dict[str, Any], str]:
    row = await ctx.conn.fetchrow(RULES_SQL)
    if row is None:
        return {}, DEFAULT_TZ
    raw = row["call_slot_rules"]
    return (json.loads(raw) if isinstance(raw, str) else dict(raw or {})), row["timezone"]


async def offered(ctx: AnyContext, now: datetime | None = None) -> dict[str, Any]:
    """What the public form shows: the free starts (ISO UTC), the agency timezone and the slot length."""
    rules, tz = await _rules(ctx)
    if not rules:
        return {"call_slots": [], "timezone": tz, "call_minutes": None}
    moment = now or datetime.now(UTC)
    horizon = moment + timedelta(days=int(rules["horizon_days"]) + 1)
    taken = {r["starts_at"] for r in await ctx.conn.fetch(TAKEN_SQL, moment, horizon)}
    starts = free_starts(rules, tz, taken, moment)
    return {"call_slots": [slot_text(s) for s in starts], "timezone": tz, "call_minutes": rules["duration_min"]}


async def is_offered(ctx: AnyContext, slot: str, now: datetime | None = None) -> bool:
    return slot in (await offered(ctx, now))["call_slots"]


async def book(
    ctx: AnyContext,
    *,
    slot: str,
    trip_id: uuid.UUID,
    traveler_id: uuid.UUID,
    submission_id: uuid.UUID,
    owner: uuid.UUID | None,
) -> str:
    """Book the slot an inquiry picked: ``booked`` (a ``call_scheduled`` task due at the slot), or ``taken`` (another
    inquiry got it first: an urgent ``call_slot_taken`` task, the inquiry itself is kept)."""
    rules, _tz = await _rules(ctx)
    starts = datetime.strptime(slot, SLOT_FORMAT).replace(tzinfo=UTC)
    minutes = int(rules.get("duration_min") or 30)
    try:
        async with ctx.conn.transaction():  # savepoint: a lost race must not undo the inquiry
            await ctx.conn.execute(BOOK_SQL, uuid.uuid4(), trip_id, traveler_id, starts, minutes, submission_id)
    except asyncpg.UniqueViolationError:
        await ctx.conn.execute(TASK_SQL, trip_id, "call_slot_taken", owner, datetime.now(UTC), "urgent")
        ctx.audit("call_slot", "allowed", "trip", str(trip_id), reason="taken")
        return "taken"
    await ctx.conn.execute(TASK_SQL, trip_id, "call_scheduled", owner, starts, "normal")
    ctx.audit("call_slot", "allowed", "trip", str(trip_id), reason="booked")
    return "booked"
