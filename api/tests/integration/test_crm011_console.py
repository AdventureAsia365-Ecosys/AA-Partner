"""The L1 console on PostgreSQL 15 with the RDS role shape (AA-941, card CRM-13a), through the real console, contexts,
services and the crm-008 / crm-010 / crm-011 triggers.

- **E** (card): AA opens an owner recovery, a member cancels it inside the 14 days -> completing it answers 409
  ``recovery_closed`` and nothing changes (every member row as before; the audit shows the open and the cancel only).
- **R** (card): an owner invite into an agency with travellers -> 409 ``has_travellers`` and no invitation row (a
  trashed traveller counts; so does a stored inquiry that no job has processed yet).
- Lock -> a member request answers 403 ``tenant_locked`` -> unlock -> the member request works again.
- Create agency, summary, usage, entitlements, insights opt-out, AA data request + owner confirmation, retry, list:
  each lands in that agency's audit as ``aa_ops`` (reads as ``ops_read``, writes with ``ticket:<REF>``).
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import pytest

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.intake import public as intake
from app.lifecycle import offboarding
from app.members import invitations, recovery
from app.members.directory import MemoryDirectory
from app.members.mail import MemoryMailer
from app.ops import console
from app.permissions import Action
from app.tenancy import tenant_tx
from tests.integration.test_crm005_intake import (
    ISS,
    SETTINGS,
    _agency,
    _form,
    _keys,
    _owner_fetch,
    answers,
)
from tests.integration.test_crm008_members import _caller

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ORIGIN = "https://partner.example.test"
STAFF = "staff-l1"


async def _audit(admin: asyncpg.Connection, tenant_id: uuid.UUID) -> list[asyncpg.Record]:
    return await _owner_fetch(
        admin,
        tenant_id,
        "SELECT actor_kind, actor_ref, action, object_kind, object_id, reason, object_ids FROM crm.audit_event "
        "ORDER BY at, event_id",
    )


async def _cancel_queued(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> None:
    """Leave the shared queue as the other test files expect it: nothing of this agency still queued."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE crm.job SET status = 'cancelled', finished_at = now() "
            "WHERE status = 'queued' AND payload->>'tenant_id' = $1",
            str(tenant_id),
        )


async def _members(admin: asyncpg.Connection, tenant_id: uuid.UUID) -> list[dict]:
    rows = await _owner_fetch(
        admin, tenant_id, "SELECT member_id, role, status, updated_at FROM crm.member ORDER BY member_id"
    )
    return [dict(r) for r in rows]


async def test_e_a_member_cancels_the_recovery_inside_14_days_and_nothing_changes(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    tenant_id, [(owner, owner_id), (agent, agent_id)] = await _agency(app_pool, "owner", "agent")
    opened = await recovery.open_request(
        app_pool, tenant_id, candidate_member_id=agent_id, reason="owner_unreachable", staff_sub=STAFF,
        directory=None, mailer=None, web_origin=ORIGIN, ticket_ref="SUP-100",
    )  # fmt: skip
    before = await _members(admin, tenant_id)
    request_id = uuid.UUID(opened["request_id"])
    # The owner (still here) cancels it from the banner, inside the 14 days.
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.RECOVERY_CANCEL) as ctx:
        assert (await recovery.cancel(ctx, request_id))["status"] == "cancelled"
    with pytest.raises(Conflict) as exc:
        await recovery.complete_request(app_pool, tenant_id, request_id, staff_sub=STAFF, ticket_ref="SUP-100")
    assert exc.value.code == "recovery_closed"
    assert await _members(admin, tenant_id) == before
    assert [m["role"] for m in before if m["member_id"] == agent_id] == ["agent"]
    events = [
        (e["action"], e["actor_kind"]) for e in await _audit(admin, tenant_id) if e["object_kind"] != "permission"
    ]
    assert events == [("recovery_open", "aa_ops"), ("recovery_cancel", "user")]
    [open_event] = [e for e in await _audit(admin, tenant_id) if e["action"] == "recovery_open"]
    assert open_event["object_ids"] == ["ticket:SUP-100"] and open_event["actor_ref"] == f"platform:{STAFF}"


