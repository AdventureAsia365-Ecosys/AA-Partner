"""A trip's booking lines and the agency ledger (design v3 M6 non-AA, R17, R18, R22, R25, R26; A-11; crm-014).

Rules the database holds (crm-014) and this module words:

- **Status** planning -> confirmed -> cancelled (planning -> cancelled too). Only confirmed lines count in margins
  and totals; planning is pipeline value; a cancelled line is hidden from travellers (R22).
- **Confirm** goes through ``crm.booking_line_confirm`` (SECURITY DEFINER): the agency's level (off / basic /
  strict) decides what must be filled in (409 ``line_incomplete`` with the missing codes), the money is frozen at the
  rate of the confirmation day (R17), and under strict a second confirmed line of one supplier with one number is 409
  ``duplicate_confirmation_no`` (card R). **The assistant is never blocked** by a missing commission: the line is
  confirmed and the owner gets the task "Add commission" (R18).
- **Money** (cost, sell, commission_expected) is written through ``crm.booking_line_money_put`` (owner and agent:
  ``MONEY_FIELDS_EDIT``) as a new row superseding the old one; frozen rows never change (card E). Money is read only
  by owner and agent (the database's RESTRICTIVE policy returns nothing to the assistant), and every response shapes
  it as keyed objects (``cost``, ``commission_expected``, ``margin_base``) that the margin serializer
  (:mod:`app.margin`) strips for any other role: a list of ``{"kind": "cost"}`` rows would not be caught.
- **AA lines** come from the Booking feed (CRM-19); members cannot write them (403 ``aa_line_read_only``).
- **waiting_on** (AA-943 contract, ``app.stages``): hook ``supplier_lines`` (priority 50) answers ``supplier`` while a
  booked / pre-departure trip has a live agency line still planning. Registered when this module is imported (the
  ledger router imports it, so the API and the in-process worker both have it).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app import stages
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.ledger import fx, refusal
from app.margin import sees_margin
from app.tenancy import AnyContext, TenantContext

KINDS = ("cost", "sell", "commission_expected")
COLLECT = ("agency", "on_arrival")
CONFIRM_CLASSES = ("confirmed", "aa_on_request", "agency_booking")
STATUSES = ("planning", "confirmed", "cancelled")
MAX_AMOUNT = Decimal("999999999999.99")
MAX_LEDGER = 500
ADD_COMMISSION_TASK = "add_commission"

_LINE_COLUMNS = """
l.booking_line_id, l.trip_id, l.supplier_kind, l.agency_supplier_id, s.name AS supplier_name, l.status,
l.aa_sub_status, l.title, l.confirmation_no, l.service_from, l.service_to, l.collect, l.confirm_class,
l.visible_to_travelers, l.confirmed_at, l.departure_id, l.created_at, l.updated_at
"""
LINES_SQL = f"""
SELECT {_LINE_COLUMNS}
  FROM crm.booking_line l JOIN crm.agency_supplier s ON s.agency_supplier_id = l.agency_supplier_id
 WHERE l.trip_id = $1 AND l.deleted_at IS NULL
 ORDER BY l.service_from NULLS LAST, l.created_at, l.booking_line_id
"""  # noqa: S608 — fixed text
LINE_SQL = f"""
SELECT {_LINE_COLUMNS}
  FROM crm.booking_line l JOIN crm.agency_supplier s ON s.agency_supplier_id = l.agency_supplier_id
 WHERE l.booking_line_id = $1 AND l.deleted_at IS NULL
"""  # noqa: S608 — fixed text
MONEY_SQL = """
SELECT booking_line_id, kind, amount, currency, base_currency, amount_base, fx_rate, fx_rate_date, fx_source,
       stale_days
  FROM crm.v_booking_line_money_current
 WHERE booking_line_id = ANY($1::uuid[])
"""
TRIP_SQL = "SELECT stage FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
SUPPLIER_SQL = "SELECT kind, deleted_at FROM crm.agency_supplier WHERE agency_supplier_id = $1"
INSERT_SQL = """
INSERT INTO crm.booking_line (trip_id, agency_supplier_id, title, confirmation_no, service_from, service_to, collect,
                              confirm_class, visible_to_travelers)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
