"""F4 flows on PostgreSQL 15 with the RDS role shape (AA-939, card CRM-11), through the real contexts and services.

- Done when: invite -> active < 5 min (timed): the owner invites, the claim creates the partner-pool account
  (in-memory directory here; the Dev E2E times the real one), the person accepts with the claimed identity.
- **R**: an agent's export or client delete -> 403 with a committed ``denied`` event; a refund above the agency's
  threshold by an agent -> 403; an assistant never reaches the Team page.
- **E**: the last owner deactivated -> recovery only (nobody inside the agency can make an owner; the recovery
  completes only after 14 days); a deactivated agent's trips land in the reassign queue.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import asyncpg
import pytest

from app.auth.route_guard import Caller
from app.errors import Conflict, Forbidden
from app.members import invitations, recovery, team
from app.members.directory import MemoryDirectory
from app.members.mail import MemoryMailer
from app.permissions import Action, require
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_crm005_intake import ISS, _agency, _keys, _owner_fetch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ORIGIN = "https://partner.example.test"


def _caller(sub: str) -> Caller:
    return Caller("agency", "partner", "partner-web", ISS, sub, ())


async def _events(admin: asyncpg.Connection, tenant_id: uuid.UUID, action: str) -> list[asyncpg.Record]:
    return await _owner_fetch(
        admin,
        tenant_id,
        "SELECT actor_kind, actor_ref, action, outcome, object_kind, object_id, reason FROM crm.audit_event "
        "WHERE action = $1 ORDER BY at, event_id",
        action,
    )


async def _invite(pool: asyncpg.Pool, keys, owner: Caller, tenant_id: uuid.UUID, email: str, role: str):  # noqa: ANN001, ANN202
    mailer = MemoryMailer()
    async with tenant_tx(pool, owner, tenant_id, action=Action.TEAM_MANAGE) as ctx:
        out = await team.invite(ctx, Vault(keys, ctx), mailer, email=email, role=role, web_origin=ORIGIN, ttl_hours=168)
    token = mailer.sent[0].text.split("/join?t=")[1].split()[0]
    return out, token


async def test_done_when_invite_to_active_in_under_five_minutes(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    tenant_id, [(owner, _)] = await _agency(app_pool, "owner")
    directory = MemoryDirectory(ISS)
    started = time.monotonic()
    invited, token = await _invite(app_pool, keys, owner, tenant_id, "Lan.Nguyen@Agency.Example", "agent")
    claimed = await invitations.claim(app_pool, keys, directory, token)
    assert claimed.state == "created" and claimed.temporary_password and claimed.email == "lan.nguyen@agency.example"
    # (managed login: temporary password -> own password -> authenticator; the Dev E2E drives the real pool)
    person = _caller(directory.accounts["lan.nguyen@agency.example"]["sub"])
    accepted = await invitations.accept(app_pool, person, token)
    elapsed = time.monotonic() - started
    assert elapsed < 300 and accepted["member_role"] == "agent" and accepted["tenant_id"] == str(tenant_id)
    async with tenant_tx(app_pool, person, tenant_id, action=Action.PIPELINE_VIEW) as ctx:
        assert (ctx.member_role, str(ctx.member_id)) == ("agent", accepted["member_id"])
    # The Team page shows the address the member was invited with; the invitation is gone from the pending list.
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.TEAM_VIEW) as ctx:
        page = await team.team(ctx, Vault(keys, ctx))
    assert [m["email"] for m in page["members"] if m["role"] == "agent"] == ["lan.nguyen@agency.example"]
    assert page["invitations"] == [] and page["plan"]["seats_used"] == 2
    assert [e["outcome"] for e in await _events(admin, tenant_id, "invite")] == ["allowed"]
    [claim_event] = await _events(admin, tenant_id, "invitation_claim")
    assert (claim_event["actor_kind"], claim_event["actor_ref"]) == ("user", f"invitation:{invited['invitation_id']}")
    assert [e["outcome"] for e in await _events(admin, tenant_id, "invitation_accept")] == ["allowed"]
    # Single use: the token does nothing any more.
    with pytest.raises(invitations.Gone):
        await invitations.claim(app_pool, keys, directory, token)
    with pytest.raises(invitations.Gone):
        await invitations.accept(app_pool, person, token)
    # The address is a member now: inviting it again is refused.
    with pytest.raises(Conflict) as exc:
        await _invite(app_pool, keys, owner, tenant_id, "lan.nguyen@agency.example", "assistant")
    assert exc.value.code == "already_member"


async def test_accept_refuses_another_identity_and_records_the_denial(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    tenant_id, [(owner, _)] = await _agency(app_pool, "owner")
    directory = MemoryDirectory(ISS)
    _, token = await _invite(app_pool, keys, owner, tenant_id, "minh@agency.example", "assistant")
    await invitations.claim(app_pool, keys, directory, token)
    with pytest.raises(Forbidden) as exc:
        await invitations.accept(app_pool, _caller(f"intruder-{uuid.uuid4()}"), token)
    assert exc.value.code == "invitation_identity_mismatch"
    [event] = await _events(admin, tenant_id, "invitation_accept")
    assert (event["outcome"], event["reason"]) == ("denied", "identity_mismatch")
    # The rightful person still can.
    sub = directory.accounts["minh@agency.example"]["sub"]
    assert (await invitations.accept(app_pool, _caller(sub), token))["member_role"] == "assistant"


async def test_an_existing_account_is_bound_without_a_new_password(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant_id, [(owner, _)] = await _agency(app_pool, "owner")
    directory = MemoryDirectory(ISS)
    sub = directory.add("hoa@agency.example")
    _, token = await _invite(app_pool, keys, owner, tenant_id, "hoa@agency.example", "owner")
    claimed = await invitations.claim(app_pool, keys, directory, token)
    assert (claimed.state, claimed.temporary_password) == ("existing", None)
    assert (await invitations.accept(app_pool, _caller(sub), token))["member_role"] == "owner"


async def test_a_password_never_seen_is_reissued_by_the_same_link_and_by_a_new_invitation(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """S263, found live on Dev: the invitation was claimed once, the join page never showed the temporary password
    (a second submit replaced it) and the account stayed in FORCE_CHANGE_PASSWORD. Opening the same link again gives
    a new temporary password; so does a new invitation to the address after the owner revoked the first one."""
    keys = _keys()
    tenant_id, [(owner, _)] = await _agency(app_pool, "owner")
    directory = MemoryDirectory(ISS)
    invited, token = await _invite(app_pool, keys, owner, tenant_id, "kim@agency.example", "agent")
    first = await invitations.claim(app_pool, keys, directory, token)
    again = await invitations.claim(app_pool, keys, directory, token)
    assert (first.state, again.state) == ("created", "created")
    assert again.temporary_password and again.temporary_password != first.temporary_password
    account = directory.accounts["kim@agency.example"]
    assert account["password"] == again.temporary_password and directory.resent == [account["sub"]]
    reasons = [e["reason"] for e in await _events(admin, tenant_id, "invitation_claim")]
    assert reasons == ["account_created", "account_password_reissued"]
    # The owner withdraws it and invites the address again: the account is still never signed in -> a new password.
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.TEAM_MANAGE) as ctx:
        await team.revoke_invitation(ctx, uuid.UUID(invited["invitation_id"]))
    _, token2 = await _invite(app_pool, keys, owner, tenant_id, "kim@agency.example", "agent")
    fresh = await invitations.claim(app_pool, keys, directory, token2)
    assert fresh.state == "created" and fresh.temporary_password == account["password"]
    assert len(directory.accounts) == 1 and directory.resent == [account["sub"], account["sub"]]
    assert (await invitations.accept(app_pool, _caller(account["sub"]), token2))["member_role"] == "agent"
    # Once the person has their own password, the link answers already_claimed (no password) — here: used.
    account["status"] = "CONFIRMED"
    with pytest.raises(invitations.Gone):
        await invitations.claim(app_pool, keys, directory, token2)


async def test_revoked_and_duplicate_invitations(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant_id, [(owner, _)] = await _agency(app_pool, "owner")
    invited, token = await _invite(app_pool, keys, owner, tenant_id, "dup@agency.example", "agent")
    with pytest.raises(Conflict) as exc:
        await _invite(app_pool, keys, owner, tenant_id, "DUP@agency.example", "agent")
    assert exc.value.code == "invitation_exists"
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.TEAM_MANAGE) as ctx:
        await team.revoke_invitation(ctx, uuid.UUID(invited["invitation_id"]))
    with pytest.raises(invitations.Gone):
        await invitations.claim(app_pool, keys, MemoryDirectory(ISS), token)
    await _invite(app_pool, keys, owner, tenant_id, "dup@agency.example", "agent")  # a new one is fine now


async def test_r_agent_export_delete_and_refund_above_threshold_are_403(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    tenant_id, [(_, _), (agent, _), (assistant, _)] = await _agency(app_pool, "owner", "agent", "assistant")
    for action in (Action.CLIENT_EXPORT, Action.AGENCY_EXPORT, Action.CLIENT_DELETE):
        with pytest.raises(Forbidden) as exc:
            async with tenant_tx(app_pool, agent, tenant_id, action=action):
                raise AssertionError("refused before any work")
        assert (exc.value.status_code, exc.value.code) == (403, "role_forbidden")
        [event] = await _events(admin, tenant_id, action.value)
        assert (event["outcome"], event["object_kind"], event["reason"]) == ("denied", "permission", "role_forbidden")
    with pytest.raises(Forbidden):
        async with tenant_tx(app_pool, assistant, tenant_id, action=Action.TEAM_VIEW):
            pass
    # Refund threshold from the agency's settings (crm-008 default 1000).
    async with tenant_tx(app_pool, agent, tenant_id, action=Action.REFUND_APPROVE) as ctx:
        await ctx.conn.execute("INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT DO NOTHING")
        threshold = await ctx.conn.fetchval("SELECT refund_owner_threshold_base FROM crm.tenant_settings")
        require(ctx, Action.REFUND_APPROVE, object_kind="refund", amount=900, threshold=threshold)
    with pytest.raises(Forbidden) as exc:
        async with tenant_tx(app_pool, agent, tenant_id, action=Action.REFUND_APPROVE) as ctx:
            threshold = await ctx.conn.fetchval("SELECT refund_owner_threshold_base FROM crm.tenant_settings")
            require(ctx, Action.REFUND_APPROVE, object_kind="refund", amount=1500, threshold=threshold)
    assert (exc.value.status_code, exc.value.code) == (403, "above_threshold")
    assert [e["reason"] for e in await _events(admin, tenant_id, "refund_approve")] == ["above_threshold"]


async def test_role_change_and_the_last_owner(app_pool: asyncpg.Pool) -> None:
    tenant_id, [(owner, owner_id), (_, agent_id)] = await _agency(app_pool, "owner", "agent")
    with pytest.raises(Conflict) as exc:
        async with tenant_tx(app_pool, owner, tenant_id, action=Action.TEAM_MANAGE) as ctx:
            await team.change_role(ctx, owner_id, "agent")
    assert exc.value.code == "last_owner"
    with pytest.raises(Conflict) as exc:
        async with tenant_tx(app_pool, owner, tenant_id, action=Action.TEAM_MANAGE) as ctx:
            await team.remove_member(ctx, None, owner_id)
    assert exc.value.code == "last_owner"
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.TEAM_MANAGE) as ctx:
        await team.change_role(ctx, agent_id, "owner")
        await team.change_role(ctx, owner_id, "assistant")
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.SESSION_VIEW) as ctx:
        assert ctx.member_role == "assistant"


async def test_e_deactivated_agent_s_trips_land_in_the_reassign_queue(app_pool: asyncpg.Pool) -> None:
    tenant_id, [(owner, owner_id), (_, agent_id), (_, other_id)] = await _agency(app_pool, "owner", "agent", "agent")
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.TRIP_EDIT) as ctx:
        open_trip = await ctx.conn.fetchval(
            "INSERT INTO crm.trip (title, owner_member_id) VALUES ('Laos, 7 nights', $1) RETURNING trip_id", agent_id
        )
        closed_trip = await ctx.conn.fetchval(
            "INSERT INTO crm.trip (title, owner_member_id, stage, lost_reason_code) "
            "VALUES ('Old', $1, 'lost', 'price') RETURNING trip_id",
            agent_id,
        )
        await ctx.conn.execute(
            "INSERT INTO crm.task (trip_id, kind, assignee_member_id) VALUES ($1, 'reply_2h', $2)", open_trip, agent_id
        )
    directory = MemoryDirectory(ISS)
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.TEAM_MANAGE) as ctx:
        removed = await team.remove_member(ctx, directory, agent_id)
    assert removed["to_reassign"] == 1 and removed["status"] == "disabled"
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.REASSIGN) as ctx:
        queue = await team.reassign_queue(ctx)
    assert [t["trip_id"] for t in queue["trips"]] == [str(open_trip)]
    assert str(closed_trip) not in {t["trip_id"] for t in queue["trips"]}
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.REASSIGN) as ctx:
        moved = await team.reassign(ctx, agent_id, other_id, None)
    assert moved["trips"] == 1
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.REASSIGN) as ctx:
        assert (await team.reassign_queue(ctx))["trips"] == []
        assert await ctx.conn.fetchval("SELECT owner_member_id FROM crm.trip WHERE trip_id = $1", open_trip) == other_id
        assert (
            await ctx.conn.fetchval("SELECT assignee_member_id FROM crm.task WHERE trip_id = $1", open_trip) == other_id
        )
    # The removed member is refused at once.
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.TEAM_VIEW) as ctx:
        statuses = {m["member_id"]: m["status"] for m in (await team.team(ctx, Vault(_keys(), ctx)))["members"]}
    assert statuses[str(agent_id)] == "disabled" and owner_id


async def test_e_last_owner_deactivated_means_recovery_only(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    tenant_id, [(_, owner_id), (agent, agent_id)] = await _agency(app_pool, "owner", "agent")
    await recovery.deactivate_member(app_pool, tenant_id, owner_id, staff_sub="staff-1", directory=None)
    listed = await recovery.members(app_pool, tenant_id, staff_sub="staff-1")
    assert listed["active_owners"] == 0
    # Inside the agency nobody can make an owner: the matrix refuses, and the database would too.
    with pytest.raises(Forbidden):
        async with tenant_tx(app_pool, agent, tenant_id, action=Action.TEAM_MANAGE):
            pass
    with pytest.raises(asyncpg.PostgresError):
        async with tenant_tx(app_pool, agent, tenant_id, action=Action.SESSION_VIEW) as ctx:
            await ctx.conn.execute("UPDATE crm.member SET role = 'owner' WHERE member_id = $1", agent_id)
    # Recovery: opened by AA, the banner for every member, not completable before 14 days.
    opened = await recovery.open_request(
        app_pool,
        tenant_id,
        candidate_member_id=agent_id,
        reason="owner_left",
        staff_sub="staff-1",
        directory=None,
        mailer=None,
        web_origin=ORIGIN,
    )
    async with tenant_tx(app_pool, agent, tenant_id, action=Action.RECOVERY_VIEW) as ctx:
        banner = (await recovery.banner(ctx))["recovery"]
    assert banner["request_id"] == opened["request_id"] and banner["is_candidate"]
    with pytest.raises(Conflict) as exc:
        await recovery.complete_request(app_pool, tenant_id, uuid.UUID(opened["request_id"]), staff_sub="staff-1")
    assert exc.value.code == "recovery_not_due"
    # A member cancels within the 14 days: nothing changes (CRM-13a E test, member side).
    async with tenant_tx(app_pool, agent, tenant_id, action=Action.RECOVERY_CANCEL) as ctx:
        await recovery.cancel(ctx, uuid.UUID(opened["request_id"]))
    # A request opened 15 days ago (written directly, as the platform) completes, and the candidate is the owner.
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', 'platform:staff-1', true)")
        old = await conn.fetchval(
            "INSERT INTO crm.owner_recovery_request (candidate_member_id, reason, created_at, eligible_at) "
            "VALUES ($1, 'owner_left', now() - interval '15 days', now() - interval '1 day') RETURNING request_id",
            agent_id,
        )
    done = await recovery.complete_request(app_pool, tenant_id, old, staff_sub="staff-1")
    assert done["owner_member_id"] == str(agent_id)
    async with tenant_tx(app_pool, agent, tenant_id, action=Action.TEAM_VIEW) as ctx:
        assert ctx.member_role == "owner"
    kinds = {(e["actor_kind"], e["action"]) for e in await _events(admin, tenant_id, "recovery_complete")}
    assert kinds == {("aa_ops", "recovery_complete")}


# ── Review round (S249): the invitation row lock, and accept never demotes an active member ──────────────────────────
class _SlowDirectory(MemoryDirectory):
    """Holds the claim open a moment, so a second claim of the same invitation has to wait for the row lock."""

    calls = 0

    async def create_or_find(self, email: str):  # noqa: ANN201
        type(self).calls += 1
        await asyncio.sleep(0.3)
        return await super().create_or_find(email)


async def test_two_claims_and_two_accepts_of_one_invitation_run_one_after_the_other(migrated_app_dsn: str) -> None:
    pool = await asyncpg.create_pool(migrated_app_dsn, min_size=0, max_size=4)
    try:
        keys = _keys()
        tenant_id, [(owner, _)] = await _agency(pool, "owner")
        _, token = await _invite(pool, keys, owner, tenant_id, "twice@agency.example", "agent")
        directory = _SlowDirectory(ISS)
        _SlowDirectory.calls = 0
        first, second = await asyncio.gather(
            invitations.claim(pool, keys, directory, token), invitations.claim(pool, keys, directory, token)
        )
        # One account; the waiting claim saw "claimed" after the first committed. The account has never been signed
        # in to, so that claim gave it a new temporary password (S263 join fix: a double submit must not leave the
        # person with a password they never saw); only the later one is valid.
        assert [first.state, second.state] == ["created", "created"]
        assert _SlowDirectory.calls == 1 and len(directory.accounts) == 1
        account = directory.accounts["twice@agency.example"]
        assert directory.resent == [account["sub"]]
        assert account["password"] in (first.temporary_password, second.temporary_password)
        person = _caller(directory.accounts["twice@agency.example"]["sub"])

        async def accept() -> str:
            try:
                await invitations.accept(pool, person, token)
                return "accepted"
            except invitations.Gone as exc:
                return exc.code

        assert sorted(await asyncio.gather(accept(), accept())) == ["accepted", "invitation_used"]
    finally:
        await pool.close()


async def test_accept_does_not_demote_an_active_owner_invited_as_agent(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant_id, [(owner, owner_id)] = await _agency(app_pool, "owner")
    directory = MemoryDirectory(ISS)
    directory.accounts["boss@agency.example"] = {"sub": owner.sub, "status": "CONFIRMED"}
    _, token = await _invite(app_pool, keys, owner, tenant_id, "boss@agency.example", "agent")
    assert (await invitations.claim(app_pool, keys, directory, token)).state == "existing"
    with pytest.raises(Conflict) as exc:
        await invitations.accept(app_pool, owner, token)
    assert exc.value.code == "already_member"
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.TEAM_VIEW) as ctx:
        assert (ctx.member_id, ctx.member_role) == (owner_id, "owner")
