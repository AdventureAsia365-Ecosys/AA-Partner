"""The agency's default retail markup and the suggested sell price of an AA option (AA-1030, crm-046; root ADR 0019
b15, ADR 0005a decision 1).

AA quotes the agency its B2B price (``option.net_amount``; Booking takes the agency's tier or contract rate card off
it) and publishes a suggested retail price (RRP = the agency price + 25 %, the public "from" price per person,
``option.aa_from_price``). The agency sells at its own price; the builder suggests one:

1. **The agency's own markup** (``crm.tenant_settings.retail_markup_pct``, per AA product type, set by the owner in
   Settings): ``sell = B2B price x (1 + markup / 100)``, rounded up to the cent;
2. else, for a tour (``trip_plan``), **AA's RRP**: ``sell = "from" price per person x party``;
3. else no suggestion (a hotel night or a transfer sold alone has no RRP).

A new AA option added without a sell by a role that sees margins (owner, agent) takes the suggestion as its sell;
an assistant's option keeps the old rule (sell 0 until an owner or agent prices it). The below-"from" warning
(``below_from_price``) is unchanged and applies to the suggested sell like any other.

Who sees what: the markup itself and a markup-based suggestion are derived from the B2B price, so they are served only
to roles with :data:`app.permissions.Action.VIEW_MARGIN`; an RRP-based suggestion is public AA data and is served to
every role.
"""

from __future__ import annotations

import json
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

import asyncpg

from app import platform_settings as ps
from app.errors import Invalid
from app.tenancy import AnyContext, TenantContext

KINDS = ("trip_plan", "hotel_room", "transfer", "guide", "activity", "entrance", "meal")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")
BASIS_MARKUP = "agency_markup"
BASIS_RRP = "aa_rrp"

READ_SQL = "SELECT retail_markup_pct FROM crm.tenant_settings"
UPSERT_SQL = """
INSERT INTO crm.tenant_settings (retail_markup_pct) VALUES ($1::jsonb)
ON CONFLICT (tenant_id) DO UPDATE SET retail_markup_pct = EXCLUDED.retail_markup_pct
"""


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def parse(raw: Any) -> dict[str, Decimal]:
    """The stored JSONB as ``{kind: percent}``; anything unreadable is left out (the CHECK keeps it clean)."""
    data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    out: dict[str, Decimal] = {}
    for kind, value in dict(data).items():
        number = _decimal(value)
        if kind in KINDS and number is not None:
            out[kind] = number
    return out


async def markups(ctx: AnyContext) -> dict[str, Decimal]:
    try:
        async with ctx.conn.transaction():  # a savepoint: a missing column must not abort the request's transaction
            raw = await ctx.conn.fetchval(READ_SQL)
    except asyncpg.UndefinedColumnError:  # crm-046 not applied yet: no markup
        return {}
    return parse(raw)


def _out(values: dict[str, Decimal]) -> dict[str, str]:
    return {k: format(values[k].normalize(), "f") for k in KINDS if k in values}


async def read(ctx: AnyContext) -> dict[str, Any]:
    return {
        "retail_markup_pct": _out(await markups(ctx)),
        "kinds": list(KINDS),
        "markup_max_pct": format(ps.decimal(ps.MARKUP_MAX), "f"),
    }


async def update(ctx: TenantContext, changes: dict[str, Any]) -> dict[str, Any]:
    """Merge ``{kind: percent | None}`` into the agency's markups (None removes the kind)."""
    current = await markups(ctx)
    # AA-1096 L4: the ceiling an owner may set is a platform setting (crm.agency_defaults.markup_max_pct, 0 to 300 %).
    ceiling = ps.decimal(ps.MARKUP_MAX)
    for kind, value in changes.items():
        if kind not in KINDS:
            raise Invalid("invalid_product_type", f"one of {', '.join(KINDS)}")
        if value is None:
            current.pop(kind, None)
            continue
        number = _decimal(value)
        if number is None or number < 0 or number > ceiling or number != number.quantize(CENT):
            raise Invalid("invalid_markup", f"a percentage from 0 to {ceiling:f} with at most 2 decimals")
        current[kind] = number
    stored = {k: float(v) if v != v.to_integral_value() else int(v) for k, v in current.items()}
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(UPSERT_SQL, json.dumps(stored))
    except asyncpg.CheckViolationError:
        raise Invalid("invalid_markup", "a percentage from 0 to 300 with at most 2 decimals") from None
    ctx.audit("update", "allowed", "tenant_settings", None, fields=("retail_markup_pct",))
    return await read(ctx)


def suggest(
    kind: str | None,
    *,
    b2b_price: Decimal | None,
    from_price: Decimal | None,
    party: int,
    agency_markups: dict[str, Decimal],
) -> tuple[Decimal, str] | None:
    """``(sell, basis)`` for an AA option, or None: the agency markup on the B2B price, else the tour's RRP x party."""
    markup = agency_markups.get(kind or "")
    if markup is not None and b2b_price is not None and b2b_price > 0:
        sell = (b2b_price * (1 + markup / HUNDRED)).quantize(CENT, rounding=ROUND_CEILING)
        return sell, BASIS_MARKUP
    if kind == "trip_plan" and from_price is not None and from_price > 0 and party >= 1:
        return (from_price * party).quantize(CENT, rounding=ROUND_CEILING), BASIS_RRP
    return None


def suggestion_out(suggested: tuple[Decimal, str] | None, *, sees_margin: bool) -> dict[str, Any]:
    """The builder's ``suggested_sell`` / ``suggested_basis`` keys (a markup suggestion only for margin roles)."""
    if suggested is None or (suggested[1] == BASIS_MARKUP and not sees_margin):
        return {"suggested_sell": None, "suggested_basis": None}
    return {"suggested_sell": format(suggested[0], "f"), "suggested_basis": suggested[1]}
