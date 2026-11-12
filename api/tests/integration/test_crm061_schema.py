"""crm-061 (AA-960, an expand file) on PostgreSQL with the RDS role shape: the agency's tags and the app's system tags.

- ``crm.tag``: kind / subject / code / name / tone checks, the ``sys_`` prefix of a system code, one tag per (agency,
  subject, code), kind / subject / code fixed (CRM55 ``tag_fixed``), a system tag written only by a job, platform or
  seed actor (CRM55 ``system_tag_app_only``).
- ``crm.tag_link``: exactly one subject, the tag's subject (``tag_subject_mismatch``) and source
  (``tag_source_mismatch``), reasons only on a system link, one link per tag and subject, a member never removes a
  system link.
- Hold rule: a link of a held trip is not deleted directly nor through its tag (CRM10).
- Tenant isolation: another agency sees nothing; the erases take every row through the cascades.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg
import pytest

from tests.integration.conftest import READERS
from tests.integration.test_crm010_schema import _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NEW_TABLES = ("tag", "tag_link")
MEMBER = "member:test"
JOB = "job:crm_workflow_tick"


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = MEMBER) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        await conn.execute("SELECT set_config('app.member_role', 'agent', true)")
        return await conn.fetchval(sql, *args)


async def _setup(pool: asyncpg.Pool) -> dict[str, Any]:
    ids = await _agency(pool)
    await _run(pool, ids["tenant"], "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING")
    return ids


async def _tag(
    pool: asyncpg.Pool, tenant: uuid.UUID, code: str, applies_to: str = "trip", *, kind: str = "agency",
    actor: str = MEMBER,
) -> Any:  # fmt: skip
    return await _run(
        pool, tenant,
        "INSERT INTO crm.tag (kind, applies_to, code, name) VALUES ($1, $2, $3, $4) RETURNING tag_id",
        kind, applies_to, code, code.replace("_", " ").title(), actor=actor,
    )  # fmt: skip


async def _refused(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str = MEMBER) -> str:
    with pytest.raises(asyncpg.PostgresError) as err:
        await _run(pool, tenant, sql, *args, actor=actor)
    return f"{err.value.sqlstate} {err.value}"


@pytest.mark.parametrize("table", NEW_TABLES)
async def test_registered_and_no_reader_grant(admin: asyncpg.Connection, table: str) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-061'") == 1
    for reader in READERS:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await admin.fetchval("SELECT has_table_privilege($1, $2, $3)", reader, f"crm.{table}", privilege)


async def test_tag_values_and_one_code_per_subject(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    await _tag(app_pool, t, "honeymoon")
    await _tag(app_pool, t, "honeymoon", "client")  # the same code on the other subject is another tag
    with pytest.raises(asyncpg.UniqueViolationError):
        await _tag(app_pool, t, "honeymoon")
    bad = [
        ("agency", "trip", "Bad Code", "x", "neutral"),
        ("agency", "trip", "sys_mine", "x", "neutral"),  # an agency code never starts with sys_
        ("agency", "booking", "ok_code", "x", "neutral"),
        ("agency", "trip", "ok_name", " padded", "neutral"),
        ("agency", "trip", "ok_long", "x" * 61, "neutral"),
        ("agency", "trip", "ok_tone", "x", "pink"),
    ]
    for kind, applies, code, name, tone in bad:
        with pytest.raises(asyncpg.CheckViolationError):
            sql = "INSERT INTO crm.tag (kind, applies_to, code, name, tone) VALUES ($1, $2, $3, $4, $5)"
            await _run(app_pool, t, sql, kind, applies, code, name, tone)
    with pytest.raises(asyncpg.CheckViolationError):  # a system code always starts with sys_
        await _tag(app_pool, t, "aa_trekking", kind="system", actor=JOB)


async def test_system_tags_are_the_apps_and_a_tag_is_fixed(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    assert "system_tag_app_only" in await _refused(
        app_pool, t, "INSERT INTO crm.tag (kind, applies_to, code, name) VALUES ('system', 'trip', 'sys_x', 'X')"
    )
    system = await _tag(app_pool, t, "sys_aa_trekking", kind="system", actor=JOB)
    for sql in ("UPDATE crm.tag SET name = 'Mine' WHERE tag_id = $1", "DELETE FROM crm.tag WHERE tag_id = $1"):
        assert "system_tag_app_only" in await _refused(app_pool, t, sql, system)
    await _run(app_pool, t, "UPDATE crm.tag SET name = 'Trekking' WHERE tag_id = $1", system, actor=JOB)
    agency = await _tag(app_pool, t, "vip")
    await _run(app_pool, t, "UPDATE crm.tag SET name = 'Very important', tone = 'purple', archived_at = now() "
               "WHERE tag_id = $1", agency)  # fmt: skip
    for sql in ("UPDATE crm.tag SET code = 'vip2' WHERE tag_id = $1",
                "UPDATE crm.tag SET applies_to = 'client' WHERE tag_id = $1"):  # fmt: skip
        assert "tag_fixed" in await _refused(app_pool, t, sql, agency)
    with pytest.raises(asyncpg.CheckViolationError):  # a system tag is never archived
        await _run(app_pool, t, "UPDATE crm.tag SET archived_at = now() WHERE tag_id = $1", system, actor=JOB)


async def test_links_follow_their_tag(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    trip_tag = await _tag(app_pool, t, "vip")
    client_tag = await _tag(app_pool, t, "repeat", "client")
    system = await _tag(app_pool, t, "sys_ready_to_travel", "client", kind="system", actor=JOB)
    insert = "INSERT INTO crm.tag_link (tag_id, trip_id, traveler_id) VALUES ($1, $2, $3) RETURNING tag_link_id"
    link = await _run(app_pool, t, insert, trip_tag, ids["trip"], None)
    await _run(app_pool, t, insert, client_tag, None, ids["lead"])
    with pytest.raises(asyncpg.UniqueViolationError):
        await _run(app_pool, t, insert, trip_tag, ids["trip"], None)
    with pytest.raises(asyncpg.CheckViolationError):
        await _run(app_pool, t, insert, trip_tag, ids["trip"], ids["lead"])
    assert "tag_subject_mismatch" in await _refused(app_pool, t, insert, trip_tag, None, ids["lead"])
    assert "tag_subject_mismatch" in await _refused(app_pool, t, insert, client_tag, ids["trip"], None)
    # A system tag: never from a member, always with source system; an agency tag never from the app.
    assert "tag_source_mismatch" in await _refused(app_pool, t, insert, system, None, ids["companion"])
    sys_insert = ("INSERT INTO crm.tag_link (tag_id, traveler_id, source, reasons) "
                  "VALUES ($1, $2, 'system', $3::jsonb) RETURNING tag_link_id")  # fmt: skip
    reasons = json.dumps([{"code": "returned_months_ago", "months": 14}])
    assert "system_tag_app_only" in await _refused(app_pool, t, sys_insert, system, ids["lead"], reasons)
    sys_link = await _run(app_pool, t, sys_insert, system, ids["lead"], reasons, actor=JOB)
    assert "tag_source_mismatch" in await _refused(app_pool, t, sys_insert, client_tag, ids["companion"], None,
                                                   actor=JOB)  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):  # reasons only on a system link
        await _run(app_pool, t, "INSERT INTO crm.tag_link (tag_id, traveler_id, reasons) VALUES ($1, $2, '[]')",
                   client_tag, ids["companion"])  # fmt: skip
    assert "system_tag_app_only" in await _refused(app_pool, t, "DELETE FROM crm.tag_link WHERE tag_link_id = $1",
                                                   sys_link)  # fmt: skip
    assert "tag_fixed" in await _refused(app_pool, t, "UPDATE crm.tag_link SET trip_id = trip_id, tag_id = $2 "
                                         "WHERE tag_link_id = $1", link, client_tag)  # fmt: skip
    await _run(app_pool, t, "UPDATE crm.tag_link SET reasons = '[]' WHERE tag_link_id = $1", sys_link, actor=JOB)
    await _run(app_pool, t, "DELETE FROM crm.tag_link WHERE tag_link_id = $1", link)
    # Removing a tag removes its links.
    await _run(app_pool, t, "DELETE FROM crm.tag WHERE tag_id = $1", client_tag)
    assert await _count(app_pool, t, "tag_link") == 1


async def test_a_held_trip_keeps_its_tags(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    t = ids["tenant"]
    tag = await _tag(app_pool, t, "vip")
    await _run(app_pool, t, "INSERT INTO crm.tag_link (tag_id, trip_id) VALUES ($1, $2)", tag, ids["trip"])
    await _run(app_pool, t, "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) VALUES ('trip', $1, "
               "'dispute')", ids["trip"])  # fmt: skip
    for sql in ("DELETE FROM crm.tag_link", "DELETE FROM crm.tag"):
        assert (await _refused(app_pool, t, sql)).startswith("CRM10"), sql


async def test_another_agency_sees_nothing_and_the_erases_take_everything(app_pool: asyncpg.Pool) -> None:
    ids = await _setup(app_pool)
    other = await _setup(app_pool)
    t = ids["tenant"]
    trip_tag = await _tag(app_pool, t, "vip")
    client_tag = await _tag(app_pool, t, "repeat", "client")
    await _run(app_pool, t, "INSERT INTO crm.tag_link (tag_id, trip_id) VALUES ($1, $2)", trip_tag, ids["trip"])
    await _run(app_pool, t, "INSERT INTO crm.tag_link (tag_id, traveler_id) VALUES ($1, $2)", client_tag, ids["lead"])
    for table in NEW_TABLES:
        assert await _count(app_pool, other["tenant"], table) == 0, table
        assert await _count(app_pool, t, table) == 2, table
    # Another agency's member cannot link this agency's tag (the tag lookup and the key see only their own agency).
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _run(app_pool, other["tenant"], "INSERT INTO crm.tag_link (tag_id, trip_id) VALUES ($1, $2)",
                   trip_tag, other["trip"])  # fmt: skip
    await _erase(app_pool, t, "trip", ids["trip"])
    assert await _count(app_pool, t, "tag_link") == 1
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, t)
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    await _erase(app_pool, t, "tenant", t)
    for table in NEW_TABLES:
        assert await _count(app_pool, t, table) == 0, table
