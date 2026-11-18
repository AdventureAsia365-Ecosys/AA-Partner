"""AA-1113 (root ADR 0009 decision 6; CIS 450): one connection cap for every caller, the CRM's side.

A model connection (an AWS account's Bedrock, a vendor API) has one monthly budget in the CIS gateway registry. The CIS
gateway leaves a connection out of every route once this UTC month's spend on it reaches that budget and the
connection's policy is a hard stop. The CRM's own gateway (:mod:`app.llm.gateway`) spends on the same connections, so
it must stop at the same cap: CIS publishes each capped connection's month-to-date CIS ledger spend, budget and policy
as the read contract ``catalog.v_llm_connection_cap_v1`` (CIS 450; CIS 451 lets ``crm_app`` read that one view, a
root ADR 0008a amendment proposed in S284: the CRM still reads no ``shared`` table). Before every call the gateway
asks :func:`refusal`:

- the CRM's provider connection is named in the CIS registry's terms (:data:`CIS_CONNECTION`: the task role's own
  Bedrock, ``bedrock``, is the account the CRM runs on, ``acc2``; a role-assumed connection, ``bedrock_role``, has no
  mapping yet, so no cap is known for it and the call goes on, logged once per process);
- no row in the view = the connection has no monthly budget: nothing to check;
- the CRM adds its own spend of the month (``crm.llm_usage_daily`` since the view's ``month_start``, guardrail rows
  included: every CRM call of this process runs on that one connection), which CIS cannot see, to the view's spend;
- at or above the budget with a hard-stop policy: the ``dev`` spend rules (:mod:`app.llm.spend_environment`, the
  default) refuse ``connection_budget_exhausted`` (class ``budget``, one 0 USD row, nothing scrubbed or sent, the draft
  says "write it by hand"); the ``prod`` rules (pay per use, tech lead S283: a connection cap never stops a customer
  call) let the call go on and log ``crm_llm_connection_cap_reached``;
- a read error: ``dev`` refuses ``spend_unreadable`` (fail closed, the CRM's rule for its other budgets), ``prod`` goes
  on with a warning. A missing view or grant (before CIS 450 / 451 are applied) means no cap is published to the CRM
  yet: the call goes on (one warning line per process).

Read on the call's own connection in a savepoint, like :mod:`app.llm.app_profile` (a platform read, no tenant rows).
**Cost of a read** (review S284): the CIS half (the view row: a month-wide aggregate of the CIS ledger) is kept per
connection for :data:`CACHE_TTL_SECONDS` in this process, every outcome included (a row, no row, a missing view or
grant, a read error), the window the CIS gateway's own ``connection_budget`` cache uses: a spend that crosses the cap is
seen within it. Only a connection with a hard-stop budget row reads the CRM's own month spend, per call (a small daily
rollup). A connection with no budget row (acc2 today) costs one view read per minute and process.
CIS does not count the CRM's spend toward the cap (it cannot read the CRM's ledger), so the CRM stops at the shared
total while CIS may pass the cap by the CRM's share; the CRM's only live route is small (AA-1113 notes).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import asyncpg

from app.aa_llm import errors as llm_errors
from app.aa_llm.budget import REASON_SPEND_UNREADABLE
from app.llm import spend_environment

log = logging.getLogger(__name__)

CONNECTION_BUDGET_EXHAUSTED = "connection_budget_exhausted"
SPEND_UNREADABLE = REASON_SPEND_UNREADABLE
# The CRM provider's connection key (app.llm.providers) -> the CIS registry's connection key. The ECS task role's own
# Bedrock is the account the CRM runs on; a role-assumed connection is named once its account is (follow-up).
CIS_CONNECTION = {"bedrock": "acc2"}

CAP_SQL = """
SELECT connection_key, monthly_budget_usd, month_start, month_spent_usd, hard_stop
  FROM catalog.v_llm_connection_cap_v1 WHERE connection_key = $1
