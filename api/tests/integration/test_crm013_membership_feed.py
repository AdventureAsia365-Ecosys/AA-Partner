"""The CIS membership feed consumer on PostgreSQL 15 with the RDS role shape (AA-978, CRM-P1), through the real job
handler, ``membership_feed_tx`` and the crm-008 / crm-013 triggers; CIS is a fake transport serving contract pages.

- **R**: an event that carries a ``crm_role`` is rejected (inbox ``rejected`` / ``crm_role_present``) and changes
  nothing; a Content studio ``role_changed`` to owner never changes a CRM role (``ignored``).
- **E**: CIS removes the agency's last active owner -> that member is disabled and signed out, the agency is locked
  with ``lock_source = cis_membership`` (every member request 403 ``tenant_locked``) -> the AA-941 owner recovery
  completes -> the agency is active again with the new owner.
- Removing a non-owner refuses only that member. ``tenant.locked`` / ``tenant.unlocked`` round-trip; an AA lock is not
  lifted by ``tenant.unlocked``. A replayed page changes nothing (no second audit event). The cursor survives a new
  handler instance. A tenant the CRM has no agency for is ignored without an inbox row.
- Review round 1: an L1 lock on a CIS-locked agency takes the lock over (``aa_ops``), so a CIS unlock cannot lift it;
  the feed waits for the crm-008 owner lock, so a concurrent owner demotion cannot leave the agency without an owner;
  a withdrawn invitation (removed, null identity) is ignored, not rejected; an event the database refuses is recorded
  as rejected / ``apply_refused`` and the feed goes on.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.cis import membership_feed
from app.cis.http import HttpResponse
from app.cis.m2m import M2mCredentials, TokenProvider
from app.cis.membership_feed import FeedConfig, MembershipFeed
from app.errors import Conflict, Forbidden
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job
from app.members import recovery
from app.members.directory import MemoryDirectory
from app.ops import console
from app.permissions import Action
from app.tenancy import tenant_tx
from tests.integration.test_crm005_intake import ISS, _agency, _owner_fetch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

JOB = Job(id=str(uuid.uuid4()), kind="crm_membership_pull", payload={}, attempt=1, max_attempts=3)
BASE = "https://api-cis.example.invalid"


class Tokens(TokenProvider):
    def __init__(self) -> None:
        super().__init__(M2mCredentials("abc123", "x", "https://m2m-auth.example.invalid/oauth2/token"))

    async def token(self, scope: str) -> str:
        assert scope == "cis.tenant/read"
        return "tok"


class Cis:
    """The CIS feed: every event ever published, served after the cursor (cursor = the event's position)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.afters: list[str | None] = []

    def publish(self, *events: dict[str, Any]) -> None:
        for event in events:
            event["cursor"] = f"m_{len(self.events) + 1:06d}"
            self.events.append(event)

    async def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout_s: float
    ) -> HttpResponse:
        query = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
        after = query.get("after")
        self.afters.append(after)
        start = int(after[2:]) if after else 0
        items = self.events[start : start + int(query["limit"])]
        next_cursor = items[-1]["cursor"] if items else (after or "")
        return HttpResponse(200, json.dumps({"items": items, "next_cursor": next_cursor}).encode())


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def _event(event_type: str, tenant_id: uuid.UUID, membership: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "cursor": "",
        "type": event_type,
        "occurred_at": _now(),
        "tenant_id": str(tenant_id),
        "membership": membership,
    }


def _membership(tenant_id: uuid.UUID, sub: str | None, *, status: str = "removed", role: str = "owner") -> dict:
    return {
        "membership_id": str(uuid.uuid4()),
        "tenant_id": str(tenant_id),
        "identity": {"iss": ISS, "sub": sub} if sub else None,
        "content_studio_role": role,
        "status": status,
        "invitation_id": None,
        "updated_at": _now(),
    }


def _removed(tenant_id: uuid.UUID, sub: str) -> dict[str, Any]:
    return _event("membership.removed", tenant_id, _membership(tenant_id, sub))


@pytest_asyncio.fixture
async def cis(admin: asyncpg.Connection) -> AsyncIterator[Cis]:
    """A fresh CIS feed and a fresh consumer cursor (the state row is one per database)."""
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute(
            "UPDATE crm.membership_feed_state SET feed_cursor = NULL, failures = 0, last_error_class = NULL, "
            "last_error_at = NULL"
        )
    yield Cis()


