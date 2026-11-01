"""Tasks (A-09, the tasks drawer; AA-959; design v3 WF-10, R20, R44 step 1, U11): one list of the agency's open tasks
in buckets, a member's own tasks, edits, and the detail of one task.

- **Buckets** (R20, in the agency's timezone): ``overdue`` (due before now), ``today``, ``upcoming`` (the next 7
  days), ``later``, ``waiting_for_dates`` (no due time: anchored on a trip date the trip has not got yet; it fills in
  when the dates are set). Scope ``mine`` (assigned to the caller, or unassigned on the caller's trips), ``team``
  (everyone's; ``member_id`` narrows it), or one trip / one client. ``status=done`` lists what was done in the last
  :data:`DONE_DAYS` days instead.
- **Source** of a task, derived: ``workflow`` (a run opened it), ``manual`` (a member's own custom task), ``aa_event``
  (an AA line or incident: the ``aa_`` kinds), ``system`` (every other module rule). A coded kind is worded by the web
  (``task.<kind>``); a custom task (kind ``custom_<hex>``) carries its title, sealed under the vault DEK and opened
  here (one ``list_view`` audit event per list, design §4.4).
- **Create** (every role, ``TASK_UPDATE``): a title, optionally a trip or a client (never both), a due time or a due
  rule on the trip's departure / return (R20), a priority, an assignee (an active member; default the caller).
- **Edit**: title (custom tasks only), due time (``due_overridden``: a date edited by hand is never recomputed when the
  trip's dates change, R20), assignee, priority.
- **Detail**: one task; ``confirm_arrival_flight`` (R44 step 1) also gets the arrival flights of the trip with a
  public flight status link (the flight number is opened and the view audited).

Lists carry no contact data: a client task names the client by id only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from app.errors import Conflict, Invalid, NotFound
from app.tenancy import AnyContext, TenantContext
from app.vault import Vault
from app.workflows import facts as wf_facts
from app.workflows import model

SCOPES = ("mine", "team", "trip", "client")
BUCKETS = ("overdue", "today", "upcoming", "later", "waiting_for_dates")
DONE_DAYS = 7
MAX_LIST = 500
FLIGHT_STATUS_URL = "https://www.flightaware.com/live/flight/{code}"

COLUMNS = """
k.task_id, k.kind, k.priority, k.assignee_member_id, k.status, k.due_at, k.due_anchor, k.due_offset_days,
k.due_overridden, k.done_at, k.created_at, k.created_by, k.trip_id, k.traveler_id, k.workflow_run_id, k.title_enc,
t.title AS trip_title, t.stage AS trip_stage
"""
LIST_SQL = f"""
SELECT {COLUMNS}
  FROM crm.task k LEFT JOIN crm.trip t ON t.trip_id = k.trip_id
 WHERE k.deleted_at IS NULL AND (t.trip_id IS NULL OR t.deleted_at IS NULL)
   AND (($1 AND k.status = 'open') OR (NOT $1 AND k.status <> 'open' AND k.done_at > now() - make_interval(days => $2)))
   AND ($3::uuid IS NULL OR k.assignee_member_id = $3
        OR ($4 AND k.assignee_member_id IS NULL AND t.owner_member_id = $3))
   AND ($5::uuid IS NULL OR k.trip_id = $5)
   AND ($6::uuid IS NULL OR k.traveler_id = $6)
 ORDER BY k.due_at NULLS LAST, k.created_at, k.task_id
 LIMIT $7
"""  # noqa: S608 — a constant column list
ONE_SQL = f"""
SELECT {COLUMNS}
  FROM crm.task k LEFT JOIN crm.trip t ON t.trip_id = k.trip_id
 WHERE k.task_id = $1 AND k.deleted_at IS NULL
"""  # noqa: S608 — a constant column list
LOCK_SQL = """
SELECT task_id, kind, status, trip_id, traveler_id FROM crm.task WHERE task_id = $1 AND deleted_at IS NULL FOR UPDATE
"""
TRIP_SQL = "SELECT 1 FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
CLIENT_SQL = "SELECT 1 FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL"
INSERT_SQL = """
INSERT INTO crm.task (task_id, trip_id, traveler_id, kind, priority, assignee_member_id, due_anchor, due_offset_days,
                      due_at, due_overridden, title_enc)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""