"""
CRM_MONTH_SQL = """
SELECT coalesce(sum(cost_usd), 0) FROM crm.llm_usage_daily WHERE day >= ($1 AT TIME ZONE 'UTC')::date
"""

CACHE_TTL_SECONDS = 60.0  # technical-constant: in-process refresh of the CIS cap read, as CIS connection_budget

_unmapped_logged: set[str] = set()  # connection keys (and unpublished:<key>) logged once


@dataclass(frozen=True)
class _ReadFailed:
    """A cached failed read of the view: ``unpublished`` for a missing view or grant (CIS 450 / 451 not applied)."""

    unpublished: bool
    error: str


# cis connection key -> (monotonic time of the read, the view row as a dict | None for no budget row | _ReadFailed)
_cis_cache: dict[str, tuple[float, dict[str, Any] | _ReadFailed | None]] = {}


def invalidate() -> None:
    """Drop the cached CIS reads (tests; a process sees a new cap within CACHE_TTL_SECONDS anyway)."""
    _cis_cache.clear()


@dataclass(frozen=True)
class CapState:
    """One capped connection as the CRM sees it: the CIS ledger's month spend plus the CRM's own."""

    connection_key: str
    monthly_budget_usd: Decimal
    cis_spent_usd: Decimal
    crm_spent_usd: Decimal
    hard_stop: bool

    @property
    def spent_usd(self) -> Decimal:
        return self.cis_spent_usd + self.crm_spent_usd

    @property
    def stops(self) -> bool:
        """At or above the budget (a 0 USD budget is reached at once) with a hard-stop policy."""
        return self.hard_stop and self.spent_usd >= self.monthly_budget_usd


def cis_connection(provider: Any) -> str | None:
    """The CIS connection key of the provider the call would use, None when it has none (the stub, a role-assumed
    connection not mapped yet: logged once per key and process)."""
    key = getattr(provider, "connection_key", None)
    if not isinstance(key, str) or not key or key.startswith("stub"):
        return None
    mapped = CIS_CONNECTION.get(key)
    if mapped is None and key not in _unmapped_logged:
        _unmapped_logged.add(key)
        log.warning("crm_llm_connection_cap_unmapped connection=%s", key)
    return mapped


async def _cis_row(conn: asyncpg.Connection, cis_key: str) -> dict[str, Any] | None:
    """The view row of the connection, None when it has no monthly budget. Raises on a read error."""
    async with conn.transaction():
        row = await conn.fetchrow(CAP_SQL, cis_key)
    return None if row is None else dict(row)


async def _state(conn: asyncpg.Connection, cis_key: str, row: dict[str, Any]) -> CapState:
    """The cap state from the view row plus the CRM's own month spend (read now, in a savepoint)."""
    async with conn.transaction():
        crm = await conn.fetchval(CRM_MONTH_SQL, row["month_start"])
    return CapState(
        connection_key=cis_key,
        monthly_budget_usd=Decimal(row["monthly_budget_usd"]),
        cis_spent_usd=Decimal(row["month_spent_usd"]),
        crm_spent_usd=Decimal(crm or 0),
        hard_stop=bool(row["hard_stop"]),
    )


async def read_state(conn: asyncpg.Connection, cis_key: str) -> CapState | None:
    """The connection's cap state read now (no cache), None when it has no monthly budget. Raises
    ``asyncpg.UndefinedTableError`` when the view is missing and any other read error as is (the caller decides)."""
    row = await _cis_row(conn, cis_key)
    return None if row is None else await _state(conn, cis_key, row)


async def _cached_cis_row(conn: asyncpg.Connection, cis_key: str) -> dict[str, Any] | _ReadFailed | None:
    """:func:`_cis_row` kept CACHE_TTL_SECONDS per connection, a failed read included (as a :class:`_ReadFailed`)."""
    now = time.monotonic()
    hit = _cis_cache.get(cis_key)
    if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    outcome: dict[str, Any] | _ReadFailed | None
    try:
        outcome = await _cis_row(conn, cis_key)
    except (asyncpg.UndefinedTableError, asyncpg.InsufficientPrivilegeError) as exc:
        outcome = _ReadFailed(unpublished=True, error=type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 — class only
        outcome = _ReadFailed(unpublished=False, error=type(exc).__name__)
    _cis_cache[cis_key] = (now, outcome)
    return outcome


def _failed(cis_key: str, failed: _ReadFailed, prod: bool) -> tuple[str | None, str | None]:
    if failed.unpublished:
        if f"unpublished:{cis_key}" not in _unmapped_logged:
            _unmapped_logged.add(f"unpublished:{cis_key}")
            log.warning("crm_llm_connection_cap_unpublished connection=%s error=%s", cis_key, failed.error)
        return None, None
    log.warning("crm_llm_connection_cap_unreadable connection=%s error=%s prod=%s", cis_key, failed.error, prod)
    return (None, None) if prod else (SPEND_UNREADABLE, None)


async def refusal(conn: asyncpg.Connection, provider: Any) -> tuple[str | None, str | None]:
    """(refusal code, error class) when the connection cap stops this call, else (None, None). Never raises."""
    cis_key = cis_connection(provider)
    if cis_key is None:
        return None, None
    prod = spend_environment.is_prod()
    row = await _cached_cis_row(conn, cis_key)
    if isinstance(row, _ReadFailed):
        return _failed(cis_key, row, prod)
    if row is None or not row["hard_stop"]:
        return None, None  # no budget, or an alert-only policy: the CRM's own spend cannot make it stop
    try:
        state = await _state(conn, cis_key, row)
    except Exception as exc:  # noqa: BLE001 — class only
        return _failed(cis_key, _ReadFailed(unpublished=False, error=type(exc).__name__), prod)
    if not state.stops:
        return None, None
    log.warning(
        "crm_llm_connection_cap_reached connection=%s spent=%s budget=%s prod=%s",
        cis_key,
        state.spent_usd,
        state.monthly_budget_usd,
        prod,
    )
    if prod:
        return None, None
    return CONNECTION_BUDGET_EXHAUSTED, llm_errors.BUDGET
