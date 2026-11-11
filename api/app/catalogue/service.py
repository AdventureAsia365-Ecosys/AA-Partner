"""The agency service catalogue (AA-1069, crm-052; S275 study item crm-23; root ADR 0019 b6).

Rules the database holds (crm-052, guard ``crm.guard_agency_service``, SQLSTATE CRM52) and this module words:

- **Categories** are the b6 ancillaries only: flight, insurance, visa, transfer, fee. A service names a live own
  supplier of the agency, or none (the agency's own fee); never the Adventure Asia row (409 ``catalogue_supplier``).
- **Money**: ``unit_sell`` per unit, ``unit_net`` per unit (NULL = not known), tiers (``min_pax`` .. ``max_pax``,
  ``unit_net``) for group-size net prices. Writes are owner / agent only (the route's ``MONEY_FIELDS_EDIT``, and
  CRM52 in the database). Reads are every role; ``unit_net`` and the tiers' net are built only for a role that sees
  margins (the margin serializer strips ``*_net`` as a backstop).
- **Quantity basis** (:func:`quantity`): per_person = every traveller, per_adult = adults, per_booking = 1,
  per_room = the proposal's rooms (1 without a room list), per_night = nights, per_person_night = travellers x nights.
- **Add from catalogue** (:func:`plan_option`): the option the builder writes is a snapshot (title, supplier, sell =
  unit sell x quantity, net = unit net (the tier of the party size) x quantity), converted to the proposal currency at
  today's rate when the service is priced in another one (409 ``fx_rate_missing`` without a rate). No key from the
  option back to the service: editing or deleting a service never changes a proposal.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.ledger import fx
from app.margin import sees_margin
from app.proposals import rules
from app.tenancy import TenantContext

TABLE = "agency_service"
CATEGORIES = ("flight", "insurance", "visa", "transfer", "fee")
QTY_BASES = ("per_person", "per_adult", "per_booking", "per_room", "per_night", "per_person_night")
MAX_UNIT = Decimal("99999999.99")
MAX_OPTION = Decimal("999999999999.99")
MAX_TIERS = 10
MAX_QUANTITY = 9999
MAX_LIST = 500
CENT = Decimal("0.01")

_COLUMNS = """
s.service_id, s.agency_supplier_id, p.name AS supplier_name, s.category, s.name, s.unit_net, s.unit_sell, s.currency,
s.qty_basis, s.valid_from, s.valid_to, s.active, s.created_at, s.updated_at
"""
LIST_SQL = f"""
SELECT {_COLUMNS}
  FROM crm.agency_service s LEFT JOIN crm.agency_supplier p ON p.agency_supplier_id = s.agency_supplier_id
 WHERE s.deleted_at IS NULL AND ($1::text IS NULL OR s.category = $1) AND ($2::boolean IS NULL OR s.active = $2)
 ORDER BY s.category, lower(s.name), s.service_id
 LIMIT {MAX_LIST}
"""  # noqa: S608 — fixed text
ONE_SQL = f"""
SELECT {_COLUMNS}
  FROM crm.agency_service s LEFT JOIN crm.agency_supplier p ON p.agency_supplier_id = s.agency_supplier_id
 WHERE s.deleted_at IS NULL AND s.service_id = $1
"""  # noqa: S608 — fixed text
TIERS_SQL = """
SELECT tier_id, service_id, min_pax, max_pax, unit_net FROM crm.agency_service_tier
 WHERE service_id = ANY($1::uuid[]) AND deleted_at IS NULL
 ORDER BY service_id, min_pax
"""
INSERT_SQL = """
INSERT INTO crm.agency_service (agency_supplier_id, category, name, unit_net, unit_sell, currency, qty_basis,
                                valid_from, valid_to, active)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
