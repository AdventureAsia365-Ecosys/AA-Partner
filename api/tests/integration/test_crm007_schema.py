"""crm-007 (AA-940, card CRM-12) on PostgreSQL 15 with the RDS role shape: the F5 audit schema rules.

- **R** (card): UPDATE of ``crm.audit_event`` as ``crm_app`` -> error (42501), and DELETE / TRUNCATE too; a partition
  named directly is refused; the owner's UPDATE hits the append-only trigger.
- ``at`` is the insert time: an explicit value from ``crm_app`` is refused (no event lands in an exported hour).
- Monthly partitions: the maker is idempotent and keeps three months ahead; the drop keeps 12 months and needs every
  agency verified past the partition (DM-7 "dropped only after its export is verified").
- The offboarding path: an agency with audit rows cannot be deleted (the foreign key), its rows leave with the
  partition drop, then the tenant row goes; the registry row cascades, the manifests stay.
- The manifest chain: seq 1 with a zero previous hash, then seq + 1, the previous hash, a contiguous period;
  append-only.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from tests.integration.conftest import APP_ROLE, OWNER, READERS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ZERO = "0" * 64
EVENT_SQL = (
    "INSERT INTO crm.audit_event (actor_kind, actor_ref, action, outcome, object_kind) "
    "VALUES ('user', 'member:m', 'view', 'allowed', 'trip')"
)


def _hex(n: int) -> str:
    return f"{n:064x}"


async def _tenant(pool: asyncpg.Pool) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant_id)
        await conn.execute(EVENT_SQL)
    return tenant_id


async def _as_owner(conn: asyncpg.Connection, tenant_id: uuid.UUID | None = None) -> None:
    await conn.execute(f"SET LOCAL ROLE {OWNER}")
    if tenant_id is not None:
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))


async def test_crm007_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-007'") == 1
    assert await admin.fetchval("SELECT relkind::text FROM pg_class WHERE oid = 'crm.audit_event'::regclass") == "p"


async def test_r_update_as_crm_app_is_refused(app_pool: asyncpg.Pool) -> None:
    tenant_id = await _tenant(app_pool)
    for statement in (
        "UPDATE crm.audit_event SET action = 'edited'",
        "DELETE FROM crm.audit_event",
        "TRUNCATE crm.audit_event",
        "SELECT count(*) FROM crm.audit_event_legacy",
        "UPDATE crm.audit_export_manifest SET event_count = 0",
    ):
        async with app_pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(statement)
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        assert await conn.fetchval("SELECT count(*) FROM crm.audit_event WHERE action = 'view'") == 1


async def test_the_owner_hits_the_append_only_trigger(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    tenant_id = await _tenant(app_pool)
    for statement in ("UPDATE crm.audit_event SET action = 'edited'", "DELETE FROM crm.audit_event"):
        tx = admin.transaction()
        await tx.start()
        try:
            await _as_owner(admin, tenant_id)
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
                await admin.execute(statement)
        finally:
            await tx.rollback()


async def test_an_explicit_at_is_refused_for_crm_app(app_pool: asyncpg.Pool) -> None:
    tenant_id = await _tenant(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="insert time"):
            await conn.execute(
                "INSERT INTO crm.audit_event (actor_kind, actor_ref, action, outcome, object_kind, at) "
                "VALUES ('user', 'member:m', 'view', 'allowed', 'trip', now() - interval '2 hours')"
            )


async def test_partitions_reach_three_months_ahead_and_the_maker_is_idempotent(app_pool: asyncpg.Pool) -> None:
    async with app_pool.acquire() as conn:
        assert await conn.fetchval("SELECT crm.audit_ensure_partitions()") == 0
        ends = [r["range_end"] for r in await conn.fetch("SELECT range_end FROM crm.audit_partition_bounds()")]
        furthest = await conn.fetchval(
            "SELECT (date_trunc('month', now() AT TIME ZONE 'UTC') + interval '4 months') AT TIME ZONE 'UTC'"
        )
    assert max(ends) == furthest
    assert ends == sorted(ends)


async def test_functions_are_for_crm_app_only(admin: asyncpg.Connection) -> None:
    for fn in (
        "crm.audit_ensure_partitions()",
        "crm.audit_drop_expired_partitions(boolean)",
        "crm.audit_export_agencies()",
        "crm.audit_partition_bounds()",
    ):
        assert await admin.fetchval("SELECT has_function_privilege($1, $2, 'EXECUTE')", APP_ROLE, fn), fn
        assert not await admin.fetchval("SELECT has_function_privilege('public', $1, 'EXECUTE')", fn), fn
        for reader in READERS:
            assert not await admin.fetchval("SELECT has_function_privilege($1, $2, 'EXECUTE')", reader, fn), fn


async def _old_partition(admin: asyncpg.Connection, tenant_id: uuid.UUID) -> None:
    """A January 2024 partition with one event of ``tenant_id`` (written as the owner: the only writer allowed an
    explicit ``at``). The crm-003 table is the first partition (MINVALUE to next month), so the caller's transaction
    detaches it first and lets the maker create the current month; the rollback restores both."""
    await admin.execute("ALTER TABLE crm.audit_event DETACH PARTITION crm.audit_event_legacy")
    assert await admin.fetchval("SELECT crm.audit_ensure_partitions()") == 1
    await admin.execute(
        "CREATE TABLE crm.audit_event_p202401 PARTITION OF crm.audit_event "
        "FOR VALUES FROM ('2024-01-01 00:00:00+00') TO ('2024-02-01 00:00:00+00')"
    )
    await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
    await admin.execute(
        "INSERT INTO crm.audit_event (actor_kind, actor_ref, action, outcome, object_kind, at) "
        "VALUES ('user', 'member:m', 'view', 'allowed', 'trip', '2024-01-15 10:00:00+00')"
    )


async def test_drop_keeps_twelve_months_and_needs_verification(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool
) -> None:
    tenant_id = await _tenant(app_pool)
    tx = admin.transaction()
    await tx.start()
    try:
        await _as_owner(admin)
        await _old_partition(admin, tenant_id)
        dry = {
            r["partition_name"]: r["action"]
            for r in await admin.fetch("SELECT * FROM crm.audit_drop_expired_partitions(true)")
        }
        # Nothing of the last 12 months is ever a candidate; the old month waits for every agency's verification.
        assert dry == {"audit_event_p202401": "kept_unverified"}
        # The owner bypasses the registry's RLS (enabled, not forced): mark every agency verified.
        await admin.execute("UPDATE crm.audit_export_tenant SET verified_until = exported_until")
        dry = {
            r["partition_name"]: r["action"]
            for r in await admin.fetch("SELECT * FROM crm.audit_drop_expired_partitions(true)")
        }
        assert dry == {"audit_event_p202401": "would_drop"}
        assert await admin.fetchval("SELECT to_regclass('crm.audit_event_p202401') IS NOT NULL")
        done = await admin.fetch("SELECT * FROM crm.audit_drop_expired_partitions(false)")
        assert [(r["partition_name"], r["action"]) for r in done] == [("audit_event_p202401", "dropped")]
        assert await admin.fetchval("SELECT to_regclass('crm.audit_event_p202401')") is None
    finally:
        await tx.rollback()


async def test_offboarding_waits_for_the_partition_drop(admin: asyncpg.Connection, app_pool: asyncpg.Pool) -> None:
    """AA-934 comment on AA-940: the tenant foreign key has no ON DELETE action and the audit has no DELETE. The path:
    rows leave with their partition, then the tenant row can go; the registry cascades, the manifests stay."""
    tenant_id = uuid.uuid4()
    tx = admin.transaction()
    await tx.start()
    try:
        await _as_owner(admin, tenant_id)
        await admin.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant_id)
        await _old_partition(admin, tenant_id)
        await admin.execute(
            "INSERT INTO crm.audit_export_manifest (seq, period_start, period_end, event_count, content_sha256, "
            "prev_manifest_sha256, manifest_sha256, object_key, object_sha256) VALUES (1, '2024-01-15 10:00+00', "
            "'2024-01-15 11:00+00', 1, $1, $2, $3, $4, $5)",
            _hex(1),
            ZERO,
            _hex(2),
            f"audit/v1/tenants/{tenant_id}/2024/01/15/1000Z-0000000001.jsonl",
            _hex(3),
        )
        sp = admin.transaction()
        await sp.start()
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await admin.execute("DELETE FROM crm.tenant WHERE tenant_id = $1", tenant_id)
        await sp.rollback()
        await admin.execute("UPDATE crm.audit_export_tenant SET verified_until = exported_until")
        await admin.fetch("SELECT * FROM crm.audit_drop_expired_partitions(false)")
        await admin.execute("DELETE FROM crm.tenant WHERE tenant_id = $1", tenant_id)
        assert await admin.fetchval("SELECT count(*) FROM crm.audit_export_tenant WHERE tenant_id = $1", tenant_id) == 0
        assert (
            await admin.fetchval("SELECT count(*) FROM crm.audit_export_manifest WHERE tenant_id = $1", tenant_id) == 1
        )
    finally:
        await tx.rollback()


async def test_a_new_agency_is_registered_and_sees_only_its_row(app_pool: asyncpg.Pool) -> None:
    a, b = await _tenant(app_pool), await _tenant(app_pool)
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(a))
        mine = await conn.fetch("SELECT tenant_id, exported_until, last_seq FROM crm.audit_export_tenant")
        assert [r["tenant_id"] for r in mine] == [a] and mine[0]["last_seq"] == 0
        listed = {r["tenant_id"] for r in await conn.fetch("SELECT tenant_id FROM crm.audit_export_agencies()")}
        assert {a, b} <= listed
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("DELETE FROM crm.audit_export_tenant")


async def _manifest(
    conn: asyncpg.Connection, tenant_id: uuid.UUID, seq: int, prev: str, start: str, end: str, own: str
) -> None:
    await conn.execute(
        "INSERT INTO crm.audit_export_manifest (seq, period_start, period_end, event_count, content_sha256, "
        "prev_manifest_sha256, manifest_sha256, object_key, object_sha256) "
        "VALUES ($1, $2::text::timestamptz, $3::text::timestamptz, 0, $4, $5, $6, $7, $8)",
        seq,
        start,
        end,
        _hex(seq),
        prev,
        own,
        f"audit/v1/tenants/{tenant_id}/2026/10/24/{seq:02d}00Z-{seq:010d}.jsonl",
        _hex(seq + 100),
    )


async def test_the_manifest_chain_is_enforced(app_pool: asyncpg.Pool) -> None:
    tenant_id = await _tenant(app_pool)
    bad_starts = [
        (2, ZERO, "2026-10-24 01:00+00", "2026-10-24 02:00+00"),  # first row must be seq 1
        (1, _hex(9), "2026-10-24 01:00+00", "2026-10-24 02:00+00"),  # with a zero previous hash
    ]
    for seq, prev, start, end in bad_starts:
        async with app_pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            with pytest.raises(asyncpg.CheckViolationError):
                await _manifest(conn, tenant_id, seq, prev, start, end, _hex(500))
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await _manifest(conn, tenant_id, 1, ZERO, "2026-10-24 01:00+00", "2026-10-24 02:00+00", _hex(501))
    breaks = [
        (3, _hex(501), "2026-10-24 02:00+00", "2026-10-24 03:00+00"),  # seq gap
        (2, _hex(777), "2026-10-24 02:00+00", "2026-10-24 03:00+00"),  # wrong previous hash
        (2, _hex(501), "2026-10-24 03:00+00", "2026-10-24 04:00+00"),  # period gap
    ]
    for seq, prev, start, end in breaks:
        async with app_pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            with pytest.raises(asyncpg.CheckViolationError):
                await _manifest(conn, tenant_id, seq, prev, start, end, _hex(502))
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await _manifest(conn, tenant_id, 2, _hex(501), "2026-10-24 02:00+00", "2026-10-24 03:00+00", _hex(502))
        assert await conn.fetchval("SELECT max(seq) FROM crm.audit_export_manifest") == 2
