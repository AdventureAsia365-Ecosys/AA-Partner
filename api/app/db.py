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
    "crm-024",
    "crm-025",  # AA-955 e-sign v1 (crm-025 needs crm-024: DB Migrate applies them in order)
    "crm-026",  # AA-967 portability (crm-026 needs crm-025)
    "crm-027",  # AA-961 duty of care + calendar (incident flags, ICS feeds; needs crm-026)
    "crm-028",  # AA-952 slice 4 invoice claim / hold extension / re-hold / reminders (expand, same PR)
    "crm-029",  # AA-953 portal per person (crm.traveler_flight, otp_challenge.sent_to_bidx; expand, needs crm-028)
    "crm-030",  # AA-962 onboarding + import (expand: import refs, onboarding / controller settings columns)
    "crm-031",  # AA-966 trip-level insight fact (expand: auto-applied after crm-028..030 in number order)
    "crm-032",  # AA-964 CRM AI gateway: llm_call ledger v2, guard role, AI budget alert state (contract)
    "crm-033",  # AA-971 AA support access L2 + break-glass L3: grants, sessions, reader columns (contract)
    "crm-034",  # AA-956 Module 8 ops release (expand: crm.ops_release, crm.supplier_manifest_export; after crm-033)
    "crm-035",  # AA-957 commission + one-action cancel (contract: crm.commission, crm.trip_cancellation, cancel kind)
    "crm-036",  # AA-958 SMS / WhatsApp (contract: messaging_connection, channel_template, message_delivery, window)
    "crm-037",  # AA-964 S263 F1/F2: AI stage profiles + routes, ledger v2 statuses on crm.llm_call (contract)
    "crm-038",  # AA-959 workflows + tasks (contract: workflow_rule, workflow_run, task title / client / run, digest)
    "crm-039",  # AA-939 follow-up: the partner-account clean-up reads crm.studio_invitation (contract: policy)
    "crm-040",  # AA-1027 F3: crm.ai_draft, the AI drafts of the trip workspace (expand: new table + guard)
    "crm-041",  # AA-1025 H1c: crm.llm_app_profile, the CRM app profile (contract: MIG-4, applied before this code)
    # crm-042 (AA-1027 F4 route data) is not read by code. crm-043: AA-1028 R1 slice 2, crm.entitlement_feed_state and
    # crm.tenant_entitlement.projected_at (contract: MIG-4, applied before this code; needs crm-042 in number order).
    "crm-042",
    "crm-043",
    "crm-044",  # AA-969 d16: crm.tenant.is_pilot, the CIS pilot marker (contract: MIG-4, applied before this code)
    # crm-045: AA-1035 R2 close-out, the kept address of crm.studio_invitation (app.members.studio reads and writes it;
    # contract: MIG-4, applied before this code). Deploy Dev schema-gate also requires every migration file below it
    # (crm-044 of another lane of the wave, when it is on main).
    "crm-045",
    "crm-046",  # AA-1030: crm.tenant_settings.retail_markup_pct (contract: MIG-4, applied before this code)
    "crm-047",  # AA-1038: crm.tenant_entitlement.tp_crm_leads (contract: MIG-4, applied before this code)
    "crm-048",  # AA-1066: retail pricing (option_price, retail_price_rule, discount_approval; contract: MIG-4)
    "crm-049",  # AA-1067: cancellation policy + charge tables (expand, shipped with crm-050)
    "crm-050",  # AA-1067: payment schedule instalments, plan, policy pin, invoice kinds (contract: MIG-4)
    "crm-051",  # AA-1068: CRM finance (v_trip_pnl, supplier bills, AA invoice copy, departure costs; contract: MIG-4)
    "crm-052",  # AA-1069: the agency service catalogue (expand, shipped with crm-053)
    "crm-053",  # AA-1069: supplier booking requests (line status requested, supplier threads; contract: MIG-4)
    "crm-054",  # AA-1070: travel ops tables (departure state, vouchers, rooming, visas; expand, shipped with crm-055)
    "crm-055",  # AA-1070: passport months, PDF document kinds, visa status moves (contract: MIG-4)
    "crm-058",  # AA-1085: legal-hold guards read the holds of the row's own agency (contract: MIG-4)
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
        """DB answers ``SELECT 1`` and every required ``crm-NNN`` is registered.

        Once the schema read succeeds the body also lists ``registered_keys``: every ``crm-NNN`` registered on this
        database, sorted (AA-993 M4a, root ADR 0017 d7), for the Deploy Dev ``schema-gate`` (M4b) to see every key up
        to the one new code requires (``missing_schema_versions`` stays the fallback for an older task)."""
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
        registered = sorted(registered or [])
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
                    "registered_keys": registered,
                },
            )
        # AA-978: the CIS membership feed in one word (ok / pending / stale / failing / not_configured / unknown). It
        # never fails readiness (a CIS outage must not roll a deploy back); the ops check membership_feed_stuck alerts
        # AA ops.
        feed_configured = bool(self._settings.cis_api_url and self._settings.m2m_client)
        feed = await feed_state.health(pool, configured=feed_configured)
        # AA-1028: the CIS account feed (crm_entitlement_pull), the same words; never fails readiness either.
        account_feed = await feed_state.health(pool, configured=feed_configured, table=feed_state.ENTITLEMENT)
        return Readiness(
            True,
            {
                "status": "ok",
                "db": "ok",
                "schema_version": version,
                "registered_keys": registered,
                "membership_feed": feed,
                "entitlement_feed": account_feed,
            },
        )


async def request_pool(request: Request) -> asyncpg.Pool:
    """The app's pool for a route, or 503 ``database_not_configured`` (never a connection attempt without a DSN)."""
    db: Database = request.app.state.db
    if not db.configured:
        raise HTTPException(status_code=503, detail="database_not_configured")
    return await db.pool()
