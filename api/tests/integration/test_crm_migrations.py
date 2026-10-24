"""Every crm-NNN file on PostgreSQL 15 with the RDS role shape: keys, idempotency, ownership, privileges, readiness."""

from __future__ import annotations

import asyncpg
import pytest

from app.db import REQUIRED_SCHEMA_VERSIONS, Database
from app.settings import Settings
from tests.integration.conftest import MIGRATIONS, OWNER, apply_migrations

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_every_file_registers_its_key(admin: asyncpg.Connection) -> None:
    keys = [
        r["version"]
        for r in await admin.fetch("SELECT version FROM shared.schema_versions WHERE version LIKE 'crm-%' ORDER BY 1")
    ]
    assert keys == [f"crm-{p.name[:3]}" for p in MIGRATIONS]
    assert set(REQUIRED_SCHEMA_VERSIONS) <= set(keys)


async def test_reapply_is_a_no_op(admin: asyncpg.Connection) -> None:
    before = await admin.fetchval("SELECT count(*) FROM crm.status_transition")
    await apply_migrations(admin)
    assert await admin.fetchval("SELECT count(*) FROM crm.status_transition") == before
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version LIKE 'crm-%'") == len(
        MIGRATIONS
    )


async def test_crm_owner_owns_everything(admin: asyncpg.Connection) -> None:
    owners = await admin.fetch(
        "SELECT DISTINCT pg_get_userbyid(relowner) AS o FROM pg_class WHERE relnamespace = 'crm'::regnamespace "
        "UNION SELECT DISTINCT pg_get_userbyid(proowner) FROM pg_proc WHERE pronamespace = 'crm'::regnamespace"
    )
    assert {r["o"] for r in owners} == {OWNER}


async def test_crm002_refuses_the_wrong_role(admin: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.RaiseError, match="must run as crm_owner"):
        await admin.execute(MIGRATIONS[1].read_text(encoding="utf-8"))


async def test_crm004_refuses_to_run_before_the_crm003_vault(admin: asyncpg.Connection) -> None:
    # crm-004 (AA-935 directory) was drafted as crm-003 and renumbered behind the AA-934 vault: Guard 0 needs crm-003.
    crm004 = next(p for p in MIGRATIONS if p.name.startswith("004_"))
    tx = admin.transaction()
    await tx.start()
    try:
        await admin.execute("DELETE FROM shared.schema_versions WHERE version = 'crm-003'")
        await admin.execute(f"SET LOCAL ROLE {OWNER}")
        with pytest.raises(asyncpg.RaiseError, match="crm-003 is not registered"):
            await admin.execute(crm004.read_text(encoding="utf-8"))
    finally:
        await tx.rollback()
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-003'") == 1


async def test_runtime_privileges(admin: asyncpg.Connection) -> None:
    def priv(role: str, rel: str, p: str) -> str:
        return f"has_table_privilege('{role}', '{rel}', '{p}')"

    row = await admin.fetchrow(
        f"SELECT {priv('crm_app', 'crm.v_schema_versions', 'SELECT')} AS view_read, "
        f"{priv('crm_app', 'crm.v_schema_versions', 'INSERT')} AS view_insert, "
        f"{priv('crm_app', 'crm.status_transition', 'SELECT')} AS st_read, "
        f"{priv('crm_app', 'crm.status_transition', 'INSERT')} AS st_insert, "
        f"{priv('crm_app', 'crm.member', 'DELETE')} AS member_dml, "
        f"{priv('crm_app', 'crm.member', 'TRUNCATE')} AS member_truncate, "
        f"{priv('crm_ai_reader', 'crm.member', 'SELECT')} AS reader_member, "
        "has_schema_privilege('crm_app', 'shared', 'USAGE') AS app_shared, "
        "has_schema_privilege('cis_app', 'crm', 'USAGE') AS cis_crm, "
        "has_schema_privilege('booking', 'crm', 'USAGE') AS booking_crm, "
        "has_function_privilege('public', 'crm.enforce_status_transition()', 'EXECUTE') AS public_fn"
    )
    assert dict(row) == {
        "view_read": True,
        "view_insert": False,
        "st_read": True,
        "st_insert": False,
        "member_dml": True,
        "member_truncate": False,
        "reader_member": False,
        "app_shared": False,
        "cis_crm": False,
        "booking_crm": False,
        "public_fn": False,
    }


async def test_crm_app_cannot_write_schema_versions_through_the_view(migrated_app_dsn: str) -> None:
    conn = await asyncpg.connect(migrated_app_dsn)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("INSERT INTO crm.v_schema_versions (version, applied_at) VALUES ('crm-999', now())")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.fetchval("SELECT count(*) FROM shared.schema_versions")
    finally:
        await conn.close()


async def test_readiness_as_crm_app(migrated_app_dsn: str) -> None:
    db = Database(Settings(database_url=migrated_app_dsn, db_ssl="disable", environment="test"))
    try:
        result = await db.readiness()
    finally:
        await db.close()
    assert result.ok, result.body
    # Readiness reports the newest registered key: the newest migration file, which may be ahead of the code's
    # requirement while a migration lands before the code that reads it (D8 / MIG-4).
    newest = f"crm-{MIGRATIONS[-1].name[:3]}"
    assert newest >= max(REQUIRED_SCHEMA_VERSIONS)
    assert result.body == {"status": "ok", "db": "ok", "schema_version": newest}