def _job(cis: Cis, directory: MemoryDirectory | None = None) -> MembershipFeed:
    return MembershipFeed(FeedConfig(base_url=BASE, tokens=Tokens(), transport=cis), directory or MemoryDirectory(ISS))


async def _run(pool: asyncpg.Pool, cis: Cis, directory: MemoryDirectory | None = None) -> dict[str, Any]:
    result = await _job(cis, directory)(pool, JOB)
    check_payload(result, what="result")
    return result


async def _inbox(admin: asyncpg.Connection, tenant_id: uuid.UUID) -> list[tuple[str | None, str, str]]:
    rows = await _owner_fetch(
        admin, tenant_id, "SELECT event_type, outcome, reason_code FROM crm.membership_inbox ORDER BY created_at"
    )
    return [(r["event_type"], r["outcome"], r["reason_code"]) for r in rows]


async def _tenant(admin: asyncpg.Connection, tenant_id: uuid.UUID) -> tuple[str, str | None]:
    (row,) = await _owner_fetch(admin, tenant_id, "SELECT status, lock_source FROM crm.tenant")
    return row["status"], row["lock_source"]


async def _member_status(admin: asyncpg.Connection, tenant_id: uuid.UUID, member_id: uuid.UUID) -> tuple[str, str]:
    (row,) = await _owner_fetch(admin, tenant_id, "SELECT role, status FROM crm.member WHERE member_id = $1", member_id)
    return row["role"], row["status"]


async def _audit(admin: asyncpg.Connection, tenant_id: uuid.UUID) -> list[tuple[str, str, str | None]]:
    rows = await _owner_fetch(
        admin,
        tenant_id,
        "SELECT actor_kind, actor_ref, action, reason, object_ids FROM crm.audit_event "
        "WHERE actor_ref = 'platform:feed-tenant-membership' ORDER BY at, event_id",
    )
    for r in rows:
        assert r["actor_kind"] == "aa_ops" and any(i.startswith("cis_event:") for i in r["object_ids"])
    return [(r["action"], r["reason"], r["actor_ref"]) for r in rows]


async def _refused(pool: asyncpg.Pool, who: Any, tenant_id: uuid.UUID) -> str | None:
    try:
        async with tenant_tx(pool, who, tenant_id, action=Action.PIPELINE_VIEW):
            return None
    except Forbidden as exc:
        return exc.code


async def test_e_the_last_owner_removed_in_cis_locks_the_agency_until_the_owner_recovery(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, cis: Cis
) -> None:
    tenant_id, [(owner, owner_id), (agent, agent_id)] = await _agency(app_pool, "owner", "agent")
    directory = MemoryDirectory(ISS)
    cis.publish(_removed(tenant_id, owner.sub))
    result = await _run(app_pool, cis, directory)
    assert (result["status"], result["applied_count"], result["signed_out_count"]) == ("ok", 1, 1)
    assert directory.signed_out == [owner.sub]
    assert await _member_status(admin, tenant_id, owner_id) == ("owner", "disabled")
    assert await _tenant(admin, tenant_id) == ("locked", "cis_membership")
    assert await _inbox(admin, tenant_id) == [("membership.removed", "applied", "agency_locked_no_owner")]
    assert [a[:2] for a in await _audit(admin, tenant_id)] == [
        ("member_deactivate", "cis_membership_removed"),
        ("tenant_lock", "cis_last_owner_removed"),
    ]
    assert await _refused(app_pool, owner, tenant_id) == "tenant_locked"
    assert await _refused(app_pool, agent, tenant_id) == "tenant_locked"  # recovery only, for everyone
    # A CIS unlock does not lift a last-owner lock.
    cis.publish(_event("tenant.unlocked", tenant_id))
    await _run(app_pool, cis)
    assert await _tenant(admin, tenant_id) == ("locked", "cis_membership")
    # The AA-941 owner recovery is the way back: opened by AA (allowed on this lock), completed after 14 days.
    opened = await recovery.open_request(
        app_pool, tenant_id, candidate_member_id=agent_id, reason="owner_left", staff_sub="staff-1",
        directory=None, mailer=None, web_origin="https://partner.example.test",
    )  # fmt: skip
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', 'platform:staff-1', true)")
        await conn.execute(
            "UPDATE crm.owner_recovery_request SET status = 'cancelled', decided_at = now(), decided_by = 'x' "
            "WHERE request_id = $1",
            uuid.UUID(opened["request_id"]),
        )
        old = await conn.fetchval(
            "INSERT INTO crm.owner_recovery_request (candidate_member_id, reason, created_at, eligible_at) "
            "VALUES ($1, 'owner_left', now() - interval '15 days', now() - interval '1 day') RETURNING request_id",
            agent_id,
        )
    done = await recovery.complete_request(app_pool, tenant_id, old, staff_sub="staff-1", ticket_ref="SUP-11")
    assert done["owner_member_id"] == str(agent_id)
    assert await _tenant(admin, tenant_id) == ("active", None)
    async with tenant_tx(app_pool, agent, tenant_id, action=Action.TEAM_VIEW) as ctx:
        assert ctx.member_role == "owner"
    assert await _refused(app_pool, owner, tenant_id) == "not_a_member"


