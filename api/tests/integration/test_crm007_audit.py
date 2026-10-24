"""AA-940 (card CRM-12) end to end on PostgreSQL 15 with the RDS role shape, as crm_app: the hourly export, its
verification, the daily retention and the agency's access log.

- **E** (card): an export retry keeps the chain (an outage after the first bucket leaves no manifest; the retry
  writes the same bytes and continues the chain); the verify script detects a deleted row.
- Empty hours are exported too; an hour is exported only after the lag; both tiers hold the same object.
- The retention job verifies, marks ``verified_until`` and (dry run) reports partitions.
- The access log: owner only (a refusal is a committed ``denied`` event), filters, paging, CSV download (audited),
  the sign-in event; key creation is audited.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app import audit_log
from app.audit_export import export, retention
from app.audit_export.archive import MemoryAuditArchive
from app.audit_export.chain import hour_floor
from app.audit_export.verify import verify_tenant
from app.errors import Forbidden
from app.jobs.queue import Job
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_crm005_intake import _agency, _keys, _owner_fetch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _backdate(admin: asyncpg.Connection, tenant_id: uuid.UUID, start: datetime, events: list[datetime]) -> None:
    """Move the agency's export start to ``start`` and add events at the given times (as crm_owner: the only writer
    allowed an explicit ``at``), committed so the crm_app pool sees them."""
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await admin.execute(
            "UPDATE crm.audit_export_tenant SET exported_until = $2 WHERE tenant_id = $1", tenant_id, start
        )
        for i, at in enumerate(events):
            await admin.execute(
                "INSERT INTO crm.audit_event (actor_kind, actor_ref, action, outcome, object_kind, object_id, at) "
                "VALUES ('user', 'member:m', 'view', 'allowed', 'trip', $1, $2)",
                f"trip-{i}",
                at,
            )


async def _manifests(admin: asyncpg.Connection, tenant_id: uuid.UUID) -> list[asyncpg.Record]:
    return await _owner_fetch(
        admin,
        tenant_id,
        "SELECT seq, period_start, period_end, event_count, prev_manifest_sha256, manifest_sha256, object_key, "
        "object_sha256 FROM crm.audit_export_manifest ORDER BY seq",
    )


def _start() -> datetime:
    """Six hours ago, on the hour, but never across a month start (keeps the rows in one partition)."""
    start = hour_floor(datetime.now(UTC)) - timedelta(hours=6)
    month = start.replace(day=1, hour=0)
    return max(start, month)


async def test_e_an_export_retry_keeps_the_chain(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    tenant_id, _ = await _agency(app_pool)
    start = _start()
    await _backdate(admin, tenant_id, start, [start + timedelta(minutes=5), start + timedelta(hours=2, minutes=1)])
    now = start + timedelta(hours=3, minutes=20)  # three closed hours past the lag
    archive = MemoryAuditArchive()
    archive.fail_next = {"governance"}  # the first hour reaches the compliance bucket, then the outage
    with pytest.raises(ConnectionError):
        await export.export_tenant(app_pool, archive, tenant_id, now=now)
    assert await _manifests(admin, tenant_id) == []
    first_key = next(iter(archive.versions))[1]
    assert len(archive.versions[("compliance", first_key)]) == 1
    assert ("governance", first_key) not in archive.versions

    assert await export.export_tenant(app_pool, archive, tenant_id, now=now) == 3
    rows = await _manifests(admin, tenant_id)
    assert [(r["seq"], r["event_count"]) for r in rows] == [(1, 1), (2, 0), (3, 1)]  # the empty hour is covered too
    assert rows[0]["prev_manifest_sha256"] == "0" * 64
    assert [r["prev_manifest_sha256"] for r in rows[1:]] == [r["manifest_sha256"] for r in rows[:-1]]
    assert [r["period_start"] for r in rows[1:]] == [r["period_end"] for r in rows[:-1]]
    # The retry wrote the same bytes again (a second, identical locked version); both tiers hold the same object.
    first = archive.versions[("compliance", first_key)]
    assert len(first) == 2 and first[0] == first[1] == archive.versions[("governance", first_key)][0]
    # Nothing more until the next hour has passed the lag.
    assert await export.export_tenant(app_pool, archive, tenant_id, now=now) == 0
    report = await verify_tenant(app_pool, archive, tenant_id)
    assert report.ok, report.problems
    assert report.checked == 3 and report.checked_until == start + timedelta(hours=3)


async def test_e_the_verify_script_detects_a_deleted_row(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    tenant_id, _ = await _agency(app_pool)
    start = _start()
    await _backdate(admin, tenant_id, start, [start + timedelta(minutes=7), start + timedelta(minutes=8)])
    archive = MemoryAuditArchive()
    await export.export_tenant(app_pool, archive, tenant_id, now=start + timedelta(hours=1, minutes=30))
    assert (await verify_tenant(app_pool, archive, tenant_id)).ok
    # Someone with the owner's rights switches the append-only trigger off and deletes one exported row.
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        part = await admin.fetchval(
            "SELECT tableoid::regclass::text FROM crm.audit_event WHERE object_id = 'trip-1' AND at >= $1", start
        )
        await admin.execute(f"ALTER TABLE {part} DISABLE TRIGGER audit_event_append_only")
        await admin.execute("DELETE FROM crm.audit_event WHERE object_id = 'trip-1' AND at >= $1", start)
        await admin.execute(f"ALTER TABLE {part} ENABLE TRIGGER audit_event_append_only")
    report = await verify_tenant(app_pool, archive, tenant_id)
    assert {"code": "content_mismatch", "seq": 1} in report.problems
    # A tampered archive copy is caught too, per tier.
    key = next(k for (_tier, k) in archive.versions)
    archive.versions[("governance", key)].append(b'{"manifest":{}}\n')
    codes = {p["code"] for p in (await verify_tenant(app_pool, archive, tenant_id)).problems}
    assert {"content_mismatch", "object_mismatch:governance"} <= codes


async def test_the_export_job_and_the_retention_job(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    tenant_id, _ = await _agency(app_pool)
    start = _start()
    await _backdate(admin, tenant_id, start, [start + timedelta(minutes=1)])
    archive = MemoryAuditArchive()
    fixed = start + timedelta(hours=2, minutes=20)
    result = await export.build_handler(archive, now=lambda: fixed)(app_pool, Job("e1", export.KIND, {}, 1, 3))
    assert result["status"] == "ok" and result["exported_count"] >= 2 and result["failed_count"] == 0
    assert (await export.build_handler(None)(app_pool, Job("e2", export.KIND, {}, 1, 3)))["status"] == "not_configured"

    done = await retention.build_handler(archive, dry_run=True)(app_pool, Job("r1", retention.KIND, {}, 1, 3))
    assert done["dry_run"] is True and done["dropped_count"] == 0
    mark = await _owner_fetch(
        admin,
        tenant_id,
        "SELECT verified_until, exported_until FROM crm.audit_export_tenant WHERE tenant_id = $1",
        tenant_id,
    )
    assert mark[0]["verified_until"] == mark[0]["exported_until"] == start + timedelta(hours=2)
    # Without the archive nothing is ever marked verified (no partition may go before the archive is proven).
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        before = await admin.fetchval("SELECT count(*) FROM crm.audit_export_tenant WHERE verified_until IS NOT NULL")
    await retention.build_handler(None, dry_run=True)(app_pool, Job("r2", retention.KIND, {}, 1, 3))
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        after = await admin.fetchval("SELECT count(*) FROM crm.audit_export_tenant WHERE verified_until IS NOT NULL")
    assert after == before


async def test_the_access_log_is_for_the_owner(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    tenant_id, [(owner, owner_id), (agent, agent_id)] = await _agency(app_pool, "owner", "agent")
    async with tenant_tx(app_pool, agent, tenant_id) as ctx:
        audit_log.record_sign_in(ctx)
    with pytest.raises(Forbidden):
        async with tenant_tx(app_pool, agent, tenant_id) as ctx:
            await audit_log.list_events(ctx, audit_log.AuditFilter(), cursor=None, limit=10)
    denied = await _owner_fetch(
        admin,
        tenant_id,
        "SELECT actor_ref, outcome, reason FROM crm.audit_event WHERE action = 'audit_view'",
    )
    assert [(r["actor_ref"], r["outcome"], r["reason"]) for r in denied] == [
        (f"member:{agent_id}", "denied", "owner_only")
    ]
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        page = await audit_log.list_events(ctx, audit_log.AuditFilter(actor="user"), cursor=None, limit=1)
    assert len(page["events"]) == 1 and page["next_cursor"]
    newest = page["events"][0]
    assert newest["action"] == "audit_view" and newest["outcome"] == "denied" and newest["member_role"] == "agent"
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        page2 = await audit_log.list_events(
            ctx, audit_log.AuditFilter(actor="user"), cursor=page["next_cursor"], limit=10
        )
        signins = await audit_log.list_events(
            ctx, audit_log.AuditFilter(action="sign_in", member_id=agent_id), cursor=None, limit=10
        )
        ai = await audit_log.list_events(ctx, audit_log.AuditFilter(actor="ai"), cursor=None, limit=10)
    assert newest["event_id"] not in {e["event_id"] for e in page2["events"]}
    assert [(e["action"], e["object_id"]) for e in signins["events"]] == [("sign_in", str(agent_id))]
    assert ai["events"] == []
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        body, count = await audit_log.export_events(ctx, audit_log.AuditFilter(), "csv")
    rows = list(csv.reader(io.StringIO(body.decode("utf-8"))))
    assert rows[0] == list(audit_log.CSV_COLUMNS) and len(rows) == count + 1 and count >= 3
    exported = await _owner_fetch(
        admin, tenant_id, "SELECT reason FROM crm.audit_event WHERE action = 'audit_export' AND outcome = 'allowed'"
    )
    assert [r["reason"] for r in exported] == ["format_csv"]


async def test_key_creation_is_audited(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    tenant_id, [(owner, _)] = await _agency(app_pool)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await Vault(keys, ctx).encrypt("traveler", "display_name_enc", uuid.uuid4(), "Synthetic Name")
    events = await _owner_fetch(
        admin, tenant_id, "SELECT object_kind, object_id, reason FROM crm.audit_event WHERE action = 'key_create'"
    )
    assert [(e["object_kind"], e["object_id"], e["reason"]) for e in events] == [
        ("tenant_key", "vault:v1", "purpose_vault")
    ]
