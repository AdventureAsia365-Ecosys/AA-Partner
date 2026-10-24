"""crm-008 (AA-939, card CRM-11) on PostgreSQL 15 with the RDS role shape: the F4 schema rules, raw SQL only.

- Membership writes: inside a member request only an owner writes ``crm.member`` (SQLSTATE CRM02); an accepting
  invitation writes exactly the member it names; platform contexts may write.
- **E** (card): the last active owner cannot stop being one inside the agency (CRM01, also under a race of two
  owners); a platform deactivation is the one way out, and then only an owner recovery (14 days) makes a new owner.
- Invitations: the token-hash lookup finds one row before the agency is known; only an owner invites an owner.
- Owner recovery: only a platform context opens / completes it, never before ``eligible_at`` (CRM03), >= 14 days.
- Entitlements: platform-only writes (CRM04), a fixed feature list. Readers get 42501 on every new table.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import pytest

from tests.integration.conftest import READERS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTPARTN"
BLOB = b"\x00\x01" + b"\x07" * 40
BIDX = b"\x05" * 16
NEW_TABLES = ("member_invitation", "owner_recovery_request", "tenant_entitlement")


@asynccontextmanager
async def ctx(
    pool: asyncpg.Pool, tenant: uuid.UUID | None, actor: str = "", role: str = "", **settings: str
) -> AsyncIterator[asyncpg.Connection]:
    """One transaction as crm_app with the settings tenant_tx() / the platform contexts would set."""
    async with pool.acquire() as conn, conn.transaction():
        if tenant is not None:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        if actor:
            await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        if role:
            await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        for name, value in settings.items():
            await conn.execute("SELECT set_config($1, $2, true)", f"app.{name}", value)
        yield conn


async def _agency(pool: asyncpg.Pool, *roles: str) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """An agency with one active member per role, written by the seeder's platform context."""
    tenant = uuid.uuid4()
    members = []
    async with ctx(pool, tenant, actor="seed:test") as conn:
        await conn.execute("INSERT INTO crm.tenant (tenant_id) VALUES ($1)", tenant)
        for role in roles:
            members.append(
                await conn.fetchval(
                    "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, $3, 'active') "
                    "RETURNING member_id",
                    ISS,
                    f"sub-{uuid.uuid4()}",
                    role,
                )
            )
    return tenant, members


def _code(exc: pytest.ExceptionInfo[BaseException]) -> str:
    return getattr(exc.value, "sqlstate", "")


async def test_crm008_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'crm-008'") == 1


async def test_only_an_owner_writes_memberships_inside_a_member_request(app_pool: asyncpg.Pool) -> None:
    tenant, (owner, agent, assistant) = await _agency(app_pool, "owner", "agent", "assistant")
    for actor, role in ((f"member:{agent}", "agent"), (f"member:{assistant}", "assistant")):
        with pytest.raises(asyncpg.PostgresError) as exc:
            async with ctx(app_pool, tenant, actor, role) as conn:
                await conn.execute("UPDATE crm.member SET role = 'owner' WHERE member_id = $1", agent)
        assert _code(exc) == "CRM02"
        with pytest.raises(asyncpg.PostgresError) as exc:
            async with ctx(app_pool, tenant, actor, role) as conn:
                await conn.execute(
                    "INSERT INTO crm.member (cognito_iss, cognito_sub, role) VALUES ($1, 'x', 'agent')", ISS
                )
        assert _code(exc) == "CRM02"
    async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
        await conn.execute("UPDATE crm.member SET role = 'agent' WHERE member_id = $1", assistant)
        await conn.execute("UPDATE crm.member SET status = 'disabled' WHERE member_id = $1", agent)
    # Any other actor kind (a job, the intake form) never writes memberships.
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, "job:crm_intake_process") as conn:
            await conn.execute("UPDATE crm.member SET status = 'active' WHERE member_id = $1", agent)
    assert _code(exc) == "CRM02"


