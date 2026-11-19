"""Ask Adventure Asia for extra hotel nights (AA-1144, S285 decision B11; crm-080 ``crm.aa_extra_night_request``).

A hotel is an Adventure Asia product: an agency never sells a hotel night of its own (root ADR 0019 b6 as amended).
When a client wants nights before or after the tour, the agent asks AA from the trip ("Ask AA for extra nights"):

- :func:`create` (``TRIP_EDIT``): the place (a city or area), before / after the tour, the check-in day, nights, rooms,
  adults and children. No traveller name and no other free text: AA ground operations read these rows across
  agencies. A cancelled trip asks nothing (409 ``trip_cancelled``). Each new request raises the ops check
  ``aa_extra_nights_requested`` (one AA ops e-mail per request) next to the staff console list.
- :func:`list_for_trip`: the trip's requests, newest first (open and closed).
- :func:`close` (``TRIP_EDIT``): open -> closed with ``quoted`` (AA answered and the agent added the AA hotel item from
  the AA drawer) or ``not_needed``. A closed request stays closed (409 ``request_closed``).
- :func:`staff_board`: the open requests of every agency for AA ground operations (``crm-ops``, one agency at a time
  through :func:`app.tenancy.ops_tx`, one ``extra_nights_list`` audit event per agency listed): agency name, trip
  reference, place, dates and counts.

The bounds below are the crm-080 CHECK constraints worded for the member (technical constants, not business
thresholds): the database refuses the same values.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import asyncpg

from app import cancellation
from app.audit_export import registry
from app.auth.route_guard import Caller
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.observability import ops_alerts
from app.tenancy import OpsContext, TenantContext, ops_tx

TABLE = "aa_extra_night_request"
POSITIONS = ("before", "after")
CLOSE_REASONS = ("quoted", "not_needed")
PLACE_CHARS = 80  # technical-constant: crm-080 aa_extra_night_request_values_chk (1..80 characters)
# technical-constant: the crm-080 CHECK bounds of each count (the same as crm.option.aa_nights and the room list).
COUNTS = {"nights": (1, 60), "rooms": (1, 30), "adults": (1, 60), "children": (0, 60)}
BOARD_MAX_PER_AGENCY = 200  # technical-constant: one page of the staff list per agency

TRIP_SQL = "SELECT trip_id FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
INSERT_SQL = """
INSERT INTO crm.aa_extra_night_request (trip_id, position, place, check_in, nights, rooms, adults, children)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING request_id
"""
_COLUMNS = """
request_id, trip_id, position, place, check_in, nights, rooms, adults, children, status, close_reason, closed_at,
created_at
"""
LIST_SQL = f"""
SELECT {_COLUMNS} FROM crm.aa_extra_night_request
 WHERE trip_id = $1 AND deleted_at IS NULL
 ORDER BY created_at DESC, request_id
"""  # noqa: S608 — fixed text
ONE_SQL = f"SELECT {_COLUMNS} FROM crm.aa_extra_night_request WHERE request_id = $1 AND deleted_at IS NULL"  # noqa: S608
CLOSE_SQL = """
UPDATE crm.aa_extra_night_request SET status = 'closed', close_reason = $2, closed_at = now()
 WHERE request_id = $1 AND status = 'open' AND deleted_at IS NULL
RETURNING request_id
"""
BOARD_SQL = f"""
SELECT {_COLUMNS} FROM crm.aa_extra_night_request
 WHERE status = 'open' AND deleted_at IS NULL
 ORDER BY check_in, created_at, request_id
 LIMIT $1
