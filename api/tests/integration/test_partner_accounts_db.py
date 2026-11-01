"""S263 (AA-939 follow-up): the partner-account clean-up on PostgreSQL 15 with the RDS role shape, through the real
console, invitation claims, studio relay, contexts (``partner_account_tx`` on trashed agencies too, the crm-039 studio
read), the job row the agency delete queues and the job handler. The partner pool is a MemoryDirectory.

- A deleted synthetic test agency: the account its owner invitation's claim created is deleted by the queued job, and
  the deletion is in that agency's audit (invitation id and a reason code only).
- The cross-agency links keep an account: an active member in another agency, a live claimed invitation in another
  agency, a claimed Content studio invitation.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import asyncpg
import pytest

from app.jobs.queue import Job
from app.members import accounts, invitations, studio
from app.members.directory import FORCE_CHANGE_PASSWORD, MemoryDirectory
from app.members.mail import MemoryMailer
from app.ops import console
from tests.integration.test_crm005_intake import ISS, _keys, _owner_fetch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ORIGIN = "https://partner.example.test"
STAFF = "staff-l1"
LONG_AGO = dt.datetime.now(dt.UTC) - dt.timedelta(days=40)


class World:
    def __init__(self) -> None:
        self.keys = _keys()
        self.mailer = MemoryMailer()
        self.directory = MemoryDirectory(ISS)

    async def agency(self, pool: asyncpg.Pool, name: str) -> uuid.UUID:
        tenant_id = uuid.uuid4()
        await console.create_agency(
            pool, tenant_id=tenant_id, agency_name=name, is_synthetic=True, default_locale="en-US",
            environment="test", staff_sub=STAFF, ticket_ref="SUP-90",
        )  # fmt: skip
        return tenant_id

    async def claimed_owner_invite(self, pool: asyncpg.Pool, tenant_id: uuid.UUID, email: str) -> str:
        await console.invite_first_owner(
            pool, tenant_id, staff_sub=STAFF, owner_email=email, ticket_ref="SUP-91", keys=self.keys,
            mailer=self.mailer, web_origin=ORIGIN, ttl_hours=168,
        )  # fmt: skip
        token = self.mailer.sent[-1].text.split("/join?t=")[1].split()[0]
        await invitations.claim(pool, self.keys, self.directory, token)
        account = self.directory.accounts[email]
        account["modified"] = LONG_AGO  # the claim happened long before anything ends below
        return account["sub"]


async def _audit(admin: asyncpg.Connection, tenant_id: uuid.UUID) -> list[asyncpg.Record]:
    return await _owner_fetch(
        admin,
        tenant_id,
        "SELECT actor_kind, actor_ref, action, object_kind, object_id, reason, object_ids FROM crm.audit_event "
        "WHERE action = 'partner_account_delete' ORDER BY at, event_id",
    )


async def test_a_deleted_test_agencys_never_used_owner_account_is_deleted_and_audited(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    world = World()
    tenant_id = await world.agency(app_pool, "Cedar Test Agency")
    sub = await world.claimed_owner_invite(app_pool, tenant_id, "cedar.owner@cedar.example")
    assert world.directory.accounts["cedar.owner@cedar.example"]["status"] == FORCE_CHANGE_PASSWORD
    await console.delete_test_agency(
        app_pool, tenant_id, staff_sub=STAFF, agency_name="Cedar Test Agency", ticket_ref="SUP-92", keys=world.keys
    )
    async with app_pool.acquire() as conn:
        [row] = await conn.fetch(
            "SELECT id::text AS id, payload FROM crm.job WHERE kind = 'crm_partner_accounts' "
            "AND payload->>'tenant_id' = $1",
            str(tenant_id),
        )
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
    assert payload == {"purpose": "agency_delete", "tenant_id": str(tenant_id)}
    handler = accounts.build_handler(world.directory, dry_run=False)
    result = await handler(app_pool, Job(id=row["id"], kind="crm_partner_accounts", payload=payload, attempt=1,
                                         max_attempts=3))  # fmt: skip
    assert (result["purpose"], result["candidate_count"], result["deleted_count"]) == ("agency_delete", 1, 1)
    assert world.directory.deleted == [sub] and "cedar.owner@cedar.example" not in world.directory.accounts
    [inv] = await _owner_fetch(admin, tenant_id, "SELECT invitation_id FROM crm.member_invitation")
    [event] = await _audit(admin, tenant_id)
    assert (event["actor_kind"], event["actor_ref"], event["object_kind"], event["reason"]) == (
        "system",
        "job:crm_partner_accounts",
        "member_invitation",
        "never_signed_in",
    )
    assert event["object_id"] == str(inv["invitation_id"]) and event["object_ids"] == [str(inv["invitation_id"])]
    # A second run finds nothing left to do.
    again = await handler(app_pool, Job(id=row["id"], kind="crm_partner_accounts", payload=payload, attempt=2,
                                        max_attempts=3))  # fmt: skip
    assert again.get("deleted_count", 0) == 0 and len(await _audit(admin, tenant_id)) == 1
    async with app_pool.acquire() as conn:
        await conn.execute(
            "UPDATE crm.job SET status = 'cancelled', finished_at = now() WHERE id = $1::uuid", row["id"]
        )


async def test_links_in_other_agencies_and_the_studio_keep_the_account(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    world = World()
    # 1. an active member elsewhere (same partner-pool identity)
    withdrawn = await world.agency(app_pool, "Fern Test Agency")
    member_sub = await world.claimed_owner_invite(app_pool, withdrawn, "fern.owner@fern.example")
    await console.withdraw_owner_invitation(app_pool, withdrawn, staff_sub=STAFF, ticket_ref="SUP-93")
    other = await world.agency(app_pool, "Fern Other Agency")
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(other))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        member_id = await conn.fetchval(
            "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, 'agent', 'invited') "
            "RETURNING member_id",
            ISS,
            member_sub,
        )
        await conn.execute("UPDATE crm.member SET status = 'active' WHERE member_id = $1", member_id)
    result = await accounts.sweep(app_pool, world.directory, dry_run=False, tenant_id=withdrawn, grace_days=0)
    assert result.get("kept_member_count") == 1 and world.directory.deleted == []

    # 2. a live claimed invitation in another agency (the claim found the same never-used account)
    first = await world.agency(app_pool, "Moss Test Agency")
    live_sub = await world.claimed_owner_invite(app_pool, first, "moss.owner@moss.example")
    await console.withdraw_owner_invitation(app_pool, first, staff_sub=STAFF, ticket_ref="SUP-94")
    second = await world.agency(app_pool, "Moss Second Agency")
    assert await world.claimed_owner_invite(app_pool, second, "moss.owner@moss.example") == live_sub
    result = await accounts.sweep(app_pool, world.directory, dry_run=False, tenant_id=first, grace_days=0)
    assert result.get("kept_live_invitation_count") == 1 and world.directory.deleted == []

    # 3. a claimed Content studio invitation (read through crm-039)
    lone = await world.agency(app_pool, "Reed Test Agency")
    studio_sub = await world.claimed_owner_invite(app_pool, lone, "reed.editor@reed.example")
    await console.withdraw_owner_invitation(app_pool, lone, staff_sub=STAFF, ticket_ref="SUP-95")
    await studio.issue(
        app_pool, cis_tenant_id=uuid.uuid4(), cis_invitation_id=uuid.uuid4(), email="reed.editor@reed.example",
        locale="en", staff_sub=STAFF, mailer=world.mailer, web_origin=ORIGIN, ttl_hours=72,
    )  # fmt: skip
    token = world.mailer.sent[-1].text.split("/join?t=")[1].split()[0]
    await studio.claim(app_pool, world.directory, token)
    world.directory.accounts["reed.editor@reed.example"]["modified"] = LONG_AGO
    assert world.directory.accounts["reed.editor@reed.example"]["sub"] == studio_sub
    result = await accounts.sweep(app_pool, world.directory, dry_run=False, tenant_id=lone, grace_days=0)
    assert result.get("kept_studio_invitation_count") == 1 and world.directory.deleted == []

    # Without a link, the withdrawn agency's own account goes (grace 0 here; 7 days in the real runs), dry run first.
    gone = await world.agency(app_pool, "Sage Test Agency")
    sage_sub = await world.claimed_owner_invite(app_pool, gone, "sage.owner@sage.example")
    await console.withdraw_owner_invitation(app_pool, gone, staff_sub=STAFF, ticket_ref="SUP-96")
    dry = await accounts.sweep(app_pool, world.directory, dry_run=True, tenant_id=gone, grace_days=0)
    assert dry.get("would_delete_count") == 1 and world.directory.deleted == []
    week = await accounts.sweep(app_pool, world.directory, dry_run=False, tenant_id=gone)
    assert week.get("kept_grace_count") == 1 and world.directory.deleted == []
    real = await accounts.sweep(app_pool, world.directory, dry_run=False, tenant_id=gone, grace_days=0)
    assert real.get("deleted_count") == 1 and world.directory.deleted == [sage_sub]
    assert len(await _audit(admin, gone)) == 1