async def test_e_last_owner_stays_unless_a_platform_context_acts(app_pool: asyncpg.Pool) -> None:
    tenant, (owner, agent) = await _agency(app_pool, "owner", "agent")
    for change in (
        "status = 'disabled'",
        "role = 'agent'",
        "deleted_at = now(), deleted_by = 'x', purge_after = now() + interval '7 days'",
    ):
        with pytest.raises(asyncpg.PostgresError) as exc:
            async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
                await conn.execute(f"UPDATE crm.member SET {change} WHERE member_id = $1", owner)  # noqa: S608
        assert _code(exc) == "CRM01"
    # A second owner makes it possible; the first may then step down.
    async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
        await conn.execute("UPDATE crm.member SET role = 'owner' WHERE member_id = $1", agent)
        await conn.execute("UPDATE crm.member SET role = 'agent' WHERE member_id = $1", owner)
    # AA deactivates the last owner (owner left): allowed, and the agency now has no owner.
    async with ctx(app_pool, tenant, "platform:l1:staff-1") as conn:
        await conn.execute("UPDATE crm.member SET status = 'disabled' WHERE member_id = $1", agent)
        owners = await conn.fetchval(
            "SELECT count(*) FROM crm.member WHERE role = 'owner' AND status = 'active' AND deleted_at IS NULL"
        )
    assert owners == 0
    # Recovery only: nobody inside the agency can make an owner (the remaining agent is not an owner).
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, f"member:{owner}", "agent") as conn:
            await conn.execute("UPDATE crm.member SET role = 'owner' WHERE member_id = $1", owner)
    assert _code(exc) == "CRM02"


async def test_two_owners_demoting_each_other_at_once_cannot_both_pass(migrated_app_dsn: str) -> None:
    pool = await asyncpg.create_pool(migrated_app_dsn, min_size=0, max_size=3)
    try:
        tenant, (a, b) = await _agency(pool, "owner", "owner")

        async def demote(me: uuid.UUID, other: uuid.UUID, hold: asyncio.Event, go: asyncio.Event) -> str:
            try:
                async with ctx(pool, tenant, f"member:{me}", "owner") as conn:
                    await conn.execute("UPDATE crm.member SET updated_at = now() WHERE member_id = $1", me)
                    hold.set()
                    await go.wait()
                    await conn.execute("UPDATE crm.member SET status = 'disabled' WHERE member_id = $1", other)
                return "ok"
            except asyncpg.PostgresError as exc:
                return exc.sqlstate or "error"

        held_a, held_b, go = asyncio.Event(), asyncio.Event(), asyncio.Event()
        task_a = asyncio.create_task(demote(a, b, held_a, go))
        task_b = asyncio.create_task(demote(b, a, held_b, go))
        await held_a.wait()
        await held_b.wait()
        go.set()
        results = sorted(await asyncio.gather(task_a, task_b))
        # Exactly one succeeds; the other hits the last-owner rule (or a deadlock the database breaks).
        assert results[1] == "ok" and results[0] in {"CRM01", "40P01"}
    finally:
        await pool.close()


def _token_hash() -> bytes:
    return hashlib.sha256(os.urandom(32)).digest()


async def _invite(
    pool: asyncpg.Pool, tenant: uuid.UUID, actor: str, role_ctx: str, role: str
) -> tuple[uuid.UUID, bytes]:
    token_hash = _token_hash()
    async with ctx(pool, tenant, actor, role_ctx) as conn:
        invitation = await conn.fetchval(
            "INSERT INTO crm.member_invitation (role, email_enc, email_bidx, token_hash, expires_at) "
            "VALUES ($1, $2, $3, $4, now() + interval '7 days') RETURNING invitation_id",
            role,
            BLOB,
            os.urandom(16),
            token_hash,
        )
    return invitation, token_hash