RETURNING service_id
"""
DELETE_SQL = "DELETE FROM crm.agency_service WHERE service_id = $1 RETURNING service_id"
DELETE_TIERS_SQL = "DELETE FROM crm.agency_service_tier WHERE service_id = $1"
INSERT_TIER_SQL = """
INSERT INTO crm.agency_service_tier (service_id, min_pax, max_pax, unit_net) VALUES ($1, $2, $3, $4)
"""
SUPPLIER_SQL = "SELECT kind, deleted_at FROM crm.agency_supplier WHERE agency_supplier_id = $1"
# A service hangs off the agency's settings row (crm-052 FK, so the agency erase takes it): make sure it exists.
ENSURE_SETTINGS_SQL = "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING"
EDITABLE = ("agency_supplier_id", "category", "name", "unit_net", "unit_sell", "currency", "qty_basis", "valid_from",
            "valid_to", "active")  # fmt: skip


# ── validation ───────────────────────────────────────────────────────────────────────────────────


def _name(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120 or any(ord(c) < 32 for c in value):
        raise Invalid("invalid_name", "1 to 120 characters")
    return value.strip()


def _choice(value: Any, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise Invalid(f"invalid_{field}", ", ".join(allowed))
    return str(value)


def unit(value: Any, field: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Invalid(f"invalid_{field}", "a number from 0, two decimals") from None
    if not number.is_finite() or number < 0 or number > MAX_UNIT:
        raise Invalid(f"invalid_{field}", "a number from 0, two decimals")
    return number.quantize(CENT)


def _date(value: Any, field: str) -> dt.date | None:
    if value is None or isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise Invalid(f"invalid_{field}", "a date, YYYY-MM-DD") from None


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError:
        raise Invalid(f"invalid_{field}", "an id") from None


def _refusal(exc: asyncpg.PostgresError) -> Exception:
    if getattr(exc, "sqlstate", "") == "CRM52":
        text = str(exc)
        if "catalogue_owner_agent" in text:
            return Forbidden("role_forbidden", "only an owner or an agent edits the service catalogue")
        if "catalogue_tier_overlap" in text:
            return Conflict("tier_overlap", "the group sizes of two tiers overlap")
        return Invalid("invalid_agency_supplier_id", "one of the agency's own suppliers, or none")
    if isinstance(exc, asyncpg.CheckViolationError):
        return Invalid("invalid_service", "check the values (name, amounts, dates)")
    if isinstance(exc, asyncpg.ForeignKeyViolationError):
        return Invalid("invalid_agency_supplier_id", "one of the agency's own suppliers, or none")
    return exc


def tiers_in(value: Any) -> list[tuple[int, int | None, Decimal]]:
    """A clean, sorted copy of a tier list ([{min_pax, max_pax, unit_net}]) or 400 ``invalid_tiers``."""
    if not isinstance(value, list) or len(value) > MAX_TIERS:
        raise Invalid("invalid_tiers", f"at most {MAX_TIERS} tiers")
    out: list[tuple[int, int | None, Decimal]] = []
    for item in value:
        if not isinstance(item, dict):
            raise Invalid("invalid_tiers", "{min_pax, max_pax, unit_net}")
        lo, hi = item.get("min_pax"), item.get("max_pax")
        if not isinstance(lo, int) or isinstance(lo, bool) or not 1 <= lo <= 999:
            raise Invalid("invalid_tiers", "min_pax 1 to 999")
        if hi is not None and (not isinstance(hi, int) or isinstance(hi, bool) or not lo <= hi <= 999):
            raise Invalid("invalid_tiers", "max_pax from min_pax to 999, or empty")
        net = unit(item.get("unit_net"), "tiers")
        assert net is not None  # noqa: S101 — not optional
        out.append((lo, hi, net))
    out.sort(key=lambda t: t[0])
    for (_lo_a, hi_a, _), (lo_b, _, _) in zip(out, out[1:], strict=False):
        if hi_a is None or hi_a >= lo_b:
            raise Conflict("tier_overlap", "the group sizes of two tiers overlap")
    return out


# ── views ────────────────────────────────────────────────────────────────────────────────────────


def _view(row: Any, tiers: list[Any], margin: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "service_id": str(row["service_id"]),
        "agency_supplier_id": str(row["agency_supplier_id"]) if row["agency_supplier_id"] else None,
        "supplier_name": row["supplier_name"],
        "category": row["category"],
        "name": row["name"],
        "unit_sell": str(row["unit_sell"]),
        "currency": row["currency"].strip(),
        "qty_basis": row["qty_basis"],
        "valid_from": row["valid_from"].isoformat() if row["valid_from"] else None,
        "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
        "active": row["active"],
        "tier_count": len(tiers),
        "updated_at": row["updated_at"].isoformat(),
    }
    if margin:  # never built for a role without view_margin (the serializer strips *_net as a backstop)
        out["unit_net"] = str(row["unit_net"]) if row["unit_net"] is not None else None
        out["tiers"] = [
            {"min_pax": t["min_pax"], "max_pax": t["max_pax"], "unit_net": str(t["unit_net"])} for t in tiers
        ]
    return out


async def _tiers_by_service(ctx: TenantContext, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Any]]:
    grouped: dict[uuid.UUID, list[Any]] = {}
    if ids:
        for r in await ctx.conn.fetch(TIERS_SQL, ids):
            grouped.setdefault(r["service_id"], []).append(r)
    return grouped


# ── reads ────────────────────────────────────────────────────────────────────────────────────────


async def list_services(ctx: TenantContext, *, category: str | None, active: bool | None) -> dict[str, Any]:
    if category is not None:
        _choice(category, CATEGORIES, "category")
    rows = await ctx.conn.fetch(LIST_SQL, category, active)
    tiers = await _tiers_by_service(ctx, [r["service_id"] for r in rows])
    margin = sees_margin(ctx.member_role)
    items = [_view(r, tiers.get(r["service_id"], []), margin) for r in rows]
    ctx.audit("list_view", "allowed", TABLE, None, object_ids=tuple(i["service_id"] for i in items))
    return {
        "services": items,
        "categories": list(CATEGORIES),
        "qty_bases": list(QTY_BASES),
        "truncated": len(rows) == MAX_LIST,
    }


async def get_service(ctx: TenantContext, service_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ONE_SQL, service_id)
    if row is None:
        raise NotFound("service_not_found")
    tiers = await _tiers_by_service(ctx, [service_id])
    return _view(row, tiers.get(service_id, []), sees_margin(ctx.member_role))


# ── writes (owner / agent: the route's MONEY_FIELDS_EDIT; CRM52 in the database) ───────────────────


async def _supplier(ctx: TenantContext, value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    supplier_id = _uuid(value, "agency_supplier_id")
    row = await ctx.conn.fetchrow(SUPPLIER_SQL, supplier_id)
    if row is None or row["deleted_at"] is not None or row["kind"] != "own":
        raise Invalid("invalid_agency_supplier_id", "one of the agency's own suppliers, or none")
    return supplier_id


def _window(start: dt.date | None, end: dt.date | None) -> None:
    if start and end and end < start:
        raise Invalid("invalid_valid_to", "on or after the start date")


async def _write_tiers(ctx: TenantContext, service_id: uuid.UUID, tiers: list[tuple[int, int | None, Decimal]]) -> None:
    await ctx.conn.execute(DELETE_TIERS_SQL, service_id)
    for lo, hi, net in tiers:
        await ctx.conn.execute(INSERT_TIER_SQL, service_id, lo, hi, net)


async def create_service(ctx: TenantContext, body: dict[str, Any]) -> dict[str, Any]:
    start, end = _date(body.get("valid_from"), "valid_from"), _date(body.get("valid_to"), "valid_to")
    _window(start, end)
    active = body.get("active", True)
    if not isinstance(active, bool):
        raise Invalid("invalid_active", "true or false")
    tiers = tiers_in(body.get("tiers") or [])
    values = (
        await _supplier(ctx, body.get("agency_supplier_id")),
        _choice(body.get("category"), CATEGORIES, "category"),
        _name(body.get("name")),
        unit(body.get("unit_net"), "unit_net", optional=True),
        unit(body.get("unit_sell"), "unit_sell"),
        fx.currency(body.get("currency")),
        _choice(body.get("qty_basis"), QTY_BASES, "qty_basis"),
        start,
        end,
        active,
    )
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(ENSURE_SETTINGS_SQL)
            service_id = await ctx.conn.fetchval(INSERT_SQL, *values)
            await _write_tiers(ctx, service_id, tiers)
    except asyncpg.PostgresError as exc:
        raise _refusal(exc) from None
    ctx.audit("create", "allowed", TABLE, str(service_id))
    return await get_service(ctx, service_id)


async def update_service(ctx: TenantContext, service_id: uuid.UUID, changes: dict[str, Any]) -> dict[str, Any]:
    current = await ctx.conn.fetchrow(ONE_SQL, service_id)
    if current is None:
        raise NotFound("service_not_found")
    sets: list[str] = []
    args: list[Any] = [service_id]
    for field in EDITABLE:
        if field not in changes:
            continue
        value = changes[field]
        if field == "agency_supplier_id":
            value = await _supplier(ctx, value)
        elif field == "category":
            value = _choice(value, CATEGORIES, field)
        elif field == "name":
            value = _name(value)
        elif field == "unit_net":
            value = unit(value, field, optional=True)
        elif field == "unit_sell":
            value = unit(value, field)
        elif field == "currency":
            value = fx.currency(value)
        elif field == "qty_basis":
            value = _choice(value, QTY_BASES, field)
        elif field in ("valid_from", "valid_to"):
            value = _date(value, field)
        elif not isinstance(value, bool):  # active
            raise Invalid("invalid_active", "true or false")
        args.append(value)
        sets.append(f"{field} = ${len(args)}")
    _window(
        _date(changes["valid_from"], "valid_from") if "valid_from" in changes else current["valid_from"],
        _date(changes["valid_to"], "valid_to") if "valid_to" in changes else current["valid_to"],
    )
    tiers = tiers_in(changes["tiers"]) if changes.get("tiers") is not None else None
    try:
        async with ctx.conn.transaction():
            if sets:
                await ctx.conn.execute(
                    f"UPDATE crm.agency_service SET {', '.join(sets)} WHERE service_id = $1",  # noqa: S608
                    *args,
                )
            if tiers is not None:
                await _write_tiers(ctx, service_id, tiers)
    except asyncpg.PostgresError as exc:
        raise _refusal(exc) from None
    fields = tuple(f for f in (*EDITABLE, "tiers") if f in changes)
    ctx.audit("update", "allowed", TABLE, str(service_id), fields=fields)
    return await get_service(ctx, service_id)


async def delete_service(ctx: TenantContext, service_id: uuid.UUID) -> dict[str, Any]:
    """A hard delete: options added from it are snapshots and keep their values."""
    try:
        async with ctx.conn.transaction():
            done = await ctx.conn.fetchval(DELETE_SQL, service_id)
    except asyncpg.PostgresError as exc:
        raise _refusal(exc) from None
    if done is None:
        raise NotFound("service_not_found")
    ctx.audit("delete", "allowed", TABLE, str(service_id))
    return {"service_id": str(service_id), "status": "deleted"}


# ── Add from catalogue ───────────────────────────────────────────────────────────────────────────


def party(rooms: Any, adults: int, child_ages: list[int] | None) -> dict[str, int]:
    """Travellers, adults and rooms of a proposal: the room list when it has one, else the trip party (as an AA
    quote counts them, :func:`app.proposals.rules.room_pax`)."""
    counts = rules.room_pax(rooms or None, int(adults or 0), list(child_ages or []))
    room_children = sum(int(r.get("children", 0)) for r in rooms or [])
    children = room_children if rooms else counts["child"] + counts["infant"]
    travellers = counts["adult"] + children
    return {"travellers": max(1, travellers), "adults": counts["adult"], "rooms": max(1, len(rooms or []))}


def quantity(basis: str, counts: dict[str, int], nights: int) -> int:
    """How many units of a service a party takes (the qty basis)."""
    nights = max(1, int(nights))
    by_basis = {
        "per_person": counts["travellers"],
        "per_adult": counts["adults"],
        "per_booking": 1,
        "per_room": counts["rooms"],
        "per_night": nights,
        "per_person_night": counts["travellers"] * nights,
    }
    return int(by_basis[_choice(basis, QTY_BASES, "qty_basis")])


def tier_net(base_net: Decimal | None, tiers: list[Any], travellers: int) -> Decimal | None:
    """The unit net for the party size: the tier whose range holds it, else the service's own unit net."""
    for t in tiers:
        if t["min_pax"] <= travellers and (t["max_pax"] is None or travellers <= t["max_pax"]):
            return Decimal(t["unit_net"])
    return base_net


