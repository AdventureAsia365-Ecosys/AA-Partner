"""``crm.aa_catalog_tour`` / ``crm.aa_catalog_state`` (crm-017): the AA tour cache, platform tables without tenant rows
or personal data. Written on the pool by the sync only (this module is on the DB-module list of
``tests/unit/test_code_rules.py``); agencies read it with :func:`search` / :func:`tour` inside their own transaction.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.catalog.client import Summary
from app.tenancy import TenantContext

STATE_SQL = """
SELECT list_cursor, pass_started_at, last_full_pass_at, failures, last_error_at FROM crm.aa_catalog_state
"""
KNOWN_SQL = """
SELECT source_id, content_hash, served_state, etag FROM crm.aa_catalog_tour
 WHERE source_system = 'cis' AND source_id = ANY($1::text[])
"""
TOUCH_SQL = """
UPDATE crm.aa_catalog_tour SET checked_at = now() WHERE source_system = 'cis' AND source_id = ANY($1::text[])
"""
UPSERT_SQL = """
INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, content_hash, etag, served_state,
                                 reason, body, fetched_at, checked_at, state_changed_at)
VALUES ($1, $2, $3, $4, $5, $6, 'served', NULL, $7::jsonb, now(), now(), now())
ON CONFLICT (source_system, source_id) DO UPDATE
   SET title = EXCLUDED.title, country_code = EXCLUDED.country_code, duration_days = EXCLUDED.duration_days,
       content_hash = EXCLUDED.content_hash, etag = EXCLUDED.etag, body = EXCLUDED.body, reason = NULL,
       fetched_at = now(), checked_at = now(),
       state_changed_at = CASE WHEN crm.aa_catalog_tour.served_state <> 'served' THEN now()
                               ELSE crm.aa_catalog_tour.state_changed_at END,
       served_state = 'served'
"""
STATE_CHANGE_SQL = """
UPDATE crm.aa_catalog_tour
   SET served_state = $2, reason = $3, checked_at = now(),
       state_changed_at = CASE WHEN served_state <> $2 THEN now() ELSE state_changed_at END
 WHERE source_system = 'cis' AND source_id = $1
"""
STALE_SQL = """
SELECT source_id, etag FROM crm.aa_catalog_tour
 WHERE source_system = 'cis' AND served_state <> 'retired' AND checked_at < $1
 ORDER BY checked_at, source_id
 LIMIT $2
"""
CURSOR_SQL = """
UPDATE crm.aa_catalog_state SET list_cursor = $1, pass_started_at = COALESCE(pass_started_at, now())
"""
CYCLE_DONE_SQL = """
UPDATE crm.aa_catalog_state
   SET list_cursor = NULL, pass_started_at = NULL, last_full_pass_at = now(), failures = 0, last_error_class = NULL,
       last_ok_at = now()
"""
OK_SQL = "UPDATE crm.aa_catalog_state SET failures = 0, last_error_class = NULL, last_ok_at = now()"
FAIL_SQL = """
UPDATE crm.aa_catalog_state
   SET failures = least(failures + 1, 1000000), last_error_class = $1, last_error_at = now()
RETURNING failures
"""
SEARCH_SQL = """
SELECT source_id, title, country_code, duration_days
  FROM crm.aa_catalog_tour
 WHERE source_system = 'cis' AND served_state = 'served'
   AND ($1::text IS NULL OR title ILIKE '%' || $1 || '%' ESCAPE '\\' OR source_id = $4)
   AND ($2::text IS NULL OR country_code = $2)
 ORDER BY title, source_id
 LIMIT $3
"""
TOUR_SQL = """
SELECT source_id, title, country_code, duration_days, served_state, reason
  FROM crm.aa_catalog_tour WHERE source_system = 'cis' AND source_id = $1
"""


@dataclass(frozen=True)
class SyncState:
    cursor: str | None
    pass_started_at: dt.datetime | None
    last_full_pass_at: dt.datetime | None
    failures: int
    last_error_at: dt.datetime | None


# crm.ops.v1 1.4 (AA-992): what the CIS admin page /admin/data-coverage reads about the cache: counts per served state
# and the sync's state row. Counts and timestamps only (no source id, no title).
COVERAGE_SQL = """
SELECT (SELECT count(*) FROM crm.aa_catalog_tour WHERE served_state = 'served')::int AS served,
       (SELECT count(*) FROM crm.aa_catalog_tour WHERE served_state = 'paused')::int AS paused,
       (SELECT count(*) FROM crm.aa_catalog_tour WHERE served_state = 'retired')::int AS retired,
       s.last_full_pass_at, s.last_ok_at, s.failures, s.last_error_class
  FROM crm.aa_catalog_state s
"""


async def coverage(pool: asyncpg.Pool) -> dict[str, Any]:
    """Cached AA tours per served state + the sync state (crm.ops.v1 ``CatalogCacheCounts``)."""
    row = await pool.fetchrow(COVERAGE_SQL)
    if row is None:  # crm-017 inserts the row
        raise RuntimeError("crm.aa_catalog_state has no row (crm-017)")
    return dict(row)


async def load_state(pool: asyncpg.Pool) -> SyncState:
    row = await pool.fetchrow(STATE_SQL)
    if row is None:  # crm-017 inserts the row
        raise RuntimeError("crm.aa_catalog_state has no row (crm-017)")
    return SyncState(row["list_cursor"], row["pass_started_at"], row["last_full_pass_at"], row["failures"],
                     row["last_error_at"])  # fmt: skip


async def known(pool: asyncpg.Pool, source_ids: list[str]) -> dict[str, Any]:
    return {r["source_id"]: r for r in await pool.fetch(KNOWN_SQL, source_ids)}


async def touch(pool: asyncpg.Pool, source_ids: list[str]) -> None:
    if source_ids:
        await pool.execute(TOUCH_SQL, source_ids)


async def upsert(pool: asyncpg.Pool, s: Summary, etag: str | None, body: dict[str, Any]) -> None:
    await pool.execute(UPSERT_SQL, s.source_id, s.title, s.country_code, s.duration_days, s.content_hash, etag,
                       json.dumps(body, ensure_ascii=False))  # fmt: skip


async def set_state(pool: asyncpg.Pool, source_id: str, state: str, reason: str | None) -> None:
    await pool.execute(STATE_CHANGE_SQL, source_id, state, reason)


async def stale(pool: asyncpg.Pool, before: dt.datetime, limit: int) -> list[tuple[str, str | None]]:
    return [(r["source_id"], r["etag"]) for r in await pool.fetch(STALE_SQL, before, limit)]


async def save_cursor(pool: asyncpg.Pool, cursor: str | None) -> None:
    await pool.execute(CURSOR_SQL, cursor)


async def pass_done(pool: asyncpg.Pool) -> None:
    await pool.execute(CYCLE_DONE_SQL)


async def succeed(pool: asyncpg.Pool) -> None:
    await pool.execute(OK_SQL)


async def fail(pool: asyncpg.Pool, error_class: str) -> int:
    return int(await pool.fetchval(FAIL_SQL, error_class[:60]) or 0)


async def search(ctx: TenantContext, q: str | None, country: str | None, limit: int) -> list[dict[str, Any]]:
    like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") if q else None
    rows = await ctx.conn.fetch(SEARCH_SQL, like, country, limit, q)
    return [dict(r) for r in rows]


async def tour(ctx: TenantContext, source_id: str) -> dict[str, Any] | None:
    row = await ctx.conn.fetchrow(TOUR_SQL, source_id)
    return dict(row) if row is not None else None