async def test_invitations_lookup_by_token_hash_and_owner_rules(app_pool: asyncpg.Pool) -> None:
    tenant, (owner, agent) = await _agency(app_pool, "owner", "agent")
    other, _ = await _agency(app_pool, "owner")
    invitation, token_hash = await _invite(app_pool, tenant, f"member:{owner}", "owner", "agent")
    await _invite(app_pool, other, "seed:test", "", "agent")
    # Before the agency is known only the matching row is visible; a wrong hash shows nothing.
    async with ctx(app_pool, None, invite_token_hash=token_hash.hex()) as conn:
        rows = await conn.fetch("SELECT invitation_id, tenant_id FROM crm.member_invitation")
    assert [(r["invitation_id"], r["tenant_id"]) for r in rows] == [(invitation, tenant)]
    async with ctx(app_pool, None, invite_token_hash=_token_hash().hex()) as conn:
        assert await conn.fetch("SELECT 1 FROM crm.member_invitation") == []
    async with ctx(app_pool, None) as conn:
        assert await conn.fetch("SELECT 1 FROM crm.member_invitation") == []
    # The lookup grants no write.
    async with ctx(app_pool, None, invite_token_hash=token_hash.hex()) as conn:
        assert await conn.execute("UPDATE crm.member_invitation SET status = 'revoked'") == "UPDATE 0"
    # An agent never invites; only an owner (or the platform) invites an owner.
    with pytest.raises(asyncpg.PostgresError) as exc:
        await _invite(app_pool, tenant, f"member:{agent}", "agent", "assistant")
    assert _code(exc) == "CRM02"
    await _invite(app_pool, tenant, f"member:{owner}", "owner", "owner")
    await _invite(app_pool, tenant, "platform:l1:staff-1", "", "owner")
    # One live invitation per address per agency.
    bidx = os.urandom(16)
    async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
        await conn.execute(
            "INSERT INTO crm.member_invitation (role, email_enc, email_bidx, token_hash, expires_at) "
            "VALUES ('agent', $1, $2, $3, now() + interval '7 days')",
            BLOB,
            bidx,
            _token_hash(),
        )
    with pytest.raises(asyncpg.UniqueViolationError):
        async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
            await conn.execute(
                "INSERT INTO crm.member_invitation (role, email_enc, email_bidx, token_hash, expires_at) "
                "VALUES ('agent', $1, $2, $3, now() + interval '7 days')",
                BLOB,
                bidx,
                _token_hash(),
            )
    with pytest.raises(asyncpg.CheckViolationError):
        async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
            await conn.execute(
                "INSERT INTO crm.member_invitation (role, email_enc, email_bidx, token_hash, expires_at) "
                "VALUES ('agent', $1, $2, $3, now() + interval '31 days')",
                BLOB,
                os.urandom(16),
                _token_hash(),
            )


async def test_an_accepting_invitation_writes_only_the_member_it_names(app_pool: asyncpg.Pool) -> None:
    tenant, (owner,) = await _agency(app_pool, "owner")
    invitation, _ = await _invite(app_pool, tenant, f"member:{owner}", "owner", "owner")
    sub = f"sub-{uuid.uuid4()}"
    async with ctx(app_pool, tenant, f"invitation:{invitation}") as conn:
        await conn.execute(
            "UPDATE crm.member_invitation SET status = 'claimed', cognito_iss = $1, cognito_sub = $2, "
            "claimed_at = now() WHERE invitation_id = $3",
            ISS,
            sub,
            invitation,
        )
    for role, subject in (("owner", "someone-else"), ("agent", sub)):
        with pytest.raises(asyncpg.PostgresError) as exc:
            async with ctx(app_pool, tenant, f"invitation:{invitation}") as conn:
                await conn.execute(
                    "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, $3, 'active')",
                    ISS,
                    subject,
                    role,
                )
        assert _code(exc) == "CRM02"
    async with ctx(app_pool, tenant, f"invitation:{invitation}") as conn:
        member = await conn.fetchval(
            "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, 'owner', 'active') "
            "RETURNING member_id",
            ISS,
            sub,
        )
        await conn.execute(
            "UPDATE crm.member_invitation SET status = 'accepted', accepted_at = now(), member_id = $1 "
            "WHERE invitation_id = $2",
            member,
            invitation,
        )
    # Once accepted the invitation names nobody any more (status is no longer claimed).
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, f"invitation:{invitation}") as conn:
            await conn.execute("UPDATE crm.member SET status = 'disabled' WHERE member_id = $1", member)
    assert _code(exc) == "CRM02"


