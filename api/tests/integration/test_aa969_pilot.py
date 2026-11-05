"""AA-969 part 2 (root ADR 0019 d16) end to end on PostgreSQL 15 with the RDS role shape, as crm_app, with real field
encryption: the pilot agency's protections.

- The CIS account feed projects the pilot marker onto ``crm.tenant.is_pilot`` (and clears it).
- The daily ``crm_lifecycle`` run leaves the pilot agency out (no ``lifecycle_run`` row, counted ``pilot_skipped``)
  while another agency runs as before; the L1 test-agency delete refuses the pilot (403 ``pilot_agency``).
- ``crm_pilot_backup`` queues one export of the pilot agency only; the export job seals it; the owner's export list
  leaves it out; a second night while one is still being built is ``busy``.
- The restore drill opens that export, restores every table into temporary tables and finds restored = manifest for
  each, with the live counts and primary keys reported; a stored object that no longer matches its digest fails it.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import pytest

from app.cis import entitlement_feed as feed
from app.documents.store import MemoryDocumentStore
from app.errors import Forbidden
from app.jobs.queue import Job
from app.lifecycle import job as lifecycle_job
from app.lifecycle import pilot
from app.ops import console
from app.portability import exports
from app.portability import restore_drill as rd
from app.tenancy import tenant_tx
from tests.integration.test_crm005_intake import _keys, _owner_fetch
from tests.integration.test_crm010_lifecycle import _done, _inquiry, _jobs, _raises

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BACKUP_JOB = Job("00000000-0000-4000-8000-000000000969", "crm_pilot_backup", {"purpose": "daily_schedule"}, 1, 3)
LIFECYCLE_JOB = Job("00000000-0000-4000-8000-000000000970", "crm_lifecycle", {"purpose": "daily_schedule"}, 1, 3)


def _projection(is_pilot: bool) -> feed.Projection:
    return feed.Projection("core", 10, ("ai_assist",), True, True, is_pilot)


async def _mark(pool: asyncpg.Pool, tenant: uuid.UUID, value: bool) -> None:
    assert await feed.apply(pool, tenant, _projection(value), feed.RunCounts()) in ("applied", "unchanged")


async def _marker(admin: asyncpg.Connection, tenant: uuid.UUID) -> bool:
    [row] = await _owner_fetch(admin, tenant, "SELECT is_pilot FROM crm.tenant WHERE tenant_id = $1", tenant)
    return bool(row["is_pilot"])


async def _runs(admin: asyncpg.Connection, tenant: uuid.UUID) -> int:
    [row] = await _owner_fetch(admin, tenant, "SELECT count(*) AS n FROM crm.lifecycle_run")
    return int(row["n"])


async def test_the_account_feed_projects_the_pilot_marker(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    a = await _inquiry(app_pool, keys, "owner")
    counts = feed.RunCounts()
    try:
        assert await feed.apply(app_pool, a["tenant"], _projection(True), counts) == "applied"
        assert counts.pilot_changed == 1 and await _marker(admin, a["tenant"]) is True
        assert await feed.apply(app_pool, a["tenant"], _projection(True), counts) == "unchanged"
    finally:
        await _mark(app_pool, a["tenant"], False)
    assert await _marker(admin, a["tenant"]) is False


async def test_clean_up_skips_the_pilot_agency(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    pilot_agency = await _inquiry(app_pool, keys, "owner")
    other = await _inquiry(app_pool, keys, "owner")
    await _mark(app_pool, pilot_agency["tenant"], True)
    try:
        result = await lifecycle_job.build_handler(dry_run=True, keys=keys)(app_pool, LIFECYCLE_JOB)
        assert result["pilot_skipped_count"] == 1
        assert await _runs(admin, pilot_agency["tenant"]) == 0 and await _runs(admin, other["tenant"]) == 1
        # the E2E / L1 test-agency delete refuses the pilot agency whatever its synthetic flag
        with _raises(Forbidden, "pilot_agency"):
            await console.delete_test_agency(
                app_pool, pilot_agency["tenant"], staff_sub="staff-sub-1", agency_name="Sen Vàng Travel",
                ticket_ref="SUP-9", keys=keys,
            )  # fmt: skip
    finally:
        await _mark(app_pool, pilot_agency["tenant"], False)


async def _backup(app_pool: asyncpg.Pool, keys: Any, store: MemoryDocumentStore, tenant: uuid.UUID) -> uuid.UUID:
    result = await pilot.build_handler()(app_pool, BACKUP_JOB)
    assert result == {"pilot_count": 1, "queued_count": 1, "busy_count": 0}
    [job] = await _jobs(app_pool, "crm_data_export", tenant)
    built = await exports.build_handler(keys=keys, store=store)(app_pool, job)
    await _done(app_pool, job)
    assert built["status"] == "ready"
    return uuid.UUID(built["export_id"])


async def test_the_nightly_backup_restores_with_equal_counts(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    store = MemoryDocumentStore()
    a = await _inquiry(app_pool, keys, "owner", "agent")
    await _mark(app_pool, a["tenant"], True)
    try:
        assert await pilot.pilot_agencies(app_pool) == [a["tenant"]]
        export_id = await _backup(app_pool, keys, store, a["tenant"])
        [row] = await _owner_fetch(
            admin, a["tenant"], "SELECT requested_by, status FROM crm.export_job WHERE export_job_id = $1", export_id
        )
        assert dict(row) == {"requested_by": "job:crm_pilot_backup", "status": "ready"}
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            assert (await exports.list_exports(ctx))["exports"] == []  # AA's restore copy, not the owner's download

        report = await rd.drill(app_pool, keys, store, a["tenant"])
        assert report["ok"] is True and report["export_id"] == str(export_id) and report["mismatched_tables"] == []
        by_table = {t["table"]: t for t in report["tables"]}
        assert {"traveler", "trip", "member", "form_submission"} <= set(by_table)
        for t in report["tables"]:
            assert t["restored_rows"] == t["manifest_rows"] == t["live_rows"], t["table"]
        assert by_table["traveler"]["restored_rows"] >= 1
        assert by_table["traveler"]["live_pk_matched"] == by_table["traveler"]["restored_rows"]
        assert report["checksums_verified"] > len(report["tables"])
        flat = repr(report)
        assert "Nguyễn" not in flat and "@" not in flat  # counts and ids only, never a value of a row
        [event] = await _owner_fetch(
            admin, a["tenant"], "SELECT reason FROM crm.audit_event WHERE action = 'restore_drill'"
        )
        assert event["reason"] == "ok"
        # a stored object that no longer matches its digest fails the drill
        [key] = await _owner_fetch(
            admin, a["tenant"], "SELECT object_key FROM crm.export_job WHERE export_job_id = $1", export_id
        )
        await store.put(key["object_key"], b"tampered")
        with pytest.raises(rd.DrillError, match="object_digest_mismatch"):
            await rd.drill(app_pool, keys, store, a["tenant"], export_id)
        # a second night while an export of the agency is still queued: busy, nothing doubled
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            await exports.request(ctx, "tenant", None)
        assert await pilot.build_handler()(app_pool, BACKUP_JOB) == {
            "pilot_count": 1,
            "queued_count": 0,
            "busy_count": 1,
        }
    finally:
        await _mark(app_pool, a["tenant"], False)
