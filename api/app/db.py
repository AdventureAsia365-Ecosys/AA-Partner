"""Database access: a lazy asyncpg pool and the readiness probe.

The pool is created on first use, not at startup, so the process (and the ALB health check on ``/crm/health``) stays
up while the database is unreachable (same rule as AA-Booking, root ADR 0004 decision 9).

Connection budget (data standards C2.2, AA-CIS-Infra ``db/connection_budget.json`` row ``crm-api``): role ``crm_app``
has CONNECTION LIMIT 6 = pool ``max_size=3`` x 2 tasks during a rolling deploy. Infra's
``check_connection_budget.py --verify-sources`` reads the first ``max_size=`` after ``create_pool(`` in this file, so
keep it a literal and change both repos together.

Request code never uses the pool directly: tenant data goes through :func:`app.tenancy.tenant_tx`, job rows through
``app.jobs.queue`` (a unit test checks the call sites).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from fastapi import HTTPException, Request

from app.cis import feed_state
from app.settings import Settings

log = logging.getLogger(__name__)

# Every crm-NNN migration this code needs. Readiness fails until ALL of them are registered.
REQUIRED_SCHEMA_VERSIONS: tuple[str, ...] = (
    "crm-001",
    "crm-002",
    "crm-003",
    "crm-004",
    "crm-005",
    "crm-006",
    "crm-007",
    "crm-008",
    "crm-009",
    "crm-010",
    "crm-011",
    "crm-012",
    "crm-013",
    "crm-014",
    "crm-015",
    "crm-016",
    "crm-017",
    "crm-018",
    "crm-019",
    "crm-020",
    "crm-021",
    "crm-022",
    "crm-023",
)
REQUIRED_SCHEMA_VERSION = max(REQUIRED_SCHEMA_VERSIONS)

READY_SQL = "SELECT coalesce(array_agg(version ORDER BY version), '{}') FROM crm.v_schema_versions"

CONNECT_TIMEOUT_S = 5.0
PROBE_TIMEOUT_S = 5.0
# Server side, in milliseconds (asyncpg passes server settings as strings). Two minutes: an S3 call inside the audit
# export transaction leaves the session idle for well under that.
IDLE_IN_TX_TIMEOUT_MS = "120000"


@dataclass(frozen=True)
class Readiness:
    ok: bool
    body: dict[str, Any] = field(default_factory=dict)


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self._settings.database_url is not None

    async def pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    dsn=self._settings.database_url,
                    ssl=self._settings.db_ssl,
                    min_size=0,
                    max_size=3,
                    timeout=CONNECT_TIMEOUT_S,
                    command_timeout=PROBE_TIMEOUT_S,
                    # Named pool on the shared RDS (pg_stat_activity, data standards C2.2). AA-940: a transaction left
                    # idle ends after IDLE_IN_TX_TIMEOUT_MS, far below the audit export's 15-minute read lag, so an
                    # audit row is never committed after the export already read its hour (it would be missing from
                    # the archive and fail verification for good).
                    server_settings={
                        "application_name": "crm-api",
                        "idle_in_transaction_session_timeout": IDLE_IN_TX_TIMEOUT_MS,
                    },
                )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def readiness(self) -> Readiness:
        """DB answers ``SELECT 1`` and every required ``crm-NNN`` is registered."""
        if not self.configured:
            return Readiness(False, {"status": "not_ready", "db": "not_configured", "schema_version": None})
        try:
            pool = await self.pool()
            async with pool.acquire() as conn:
                await asyncio.wait_for(conn.fetchval("SELECT 1"), PROBE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — any failure means "not ready"; never leak the message
            log.warning("readiness: database unreachable (%s)", type(exc).__name__)
            return Readiness(
                False,
                {"status": "not_ready", "db": "error", "error": type(exc).__name__, "schema_version": None},
            )
        try:
            async with pool.acquire() as conn:
                registered = await asyncio.wait_for(
                    conn.fetchval(READY_SQL),
                    PROBE_TIMEOUT_S,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("readiness: schema check failed (%s)", type(exc).__name__)
            return Readiness(
                False,
                {"status": "not_ready", "db": "ok", "error": type(exc).__name__, "schema_version": None},
            )
        registered = list(registered or [])
        version = max(registered) if registered else None
        missing = [v for v in REQUIRED_SCHEMA_VERSIONS if v not in registered]
        if missing:
            return Readiness(
                False,
                {
                    "status": "not_ready",
                    "db": "ok",
                    "schema_version": version,
                    "required_schema_version": REQUIRED_SCHEMA_VERSION,
                    "missing_schema_versions": missing,
                },
            )
        # AA-978: the CIS membership feed in one word (ok / pending / stale / failing / not_configured / unknown). It
        # never fails readiness (a CIS outage must not roll a deploy back); the alarm keys on crm_membership_feed_stuck.
        feed_configured = bool(self._settings.cis_api_url and self._settings.m2m_client)
        feed = await feed_state.health(pool, configured=feed_configured)
        return Readiness(True, {"status": "ok", "db": "ok", "schema_version": version, "membership_feed": feed})


async def request_pool(request: Request) -> asyncpg.Pool:
    """The app's pool for a route, or 503 ``database_not_configured`` (never a connection attempt without a DSN)."""
    db: Database = request.app.state.db
    if not db.configured:
        raise HTTPException(status_code=503, detail="database_not_configured")
    return await db.pool()