async def test_owner_recovery_is_platform_only_and_waits_14_days(app_pool: asyncpg.Pool) -> None:
    tenant, (owner, agent) = await _agency(app_pool, "owner", "agent")
    open_sql = (
        "INSERT INTO crm.owner_recovery_request (candidate_member_id, reason, eligible_at) "
        "VALUES ($1, 'owner_left', now() + make_interval(days => $2::int)) RETURNING request_id"
    )
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
            await conn.fetchval(open_sql, agent, 14)
    assert _code(exc) == "CRM04"
    with pytest.raises(asyncpg.CheckViolationError, match="owner_recovery_wait_chk"):
        async with ctx(app_pool, tenant, "platform:l1:staff-1") as conn:
            await conn.fetchval(open_sql, agent, 13)
    async with ctx(app_pool, tenant, "platform:l1:staff-1") as conn:
        request = await conn.fetchval(open_sql, agent, 14)
    with pytest.raises(asyncpg.UniqueViolationError):
        async with ctx(app_pool, tenant, "platform:l1:staff-1") as conn:
            await conn.fetchval(open_sql, agent, 15)
    complete = (
        "UPDATE crm.owner_recovery_request SET status = 'completed', decided_at = now(), decided_by = $2 "
        "WHERE request_id = $1"
    )
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, "platform:l1:staff-1") as conn:
            await conn.execute(complete, request, "platform:l1:staff-1")
    assert _code(exc) == "CRM03"
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
            await conn.execute(complete, request, f"member:{owner}")
    assert _code(exc) == "CRM04"
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, "platform:l1:staff-1") as conn:
            await conn.execute(
                "UPDATE crm.owner_recovery_request SET eligible_at = now() WHERE request_id = $1", request
            )
    assert _code(exc) == "CRM04"
    # Any member may cancel while pending.
    async with ctx(app_pool, tenant, f"member:{agent}", "agent") as conn:
        await conn.execute(
            "UPDATE crm.owner_recovery_request SET status = 'cancelled', decided_at = now(), decided_by = $2 "
            "WHERE request_id = $1",
            request,
            f"member:{agent}",
        )
    with pytest.raises(asyncpg.CheckViolationError, match="not an allowed transition"):
        async with ctx(app_pool, tenant, "platform:l1:staff-1") as conn:
            await conn.execute(complete, request, "platform:l1:staff-1")


async def test_entitlements_are_written_by_the_platform_only(app_pool: asyncpg.Pool) -> None:
    tenant, (owner,) = await _agency(app_pool, "owner")
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
            await conn.execute("INSERT INTO crm.tenant_entitlement (plan) VALUES ('pro')")
    assert _code(exc) == "CRM04"
    async with ctx(app_pool, tenant, "platform:l1:staff-1") as conn:
        await conn.execute("INSERT INTO crm.tenant_entitlement (allowed_features) VALUES (ARRAY['ai_assist'])")
        row = await conn.fetchrow("SELECT plan, member_limit, allowed_features FROM crm.tenant_entitlement")
    assert (row["plan"], row["member_limit"], row["allowed_features"]) == ("free", 10, ["ai_assist"])
    with pytest.raises(asyncpg.CheckViolationError):
        async with ctx(app_pool, tenant, "platform:l1:staff-1") as conn:
            await conn.execute("UPDATE crm.tenant_entitlement SET allowed_features = ARRAY['unlimited_everything']")
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
            await conn.execute("UPDATE crm.tenant_entitlement SET member_limit = 500")
    assert _code(exc) == "CRM04"


async def test_refund_threshold_default_and_no_delete_on_state_tables(app_pool: asyncpg.Pool) -> None:
    tenant, _ = await _agency(app_pool, "owner")
    async with ctx(app_pool, tenant, "seed:test") as conn:
        await conn.execute("INSERT INTO crm.tenant_settings DEFAULT VALUES")
        assert await conn.fetchval("SELECT refund_owner_threshold_base FROM crm.tenant_settings") == 1000
    for table in NEW_TABLES:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with ctx(app_pool, tenant, "seed:test") as conn:
                await conn.execute(f"DELETE FROM crm.{table}")  # noqa: S608


