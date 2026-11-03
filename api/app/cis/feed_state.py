"""``crm.membership_feed_state`` (crm-013) and ``crm.entitlement_feed_state`` (crm-043): the one consumer state of each
CIS feed, the membership feed (AA-978) and the account feed (AA-1028, ``crm_entitlement_pull``).

Platform singletons (no tenant column, no personal data), so they are read and written on the pool directly; this
module is on the DB-module list of ``tests/unit/test_code_rules.py``, and only the two feed modules
(:mod:`app.cis.membership_feed`, :mod:`app.cis.entitlement_feed`) call it. Every function takes ``table``
(:data:`MEMBERSHIP` by default, :data:`ENTITLEMENT`), a fixed name, never caller text.

- :func:`load`: the cursor, the consecutive failures and the time of the last error (the job's backoff).
- :func:`advance`: moves the cursor **only from the value the caller read** (compare and set), so two pulls that
  overlap (a slow run and the next slot's) never move it backwards; records the success.
- :func:`succeed` / :func:`fail`: the run's outcome (an error class code only, never a message).
- :func:`health`: one word for ``/crm/health/ready`` (review round 1: a stuck feed must show without reading the
  table); ``app/db.py`` calls it from the readiness probe.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import asyncpg

MEMBERSHIP = "crm.membership_feed_state"
ENTITLEMENT = "crm.entitlement_feed_state"
TABLES = frozenset({MEMBERSHIP, ENTITLEMENT})

LOAD_SQL = "SELECT feed_cursor, failures, last_error_at, last_ok_at FROM {table}"
ADVANCE_SQL = """
UPDATE {table} SET feed_cursor = $2, last_ok_at = now(), failures = 0
 WHERE feed_cursor IS NOT DISTINCT FROM $1
RETURNING 1
"""
SUCCEED_SQL = "UPDATE {table} SET last_ok_at = now(), failures = 0"
FAIL_SQL = """
UPDATE {table}
   SET last_error_class = $1, last_error_at = now(), failures = least(failures + 1, 1000000)
RETURNING failures
"""
# crm-043 only: the hourly pass that re-projects every agency.
RECONCILE_DUE_SQL = "SELECT last_reconcile_at FROM crm.entitlement_feed_state"
RECONCILED_SQL = "UPDATE crm.entitlement_feed_state SET last_reconcile_at = now()"


def _sql(template: str, table: str) -> str:
    if table not in TABLES:
        raise ValueError(f"unknown feed state table {table!r}")
    return template.format(table=table)


# A run every 20-35 s: no success for this long while nothing failed means the pull is not running at all.
STALE_AFTER = dt.timedelta(minutes=10)


@dataclass(frozen=True)
class FeedState:
    cursor: str | None
    failures: int
    last_error_at: dt.datetime | None
    last_ok_at: dt.datetime | None


async def load(pool: asyncpg.Pool, table: str = MEMBERSHIP) -> FeedState:
    row = await pool.fetchrow(_sql(LOAD_SQL, table))
    if row is None:  # crm-013 / crm-043 insert the row; a missing one is a broken database, not "no cursor"
        raise RuntimeError(f"{table} has no row")
    return FeedState(row["feed_cursor"], row["failures"], row["last_error_at"], row["last_ok_at"])


async def advance(pool: asyncpg.Pool, old: str | None, new: str, table: str = MEMBERSHIP) -> bool:
    """True when the cursor moved from ``old`` to ``new``; False when another pull moved it first."""
    return await pool.fetchval(_sql(ADVANCE_SQL, table), old, new) is not None


async def succeed(pool: asyncpg.Pool, table: str = MEMBERSHIP) -> None:
    await pool.execute(_sql(SUCCEED_SQL, table))


async def fail(pool: asyncpg.Pool, error_class: str, table: str = MEMBERSHIP) -> int:
    """Records the error class; returns the consecutive failures so far."""
    return int(await pool.fetchval(_sql(FAIL_SQL, table), error_class) or 0)


async def last_reconcile(pool: asyncpg.Pool) -> dt.datetime | None:
    """crm-043: when the account feed last re-projected every agency (None = never)."""
    return await pool.fetchval(RECONCILE_DUE_SQL)


async def reconciled(pool: asyncpg.Pool) -> None:
    await pool.execute(RECONCILED_SQL)


def health_word(state: FeedState, now: dt.datetime) -> str:
    """``failing`` (consecutive failures), ``pending`` (no success yet), ``stale`` (no success for
    :data:`STALE_AFTER`: the worker or its Recurring slot is not running) or ``ok``."""
    if state.failures:
        return "failing"
    if state.last_ok_at is None:
        return "pending"
    return "stale" if now - state.last_ok_at > STALE_AFTER else "ok"


async def health(pool: asyncpg.Pool, *, configured: bool, table: str = MEMBERSHIP) -> str:
    """The feed in one word for readiness (never a reason to fail it): ``not_configured`` (no ``CIS_API_URL`` /
    ``M2M_CLIENT``), :func:`health_word`, or ``unknown`` when the row cannot be read."""
    if not configured:
        return "not_configured"
    try:
        state = await load(pool, table)
    except Exception:  # noqa: BLE001 — readiness reports, never raises
        return "unknown"
    return health_word(state, dt.datetime.now(dt.UTC))