async def test_a_recovery_is_refused_on_any_other_lock(app_pool: asyncpg.Pool) -> None:
    tenant_id, [_, (_, agent_id)] = await _agency(app_pool, "owner", "agent")
    await console.lock_agency(app_pool, tenant_id, staff_sub="staff-1", reason_code="payment", ticket_ref="SUP-1")
    with pytest.raises(Conflict) as exc:
        await recovery.open_request(
            app_pool, tenant_id, candidate_member_id=agent_id, reason="owner_left", staff_sub="staff-1",
            directory=None, mailer=None, web_origin="https://partner.example.test",
        )  # fmt: skip
    assert exc.value.code == "tenant_locked"


async def test_removing_a_non_owner_refuses_only_that_member(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, cis: Cis
) -> None:
    tenant_id, [(owner, _), (agent, agent_id)] = await _agency(app_pool, "owner", "agent")
    cis.publish(_removed(tenant_id, agent.sub))
    assert (await _run(app_pool, cis))["applied_count"] == 1
    assert await _member_status(admin, tenant_id, agent_id) == ("agent", "disabled")
    assert await _refused(app_pool, agent, tenant_id) == "not_a_member"
    assert await _refused(app_pool, owner, tenant_id) is None
    assert await _tenant(admin, tenant_id) == ("active", None)
    assert await _inbox(admin, tenant_id) == [("membership.removed", "applied", "member_disabled")]


async def test_r_a_cis_event_never_grants_or_changes_a_crm_role(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, cis: Cis
) -> None:
    tenant_id, [_, (agent, agent_id)] = await _agency(app_pool, "owner", "agent")
    granting = _event("membership.activated", tenant_id, _membership(tenant_id, agent.sub, status="active"))
    granting["membership"]["crm_role"] = "owner"
    promoted = _event("membership.role_changed", tenant_id, _membership(tenant_id, agent.sub, status="active"))
    newcomer = _event("membership.activated", tenant_id, _membership(tenant_id, f"sub-{uuid.uuid4()}", status="active"))
    cis.publish(granting, promoted, newcomer)
    result = await _run(app_pool, cis)
    assert (result["rejected_count"], result["ignored_count"], result["applied_count"]) == (1, 2, 0)
    assert await _inbox(admin, tenant_id) == [
        ("membership.activated", "rejected", "crm_role_present"),
        ("membership.role_changed", "ignored", "informational"),
        ("membership.activated", "ignored", "informational"),
    ]
    assert await _member_status(admin, tenant_id, agent_id) == ("agent", "active")
    members = await _owner_fetch(admin, tenant_id, "SELECT count(*) AS n FROM crm.member")
    assert members[0]["n"] == 2  # no member created from CIS
    assert await _audit(admin, tenant_id) == []


