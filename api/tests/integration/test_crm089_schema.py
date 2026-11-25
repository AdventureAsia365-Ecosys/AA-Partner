"""crm-089 (AA-1011 part 3, a contract file) on PostgreSQL with the RDS role shape.

- ``crm.integration_credential.provider`` takes ``google_calendar`` / ``microsoft_calendar`` beside the member's mailbox
  credential of the same member;
- ``crm.calendar_connection``: checks, one per member and provider, the guard (CRM57: own calendar only, fixed member /
  provider / credential, only the member or an owner changes it, the push counters belong to the job), active ->
  revoked only, crm_app never deletes one, the credential delete takes it, tenant isolation, and the agency erase.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import OWNER
from tests.integration.test_crm010_schema import ISS, _agency, _count, _ctx, _erase

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SEALED = b"\x00\x01" + b"s" * 40  # a stand-in ciphertext (length only; the vault is tested in the code PR)
CALENDAR_PROVIDERS = ("google_calendar", "microsoft_calendar")
_TENANTS: set[uuid.UUID] = set()


CRM089 = Path(__file__).resolve().parents[2] / "migrations" / "089_crm_calendar_connection.sql"


@pytest_asyncio.fixture(autouse=True)
async def _crm089(admin: asyncpg.Connection) -> AsyncIterator[None]:
    """crm-089 again (re-runnable) before each test: an earlier test of the session may have re-run an older file
    (test_crm057_schema re-runs crm-057 before each of its tests, whose provider CHECK lacks the calendar providers)."""
    await admin.execute(f"SET ROLE {OWNER}")
    try:
        await admin.execute(CRM089.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    yield


@pytest_asyncio.fixture(autouse=True)
async def _no_calendar_credential_left(app_pool: asyncpg.Pool) -> Any:
    """Delete the calendar credentials (and so the connections) these tests made: test_reapply_is_a_no_op re-runs
    crm-057, whose provider CHECK does not list the calendar providers, and fails on a row left behind."""
    yield
    for tenant in list(_TENANTS):
        async with app_pool.acquire() as conn, conn.transaction():
            await _ctx(conn, tenant, "job:crm_calendar_push")
            await conn.execute(
                "DELETE FROM crm.integration_credential WHERE provider = ANY($1::text[])", list(CALENDAR_PROVIDERS)
            )
    _TENANTS.clear()


async def _run(pool: asyncpg.Pool, tenant: uuid.UUID, sql: str, *args: Any, actor: str, role: str = "") -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await _ctx(conn, tenant, actor)
        await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        return await conn.fetchval(sql, *args)


async def _refused(coro: Any) -> str:
    with pytest.raises(asyncpg.PostgresError) as err:
        await coro
    return f"{err.value.sqlstate} {err.value}"


async def _member(pool: asyncpg.Pool, tenant: uuid.UUID, role: str = "agent") -> uuid.UUID:
    return await _run(
        pool, tenant,
        "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, $3, 'active') "
        "RETURNING member_id", ISS, f"sub-{uuid.uuid4()}", role, actor="seed:test",
    )  # fmt: skip


async def _credential(pool: asyncpg.Pool, tenant: uuid.UUID, member: uuid.UUID, provider: str) -> uuid.UUID:
    _TENANTS.add(tenant)
    return await _run(
        pool, tenant,
        "INSERT INTO crm.integration_credential (provider, member_id, secret_enc, status) "
        "VALUES ($1, $2, $3, 'connected') RETURNING integration_credential_id",
        provider, member, SEALED, actor=f"member:{member}",
    )  # fmt: skip


async def _connect(
    pool: asyncpg.Pool, tenant: uuid.UUID, member: uuid.UUID, provider: str = "google", **kw: Any
) -> Any:
    credential = kw.get("credential") or await _credential(pool, tenant, member, f"{provider}_calendar")
    return await _run(
        pool, tenant,
        "INSERT INTO crm.calendar_connection (member_id, provider, scope, credential_id, address_enc) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING calendar_connection_id",
        member, kw.get("as_provider", provider), kw.get("scope", "mine"), credential, SEALED,
        actor=kw.get("actor", f"member:{member}"),
    )  # fmt: skip


async def test_crm089_is_registered_with_row_security(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 AS ok FROM shared.schema_versions WHERE version = 'crm-089'") == 1
    assert await admin.fetchval(
        "SELECT relrowsecurity AND relforcerowsecurity AS ok FROM pg_class "
        "WHERE oid = 'crm.calendar_connection'::regclass"
    )
    grants = await admin.fetchval(
        "SELECT array_agg(privilege_type::text ORDER BY privilege_type) AS privs "
        "FROM information_schema.table_privileges WHERE table_schema = 'crm' "
        "AND table_name = 'calendar_connection' AND grantee = 'crm_app'"
    )
    assert sorted(grants) == ["INSERT", "SELECT", "UPDATE"]


async def test_calendar_credentials_sit_beside_the_mailbox_credential(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    member = ids["member"]
    for provider in ("google", "google_calendar", "microsoft_calendar"):
        assert await _credential(app_pool, ids["tenant"], member, provider) is not None
    assert "23514" in await _refused(_credential(app_pool, ids["tenant"], member, "outlook_calendar"))
    assert "23505" in await _refused(_credential(app_pool, ids["tenant"], member, "google_calendar"))


async def test_connection_checks_and_one_per_member_and_provider(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t, member = ids["tenant"], ids["member"]
    assert await _connect(app_pool, t, member) is not None
    assert await _connect(app_pool, t, member, "microsoft", scope="agency") is not None
    spare = await _credential(app_pool, t, await _member(app_pool, t), "google_calendar")
    refused = await _refused(_connect(app_pool, t, member, "google", credential=spare))
    assert "calendar_connection_member_provider_uq" in refused
    other = await _member(app_pool, t)
    assert "calendar_connection_scope_chk" in await _refused(_connect(app_pool, t, other, scope="team"))
    third = await _member(app_pool, t)
    assert "calendar_connection_provider_chk" in await _refused(
        _connect(app_pool, t, third, "google", as_provider="apple")
    )


async def test_the_guard_keeps_a_connection_to_its_member(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t, owner = ids["tenant"], ids["member"]
    agent = await _member(app_pool, t)
    colleague = await _member(app_pool, t)
    # A member connects only their own calendar.
    credential = await _credential(app_pool, t, agent, "google_calendar")
    refused = await _refused(_connect(app_pool, t, agent, credential=credential, actor=f"member:{colleague}"))
    assert "CRM57" in refused
    conn_id = await _connect(app_pool, t, agent, credential=credential)
    update = "UPDATE crm.calendar_connection SET scope = 'agency' WHERE calendar_connection_id = $1 RETURNING scope"
    assert "CRM57" in await _refused(_run(app_pool, t, update, conn_id, actor=f"member:{colleague}", role="agent"))
    assert await _run(app_pool, t, update, conn_id, actor=f"member:{owner}", role="owner") == "agency"
    # The member, provider and credential are fixed; the push counters are the job's.
    fixed = "UPDATE crm.calendar_connection SET provider = 'microsoft' WHERE calendar_connection_id = $1"
    assert "CRM57" in await _refused(_run(app_pool, t, fixed, conn_id, actor=f"member:{agent}"))
    counters = "UPDATE crm.calendar_connection SET event_count = 3 WHERE calendar_connection_id = $1"
    assert "CRM57" in await _refused(_run(app_pool, t, counters, conn_id, actor=f"member:{agent}"))
    await _run(app_pool, t, counters, conn_id, actor="job:crm_calendar_push")
    assert "CRM57" in await _refused(_run(app_pool, t, counters, conn_id, actor="calendar_feed:x"))


async def test_revoked_is_final_and_crm_app_never_deletes(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t, member = ids["tenant"], ids["member"]
    conn_id = await _connect(app_pool, t, member)
    revoke = (
        "UPDATE crm.calendar_connection SET status = 'revoked', revoked_at = now(), revoked_by = $2 "
        "WHERE calendar_connection_id = $1 RETURNING status"
    )
    assert await _run(app_pool, t, revoke, conn_id, f"member:{member}", actor=f"member:{member}") == "revoked"
    back = (
        "UPDATE crm.calendar_connection SET status = 'active', revoked_at = NULL, revoked_by = NULL "
        "WHERE calendar_connection_id = $1"
    )
    refused = await _refused(_run(app_pool, t, back, conn_id, actor="job:crm_calendar_push"))
    assert "not an allowed transition" in refused
    half = "UPDATE crm.calendar_connection SET revoked_by = NULL WHERE calendar_connection_id = $1"
    assert "calendar_connection_revoked_chk" in await _refused(_run(app_pool, t, half, conn_id, actor="job:x"))
    delete = "DELETE FROM crm.calendar_connection WHERE calendar_connection_id = $1"
    assert "42501" in await _refused(_run(app_pool, t, delete, conn_id, actor=f"member:{member}"))


async def test_deleting_the_credential_removes_the_connection(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    t, member = ids["tenant"], ids["member"]
    credential = await _credential(app_pool, t, member, "google_calendar")
    await _connect(app_pool, t, member, credential=credential)
    await _run(
        app_pool, t, "DELETE FROM crm.integration_credential WHERE integration_credential_id = $1", credential,
        actor="job:crm_calendar_push",
    )  # fmt: skip
    assert await _count(app_pool, t, "calendar_connection") == 0


async def test_connection_is_isolated_and_erased_with_the_agency(app_pool: asyncpg.Pool) -> None:
    ids = await _agency(app_pool)
    other = await _agency(app_pool)
    await _connect(app_pool, ids["tenant"], ids["member"])
    assert await _count(app_pool, ids["tenant"], "calendar_connection") == 1
    assert await _count(app_pool, other["tenant"], "calendar_connection") == 0
    # Writing a row into another agency is refused by the policy (the default tenant_id is the context's).
    spoof = (
        "INSERT INTO crm.calendar_connection (tenant_id, member_id, provider, credential_id, address_enc) "
        "VALUES ($1, $2, 'google', $3, $4)"
    )
    credential = await _credential(app_pool, ids["tenant"], ids["member"], "microsoft_calendar")
    refused = await _refused(_run(app_pool, other["tenant"], spoof, ids["tenant"], ids["member"], credential, SEALED,
                                  actor=f"member:{ids['member']}"))  # fmt: skip
    assert "42501" in refused or "row-level security" in refused
    async with app_pool.acquire() as conn, conn.transaction():
        await _ctx(conn, ids["tenant"])
        await conn.execute("UPDATE crm.tenant SET status = 'offboarding'")
    await _erase(app_pool, ids["tenant"], "tenant", ids["tenant"])
    assert await _count(app_pool, ids["tenant"], "calendar_connection") == 0
