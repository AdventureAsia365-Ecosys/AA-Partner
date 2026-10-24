"""crm-011 (AA-941, card CRM-13a) on PostgreSQL 15 with the RDS role shape: the L1 console's schema rules, raw SQL.

- ``crm.tenant_entitlement.insights_opt_out`` (Q2 3.12): default false; a member write is refused by the crm-008
  platform-only trigger (CRM04); a platform write passes.
- AA-raised data requests on ``crm.erasure_request`` (``purpose``, ``owner_accepted_at``) and their guard (CRM18): AA
  inserts only an unconfirmed agency request; only the owner confirms it, once; an unconfirmed AA request only moves to
  cancelled; export_only never erases; AA never confirms or runs a request; nobody forges an AA request.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTPARTN"
MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "011_crm_l1_console.sql"
INSERT_AA = """
INSERT INTO crm.erasure_request (scope, subject_id, purpose) VALUES ('tenant', $1, $2) RETURNING request_id
"""


@asynccontextmanager
async def ctx(
    pool: asyncpg.Pool, tenant: uuid.UUID, actor: str = "", role: str = ""
) -> AsyncIterator[asyncpg.Connection]:
    """One transaction as crm_app with the settings tenant_tx() / platform_tx() would set."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        if actor:
            await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        if role:
            await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        yield conn


async def _agency(pool: asyncpg.Pool) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant = uuid.uuid4()
    async with ctx(pool, tenant, actor="seed:test") as conn:
        await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant)
        ids = []
        for role in ("owner", "agent"):
            ids.append(
                await conn.fetchval(
                    "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, $3, 'active') "
                    "RETURNING member_id",
                    ISS,
                    f"sub-{uuid.uuid4()}",
                    role,
                )
            )
    return tenant, ids[0], ids[1]


async def _raise(pool: asyncpg.Pool, tenant: uuid.UUID, purpose: str = "offboard") -> uuid.UUID:
    async with ctx(pool, tenant, actor="platform:staff-1") as conn:
        return await conn.fetchval(INSERT_AA, tenant, purpose)


async def _refused(pool: asyncpg.Pool, tenant: uuid.UUID, actor: str, role: str, sql: str, *args: object) -> str:
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(pool, tenant, actor, role) as conn:
            await conn.execute(sql, *args)
    return getattr(exc.value, "sqlstate", "")