UPDATE_SQL = """
UPDATE crm.task
   SET title_enc = coalesce($2, title_enc),
       due_at = CASE WHEN $3 THEN $4 ELSE due_at END,
       due_overridden = due_overridden OR $3,
       assignee_member_id = CASE WHEN $5 THEN $6 ELSE assignee_member_id END,
       priority = coalesce($7, priority)
 WHERE task_id = $1
"""
FLIGHTS_SQL = """
SELECT traveler_flight_id, flight_no_enc, airport_iata, sched_local, tz FROM crm.traveler_flight
 WHERE trip_id = $1 AND direction = 'arrival' AND deleted_at IS NULL
 ORDER BY sched_local, traveler_flight_id
 LIMIT 20
"""
# R20: date the open tasks anchored on the trip's dates (never one a member edited by hand).
REDATE_SQL = """
WITH due AS (
    SELECT k.task_id,
           CASE WHEN d.day IS NULL THEN NULL
                ELSE ((d.day + k.due_offset_days) + time '09:00') AT TIME ZONE $1 END AS due_at
      FROM crm.task k
      JOIN crm.trip t ON t.trip_id = k.trip_id
      CROSS JOIN LATERAL (SELECT CASE k.due_anchor WHEN 'departure' THEN t.start_date ELSE t.end_date END AS day) d
     WHERE k.status = 'open' AND k.deleted_at IS NULL AND NOT k.due_overridden
       AND k.due_anchor IN ('departure', 'return') AND k.due_offset_days IS NOT NULL
)
UPDATE crm.task k SET due_at = due.due_at
  FROM due
 WHERE k.task_id = due.task_id AND k.due_at IS DISTINCT FROM due.due_at
"""
DIGEST_SQL = "SELECT coalesce((SELECT task_digest FROM crm.tenant_settings), false)"
DIGEST_SET_SQL = """
INSERT INTO crm.tenant_settings (task_digest) VALUES ($1)
ON CONFLICT (tenant_id) DO UPDATE SET task_digest = EXCLUDED.task_digest
"""


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def source(r: Any) -> str:
    if r["workflow_run_id"] is not None:
        return "workflow"
    if model.is_custom(r["kind"]):
        return "manual"
    if r["kind"].startswith("aa_"):
        return "aa_event"
    return "system"


def bucket(due_at: dt.datetime | None, now: dt.datetime, tz: ZoneInfo) -> str:
    if due_at is None:
        return "waiting_for_dates"
    if due_at < now:
        return "overdue"
    days = (due_at.astimezone(tz).date() - now.astimezone(tz).date()).days
    if days <= 0:
        return "today"
    if days <= 7:
        return "upcoming"
    return "later"


async def _view(vault: Vault, r: Any) -> dict[str, Any]:
    title = None
    if r["title_enc"] is not None:
        title = await vault.decrypt("task", "title_enc", r["task_id"], r["title_enc"])
    return {
        "task_id": str(r["task_id"]),
        "kind": r["kind"],
        "title": title,
        "priority": r["priority"],
        "status": r["status"],
        "assignee_member_id": str(r["assignee_member_id"]) if r["assignee_member_id"] else None,
        "due_at": _iso(r["due_at"]),
        "due_anchor": r["due_anchor"],
        "due_offset_days": r["due_offset_days"],
        "due_overridden": r["due_overridden"],
        "done_at": _iso(r["done_at"]),
        "created_at": _iso(r["created_at"]),
        "trip_id": str(r["trip_id"]) if r["trip_id"] else None,
        "trip_title": r["trip_title"],
        "trip_stage": r["trip_stage"],
        "traveler_id": str(r["traveler_id"]) if r["traveler_id"] else None,
        "workflow_run_id": str(r["workflow_run_id"]) if r["workflow_run_id"] else None,
        "source": source(r),
    }