RETURNING booking_line_id
"""
TRIP_OF_LINE_SQL = """
SELECT trip_id, status, supplier_kind FROM crm.booking_line WHERE booking_line_id = $1 AND deleted_at IS NULL
"""
CONFIRM_SQL = "SELECT crm.booking_line_confirm($1)"
CHECK_SQL = "SELECT crm.booking_line_confirm_check($1)"
CANCEL_SQL = """
UPDATE crm.booking_line SET status = 'cancelled' WHERE booking_line_id = $1 AND status IN ('planning', 'confirmed')
RETURNING booking_line_id
"""
MONEY_PUT_SQL = "SELECT crm.booking_line_money_put($1, $2, $3, $4)"
OWNER_SQL = """
SELECT member_id FROM crm.member WHERE role = 'owner' AND status = 'active' ORDER BY created_at, member_id LIMIT 1
"""
TASK_SQL = """
INSERT INTO crm.task (trip_id, kind, assignee_member_id, due_anchor, due_at)
VALUES ($1, $2, $3, 'created', now() + interval '1 day')
ON CONFLICT (trip_id, kind) WHERE status = 'open' AND deleted_at IS NULL AND trip_id IS NOT NULL DO NOTHING
RETURNING task_id
"""
OPEN_SUPPLIER_LINE_SQL = """
SELECT EXISTS (SELECT 1 FROM crm.booking_line
                WHERE trip_id = $1 AND supplier_kind = 'agency' AND status = 'planning' AND deleted_at IS NULL)
"""
LEDGER_SQL = f"""
SELECT {_LINE_COLUMNS}, t.title AS trip_title, t.stage AS trip_stage
  FROM crm.booking_line l
  JOIN crm.agency_supplier s ON s.agency_supplier_id = l.agency_supplier_id
  JOIN crm.trip t ON t.trip_id = l.trip_id AND t.deleted_at IS NULL
 WHERE l.deleted_at IS NULL AND ($1::text IS NULL OR l.status = $1)
   AND ($2::date IS NULL OR l.service_from >= $2) AND ($3::date IS NULL OR l.service_from <= $3)
 ORDER BY l.service_from NULLS LAST, l.created_at, l.booking_line_id
 LIMIT $4
"""  # noqa: S608 — fixed text
# Totals in the base currency: confirmed lines from their frozen base amounts; planning sell as pipeline value at
# today's rate (an estimate; a line whose currency has no rate is counted apart). A confirmed amount frozen in an
# earlier base currency (the owner changed it in Settings > Payments; stored rows are never re-converted) is left out
# of the sums and its lines counted apart (confirmed_other_base), so the totals never add two currencies.
TOTALS_SQL = """
WITH heads AS (
    SELECT m.booking_line_id, m.kind, m.amount, m.currency, m.amount_base, m.base_currency, l.status
      FROM crm.v_booking_line_money_current m
      JOIN crm.booking_line l ON l.booking_line_id = m.booking_line_id AND l.deleted_at IS NULL
      JOIN crm.trip t ON t.trip_id = l.trip_id AND t.deleted_at IS NULL
     WHERE l.status IN ('confirmed', 'planning')
       AND ($1::date IS NULL OR l.service_from >= $1) AND ($2::date IS NULL OR l.service_from <= $2)
)
SELECT
    COALESCE(sum(amount_base) FILTER (WHERE status = 'confirmed' AND kind = 'sell' AND base_currency = $3), 0)
        AS confirmed_sell_base,
    COALESCE(sum(amount_base) FILTER (WHERE status = 'confirmed' AND kind = 'cost' AND base_currency = $3), 0)
        AS confirmed_cost_base,
    COALESCE(sum(amount_base) FILTER (WHERE status = 'confirmed' AND kind = 'commission_expected'
                                        AND base_currency = $3), 0) AS confirmed_commission_expected_base,
    count(DISTINCT h.booking_line_id) FILTER (WHERE status = 'confirmed' AND base_currency <> $3)
        AS confirmed_other_base,
    COALESCE(sum(round(h.amount * r.rate, 2)) FILTER (WHERE status = 'planning' AND kind = 'sell'), 0)
        AS pipeline_sell_base,
    count(*) FILTER (WHERE status = 'planning' AND kind = 'sell' AND r.rate IS NULL) AS pipeline_unconverted
  FROM heads h
  LEFT JOIN LATERAL (SELECT x.rate FROM crm.fx_resolve(h.currency, $3, $4) x) r ON h.status = 'planning'
