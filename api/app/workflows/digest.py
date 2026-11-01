"""``crm_task_digest``: one agency's morning digest of open tasks (AA-959; design WF-10 "the morning digest carries
counts only", R20; BYO errata S247 §3 item 3: an operational alert to agency people is AA system mail).

Queued by the hourly tick at the agency's local 07:00 when the owner switched it on (``tenant_settings.task_digest``),
once per local day (idempotency key per agency and date). Per active member with at least one open task assigned:
the counts overdue / today / next 7 days / waiting for dates, through AA's SES (the partner pool address, never a
client's), best effort per member. Payload ``{tenant_id}``; result counts only. The mail holds no client, trip or task
text."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

import asyncpg

from app.errors import Forbidden, NotFound
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.members.mail import task_digest_message
from app.tenancy import system_tx
from app.workflows import facts as wf_facts

log = logging.getLogger(__name__)

ACTOR = "job:crm_task_digest"
COUNTS_SQL = """
SELECT m.member_id, m.cognito_iss, m.cognito_sub,
       count(*) FILTER (WHERE k.due_at < $1)::int AS overdue,
       count(*) FILTER (WHERE k.due_at >= $1 AND k.due_at < $2)::int AS today,
       count(*) FILTER (WHERE k.due_at >= $2 AND k.due_at < $3)::int AS upcoming,
       count(*) FILTER (WHERE k.due_at IS NULL)::int AS waiting
  FROM crm.task k JOIN crm.member m ON m.member_id = k.assignee_member_id
 WHERE k.status = 'open' AND k.deleted_at IS NULL AND m.status = 'active' AND m.deleted_at IS NULL
 GROUP BY m.member_id, m.cognito_iss, m.cognito_sub
"""
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
ENABLED_SQL = "SELECT task_digest FROM crm.tenant_settings"


def windows(now: dt.datetime, tz: Any) -> tuple[dt.datetime, dt.datetime, dt.datetime]:
    """Now, the end of today and the end of the next 7 days in the agency's timezone."""
    local = now.astimezone(tz)
    midnight = dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time(0), tzinfo=tz)
    return now, midnight.astimezone(dt.UTC), (midnight + dt.timedelta(days=7)).astimezone(dt.UTC)


def build_handler(mail_runtime: Any) -> Handler:
    async def crm_task_digest(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        try:
            tenant_id = uuid.UUID(str(job.payload["tenant_id"]))
        except (KeyError, ValueError):
            raise PermanentError("bad_payload") from None
        directory = getattr(mail_runtime, "directory", None)
        mailer = getattr(mail_runtime, "mailer", None)
        if directory is None or mailer is None:
            return {"sent_count": 0, "reason_code": "mail_not_configured"}
        try:
            async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                if not await ctx.conn.fetchval(ENABLED_SQL):
                    return {"sent_count": 0, "reason_code": "digest_off"}
                tz = await wf_facts.timezone(ctx)
                now, today_end, week_end = windows(dt.datetime.now(dt.UTC), tz)
                rows = await ctx.conn.fetch(COUNTS_SQL, now, today_end, week_end)
                agency = await ctx.conn.fetchval(AGENCY_SQL, tenant_id) or "your agency"
                ctx.audit("task_digest", "allowed", "member", object_ids=tuple(str(r["member_id"]) for r in rows)[:200])
        except (Forbidden, NotFound):
            return {"sent_count": 0, "reason_code": "agency_skipped"}
        sent = failed = 0
        origin = getattr(mail_runtime, "web_origin", "")
        for r in rows:
            if r["cognito_iss"] != directory.issuer:
                continue
            counts = {k: int(r[k]) for k in ("overdue", "today", "upcoming", "waiting")}
            try:
                email = await directory.email_for(r["cognito_sub"])
                if email:
                    await mailer.send(task_digest_message(email, agency=agency, counts=counts, link=origin))
                    sent += 1
            except Exception as exc:  # noqa: BLE001 — one member's mail must not stop the others
                failed += 1
                log.warning("crm_task_digest_failed tenant=%s error=%s", tenant_id, type(exc).__name__)
        return {"member_count": len(rows), "sent_count": sent, "failed_count": failed}

    return crm_task_digest