async def test_crm011_is_registered_and_the_columns_have_their_defaults(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-011'") == 1
    cols = {
        (r["table_name"], r["column_name"]): (r["data_type"], r["is_nullable"], r["column_default"])
        for r in await admin.fetch(
            "SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'crm' AND (table_name, column_name) IN "
            "(('tenant_entitlement', 'insights_opt_out'), ('erasure_request', 'purpose'), "
            "('erasure_request', 'owner_accepted_at'))"
        )
    }
    assert cols[("tenant_entitlement", "insights_opt_out")] == ("boolean", "NO", "false")
    assert cols[("erasure_request", "purpose")] == ("text", "NO", "'offboard'::text")
    assert cols[("erasure_request", "owner_accepted_at")][:2] == ("timestamp with time zone", "YES")


async def test_crm011_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    before = await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version LIKE 'crm-%'")
    await admin.execute("SET ROLE crm_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version LIKE 'crm-%'") == before


async def test_the_insights_opt_out_is_written_by_aa_only(app_pool: asyncpg.Pool) -> None:
    tenant, owner, _agent = await _agency(app_pool)
    upsert = (
        "INSERT INTO crm.tenant_entitlement (insights_opt_out) VALUES ($1) "
        "ON CONFLICT (tenant_id) DO UPDATE SET insights_opt_out = EXCLUDED.insights_opt_out"
    )
    assert await _refused(app_pool, tenant, f"member:{owner}", "owner", upsert, True) == "CRM04"
    async with ctx(app_pool, tenant, "platform:staff-1") as conn:
        await conn.execute(upsert, True)
        assert await conn.fetchval("SELECT insights_opt_out FROM crm.tenant_entitlement") is True
    sql = "UPDATE crm.tenant_entitlement SET insights_opt_out = false"
    assert await _refused(app_pool, tenant, f"member:{owner}", "owner", sql) == "CRM04"
    async with ctx(app_pool, tenant, "platform:staff-1") as conn:
        await conn.execute(sql)
        # Default for a row created without the column: included.
    other, _o, _a = await _agency(app_pool)
    async with ctx(app_pool, other, "platform:staff-1") as conn:
        await conn.execute("INSERT INTO crm.tenant_entitlement (plan) VALUES ('free')")
        assert await conn.fetchval("SELECT insights_opt_out FROM crm.tenant_entitlement") is False


async def test_aa_inserts_only_an_unconfirmed_agency_request(app_pool: asyncpg.Pool) -> None:
    tenant, owner, _agent = await _agency(app_pool)
    for sql, args in (
        (
            "INSERT INTO crm.erasure_request (scope, subject_id, owner_accepted_at) VALUES ('tenant', $1, now())",
            (tenant,),
        ),
        ("INSERT INTO crm.erasure_request (scope, subject_id, export_waived) VALUES ('tenant', $1, true)", (tenant,)),
        ("INSERT INTO crm.erasure_request (scope, subject_id, status) VALUES ('tenant', $1, 'erasing')", (tenant,)),
        (
            "INSERT INTO crm.erasure_request (scope, subject_id, confirmed_by) VALUES ('tenant', $1, 'platform:x')",
            (tenant,),
        ),
        ("INSERT INTO crm.erasure_request (scope, subject_id) VALUES ('traveler', $1)", (uuid.uuid4(),)),
        (
            "INSERT INTO crm.erasure_request (scope, subject_id, requested_by) VALUES ('tenant', $1, 'member:x')",
            (tenant,),
        ),
    ):
        assert await _refused(app_pool, tenant, "platform:staff-1", "", sql, *args) == "CRM18", sql
    # A member never forges an AA request, and never inserts one already accepted.
    forged = "INSERT INTO crm.erasure_request (scope, subject_id, requested_by) VALUES ('tenant', $1, 'platform:x')"
    assert await _refused(app_pool, tenant, f"member:{owner}", "owner", forged, tenant) == "CRM18"
    accepted = "INSERT INTO crm.erasure_request (scope, subject_id, owner_accepted_at) VALUES ('tenant', $1, now())"
    assert await _refused(app_pool, tenant, f"member:{owner}", "owner", accepted, tenant) == "CRM18"
    # export_only exists for an agency request only (CHECK).
    with pytest.raises(asyncpg.CheckViolationError):
        async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
            await conn.execute(
                "INSERT INTO crm.erasure_request (scope, subject_id, purpose) VALUES ('traveler', $1, 'export_only')",
                uuid.uuid4(),
            )
    request_id = await _raise(app_pool, tenant)
    async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
        row = await conn.fetchrow(
            "SELECT purpose, requested_by, owner_accepted_at, status FROM crm.erasure_request WHERE request_id = $1",
            request_id,
        )
    assert dict(row) == {
        "purpose": "offboard",
        "requested_by": "platform:staff-1",
        "owner_accepted_at": None,
        "status": "requested",
    }


async def test_only_the_owner_confirms_an_aa_request_once(app_pool: asyncpg.Pool) -> None:
    tenant, owner, agent = await _agency(app_pool)
    request_id = await _raise(app_pool, tenant)
    accept = "UPDATE crm.erasure_request SET owner_accepted_at = now() WHERE request_id = $1"
    assert await _refused(app_pool, tenant, f"member:{agent}", "agent", accept, request_id) == "CRM18"
    assert await _refused(app_pool, tenant, "platform:staff-1", "", accept, request_id) == "CRM18"
    assert await _refused(app_pool, tenant, "job:crm_offboard", "", accept, request_id) == "CRM18"
    # Accept and move a status in one statement: refused (the accept moves nothing else's state).
    both = "UPDATE crm.erasure_request SET owner_accepted_at = now(), status = 'export_ready' WHERE request_id = $1"
    assert await _refused(app_pool, tenant, f"member:{owner}", "owner", both, request_id) == "CRM18"
    async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
        await conn.execute(
            "UPDATE crm.erasure_request SET owner_accepted_at = now(), export_waived = true, dry_run_counts = $2 "
            "WHERE request_id = $1",
            request_id,
            json.dumps({"trip": 1}),
        )
    again = "UPDATE crm.erasure_request SET owner_accepted_at = now() + interval '1 hour' WHERE request_id = $1"
    assert await _refused(app_pool, tenant, f"member:{owner}", "owner", again, request_id) == "CRM18"
    fixed = "UPDATE crm.erasure_request SET purpose = 'export_only' WHERE request_id = $1"
    assert await _refused(app_pool, tenant, f"member:{owner}", "owner", fixed, request_id) == "CRM18"
    who = "UPDATE crm.erasure_request SET requested_by = 'member:x' WHERE request_id = $1"
    assert await _refused(app_pool, tenant, f"member:{owner}", "owner", who, request_id) == "CRM18"
    # Confirmed: the owner flow goes on as in AA-942 (the owner confirms the erase).
    async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
        await conn.execute(
            "UPDATE crm.erasure_request SET status = 'erasing', confirmed_by = $2 WHERE request_id = $1",
            request_id,
            f"member:{owner}",
        )


async def test_an_unconfirmed_aa_request_only_gets_cancelled(app_pool: asyncpg.Pool) -> None:
    tenant, owner, _agent = await _agency(app_pool)
    request_id = await _raise(app_pool, tenant)
    for sql in (
        "UPDATE crm.erasure_request SET status = 'export_ready', export_object_key = NULL WHERE request_id = $1",
        "UPDATE crm.erasure_request SET status = 'erasing', confirmed_by = 'member:x' WHERE request_id = $1",
        "UPDATE crm.erasure_request SET export_waived = true WHERE request_id = $1",
        "UPDATE crm.erasure_request SET dry_run_counts = '{\"trip\": 1}' WHERE request_id = $1",
    ):
        assert await _refused(app_pool, tenant, f"member:{owner}", "owner", sql, request_id) == "CRM18", sql
    for actor in ("job:crm_offboard", "platform:staff-1"):
        sql = "UPDATE crm.erasure_request SET status = 'export_ready' WHERE request_id = $1"
        assert await _refused(app_pool, tenant, actor, "", sql, request_id) == "CRM18"
    # The owner declines: cancelled, nothing else moved.
    async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
        await conn.execute("UPDATE crm.erasure_request SET status = 'cancelled' WHERE request_id = $1", request_id)


async def test_an_export_only_request_never_erases(app_pool: asyncpg.Pool) -> None:
    tenant, owner, _agent = await _agency(app_pool)
    request_id = await _raise(app_pool, tenant, "export_only")
    async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
        await conn.execute("UPDATE crm.erasure_request SET owner_accepted_at = now() WHERE request_id = $1", request_id)
    erase = "UPDATE crm.erasure_request SET status = 'erasing', confirmed_by = $2 WHERE request_id = $1"
    assert await _refused(app_pool, tenant, f"member:{owner}", "owner", erase, request_id, f"member:{owner}") == "CRM18"
    # The export job still moves it to export_ready.
    async with ctx(app_pool, tenant, "job:crm_offboard") as conn:
        await conn.execute(
            "UPDATE crm.erasure_request SET status = 'export_ready', export_object_key = $2, export_sha256 = $3, "
            "export_expires_at = now() + interval '7 days' WHERE request_id = $1",
            request_id,
            f"exports/tenants/{tenant}/{request_id}.jsonl.enc",
            "0" * 64,
        )


async def test_aa_never_confirms_or_runs_a_request_but_keeps_the_aa942_recovery(app_pool: asyncpg.Pool) -> None:
    tenant, owner, _agent = await _agency(app_pool)
    # An owner-made request, confirmed by the owner (AA-942 flow).
    async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
        request_id = await conn.fetchval(
            "INSERT INTO crm.erasure_request (scope, subject_id, export_waived) VALUES ('tenant', $1, true) "
            "RETURNING request_id",
            tenant,
        )
    confirm = "UPDATE crm.erasure_request SET status = 'erasing', confirmed_by = 'platform:x' WHERE request_id = $1"
    assert await _refused(app_pool, tenant, "platform:staff-1", "", confirm, request_id) == "CRM18"
    async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
        await conn.execute(
            "UPDATE crm.erasure_request SET status = 'erasing', confirmed_by = $2 WHERE request_id = $1",
            request_id,
            f"member:{owner}",
        )
    # The documented platform recovery (AA-942 notes): erasing -> failed stays allowed; failed -> erasing is the job's.
    async with ctx(app_pool, tenant, "platform:l1") as conn:
        await conn.execute(
            "UPDATE crm.erasure_request SET status = 'failed', failure_class = 'ops_recovery' WHERE request_id = $1",
            request_id,
        )
    rerun = "UPDATE crm.erasure_request SET status = 'erasing' WHERE request_id = $1"
    assert await _refused(app_pool, tenant, "platform:l1", "", rerun, request_id) == "CRM18"
    async with ctx(app_pool, tenant, "job:crm_offboard") as conn:
        await conn.execute(rerun, request_id)


async def test_a_traveller_erasure_receipt_still_inserts(app_pool: asyncpg.Pool) -> None:
    """The AA-942 client erasure writes its receipt row at once (status erased, confirmed_by the member)."""
    tenant, owner, _agent = await _agency(app_pool)
    async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
        await conn.execute(
            "INSERT INTO crm.erasure_request (scope, subject_id, status, confirmed_by, executed_at, backup_purge_by) "
            "VALUES ('traveler', $1, 'erased', $2, now(), current_date + 30)",
            uuid.uuid4(),
            f"member:{owner}",
        )


async def test_the_guard_is_not_public_and_fires_on_every_write(admin: asyncpg.Connection) -> None:
    assert not await admin.fetchval(
        "SELECT has_function_privilege('public', 'crm.guard_erasure_request_owner_confirm()', 'EXECUTE')"
    )
    row = await admin.fetchrow(
        "SELECT tgtype, cardinality(tgattr::int2[]) AS cols FROM pg_trigger "
        "WHERE tgrelid = 'crm.erasure_request'::regclass AND tgname = 'erasure_request_owner_confirm'"
    )
    assert row["cols"] == 0 and row["tgtype"] & 4 and row["tgtype"] & 16
    assert (
        await admin.fetchval(
            "SELECT proconfig FROM pg_proc WHERE oid = 'crm.guard_erasure_request_owner_confirm()'::regprocedure"
        )
    ) == ["search_path=pg_catalog"]
