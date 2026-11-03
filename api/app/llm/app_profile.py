"""The CRM app profile (AA-1025 H1c; root ADR 0009 §4, root ADR 0008a §6): ``crm.llm_app_profile`` (crm-041).

The app-wide switches of the CRM's AI, the shape of the CIS app profile (CIS 345) kept in schema ``crm`` (the CRM reads
no ``shared.llm_*`` table). At most one row (``app = 'crm'``); no row = the defaults (AI on, no daily budget). The CIS
admin writes it when it edits the ``crm`` row of AI providers › Providers › App profiles: CIS relays the staff
member's own WORKFORCE token to ``PUT /crm/ops/v1/llm/app-profile`` (crm.ops.v1 1.10, ``crm-platform-admin``).

The gateway (:mod:`app.llm.gateway`) reads it at the start of every call, in the call's own transaction (a savepoint):

- ``ai_enabled`` false: refused ``app_disabled`` (error class ``disabled``), a 0 USD ledger row, nothing scrubbed or
  sent; the draft says "reply by hand" (the CRM's degrade mode ``manual_reply_needed``);
- ``default_daily_budget_usd``: today's (UTC) AI spend of every agency together (``crm.llm_usage_daily``, guardrail
  rows included) against it, the shared core's ``budget.decide``: at 100 % refused ``app_budget_exhausted`` (class
  ``budget``), unreadable refused ``spend_unreadable`` (fail closed, as the agency budget). Each agency's own monthly
  budget still applies after it.

A missing table (before crm-041 is applied) or an unreadable profile counts as the defaults (fail open, the CIS rule
for the app switch: a database blip must not stop every AI call; the stage profile read that follows fails closed on
the same blip). Platform table: no tenant column, no personal data, read on a pool connection by the ops routes (a DB
lint platform module) and on the call's connection by the gateway.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import asyncpg

APP = "crm"
DEGRADE_MODES = ("manual_reply_needed",)
REASON_MAX = 500
BUDGET_MAX_USD = Decimal("100000")
EDITABLE = ("ai_enabled", "bulk_paused", "default_daily_budget_usd", "degrade_mode", "disabled_reason")

PROFILE_SQL = """
SELECT ai_enabled, bulk_paused, default_daily_budget_usd, degrade_mode, disabled_reason, updated_at, updated_by
  FROM crm.llm_app_profile WHERE app = 'crm'
"""
DAY_SPEND_SQL = """
SELECT coalesce(sum(cost_usd), 0) FROM crm.llm_usage_daily WHERE day = (now() AT TIME ZONE 'UTC')::date
"""
UPSERT_SQL = """
INSERT INTO crm.llm_app_profile (app, ai_enabled, bulk_paused, default_daily_budget_usd, degrade_mode, disabled_reason,
                                 updated_at, updated_by)
VALUES ('crm', $1, $2, $3, $4, $5, now(), $6)
ON CONFLICT (app) DO UPDATE
   SET ai_enabled = EXCLUDED.ai_enabled, bulk_paused = EXCLUDED.bulk_paused,
       default_daily_budget_usd = EXCLUDED.default_daily_budget_usd, degrade_mode = EXCLUDED.degrade_mode,
       disabled_reason = EXCLUDED.disabled_reason, updated_at = now(), updated_by = EXCLUDED.updated_by
