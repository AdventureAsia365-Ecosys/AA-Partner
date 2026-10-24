"""crm-009 (AA-938, card CRM-10) on PostgreSQL 15 with the RDS role shape: the F3 placeholder map rules.

- ``crm.placeholder_map``: FORCE RLS (another agency sees nothing), written once (crm_app has no UPDATE), expires at
  most 24 h after creation, a same-agency key to its ``crm.llm_call``, no NOLOGIN reader (42501).
- The purge policy: a DELETE by crm_app removes expired maps of every agency only while ``app.purge_scope`` is
  ``placeholder_map``; live maps stay; the scope gives no SELECT.
- ``crm.llm_call.scrub_receipt`` takes only the receipt keys (counts and versions), so a value cannot ride in it.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest

from tests.integration.conftest import READERS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BLOB = b"\x00\x01" + b"\x07" * 40  # shaped like a sealed value; never opened here
INSERT_CALL = (
    "INSERT INTO crm.llm_call (stage, role, provider, model, outcome, scrub_receipt) "
    "VALUES ('crm_x', 'writer', 'stub', 'stub-echo-1', 'ok', $1::jsonb) RETURNING llm_call_id"
)
INSERT_MAP = (
    "INSERT INTO crm.placeholder_map (placeholder_map_id, llm_call_id, map_enc, placeholder_count, scrubber_version"
    "{extra_cols}) VALUES ($1, $2, $3, 1, 'f3-1'{extra_vals})"
)


@asynccontextmanager
async def in_tenant(pool: asyncpg.Pool, tenant_id: uuid.UUID):
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        yield conn


async def _agency(pool: asyncpg.Pool) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with in_tenant(pool, tenant_id) as conn:
        await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant_id)
    return tenant_id


async def _call(conn: asyncpg.Connection, receipt: str = '{"scrubber": "f3-1", "entities": 1}') -> uuid.UUID:
    return await conn.fetchval(INSERT_CALL, receipt)


async def _map(conn: asyncpg.Connection, call_id: uuid.UUID, expires: str | None = None) -> uuid.UUID:
    map_id = uuid.uuid4()
    sql = INSERT_MAP.format(extra_cols=", expires_at" if expires else "", extra_vals=f", {expires}" if expires else "")
    await conn.execute(sql, map_id, call_id, BLOB)
    return map_id


async def test_map_is_tenant_bound_written_once_and_24h_at_most(app_pool: asyncpg.Pool) -> None:
    tenant_id = await _agency(app_pool)
    async with in_tenant(app_pool, tenant_id) as conn:
        call_id, spare = await _call(conn), await _call(conn)
        await _map(conn, call_id)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with in_tenant(app_pool, tenant_id) as conn:
            await conn.execute("UPDATE crm.placeholder_map SET placeholder_count = 2")
    with pytest.raises(asyncpg.CheckViolationError):
        async with in_tenant(app_pool, tenant_id) as conn:
            await _map(conn, spare, "now() + interval '25 hours'")
    with pytest.raises(asyncpg.CheckViolationError):
        async with in_tenant(app_pool, tenant_id) as conn:
            await conn.execute(INSERT_MAP.format(extra_cols="", extra_vals=""), uuid.uuid4(), spare, b"short")
    with pytest.raises(asyncpg.UniqueViolationError):  # one map per call
        async with in_tenant(app_pool, tenant_id) as conn:
            await _map(conn, call_id)
    other = await _agency(app_pool)
    with pytest.raises(asyncpg.ForeignKeyViolationError):  # same-agency key: not another agency's call
        async with in_tenant(app_pool, other) as conn:
            await _map(conn, spare)
    async with in_tenant(app_pool, other) as conn:
        assert await conn.fetchval("SELECT count(*) FROM crm.placeholder_map") == 0
    async with app_pool.acquire() as conn:  # no tenant set: nothing
        assert await conn.fetchval("SELECT count(*) FROM crm.placeholder_map") == 0


async def test_no_reader_reaches_the_map(app_pool: asyncpg.Pool) -> None:
    tenant_id = await _agency(app_pool)
    for reader in READERS:
        async with in_tenant(app_pool, tenant_id) as conn:
            await conn.execute(f"SET LOCAL ROLE {reader}")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.fetch("SELECT map_enc FROM crm.placeholder_map")


async def test_receipt_takes_counts_and_versions_only(app_pool: asyncpg.Pool) -> None:
    tenant_id = await _agency(app_pool)
    full = (
        '{"scrubber": "f3-1", "detector": "bedrock_guardrails", "entities": 3, "counts": {"PERSON": 2, "PHONE": 1}, '
        '"passes": {"known": 2, "regex": 1, "detector": 0}, "known_persons": 2}'
    )
    async with in_tenant(app_pool, tenant_id) as conn:
        assert await _call(conn, full) and await _call(conn, '{"scrubber": "stub-0", "entities": 0}')
    async with in_tenant(app_pool, tenant_id) as conn:
        assert await _call(conn, "{}") and await _call(conn, '{"scrubber": "unavailable", "counts": {}}')
    # Option C (AA-938, S249): the new kind PLACE and version f3-2 fit the existing CHECK, no migration.
    async with in_tenant(app_pool, tenant_id) as conn:
        assert await _call(conn, '{"scrubber": "f3-2", "entities": 3, "counts": {"PERSON": 1, "PLACE": 2}}')
    # Option B (AA-938, S250): version f3-3 and the PLACE_KEPT count fit it too.
    async with in_tenant(app_pool, tenant_id) as conn:
        assert await _call(conn, '{"scrubber": "f3-3", "entities": 1, "counts": {"PERSON": 1, "PLACE_KEPT": 2}}')
    # Review AA-938: closed at every level, so neither a nested key nor a value can carry a name.
    for bad in (
        '{"scrubber": "f3-1", "name": "Hoa"}',
        '{"counts": {"Hoa Nguyen": 1}}',
        '{"counts": {"PERSON": "Nguyễn Thị Hoa"}}',
        '{"counts": ["PERSON"]}',
        '{"passes": {"known": 1, "Hoa": 1}}',
        '{"passes": {"known": "Hoa"}}',
        '{"scrubber": "Nguyen Thi Hoa"}',
        '{"detector": "Hoa Nguyen"}',
        '{"detector": 1}',
        '{"entities": "Hoa"}',
        '{"known_persons": {"Hoa": 1}}',
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            async with in_tenant(app_pool, tenant_id) as conn:
                await _call(conn, bad)


async def test_purge_policy_deletes_expired_maps_of_every_agency_only_with_the_scope(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    agencies = []
    for _ in range(2):
        tenant_id = await _agency(app_pool)
        async with in_tenant(app_pool, tenant_id) as conn:
            await _map(conn, await _call(conn), "now() + interval '1 second'")
            await _map(conn, await _call(conn), "now() + interval '23 hours'")
        agencies.append(tenant_id)
    await asyncio.sleep(1.2)
    async with app_pool.acquire() as conn:
        assert await conn.execute("DELETE FROM crm.placeholder_map") == "DELETE 0"  # no scope: nothing
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.purge_scope', 'placeholder_map', true)")
        assert await conn.fetchval("SELECT count(*) FROM crm.placeholder_map") == 0  # the scope gives no read
        deleted = int((await conn.execute("DELETE FROM crm.placeholder_map")).split()[-1])
    assert deleted >= 2
    for tenant_id in agencies:
        async with admin.transaction():
            await admin.execute("SET LOCAL ROLE crm_owner")
            await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            assert await admin.fetchval("SELECT count(*) FROM crm.placeholder_map") == 1  # the live map stays