"""  # noqa: S608 — fixed text
AGENCY_NAME_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"


def _place(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= PLACE_CHARS or any(ord(c) < 32 for c in value):
        raise Invalid("invalid_place", f"the city or area of the nights, 1 to {PLACE_CHARS} characters")
    return value.strip()


def _count(body: dict[str, Any], field: str) -> int:
    low, high = COUNTS[field]
    value = body.get(field, 0 if field == "children" else None)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Invalid(f"invalid_{field}", f"a whole number from {low} to {high}")
    return value


def _check_in(value: Any) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise Invalid("invalid_check_in", "a date, YYYY-MM-DD") from None


def _position(value: Any) -> str:
    if value not in POSITIONS:
        raise Invalid("invalid_position", "before or after the tour")
    return str(value)


def _iso(value: dt.date | dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _view(row: Any) -> dict[str, Any]:
    return {
        "request_id": str(row["request_id"]),
        "trip_id": str(row["trip_id"]),
        "position": row["position"],
        "place": row["place"],
        "check_in": _iso(row["check_in"]),
        "check_out": _iso(row["check_in"] + dt.timedelta(days=row["nights"])),
        "nights": row["nights"],
        "rooms": row["rooms"],
        "adults": row["adults"],
        "children": row["children"],
        "status": row["status"],
        "close_reason": row["close_reason"],
        "closed_at": _iso(row["closed_at"]),
        "created_at": _iso(row["created_at"]),
    }


async def create(ctx: TenantContext, trip_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
    if await ctx.conn.fetchval(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    await cancellation.ensure_not_cancelled(ctx, trip_id, "no new request to Adventure Asia")
    values = (
        _position(body.get("position")),
        _place(body.get("place")),
        _check_in(body.get("check_in")),
        *(_count(body, f) for f in COUNTS),
    )
    request_id = await ctx.conn.fetchval(INSERT_SQL, trip_id, *values)
    ctx.audit("create", "allowed", TABLE, str(request_id))
    ops_alerts.emit("aa_extra_nights_requested", request_id, tenant_id=ctx.tenant_id, request_id=request_id)
    return _view(await ctx.conn.fetchrow(ONE_SQL, request_id))


async def list_for_trip(ctx: TenantContext, trip_id: uuid.UUID) -> dict[str, Any]:
    if await ctx.conn.fetchval(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    return {"requests": [_view(r) for r in await ctx.conn.fetch(LIST_SQL, trip_id)]}


async def close(ctx: TenantContext, request_id: uuid.UUID, reason: Any) -> dict[str, Any]:
    if reason not in CLOSE_REASONS:
        raise Invalid("invalid_reason", "quoted or not_needed")
    row = await ctx.conn.fetchrow(ONE_SQL, request_id)
    if row is None:
        raise NotFound("request_not_found")
    if await ctx.conn.fetchval(CLOSE_SQL, request_id, reason) is None:
        raise Conflict("request_closed", "this request is closed already")
    ctx.audit("update", "allowed", TABLE, str(request_id), fields=("status",), reason=str(reason))
    return _view(await ctx.conn.fetchrow(ONE_SQL, request_id))


async def staff_board(pool: asyncpg.Pool, caller: Caller) -> dict[str, Any]:
    """The open requests of every agency (AA ground operations): no personal data."""
    items: list[dict[str, Any]] = []
    for agency in await registry.agencies(pool):
        try:
            async with ops_tx(pool, caller, agency.tenant_id) as ctx:
                items.extend(await _agency_board(ctx))
        except (NotFound, Forbidden):
            continue  # offboarding or gone; AA-1019: the Dev-only E2E staff identity on a non-synthetic agency
    items.sort(key=lambda i: (i["check_in"], i["agency_name"] or "", i["request_id"]))
    return {"items": items}


async def _agency_board(ctx: OpsContext) -> list[dict[str, Any]]:
    rows = await ctx.conn.fetch(BOARD_SQL, BOARD_MAX_PER_AGENCY)
    if not rows:
        return []
    name = await ctx.conn.fetchval(AGENCY_NAME_SQL, ctx.tenant_id)
    ctx.audit("extra_nights_list", "allowed", TABLE, None, reason="board",
              object_ids=tuple(str(r["request_id"]) for r in rows))  # fmt: skip
    return [
        {**_view(r), "tenant_id": str(ctx.tenant_id), "agency_name": name, "trip_ref": str(r["trip_id"])[:8]}
        for r in rows
    ]
