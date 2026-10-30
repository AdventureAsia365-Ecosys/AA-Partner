"""The agency's data-request log (AA-967, errata S247 CRM-35: "received, type, due date, done"; ``crm.data_request``,
crm-026).

The agency is the controller (ADR 0008a): its clients ask it for a copy, a correction, an erasure; AA forwards what it
receives (channel ``aa_forwarded``). The owner logs each request here, answers it with the tools of Settings > Data and
privacy (a client export, an erasure, a correction in the client profile) and closes it with an outcome. The log holds
codes, dates and the client id only (no free text), so it is kept like the erasure receipts.

Due date: received + :data:`DEFAULT_DUE_DAYS` unless given; an extension moves it up to received + :data:`MAX_DUE_DAYS`
(GDPR art. 12(3): one month, extendable by two; an agency under a stricter law sets an earlier date).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import asyncpg

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.tenancy import TenantContext

DEFAULT_DUE_DAYS = 30
MAX_DUE_DAYS = 92
KINDS = ("access", "portability", "erasure", "rectification", "restriction", "objection", "other")
CHANNELS = ("email", "phone", "in_person", "post", "client_portal", "aa_forwarded", "other")
OUTCOMES = {
    "done": ("fulfilled", "partly_fulfilled", "no_data"),
    "refused": ("identity_not_verified", "exempt", "manifestly_unfounded"),
    "withdrawn": ("withdrawn_by_subject",),
}
OPEN = ("open", "extended")
LIST_LIMIT = 100

LIST_SQL = """
SELECT * FROM crm.data_request
 ORDER BY (status IN ('open', 'extended')) DESC, due_on, received_on DESC, data_request_id LIMIT $1
"""
ROW_SQL = "SELECT * FROM crm.data_request WHERE data_request_id = $1"
LOCK_SQL = "SELECT * FROM crm.data_request WHERE data_request_id = $1 FOR UPDATE"
TRAVELER_SQL = "SELECT 1 FROM crm.traveler WHERE traveler_id = $1"
INSERT_SQL = """
INSERT INTO crm.data_request (kind, channel, traveler_id, received_on, due_on)
VALUES ($1, $2, $3, $4, $5) RETURNING data_request_id
"""
EXTEND_SQL = "UPDATE crm.data_request SET status = 'extended', due_on = $2 WHERE data_request_id = $1"
CLOSE_SQL = """
UPDATE crm.data_request SET status = $2, outcome = $3, closed_at = now(), closed_by = $4,
       erasure_request_id = coalesce($5, erasure_request_id)
 WHERE data_request_id = $1
