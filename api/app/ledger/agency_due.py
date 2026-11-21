"""A task when an agency line nears its deadline (AA-1147; S285 decision T18; crm-083 ``crm.booking_line.due_at``).

An agency's own line can carry a deadline (``due_at``): the flight's ticketing time limit (past it the held seat is
cancelled), the visa file's submission date, the day to buy the insurance. The hourly workflow tick
(:mod:`app.workflows.tick`, step 5) opens one task for the trip owner when a **planning or requested** line of a live
trip comes within the agency's lead time (setting ``crm.agency_lines.due_lead_days``, :func:`app.ledger.agency_extras.
lead_days`; never a code literal):

- kind by the line's type: ``agency_flight_due``, ``agency_visa_due``, ``agency_insurance_due``, else
  ``agency_line_due`` (the fee, an older line without a type); ``crm.task.booking_line_id`` names the line, the
  task's due time is the line's deadline, urgent;
- once per line and deadline: a task of that line already made for that deadline (open, done or skipped) is never
  made again; moving the deadline makes the next one;
- a trip holds one open task of a kind (crm-005 ``task_open_kind_uq``): a second flight due while the first one's task
  is open waits for it to be done, then gets its own;
- a confirmed or cancelled line, a trashed line or trip, and a Lost, cancelled or completed trip open nothing.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Any

from app.ledger import agency_extras
from app.tenancy import AnyContext

FLIGHT_TASK = "agency_flight_due"
VISA_TASK = "agency_visa_due"
INSURANCE_TASK = "agency_insurance_due"
OTHER_TASK = "agency_line_due"
KIND_BY_CATEGORY = {"flight": FLIGHT_TASK, "visa": VISA_TASK, "insurance": INSURANCE_TASK}
KINDS = (FLIGHT_TASK, VISA_TASK, INSURANCE_TASK, OTHER_TASK)
OPEN_LINE_STATUSES = ("planning", "requested")
CLOSED_STAGES = ("lost", "cancelled", "completed")
MAX_LINES = 500  # technical-constant: one agency's batch per hourly tick (the rest wait for the next hour)

DUE_SQL = """
SELECT l.booking_line_id, l.trip_id, l.category, l.due_at
  FROM crm.booking_line l
  JOIN crm.trip t ON t.trip_id = l.trip_id AND t.deleted_at IS NULL AND t.stage <> ALL($3::text[])
 WHERE l.supplier_kind = 'agency' AND l.deleted_at IS NULL AND l.due_at IS NOT NULL
   AND l.status = ANY($2::text[]) AND l.due_at <= $1
   AND NOT EXISTS (SELECT 1 FROM crm.task k
                    WHERE k.booking_line_id = l.booking_line_id AND k.kind = ANY($4::text[]) AND k.due_at = l.due_at
                      AND k.deleted_at IS NULL)
 ORDER BY l.due_at, l.booking_line_id
 LIMIT $5
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at, booking_line_id)
SELECT $1, $2, 'urgent', t.owner_member_id, 'created', $3, $4 FROM crm.trip t WHERE t.trip_id = $1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
RETURNING task_id
"""


def kind_of(category: str | None) -> str:
    return KIND_BY_CATEGORY.get(category or "", OTHER_TASK)


async def due_tasks(ctx: AnyContext, now: dt.datetime, *, limit: int = MAX_LINES) -> Counter[str]:
    """Step 5 of the hourly tick for one agency (its ``system_tx``): open the tasks of the lines now within the lead
    time. Counts: ``agency_lines_due`` (lines in the window without their task), ``agency_line_tasks_opened``."""
    counts: Counter[str] = Counter()
    days = await agency_extras.lead_days(ctx.conn, ctx.tenant_id)
    horizon = now + dt.timedelta(days=days)
    rows: list[Any] = await ctx.conn.fetch(
        DUE_SQL, horizon, list(OPEN_LINE_STATUSES), list(CLOSED_STAGES), list(KINDS), limit
    )
    for r in rows:
        counts["agency_lines_due"] += 1
        kind = kind_of(r["category"])
        made = await ctx.conn.fetchval(TASK_SQL, r["trip_id"], kind, r["due_at"], r["booking_line_id"])
        if made is not None:
            counts["agency_line_tasks_opened"] += 1
    return counts