async def test_tenant_lock_and_unlock_round_trip_and_an_aa_lock_stays(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, cis: Cis
) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool, "owner")
    cis.publish(_event("tenant.locked", tenant_id))
    await _run(app_pool, cis)
    assert await _tenant(admin, tenant_id) == ("locked", "cis_tenant")
    assert await _refused(app_pool, owner, tenant_id) == "tenant_locked"
    cis.publish(_event("tenant.unlocked", tenant_id))
    await _run(app_pool, cis)
    assert await _tenant(admin, tenant_id) == ("active", None)
    assert await _refused(app_pool, owner, tenant_id) is None
    # AA locks it (L1 console): CIS cannot lift that one, and tenant.deleted finds it locked already.
    await console.lock_agency(app_pool, tenant_id, staff_sub="staff-1", reason_code="abuse", ticket_ref="SUP-2")
    cis.publish(_event("tenant.unlocked", tenant_id), _event("tenant.deleted", tenant_id))
    await _run(app_pool, cis)
    assert await _tenant(admin, tenant_id) == ("locked", "aa_ops")
    assert [r[2] for r in await _inbox(admin, tenant_id)] == [
        "agency_locked",
        "agency_unlocked",
        "lock_not_from_cis_tenant",
        "already_locked",
    ]
    assert [a[:2] for a in await _audit(admin, tenant_id)] == [
        ("tenant_lock", "cis_tenant_locked"),
        ("tenant_unlock", "cis_tenant_unlocked"),
    ]


async def test_a_replayed_page_is_a_no_op_and_the_cursor_survives_a_restart(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, cis: Cis
) -> None:
    tenant_id, [_, (agent, _)] = await _agency(app_pool, "owner", "agent")
    cis.publish(_removed(tenant_id, agent.sub), _event("tenant.created", uuid.uuid4()))
    first = await _run(app_pool, cis)
    assert (first["applied_count"], first["ignored_count"]) == (1, 1)  # the second tenant has no CRM agency
    # The cursor is stored: a new handler instance (a restart) asks after it.
    await _run(app_pool, cis)
    assert cis.afters[-1] == "m_000002"
    # CIS serves the same page again (its cursor went back): every event is skipped.
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("UPDATE crm.membership_feed_state SET feed_cursor = NULL")
    again = await _run(app_pool, cis)
    # The unknown tenant's event has no inbox row, so it is ignored again (it changes nothing either way).
    assert (again["replayed_count"], again["applied_count"], again["ignored_count"]) == (1, 0, 1)
    assert len(await _inbox(admin, tenant_id)) == 1
    assert len(await _audit(admin, tenant_id)) == 1


async def test_an_unknown_agency_and_an_unrecordable_event(
    admin: asyncpg.Connection, app_pool: asyncpg.Pool, cis: Cis
) -> None:
    stranger = uuid.uuid4()
    cis.publish(_event("tenant.locked", stranger), {"type": "tenant.locked", "cursor": "x"})
    result = await _run(app_pool, cis)
    assert (result["ignored_count"], result["unrecorded_count"]) == (1, 1)
    assert await _inbox(admin, stranger) == []  # no row: no erase would ever reach it (review round 1)


# ── review round 1 ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cis_lock", ["tenant.locked", "last_owner"])
async def test_an_aa_lock_on_a_cis_locked_agency_outlives_a_cis_unlock(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, cis: Cis, cis_lock: str
) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool, "owner")
    cis.publish(_event("tenant.locked", tenant_id) if cis_lock == "tenant.locked" else _removed(tenant_id, owner.sub))
    await _run(app_pool, cis)
    assert (await _tenant(admin, tenant_id))[1] == ("cis_tenant" if cis_lock == "tenant.locked" else "cis_membership")
    # AA locks it too (fraud, say): AA takes the lock over, audited.
    await console.lock_agency(app_pool, tenant_id, staff_sub="staff-1", reason_code="abuse", ticket_ref="SUP-3")
    assert await _tenant(admin, tenant_id) == ("locked", "aa_ops")
    audit = await _owner_fetch(
        admin,
        tenant_id,
        "SELECT action, reason, fields FROM crm.audit_event WHERE actor_ref = 'platform:staff-1' ORDER BY at",
    )
    assert [(a["action"], a["reason"], list(a["fields"])) for a in audit] == [("tenant_lock", "abuse", ["lock_source"])]
    # A second AA lock is a no-op without an audit event, as before.
    await console.lock_agency(app_pool, tenant_id, staff_sub="staff-1", reason_code="abuse", ticket_ref="SUP-3")
    assert (
        len(await _owner_fetch(admin, tenant_id, "SELECT 1 FROM crm.audit_event WHERE actor_ref = 'platform:staff-1'"))
        == 1
    )
    cis.publish(_event("tenant.unlocked", tenant_id))
    await _run(app_pool, cis)
    assert await _tenant(admin, tenant_id) == ("locked", "aa_ops")
    assert (await _inbox(admin, tenant_id))[-1] == ("tenant.unlocked", "ignored", "lock_not_from_cis_tenant")


