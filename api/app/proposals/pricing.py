"""Retail pricing of the proposal builder (AA-1066, crm-048; root docs/research/2026-11-10-odoo-tour-travel-plugin-
comparison.md items crm-05 and crm-06). The discount and its owner approval (crm-07) are :mod:`app.proposals.discount`.

- **Age bands** (``crm.tenant_settings.child_age_max`` / ``infant_age_max``, default 11 / 1): a traveller up to the
  infant band is an infant, up to the child band a child, older an adult. They count the travellers of the retail
  price grid only; AA's Booking quote keeps AA's own rule (infants under 2, :func:`app.proposals.rules.room_pax`), so
  an infant is never a seat in the quote.
- **Price per traveller type** (``crm.option_price``): rows ``adult`` / ``child`` / ``infant`` / ``single_supp`` (the
  single room supplement) with a unit sell, a unit net (agency items only: an AA item's net is the quote's) and a
  quantity. With rows the option's sell is their sum (and an agency item's net the sum of the unit nets when every
  row has one). :func:`grid` suggests rows from the party and a total: the same unit for adults and children (the
  total split over them), infants and the supplement at 0 for the agent to set. AA's quote has no price per traveller
  type (booking.b2b.v1 quotes a line total), so the grid cannot start from an AA split today.
- **Retail price rules** (``crm.retail_price_rule``, owner-set in Settings > Pricing): season (the service date in a
  range), group size (that many travellers or more), early booking (that many days or more before the service date),
  last minute (that many days or fewer). A rule matches an AA option of its product scope (all, or one Booking
  component kind; optionally one AA tour). Every matching ``add`` rule adds its percent; of the matching
  ``best_only`` rules only the first by priority (then the lower percent) applies, on top of the add rules; the sum is
  never below -90 %. :func:`adjusted` applies it to the suggested sell of :mod:`app.proposals.retail` (the agency
  markup or AA's RRP), rounded up to the cent. The rules used are stored on the option (``option.price_rules``) when
  the agent takes the suggestion, so a later rename or delete does not change a sent proposal.

Every function takes an open context and never opens one.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.proposals import retail
from app.tenancy import AnyContext, TenantContext

PAX_TYPES = ("adult", "child", "infant", "single_supp")
RULE_KINDS = ("season", "group_size", "early_booking", "last_minute")
SCOPES = ("all", *retail.KINDS)
STACKING = ("add", "best_only")
MAX_RULES = 50
MAX_UNIT = Decimal("99999999.99")
MIN_TOTAL_PCT = Decimal("-90")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")


@dataclass(frozen=True)
class Settings:
    child_age_max: int = 11
    infant_age_max: int = 1
    discount_owner_limit_pct: Decimal = Decimal("10")


@dataclass(frozen=True)
class PriceRule:
    rule_id: str
    name: str
    kind: str
    product_scope: str
    aa_tour_source_id: str | None
    date_from: dt.date | None
    date_to: dt.date | None
    min_pax: int | None
    days_before: int | None
    adjust_pct: Decimal
    priority: int
    stacking: str
    active: bool


SETTINGS_SQL = "SELECT child_age_max, infant_age_max, discount_owner_limit_pct FROM crm.tenant_settings"
ENSURE_SETTINGS_SQL = "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING"
UPDATE_SETTINGS_SQL = """
UPDATE crm.tenant_settings SET child_age_max = $1, infant_age_max = $2, discount_owner_limit_pct = $3
"""
RULE_COLUMNS = (
    "retail_price_rule_id, name, kind, product_scope, aa_tour_source_id, date_from, date_to, min_pax, days_before, "
    "adjust_pct, priority, stacking, active"
)
RULES_SQL = f"""
SELECT {RULE_COLUMNS} FROM crm.retail_price_rule WHERE deleted_at IS NULL ORDER BY priority, created_at
"""  # noqa: S608 — fixed column list
RULE_SQL = f"SELECT {RULE_COLUMNS} FROM crm.retail_price_rule WHERE retail_price_rule_id = $1 AND deleted_at IS NULL"  # noqa: S608
RULE_COUNT_SQL = "SELECT count(*) FROM crm.retail_price_rule WHERE deleted_at IS NULL"
INSERT_RULE_SQL = """
INSERT INTO crm.retail_price_rule (name, kind, product_scope, aa_tour_source_id, date_from, date_to, min_pax,
                                   days_before, adjust_pct, priority, stacking, active)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
