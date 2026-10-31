"""Overdue invoice reminders, agent first (card CRM-23 slice 4, AA-952; design v3 U8 "reminders to the agent first",
§3.3 ``tenant_settings.reminders_mode = 'agent_first'`` (Fix E3), A-10 "Reminders queue", the bell; migration crm-028).

Run by the hourly trip date rule (:mod:`app.stages_job`, actor ``job:crm_stage_date_rule``) with "today" in the
agency's own timezone. An issued invoice still ``open`` with money due and its due date passed gives the trip owner a
bell item ``invoice.overdue`` and one open task ``invoice_overdue`` per trip (the agent decides how to chase the client:
the CRM never writes to the client by itself). Schedule: the first reminder the day after the due date, then every
:data:`EVERY_DAYS` days, at most :data:`MAX_REMINDERS` per invoice (``crm.invoice.last_reminded_at`` /
``reminder_count``). Not reminded: a claimed invoice (the client says they paid: the verify task runs), an invoice
waiting for its AA hold to be placed again, a paid / void one. A deposit whose AA hold ran out says so
(``pay_window_closed``): the agent re-quotes rather than chases.

Details carry the invoice number, kind and counts only (no name, no amount of another trip).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.payments import claims
from app.proposals import events
from app.tenancy import AnyContext

EVERY_DAYS = 3
MAX_REMINDERS = 5
TASK = "invoice_overdue"
BATCH = 200

DUE_SQL = """
SELECT i.invoice_id, i.trip_id, i.proposal_id, i.kind, i.status, i.number, i.due_on, i.reminder_count,
       i.schedule_id, i.claimed_at, i.hold_extended_at, i.hold_extend_code, i.rehold_requested_at, i.rehold_code,
       p.kind AS proposal_kind, p.accepted_hold_id, p.hold_id, p.hold_expires_at
  FROM crm.invoice i
  JOIN crm.trip t ON t.trip_id = i.trip_id AND t.deleted_at IS NULL AND t.stage NOT IN ('lost', 'cancelled')
  LEFT JOIN crm.proposal p ON p.proposal_id = i.proposal_id
 WHERE i.status = 'open' AND i.deleted_at IS NULL AND i.rehold_requested_at IS NULL AND i.due_on < $1
   AND i.reminder_count < $2
   AND (i.last_reminded_at IS NULL OR i.last_reminded_at <= now() - make_interval(days => $3))
   AND i.total_amount > (SELECT coalesce(sum(x.amount), 0) FROM crm.payment x
                          WHERE x.invoice_id = i.invoice_id AND x.entry_kind = 'payment')
 ORDER BY i.due_on, i.invoice_id
 LIMIT $4
   FOR UPDATE OF i SKIP LOCKED
"""
REMINDED_SQL = """
UPDATE crm.invoice SET last_reminded_at = now(), reminder_count = reminder_count + 1 WHERE invoice_id = $1
RETURNING reminder_count
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, priority, assignee_member_id, due_anchor, due_at)
SELECT $1, $2, 'normal', t.owner_member_id, 'created', now() + interval '1 day' FROM crm.trip t WHERE t.trip_id = $1
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
"""


async def overdue(ctx: AnyContext, today: dt.date, now: dt.datetime | None = None) -> int:
    """One pass (inside the date rule's transaction): the reminders due now; returns how many went to the bell."""
    now = now or dt.datetime.now(dt.UTC)
    sent = 0
    for row in await ctx.conn.fetch(DUE_SQL, today, MAX_REMINDERS, EVERY_DAYS, BATCH):
        count = await ctx.conn.fetchval(REMINDED_SQL, row["invoice_id"])
        await ctx.conn.execute(TASK_SQL, row["trip_id"], TASK)
        detail: dict[str, Any] = {
            "number": row["number"],
            "kind": row["kind"],
            "days": (today - row["due_on"]).days,
            "reminder": count,
            "pay_window_closed": claims.pay_window(row, now)["state"] == "expired",
        }
        await events.record(ctx, row["trip_id"], events.INVOICE_OVERDUE, proposal_id=row["proposal_id"], bell=True,
                            detail=detail)  # fmt: skip
        ctx.audit("invoice_reminder", "allowed", "invoice", str(row["invoice_id"]), reason="overdue")
        sent += 1
    return sent
