"""Real-PostgreSQL fixtures. Needs CRM_TEST_ADMIN_DSN; every test here is skipped without it (CI fails on a skip).

The admin DSN must log in as ``aa_cis_admin`` (the role DB Migrate logs in as on Dev) and point at a throwaway
database whose name ends in ``_test`` or ``_ci``: the setup drops schema ``crm`` there. Run
``python -m tests.integration.bootstrap_roles <superuser DSN>`` once per database first (the RDS role shape).

Every migration file runs the way AA-CIS-Infra ``db/run_migration.py`` runs it for owner ``crm``: the whole file in one
simple-protocol call, as ``crm_owner`` (``SET ROLE``: the live owner of the anchor schema crm, XD8). Afterwards the
login role must own nothing in schema crm (the runner's ownership post-check).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

ADMIN_DSN = os.environ.get("CRM_TEST_ADMIN_DSN", "")
MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "migrations").glob("*.sql"))
MASTER = "aa_cis_admin"
OWNER = "crm_owner"
APP_ROLE = "crm_app"
READERS = ("crm_ai_reader", "crm_insights", "crm_support_reader", "crm_eraser")
# CI-only value for the step Nghiệp does out of band on Dev (the crm_app password, DB Migrate set-password).
CRM_TEST_PASSWORD = "ci-only-not-a-real-password"  # noqa: S105

# The parts of the shared schema crm migrations touch (CIS owns them on Dev; CIS 269 grants crm_owner its rights).
STUB_SHARED = """
CREATE SCHEMA IF NOT EXISTS shared;
CREATE TABLE IF NOT EXISTS shared.schema_versions (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    description TEXT
);
GRANT USAGE ON SCHEMA shared TO crm_owner;
GRANT SELECT, INSERT ON shared.schema_versions TO crm_owner;
"""

STRAY_SQL = """
SELECT c.relname AS name FROM pg_class c
 WHERE c.relnamespace = 'crm'::regnamespace AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = session_user)
UNION ALL
SELECT p.proname FROM pg_proc p
 WHERE p.pronamespace = 'crm'::regnamespace AND p.proowner = (SELECT oid FROM pg_roles WHERE rolname = session_user)"""


def require_admin_dsn() -> str:
    if not ADMIN_DSN:
        pytest.skip("CRM_TEST_ADMIN_DSN not set")
    parts = urlsplit(ADMIN_DSN)
    database = parts.path.lstrip("/")
    if not (database.endswith("_test") or database.endswith("_ci")):
        pytest.fail(f"refusing to reset database {database!r}: name must end in _test or _ci")
    if parts.username != MASTER:
        pytest.fail(f"CRM_TEST_ADMIN_DSN must log in as {MASTER} (the DB Migrate identity)")
    return ADMIN_DSN


def app_dsn(admin_dsn: str) -> str:
    parts = urlsplit(admin_dsn)
    host = parts.hostname or "localhost"
    netloc = f"{APP_ROLE}:{CRM_TEST_PASSWORD}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


EXPAND_HEADER = "-- migrate: expand"


async def apply_migrations(conn: asyncpg.Connection) -> None:
    """Every crm-NNN file in order, as crm_owner, then the ownership post-check. An expand file (root ADR 0017: no
    DROP, no CREATE OR REPLACE, no DO block, so it cannot be written to run twice) that is already registered is
    skipped, as DB Migrate never applies a registered file again; every other file runs again as a no-op."""
    for path in MIGRATIONS:
        text = path.read_text(encoding="utf-8")
        if text.startswith(EXPAND_HEADER) and await conn.fetchval(
            "SELECT 1 FROM shared.schema_versions WHERE version = $1", f"crm-{path.name[:3]}"
        ):
            continue
        await conn.execute(f"SET ROLE {OWNER}")
        try:
            await conn.execute(text)
        finally:
            await conn.execute("RESET ROLE")
        stray = [r["name"] for r in await conn.fetch(STRAY_SQL)]
        assert not stray, f"{path.name} left objects owned by the login role: {stray}"


async def _reset_and_migrate(admin_dsn: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        shape = await conn.fetchrow(
            "SELECT rolsuper, rolcreaterole, pg_has_role(current_user, $1, 'MEMBER') AS owner_member "
            "FROM pg_roles WHERE rolname = current_user",
            OWNER,
        )
        if dict(shape) != {"rolsuper": False, "rolcreaterole": True, "owner_member": True}:
            pytest.fail(f"{MASTER} does not have the RDS shape ({dict(shape)}): run tests.integration.bootstrap_roles")
        await conn.execute("DROP SCHEMA IF EXISTS crm CASCADE")
        # Infra rl-003: the master creates the EMPTY schema crm owned by crm_owner.
        await conn.execute(f"CREATE SCHEMA crm AUTHORIZATION {OWNER}")
        await conn.execute(STUB_SHARED)
        await conn.execute("DELETE FROM shared.schema_versions WHERE version LIKE 'crm-%'")
        await apply_migrations(conn)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def migrated_admin_dsn() -> str:
    dsn = require_admin_dsn()
    asyncio.run(_reset_and_migrate(dsn))
    return dsn


@pytest.fixture(scope="session")
def migrated_app_dsn(migrated_admin_dsn: str) -> str:
    return app_dsn(migrated_admin_dsn)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def app_pool(migrated_app_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(migrated_app_dsn, min_size=0, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()