RETURNING retail_price_rule_id
"""
UPDATE_RULE_SQL = """
UPDATE crm.retail_price_rule SET name = $2, kind = $3, product_scope = $4, aa_tour_source_id = $5, date_from = $6,
       date_to = $7, min_pax = $8, days_before = $9, adjust_pct = $10, priority = $11, stacking = $12, active = $13
 WHERE retail_price_rule_id = $1
"""
DELETE_RULE_SQL = "DELETE FROM crm.retail_price_rule WHERE retail_price_rule_id = $1"
PRICES_SQL = """
SELECT option_id, pax_type, unit_sell, unit_net, qty FROM crm.option_price WHERE proposal_id = $1
"""
DELETE_PRICES_SQL = "DELETE FROM crm.option_price WHERE option_id = $1"
INSERT_PRICE_SQL = """
INSERT INTO crm.option_price (proposal_id, option_id, pax_type, unit_sell, unit_net, qty)
VALUES ($1, $2, $3, $4, $5, $6)
"""


# ── Settings ─────────────────────────────────────────────────────────────────────────────────────


async def settings(ctx: AnyContext) -> Settings:
    """The agency's bands and discount limit (the defaults while it has no settings row)."""
    row = await ctx.conn.fetchrow(SETTINGS_SQL)
    try:
        return Settings(int(row["child_age_max"]), int(row["infant_age_max"]), Decimal(row["discount_owner_limit_pct"]))
    except (TypeError, KeyError, IndexError, InvalidOperation):  # no settings row yet: the defaults
        return Settings()


def _rule_out(r: PriceRule) -> dict[str, Any]:
    return {
        "retail_price_rule_id": r.rule_id,
        "name": r.name,
        "kind": r.kind,
        "product_scope": r.product_scope,
        "aa_tour_source_id": r.aa_tour_source_id,
        "date_from": r.date_from.isoformat() if r.date_from else None,
        "date_to": r.date_to.isoformat() if r.date_to else None,
        "min_pax": r.min_pax,
        "days_before": r.days_before,
        "adjust_pct": format(r.adjust_pct.normalize(), "f"),
        "priority": r.priority,
        "stacking": r.stacking,
        "active": r.active,
    }


def _rule(row: Mapping[str, Any]) -> PriceRule:
    return PriceRule(
        str(row["retail_price_rule_id"]), row["name"], row["kind"], row["product_scope"], row["aa_tour_source_id"],
        row["date_from"], row["date_to"], row["min_pax"], row["days_before"], Decimal(row["adjust_pct"]),
        int(row["priority"]), row["stacking"], bool(row["active"]),
    )  # fmt: skip


async def rules_of(ctx: AnyContext) -> list[PriceRule]:
    return [_rule(r) for r in await ctx.conn.fetch(RULES_SQL)]


async def read(ctx: AnyContext) -> dict[str, Any]:
    """Settings > Pricing: the bands, the discount limit and every rule (owner only, checked by the route)."""
    s = await settings(ctx)
    return {
        "child_age_max": s.child_age_max,
        "infant_age_max": s.infant_age_max,
        "discount_owner_limit_pct": format(s.discount_owner_limit_pct.normalize(), "f"),
        "rules": [_rule_out(r) for r in await rules_of(ctx)],
        "kinds": list(RULE_KINDS),
        "scopes": list(SCOPES),
        "max_rules": MAX_RULES,
    }


def _int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Invalid(f"invalid_{name}", f"a whole number from {low} to {high}")
    return value


def _pct(value: Any, name: str, low: Decimal, high: Decimal) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Invalid(f"invalid_{name}", f"a percentage from {low} to {high}, two decimals at most") from None
    if not number.is_finite() or not low <= number <= high or number != number.quantize(CENT):
        raise Invalid(f"invalid_{name}", f"a percentage from {low} to {high}, two decimals at most")
    return number


async def update_settings(ctx: TenantContext, changes: dict[str, Any]) -> dict[str, Any]:
    """The bands and the discount limit (only the keys given change)."""
    current = await settings(ctx)
    child = _int(changes.get("child_age_max", current.child_age_max), "child_age_max", 1, 17)
    infant = _int(changes.get("infant_age_max", current.infant_age_max), "infant_age_max", 0, 5)
    if infant >= child:
        raise Invalid("invalid_age_bands", "the infant band ends below the child band")
    limit = _pct(changes.get("discount_owner_limit_pct", current.discount_owner_limit_pct), "discount_limit",
                 Decimal("0"), HUNDRED)  # fmt: skip
    await ctx.conn.execute(ENSURE_SETTINGS_SQL)
    await ctx.conn.execute(UPDATE_SETTINGS_SQL, child, infant, limit)
    named = ("child_age_max", "infant_age_max", "discount_owner_limit_pct")
    ctx.audit("update", "allowed", "tenant_settings", None, fields=tuple(k for k in named if k in changes))
    return await read(ctx)