async def test_readers_reach_none_of_the_new_tables(app_pool: asyncpg.Pool) -> None:
    tenant, _ = await _agency(app_pool, "owner")
    for reader in READERS:
        for table in NEW_TABLES:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with ctx(app_pool, tenant) as conn:
                    await conn.execute(f"SET LOCAL ROLE {reader}")
                    await conn.fetch(f"SELECT 1 FROM crm.{table}")  # noqa: S608


# ── Review round (S249): the three bypasses of the first crm-008 draft, now refused ──────────────────────────────────
async def _owners(pool: asyncpg.Pool, tenant: uuid.UUID) -> int:
    async with ctx(pool, tenant, "seed:test") as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM crm.member WHERE role = 'owner' AND status = 'active' AND deleted_at IS NULL"
        )


async def test_no_crm_app_session_deletes_a_member(app_pool: asyncpg.Pool) -> None:
    """An agent deleting the only owner, the last owner deleting themself, even a platform actor: all CRM02."""
    tenant, (owner, agent) = await _agency(app_pool, "owner", "agent")
    for actor, role, target in (
        (f"member:{agent}", "agent", owner),
        (f"member:{owner}", "owner", owner),
        (f"member:{owner}", "owner", agent),
        ("platform:l1:staff-1", "", owner),
    ):
        with pytest.raises(asyncpg.PostgresError) as exc:
            async with ctx(app_pool, tenant, actor, role) as conn:
                await conn.execute("DELETE FROM crm.member WHERE member_id = $1", target)
        assert _code(exc) == "CRM02", actor
    assert await _owners(app_pool, tenant) == 1


async def test_a_members_identity_is_fixed_outside_platform_contexts(app_pool: asyncpg.Pool) -> None:
    """The agent cannot park its own subject and move it onto the owner's row (agent -> owner escalation)."""
    tenant, (owner, agent) = await _agency(app_pool, "owner", "agent")
    other, _ = await _agency(app_pool, "owner")
    async with ctx(app_pool, tenant, "seed:test") as conn:
        agent_sub = await conn.fetchval("SELECT cognito_sub FROM crm.member WHERE member_id = $1", agent)
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, f"member:{agent}", "agent") as conn:
            await conn.execute("UPDATE crm.member SET cognito_sub = 'parked' WHERE member_id = $1", agent)
            await conn.execute("UPDATE crm.member SET cognito_sub = $2 WHERE member_id = $1", owner, agent_sub)
    assert _code(exc) == "CRM02"
    # Not even the owner rebinds a row to another subject, issuer or agency.
    for change, value in (
        ("cognito_sub = $2", "someone-else"),
        ("cognito_iss = $2", ISS + "-other"),
        ("tenant_id = $2::uuid", str(other)),
        ("created_by = $2", "member:forged"),
    ):
        with pytest.raises(asyncpg.PostgresError) as exc:
            async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
                await conn.execute(f"UPDATE crm.member SET {change} WHERE member_id = $1", agent, value)  # noqa: S608
        assert _code(exc) == "CRM02", change
    async with ctx(app_pool, tenant, "seed:test") as conn:
        assert await conn.fetchval("SELECT cognito_sub FROM crm.member WHERE member_id = $1", agent) == agent_sub
    # A platform context (L1 console) may correct a subject.
    async with ctx(app_pool, tenant, "platform:l1:staff-1") as conn:
        await conn.execute("UPDATE crm.member SET cognito_sub = $2 WHERE member_id = $1", agent, f"{agent_sub}-fixed")


async def test_an_actorless_crm_app_session_writes_no_membership_and_no_platform_row(app_pool: asyncpg.Pool) -> None:
    tenant, (owner, agent) = await _agency(app_pool, "owner", "agent")
    for sql, args, code in (
        (
            "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, 'x', 'owner', 'active')",
            (ISS,),
            "CRM02",
        ),
        ("UPDATE crm.member SET role = 'owner' WHERE member_id = $1", (agent,), "CRM02"),
        ("UPDATE crm.member SET status = 'disabled' WHERE member_id = $1", (owner,), "CRM02"),
        ("INSERT INTO crm.tenant_entitlement (plan) VALUES ('pro')", (), "CRM04"),
        (
            "INSERT INTO crm.owner_recovery_request (candidate_member_id, reason, eligible_at) "
            "VALUES ($1, 'owner_left', now() + interval '14 days')",
            (agent,),
            "CRM04",
        ),
        (
            "INSERT INTO crm.member_invitation (role, email_enc, email_bidx, token_hash, expires_at) "
            "VALUES ('owner', $1, $2, $3, now() + interval '7 days')",
            (BLOB, BIDX, b"\x09" * 32),
            "CRM02",
        ),
    ):
        with pytest.raises(asyncpg.PostgresError) as exc:
            async with ctx(app_pool, tenant) as conn:
                await conn.execute(sql, *args)
        assert _code(exc) == code, sql