"""
EXPORTS_SQL = """
SELECT export_job_id, status FROM crm.export_job WHERE data_request_id = $1 ORDER BY created_at DESC LIMIT 5
"""


def _owner(ctx: TenantContext, action: str, object_id: str | None = None) -> None:
    if ctx.member_role != "owner":
        ctx.audit(action, "denied", "data_request", object_id, reason="owner_only")
        raise Forbidden("owner_only", "only the agency owner keeps the data-request log")


def today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def view(row: Any, exports: list[Any] | None = None, *, on: dt.date | None = None) -> dict[str, Any]:
    on = on or today()
    return {
        "data_request_id": str(row["data_request_id"]),
        "kind": row["kind"],
        "channel": row["channel"],
        "traveler_id": str(row["traveler_id"]) if row["traveler_id"] else None,
        "received_on": row["received_on"].isoformat(),
        "due_on": row["due_on"].isoformat(),
        "status": row["status"],
        "overdue": row["status"] in OPEN and row["due_on"] < on,
        "max_due_on": (row["received_on"] + dt.timedelta(days=MAX_DUE_DAYS)).isoformat(),
        "outcome": row["outcome"],
        "closed_at": row["closed_at"].isoformat() if row["closed_at"] else None,
        "erasure_request_id": str(row["erasure_request_id"]) if row["erasure_request_id"] else None,
        "exports": [{"export_id": str(e["export_job_id"]), "status": e["status"]} for e in exports or []],
        "created_at": row["created_at"].isoformat(),
    }


async def _view(ctx: TenantContext, request_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ROW_SQL, request_id)
    return view(row, list(await ctx.conn.fetch(EXPORTS_SQL, request_id)))


async def list_requests(ctx: TenantContext) -> dict[str, Any]:
    _owner(ctx, "data_request_list")
    rows = await ctx.conn.fetch(LIST_SQL, LIST_LIMIT)
    on = today()
    out = [view(r, list(await ctx.conn.fetch(EXPORTS_SQL, r["data_request_id"])), on=on) for r in rows]
    return {
        "requests": out,
        "kinds": list(KINDS),
        "channels": list(CHANNELS),
        "outcomes": {k: list(v) for k, v in OUTCOMES.items()},
        "default_due_days": DEFAULT_DUE_DAYS,
        "max_due_days": MAX_DUE_DAYS,
        "today": on.isoformat(),
    }


async def create(
    ctx: TenantContext,
    *,
    kind: str,
    channel: str,
    received_on: dt.date,
    due_on: dt.date | None = None,
    traveler_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    _owner(ctx, "data_request_create")
    if kind not in KINDS or channel not in CHANNELS:
        raise Invalid("invalid_request", "unknown kind or channel")
    if received_on > today():
        raise Invalid("received_in_future", "a request cannot be received after today")
    due = due_on or received_on + dt.timedelta(days=DEFAULT_DUE_DAYS)
    if not received_on <= due <= received_on + dt.timedelta(days=MAX_DUE_DAYS):
        raise Invalid("due_out_of_range", f"due between the receipt and {MAX_DUE_DAYS} days after it")
    if traveler_id is not None and not await ctx.conn.fetchval(TRAVELER_SQL, traveler_id):
        raise NotFound("client_not_found")
    request_id = await ctx.conn.fetchval(INSERT_SQL, kind, channel, traveler_id, received_on, due)
    ctx.audit("data_request_create", "allowed", "data_request", str(request_id), reason=kind)
    return await _view(ctx, request_id)


async def _open(ctx: TenantContext, request_id: uuid.UUID) -> Any:
    row = await ctx.conn.fetchrow(LOCK_SQL, request_id)
    if row is None:
        raise NotFound("data_request_not_found")
    if row["status"] not in OPEN:
        raise Conflict("data_request_closed", f"the request is {row['status']}")
    return row


async def extend(ctx: TenantContext, request_id: uuid.UUID, due_on: dt.date) -> dict[str, Any]:
    _owner(ctx, "data_request_update", str(request_id))
    row = await _open(ctx, request_id)
    if not row["due_on"] < due_on <= row["received_on"] + dt.timedelta(days=MAX_DUE_DAYS):
        raise Invalid("due_out_of_range", f"a later date, at most {MAX_DUE_DAYS} days after the receipt")
    await ctx.conn.execute(EXTEND_SQL, request_id, due_on)
    ctx.audit("data_request_update", "allowed", "data_request", str(request_id), reason="extended")
    return await _view(ctx, request_id)


async def close(
    ctx: TenantContext,
    request_id: uuid.UUID,
    status: str,
    outcome: str,
    *,
    erasure_request_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    _owner(ctx, "data_request_update", str(request_id))
    if outcome not in OUTCOMES.get(status, ()):
        raise Invalid("invalid_outcome", "the outcome does not fit the status", outcomes=list(OUTCOMES.get(status, ())))
    await _open(ctx, request_id)
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(
                CLOSE_SQL, request_id, status, outcome, f"member:{ctx.member_id}", erasure_request_id
            )
    except asyncpg.ForeignKeyViolationError:
        raise NotFound("erasure_request_not_found") from None
    ctx.audit("data_request_update", "allowed", "data_request", str(request_id), reason=f"{status}_{outcome}")
    return await _view(ctx, request_id)


async def for_erasure(ctx: TenantContext, request_id: uuid.UUID, traveler_id: uuid.UUID) -> Any:
    """The logged request a client erasure will close, locked, checked before anything is deleted (AA-967 review):
    open (``open`` / ``extended``, else 409 ``data_request_closed``), an erasure request (409
    ``data_request_not_erasure``) about this client or no client yet (409 ``data_request_other_client``)."""
    row = await _open(ctx, request_id)
    if row["kind"] != "erasure":
        raise Conflict("data_request_not_erasure", f"the request is a {row['kind']} request, not an erasure")
    if row["traveler_id"] is not None and row["traveler_id"] != traveler_id:
        raise Conflict("data_request_other_client", "the request is about another client")
    return row


async def close_erased(ctx: TenantContext, request_id: uuid.UUID, erasure_request_id: uuid.UUID) -> None:
    """An erasure run from a logged request closes it (done / fulfilled) with the receipt."""
    await close(ctx, request_id, "done", "fulfilled", erasure_request_id=erasure_request_id)
