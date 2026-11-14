"""The CRM record of the CIS tenant erasure saga (AA-1093 stage 2, crm-066 / crm-067): three platform tables, no tenant
column (the CIS tenant may have no CRM agency, and the record outlives the agency erase it drives).

- ``crm.tenant_erasure_feed_state``: the feed cursor and its failures (the singleton row is inserted on first use);
- ``crm.tenant_erasure_saga``: the newest state of each request the feed served (an older item never overwrites a
  newer one);
- ``crm.tenant_erasure_report``: one report per request, round and mode, written in the transaction that did the work
  (:func:`insert_report` on that transaction's connection), then posted (:func:`posted`, :func:`post_failed`).

Plus the one path to a CIS tenant's Content studio invitations, ``crm.erase_studio_invitations`` (crm-067, EXECUTE
``crm_eraser`` only, actor ``job:crm_tenant_erase`` only): :func:`studio_invitations` on a caller's connection inside
a savepoint, :func:`without_agency` for a tenant the CRM holds no agency for.

On the DB-module list of ``tests/unit/test_code_rules.py``; only :mod:`app.lifecycle.tenant_erase` calls it. Ids,
codes, hashes, counts and times only.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import asyncpg

from app.errors import Unavailable
from app.lifecycle.saga_rules import Made, SagaRequest, entry
from app.tenancy import as_eraser

ACTOR = "job:crm_tenant_erase"

ENSURE_STATE_SQL = "INSERT INTO crm.tenant_erasure_feed_state (singleton) VALUES (true) ON CONFLICT DO NOTHING"
LOAD_STATE_SQL = "SELECT feed_cursor, failures, last_error_at, last_ok_at FROM crm.tenant_erasure_feed_state"
ADVANCE_SQL = """
UPDATE crm.tenant_erasure_feed_state SET feed_cursor = $2, last_ok_at = now(), failures = 0
 WHERE feed_cursor IS NOT DISTINCT FROM $1
RETURNING 1
"""
SUCCEED_SQL = "UPDATE crm.tenant_erasure_feed_state SET last_ok_at = now(), failures = 0"
FAIL_SQL = """
UPDATE crm.tenant_erasure_feed_state
   SET last_error_class = $1, last_error_at = now(), failures = least(failures + 1, 1000000)