async def test_the_feed_waits_for_the_owner_lock_so_an_agency_never_ends_without_an_owner(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, cis: Cis
) -> None:
    """Owners A and B. A demotes B in a member request (the crm-008 guard takes the per-agency owner lock and sees A
    as the other owner) while CIS removes A. The feed must wait for that request: then it sees no owner left and
    locks the agency (card E). Without the lock both would pass their count and the agency would stay active with
    no owner."""
    tenant_id, [(owner_a, a_id), (_, b_id)] = await _agency(app_pool, "owner", "owner")
    async with app_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            await conn.execute("SELECT set_config('app.actor', $1, true)", f"member:{a_id}")
            await conn.execute("SELECT set_config('app.member_role', 'owner', true)")
            await conn.execute("UPDATE crm.member SET role = 'agent' WHERE member_id = $1", b_id)
            cis.publish(_removed(tenant_id, owner_a.sub))
            pull = asyncio.create_task(_run(app_pool, cis))
            await asyncio.sleep(0.5)
            assert not pull.done()  # blocked on the owner lock the member request holds
        except BaseException:
            await tx.rollback()
            raise
        await tx.commit()
    result = await asyncio.wait_for(pull, 10)
    assert result["applied_count"] == 1
    assert await _member_status(admin, tenant_id, a_id) == ("owner", "disabled")
    assert await _member_status(admin, tenant_id, b_id) == ("agent", "active")
    assert await _tenant(admin, tenant_id) == ("locked", "cis_membership")
    assert await _inbox(admin, tenant_id) == [("membership.removed", "applied", "agency_locked_no_owner")]


async def test_a_withdrawn_invitation_is_ignored_not_rejected(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, cis: Cis
) -> None:
    tenant_id, _ = await _agency(app_pool, "owner")
    cis.publish(_event("membership.removed", tenant_id, _membership(tenant_id, None, role="editor")))
    result = await _run(app_pool, cis)
    assert (result["rejected_count"], result["ignored_count"]) == (0, 1)
    assert await _inbox(admin, tenant_id) == [("membership.removed", "ignored", "no_identity")]


async def test_an_event_the_database_refuses_is_recorded_and_the_feed_goes_on(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, cis: Cis, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, [_, (agent, agent_id)] = await _agency(app_pool, "owner", "agent")
    refused = _event("tenant.locked", tenant_id)
    real = membership_feed._decide_and_apply

    async def decide(ctx: Any, event_id: uuid.UUID, event: dict[str, Any]) -> Any:
        if str(event_id) == refused["event_id"]:
            lock = "UPDATE crm.tenant SET status = 'locked', lock_source = 'cis_tenant' WHERE tenant_id = $1"
            await ctx.conn.execute(lock, ctx.tenant_id)
            raise asyncpg.exceptions.CheckViolationError("a rule this code does not know")
        return await real(ctx, event_id, event)

    monkeypatch.setattr(membership_feed, "_decide_and_apply", decide)
    cis.publish(refused, _removed(tenant_id, agent.sub))
    result = await _run(app_pool, cis)
    assert (result["status"], result["rejected_count"], result["applied_count"]) == ("ok", 1, 1)
    assert await _inbox(admin, tenant_id) == [
        ("tenant.locked", "rejected", "apply_refused"),
        ("membership.removed", "applied", "member_disabled"),
    ]
    assert await _tenant(admin, tenant_id) == ("active", None)  # the refused event's work was rolled back
    assert await _member_status(admin, tenant_id, agent_id) == ("agent", "disabled")
    await _run(app_pool, cis)
    assert cis.afters[-1] == "m_000002"  # the cursor moved past the refused event