async def _claim(pool: asyncpg.Pool, tenant: uuid.UUID, invitation: uuid.UUID, sub: str) -> None:
    async with ctx(pool, tenant, f"invitation:{invitation}") as conn:
        await conn.execute(
            "UPDATE crm.member_invitation SET status = 'claimed', cognito_iss = $1, cognito_sub = $2, "
            "claimed_at = now() WHERE invitation_id = $3",
            ISS,
            sub,
            invitation,
        )


async def test_an_agent_cannot_rewrite_an_invitation(app_pool: asyncpg.Pool) -> None:
    """The draft let an agent swap the token hash of a pending OWNER invitation, then claim and accept it."""
    tenant, (owner, agent, assistant) = await _agency(app_pool, "owner", "agent", "assistant")
    invitation, _ = await _invite(app_pool, tenant, f"member:{owner}", "owner", "owner")
    attacker = hashlib.sha256(b"attacker token").digest()
    for actor, role in ((f"member:{agent}", "agent"), (f"member:{assistant}", "assistant"), ("job:x", "")):
        for change, args in (
            ("token_hash = $2", (attacker,)),
            ("status = 'revoked'", ()),
            ("expires_at = now() + interval '20 days'", ()),
            ("deleted_at = now(), deleted_by = 'x', purge_after = now()", ()),
            ("status = 'claimed', cognito_iss = $2, cognito_sub = 'x', claimed_at = now()", (ISS,)),
        ):
            with pytest.raises(asyncpg.PostgresError) as exc:
                async with ctx(app_pool, tenant, actor, role) as conn:
                    await conn.execute(
                        f"UPDATE crm.member_invitation SET {change} WHERE invitation_id = $1",  # noqa: S608
                        invitation,
                        *args,
                    )
            assert _code(exc) == "CRM02", (actor, change)
    async with ctx(app_pool, tenant, "seed:test") as conn:
        row = await conn.fetchrow(
            "SELECT status, token_hash FROM crm.member_invitation WHERE invitation_id = $1", invitation
        )
    assert row["status"] == "pending" and bytes(row["token_hash"]) != attacker


async def test_the_owner_only_invites_and_withdraws(app_pool: asyncpg.Pool) -> None:
    tenant, (owner,) = await _agency(app_pool, "owner")
    invitation, _ = await _invite(app_pool, tenant, f"member:{owner}", "owner", "agent")
    for change, args in (
        ("token_hash = $2", (_token_hash(),)),
        ("email_bidx = $2", (os.urandom(16),)),
        ("expires_at = now() + interval '20 days'", ()),
        ("status = 'claimed', cognito_iss = $2, cognito_sub = 'x', claimed_at = now()", (ISS,)),
    ):
        with pytest.raises(asyncpg.PostgresError) as exc:
            async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
                await conn.execute(
                    f"UPDATE crm.member_invitation SET {change} WHERE invitation_id = $1",  # noqa: S608
                    invitation,
                    *args,
                )
        assert _code(exc) == "CRM02", change
    # A new invitation starts pending and unbound.
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
            await conn.execute(
                "INSERT INTO crm.member_invitation (role, email_enc, email_bidx, token_hash, expires_at, status, "
                "cognito_iss, cognito_sub, claimed_at) VALUES ('owner', $1, $2, $3, now() + interval '7 days', "
                "'claimed', $4, 'x', now())",
                BLOB,
                os.urandom(16),
                _token_hash(),
                ISS,
            )
    assert _code(exc) == "CRM02"
    async with ctx(app_pool, tenant, f"member:{owner}", "owner") as conn:
        assert (
            await conn.execute(
                "UPDATE crm.member_invitation SET status = 'revoked' WHERE invitation_id = $1", invitation
            )
            == "UPDATE 1"
        )


