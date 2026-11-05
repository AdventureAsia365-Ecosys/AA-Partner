"""AA-969 part 2 (root ADR 0019 d16) end to end on PostgreSQL 15 with the RDS role shape, as crm_app, with real field
encryption: the pilot agency's protections.

- The CIS account feed projects the pilot marker onto ``crm.tenant.is_pilot`` (and clears it).
- The daily ``crm_lifecycle`` run skips the AA-initiated clean-up of the pilot agency (counted ``pilot_skipped``) and
  still runs its own deletions (the trash purge): its ``lifecycle_run`` row holds trash counts only
  while another agency runs as before; the L1 test-agency delete refuses the pilot (403 ``pilot_agency``).
- ``crm_pilot_backup`` queues one export of the pilot agency only; the export job seals it; the owner's export list
  leaves it out; a second night while one is still being built is ``busy``.
- The restore drill opens that export, restores every table into temporary tables and finds restored = manifest for
  each, with the live counts and primary keys reported; a stored object that no longer matches its digest fails it.
- S269: an agency with a sent text template (``message_delivery.variables_enc``, the ``mail`` DEK) and an AA ops
  release (``ops_release.payload_enc``, its own DEK) backs up and restores; a value that no longer opens fails the
  backup naming its table and column, and raises the ``pilot_backup_failed`` ops alert.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from typing import Any

import asyncpg
import pytest

from app.cis import entitlement_feed as feed
from app.documents.store import MemoryDocumentStore
from app.errors import Forbidden
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.lifecycle import job as lifecycle_job
from app.lifecycle import pilot
from app.messaging import health
from app.observability import ops_alerts
from app.ops import console
from app.portability import exports
from app.portability import restore_drill as rd
from app.tenancy import lifecycle_tx, tenant_tx
from app.vault import Vault
from tests.integration import test_messaging_db as texts
from tests.integration import test_ops_release_api as releases
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
        assert await _runs(admin, pilot_agency["tenant"]) == 1 and await _runs(admin, other["tenant"]) == 1
        [run] = await _owner_fetch(admin, pilot_agency["tenant"], "SELECT counts FROM crm.lifecycle_run")
        [other_run] = await _owner_fetch(admin, other["tenant"], "SELECT counts FROM crm.lifecycle_run")
        aa_steps = {"submission_redacted", "llm_call_purged", "membership_inbox_purged", "traveler_auto_archived"}
        assert not aa_steps & set(json.loads(run["counts"]) if isinstance(run["counts"], str) else run["counts"])
        assert aa_steps <= set(
            json.loads(other_run["counts"]) if isinstance(other_run["counts"], str) else other_run["counts"]
        )
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
        assert report["complete"] is True and report["documents"]["lost"] == 0 and rd.exit_code(report) == 0
        assert "tenant_key" in report["not_in_backup"]["tables"]
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


async def test_the_owner_export_takes_over_an_open_nightly_backup(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """AA's hidden nightly backup never blocks the owner's own agency export: the open backup is cancelled."""
    keys = _keys()
    store = MemoryDocumentStore()
    a = await _inquiry(app_pool, keys, "owner")
    await _mark(app_pool, a["tenant"], True)
    try:
        assert await pilot.build_handler()(app_pool, BACKUP_JOB) == {
            "pilot_count": 1,
            "queued_count": 1,
            "busy_count": 0,
        }
        [backup_job] = await _jobs(app_pool, "crm_data_export", a["tenant"])
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            mine = await exports.request(ctx, "tenant", None)
        assert mine["status"] == "queued"
        rows = await _owner_fetch(admin, a["tenant"], "SELECT export_job_id, requested_by, status FROM crm.export_job")
        by_id = {str(r["export_job_id"]): r for r in rows}
        assert by_id[backup_job.payload["export_id"]]["status"] == "cancelled"
        assert by_id[mine["export_id"]]["status"] == "queued"
        assert by_id[mine["export_id"]]["requested_by"] != "job:crm_pilot_backup"
        # the cancelled backup's job builds nothing
        assert (await exports.build_handler(keys=keys, store=store)(app_pool, backup_job))["status"] == "skipped"
        await _done(app_pool, backup_job)
    finally:
        await _mark(app_pool, a["tenant"], False)


async def _zip_table(app_pool: asyncpg.Pool, keys: Any, store: MemoryDocumentStore, tenant: uuid.UUID,
                     export_id: uuid.UUID, table: str) -> list[dict[str, Any]]:  # fmt: skip
    async with lifecycle_tx(app_pool, tenant, actor=rd.ACTOR) as ctx:
        [row] = await ctx.conn.fetch("SELECT object_key FROM crm.export_job WHERE export_job_id = $1", export_id)
        content = await Vault(keys, ctx).decrypt_bytes(
            "export_job", "object", export_id, await store.get(row["object_key"]), purpose="export"
        )
    return json.loads(zipfile.ZipFile(io.BytesIO(content)).read(f"json/{table}.json"))


async def test_a_backup_with_text_template_values_and_an_ops_release_restores(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """S269 (live): the pilot agency's backup failed ``decrypt_denied`` for good once it had sent a text template, as
    the export opened ``message_delivery.variables_enc`` under the vault DEK instead of the mail DEK; the AA ops
    release had the same hole. Both are written here by their real writers, then backed up and restored."""
    d = await texts._setup(app_pool)
    keys, tenant = d["keys"], d["tenant"]
    store = MemoryDocumentStore()
    # A sent text template: the approved templates of the sandbox account, then one with its values (sealed, mail DEK).
    sync = Job("j", "crm_messaging_templates", {"tenant_id": str(tenant)}, 1, 3)
    assert (await health.build_templates_handler(d["runtime"])(app_pool, sync))["approved_count"] >= 1
    [template] = await texts._sql(app_pool, tenant, "SELECT content_sid FROM crm.channel_template "
                                  "WHERE approval = 'approved' AND language = 'en' LIMIT 1")  # fmt: skip
    values = {"1": "Lumi", "2": "Riverbend"}
    sent = await texts._send(app_pool, d, body=None, content_sid=template["content_sid"], variables=values)
    # An AA ops release of the trip (sealed, ops_release DEK): an AA line Booking confirms, then the release job.
    owner = d["owner"]
    async with tenant_tx(app_pool, owner, tenant) as ctx:
        today = await releases.service.today(ctx)
    start, end = today + 40 * releases.DAY, today + 45 * releases.DAY
    line = await releases._aa_line(app_pool, tenant, d["trip"], start, end)
    assert await releases._feed(app_pool, tenant, "line.confirmed", line) == ("applied", "line_confirmed")
    assert (await releases._sync(app_pool, keys, tenant, d["trip"]))["created"] == 1

    await _mark(app_pool, tenant, True)
    try:
        export_id = await _backup(app_pool, keys, store, tenant)
        report = await rd.drill(app_pool, keys, store, tenant, export_id)
        assert report["ok"] is True and report["mismatched_tables"] == [], report
        by_table = {t["table"]: t for t in report["tables"]}
        for table in ("message_delivery", "ops_release"):
            assert by_table[table]["restored_rows"] == by_table[table]["manifest_rows"] == 1, table
            assert by_table[table]["live_pk_matched"] == 1, table
        [delivery] = await _zip_table(app_pool, keys, store, tenant, export_id, "message_delivery")
        assert delivery["message_id"] == sent["message_id"] and json.loads(delivery["variables"]) == values
        [release] = await _zip_table(app_pool, keys, store, tenant, export_id, "ops_release")
        assert isinstance(release["payload"], str) and release["payload"] and "payload_enc" not in release

        # A value that no longer opens (sealed under another DEK): the backup fails, fail-closed, naming the table
        # and column (codes only), and AA ops are alerted once for that export.
        async with tenant_tx(app_pool, owner, tenant) as ctx:
            blob = await Vault(keys, ctx).encrypt(
                "message_delivery", "variables_enc", uuid.UUID(sent["message_id"]), json.dumps(values)
            )
            await ctx.conn.execute(
                "UPDATE crm.message_delivery SET variables_enc = $2 WHERE message_id = $1",
                uuid.UUID(sent["message_id"]),
                blob,
            )
        ops_alerts.PENDING.drain()
        assert (await pilot.build_handler()(app_pool, BACKUP_JOB))["queued_count"] == 1
        [job] = await _jobs(app_pool, "crm_data_export", tenant)
        with pytest.raises(PermanentError, match="decrypt_denied"):
            await exports.build_handler(keys=keys, store=store)(app_pool, job)
        await _done(app_pool, job)
        failed_id = uuid.UUID(job.payload["export_id"])
        [row] = await _owner_fetch(
            admin, tenant, "SELECT status, failure_class FROM crm.export_job WHERE export_job_id = $1", failed_id
        )
        assert dict(row) == {"status": "failed", "failure_class": "decrypt_denied:message_delivery:variables_enc"}
        alerts = [f for f in ops_alerts.PENDING.drain() if f.check == "pilot_backup_failed"]
        assert [(f.scope, f.details["failure"]) for f in alerts] == [
            (str(failed_id), "decrypt_denied:message_delivery:variables_enc")
        ]
        assert "Lumi" not in repr(alerts)
        # The restore drill still finds the last good night.
        assert (await rd.drill(app_pool, keys, store, tenant))["export_id"] == str(export_id)
    finally:
        await _mark(app_pool, tenant, False)