async def listing(
    ctx: TenantContext,
    vault: Vault,
    *,
    scope: str = "mine",
    member_id: uuid.UUID | None = None,
    trip_id: uuid.UUID | None = None,
    traveler_id: uuid.UUID | None = None,
    status: str = "open",
) -> dict[str, Any]:
    if scope not in SCOPES:
        raise Invalid("invalid_scope", ", ".join(SCOPES))
    if status not in ("open", "done"):
        raise Invalid("invalid_status", "open or done")
    if scope == "trip" and trip_id is None or scope == "client" and traveler_id is None:
        raise Invalid("target_required", "a trip or a client")
    who = ctx.member_id if scope == "mine" else member_id if scope == "team" else None
    rows = await ctx.conn.fetch(
        LIST_SQL, status == "open", DONE_DAYS, who, scope == "mine",
        trip_id if scope == "trip" else None, traveler_id if scope == "client" else None, MAX_LIST,
    )  # fmt: skip
    tz = await wf_facts.timezone(ctx)
    now = await ctx.conn.fetchval("SELECT now()")
    buckets: dict[str, list[dict[str, Any]]] = {b: [] for b in BUCKETS}
    done: list[dict[str, Any]] = []
    for r in rows:
        item = await _view(vault, r)
        if status == "open":
            item["bucket"] = bucket(r["due_at"], now, tz)
            buckets[item["bucket"]].append(item)
        else:
            done.append(item)
    ctx.audit("list_view", "allowed", "task", None, object_ids=tuple(str(r["task_id"]) for r in rows)[:200])
    out: dict[str, Any] = {
        "scope": scope,
        "status": status,
        "timezone": tz.key,
        "digest": bool(await ctx.conn.fetchval(DIGEST_SQL)),
        "can_set_digest": ctx.member_role == "owner",
    }
    if status == "open":
        out["buckets"] = buckets
        out["counts"] = {b: len(v) for b, v in buckets.items()}
    else:
        out["done"] = done
    return out


async def _assignee(ctx: TenantContext, member_id: uuid.UUID | None) -> uuid.UUID | None:
    if member_id is None:
        return None
    if not await wf_facts.active_member(ctx, member_id):
        raise Invalid("member_not_found", "an active member of this agency")
    return member_id


async def _relative_due(
    ctx: TenantContext, trip_id: uuid.UUID | None, anchor: str, offset: int
) -> tuple[str, int, dt.datetime | None]:
    if trip_id is None or anchor not in ("departure", "return"):
        raise Invalid("invalid_due", "a due rule needs a trip and departure or return")
    if not -model.MAX_TASK_OFFSET_DAYS <= offset <= model.MAX_TASK_OFFSET_DAYS:
        raise Invalid("invalid_due", "offset in days from -365 to 365")
    facts = await wf_facts.trip(ctx, trip_id)
    tz = await wf_facts.timezone(ctx)
    due = model.due_at({"anchor": anchor, "offset_days": offset}, facts, tz, dt.datetime.now(dt.UTC))
    return anchor, offset, due