RETURNING failures
"""
UPSERT_SQL = """
INSERT INTO crm.tenant_erasure_saga AS s (request_id, subject_tenant_id, phase, round, step_status, not_before,
                                          approved_plan_hash, approval_expires_at, is_pilot, is_synthetic,
                                          cis_updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
ON CONFLICT (request_id) DO UPDATE SET
    phase = EXCLUDED.phase, round = EXCLUDED.round, step_status = EXCLUDED.step_status,
    not_before = EXCLUDED.not_before, approved_plan_hash = EXCLUDED.approved_plan_hash,
    approval_expires_at = EXCLUDED.approval_expires_at, is_pilot = EXCLUDED.is_pilot,
    is_synthetic = EXCLUDED.is_synthetic, cis_updated_at = EXCLUDED.cis_updated_at
 WHERE s.subject_tenant_id = EXCLUDED.subject_tenant_id AND s.cis_updated_at <= EXCLUDED.cis_updated_at
RETURNING 1
"""
_COLUMNS = (
    "request_id, subject_tenant_id, phase, round, step_status, not_before, approved_plan_hash, approval_expires_at, "
    "is_pilot, is_synthetic, cis_updated_at"
)
# Requests with work: an active step in a working phase, or a report not posted yet. A handful at most.
ACTIONABLE_SQL = f"""
SELECT {_COLUMNS} FROM crm.tenant_erasure_saga s
 WHERE (s.step_status = 'active' AND s.phase IN ('dry_run', 'apply', 'verify'))
    OR EXISTS (SELECT 1 FROM crm.tenant_erasure_report r WHERE r.request_id = s.request_id AND r.post_outcome IS NULL)
 ORDER BY s.cis_updated_at, s.request_id
 LIMIT $1
"""  # noqa: S608 — fixed column list
REQUEST_SQL = f"SELECT {_COLUMNS} FROM crm.tenant_erasure_saga WHERE request_id = $1"  # noqa: S608
MADE_SQL = """
SELECT mode, outcome, post_outcome FROM crm.tenant_erasure_report WHERE request_id = $1 AND round = $2 ORDER BY mode
"""
UNPOSTED_SQL = """
SELECT report_id, body FROM crm.tenant_erasure_report WHERE request_id = $1 AND post_outcome IS NULL
 ORDER BY round, array_position(ARRAY['dry_run', 'apply', 'verify'], mode)
"""
INSERT_REPORT_SQL = """
INSERT INTO crm.tenant_erasure_report (report_id, request_id, round, mode, outcome, error_code, plan_hash, body,
                                       reported_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
ON CONFLICT (request_id, round, mode) DO NOTHING
RETURNING report_id
"""
# The work of one request is serialised (two runs at once: a rolling deploy, a reaped lease, a slot run longer than the
# slot): a transaction-scoped advisory lock, then the round's report of the mode is read again under it.
CLAIM_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('crm_tenant_erase:' || $1::uuid::text, 0))"
HAS_REPORT_SQL = """
SELECT EXISTS (SELECT 1 FROM crm.tenant_erasure_report WHERE request_id = $1 AND round = $2 AND mode = $3) AS made
"""
POSTED_SQL = """
UPDATE crm.tenant_erasure_report
   SET post_outcome = $2, posted_at = CASE WHEN $2 = 'accepted' THEN now() END, post_attempts = post_attempts + 1
 WHERE report_id = $1 AND post_outcome IS NULL
"""
POST_FAILED_SQL = """
UPDATE crm.tenant_erasure_report SET post_attempts = least(post_attempts + 1, 1000000)
 WHERE report_id = $1 AND post_outcome IS NULL
"""
INVITATIONS_SQL = "SELECT crm.erase_studio_invitations($1, $2)"
AGENCY_STATUS_SQL = "SELECT status FROM crm.tenant WHERE tenant_id = $1"
# The census: every crm table with a tenant column and its pii: columns (DM-6.1 comments); the studio invitations
# (keyed by cis_tenant_id) are counted through crm-067's function.
PII_COLUMNS_SQL = """
SELECT c.relname::text AS tbl, array_agg(a.attname::text ORDER BY a.attname) AS cols,
       EXISTS (SELECT 1 FROM pg_catalog.pg_attribute t
                WHERE t.attrelid = c.oid AND t.attname = 'tenant_id' AND NOT t.attisdropped) AS has_tenant
  FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
  JOIN pg_catalog.pg_description d ON d.objoid = c.oid AND d.classoid = 'pg_catalog.pg_class'::regclass
                                  AND d.objsubid = a.attnum
 WHERE c.relnamespace = 'crm'::regnamespace AND c.relkind IN ('r', 'p') AND NOT c.relispartition
   AND d.description LIKE 'pii:%'
 GROUP BY c.oid, c.relname
 ORDER BY c.relname
"""
INVITATION_TABLE = "studio_invitation"


@dataclass(frozen=True)
class FeedState:
    cursor: str | None
    failures: int
    last_error_at: dt.datetime | None
    last_ok_at: dt.datetime | None


def _request(row: Any) -> SagaRequest:
    return SagaRequest(
        request_id=row["request_id"],
        tenant_id=row["subject_tenant_id"],
        phase=row["phase"],
        round=int(row["round"]),
        step_status=row["step_status"],
        not_before=row["not_before"],
        approved_plan_hash=row["approved_plan_hash"],
        approval_expires_at=row["approval_expires_at"],
        is_pilot=bool(row["is_pilot"]),
        is_synthetic=bool(row["is_synthetic"]),
        updated_at=row["cis_updated_at"],
    )


# ── feed state ───────────────────────────────────────────────────────────────────────────────────────────────────
async def load_state(pool: asyncpg.Pool) -> FeedState:
    await pool.execute(ENSURE_STATE_SQL)
    row = await pool.fetchrow(LOAD_STATE_SQL)
    return FeedState(row["feed_cursor"], int(row["failures"]), row["last_error_at"], row["last_ok_at"])


async def advance(pool: asyncpg.Pool, old: str | None, new: str) -> bool:
    """Compare and set: True when the cursor moved from ``old`` (the value this run read) to ``new``."""
    return await pool.fetchval(ADVANCE_SQL, old, new) is not None


async def succeed(pool: asyncpg.Pool) -> None:
    await pool.execute(SUCCEED_SQL)


async def fail(pool: asyncpg.Pool, error_class: str) -> int:
    return int(await pool.fetchval(FAIL_SQL, error_class) or 0)


# ── requests and reports ─────────────────────────────────────────────────────────────────────────────────────────
async def upsert_request(pool: asyncpg.Pool, req: SagaRequest) -> bool:
    """Store the feed's state of a request; False when a newer state is stored already (or the tenant differs)."""
    return (
        await pool.fetchval(
            UPSERT_SQL,
            req.request_id,
            req.tenant_id,
            req.phase,
            req.round,
            req.step_status,
            req.not_before,
            req.approved_plan_hash,
            req.approval_expires_at,
            req.is_pilot,
            req.is_synthetic,
            req.updated_at,
        )
        is not None
    )


async def actionable(pool: asyncpg.Pool, limit: int) -> list[SagaRequest]:
    return [_request(r) for r in await pool.fetch(ACTIONABLE_SQL, limit)]


async def request(pool: asyncpg.Pool, request_id: uuid.UUID) -> SagaRequest | None:
    row = await pool.fetchrow(REQUEST_SQL, request_id)
    return None if row is None else _request(row)


async def made(pool: asyncpg.Pool, request_id: uuid.UUID, round_: int) -> list[Made]:
    return [Made(r["mode"], r["outcome"], r["post_outcome"]) for r in await pool.fetch(MADE_SQL, request_id, round_)]


async def unposted(pool: asyncpg.Pool, request_id: uuid.UUID) -> list[tuple[uuid.UUID, dict[str, Any]]]:
    out = []
    for r in await pool.fetch(UNPOSTED_SQL, request_id):
        body = r["body"]
        out.append((r["report_id"], json.loads(body) if isinstance(body, str) else dict(body)))
    return out


async def insert_report(conn: asyncpg.Connection | asyncpg.Pool, body: dict[str, Any]) -> bool:
    """Record a report (a ``tenant.erasure.v1`` ErasureReport). Pass the connection of the transaction that did the
    work, so the record commits with it. False when this request, round and mode has a report already."""
    return (
        await conn.fetchval(
            INSERT_REPORT_SQL,
            uuid.UUID(body["report_id"]),
            uuid.UUID(body["request_id"]),
            int(body["round"]),
            body["mode"],
            body["status"],
            body["error_code"],
            body["plan_hash"],
            json.dumps(body, sort_keys=True),
            dt.datetime.fromisoformat(body["reported_at"].replace("Z", "+00:00")),
        )
        is not None
    )


async def claim(conn: asyncpg.Connection, request_id: uuid.UUID, round_: int, mode: str) -> bool:
    """Take the request's work lock on ``conn`` (held to the end of its transaction) and read the record again under it:
    False when this request, round and mode has a report already (another run made it; it is posted from the record)."""
    await conn.execute(CLAIM_SQL, request_id)
    return not await conn.fetchval(HAS_REPORT_SQL, request_id, round_, mode)


async def posted(pool: asyncpg.Pool, report_id: uuid.UUID, outcome: str) -> None:
    """CIS answered: ``accepted`` or ``refused:<code>`` (settled, never posted again)."""
    await pool.execute(POSTED_SQL, report_id, outcome)


async def post_failed(pool: asyncpg.Pool, report_id: uuid.UUID) -> None:
    """CIS was not reached or answered 401 / 429 / 5xx: the next run posts the same body again."""
    await pool.execute(POST_FAILED_SQL, report_id)


# ── the studio invitations of a CIS tenant (crm-067) ─────────────────────────────────────────────────────────────
async def studio_invitations(conn: asyncpg.Connection, tenant_id: uuid.UUID, *, dry_run: bool) -> int:
    """Count (dry run) or delete the tenant's crm.studio_invitation rows on ``conn`` (actor job:crm_tenant_erase set
    by the caller), as crm_eraser inside a savepoint (a failure rolls the role back too)."""
    # as_eraser needs only the connection and the reader role of a context: a tenant may have no agency (no context)
    async with as_eraser(SimpleNamespace(conn=conn, reader_role=None)):  # type: ignore[arg-type]
        rows = await conn.fetchval(INVITATIONS_SQL, tenant_id, dry_run)
    return int(rows or 0)


async def without_agency(
    pool: asyncpg.Pool,
    tenant_id: uuid.UUID,
    work: Callable[[asyncpg.Connection], Awaitable[dict[str, Any] | None]],
) -> dict[str, Any] | None:
    """One transaction outside an agency context: ``app.actor`` = job:crm_tenant_erase, ``app.tenant_id`` = the tenant
    (FORCE RLS: a census reads its rows only; a tenant without an agency has none). For a tenant the CRM holds no
    agency for, and for the report of a refused apply (the apply's own transaction rolled back). ``work`` gets the
    connection (for :func:`studio_invitations`, :func:`insert_report` and the census)."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.actor', $1, true)", ACTOR)
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        return await work(conn)


async def agency_status(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> str | None:
    """The status of the tenant's CRM agency, None when the CRM holds none (crm.tenant: FORCE RLS on app.tenant_id)."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        return await conn.fetchval(AGENCY_STATUS_SQL, tenant_id)


# ── the census (verify) ──────────────────────────────────────────────────────────────────────────────────────────
async def census(conn: asyncpg.Connection, tenant_id: uuid.UUID) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """``({pii_columns_checked, pii_rows_found}, the tables with rows left)`` read on ``conn`` (``app.tenant_id`` set by
    the caller: FORCE RLS shows the tenant's rows only): every ``pii:`` column (DM-6.1) of every crm table with a
    tenant column, and the tenant's studio invitations. Raises :class:`Unavailable` ``census_incomplete`` when a table
    cannot be read (the report says so; a table is never skipped)."""
    checked = found = 0
    left: list[dict[str, Any]] = []
    pii = {r["tbl"]: (list(r["cols"]), bool(r["has_tenant"])) for r in await conn.fetch(PII_COLUMNS_SQL)}
    for table, (cols, has_tenant) in sorted(pii.items()):
        if not has_tenant:
            continue
        where = " OR ".join(f'"{col}" IS NOT NULL' for col in cols)
        try:
            async with conn.transaction():
                rows = int(await conn.fetchval(f'SELECT count(*) FROM crm."{table}" WHERE {where}') or 0)  # noqa: S608
        except asyncpg.PostgresError:
            raise Unavailable("census_incomplete", f"crm.{table} could not be read") from None
        checked += len(cols)
        found += rows
        if rows:
            left.append(entry(f"crm.{table}", "delete", rows))
    invitations = await studio_invitations(conn, tenant_id, dry_run=True)
    checked += len(pii.get(INVITATION_TABLE, ((), False))[0])
    found += invitations
    if invitations:
        left.append(entry(f"crm.{INVITATION_TABLE}", "delete", invitations))
    return {"pii_columns_checked": checked, "pii_rows_found": found}, left