"""

EDITABLE = (
    "title",
    "confirmation_no",
    "service_from",
    "service_to",
    "collect",
    "confirm_class",
    "visible_to_travelers",
    "agency_supplier_id",
)


# ── validation ───────────────────────────────────────────────────────────────────────────────────


def _title(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200 or any(ord(c) < 32 for c in value):
        raise Invalid("invalid_title", "1 to 200 characters")
    return value.strip()


def _confirmation(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80 or any(ord(c) < 32 for c in value):
        raise Invalid("invalid_confirmation_no", "at most 80 characters")
    return value.strip()


def _choice(value: Any, allowed: tuple[str, ...], field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if value not in allowed:
        raise Invalid(f"invalid_{field}", ", ".join(allowed))
    return str(value)


def _date(value: Any, field: str) -> dt.date | None:
    if value is None or isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise Invalid(f"invalid_{field}", "a date, YYYY-MM-DD") from None


def amount(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Invalid("invalid_amount", "a number from 0, two decimals") from None
    if not number.is_finite() or number < 0 or number > MAX_AMOUNT:
        raise Invalid("invalid_amount", "a number from 0, two decimals")
    return number.quantize(Decimal("0.01"))


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError:
        raise Invalid(f"invalid_{field}", "an id") from None


def _db(exc: asyncpg.PostgresError) -> Exception:
    mapped = refusal(exc)
    return mapped if mapped is not None else exc


# ── views ────────────────────────────────────────────────────────────────────────────────────────


def _money_view(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"amount": str(row["amount"]), "currency": row["currency"]}
    if row["fx_rate"] is not None:
        out.update(
            {
                "amount_base": str(row["amount_base"]),
                "base_currency": row["base_currency"],
                "fx": {
                    "rate": str(row["fx_rate"]),
                    "rate_date": row["fx_rate_date"].isoformat(),
                    "source": row["fx_source"],
                    "stale_days": row["stale_days"],
                },
            }
        )
        if row["fx_source"] == "platform_daily" and row["fx_rate"] != 1:
            out["fx"]["attribution"] = fx.ATTRIBUTION
    return out


def _money(rows: list[Any]) -> dict[str, Any]:
    """Keyed objects (never a list of rows with a kind): the margin serializer strips ``cost`` /
    ``commission_expected`` / ``margin_base`` by key name for a role without ``view_margin``."""
    out: dict[str, Any] = {kind: None for kind in KINDS}
    for r in rows:
        out[r["kind"]] = _money_view(r)
    sell, cost = out["sell"], out["cost"]
    frozen = sell and cost and "amount_base" in sell and "amount_base" in cost
    if frozen and sell["base_currency"] == cost["base_currency"]:
        out["margin_base"] = str(Decimal(sell["amount_base"]) - Decimal(cost["amount_base"]))
    return out


def _line_view(row: Any, money: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "booking_line_id": str(row["booking_line_id"]),
        "trip_id": str(row["trip_id"]),
        "supplier_kind": row["supplier_kind"],
        "agency_supplier_id": str(row["agency_supplier_id"]),
        "supplier_name": row["supplier_name"],
        "status": row["status"],
        "aa_sub_status": row["aa_sub_status"],
        "title": row["title"],
        "confirmation_no": row["confirmation_no"],
        "service_from": row["service_from"].isoformat() if row["service_from"] else None,
        "service_to": row["service_to"].isoformat() if row["service_to"] else None,
        "collect": row["collect"],
        "confirm_class": row["confirm_class"],
        "visible_to_travelers": row["visible_to_travelers"],
        "confirmed_at": row["confirmed_at"].isoformat() if row["confirmed_at"] else None,
        "read_only": row["supplier_kind"] == "aa",
    }
    if money is not None:
        out["money"] = money
    return out


async def _money_by_line(ctx: TenantContext, line_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]] | None:
    """Money per line for a role that sees margins; None for any other role (no query: the database would return
    nothing anyway, and an empty block must not read as "no cost")."""
    if not sees_margin(ctx.member_role):
        return None
    grouped: dict[uuid.UUID, list[Any]] = {}
    for r in await ctx.conn.fetch(MONEY_SQL, line_ids):
        grouped.setdefault(r["booking_line_id"], []).append(r)
    return {line: _money(grouped.get(line, [])) for line in line_ids}


# ── reads ────────────────────────────────────────────────────────────────────────────────────────


async def trip_lines(ctx: TenantContext, trip_id: uuid.UUID) -> dict[str, Any]:
    if await ctx.conn.fetchval(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    rows = await ctx.conn.fetch(LINES_SQL, trip_id)
    money = await _money_by_line(ctx, [r["booking_line_id"] for r in rows])
    settings = await fx.settings(ctx)
    return {
        "trip_id": str(trip_id),
        "lines": [_line_view(r, money.get(r["booking_line_id"]) if money is not None else None) for r in rows],
        "base_currency": settings["base_currency"],
        "booking_validation_level": settings["booking_validation_level"],
    }


async def line(ctx: TenantContext, line_id: uuid.UUID, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(LINE_SQL, line_id)
    if row is None:
        raise NotFound("line_not_found")
    money = await _money_by_line(ctx, [line_id])
    view = _line_view(row, money.get(line_id) if money is not None else None)
    return {**view, **(extra or {})}


async def ledger(
    ctx: TenantContext, *, status: str | None, date_from: dt.date | None, date_to: dt.date | None
) -> dict[str, Any]:
    """A-11: every live line across the agency's live trips; totals in the base currency for owner and agent."""
    if status is not None:
        _choice(status, STATUSES, "status")
    rows = await ctx.conn.fetch(LEDGER_SQL, status, date_from, date_to, MAX_LEDGER)
    money = await _money_by_line(ctx, [r["booking_line_id"] for r in rows])
    settings = await fx.settings(ctx)
    items = []
    for r in rows:
        view = _line_view(r, money.get(r["booking_line_id"]) if money is not None else None)
        view.update({"trip_title": r["trip_title"], "trip_stage": r["trip_stage"]})
        items.append(view)
    out: dict[str, Any] = {
        "lines": items,
        "base_currency": settings["base_currency"],
        "truncated": len(rows) == MAX_LEDGER,
    }
    if sees_margin(ctx.member_role):
        today = await fx.agency_today(ctx)
        t = await ctx.conn.fetchrow(TOTALS_SQL, date_from, date_to, settings["base_currency"], today)
        confirmed_sell, confirmed_cost = Decimal(t["confirmed_sell_base"]), Decimal(t["confirmed_cost_base"])
        out["totals"] = {
            "confirmed_sell_base": str(confirmed_sell),
            "confirmed_cost_base": str(confirmed_cost),
            "confirmed_margin_base": str(confirmed_sell - confirmed_cost),
            "confirmed_commission_expected_base": str(Decimal(t["confirmed_commission_expected_base"])),
            "pipeline_sell_base": str(Decimal(t["pipeline_sell_base"])),
            "pipeline_unconverted": int(t["pipeline_unconverted"]),
            "confirmed_other_base": int(t["confirmed_other_base"]),
        }
    ids = tuple(str(r["booking_line_id"]) for r in rows)
    ctx.audit("list_view", "allowed", "booking_line", None, reason="ledger", object_ids=ids)
    return out


