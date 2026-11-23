"""The agency's own rounded prices (AA-1152, crm-086 / crm-087; S285 decision B2 option C, root ADR 0019 b5 as amended
by AA-1152).

Every price the CRM suggests to an agency is rounded **up** to a step of its currency, per traveller first and then
times the party, so a proposal or a planner page never shows odd cents (advisory B2: 41.09 USD -> 42 at step 1, 45 at
step 5; 1,038,000 VND -> 1,040,000 at step 10,000). Rounding is always up: it never costs the agency margin.

- **The step** of a currency: the agency's own choice (``crm.tenant_settings.price_round_step``, Settings > Payments,
  owner only), else the platform default (setting ``crm.agency_defaults.price_round_step``, seeded USD 1, VND
  10,000), else 1 whole unit. An owner may pick only a step of the platform list (``crm.agency_defaults.
  price_round_steps``, seeded USD 1 / 5 / 10, VND 10,000 / 50,000 / 100,000; a currency not listed offers its default
  step only).
- **The switch** ``crm.tenant_settings.show_planner_prices`` (default on): off = the agency's TripPlanner pages show
  no price (contract ``tripplanner.agency_prices.v1``; :mod:`app.tripplanner.prices`).
- **One function** (:func:`suggested_total`) for every suggestion: the builder's AA option (:func:`app.proposals.
  builder._suggestion`, after the agency's price rules) and the planner "from" prices.

Before crm-086 is applied the columns are missing: the read falls back to the defaults (no step chosen, prices shown).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

import asyncpg

from app import platform_settings as ps
from app.errors import Invalid
from app.tenancy import AnyContext, TenantContext

CENT = Decimal("0.01")
ONE = Decimal("1")

READ_SQL = "SELECT price_round_step, show_planner_prices, base_currency FROM crm.tenant_settings"
BASE_SQL = "SELECT base_currency FROM crm.tenant_settings"
UPSERT_SQL = """
INSERT INTO crm.tenant_settings (price_round_step, show_planner_prices) VALUES ($1::jsonb, $2)
ON CONFLICT (tenant_id) DO UPDATE SET price_round_step = EXCLUDED.price_round_step,
                                      show_planner_prices = EXCLUDED.show_planner_prices
"""


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _json(raw: Any) -> dict[str, Any]:
    data = json.loads(raw) if isinstance(raw, str) else raw
    return dict(data) if isinstance(data, dict) else {}


def default_steps() -> dict[str, Decimal]:
    """The platform default step per currency (``crm.agency_defaults.price_round_step``)."""
    return {cur: d for cur, v in _json(ps.get(ps.PRICE_ROUND_STEP)).items() if (d := _decimal(v)) is not None}


def allowed_steps() -> dict[str, list[Decimal]]:
    """The steps an owner may choose per currency (``crm.agency_defaults.price_round_steps``)."""
    out: dict[str, list[Decimal]] = {}
    for cur, values in _json(ps.get(ps.PRICE_ROUND_STEPS)).items():
        steps = sorted({d for v in (values if isinstance(values, list) else []) if (d := _decimal(v)) is not None})
        if steps:
            out[cur] = steps
    return out


def choices(currency: str) -> list[Decimal]:
    """The steps offered for ``currency``: the platform list, else its default step alone."""
    return allowed_steps().get(currency) or [default_steps().get(currency, ONE)]


@dataclass(frozen=True)
class PriceSettings:
    """The agency's own choices (``chosen``: per currency) and its show-prices switch."""

    chosen: dict[str, Decimal] = field(default_factory=dict)
    show_planner_prices: bool = True
    base_currency: str = "USD"

    def step(self, currency: str) -> Decimal:
        return self.chosen.get(currency) or default_steps().get(currency) or ONE


def round_up(amount: Decimal, step: Decimal) -> Decimal:
    """``amount`` up to a multiple of ``step`` (a step of 0 or less = up to the cent)."""
    if step <= 0:
        return amount.quantize(CENT, rounding=ROUND_CEILING)
    return ((amount / step).to_integral_value(rounding=ROUND_CEILING) * step).quantize(CENT)


def per_person(total: Decimal, party: int, step: Decimal) -> Decimal:
    """A party price as the rounded price per traveller (``party`` < 1 = the total is one traveller's)."""
    return round_up(total / max(party, 1), step)