async def create(
    ctx: TenantContext,
    vault: Vault,
    *,
    title: str,
    trip_id: uuid.UUID | None = None,
    traveler_id: uuid.UUID | None = None,
    due_at: dt.datetime | None = None,
    due_on: dt.date | None = None,
    due_rule: tuple[str, int] | None = None,
    priority: str = "normal",
    assignee_member_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    if trip_id is not None and traveler_id is not None:
        raise Invalid("one_subject", "a task names a trip or a client, not both")
    if trip_id is not None and await ctx.conn.fetchval(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    if traveler_id is not None and await ctx.conn.fetchval(CLIENT_SQL, traveler_id) is None:
        raise NotFound("traveler_not_found")
    if priority not in model.PRIORITIES:
        raise Invalid("invalid_priority", "normal or urgent")
    if sum(x is not None for x in (due_at, due_on, due_rule)) > 1:
        raise Invalid("invalid_due", "a due time, a due date or a due rule, not one more")
    if due_on is not None:  # a date: due at 09:00 in the agency's timezone
        due_at = model.at_local(due_on, await wf_facts.timezone(ctx))
    clean = model.clean_text(title, "title", model.MAX_TITLE)
    assignee = await _assignee(ctx, assignee_member_id) if assignee_member_id else ctx.member_id
    anchor, offset, overridden = ("created", 0, due_at is not None)
    if due_rule is not None:
        anchor, offset, due_at = await _relative_due(ctx, trip_id, *due_rule)
    task_id = uuid.uuid4()
    blob = await vault.encrypt("task", "title_enc", task_id, clean)
    await ctx.conn.execute(
        INSERT_SQL, task_id, trip_id, traveler_id, model.manual_kind(), priority, assignee, anchor, offset, due_at,
        overridden, blob,
    )  # fmt: skip
    ctx.audit("task_create", "allowed", "task", str(task_id), reason="manual")
    if trip_id is not None:
        from app.stages import touch

        await touch(ctx, trip_id)
    return await get(ctx, vault, task_id)


async def update(
    ctx: TenantContext,
    vault: Vault,
    task_id: uuid.UUID,
    *,
    title: str | None = None,
    due_at: dt.datetime | None = None,
    due_on: dt.date | None = None,
    set_due: bool = False,
    assignee_member_id: uuid.UUID | None = None,
    set_assignee: bool = False,
    priority: str | None = None,
) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(LOCK_SQL, task_id)
    if row is None:
        raise NotFound("task_not_found")
    if row["status"] != "open":
        raise Conflict("task_closed", "reopen the task first")
    blob = None
    if title is not None:
        if not model.is_custom(row["kind"]):
            raise Conflict("title_fixed", "only your own tasks have a title to edit")
        blob = await vault.encrypt("task", "title_enc", task_id, model.clean_text(title, "title", model.MAX_TITLE))
    if priority is not None and priority not in model.PRIORITIES:
        raise Invalid("invalid_priority", "normal or urgent")
    assignee = await _assignee(ctx, assignee_member_id) if set_assignee else None
    if due_on is not None:
        due_at, set_due = model.at_local(due_on, await wf_facts.timezone(ctx)), True
    await ctx.conn.execute(UPDATE_SQL, task_id, blob, set_due, due_at, set_assignee, assignee, priority)
    fields = [f for f, on in (("title", title is not None), ("due_at", set_due), ("assignee", set_assignee),
                              ("priority", priority is not None)) if on]  # fmt: skip
    ctx.audit("update", "allowed", "task", str(task_id), fields=tuple(fields))
    return await get(ctx, vault, task_id)


async def get(ctx: TenantContext, vault: Vault, task_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ONE_SQL, task_id)
    if row is None:
        raise NotFound("task_not_found")
    out = await _view(vault, row)
    if row["kind"] == model.ARRIVAL_FLIGHT_TASK and row["trip_id"] is not None:
        flights = []
        rows = await ctx.conn.fetch(FLIGHTS_SQL, row["trip_id"])
        for f in rows:
            number = await vault.decrypt(
                "traveler_flight", "flight_no_enc", f["traveler_flight_id"], f["flight_no_enc"]
            )
            code = "".join(ch for ch in number.upper() if ch.isalnum())
            flights.append({
                "flight_no": number,
                "airport_iata": f["airport_iata"],
                "sched_local": f["sched_local"].isoformat(),
                "tz": f["tz"],
                "status_url": FLIGHT_STATUS_URL.format(code=code) if code else None,
            })  # fmt: skip
        out["flights"] = flights
        if flights:
            ids = tuple(str(f["traveler_flight_id"]) for f in rows)
            ctx.audit("view", "allowed", "traveler_flight", None, fields=("flight_no",), object_ids=ids)
    return out


async def redate(ctx: AnyContext, tz: ZoneInfo | None = None) -> int:
    """R20: the due time of every open task anchored on its trip's departure or return, never one a member edited by
    hand (the hourly tick, and a member's change of the trip's dates)."""
    tz = tz or await wf_facts.timezone(ctx)
    status = await ctx.conn.execute(REDATE_SQL, tz.key)
    tail = str(status).split()[-1] if status else ""
    return int(tail) if tail.isdigit() else 0


async def set_digest(ctx: TenantContext, enabled: bool) -> dict[str, Any]:
    await ctx.conn.execute(DIGEST_SET_SQL, enabled)
    ctx.audit("settings_update", "allowed", "tenant_settings", None, fields=("task_digest",))
    return {"digest": enabled}
