"""crm-064 (AA-1096 lane L4, a contract file) on PostgreSQL with the RDS role shape: the CRM settings registry.

- ``crm.setting_definition``: the seed equals the compiled defaults; only ``crm_owner`` writes it (the guard trigger
  refuses ``crm_app``, which crm-001's default privileges let INSERT / UPDATE / DELETE);
- ``crm.setting_value``: only AA staff write it (``app.actor`` ``platform:*``, named on the row); a value is checked
  against its definition; a maker-checker key needs a second person or a recorded self-approval while two-person
  approval is off; rows are append-only (decision columns only, a platform row is never deleted, no TRUNCATE);
- RLS: a platform row is seen only outside an agency context, an agency's row only in its own; an agency's rows go
  with its ``crm.tenant_settings`` row (FK ON DELETE CASCADE, the agency erase);
- no reader role gets either table.

Every write runs in a transaction that is rolled back: platform rows are append-only, so a committed one would stay
for the rest of the session.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import READERS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DEFAULTS = Path(__file__).resolve().parents[2] / "app" / "platform_settings" / "compiled_defaults.json"
STAFF = "platform:zz-staff-1"
OTHER = "platform:zz-staff-2"
REASON = "zz test reason long enough for the check"
PROPOSE = (
    "INSERT INTO crm.setting_value (key, tenant_id, value, version, proposed_by, reason) "
    "VALUES ($1, $2, $3::jsonb, $4, $5, $6) RETURNING id"
)


@pytest_asyncio.fixture
async def conn(app_pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    """A crm_app connection inside a transaction that is always rolled back."""
    async with app_pool.acquire() as c:
        tx = c.transaction()
        await tx.start()
        try:
            yield c
        finally:
            await tx.rollback()


async def _actor(c: asyncpg.Connection, actor: str) -> None:
    await c.execute("SELECT set_config('app.actor', $1, true)", actor)


async def _tenant(c: asyncpg.Connection, tenant_id: str) -> None:
    await c.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)


async def _agency(c: asyncpg.Connection) -> uuid.UUID:
    """A synthetic agency with its settings row, written the way the API does (tenant set first)."""
    tenant_id = uuid.uuid4()
    await _tenant(c, str(tenant_id))
    await _actor(c, "seed:test")
    await c.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant_id)
    await c.execute("INSERT INTO crm.tenant_settings DEFAULT VALUES")
    return tenant_id


async def test_crm064_is_registered_and_seeds_the_compiled_defaults(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-064'") == 1
    rows = await admin.fetch(
        "SELECT key, value_type, default_value::text AS d, scope_kind, approval, env_override "
        "FROM crm.setting_definition ORDER BY key"
    )
    compiled = json.loads(DEFAULTS.read_text(encoding="utf-8"))["definitions"]
    assert [r["key"] for r in rows] == sorted(d["key"] for d in compiled)
    by_key = {d["key"]: d for d in compiled}
    for r in rows:
        want = by_key[r["key"]]
        assert json.loads(r["d"]) == want["default_value"], r["key"]
        assert (r["value_type"], r["scope_kind"], r["approval"], r["env_override"]) == (
            want["value_type"],
            want["scope_kind"],
            want["approval"],
            want["env_override"],
        ), r["key"]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE crm.setting_definition SET max_value = 99999 WHERE key = 'crm.rate.ai_per_minute'",
        "DELETE FROM crm.setting_definition WHERE key = 'crm.rate.ai_per_minute'",
        "INSERT INTO crm.setting_definition (key, app, area, label, description, value_type, default_value, impact, "
        "approval) VALUES ('crm.zz.x', 'crm', 'zz', 'zz label', 'zz description long enough', 'int', '1', 'low', "
        "'single')",
        "TRUNCATE crm.setting_definition CASCADE",
    ],
)
async def test_crm_app_never_writes_a_definition(conn: asyncpg.Connection, sql: str) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute(sql)


async def test_only_aa_staff_write_a_value_named_on_the_row(conn: asyncpg.Connection) -> None:
    for actor in ("", "member:zz", "seed:test"):
        await _actor(conn, actor)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():  # a savepoint per attempt
                await conn.execute(PROPOSE, "crm.rate.ai_per_minute", None, "7", 1, STAFF, REASON)
    await _actor(conn, STAFF)
    with pytest.raises(asyncpg.CheckViolationError, match="proposed_by"):
        async with conn.transaction():
            await conn.execute(PROPOSE, "crm.rate.ai_per_minute", None, "7", 1, OTHER, REASON)
    assert await conn.fetchval(PROPOSE, "crm.rate.ai_per_minute", None, "7", 1, STAFF, REASON) is not None


@pytest.mark.parametrize(
    ("key", "value", "error"),
    [
        ("crm.rate.ai_per_minute", "601", asyncpg.CheckViolationError),
        ("crm.rate.ai_per_minute", "2.5", asyncpg.CheckViolationError),
        ("crm.rate.ai_per_minute", '"six"', asyncpg.CheckViolationError),
        ("crm.retention.job_dry_run", "1", asyncpg.CheckViolationError),
        ("crm.zz.unknown_key", "1", asyncpg.ForeignKeyViolationError),
    ],
)
async def test_a_value_must_fit_its_definition(conn: asyncpg.Connection, key: str, value: str, error: type) -> None:
    await _actor(conn, STAFF)
    with pytest.raises(error):
        await conn.execute(PROPOSE, key, None, value, 1, STAFF, REASON)


async def test_short_reason_and_inserted_decision_are_refused(conn: asyncpg.Connection) -> None:
    await _actor(conn, STAFF)
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(PROPOSE, "crm.rate.ai_per_minute", None, "7", 1, STAFF, "too short")
    with pytest.raises(asyncpg.CheckViolationError, match="proposal"):
        await conn.execute(
            "INSERT INTO crm.setting_value (key, value, version, status, proposed_by, reason, decided_by, decided_at) "
            "VALUES ('crm.rate.ai_per_minute', '7', 1, 'approved', $1, $2, $1, now())",
            STAFF,
            REASON,
        )


async def test_maker_checker_needs_a_second_person_or_a_recorded_self_approval(conn: asyncpg.Connection) -> None:
    await _actor(conn, STAFF)
    vid = await conn.fetchval(PROPOSE, "crm.rate.ai_per_minute", None, "7", 1, STAFF, REASON)
    decide = (
        "UPDATE crm.setting_value SET status = 'approved', decided_by = $2, decided_at = now(), self_approved = $3 "
        "WHERE id = $1"
    )
    with pytest.raises(asyncpg.CheckViolationError, match="second person"):
        async with conn.transaction():
            await conn.execute(decide, vid, STAFF, False)
    with pytest.raises(asyncpg.CheckViolationError, match="acting staff"):
        async with conn.transaction():
            await conn.execute(decide, vid, OTHER, False)  # the session's actor is STAFF
    await conn.execute(decide, vid, STAFF, True)  # two-person approval is off (the seed): a recorded self-approval
    assert await conn.fetchval("SELECT status FROM crm.setting_value WHERE id = $1", vid) == "approved"


async def test_a_self_approval_is_refused_once_two_person_approval_is_on(conn: asyncpg.Connection) -> None:
    await _actor(conn, OTHER)
    flag = await conn.fetchval(PROPOSE, "crm.settings.two_person_approval", None, "true", 1, OTHER, REASON)
    await conn.execute(
        "UPDATE crm.setting_value SET status = 'approved', decided_by = $2, decided_at = now(), self_approved = true "
        "WHERE id = $1",
        flag,
        OTHER,
    )
    await _actor(conn, STAFF)
    vid = await conn.fetchval(PROPOSE, "crm.rate.ai_per_minute", None, "7", 1, STAFF, REASON)
    with pytest.raises(asyncpg.CheckViolationError, match="two-person approval is on"):
        await conn.execute(
            "UPDATE crm.setting_value SET status = 'approved', decided_by = $2, decided_at = now(), "
            "self_approved = true WHERE id = $1",
            vid,
            STAFF,
        )


async def test_an_agency_row_self_approval_reads_the_platform_flag(conn: asyncpg.Connection) -> None:
    """The flag is a platform row (hidden inside an agency context by RLS): the trigger still sees it there, and the
    agency context is back after the check."""
    await _actor(conn, OTHER)
    flag = await conn.fetchval(PROPOSE, "crm.settings.two_person_approval", None, "true", 1, OTHER, REASON)
    await conn.execute(
        "UPDATE crm.setting_value SET status = 'approved', decided_by = $2, decided_at = now(), self_approved = true "
        "WHERE id = $1",
        flag,
        OTHER,
    )
    agency = await _agency(conn)
    await _actor(conn, STAFF)
    vid = await conn.fetchval(PROPOSE, "crm.rate.intake_form_per_hour", agency, "900", 1, STAFF, REASON)
    with pytest.raises(asyncpg.CheckViolationError, match="two-person approval is on"):
        async with conn.transaction():
            await conn.execute(
                "UPDATE crm.setting_value SET status = 'approved', decided_by = $2, decided_at = now(), "
                "self_approved = true WHERE id = $1",
                vid,
                STAFF,
            )
    await _actor(conn, OTHER)
    await conn.execute(
        "UPDATE crm.setting_value SET status = 'approved', decided_by = $2, decided_at = now() WHERE id = $1",
        vid,
        OTHER,
    )
    assert await conn.fetchval("SELECT current_setting('app.tenant_id') AS t") == str(agency)
    assert await conn.fetchval("SELECT status FROM crm.setting_value WHERE id = $1", vid) == "approved"


async def test_rows_are_append_only(conn: asyncpg.Connection) -> None:
    await _actor(conn, STAFF)
    vid = await conn.fetchval(PROPOSE, "crm.payments.invoice_reminder_max", None, "4", 1, STAFF, REASON)
    for sql in (
        "UPDATE crm.setting_value SET value = '3' WHERE id = $1",
        "UPDATE crm.setting_value SET reason = 'zz another reason long enough' WHERE id = $1",
        "DELETE FROM crm.setting_value WHERE id = $1",
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(sql, vid)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # crm_app holds no TRUNCATE (the trigger backs it up)
        async with conn.transaction():
            await conn.execute("TRUNCATE crm.setting_value")
    # single approval: the proposer decides alone; then supersede keeps the decision
    await conn.execute(
        "UPDATE crm.setting_value SET status = 'approved', decided_by = $2, decided_at = now() WHERE id = $1",
        vid,
        STAFF,
    )
    await conn.execute("UPDATE crm.setting_value SET status = 'superseded' WHERE id = $1", vid)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("UPDATE crm.setting_value SET status = 'approved' WHERE id = $1", vid)


async def test_rls_keeps_platform_and_agency_rows_apart(conn: asyncpg.Connection) -> None:
    await _actor(conn, STAFF)
    platform_id = await conn.fetchval(PROPOSE, "crm.rate.intake_form_per_hour", None, "300", 1, STAFF, REASON)
    agency = await _agency(conn)
    await _actor(conn, STAFF)
    # a platform-scope key never takes an agency row; a platform+tenant key does
    with pytest.raises(asyncpg.CheckViolationError, match="per-agency"):
        async with conn.transaction():
            await conn.execute(PROPOSE, "crm.rate.ai_per_minute", agency, "7", 1, STAFF, REASON)
    agency_id = await conn.fetchval(PROPOSE, "crm.rate.intake_form_per_hour", agency, "900", 1, STAFF, REASON)
    seen = {r["id"] for r in await conn.fetch("SELECT id FROM crm.setting_value")}
    assert agency_id in seen and platform_id not in seen  # inside the agency: its own rows only
    # a platform row cannot be written from inside an agency context
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        async with conn.transaction():
            await conn.execute(PROPOSE, "crm.rate.public_per_minute", None, "70", 1, STAFF, REASON)
    await _tenant(conn, "")
    seen = {r["id"] for r in await conn.fetch("SELECT id FROM crm.setting_value")}
    assert platform_id in seen and agency_id not in seen  # outside: platform rows only
    other = uuid.uuid4()
    await _tenant(conn, str(other))
    assert await conn.fetchval("SELECT count(*) FROM crm.setting_value WHERE id = $1", agency_id) == 0


async def test_an_agency_row_needs_its_settings_row_and_leaves_with_it(admin: asyncpg.Connection) -> None:
    tenant_id = uuid.uuid4()
    tx = admin.transaction()
    await tx.start()
    try:
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await admin.execute("SELECT set_config('app.actor', 'seed:test', true)")
        await admin.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant_id)
        await admin.execute("SELECT set_config('app.actor', $1, true)", STAFF)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with admin.transaction():
                await admin.execute(PROPOSE, "crm.ai.budget_max_usd", tenant_id, "5", 1, STAFF, REASON)
        await admin.execute("INSERT INTO crm.tenant_settings DEFAULT VALUES")
        vid = await admin.fetchval(PROPOSE, "crm.ai.budget_max_usd", tenant_id, "5", 1, STAFF, REASON)
        await admin.execute("DELETE FROM crm.tenant_settings WHERE tenant_id = $1", tenant_id)
        assert await admin.fetchval("SELECT count(*) FROM crm.setting_value WHERE id = $1", vid) == 0
    finally:
        await tx.rollback()


async def test_even_the_owner_never_truncates_the_values(admin: asyncpg.Connection) -> None:
    tx = admin.transaction()
    await tx.start()
    try:
        await admin.execute("SET LOCAL ROLE crm_owner")
        with pytest.raises(asyncpg.CheckViolationError, match="truncated"):
            await admin.execute("TRUNCATE crm.setting_value")
    finally:
        await tx.rollback()


async def test_readers_get_nothing(admin: asyncpg.Connection) -> None:
    for role in READERS:
        for table in ("crm.setting_definition", "crm.setting_value"):
            allowed = await admin.fetchval("SELECT has_table_privilege($1, $2, 'SELECT') AS ok", role, table)
            assert allowed is False, (role, table)


async def test_a_json_table_entry_outside_the_bounds_is_refused(admin: asyncpg.Connection) -> None:
    """No seeded key is a json table yet; a migration-written one (as crm_owner) still has its entries checked."""
    tx = admin.transaction()
    await tx.start()
    try:
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute(
            "INSERT INTO crm.setting_definition (key, app, area, label, description, value_type, min_value, "
            "max_value, default_value, impact, approval) VALUES ('crm.zz.table', 'crm', 'zz', 'Zz table', "
            "'An invented json table setting for this test only', 'json', 0, 10, '{\"a\": 1}', 'low', 'single')"
        )
        await admin.execute(PROPOSE, "crm.zz.table", None, '{"a": 5}', 1, STAFF, REASON)
        with pytest.raises(asyncpg.CheckViolationError, match="is outside 0..10"):
            await admin.execute(PROPOSE, "crm.zz.table", None, '{"a": 50}', 2, STAFF, REASON)
    finally:
        await tx.rollback()