def _date(value: Any, name: str) -> dt.date | None:
    if value is None or isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise Invalid(f"invalid_{name}", "a date (YYYY-MM-DD)") from None


def check_rule(body: Mapping[str, Any]) -> dict[str, Any]:
    """A rule as stored, or 422: each kind carries exactly its own condition (the crm-048 CHECK, said first)."""
    name = body.get("name")
    if not isinstance(name, str) or not 1 <= len(" ".join(name.split())) <= 80:
        raise Invalid("invalid_rule_name", "1 to 80 characters")
    kind = body.get("kind")
    if kind not in RULE_KINDS:
        raise Invalid("invalid_rule_kind", ", ".join(RULE_KINDS))
    scope = body.get("product_scope") or "all"
    if scope not in SCOPES:
        raise Invalid("invalid_product_scope", ", ".join(SCOPES))
    tour = body.get("aa_tour_source_id") or None
    if tour is not None and (not isinstance(tour, str) or len(tour) > 128 or not tour.isprintable()):
        raise Invalid("invalid_tour_source_id")
    out: dict[str, Any] = {
        "name": " ".join(name.split()), "kind": kind, "product_scope": scope, "aa_tour_source_id": tour,
        "date_from": None, "date_to": None, "min_pax": None, "days_before": None,
        "adjust_pct": _pct(body.get("adjust_pct"), "adjust_pct", Decimal("-90"), Decimal("300")),
        "priority": _int(body.get("priority", 100), "priority", 1, 999),
        "stacking": body.get("stacking") or "add", "active": bool(body.get("active", True)),
    }  # fmt: skip
    if out["adjust_pct"] == 0:
        raise Invalid("invalid_adjust_pct", "not 0: a rise above 0, a reduction below")
    if out["stacking"] not in STACKING:
        raise Invalid("invalid_stacking", "add or best_only")
    if kind == "season":
        out["date_from"] = _date(body.get("date_from"), "date_from")
        out["date_to"] = _date(body.get("date_to"), "date_to")
        if out["date_from"] is None or out["date_to"] is None or out["date_from"] > out["date_to"]:
            raise Invalid("invalid_season", "a start date on or before the end date")
        if (out["date_to"] - out["date_from"]).days > 366:
            raise Invalid("invalid_season", "at most one year")
    elif kind == "group_size":
        out["min_pax"] = _int(body.get("min_pax"), "min_pax", 2, 99)
    elif kind == "early_booking":
        out["days_before"] = _int(body.get("days_before"), "days_before", 1, 730)
    else:
        out["days_before"] = _int(body.get("days_before"), "days_before", 0, 365)
    return out


def _rule_args(r: Mapping[str, Any]) -> tuple[Any, ...]:
    return (r["name"], r["kind"], r["product_scope"], r["aa_tour_source_id"], r["date_from"], r["date_to"],
            r["min_pax"], r["days_before"], r["adjust_pct"], r["priority"], r["stacking"], r["active"])  # fmt: skip


async def create_rule(ctx: TenantContext, body: Mapping[str, Any]) -> dict[str, Any]:
    rule = check_rule(body)
    if int(await ctx.conn.fetchval(RULE_COUNT_SQL) or 0) >= MAX_RULES:
        raise Conflict("too_many_price_rules", f"at most {MAX_RULES} rules")
    await ctx.conn.execute(ENSURE_SETTINGS_SQL)  # a rule hangs on the agency's settings row (crm-048 key)
    try:
        async with ctx.conn.transaction():
            rule_id = await ctx.conn.fetchval(INSERT_RULE_SQL, *_rule_args(rule))
    except asyncpg.CheckViolationError:
        raise Invalid("invalid_price_rule") from None
    ctx.audit("create", "allowed", "retail_price_rule", str(rule_id))
    return await read(ctx)


async def update_rule(ctx: TenantContext, rule_id: uuid.UUID, changes: Mapping[str, Any]) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(RULE_SQL, rule_id)
    if row is None:
        raise NotFound("price_rule_not_found")
    merged = {**_rule_out(_rule(row)), **dict(changes)}
    # A kind change drops the old kind's condition (check_rule reads only the new kind's fields).
    rule = check_rule(merged)
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(UPDATE_RULE_SQL, rule_id, *_rule_args(rule))
    except asyncpg.CheckViolationError:
        raise Invalid("invalid_price_rule") from None
    ctx.audit("update", "allowed", "retail_price_rule", str(rule_id), fields=tuple(sorted(changes)))
    return await read(ctx)


