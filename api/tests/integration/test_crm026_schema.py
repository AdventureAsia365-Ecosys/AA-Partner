"""crm-026 (AA-967 CRM-35 module 18) on PostgreSQL with the RDS role shape: portability exports, the data-request log,
the agency-deletion cool-off.

- ``crm.export_job`` / ``crm.data_request``: base template, FORCE RLS (an isolation probe across two agencies), crm_app
  SELECT / INSERT / UPDATE only, nothing for the readers; the CHECKs and the status moves.
- ``crm.erasure_request``: ``erase_not_before`` only with ``confirmed_by``; ``verified_at`` only on an erased row.
- ``crm.erase_subject`` (the crm-025 text + two keep-list names): the agency erase keeps both tables' rows (receipts,
  no personal data), a traveller erase leaves the log entry that names the client.
- Card R: DELETE on ``crm.signature_event`` as crm_app is refused (crm-025's grant, checked again on this schema).

The API's use of them is in tests/integration/test_portability_api.py.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import OWNER, READERS
from tests.integration.test_crm010_schema import _agency, _client, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "026_crm_portability_requests.sql"
NEW_TABLES = ("export_job", "data_request")


async def _reapply(admin: asyncpg.Connection) -> None:
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")


async def _in(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = "member:test") -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        return await conn.fetchval(sql, *args)


async def _refused(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any) -> str:
    try:
        await _in(pool, tenant, sql, *args)
    except asyncpg.PostgresError as exc:
        return type(exc).__name__
    raise AssertionError(f"not refused: {sql}")


async def _request(pool: asyncpg.Pool, tenant: uuid.UUID, traveler: uuid.UUID | None = None, **cols: Any) -> uuid.UUID:
    return await _in(
        pool,
        tenant,
        "INSERT INTO crm.data_request (kind, channel, traveler_id, received_on, due_on) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING data_request_id",
        cols.get("kind", "access"),
        cols.get("channel", "email"),
        traveler,
        cols.get("received_on", dt.date(2026, 10, 1)),
        cols.get("due_on", dt.date(2026, 10, 31)),
    )


async def _export(
    pool: asyncpg.Pool, tenant: uuid.UUID, scope: str = "tenant", subject: uuid.UUID | None = None
) -> uuid.UUID:
    return await _in(
        pool,
        tenant,
        "INSERT INTO crm.export_job (scope, subject_id) VALUES ($1, $2) RETURNING export_job_id",
        scope,
        subject or tenant,
    )


async def _ready(pool: asyncpg.Pool, tenant: uuid.UUID, export_id: uuid.UUID) -> None:
    await _in(pool, tenant, "UPDATE crm.export_job SET status = 'building' WHERE export_job_id = $1", export_id)
    digest = hashlib.sha256(b"zip").hexdigest()
    await _in(
        pool,
        tenant,
        "UPDATE crm.export_job SET status = 'ready', object_key = $2, object_sha256 = $3, content_sha256 = $3, "
        "size_bytes = 3, completed_at = now(), expires_at = now() + interval '7 days' WHERE export_job_id = $1",
        export_id,
        f"exports/tenants/{tenant}/portability/{export_id}.zip.enc",
        digest,
    )


async def test_crm026_is_registered_and_runs_again_as_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-026'") == 1
    await _reapply(admin)
    erase = await admin.fetchval(
        "SELECT prosrc FROM pg_proc WHERE oid = 'crm.erase_subject(text,uuid,uuid,boolean)'::regprocedure"
    )
    # The crm-025 text whole, plus the two keep-list names.
    assert "signature_event_kept" in erase and "'export_job', 'data_request'" in erase
    assert erase.count("'terms_acceptance'") == 1


@pytest.mark.parametrize("table", NEW_TABLES)
async def test_privileges_rls_and_keys(admin: asyncpg.Connection, table: str) -> None:
    for privilege, want in (("SELECT", True), ("INSERT", True), ("UPDATE", True), ("DELETE", False),
                            ("TRUNCATE", False)):  # fmt: skip
        got = await admin.fetchval("SELECT has_table_privilege('crm_app', $1, $2)", f"crm.{table}", privilege)
        assert got is want, (table, privilege)
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege)
    assert await admin.fetchval(
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = $1::regclass", f"crm.{table}"
    )
    refs = await admin.fetch(
        "SELECT confrelid::regclass::text AS ref FROM pg_constraint WHERE conrelid = $1::regclass AND contype = 'f'",
        f"crm.{table}",
    )
    # No key on a trip or a traveller: both outlive the client's erasure.
    assert {r["ref"] for r in refs} <= {"crm.tenant", "crm.data_request", "crm.erasure_request"}


async def test_isolation_probe_across_agencies(app_pool: asyncpg.Pool) -> None:
    a = await _agency(app_pool)
    b = await _agency(app_pool)
    await _request(app_pool, a["tenant"], a["lead"])
    await _export(app_pool, a["tenant"])
    for table in NEW_TABLES:
        assert await _count(app_pool, a["tenant"], table) == 1
        assert await _count(app_pool, b["tenant"], table) == 0
    # No tenant set = no row; a write naming another agency is refused by the policy.
    async with app_pool.acquire() as conn, conn.transaction():
        assert await conn.fetchval("SELECT count(*) FROM crm.export_job") == 0
    assert (
        await _refused(
            app_pool,
            b["tenant"],
            "INSERT INTO crm.export_job (tenant_id, scope, subject_id) VALUES ($1, 'tenant', $1)",
            a["tenant"],
        )
        == "InsufficientPrivilegeError"
    )


async def test_export_job_checks_and_moves(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    # Scope tenant names the agency itself; one open export per subject.
    assert await _refused(app_pool, t, "INSERT INTO crm.export_job (scope, subject_id) VALUES ('tenant', $1)",
                          uuid.uuid4()) == "CheckViolationError"  # fmt: skip
    first = await _export(app_pool, t)
    assert await _refused(app_pool, t, "INSERT INTO crm.export_job (scope, subject_id) VALUES ('tenant', $1)",
                          t) == "UniqueViolationError"  # fmt: skip
    client = await _export(app_pool, t, "traveler", ids["lead"])  # another subject: fine
    # ready needs the object, the checksum, the size and the expiry.
    await _in(app_pool, t, "UPDATE crm.export_job SET status = 'building' WHERE export_job_id = $1", first)
    assert await _refused(app_pool, t, "UPDATE crm.export_job SET status = 'ready' WHERE export_job_id = $1",
                          first) == "CheckViolationError"  # fmt: skip
    await _in(
        app_pool, t, "UPDATE crm.export_job SET status = 'building', progress_done = 2, progress_total = 5 "
        "WHERE export_job_id = $1", first)  # fmt: skip
    assert await _refused(app_pool, t, "UPDATE crm.export_job SET progress_done = 6 WHERE export_job_id = $1",
                          first) == "CheckViolationError"  # fmt: skip
    await _ready(app_pool, t, first)
    # The key shape and a link only on a ready export.
    bad_key = "UPDATE crm.export_job SET object_key = 'exports/x.zip' WHERE export_job_id = $1"
    assert await _refused(app_pool, t, bad_key, first) == "CheckViolationError"
    assert await _refused(
        app_pool, t, "UPDATE crm.export_job SET link_sha256 = $2, link_expires_at = now() WHERE export_job_id = $1",
        client, os.urandom(32)) == "CheckViolationError"  # fmt: skip
    await _in(app_pool, t, "UPDATE crm.export_job SET link_sha256 = $2, link_expires_at = now() + interval '5 minutes' "
              "WHERE export_job_id = $1", first, os.urandom(32))  # fmt: skip
    # Status moves: ready -> expired; never back to building; failed needs a code.
    assert await _refused(app_pool, t, "UPDATE crm.export_job SET status = 'building', object_key = NULL, "
                          "object_sha256 = NULL, completed_at = NULL, link_sha256 = NULL, link_expires_at = NULL "
                          "WHERE export_job_id = $1", first) == "CheckViolationError"  # fmt: skip
    assert await _refused(app_pool, t, "UPDATE crm.export_job SET status = 'failed' WHERE export_job_id = $1",
                          client) == "CheckViolationError"  # fmt: skip
    await _in(app_pool, t, "UPDATE crm.export_job SET status = 'failed', failure_class = 'decrypt_failed' "
              "WHERE export_job_id = $1", client)  # fmt: skip
    await _in(app_pool, t, "UPDATE crm.export_job SET status = 'expired', object_key = NULL, object_sha256 = NULL, "
              "link_sha256 = NULL, link_expires_at = NULL WHERE export_job_id = $1", first)  # fmt: skip
    # crm_app never deletes a receipt.
    assert await _refused(app_pool, t, "DELETE FROM crm.export_job WHERE export_job_id = $1",
                          first) == "InsufficientPrivilegeError"  # fmt: skip


async def test_data_request_checks_and_moves(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    # Due at most 92 days after receipt, never before it.
    for due in (dt.date(2026, 9, 30), dt.date(2027, 1, 2)):
        with pytest.raises(asyncpg.CheckViolationError):
            await _request(app_pool, t, due_on=due)
    with pytest.raises(asyncpg.CheckViolationError):
        await _request(app_pool, t, kind="complaint")
    req = await _request(app_pool, t, ids["lead"])
    # Closing needs the outcome and who closed it, and the outcome must fit the status.
    assert await _refused(app_pool, t, "UPDATE crm.data_request SET status = 'done' WHERE data_request_id = $1",
                          req) == "CheckViolationError"  # fmt: skip
    assert await _refused(
        app_pool, t, "UPDATE crm.data_request SET status = 'done', outcome = 'exempt', closed_at = now(), "
        "closed_by = 'member:x' WHERE data_request_id = $1", req) == "CheckViolationError"  # fmt: skip
    await _in(app_pool, t, "UPDATE crm.data_request SET status = 'extended', due_on = '2026-12-30' "
              "WHERE data_request_id = $1", req)  # fmt: skip
    await _in(app_pool, t, "UPDATE crm.data_request SET status = 'done', outcome = 'fulfilled', closed_at = now(), "
              "closed_by = 'member:x' WHERE data_request_id = $1", req)  # fmt: skip
    assert await _refused(app_pool, t, "UPDATE crm.data_request SET status = 'open', outcome = NULL, closed_at = NULL, "
                          "closed_by = NULL WHERE data_request_id = $1", req) == "CheckViolationError"  # fmt: skip
    assert await _refused(app_pool, t, "DELETE FROM crm.data_request WHERE data_request_id = $1",
                          req) == "InsufficientPrivilegeError"  # fmt: skip


async def test_erasure_request_cool_off_and_verified(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    req = await _in(app_pool, t, "INSERT INTO crm.erasure_request (scope, subject_id) VALUES ('tenant', $1) "
                    "RETURNING request_id", t)  # fmt: skip
    # The cool-off end only with the confirmation; verified only once erased.
    assert await _refused(app_pool, t, "UPDATE crm.erasure_request SET erase_not_before = now() WHERE request_id = $1",
                          req) == "CheckViolationError"  # fmt: skip
    assert await _refused(app_pool, t, "UPDATE crm.erasure_request SET verified_at = now() WHERE request_id = $1",
                          req) == "CheckViolationError"  # fmt: skip
    await _in(app_pool, t, "UPDATE crm.erasure_request SET confirmed_by = 'member:x', "
              "erase_not_before = now() + interval '72 hours' WHERE request_id = $1", req)  # fmt: skip
    log = await _request(app_pool, t, kind="erasure")
    await _in(app_pool, t, "UPDATE crm.data_request SET erasure_request_id = $2 WHERE data_request_id = $1", log, req)


async def test_the_agency_erase_keeps_the_receipts_and_a_traveller_erase_the_log(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t = ids["tenant"]
    client, token = await _client(app_pool, t)  # on no trip
    log = await _request(app_pool, t, client, kind="erasure")
    await _ready(app_pool, t, await _export(app_pool, t, "traveler", client))
    done = await _erase(app_pool, t, "traveler", client, token)
    assert "export_job" not in done["counts"] and "data_request" not in done["counts"]
    assert await _in(app_pool, t, "SELECT traveler_id FROM crm.data_request WHERE data_request_id = $1", log) == client
    await _export(app_pool, t)
    await _in(app_pool, t, "UPDATE crm.tenant SET status = 'offboarding'", actor="platform:test")
    done = await _erase(app_pool, t, "tenant", t)
    assert "export_job" not in done["counts"]
    assert await _count(app_pool, t, "export_job") == 2 and await _count(app_pool, t, "data_request") == 1
    assert await _count(app_pool, t, "traveler") == 0


async def test_r_crm_app_cannot_delete_a_signature_event(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    assert (
        await _refused(app_pool, ids["tenant"], "DELETE FROM crm.signature_event WHERE true")
        == "InsufficientPrivilegeError"
    )