def suggested_total(total: Decimal, party: int, step: Decimal | None) -> Decimal:
    """The rounded suggestion of a party price: per traveller up to the step (None = 1 whole unit), then times the
    party."""
    return (per_person(total, party, step or ONE) * max(party, 1)).quantize(CENT)


HUNDRED = Decimal("100")


def from_price(
    b2b_per_person: Decimal | None,
    rrp_per_person: Decimal | None,
    markup_pct: Decimal | None,
    step: Decimal,
    adjust_pct: Decimal = Decimal("0"),
) -> tuple[Decimal, str] | None:
    """``(price per traveller, basis)``: the agency's own price of an AA tour or add-on (B2 option C, the same order as
    :func:`app.proposals.retail.suggest`): AA's B2B price x (1 + the agency's markup of that product type), else AA's
    RRP; then the agency's price rules (``adjust_pct``, only with a date) and one round up to ``step``. None when AA
    gives neither price."""
    if markup_pct is not None and b2b_per_person is not None and b2b_per_person > 0:
        base, basis = b2b_per_person * (1 + markup_pct / HUNDRED), "agency_markup"
    elif rrp_per_person is not None and rrp_per_person > 0:
        base, basis = rrp_per_person, "aa_rrp"
    else:
        return None
    return round_up(max(base * (1 + adjust_pct / HUNDRED), Decimal("0")), step), basis


def parse(row: Any) -> PriceSettings:
    if row is None:
        return PriceSettings()
    chosen = {cur: d for cur, v in _json(row["price_round_step"]).items() if (d := _decimal(v)) is not None}
    return PriceSettings(chosen, bool(row["show_planner_prices"]), str(row["base_currency"] or "USD"))


async def settings(ctx: AnyContext) -> PriceSettings:
    """The agency's choices; the defaults before crm-086 (a missing column never aborts the request's transaction)."""
    try:
        async with ctx.conn.transaction():
            return parse(await ctx.conn.fetchrow(READ_SQL))
    except asyncpg.UndefinedColumnError:
        base = await ctx.conn.fetchval(BASE_SQL)
        return PriceSettings(base_currency=str(base or "USD"))


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def view(current: PriceSettings) -> dict[str, Any]:
    """The Settings > Payments panel: the step in force per currency offered, the choices and the switch."""
    currencies = sorted({current.base_currency, *allowed_steps(), *default_steps()})
    return {
        "show_planner_prices": current.show_planner_prices,
        "base_currency": current.base_currency,
        "currencies": [
            {
                "currency": cur,
                "step": _text(current.step(cur)),
                "chosen": cur in current.chosen,
                "default": _text(default_steps().get(cur, ONE)),
                "choices": [_text(s) for s in choices(cur)],
            }
            for cur in currencies
        ],  # fmt: skip
    }


async def read(ctx: AnyContext) -> dict[str, Any]:
    return view(await settings(ctx))


async def update(ctx: TenantContext, changes: dict[str, Any]) -> dict[str, Any]:
    """Merge ``{"steps": {currency: step | None}, "show_planner_prices": bool}`` (None = back to the default). A step
    must be one of the platform list of its currency (422 ``invalid_round_step`` naming the currency)."""
    current = await settings(ctx)
    chosen = dict(current.chosen)
    for cur, value in (changes.get("steps") or {}).items():
        if value is None:
            chosen.pop(cur, None)
            continue
        step = _decimal(value)
        offered = choices(cur)
        if step is None or step not in offered:
            raise Invalid("invalid_round_step", f"{cur}: one of {', '.join(_text(s) for s in offered)}",
                          currency=cur, choices=[_text(s) for s in offered])  # fmt: skip
        chosen[cur] = step
    show = changes.get("show_planner_prices")
    show = current.show_planner_prices if show is None else bool(show)
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(UPSERT_SQL, json.dumps({k: _text(v) for k, v in chosen.items()}), show)
    except asyncpg.UndefinedColumnError:
        raise Invalid("planner_prices_not_ready", "price rounding is not available yet; try again later") from None
    ctx.audit("update", "allowed", "tenant_settings", None, fields=("price_round_step", "show_planner_prices"))
    return view(PriceSettings(chosen, show, current.base_currency))