async def delete_rule(ctx: TenantContext, rule_id: uuid.UUID) -> dict[str, Any]:
    done = await ctx.conn.execute(DELETE_RULE_SQL, rule_id)
    if str(done).endswith(" 0"):
        raise NotFound("price_rule_not_found")
    ctx.audit("delete", "allowed", "retail_price_rule", str(rule_id))
    return await read(ctx)


# ── Rules on an option ───────────────────────────────────────────────────────────────────────────


def matches(
    rule: PriceRule,
    *,
    kind: str | None,
    tour_source_id: str | None,
    service_date: dt.date | None,
    pax: int,
    today: dt.date,
) -> bool:
    """Whether a rule applies to an AA option of this kind and tour, on this service date, for this party."""
    if not rule.active or kind is None:
        return False
    if rule.product_scope != "all" and rule.product_scope != kind:
        return False
    if rule.aa_tour_source_id is not None and rule.aa_tour_source_id != tour_source_id:
        return False
    if rule.kind == "group_size":
        return rule.min_pax is not None and pax >= rule.min_pax
    if service_date is None:
        return False  # season and booking-window rules need the service date (day 1 date set)
    if rule.kind == "season":
        return (
            rule.date_from is not None and rule.date_to is not None and (rule.date_from <= service_date <= rule.date_to)
        )
    days = (service_date - today).days
    if rule.kind == "early_booking":
        return rule.days_before is not None and days >= rule.days_before
    return rule.days_before is not None and 0 <= days <= rule.days_before  # last_minute


def adjustment(rules: Iterable[PriceRule], **facts: Any) -> tuple[Decimal, list[dict[str, str]]]:
    """``(total percent, the rules used)``: every matching ``add`` rule, plus the first matching ``best_only`` rule
    (priority, then the lower percent); the total never below -90 %."""
    matched = sorted((r for r in rules if matches(r, **facts)), key=lambda r: (r.priority, r.adjust_pct, r.rule_id))
    best = next((r for r in matched if r.stacking == "best_only"), None)
    used = [r for r in matched if r.stacking == "add" or r is best]
    total = max(MIN_TOTAL_PCT, sum((r.adjust_pct for r in used), Decimal("0")))
    return total, [
        {"rule_id": r.rule_id, "name": r.name, "adjust_pct": format(r.adjust_pct.normalize(), "f")} for r in used
    ]


def adjusted(amount: Decimal, pct: Decimal) -> Decimal:
    """``amount`` with ``pct`` applied, rounded up to the cent (never below 0)."""
    if pct == 0:
        return amount
    return max(Decimal("0.00"), (amount * (1 + pct / HUNDRED)).quantize(CENT, rounding=ROUND_CEILING))


def today_in(tz: str | None) -> dt.date:
    try:
        return dt.datetime.now(ZoneInfo(tz or "UTC")).date()
    except (ZoneInfoNotFoundError, ValueError):
        return dt.datetime.now(dt.UTC).date()


def parse_applied(raw: Any) -> list[dict[str, str]] | None:
    """``option.price_rules`` as stored (a JSON array), or None."""
    if raw is None:
        return None
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, list):
        return None
    return [
        {
            "rule_id": str(x.get("rule_id", "")),
            "name": str(x.get("name", "")),
            "adjust_pct": str(x.get("adjust_pct", "")),
        }  # fmt: skip
        for x in data
        if isinstance(x, Mapping)
    ]


# ── Party counts and the per-traveller grid ──────────────────────────────────────────────────────


def party_counts(
    rooms: Sequence[Mapping[str, Any]] | None, adults: int, child_ages: Sequence[int], bands: Settings
) -> dict[str, int]:
    """The travellers by retail type with the agency's bands: a child older than the child band is an adult; the
    single room supplement counts the single rooms."""
    infants = sum(1 for a in child_ages if a <= bands.infant_age_max)
    children = sum(1 for a in child_ages if bands.infant_age_max < a <= bands.child_age_max)
    older = sum(1 for a in child_ages if a > bands.child_age_max)
    if rooms:
        adults = sum(int(r.get("adults", 0)) for r in rooms) or adults
    singles = sum(1 for r in rooms or () if r.get("type") == "single")
    return {"adult": max(1, int(adults)) + older, "child": children, "infant": infants, "single_supp": singles}