async def test_r_an_owner_invite_into_an_agency_with_travellers_is_refused(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys, mailer = _keys(), MemoryMailer()
    tenant_id = uuid.uuid4()
    await console.create_agency(
        app_pool, tenant_id=tenant_id, agency_name="Hanoi Trails", is_synthetic=True, default_locale="en-GB",
        environment="test", staff_sub=STAFF, ticket_ref="SUP-1",
    )  # fmt: skip
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        traveler = await conn.fetchval(
            "INSERT INTO crm.traveler (display_name_enc) VALUES ($1) RETURNING traveler_id", b"\x00\x01" + b"\x07" * 40
        )
        # Trashed: still a traveller of this agency for the first-owner rule.
        await conn.execute(
            "UPDATE crm.traveler SET deleted_at = now(), deleted_by = 'seed:test', "
            "purge_after = now() + interval '7 days' WHERE traveler_id = $1",
            traveler,
        )
    summary = await console.agency_summary(app_pool, tenant_id, staff_sub=STAFF)
    assert summary["holds_travellers"] is True and summary["has_owner"] is False
    with pytest.raises(Conflict) as exc:
        await console.invite_first_owner(
            app_pool, tenant_id, staff_sub=STAFF, owner_email="owner@hanoi.example", ticket_ref="SUP-2", keys=keys,
            mailer=mailer, web_origin=ORIGIN, ttl_hours=168,
        )  # fmt: skip
    assert exc.value.code == "has_travellers" and mailer.sent == []
    assert await _owner_fetch(admin, tenant_id, "SELECT 1 FROM crm.member_invitation") == []


async def test_r_a_stored_inquiry_counts_as_a_traveller(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys, mailer = _keys(), MemoryMailer()
    tenant_id, [(owner, owner_id)] = await _agency(app_pool, "owner")
    form = await _form(app_pool, tenant_id, owner, default_owner_member_id=owner_id)
    assert (await intake.submit(app_pool, keys, SETTINGS, form.public_key, answers(), origin=None)).stored
    await recovery.deactivate_member(app_pool, tenant_id, owner_id, staff_sub=STAFF, directory=None)  # owner left
    with pytest.raises(Conflict) as exc:
        await console.invite_first_owner(
            app_pool, tenant_id, staff_sub=STAFF, owner_email="new@agency.example", ticket_ref="SUP-3", keys=keys,
            mailer=mailer, web_origin=ORIGIN, ttl_hours=168,
        )  # fmt: skip
    assert exc.value.code == "has_travellers"
    assert await _owner_fetch(admin, tenant_id, "SELECT 1 FROM crm.traveler") == []  # no job processed it yet
    await _cancel_queued(app_pool, tenant_id)


async def test_first_owner_invite_on_an_empty_agency_then_owner_exists(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys, mailer = _keys(), MemoryMailer()
    tenant_id = uuid.uuid4()
    summary = await console.create_agency(
        app_pool, tenant_id=tenant_id, agency_name="Lữ hành Sen Vàng", is_synthetic=True, default_locale="vi-VN",
        environment="test", staff_sub=STAFF, ticket_ref="SUP-3",
    )  # fmt: skip
    assert summary["agency_name"] == "Lữ hành Sen Vàng" and summary["state"] == "active"
    out = await console.invite_first_owner(
        app_pool, tenant_id, staff_sub=STAFF, owner_email="Owner@SenVang.example", ticket_ref="SUP-4", keys=keys,
        mailer=mailer, web_origin=ORIGIN, ttl_hours=168,
    )  # fmt: skip
    assert out["status"] == "pending" and "senvang" not in json.dumps(out).lower()
    assert len(mailer.sent) == 1 and mailer.sent[0].to == "owner@senvang.example"
    [row] = await _owner_fetch(admin, tenant_id, "SELECT role, email_enc, invited_by FROM crm.member_invitation")
    assert row["role"] == "owner" and b"senvang" not in bytes(row["email_enc"]).lower()
    assert row["invited_by"] == f"platform:{STAFF}"
    with pytest.raises(Conflict) as exc:
        await console.invite_first_owner(
            app_pool, tenant_id, staff_sub=STAFF, owner_email="other@senvang.example", ticket_ref="SUP-5", keys=keys,
            mailer=mailer, web_origin=ORIGIN, ttl_hours=168,
        )  # fmt: skip
    assert exc.value.code == "owner_invite_pending"
    events = {e["action"]: e for e in await _audit(admin, tenant_id)}
    assert events["invite"]["actor_kind"] == "aa_ops" and events["invite"]["object_ids"] == ["ticket:SUP-4"]
    assert events["invite"]["reason"] == "role_owner" and events["agency_create"]["actor_kind"] == "aa_ops"
    owned, [(owner, _)] = await _agency(app_pool, "owner")
    with pytest.raises(Conflict) as exc:
        await console.invite_first_owner(
            app_pool, owned, staff_sub=STAFF, owner_email="x@y.example", ticket_ref="SUP-6", keys=keys,
            mailer=mailer, web_origin=ORIGIN, ttl_hours=168,
        )  # fmt: skip
    assert exc.value.code == "owner_exists"


async def test_create_on_an_existing_id_is_409_and_never_renames(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """AA-941 review probe: create, lock, then create again with the same id and another name / locale -> 409
    ``tenant_exists``; the agency keeps its name, locale and status, and its audit gains nothing."""
    tenant_id = uuid.uuid4()
    await console.create_agency(
        app_pool, tenant_id=tenant_id, agency_name="Alpha Travel", is_synthetic=True, default_locale="en-AU",
        environment="test", staff_sub=STAFF, ticket_ref="SUP-1",
    )  # fmt: skip
    await console.lock_agency(app_pool, tenant_id, staff_sub=STAFF, reason_code="test", ticket_ref="SUP-2")
    for name, locale in (("Totally Different Name", "vi-VN"), ("Alpha Travel", "en-AU")):
        with pytest.raises(Conflict) as exc:
            await console.create_agency(
                app_pool, tenant_id=tenant_id, agency_name=name, is_synthetic=True, default_locale=locale,
                environment="test", staff_sub=STAFF, ticket_ref="SUP-3",
            )  # fmt: skip
        assert exc.value.code == "tenant_exists"
    [row] = await _owner_fetch(admin, tenant_id, "SELECT display_name, default_locale, status FROM crm.tenant")
    assert tuple(row) == ("Alpha Travel", "en-AU", "locked")
    events = [(e["action"], e["object_ids"]) for e in await _audit(admin, tenant_id) if e["action"] != "ops_read"]
    assert events == [("agency_create", ["ticket:SUP-1"]), ("tenant_lock", ["ticket:SUP-2"])]


async def _aa_owner_invite(app_pool: asyncpg.Pool, tenant_id: uuid.UUID, email: str, ticket: str) -> tuple:  # noqa: ANN001
    """AA's first-owner invitation, claimed: (token, the invited person's caller)."""
    keys, mailer, directory = _keys(), MemoryMailer(), MemoryDirectory(ISS)
    await console.invite_first_owner(
        app_pool, tenant_id, staff_sub=STAFF, owner_email=email, ticket_ref=ticket, keys=keys, mailer=mailer,
        web_origin=ORIGIN, ttl_hours=168,
    )  # fmt: skip
    token = mailer.sent[0].text.split("/join?t=")[1].split()[0]
    await invitations.claim(app_pool, keys, directory, token)
    return token, _caller(directory.accounts[email]["sub"])


async def test_the_first_owner_rule_is_checked_again_at_accept(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """AA-941 review probe: owner + agent; AA disables the owner and invites a first owner (no travellers yet); the
    agent then adds a traveller -> accepting the owner invitation answers 409 has_travellers, no membership is
    written and the refusal is on record. On an agency still without owner and travellers the accept works."""
    tenant_id, [(_, owner_id), _agent] = await _agency(app_pool, "owner", "agent")
    await recovery.deactivate_member(app_pool, tenant_id, owner_id, staff_sub=STAFF, directory=None)
    token, person = await _aa_owner_invite(app_pool, tenant_id, "new.owner@agency.example", "SUP-21")
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        await conn.execute("INSERT INTO crm.traveler (display_name_enc) VALUES ($1)", b"\x00\x01" + b"\x07" * 40)
    before = await _members(admin, tenant_id)
    with pytest.raises(Conflict) as exc:
        await invitations.accept(app_pool, person, token)
    assert exc.value.code == "has_travellers"
    assert await _members(admin, tenant_id) == before
    [denied] = [e for e in await _audit(admin, tenant_id) if e["action"] == "invitation_accept"]
    assert denied["reason"] == "has_travellers"
    [inv] = await _owner_fetch(admin, tenant_id, "SELECT status FROM crm.member_invitation")
    assert inv["status"] == "claimed"
    # Still a first owner: accepted.
    empty, [(_, first_owner)] = await _agency(app_pool, "owner")
    await recovery.deactivate_member(app_pool, empty, first_owner, staff_sub=STAFF, directory=None)
    token, person = await _aa_owner_invite(app_pool, empty, "first.owner@agency.example", "SUP-22")
    assert (await invitations.accept(app_pool, person, token))["member_role"] == "owner"


async def test_s263_withdraw_a_claimed_owner_invitation_then_a_new_one_reissues_the_password(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """S263, found live on Dev: the first owner's invitation was claimed once, the password was never seen, and a new
    owner invitation was refused (409 owner_invite_pending) for the claimed one's 7 days. AA withdraws it (ticket); the
    old link is used; a new invitation to the address gives the never-signed-in account a new temporary password and
    the owner joins."""
    keys, mailer, directory = _keys(), MemoryMailer(), MemoryDirectory(ISS)
    tenant_id = uuid.uuid4()
    await console.create_agency(
        app_pool, tenant_id=tenant_id, agency_name="Hoa Sen Test Tours", is_synthetic=True, default_locale="en-US",
        environment="test", staff_sub=STAFF, ticket_ref="SUP-30",
    )  # fmt: skip
    invite = dict(staff_sub=STAFF, owner_email="mai.owner@lotus.example", keys=keys, mailer=mailer,
                  web_origin=ORIGIN, ttl_hours=168)  # fmt: skip
    await console.invite_first_owner(app_pool, tenant_id, ticket_ref="SUP-31", **invite)
    first_token = mailer.sent[0].text.split("/join?t=")[1].split()[0]
    first = await invitations.claim(app_pool, keys, directory, first_token)
    assert first.state == "created" and first.temporary_password
    with pytest.raises(Conflict) as exc:
        await console.invite_first_owner(app_pool, tenant_id, ticket_ref="SUP-32", **invite)
    assert exc.value.code == "owner_invite_pending"
    out = await console.withdraw_owner_invitation(app_pool, tenant_id, staff_sub=STAFF, ticket_ref="SUP-33")
    assert out["status"] == "revoked"
    [inv] = await _owner_fetch(admin, tenant_id, "SELECT invitation_id, status FROM crm.member_invitation")
    assert (str(inv["invitation_id"]), inv["status"]) == (out["invitation_id"], "revoked")
    with pytest.raises(invitations.Gone) as gone:
        await invitations.claim(app_pool, keys, directory, first_token)
    assert gone.value.code == "invitation_used"
    with pytest.raises(NotFound) as none:
        await console.withdraw_owner_invitation(app_pool, tenant_id, staff_sub=STAFF, ticket_ref="SUP-34")
    assert none.value.code == "owner_invite_not_found"
    # Invite first owner works again; the account never signed in -> a new temporary password, then the owner joins.
    await console.invite_first_owner(app_pool, tenant_id, ticket_ref="SUP-35", **invite)
    second_token = mailer.sent[-1].text.split("/join?t=")[1].split()[0]
    second = await invitations.claim(app_pool, keys, directory, second_token)
    account = directory.accounts["mai.owner@lotus.example"]
    assert second.state == "created" and second.temporary_password == account["password"] != first.temporary_password
    assert directory.resent == [account["sub"]] and len(directory.accounts) == 1
    assert (await invitations.accept(app_pool, _caller(account["sub"]), second_token))["member_role"] == "owner"
    withdrawn = [e for e in await _audit(admin, tenant_id) if e["action"] == "owner_invite_withdraw"]
    assert [(e["actor_kind"], e["object_id"], e["object_ids"]) for e in withdrawn] == [
        ("aa_ops", out["invitation_id"], ["ticket:SUP-33"])
    ]
    # An agency with an owner has no AA invitation to withdraw.
    with pytest.raises(NotFound):
        await console.withdraw_owner_invitation(app_pool, tenant_id, staff_sub=STAFF, ticket_ref="SUP-36")


async def test_s263_aa_deletes_a_synthetic_test_agency_and_nothing_else(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """S263: AA deletes a synthetic agency without members or travellers (typed name, ticket): its live invitations
    are revoked, its keys destroyed (the invited address is unreadable for good), the row trashed; no context opens it,
    the L1 list leaves it out, its invitation link is dead. A real agency, or one with a member, is refused."""
    keys, mailer, directory = _keys(), MemoryMailer(), MemoryDirectory(ISS)
    tenant_id = uuid.uuid4()
    await console.create_agency(
        app_pool, tenant_id=tenant_id, agency_name="Bamboo Test Agency", is_synthetic=True, default_locale="en-US",
        environment="test", staff_sub=STAFF, ticket_ref="SUP-40",
    )  # fmt: skip
    await console.invite_first_owner(
        app_pool, tenant_id, staff_sub=STAFF, owner_email="tester@bamboo.example", ticket_ref="SUP-41", keys=keys,
        mailer=mailer, web_origin=ORIGIN, ttl_hours=168,
    )  # fmt: skip
    token = mailer.sent[0].text.split("/join?t=")[1].split()[0]
    await invitations.claim(app_pool, keys, directory, token)
    with pytest.raises(Invalid):
        await console.delete_test_agency(
            app_pool, tenant_id, staff_sub=STAFF, agency_name="bamboo test agency", ticket_ref="SUP-42", keys=keys
        )
    out = await console.delete_test_agency(
        app_pool, tenant_id, staff_sub=STAFF, agency_name="Bamboo Test Agency", ticket_ref="SUP-43", keys=keys
    )
    assert out == {"tenant_id": str(tenant_id), "status": "deleted"}
    # S263 (AA-939 follow-up): the partner-account clean-up of this agency was queued with the delete.
    async with app_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) AS n FROM crm.job WHERE kind = 'crm_partner_accounts' AND payload->>'tenant_id' = $1",
                str(tenant_id),
            )
            == 1
        )
    await _cancel_queued(app_pool, tenant_id)
    [row] = await _owner_fetch(admin, tenant_id, "SELECT deleted_at, deleted_by, purge_after FROM crm.tenant")
    assert row["deleted_at"] is not None and row["deleted_by"] == f"platform:{STAFF}"
    assert row["purge_after"] > row["deleted_at"]
    [inv] = await _owner_fetch(admin, tenant_id, "SELECT status FROM crm.member_invitation")
    assert inv["status"] == "revoked"
    key_rows = await _owner_fetch(admin, tenant_id, "SELECT wrapped_dek, destroyed_at FROM crm.tenant_key")
    assert key_rows and all(k["wrapped_dek"] is None and k["destroyed_at"] is not None for k in key_rows)
    with pytest.raises(NotFound):
        await console.agency_summary(app_pool, tenant_id, staff_sub=STAFF)
    listed = await console.list_agencies(app_pool, staff_sub=STAFF, cursor=None, limit=200)
    assert str(tenant_id) not in [a["tenant_id"] for a in listed["items"]]
    with pytest.raises(NotFound):
        await invitations.claim(app_pool, keys, directory, token)
    events = [(e["action"], e["reason"], e["object_ids"]) for e in await _audit(admin, tenant_id)
              if e["action"] in ("invite_revoke", "key_destroy", "agency_delete")]  # fmt: skip
    assert events == [
        ("invite_revoke", "agency_delete", []),
        ("key_destroy", "crypto_shred", []),
        ("agency_delete", "synthetic_test_agency", ["ticket:SUP-43"]),
    ]
    # A real agency is refused (and the attempt is on record); so is a synthetic one with an active member.
    real = uuid.uuid4()
    await console.create_agency(
        app_pool, tenant_id=real, agency_name="Real Lotus Travel", is_synthetic=False, default_locale="en-US",
        environment="prod", staff_sub=STAFF, ticket_ref="SUP-44",
    )  # fmt: skip
    with pytest.raises(Forbidden) as exc:
        await console.delete_test_agency(
            app_pool, real, staff_sub=STAFF, agency_name="Real Lotus Travel", ticket_ref="SUP-45", keys=keys
        )
    assert exc.value.code == "not_synthetic"
    [denied] = [e for e in await _audit(admin, real) if e["action"] == "agency_delete"]
    assert denied["reason"] == "not_synthetic"
    staffed, _people = await _agency(app_pool, "owner")
    with pytest.raises(Conflict) as exc:
        await console.delete_test_agency(
            app_pool, staffed, staff_sub=STAFF, agency_name="Sen Vàng Travel", ticket_ref="SUP-46", keys=keys
        )
    assert exc.value.code == "has_members"
    [kept] = await _owner_fetch(admin, staffed, "SELECT deleted_at FROM crm.tenant")
    assert kept["deleted_at"] is None


async def test_lock_refuses_member_requests_until_the_unlock(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool, "owner")
    locked = await console.lock_agency(app_pool, tenant_id, staff_sub=STAFF, reason_code="payment", ticket_ref="SUP-7")
    assert locked["state"] == "locked"
    with pytest.raises(Forbidden) as exc:
        async with tenant_tx(app_pool, owner, tenant_id, action=Action.PIPELINE_VIEW):
            pass
    assert exc.value.code == "tenant_locked"
    # Locked: an owner invite or an AA data request waits (409), reads still work.
    with pytest.raises(Conflict) as conflict:
        await console.raise_data_request(
            app_pool, tenant_id, staff_sub=STAFF, kind="agency_export", ticket_ref="SUP-8", directory=None,
            mailer=None, web_origin=ORIGIN,
        )  # fmt: skip
    assert conflict.value.code == "tenant_locked"
    assert (await console.agency_usage(app_pool, tenant_id, staff_sub=STAFF))["seats_used"] == 1
    await console.lock_agency(app_pool, tenant_id, staff_sub=STAFF, reason_code="payment", ticket_ref="SUP-7")
    unlocked = await console.unlock_agency(app_pool, tenant_id, staff_sub=STAFF, ticket_ref="SUP-9")
    assert unlocked["state"] == "active"
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.PIPELINE_VIEW) as ctx:
        assert ctx.member_role == "owner"
    actions = [(e["action"], e["reason"], e["object_ids"]) for e in await _audit(admin, tenant_id)
               if e["action"] in ("tenant_lock", "tenant_unlock")]  # fmt: skip
    assert actions == [("tenant_lock", "payment", ["ticket:SUP-7"]), ("tenant_unlock", None, ["ticket:SUP-9"])]
    with pytest.raises(NotFound):
        await console.agency_summary(app_pool, uuid.uuid4(), staff_sub=STAFF)


async def test_entitlements_opt_out_and_reads_are_on_record(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool, "owner")
    assert await console.agency_entitlements(app_pool, tenant_id, staff_sub=STAFF) == {
        "plan": "free",
        "seats_allowed": 10,
        "features_allowed": [],
        "insights_opt_out": False,
    }
    allowed = await console.allow_features(
        app_pool, tenant_id, staff_sub=STAFF, features=["campaigns", "ai_assist"], ticket_ref="SUP-10"
    )
    assert allowed["features_allowed"] == ["ai_assist", "campaigns"]
    assert await console.set_insights_opt_out(
        app_pool, tenant_id, staff_sub=STAFF, opt_out=True, owner_request_ref="OWN-1"
    ) == {"opt_out": True}
    entitlements = await console.agency_entitlements(app_pool, tenant_id, staff_sub=STAFF)
    assert entitlements["insights_opt_out"] is True and entitlements["features_allowed"] == ["ai_assist", "campaigns"]
    # The owner never writes it (crm-008 platform-only trigger).
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with tenant_tx(app_pool, owner, tenant_id, action=Action.PIPELINE_VIEW) as ctx:
            await ctx.conn.execute("UPDATE crm.tenant_entitlement SET insights_opt_out = false")
    assert exc.value.sqlstate == "CRM04"
    usage = await console.agency_usage(app_pool, tenant_id, staff_sub=STAFF)
    assert usage["seats_allowed"] == 10 and usage["ai_cost_usd_month"] == "0.0000"
    assert usage["ai_budget_usd_month"] == "2.0000" and usage["inbound_mail_30d"] == 0
    # Keyset cursor just below this agency: the list is in tenant id order and the shared test DB holds more agencies
    # than one page, so a first page of 200 need not contain a random new id (S251: red on main).
    page = await console.list_agencies(app_pool, staff_sub=STAFF, cursor=uuid.UUID(int=tenant_id.int - 1), limit=1)
    assert [i["tenant_id"] for i in page["items"]] == [str(tenant_id)]
    reads = [e["reason"] for e in await _audit(admin, tenant_id) if e["action"] == "ops_read"]
    assert reads == ["entitlements", "entitlements", "usage", "list"]
    writes = [(e["action"], e["reason"]) for e in await _audit(admin, tenant_id) if e["actor_kind"] == "aa_ops"
              and e["action"] != "ops_read"]  # fmt: skip
    assert writes == [("entitlements_allow", None), ("insights_opt_out", "opt_out")]


async def test_aa_raises_a_deletion_the_owner_confirms_or_declines(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    tenant_id, [(owner, _), (agent, _)] = await _agency(app_pool, "owner", "agent")
    directory, mailer = MemoryDirectory(ISS), MemoryMailer()
    raised = await console.raise_data_request(
        app_pool, tenant_id, staff_sub=STAFF, kind="agency_deletion", ticket_ref="SUP-11", directory=directory,
        mailer=mailer, web_origin=ORIGIN,
    )  # fmt: skip
    assert raised["status"] == "awaiting_owner_confirmation" and raised["kind"] == "agency_deletion"
    assert await _owner_fetch(admin, tenant_id, "SELECT 1 FROM crm.job WHERE payload->>'tenant_id' = $1",
                              str(tenant_id)) == []  # fmt: skip
    with pytest.raises(Conflict) as exc:
        await console.raise_data_request(
            app_pool, tenant_id, staff_sub=STAFF, kind="agency_export", ticket_ref="SUP-12", directory=None,
            mailer=None, web_origin=ORIGIN,
        )  # fmt: skip
    assert exc.value.code == "offboarding_open"
    request_id = uuid.UUID(raised["request_id"])
    # The owner sees it, cannot start their own request meanwhile, and cannot erase before confirming.
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
        seen = await offboarding.latest(ctx)
    assert seen["raised_by"] == "aa" and seen["awaiting_owner"] is True and seen["purpose"] == "offboard"  # type: ignore[index]
    with pytest.raises(Conflict) as exc:
        async with tenant_tx(app_pool, owner, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
            await offboarding.request(ctx, export_waived=False)
    assert exc.value.code == "data_request_awaiting"
    # The owner confirms: counts taken, the export job queued.
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
        accepted = await offboarding.accept(ctx, request_id, export_waived=False)
    assert accepted["awaiting_owner"] is False and accepted["dry_run_counts"]
    jobs = await _owner_fetch(admin, tenant_id, "SELECT payload FROM crm.job WHERE payload->>'request_id' = $1",
                              str(request_id))  # fmt: skip
    assert [json.loads(j["payload"])["mode"] for j in jobs] == ["export"]
    [listed] = (await console.agency_data_requests(app_pool, tenant_id, staff_sub=STAFF))["items"]
    assert listed["status"] == "confirmed"
    # The owner cancels; AA raises an export; the owner declines it.
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
        await offboarding.cancel(ctx, request_id, None)
    export = await console.raise_data_request(
        app_pool, tenant_id, staff_sub=STAFF, kind="agency_export", ticket_ref="SUP-13", directory=None,
        mailer=None, web_origin=ORIGIN,
    )  # fmt: skip
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
        with pytest.raises(Conflict) as exc:
            await offboarding.confirm_erase(ctx, uuid.UUID(export["request_id"]), confirm_tenant_id=tenant_id,
                                            dry_run=False)  # fmt: skip
    assert exc.value.code == "export_only_request"
    async with tenant_tx(app_pool, owner, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
        await offboarding.cancel(ctx, uuid.UUID(export["request_id"]), None)
    states = [(i["kind"], i["status"]) for i in (await console.agency_data_requests(
        app_pool, tenant_id, staff_sub=STAFF))["items"]]  # fmt: skip
    assert states == [("agency_export", "rejected"), ("agency_deletion", "cancelled")]
    raises = [(e["reason"], e["object_ids"]) for e in await _audit(admin, tenant_id)
              if e["action"] == "data_request_raise"]  # fmt: skip
    assert raises == [("agency_deletion", ["ticket:SUP-11"]), ("agency_export", ["ticket:SUP-13"])]
    await _cancel_queued(app_pool, tenant_id)


async def test_retry_only_this_agency_s_failed_job(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    tenant_id, _people = await _agency(app_pool, "owner")
    other, _ = await _agency(app_pool, "owner")
    async with app_pool.acquire() as conn:
        mine = await conn.fetchval(
            "INSERT INTO crm.job (kind, payload, status, attempt, finished_at, error) "
            "VALUES ('crm_dispatch', $1::jsonb, 'failed', 5, now(), 'PermanentError') RETURNING id",
            json.dumps({"tenant_id": str(tenant_id), "dispatch_id": str(uuid.uuid4())}),
        )
        theirs = await conn.fetchval(
            "INSERT INTO crm.job (kind, payload, status, attempt, finished_at, error) "
            "VALUES ('crm_dispatch', $1::jsonb, 'failed', 5, now(), 'PermanentError') RETURNING id",
            json.dumps({"tenant_id": str(other), "dispatch_id": str(uuid.uuid4())}),
        )
    listed = (await console.agency_failed_jobs(app_pool, tenant_id, staff_sub=STAFF))["items"]
    assert [j["job_id"] for j in listed] == [str(mine)] and listed[0]["error_class"] == "PermanentError"
    usage = await console.agency_usage(app_pool, tenant_id, staff_sub=STAFF)
    assert usage["failed_jobs_24h"] == 1
    with pytest.raises(NotFound) as exc:
        await console.retry_agency_job(app_pool, tenant_id, theirs, staff_sub=STAFF, ticket_ref="SUP-14")
    assert exc.value.code == "job_not_found"
    with pytest.raises(NotFound):
        await console.retry_agency_job(app_pool, tenant_id, uuid.uuid4(), staff_sub=STAFF, ticket_ref="SUP-14")
    assert await console.retry_agency_job(app_pool, tenant_id, mine, staff_sub=STAFF, ticket_ref="SUP-15") == {
        "job_id": str(mine),
        "status": "queued",
    }
    async with app_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, attempt, error, finished_at FROM crm.job WHERE id = $1", mine)
    await _cancel_queued(app_pool, tenant_id)
    assert dict(row) == {"status": "queued", "attempt": 0, "error": None, "finished_at": None}
    [event] = [e for e in await _audit(admin, tenant_id) if e["action"] == "job_retry"]
    assert event["object_id"] == str(mine) and event["object_ids"] == ["ticket:SUP-15"]