RETURNING ai_enabled, bulk_paused, default_daily_budget_usd, degrade_mode, disabled_reason, updated_at, updated_by
"""

log = logging.getLogger(__name__)


class AppProfileUnavailable(RuntimeError):
    """crm.llm_app_profile is not there (crm-041 not applied): the ops routes answer 503."""


@dataclass(frozen=True)
class AppProfile:
    ai_enabled: bool = True
    bulk_paused: bool = False
    default_daily_budget_usd: Decimal | None = None
    degrade_mode: str = "manual_reply_needed"
    disabled_reason: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        budget = self.default_daily_budget_usd
        out["default_daily_budget_usd"] = None if budget is None else f"{budget:.4f}"
        return {"app": APP, **out}


DEFAULT = AppProfile()


def profile_of(row: Any) -> AppProfile:
    """The profile of one ``PROFILE_SQL`` row, the defaults for None. Pure."""
    if row is None:
        return DEFAULT
    updated = row["updated_at"]
    budget = row["default_daily_budget_usd"]
    return AppProfile(
        ai_enabled=row["ai_enabled"] is not False,
        bulk_paused=row["bulk_paused"] is True,
        default_daily_budget_usd=None if budget is None else Decimal(budget),
        degrade_mode=row["degrade_mode"] if row["degrade_mode"] in DEGRADE_MODES else DEGRADE_MODES[0],
        disabled_reason=row["disabled_reason"] or None,
        updated_at=updated.isoformat() if isinstance(updated, datetime) else (str(updated) if updated else None),
        updated_by=row["updated_by"] or None,
    )


async def for_call(conn: asyncpg.Connection) -> AppProfile:
    """The profile the gateway acts on, read on the call's connection in a savepoint. Never raises: a missing table or
    any read error = the defaults (module docstring), one warning line for an error other than the missing table."""
    try:
        async with conn.transaction():
            return profile_of(await conn.fetchrow(PROFILE_SQL))
    except asyncpg.UndefinedTableError:
        return DEFAULT
    except Exception as exc:  # noqa: BLE001 — fail open, class only
        log.warning("crm_llm_app_profile_unreadable error=%s", type(exc).__name__)
        return DEFAULT


async def day_spend(conn: asyncpg.Connection) -> Decimal | None:
    """Today's (UTC) AI spend of every agency together, None when it cannot be read (the decision fails closed)."""
    try:
        async with conn.transaction():
            return Decimal(await conn.fetchval(DAY_SPEND_SQL))
    except Exception as exc:  # noqa: BLE001 — class only
        log.warning("crm_llm_app_spend_unreadable error=%s", type(exc).__name__)
        return None


# ── the ops routes (crm.ops.v1 1.10) ─────────────────────────────────────────────────────────────────────────────


async def read(pool: asyncpg.Pool) -> AppProfile:
    """The stored profile (the defaults when there is no row). Raises ``AppProfileUnavailable`` before crm-041."""
    try:
        async with pool.acquire() as conn:
            return profile_of(await conn.fetchrow(PROFILE_SQL))
    except asyncpg.UndefinedTableError as exc:
        raise AppProfileUnavailable("the app profile needs migration crm-041") from exc


def merged(fields: dict[str, Any]) -> AppProfile:
    """The profile a PUT stores: the whole profile is sent (CIS sends its row after the edit). Switching AI on clears
    the reason; switching it off needs one (3 to 500 characters). Raises ValueError on a value the table would refuse.
    Pure."""
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    mode = fields.get("degrade_mode") or DEGRADE_MODES[0]
    if mode not in DEGRADE_MODES:
        raise ValueError(f"degrade_mode must be one of {', '.join(DEGRADE_MODES)}")
    budget = fields.get("default_daily_budget_usd")
    if budget is not None:
        budget = Decimal(str(budget)).quantize(Decimal("0.0001"))
        if not Decimal(0) <= budget <= BUDGET_MAX_USD:
            raise ValueError("default_daily_budget_usd must be between 0 and 100000")
    ai_enabled = fields.get("ai_enabled") is not False
    reason = (fields.get("disabled_reason") or "").strip() or None
    if ai_enabled:
        reason = None
    elif reason is None or len(reason) < 3:
        raise ValueError("say why AI is switched off (disabled_reason, at least 3 characters)")
    elif len(reason) > REASON_MAX:
        raise ValueError(f"disabled_reason must be at most {REASON_MAX} characters")
    return AppProfile(
        ai_enabled=ai_enabled,
        bulk_paused=fields.get("bulk_paused") is True,
        default_daily_budget_usd=budget,
        degrade_mode=mode,
        disabled_reason=reason,
    )


async def write(pool: asyncpg.Pool, fields: dict[str, Any], *, actor: str) -> AppProfile:
    """Store the profile (insert the row or replace it). Raises ValueError (a refused value) or
    ``AppProfileUnavailable`` (before crm-041)."""
    after = merged(fields)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                UPSERT_SQL,
                after.ai_enabled,
                after.bulk_paused,
                after.default_daily_budget_usd,
                after.degrade_mode,
                after.disabled_reason,
                actor[:120],
            )
    except asyncpg.UndefinedTableError as exc:
        raise AppProfileUnavailable("the app profile needs migration crm-041") from exc
    saved = profile_of(row)
    log.info(
        "crm_llm_app_profile_written ai_enabled=%s budget_set=%s",
        saved.ai_enabled,
        saved.default_daily_budget_usd is not None,
    )
    return saved
