"""FX for the ledger (design v3 R17 §3.1) and Settings > Payments.

- :func:`resolve`: the rate the database would freeze (``crm.fx_resolve``: the agency override of the day, else the
  platform rate of the day, else the latest earlier day within 30 days with ``stale_days``). The resolution lives in
  the database because the confirm (``crm.booking_line_confirm``, SECURITY DEFINER) freezes with it; Python only asks.
- Overrides (``crm.fx_rate_override``): the owner's rate for a day, append-only, the latest row of a day wins.
- Payment settings (``crm.tenant_settings``): ``base_currency`` (default USD), ``booking_validation_level``
  (off / basic / strict, default basic), ``fx_mode`` (automatic / manual). Changing the base currency never re-converts
  a stored row (R17 "no drift"); new confirmations use the new one.

Platform rates are never handed to agencies as a feed (ExchangeRate-API open access terms): only the rate a line used,
with the attribution :data:`ATTRIBUTION` wherever a platform rate is shown.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app.errors import Invalid
from app.tenancy import AnyContext, TenantContext

ATTRIBUTION = {"text": "Rates By Exchange Rate API", "url": "https://www.exchangerate-api.com"}
CURRENCY = re.compile(r"^[A-Z]{3}$")
LEVELS = ("off", "basic", "strict")
FX_MODES = ("automatic", "manual")
DEFAULTS = {"base_currency": "USD", "booking_validation_level": "basic", "fx_mode": "automatic"}
MAX_RATE = Decimal("9999999999")
MAX_OVERRIDES = 100

RESOLVE_SQL = "SELECT rate, rate_date, fx_source, stale_days FROM crm.fx_resolve($1, $2, $3)"
TODAY_SQL = """
SELECT (now() AT TIME ZONE COALESCE((SELECT timezone FROM crm.tenant_settings), 'Asia/Ho_Chi_Minh'))::date
"""
DAY_OF_SQL = """
SELECT ($1::timestamptz AT TIME ZONE COALESCE((SELECT timezone FROM crm.tenant_settings), 'Asia/Ho_Chi_Minh'))::date
"""
SETTINGS_SQL = "SELECT base_currency, booking_validation_level, fx_mode FROM crm.tenant_settings"
UPSERT_SETTINGS_SQL = """
INSERT INTO crm.tenant_settings (base_currency, booking_validation_level, fx_mode) VALUES ($1, $2, $3)
ON CONFLICT (tenant_id) DO UPDATE
   SET base_currency = EXCLUDED.base_currency, booking_validation_level = EXCLUDED.booking_validation_level,
       fx_mode = EXCLUDED.fx_mode
"""
OVERRIDES_SQL = """
SELECT fx_rate_override_id, rate_date, base, quote, rate, set_by, created_at FROM crm.fx_rate_override
 ORDER BY rate_date DESC, created_at DESC
 LIMIT $1