def grid(total: Decimal, counts: Mapping[str, int]) -> list[dict[str, Any]]:
    """Starting rows for a total: the adults and children share it per head (rounded up), infants and the single
    supplement start at 0. Types with nobody are left out."""
    heads = counts.get("adult", 0) + counts.get("child", 0)
    unit = (total / heads).quantize(CENT, rounding=ROUND_CEILING) if heads and total > 0 else Decimal("0.00")
    rows = []
    for pax in PAX_TYPES:
        qty = int(counts.get(pax, 0))
        if qty <= 0:
            continue
        rows.append({"pax_type": pax, "unit_sell": unit if pax in ("adult", "child") else Decimal("0.00"),
                     "unit_net": None, "qty": qty})  # fmt: skip
    return rows


def _money(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Invalid(f"invalid_{name}", "zero or more, two decimals at most") from None
    if not amount.is_finite() or amount < 0 or amount > MAX_UNIT or amount != amount.quantize(CENT):
        raise Invalid(f"invalid_{name}", "zero or more, two decimals at most")
    return amount


def check_rows(rows: Any, *, aa: bool) -> list[dict[str, Any]]:
    """Clean price rows or 422: one row per known type, money with two decimals, quantity 0-99, something charged;
    an AA item never takes a unit net (403 ``aa_net_from_quote``); unit nets on every row or on none."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= len(PAX_TYPES):
        raise Invalid("invalid_prices", f"1 to {len(PAX_TYPES)} rows, one per traveller type")
    out = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or row.get("pax_type") not in PAX_TYPES or row["pax_type"] in seen:
            raise Invalid("invalid_prices", "one row per traveller type: " + ", ".join(PAX_TYPES))
        seen.add(row["pax_type"])
        net = _money(row.get("unit_net"), "unit_net")
        if aa and net is not None:
            raise Forbidden("aa_net_from_quote", "an AA item's net price comes from the live quote")
        sell = _money(row.get("unit_sell"), "unit_sell")
        if sell is None:
            raise Invalid("invalid_unit_sell", "zero or more, two decimals at most")
        out.append({"pax_type": row["pax_type"], "unit_sell": sell, "unit_net": net,
                    "qty": _int(row.get("qty"), "qty", 0, 99)})  # fmt: skip
    if len({r["unit_net"] is None for r in out}) > 1:
        raise Invalid("unit_net_partial", "a unit net on every row, or on none")
    if sum(r["unit_sell"] * r["qty"] for r in out) <= 0:
        raise Invalid("prices_nothing_charged", "at least one traveller type with a price and a quantity")
    out.sort(key=lambda r: PAX_TYPES.index(r["pax_type"]))
    return out


def sums(rows: Sequence[Mapping[str, Any]]) -> tuple[Decimal, Decimal | None]:
    """``(sell, net)`` of price rows (net None when the rows carry none)."""
    sell = sum((Decimal(r["unit_sell"]) * int(r["qty"]) for r in rows), Decimal("0"))
    net: Decimal | None = None
    if rows and all(r["unit_net"] is not None for r in rows):
        net = sum((Decimal(r["unit_net"]) * int(r["qty"]) for r in rows), Decimal("0"))
    return sell.quantize(CENT), net.quantize(CENT) if net is not None else None


async def prices_of(ctx: AnyContext, proposal_id: uuid.UUID) -> dict[str, list[dict[str, Any]]]:
    """Every option's rows of a version, by option id, in type order."""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in await ctx.conn.fetch(PRICES_SQL, proposal_id):
        out.setdefault(str(r["option_id"]), []).append(dict(r))
    for rows in out.values():
        rows.sort(key=lambda r: PAX_TYPES.index(r["pax_type"]) if r["pax_type"] in PAX_TYPES else 99)
    return out


async def replace(
    ctx: TenantContext, proposal_id: uuid.UUID, option_id: uuid.UUID, rows: Sequence[Mapping[str, Any]]
) -> None:
    await ctx.conn.execute(DELETE_PRICES_SQL, option_id)
    for r in rows:
        await ctx.conn.execute(INSERT_PRICE_SQL, proposal_id, option_id, r["pax_type"], r["unit_sell"], r["unit_net"],
                               r["qty"])  # fmt: skip


def rows_out(rows: Sequence[Mapping[str, Any]], *, sees_margin: bool) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        item: dict[str, Any] = {"pax_type": r["pax_type"], "unit_sell": str(Decimal(r["unit_sell"]).quantize(CENT)),
                                "qty": int(r["qty"])}  # fmt: skip
        if sees_margin:
            item["unit_net"] = str(Decimal(r["unit_net"]).quantize(CENT)) if r.get("unit_net") is not None else None
        out.append(item)
    return out