def option_title(name: str, qty: int) -> str:
    return name if qty == 1 else f"{name} x {qty}"


async def plan_option(
    ctx: TenantContext,
    service_id: uuid.UUID,
    *,
    proposal: Any,
    service_date: dt.date | None,
    days: int,
    quantity_override: int | None,
    nights_override: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The agency option body for ``builder.add_agency_option`` and the summary of how it was priced (returned with
    the builder view). ``proposal``: the builder row (currency, room_config, party); ``service_date``: the day's date
    (None = the proposal has no day 1 date: today counts for the validity window)."""
    row = await ctx.conn.fetchrow(ONE_SQL, service_id)
    if row is None:
        raise NotFound("service_not_found")
    if not row["active"]:
        raise Conflict("service_inactive", "this service is switched off in the catalogue")
    day = service_date or await fx.agency_today(ctx)
    if (row["valid_from"] and day < row["valid_from"]) or (row["valid_to"] and day > row["valid_to"]):
        raise Conflict(
            "service_not_valid",
            "this service is not offered on that date",
            valid_from=row["valid_from"].isoformat() if row["valid_from"] else None,
            valid_to=row["valid_to"].isoformat() if row["valid_to"] else None,
        )
    counts = party(proposal["room_config"], proposal["party_adults"], proposal["party_child_ages"])
    nights = nights_override or proposal.get("trip_nights") or max(1, days - 1)
    if quantity_override is not None:
        if not 1 <= quantity_override <= MAX_QUANTITY:
            raise Invalid("invalid_quantity", f"1 to {MAX_QUANTITY}")
        qty = quantity_override
    else:
        qty = quantity(row["qty_basis"], counts, nights)
    tiers = (await _tiers_by_service(ctx, [service_id])).get(service_id, [])
    net_unit = tier_net(row["unit_net"], tiers, counts["travellers"])
    currency, target = row["currency"].strip(), (proposal["currency"] or "").strip()
    rate = Decimal(1)
    fx_view: dict[str, Any] | None = None
    if target and currency != target:
        today = await fx.agency_today(ctx)
        found = await fx.resolve(ctx, currency, target, today)
        if found is None:
            raise Conflict("fx_rate_missing", "no exchange rate for this currency in the last 30 days",
                           currency=currency)  # fmt: skip
        rate, fx_view = found.rate, found.view()
    sell = (Decimal(row["unit_sell"]) * qty * rate).quantize(CENT)
    net = (net_unit * qty * rate).quantize(CENT) if net_unit is not None else None
    if sell > MAX_OPTION or (net is not None and net > MAX_OPTION):
        raise Invalid("invalid_quantity", "the amount is too large")
    body = {
        "title": option_title(row["name"], qty),
        "agency_supplier_id": row["agency_supplier_id"],
        "sell_amount": sell,
        "net_amount": net,
    }
    summary: dict[str, Any] = {
        "service_id": str(service_id),
        "name": row["name"],
        "qty_basis": row["qty_basis"],
        "quantity": qty,
        "travellers": counts["travellers"],
        "adults": counts["adults"],
        "rooms": counts["rooms"],
        "nights": int(nights),
        "unit_sell": str(row["unit_sell"]),
        "currency": currency,
        "sell_amount": str(sell),
        "fx": fx_view,
    }
    if sees_margin(ctx.member_role):
        summary["unit_net"] = str(net_unit) if net_unit is not None else None
        summary["net_amount"] = str(net) if net is not None else None
    return body, summary