"""
ADD_OVERRIDE_SQL = """
INSERT INTO crm.fx_rate_override (rate_date, base, quote, rate, set_by) VALUES ($1, $2, $3, $4, $5)
RETURNING fx_rate_override_id, created_at
"""


@dataclass(frozen=True)
class Rate:
    rate: Decimal
    rate_date: dt.date
    source: str  # platform_daily | manual
    stale_days: int

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "rate": str(self.rate),
            "rate_date": self.rate_date.isoformat(),
            "source": self.source,
            "stale_days": self.stale_days,
        }
        if self.source == "platform_daily" and self.rate != 1:
            out["attribution"] = ATTRIBUTION
        return out


def currency(value: Any, field: str = "currency") -> str:
    if not isinstance(value, str) or not CURRENCY.match(value):
        raise Invalid(f"invalid_{field}", "a three-letter ISO 4217 code, e.g. USD or VND")
    return value


def positive_rate(value: Any) -> Decimal:
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Invalid("invalid_rate", "a positive number") from None
    if not rate.is_finite() or rate <= 0 or rate > MAX_RATE:
        raise Invalid("invalid_rate", "a positive number")
    return rate


async def agency_today(ctx: AnyContext) -> dt.date:
    """Today in the agency timezone (crm-012 ``tenant_settings.timezone``), the FX basis date of a confirm."""
    day = await ctx.conn.fetchval(TODAY_SQL)
    assert isinstance(day, dt.date)  # noqa: S101 — the SQL returns a date
    return day


async def agency_day(ctx: AnyContext, at: dt.datetime | None) -> dt.date:
    """The agency-timezone day of an instant (``None``: today), the FX basis date of an event Booking timestamps in UTC
    (AA-957: the day Booking cancelled a booking, for its fee)."""
    if at is None:
        return await agency_today(ctx)
    day = await ctx.conn.fetchval(DAY_OF_SQL, at)
    assert isinstance(day, dt.date)  # noqa: S101 — the SQL returns a date
    return day


async def resolve(ctx: AnyContext, frm: str, to: str, day: dt.date) -> Rate | None:
    row = await ctx.conn.fetchrow(RESOLVE_SQL, frm, to, day)
    if row is None:
        return None
    return Rate(Decimal(row["rate"]), row["rate_date"], row["fx_source"], int(row["stale_days"]))


async def settings(ctx: AnyContext) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(SETTINGS_SQL)
    if row is None:
        return dict(DEFAULTS)
    return {
        "base_currency": row["base_currency"],
        "booking_validation_level": row["booking_validation_level"],
        "fx_mode": row["fx_mode"],
    }


async def payment_settings(ctx: TenantContext) -> dict[str, Any]:
    return {**await settings(ctx), "levels": list(LEVELS), "fx_modes": list(FX_MODES), "attribution": ATTRIBUTION}


async def update_payment_settings(ctx: TenantContext, changes: dict[str, Any]) -> dict[str, Any]:
    merged = {**await settings(ctx), **{k: v for k, v in changes.items() if k in DEFAULTS and v is not None}}
    currency(merged["base_currency"], "base_currency")
    if merged["booking_validation_level"] not in LEVELS:
        raise Invalid("invalid_booking_validation_level", ", ".join(LEVELS))
    if merged["fx_mode"] not in FX_MODES:
        raise Invalid("invalid_fx_mode", ", ".join(FX_MODES))
    await ctx.conn.execute(
        UPSERT_SETTINGS_SQL, merged["base_currency"], merged["booking_validation_level"], merged["fx_mode"]
    )
    ctx.audit("update", "allowed", "tenant_settings", str(ctx.tenant_id), fields=tuple(sorted(changes)))
    return await payment_settings(ctx)


def _override_view(row: Any) -> dict[str, Any]:
    return {
        "fx_rate_override_id": str(row["fx_rate_override_id"]),
        "rate_date": row["rate_date"].isoformat(),
        "base": row["base"],
        "quote": row["quote"],
        "rate": str(row["rate"]),
        "set_by": str(row["set_by"]) if row.get("set_by") is not None else None,
        "created_at": row["created_at"].isoformat(),
    }


async def list_overrides(ctx: TenantContext) -> dict[str, Any]:
    rows = await ctx.conn.fetch(OVERRIDES_SQL, MAX_OVERRIDES)
    return {"overrides": [_override_view(r) for r in rows]}


async def add_override(
    ctx: TenantContext, *, base: str, quote: str, rate: Any, rate_date: dt.date | None
) -> dict[str, Any]:
    base, quote = currency(base, "base"), currency(quote, "quote")
    if base == quote:
        raise Invalid("invalid_pair", "two different currencies")
    value = positive_rate(rate)
    day = rate_date or await agency_today(ctx)
    today = await agency_today(ctx)
    if day > today or day < today - dt.timedelta(days=366):
        raise Invalid("invalid_rate_date", "today or a day within the last year")
    try:
        async with ctx.conn.transaction():
            row = await ctx.conn.fetchrow(ADD_OVERRIDE_SQL, day, base, quote, value, ctx.member_id)
    except asyncpg.CheckViolationError:
        raise Invalid("invalid_rate", "a positive number") from None
    override_id: uuid.UUID = row["fx_rate_override_id"]
    ctx.audit("create", "allowed", "fx_rate_override", str(override_id))
    return _override_view(
        {
            "fx_rate_override_id": override_id,
            "rate_date": day,
            "base": base,
            "quote": quote,
            "rate": value,
            "set_by": ctx.member_id,
            "created_at": row["created_at"],
        }
    )