async def test_an_invitation_moves_only_itself_and_only_forward(app_pool: asyncpg.Pool) -> None:
    tenant, (owner, agent) = await _agency(app_pool, "owner", "agent")
    invitation, _ = await _invite(app_pool, tenant, f"member:{owner}", "owner", "owner")
    sibling, _ = await _invite(app_pool, tenant, f"member:{owner}", "owner", "agent")
    async with ctx(app_pool, tenant, "seed:test") as conn:
        agent_sub = await conn.fetchval("SELECT cognito_sub FROM crm.member WHERE member_id = $1", agent)
    # Another invitation's actor, a token swap during the claim, an early expiry: refused.
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, f"invitation:{sibling}") as conn:
            await conn.execute(
                "UPDATE crm.member_invitation SET status = 'claimed', cognito_iss = $1, cognito_sub = 'x', "
                "claimed_at = now() WHERE invitation_id = $2",
                ISS,
                invitation,
            )
    assert _code(exc) == "CRM02"
    for change, args in (
        (
            "status = 'claimed', cognito_iss = $2, cognito_sub = 'x', claimed_at = now(), token_hash = $3",
            (ISS, _token_hash()),
        ),
        ("status = 'expired'", ()),
        ("status = 'revoked'", ()),
        ("expires_at = now() + interval '20 days'", ()),
    ):
        with pytest.raises(asyncpg.PostgresError) as exc:
            async with ctx(app_pool, tenant, f"invitation:{invitation}") as conn:
                await conn.execute(
                    f"UPDATE crm.member_invitation SET {change} WHERE invitation_id = $1",  # noqa: S608
                    invitation,
                    *args,
                )
        assert _code(exc) == "CRM02", change
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, f"invitation:{invitation}") as conn:
            await conn.execute(
                "INSERT INTO crm.member_invitation (role, email_enc, email_bidx, token_hash, expires_at) "
                "VALUES ('owner', $1, $2, $3, now() + interval '7 days')",
                BLOB,
                os.urandom(16),
                _token_hash(),
            )
    assert _code(exc) == "CRM02"
    # Claimed by a fresh subject; accept may not name an existing member of another subject (the agent).
    sub = f"sub-{uuid.uuid4()}"
    await _claim(app_pool, tenant, invitation, sub)
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, f"invitation:{invitation}") as conn:
            await conn.execute(
                "UPDATE crm.member_invitation SET status = 'accepted', accepted_at = now(), member_id = $1 "
                "WHERE invitation_id = $2",
                agent,
                invitation,
            )
    assert _code(exc) == "CRM02"
    # Nor re-point the claim at the agent's own subject after the fact.
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with ctx(app_pool, tenant, f"invitation:{invitation}") as conn:
            await conn.execute(
                "UPDATE crm.member_invitation SET cognito_sub = $1 WHERE invitation_id = $2", agent_sub, invitation
            )
    assert _code(exc) == "CRM02"


async def test_an_expired_invitation_may_be_marked_expired_by_its_own_step(app_pool: asyncpg.Pool) -> None:
    tenant, _ = await _agency(app_pool, "owner")
    async with ctx(app_pool, tenant, "platform:l1:staff-1") as conn:
        invitation = await conn.fetchval(
            "INSERT INTO crm.member_invitation (role, email_enc, email_bidx, token_hash, created_at, expires_at) "
            "VALUES ('agent', $1, $2, $3, now() - interval '8 days', now() - interval '1 day') RETURNING invitation_id",
            BLOB,
            os.urandom(16),
            _token_hash(),
        )
    async with ctx(app_pool, tenant, f"invitation:{invitation}") as conn:
        assert (
            await conn.execute(
                "UPDATE crm.member_invitation SET status = 'expired' WHERE invitation_id = $1", invitation
            )
            == "UPDATE 1"
        )