# ── writes ───────────────────────────────────────────────────────────────────────────────────────


async def _own_supplier(ctx: TenantContext, supplier_id: uuid.UUID) -> None:
    row = await ctx.conn.fetchrow(SUPPLIER_SQL, supplier_id)
    if row is None or row["deleted_at"] is not None:
        raise Invalid("invalid_agency_supplier_id", "a supplier of the agency's directory")
    if row["kind"] == "aa":
        # AA lines come from the Booking feed (CRM-19), never from a member.
        raise Invalid("aa_line_read_only", "Adventure Asia lines come from your AA bookings")


async def create_line(ctx: TenantContext, trip_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
    if await ctx.conn.fetchval(TRIP_SQL, trip_id) is None:
        raise NotFound("trip_not_found")
    supplier_id = _uuid(body.get("agency_supplier_id"), "agency_supplier_id")
    await _own_supplier(ctx, supplier_id)
    start, end = _date(body.get("service_from"), "service_from"), _date(body.get("service_to"), "service_to")
    if start and end and end < start:
        raise Invalid("invalid_service_to", "on or after the start date")
    visible = body.get("visible_to_travelers", False)
    if not isinstance(visible, bool):
        raise Invalid("invalid_visible_to_travelers", "true or false")
    try:
        async with ctx.conn.transaction():
            line_id = await ctx.conn.fetchval(
                INSERT_SQL,
                trip_id,
                supplier_id,
                _title(body.get("title")),
                _confirmation(body.get("confirmation_no")),
                start,
                end,
                _choice(body.get("collect", "agency"), COLLECT, "collect"),
                _choice(body.get("confirm_class"), CONFIRM_CLASSES, "confirm_class", optional=True),
                visible,
            )
    except asyncpg.PostgresError as exc:
        raise _db(exc) from None
    ctx.audit("create", "allowed", "booking_line", str(line_id))
    await stages.refresh_waiting_on(ctx, trip_id)
    return await line(ctx, line_id)


async def update_line(ctx: TenantContext, line_id: uuid.UUID, changes: dict[str, Any]) -> dict[str, Any]:
    current = await ctx.conn.fetchrow(TRIP_OF_LINE_SQL, line_id)
    if current is None:
        raise NotFound("line_not_found")
    sets: list[str] = []
    args: list[Any] = [line_id]
    for field in EDITABLE:
        if field not in changes:
            continue
        value = changes[field]
        if field == "title":
            value = _title(value)
        elif field == "confirmation_no":
            value = _confirmation(value)
            if value is None and current["status"] == "confirmed" and current["supplier_kind"] == "agency":
                # R18: the number the level asked for at confirm stays (crm-014 refuses it too, CRM23).
                if (await fx.settings(ctx))["booking_validation_level"] != "off":
                    raise Conflict(
                        "line_incomplete", "a confirmed line keeps its confirmation number", missing=["confirmation_no"]
                    )
        elif field in ("service_from", "service_to"):
            value = _date(value, field)
        elif field == "collect":
            value = _choice(value, COLLECT, field)
        elif field == "confirm_class":
            value = _choice(value, CONFIRM_CLASSES, field, optional=True)
        elif field == "visible_to_travelers":
            if not isinstance(value, bool):
                raise Invalid("invalid_visible_to_travelers", "true or false")
        else:  # agency_supplier_id: a planning line moves to another own supplier
            value = _uuid(value, field)
            await _own_supplier(ctx, value)
            if current["status"] != "planning":
                raise Invalid("line_not_editable", "the supplier of a confirmed line does not change; cancel it")
        args.append(value)
        sets.append(f"{field} = ${len(args)}")
    if sets:
        try:
            async with ctx.conn.transaction():
                await ctx.conn.execute(
                    f"UPDATE crm.booking_line SET {', '.join(sets)} WHERE booking_line_id = $1",  # noqa: S608
                    *args,
                )
        except asyncpg.CheckViolationError as exc:
            if exc.constraint_name == "booking_line_dates_chk":
                raise Invalid("invalid_service_to", "on or after the start date") from None
            raise _db(exc) from None
        except asyncpg.PostgresError as exc:
            raise _db(exc) from None
        ctx.audit("update", "allowed", "booking_line", str(line_id), fields=tuple(f for f in EDITABLE if f in changes))
    return await line(ctx, line_id)


async def _add_commission_task(ctx: TenantContext, trip_id: uuid.UUID) -> uuid.UUID | None:
    owner = await ctx.conn.fetchval(OWNER_SQL)
    return await ctx.conn.fetchval(TASK_SQL, trip_id, ADD_COMMISSION_TASK, owner)


async def confirm(ctx: TenantContext, line_id: uuid.UUID) -> dict[str, Any]:
    current = await ctx.conn.fetchrow(TRIP_OF_LINE_SQL, line_id)
    if current is None:
        raise NotFound("line_not_found")
    try:
        async with ctx.conn.transaction():
            raw = await ctx.conn.fetchval(CONFIRM_SQL, line_id)
    except asyncpg.PostgresError as exc:
        raise _db(exc) from None
    result = json.loads(raw) if isinstance(raw, str) else dict(raw)
    extra: dict[str, Any] = {"frozen": int(result["frozen"]), "stale_days": int(result["stale_days"])}
    pending = [p for p in result.get("pending", []) if p == "commission"]
    if pending and ctx.member_role == "assistant":
        # R18: never block the assistant; the owner adds the commission later.
        task_id = await _add_commission_task(ctx, current["trip_id"])
        extra["owner_task"] = {"kind": ADD_COMMISSION_TASK, "task_id": str(task_id) if task_id else None}
    ctx.audit("confirm", "allowed", "booking_line", str(line_id))
    await stages.refresh_waiting_on(ctx, current["trip_id"])
    return await line(ctx, line_id, extra=extra)


async def confirm_check(ctx: TenantContext, line_id: uuid.UUID) -> dict[str, Any]:
    """What a confirm would miss at the agency's level (codes only; works for the assistant too)."""
    try:
        async with ctx.conn.transaction():
            missing = await ctx.conn.fetchval(CHECK_SQL, line_id)
    except asyncpg.PostgresError as exc:
        raise _db(exc) from None
    blocking = [m for m in missing if not (m == "commission" and ctx.member_role == "assistant")]
    return {"booking_line_id": str(line_id), "missing": list(missing), "blocking": blocking}


async def cancel(ctx: TenantContext, line_id: uuid.UUID) -> dict[str, Any]:
    current = await ctx.conn.fetchrow(TRIP_OF_LINE_SQL, line_id)
    if current is None:
        raise NotFound("line_not_found")
    try:
        async with ctx.conn.transaction():
            done = await ctx.conn.fetchval(CANCEL_SQL, line_id)
    except asyncpg.PostgresError as exc:
        raise _db(exc) from None
    if done is None:
        raise Invalid("line_not_editable", "this line is cancelled already")
    ctx.audit("cancel", "allowed", "booking_line", str(line_id))
    await stages.refresh_waiting_on(ctx, current["trip_id"])
    return await line(ctx, line_id)


async def put_money(ctx: TenantContext, line_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
    """Set any of cost / sell / commission_expected ({amount, currency} each). Owner and agent only (the route's
    ``MONEY_FIELDS_EDIT``; the database re-checks the role)."""
    entries = {k: v for k, v in body.items() if k in KINDS and v is not None}
    if not entries:
        raise Invalid("invalid_money", "cost, sell or commission_expected, each {amount, currency}")
    if await ctx.conn.fetchrow(TRIP_OF_LINE_SQL, line_id) is None:
        raise NotFound("line_not_found")
    for kind, entry in entries.items():
        if not isinstance(entry, dict):
            raise Invalid(f"invalid_{kind}", "{amount, currency}")
        value, currency = amount(entry.get("amount")), fx.currency(entry.get("currency"))
        try:
            async with ctx.conn.transaction():
                await ctx.conn.fetchval(MONEY_PUT_SQL, line_id, kind, value, currency)
        except asyncpg.PostgresError as exc:
            raise _db(exc) from None
    ctx.audit("update", "allowed", "booking_line", str(line_id), fields=tuple(sorted(entries)))
    return await line(ctx, line_id)


# ── waiting_on (AA-943 hook contract) ────────────────────────────────────────────────────────────


async def supplier_lines_hook(ctx: AnyContext, facts: stages.TripFacts) -> str | None:
    """``supplier`` while the booked / pre-departure trip has a live agency line still planning; else no opinion."""
    return "supplier" if await ctx.conn.fetchval(OPEN_SUPPLIER_LINE_SQL, facts.trip_id) else None


stages.register_waiting_on_hook(50, "supplier_lines", supplier_lines_hook)


async def trash_check(ctx: TenantContext, line_id: uuid.UUID) -> None:
    """Before the trash: only a planning agency line (the database refuses the others too, CRM24 / CRM26)."""
    current = await ctx.conn.fetchrow(TRIP_OF_LINE_SQL, line_id)
    if current is None:
        raise NotFound("booking_line_not_found")
    if current["supplier_kind"] == "aa":
        raise Forbidden("aa_line_read_only", "Adventure Asia lines come from your AA bookings")
    if current["status"] != "planning":
        raise Conflict("line_not_editable", "only a planning line is deleted; cancel a confirmed one")
