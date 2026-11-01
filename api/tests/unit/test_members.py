"""F4 team and roles (AA-939): the Python branches of app.members (team, invitations, recovery, directory, mail) and
the new tenancy paths (the matrix refusal in tenant_tx, invitation_tx). The SQL, RLS and the crm-008 triggers run on
PostgreSQL in tests/integration/test_crm008_*.py."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import asyncpg
import pytest

from app import tenancy
from app.auth.route_guard import Caller
from app.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from app.members import invitations, recovery, team
from app.members.directory import (
    CognitoDirectory,
    MemoryDirectory,
    _quoted,
    require_directory,
    temporary_password,
)
from app.members.mail import (
    MemoryMailer,
    SesMailer,
    invitation_message,
    recovery_message,
    require_mailer,
)
from app.permissions import Action
from app.tenancy import Invitation, SystemContext, invitation_tx, tenant_tx
from tests.tokens import PARTNER_ISSUER, SUB
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, OTHER_MEMBER, TENANT, FakeVault, enc, fake_tx, tenant_ctx

NOW = dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)
INV = uuid.UUID("00000000-0000-4000-8000-00000000aaaa")
REQ = uuid.UUID("00000000-0000-4000-8000-00000000bbbb")
CALLER = Caller("agency", "partner", "partner-web", PARTNER_ISSUER, SUB, ())


@contextmanager
def raises(cls: type[BaseException], code: str) -> Iterator[None]:
    """pytest.raises that checks the service error's code (the message is the human text)."""
    with pytest.raises(cls) as exc:
        yield
    assert exc.value.code == code  # type: ignore[attr-defined]


class PgError(asyncpg.PostgresError):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


# ── team ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_plan_defaults_and_the_entitlement_row() -> None:
    conn = ScriptedConn().on("SELECT (SELECT count(*) FROM crm.member", 3)
    assert await team.plan(tenant_ctx(conn)) == {
        "plan": "free",
        "member_limit": 10,
        "allowed_features": [],
        "seats_used": 3,
    }
    conn.on("FROM crm.tenant_entitlement", {"plan": "core", "member_limit": 25, "allowed_features": ["ai_assist"]})
    assert (await team.plan(tenant_ctx(conn)))["member_limit"] == 25
    with pytest.raises(Forbidden):
        await team.plan(tenant_ctx(conn, role="agent"))


@pytest.mark.asyncio
async def test_team_lists_members_with_their_invited_address_and_masked_invitations() -> None:
    conn = ScriptedConn()
    conn.on(
        "FROM crm.member m",
        [
            {
                "member_id": MEMBER,
                "role": "owner",
                "status": "active",
                "created_at": NOW,
                "cognito_iss": PARTNER_ISSUER,
                "invitation_id": None,
                "email_enc": None,
                "open_trips": 2,
            },
            {
                "member_id": OTHER_MEMBER,
                "role": "agent",
                "status": "active",
                "created_at": NOW,
                "cognito_iss": PARTNER_ISSUER,
                "invitation_id": INV,
                "email_enc": enc("member_invitation", "email_enc", "lan@agency.example"),
                "open_trips": 0,
            },
        ],
    )
    conn.on(
        "ORDER BY created_at DESC",
        [
            {
                "invitation_id": INV,
                "role": "assistant",
                "status": "pending",
                "expires_at": NOW,
                "created_at": NOW,
                "email_enc": enc("member_invitation", "email_enc", "minh@agency.example"),
            }
        ],
    )
    ctx = tenant_ctx(conn)
    out = await team.team(ctx, FakeVault())  # type: ignore[arg-type]
    assert [m["email"] for m in out["members"]] == [None, "lan@agency.example"]
    assert out["members"][0]["is_me"] and out["members"][0]["open_trips"] == 2
    assert out["invitations"][0]["email_masked"] == "m***@agency.example"
    assert "email" not in out["invitations"][0]
    assert conn.sql("SET status = 'expired'") == [()]
    [event] = ctx.audit_events
    assert event.action == "list_view" and set(event.object_ids) == {str(MEMBER), str(OTHER_MEMBER), str(INV)}


def _invite_conn(**over: Any) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("SELECT (SELECT count(*) FROM crm.member", over.get("seats", 1))
    conn.on("JOIN crm.member m ON m.member_id = i.member_id", over.get("already", None))
    conn.on("INSERT INTO crm.member_invitation", over.get("insert", NOW + dt.timedelta(days=7)))
    conn.on("SELECT display_name FROM crm.tenant", "Sen Vàng Travel")
    return conn


async def _invite(
    conn: ScriptedConn,
    mailer: MemoryMailer,
    *,
    role: str = "agent",
    email: str = " Lan@Agency.Example ",
    ctx_role: str = "owner",
) -> dict[str, Any]:
    return await team.invite(
        tenant_ctx(conn, role=ctx_role),
        FakeVault(),
        mailer,  # type: ignore[arg-type]
        email=email,
        role=role,
        web_origin="https://partner.example.test",
        ttl_hours=168,
    )


@pytest.mark.asyncio
async def test_invite_stores_a_token_hash_and_mails_the_link() -> None:
    conn, mailer = _invite_conn(), MemoryMailer()
    out = await _invite(conn, mailer)
    assert out["status"] == "pending" and out["email_masked"] == "l***@agency.example" and out["role"] == "agent"
    [args] = conn.sql("INSERT INTO crm.member_invitation")
    invitation_id, role, email_enc, bidx, token_hash, ttl = args
    assert (role, bidx, ttl) == ("agent", b"e:lan@agency.example", 168)
    assert email_enc == enc("member_invitation", "email_enc", "lan@agency.example")
    [message] = mailer.sent
    assert message.to == "lan@agency.example" and "Sen Vàng Travel" in message.text and message.kind == "invitation"
    token = message.text.split("/join?t=")[1].split()[0]
    assert hashlib.sha256(token.encode()).digest() == token_hash and len(token) == 43
    assert token not in str(out)


@pytest.mark.asyncio
async def test_invite_takes_the_agency_seat_lock_before_counting() -> None:
    conn = _invite_conn()
    await _invite(conn, MemoryMailer())
    [lock] = [i for i, (sql, _) in enumerate(conn.calls) if "pg_advisory_xact_lock" in sql]
    [seats] = [i for i, (sql, _) in enumerate(conn.calls) if "count(*) FROM crm.member " in sql]
    assert lock < seats and conn.calls[lock][1] == (str(TENANT),)


@pytest.mark.parametrize(
    ("kwargs", "conn_over", "error", "code"),
    [
        ({"role": "admin"}, {}, Invalid, "invalid_role"),
        ({"email": "not-an-email"}, {}, Invalid, "invalid_email"),
        ({}, {"seats": 10}, Conflict, "member_limit_reached"),
        ({}, {"already": 1}, Conflict, "already_member"),
        ({}, {"insert": asyncpg.UniqueViolationError("dup")}, Conflict, "invitation_exists"),
        ({"ctx_role": "agent"}, {}, Forbidden, "role_forbidden"),
        ({"ctx_role": "assistant"}, {}, Forbidden, "role_forbidden"),
    ],
)
@pytest.mark.asyncio
async def test_invite_refusals(kwargs: dict[str, Any], conn_over: dict[str, Any], error: type, code: str) -> None:
    mailer = MemoryMailer()
    with pytest.raises(error) as exc:
        await _invite(_invite_conn(**conn_over), mailer, **kwargs)
    assert exc.value.code == code and mailer.sent == []


@pytest.mark.asyncio
async def test_revoke_invitation() -> None:
    conn = ScriptedConn().on("SET status = 'revoked'", INV)
    assert await team.revoke_invitation(tenant_ctx(conn), INV) == {"invitation_id": str(INV), "status": "revoked"}
    with pytest.raises(NotFound):
        await team.revoke_invitation(tenant_ctx(ScriptedConn()), INV)


def _member(role: str = "agent", status: str = "active") -> dict[str, Any]:
    return {
        "member_id": OTHER_MEMBER,
        "role": role,
        "status": status,
        "cognito_iss": PARTNER_ISSUER,
        "cognito_sub": "s-1",
    }


@pytest.mark.asyncio
async def test_change_role_paths() -> None:
    conn = ScriptedConn().on("FOR UPDATE", _member())
    ctx = tenant_ctx(conn)
    assert (await team.change_role(ctx, OTHER_MEMBER, "assistant"))["role"] == "assistant"
    assert conn.sql("UPDATE crm.member SET role") == [(OTHER_MEMBER, "assistant")]
    assert ctx.audit_events[-1].reason == "agent_to_assistant"
    same = ScriptedConn().on("FOR UPDATE", _member())
    await team.change_role(tenant_ctx(same), OTHER_MEMBER, "agent")
    assert same.sql("UPDATE crm.member SET role") == []
    with pytest.raises(Invalid):
        await team.change_role(tenant_ctx(conn), OTHER_MEMBER, "boss")
    with pytest.raises(NotFound):
        await team.change_role(tenant_ctx(ScriptedConn()), OTHER_MEMBER, "agent")
    with raises(Conflict, "member_disabled"):
        await team.change_role(
            tenant_ctx(ScriptedConn().on("FOR UPDATE", _member(status="disabled"))), OTHER_MEMBER, "owner"
        )
    for state, code in (("CRM01", "last_owner"), ("CRM02", "role_write_forbidden")):
        failing = ScriptedConn().on("FOR UPDATE", _member("owner")).on("UPDATE crm.member SET role", PgError(state))
        with pytest.raises(Conflict) as exc:
            await team.change_role(tenant_ctx(failing), OTHER_MEMBER, "agent")
        assert exc.value.code == code
    other = ScriptedConn().on("FOR UPDATE", _member("owner")).on("UPDATE crm.member SET role", PgError("23514"))
    with pytest.raises(asyncpg.PostgresError):
        await team.change_role(tenant_ctx(other), OTHER_MEMBER, "agent")


@pytest.mark.asyncio
async def test_remove_member_disables_signs_out_and_counts_trips_to_reassign() -> None:
    directory = MemoryDirectory(PARTNER_ISSUER)
    conn = ScriptedConn().on("FOR UPDATE", _member()).on("SELECT count(*) FROM crm.trip", 3)
    out = await team.remove_member(tenant_ctx(conn), directory, OTHER_MEMBER)
    assert out == {"member_id": str(OTHER_MEMBER), "status": "disabled", "signed_out": True, "to_reassign": 3}
    assert directory.signed_out == ["s-1"] and conn.sql("SET status = 'disabled'") == [(OTHER_MEMBER,)]
    # Already disabled, another issuer (EXTERNAL bridge), no directory: no update, no sign-out.
    gone = ScriptedConn().on("FOR UPDATE", {**_member(status="disabled"), "cognito_iss": "https://other/x"})
    assert (await team.remove_member(tenant_ctx(gone), directory, OTHER_MEMBER))["signed_out"] is False
    assert gone.sql("SET status = 'disabled'") == []
    assert (await team.remove_member(tenant_ctx(ScriptedConn().on("FOR UPDATE", _member())), None, OTHER_MEMBER))[
        "signed_out"
    ] is False
    with pytest.raises(NotFound):
        await team.remove_member(tenant_ctx(ScriptedConn()), directory, OTHER_MEMBER)
    last = ScriptedConn().on("FOR UPDATE", _member("owner")).on("SET status = 'disabled'", PgError("CRM01"))
    with raises(Conflict, "last_owner"):
        await team.remove_member(tenant_ctx(last), directory, OTHER_MEMBER)

    class Broken(MemoryDirectory):
        async def global_sign_out(self, sub: str) -> None:
            raise RuntimeError("cognito down")

    broken = ScriptedConn().on("FOR UPDATE", _member())
    assert (await team.remove_member(tenant_ctx(broken), Broken(PARTNER_ISSUER), OTHER_MEMBER))["signed_out"] is False


@pytest.mark.asyncio
async def test_reassign_queue_and_reassign() -> None:
    trip = uuid.uuid4()
    conn = ScriptedConn()
    conn.on(
        "JOIN crm.member m ON m.member_id = t.owner_member_id",
        [{"trip_id": trip, "title": "Laos", "stage": "qualifying", "owner_member_id": OTHER_MEMBER}],
    )
    conn.on("FROM crm.traveler tr", [{"owner_member_id": OTHER_MEMBER, "clients": 4}])
    ctx = tenant_ctx(conn)
    queue = await team.reassign_queue(ctx)
    assert queue["trips"][0]["from_member_id"] == str(OTHER_MEMBER)
    assert queue["clients_by_member"] == {str(OTHER_MEMBER): 4}
    assert ctx.audit_events[-1].object_ids == (str(trip),)
    with pytest.raises(Forbidden):
        await team.reassign_queue(tenant_ctx(conn, role="agent"))

    move = ScriptedConn().on("SELECT 1 FROM crm.member WHERE member_id = $1 AND status = 'active'", 1)
    move.on("UPDATE crm.trip SET owner_member_id", [{"trip_id": trip}])
    move.on("UPDATE crm.traveler SET owner_member_id", 4)

    async def execute(sql: str, *args: Any) -> str:
        move._answer(sql, args)
        return "UPDATE 4" if "crm.traveler" in sql else "UPDATE 1"

    move.execute = execute  # type: ignore[method-assign]
    out = await team.reassign(tenant_ctx(move), OTHER_MEMBER, MEMBER, None)
    assert out == {"from_member_id": str(OTHER_MEMBER), "to_member_id": str(MEMBER), "trips": 1, "clients": 4}
    assert move.sql("UPDATE crm.task SET assignee_member_id") == [(OTHER_MEMBER, MEMBER, [trip])]
    out = await team.reassign(tenant_ctx(move), OTHER_MEMBER, MEMBER, [trip])
    assert out["clients"] == 0
    with raises(Invalid, "same_member"):
        await team.reassign(tenant_ctx(move), MEMBER, MEMBER, None)
    with raises(Invalid, "invalid_member"):
        await team.reassign(tenant_ctx(ScriptedConn()), OTHER_MEMBER, MEMBER, None)


def test_email_helpers() -> None:
    assert team.normalise_email("  A.B@Example.COM ") == "a.b@example.com"
    for bad in ("", "a@b", "a b@example.com", "x" * 250 + "@example.com"):
        with pytest.raises(Invalid):
            team.normalise_email(bad)
    assert team.mask_email("lan@agency.example") == "l***@agency.example" and team.mask_email("x") == "***"
    token, digest = team.new_token()
    assert len(token) == 43 and hashlib.sha256(token.encode()).digest() == digest


# ── invitations ──────────────────────────────────────────────────────────────────────────────────
def _invitation(
    status: str = "pending", *, expired: bool = False, sub: str | None = None, role: str = "agent"
) -> Invitation:
    return Invitation(
        invitation_id=INV,
        tenant_id=TENANT,
        role=role,
        status=status,
        expired=expired,
        cognito_iss=PARTNER_ISSUER if sub else None,
        cognito_sub=sub,
        member_id=None,
        email_enc=enc("member_invitation", "email_enc", "lan@agency.example"),
    )


def _system(conn: ScriptedConn) -> SystemContext:
    return SystemContext(conn=conn, tenant_id=TENANT, actor=f"invitation:{INV}")


TOKEN = "t" * 43


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(invitations, "Vault", lambda keys, ctx: FakeVault())


@pytest.mark.asyncio
async def test_token_hash_refuses_malformed_tokens() -> None:
    for bad in ("", "short", "t" * 42 + "!", "t" * 44):
        with pytest.raises(NotFound):
            invitations.token_hash_hex(bad)
    assert invitations.token_hash_hex(TOKEN) == hashlib.sha256(TOKEN.encode()).hexdigest()


@pytest.mark.asyncio
async def test_claim_creates_an_account_once(monkeypatch: pytest.MonkeyPatch, vault: None) -> None:
    conn = ScriptedConn()
    ctx = _system(conn)
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(ctx, _invitation()))
    directory = MemoryDirectory(PARTNER_ISSUER)
    claimed = await invitations.claim(conn, object(), directory, TOKEN)  # type: ignore[arg-type]
    assert claimed.state == "created" and claimed.email == "lan@agency.example" and claimed.temporary_password
    assert "temporary_password" in claimed.body()
    sub = directory.accounts["lan@agency.example"]["sub"]
    assert conn.sql("SET status = 'claimed'") == [(INV, PARTNER_ISSUER, sub)]
    assert ctx.audit_events[-1].reason == "account_created"


@pytest.mark.asyncio
async def test_claim_finds_an_existing_account_and_shows_no_password(
    monkeypatch: pytest.MonkeyPatch, vault: None
) -> None:
    conn = ScriptedConn()
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(_system(conn), _invitation()))
    directory = MemoryDirectory(PARTNER_ISSUER)
    directory.add("lan@agency.example")
    claimed = await invitations.claim(conn, object(), directory, TOKEN)  # type: ignore[arg-type]
    assert claimed.state == "existing" and claimed.temporary_password is None
    assert "temporary_password" not in claimed.body()


@pytest.mark.asyncio
async def test_claim_again_and_closed_invitations(monkeypatch: pytest.MonkeyPatch, vault: None) -> None:
    directory = MemoryDirectory(PARTNER_ISSUER)
    conn = ScriptedConn()
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(_system(conn), _invitation("claimed", sub="s-1")))
    again = await invitations.claim(conn, object(), directory, TOKEN)  # type: ignore[arg-type]
    assert again.state == "already_claimed" and directory.accounts == {}
    for status, code in (
        ("revoked", "invitation_used"),
        ("accepted", "invitation_used"),
        ("expired", "invitation_expired"),
    ):
        monkeypatch.setattr(invitations, "invitation_tx", fake_tx(_system(conn), _invitation(status)))
        with pytest.raises(invitations.Gone) as exc:
            await invitations.claim(conn, object(), directory, TOKEN)  # type: ignore[arg-type]
        assert exc.value.code == code and exc.value.status_code == 410
    late = ScriptedConn()
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(_system(late), _invitation(expired=True)))
    with raises(invitations.Gone, "invitation_expired"):
        await invitations.claim(late, object(), directory, TOKEN)  # type: ignore[arg-type]
    assert late.sql("SET status = 'expired'") == [(INV,)]


@pytest.mark.asyncio
async def test_a_new_invitation_to_an_account_never_signed_in_gives_a_new_temporary_password(
    monkeypatch: pytest.MonkeyPatch, vault: None
) -> None:
    """S263 (live, Dev): the first claim's password was never seen and the account stayed in FORCE_CHANGE_PASSWORD.
    A new invitation to that address used to answer 409 account_setup_pending; now it re-issues a password."""
    conn = ScriptedConn()
    ctx = _system(conn)
    pending_setup = MemoryDirectory(PARTNER_ISSUER)
    sub = pending_setup.add("lan@agency.example", status="FORCE_CHANGE_PASSWORD")
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(ctx, _invitation()))
    claimed = await invitations.claim(conn, object(), pending_setup, TOKEN)  # type: ignore[arg-type]
    assert claimed.state == "created" and claimed.temporary_password
    assert claimed.temporary_password == pending_setup.accounts["lan@agency.example"]["password"]
    assert pending_setup.resent == [sub] and len(pending_setup.accounts) == 1
    assert conn.sql("SET status = 'claimed'") == [(INV, PARTNER_ISSUER, sub)]
    assert ctx.audit_events[-1].reason == "account_password_reissued"


@pytest.mark.asyncio
async def test_claim_again_reissues_only_while_the_bound_account_was_never_signed_in(
    monkeypatch: pytest.MonkeyPatch, vault: None
) -> None:
    """S263: the invitation was claimed but the person never saw the one-time password. Opening the same link again
    gives the bound account a new one while it is still in FORCE_CHANGE_PASSWORD; never once it has its own password,
    never for another account with that address, never for a binding to another pool."""
    directory = MemoryDirectory(PARTNER_ISSUER)
    first = await directory.create_or_find("lan@agency.example")
    conn = ScriptedConn()
    ctx = _system(conn)
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(ctx, _invitation("claimed", sub=first.sub)))
    again = await invitations.claim(conn, object(), directory, TOKEN)  # type: ignore[arg-type]
    assert again.state == "created" and again.temporary_password not in (None, first.temporary_password)
    assert directory.resent == [first.sub] and conn.sql("SET status = 'claimed'") == []
    assert ctx.audit_events[-1].reason == "account_password_reissued"
    # The person finished the first sign-in: no password any more.
    directory.accounts["lan@agency.example"]["status"] = "CONFIRMED"
    done = await invitations.claim(conn, object(), directory, TOKEN)  # type: ignore[arg-type]
    assert done.state == "already_claimed" and done.temporary_password is None
    # A binding to another sub (the address has another account now) or to another pool: nothing is reissued.
    directory.accounts["lan@agency.example"]["status"] = "FORCE_CHANGE_PASSWORD"
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(ctx, _invitation("claimed", sub="someone-else")))
    assert (await invitations.claim(conn, object(), directory, TOKEN)).state == "already_claimed"  # type: ignore[arg-type]
    other_pool = MemoryDirectory("https://cognito-idp.local.test/other_pool")
    other_pool.accounts = directory.accounts
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(ctx, _invitation("claimed", sub=first.sub)))
    assert (await invitations.claim(conn, object(), other_pool, TOKEN)).state == "already_claimed"  # type: ignore[arg-type]
    assert directory.resent == [first.sub]


@pytest.mark.asyncio
async def test_accept_binds_the_caller_s_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn().on("INSERT INTO crm.member", OTHER_MEMBER)
    ctx = _system(conn)
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(ctx, _invitation("claimed", sub=SUB, role="assistant")))
    out = await invitations.accept(conn, CALLER, TOKEN)  # type: ignore[arg-type]
    assert out == {"tenant_id": str(TENANT), "member_id": str(OTHER_MEMBER), "member_role": "assistant"}
    assert conn.sql("INSERT INTO crm.member") == [(PARTNER_ISSUER, SUB, "assistant")]
    assert conn.sql("SET status = 'accepted'") == [(INV, OTHER_MEMBER)]
    assert ctx.audit_events[-1].action == "invitation_accept" and ctx.audit_events[-1].outcome == "allowed"


@pytest.mark.asyncio
async def test_accept_brings_a_disabled_member_back_with_the_invited_role(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn().on("FOR UPDATE", {"member_id": OTHER_MEMBER, "status": "disabled"})
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(_system(conn), _invitation("claimed", sub=SUB)))
    await invitations.accept(conn, CALLER, TOKEN)  # type: ignore[arg-type]
    assert conn.sql("SET role = $2, status = 'active'") == [(OTHER_MEMBER, "agent")]
    assert conn.sql("INSERT INTO crm.member") == []


@pytest.mark.asyncio
async def test_accept_never_changes_an_active_member_s_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """A seeded owner (no accepted invitation) invited as agent is not demoted: 409, nothing written."""
    conn = ScriptedConn().on("FOR UPDATE", {"member_id": OTHER_MEMBER, "status": "active"})
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(_system(conn), _invitation("claimed", sub=SUB)))
    with raises(Conflict, "already_member"):
        await invitations.accept(conn, CALLER, TOKEN)  # type: ignore[arg-type]
    assert conn.sql("SET role = $2") == [] and conn.sql("SET status = 'accepted'") == []


@pytest.mark.asyncio
async def test_accept_rechecks_the_first_owner_rule_for_an_aa_owner_invitation(monkeypatch: pytest.MonkeyPatch) -> None:
    """AA-941 review: an owner invitation sent by AA's L1 console (invited_by platform:*) is re-checked at accept:
    an agency that now has an active owner or holds a traveller answers 409 and no membership is written."""
    recheck = "i.invited_by LIKE 'platform:%' AS by_platform"
    for row, code in (
        ({"by_platform": True, "has_owner": True, "holds_travellers": False}, "owner_exists"),
        ({"by_platform": True, "has_owner": False, "holds_travellers": True}, "has_travellers"),
    ):
        conn = ScriptedConn().on(recheck, row).on("INSERT INTO crm.member", OTHER_MEMBER)
        ctx = _system(conn)
        monkeypatch.setattr(invitations, "invitation_tx", fake_tx(ctx, _invitation("claimed", sub=SUB, role="owner")))
        with raises(Conflict, code):
            await invitations.accept(conn, CALLER, TOKEN)  # type: ignore[arg-type]
        assert conn.sql("INSERT INTO crm.member") == [] and conn.sql("SET status = 'accepted'") == []
        assert (ctx.audit_events[-1].outcome, ctx.audit_events[-1].reason) == ("denied", code)
    # Still a first owner (no owner, no traveller), or an owner invitation the agency's own owner sent: accepted.
    for row in (
        {"by_platform": True, "has_owner": False, "holds_travellers": False},
        {"by_platform": False, "has_owner": True, "holds_travellers": True},
    ):
        conn = ScriptedConn().on(recheck, row).on("INSERT INTO crm.member", OTHER_MEMBER)
        monkeypatch.setattr(
            invitations, "invitation_tx", fake_tx(_system(conn), _invitation("claimed", sub=SUB, role="owner"))
        )
        assert (await invitations.accept(conn, CALLER, TOKEN))["member_role"] == "owner"  # type: ignore[arg-type]
    # Other roles never run the check.
    conn = ScriptedConn().on("INSERT INTO crm.member", OTHER_MEMBER)
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(_system(conn), _invitation("claimed", sub=SUB)))
    await invitations.accept(conn, CALLER, TOKEN)  # type: ignore[arg-type]
    assert conn.sql(recheck) == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (asyncpg.UniqueViolationError("member_identity_uq"), "invitation_race"),
        (PgError("CRM01"), "last_owner"),
        (PgError("CRM02"), "role_write_forbidden"),
    ],
)
@pytest.mark.asyncio
async def test_accept_maps_database_refusals_to_409(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, code: str
) -> None:
    conn = ScriptedConn().on("INSERT INTO crm.member", error)
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(_system(conn), _invitation("claimed", sub=SUB)))
    with raises(Conflict, code):
        await invitations.accept(conn, CALLER, TOKEN)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_accept_lets_other_database_errors_through(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn().on("SET status = 'accepted'", PgError("57014"))
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(_system(conn), _invitation("claimed", sub=SUB)))
    with pytest.raises(PgError):
        await invitations.accept(conn, CALLER, TOKEN)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_accept_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn()
    ctx = _system(conn)
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(ctx, _invitation("claimed", sub="someone-else")))
    with raises(Forbidden, "invitation_identity_mismatch"):
        await invitations.accept(conn, CALLER, TOKEN)  # type: ignore[arg-type]
    assert ctx.audit_events[-1].outcome == "denied" and ctx.audit_events[-1].reason == "identity_mismatch"
    monkeypatch.setattr(invitations, "invitation_tx", fake_tx(_system(conn), _invitation("pending")))
    with raises(invitations.Gone, "invitation_not_claimed"):
        await invitations.accept(conn, CALLER, TOKEN)  # type: ignore[arg-type]
    late = ScriptedConn()
    monkeypatch.setattr(
        invitations, "invitation_tx", fake_tx(_system(late), _invitation("claimed", sub=SUB, expired=True))
    )
    with raises(invitations.Gone, "invitation_expired"):
        await invitations.accept(late, CALLER, TOKEN)  # type: ignore[arg-type]
    assert late.sql("SET status = 'expired'") == [(INV,)]


# ── recovery ─────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_banner_and_cancel() -> None:
    assert await recovery.banner(tenant_ctx(ScriptedConn(), role="assistant")) == {"recovery": None}
    conn = ScriptedConn().on(
        "WHERE status = 'pending'",
        {
            "request_id": REQ,
            "candidate_member_id": MEMBER,
            "reason": "owner_left",
            "eligible_at": NOW,
            "created_at": NOW,
        },
    )
    out = await recovery.banner(tenant_ctx(conn, role="assistant"))
    assert out["recovery"]["is_candidate"] and out["recovery"]["request_id"] == str(REQ)
    cancel = ScriptedConn().on("SET status = 'cancelled'", REQ)
    ctx = tenant_ctx(cancel, role="assistant")
    assert (await recovery.cancel(ctx, REQ))["status"] == "cancelled"
    assert cancel.sql("SET status = 'cancelled'") == [(REQ, f"member:{MEMBER}")]
    with pytest.raises(NotFound):
        await recovery.cancel(tenant_ctx(ScriptedConn()), REQ)


def _platform(conn: ScriptedConn) -> SystemContext:
    return SystemContext(conn=conn, tenant_id=TENANT, actor="platform:staff-1")


@pytest.mark.asyncio
async def test_platform_members_list(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn().on(
        "ORDER BY created_at, member_id",
        [
            {"member_id": MEMBER, "role": "owner", "status": "disabled", "created_at": NOW},
            {"member_id": OTHER_MEMBER, "role": "agent", "status": "active", "created_at": NOW},
        ],
    )
    monkeypatch.setattr(recovery, "platform_tx", fake_tx(_platform(conn)))
    out = await recovery.members(conn, TENANT, staff_sub="staff-1")  # type: ignore[arg-type]
    assert out["active_owners"] == 0 and out["recovery"] is None and len(out["members"]) == 2


@pytest.mark.asyncio
async def test_open_request_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    directory = MemoryDirectory(PARTNER_ISSUER)
    owner_sub = directory.add("owner@agency.example")
    mailer = MemoryMailer()
    conn = ScriptedConn()
    conn.on("FROM crm.member WHERE member_id = $1", {**_member(), "member_id": OTHER_MEMBER})
    conn.on("INSERT INTO crm.owner_recovery_request", {"request_id": REQ, "eligible_at": NOW + dt.timedelta(days=14)})
    conn.on(
        "WHERE role = 'owner' AND status = 'active'",
        [
            {"cognito_iss": PARTNER_ISSUER, "cognito_sub": owner_sub},
            {"cognito_iss": "https://other/x", "cognito_sub": "x"},
        ],
    )
    ctx = _platform(conn)
    monkeypatch.setattr(recovery, "platform_tx", fake_tx(ctx))
    kwargs = dict(
        candidate_member_id=OTHER_MEMBER,
        staff_sub="staff-1",
        directory=directory,
        mailer=mailer,
        web_origin="https://partner.example.test",
    )
    out = await recovery.open_request(conn, TENANT, reason="owner_left", **kwargs)  # type: ignore[arg-type]
    assert out["owners_notified"] == 1 and out["status"] == "pending"
    assert mailer.sent[0].to == "owner@agency.example" and mailer.sent[0].kind == "recovery_notice"
    assert conn.sql("INSERT INTO crm.owner_recovery_request") == [(OTHER_MEMBER, "owner_left", 14)]
    assert ctx.audit_events[-1].actor_kind == "aa_ops"
    with pytest.raises(Invalid):
        await recovery.open_request(conn, TENANT, reason="bored", **kwargs)  # type: ignore[arg-type]
    for member, code in (
        (None, "member_not_found"),
        (_member(status="disabled"), "candidate_inactive"),
        (_member("owner"), "candidate_is_owner"),
    ):
        bad = ScriptedConn().on("FROM crm.member WHERE member_id = $1", member)
        monkeypatch.setattr(recovery, "platform_tx", fake_tx(_platform(bad)))
        with pytest.raises((NotFound, Conflict)) as exc:
            await recovery.open_request(bad, TENANT, reason="owner_left", **kwargs)  # type: ignore[arg-type]
        assert exc.value.code == code
    dup = ScriptedConn().on("FROM crm.member WHERE member_id = $1", _member())
    dup.on("INSERT INTO crm.owner_recovery_request", asyncpg.UniqueViolationError("dup"))
    monkeypatch.setattr(recovery, "platform_tx", fake_tx(_platform(dup)))
    with raises(Conflict, "recovery_pending"):
        await recovery.open_request(dup, TENANT, reason="owner_left", **kwargs)  # type: ignore[arg-type]

    class Broken(MemoryDirectory):
        async def email_for(self, sub: str) -> str | None:
            raise RuntimeError("down")

    monkeypatch.setattr(recovery, "platform_tx", fake_tx(_platform(conn)))
    kwargs["directory"] = Broken(PARTNER_ISSUER)
    assert (await recovery.open_request(conn, TENANT, reason="no_owner", **kwargs))["owners_notified"] == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_complete_request_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = "WHERE request_id = $1 AND deleted_at IS NULL FOR UPDATE"
    conn = ScriptedConn().on(
        lock, {"request_id": REQ, "candidate_member_id": OTHER_MEMBER, "status": "pending", "eligible_at": NOW}
    )
    conn.on("FROM crm.member WHERE member_id = $1", _member())
    ctx = _platform(conn)
    monkeypatch.setattr(recovery, "platform_tx", fake_tx(ctx))
    out = await recovery.complete_request(conn, TENANT, REQ, staff_sub="staff-1")  # type: ignore[arg-type]
    assert out == {"request_id": str(REQ), "status": "completed", "owner_member_id": str(OTHER_MEMBER)}
    assert conn.sql("SET role = 'owner'") == [(OTHER_MEMBER,)]
    assert [e.action for e in ctx.audit_events] == ["recovery_complete", "role_change"]
    early = (
        ScriptedConn()
        .on(lock, {"request_id": REQ, "candidate_member_id": OTHER_MEMBER, "status": "pending", "eligible_at": NOW})
        .on("FROM crm.member WHERE member_id = $1", _member())
    )
    early.on("SET status = 'completed'", PgError("CRM03"))
    monkeypatch.setattr(recovery, "platform_tx", fake_tx(_platform(early)))
    with raises(Conflict, "recovery_not_due"):
        await recovery.complete_request(early, TENANT, REQ, staff_sub="staff-1")  # type: ignore[arg-type]
    other = (
        ScriptedConn()
        .on(lock, {"request_id": REQ, "candidate_member_id": OTHER_MEMBER, "status": "pending", "eligible_at": NOW})
        .on("FROM crm.member WHERE member_id = $1", _member())
    )
    other.on("SET status = 'completed'", PgError("23514"))
    monkeypatch.setattr(recovery, "platform_tx", fake_tx(_platform(other)))
    with pytest.raises(asyncpg.PostgresError):
        await recovery.complete_request(other, TENANT, REQ, staff_sub="staff-1")  # type: ignore[arg-type]
    for row, code in (
        (None, "recovery_not_found"),
        (
            {"request_id": REQ, "candidate_member_id": OTHER_MEMBER, "status": "cancelled", "eligible_at": NOW},
            "recovery_closed",
        ),
    ):
        closed = ScriptedConn().on(lock, row)
        monkeypatch.setattr(recovery, "platform_tx", fake_tx(_platform(closed)))
        with pytest.raises((NotFound, Conflict)) as exc:
            await recovery.complete_request(closed, TENANT, REQ, staff_sub="staff-1")  # type: ignore[arg-type]
        assert exc.value.code == code


@pytest.mark.asyncio
async def test_deactivate_member(monkeypatch: pytest.MonkeyPatch) -> None:
    directory = MemoryDirectory(PARTNER_ISSUER)
    conn = ScriptedConn().on("FROM crm.member WHERE member_id = $1", _member("owner"))
    monkeypatch.setattr(recovery, "platform_tx", fake_tx(_platform(conn)))
    out = await recovery.deactivate_member(conn, TENANT, OTHER_MEMBER, staff_sub="staff-1", directory=directory)  # type: ignore[arg-type]
    assert out == {"member_id": str(OTHER_MEMBER), "status": "disabled", "signed_out": True, "active_owners": 0}
    with pytest.raises(NotFound):
        monkeypatch.setattr(recovery, "platform_tx", fake_tx(_platform(ScriptedConn())))
        await recovery.deactivate_member(ScriptedConn(), TENANT, OTHER_MEMBER, staff_sub="s", directory=None)  # type: ignore[arg-type]

    class Broken(MemoryDirectory):
        async def global_sign_out(self, sub: str) -> None:
            raise RuntimeError("down")

    monkeypatch.setattr(recovery, "platform_tx", fake_tx(_platform(conn)))
    out = await recovery.deactivate_member(conn, TENANT, OTHER_MEMBER, staff_sub="s", directory=Broken(PARTNER_ISSUER))  # type: ignore[arg-type]
    assert out["signed_out"] is False
    assert recovery.platform_actor("x" * 200).startswith("platform:") and len(recovery.platform_actor("x" * 200)) == 80


# ── directory ────────────────────────────────────────────────────────────────────────────────────
class FakeIdp:
    class exceptions:  # noqa: N801 — boto3 shape
        class UsernameExistsException(Exception):
            pass

    def __init__(self, *, exists: dict[str, Any] | None = None, sub: str | None = "sub-new") -> None:
        self.exists = exists
        self.sub = sub
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def admin_create_user(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("admin_create_user", kw))
        if kw["MessageAction"] == "RESEND":  # resets an existing account's temporary password
            return {"User": self.exists or {}}
        if self.exists is not None:
            raise self.exceptions.UsernameExistsException()
        attrs = [{"Name": "sub", "Value": self.sub}] if self.sub else []
        return {"User": {"Attributes": attrs}}

    def list_users(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("list_users", kw))
        return {"Users": [self.exists] if self.exists else []}

    def admin_user_global_sign_out(self, **kw: Any) -> None:
        self.calls.append(("admin_user_global_sign_out", kw))


POOL = "us-west-1_TESTPARTN"


@pytest.mark.asyncio
async def test_cognito_directory_creates_with_suppress_and_a_strong_temporary_password() -> None:
    idp = FakeIdp()
    directory = CognitoDirectory(POOL, PARTNER_ISSUER, client=idp)
    account = await directory.create_or_find("lan@agency.example")
    assert account.sub == "sub-new" and account.temporary_password
    [(name, kw)] = idp.calls
    assert name == "admin_create_user" and kw["MessageAction"] == "SUPPRESS" and kw["UserPoolId"] == POOL
    assert {"Name": "email_verified", "Value": "true"} in kw["UserAttributes"]
    assert kw["TemporaryPassword"] == account.temporary_password
    await directory.global_sign_out("sub-new")
    assert idp.calls[-1] == ("admin_user_global_sign_out", {"UserPoolId": POOL, "Username": "sub-new"})


@pytest.mark.asyncio
async def test_cognito_directory_existing_accounts() -> None:
    existing = {
        "UserStatus": "CONFIRMED",
        "Attributes": [{"Name": "sub", "Value": "s-old"}, {"Name": "email", "Value": "e@x.io"}],
    }
    idp = FakeIdp(exists=existing)
    directory = CognitoDirectory(POOL, PARTNER_ISSUER, client=idp)
    assert await directory.create_or_find("e@x.io") == type(await directory.create_or_find("e@x.io"))("s-old", None)
    assert idp.calls[1] == ("list_users", {"UserPoolId": POOL, "Filter": 'email = "e@x.io"', "Limit": 2})
    assert await directory.email_for("s-old") == "e@x.io"
    with pytest.raises(Unavailable):
        await CognitoDirectory(
            POOL, PARTNER_ISSUER, client=FakeIdp(exists={"UserStatus": "CONFIRMED", "Attributes": []})
        ).create_or_find("e@x.io")
    with pytest.raises(Unavailable):
        await CognitoDirectory(POOL, PARTNER_ISSUER, client=FakeIdp(sub=None)).create_or_find("e@x.io")

    class Vanished(FakeIdp):
        def list_users(self, **kw: Any) -> dict[str, Any]:
            return {"Users": []}

    with pytest.raises(Unavailable):
        await CognitoDirectory(POOL, PARTNER_ISSUER, client=Vanished(exists=existing)).create_or_find("e@x.io")

    class Twins(FakeIdp):
        def list_users(self, **kw: Any) -> dict[str, Any]:
            return {"Users": [existing, existing]}

    with raises(Conflict, "account_ambiguous"):
        await CognitoDirectory(POOL, PARTNER_ISSUER, client=Twins(exists=existing)).create_or_find("e@x.io")
    assert await CognitoDirectory(POOL, PARTNER_ISSUER, client=FakeIdp()).email_for("nobody") is None


@pytest.mark.asyncio
async def test_cognito_directory_resends_a_temporary_password_to_an_account_never_signed_in() -> None:
    """S263: an existing FORCE_CHANGE_PASSWORD account gets a new temporary password through AdminCreateUser RESEND
    (the action the task role already has), mailed by e-mail (never the SMS default)."""
    pending = {
        "Username": "u-1",
        "UserStatus": "FORCE_CHANGE_PASSWORD",
        "Attributes": [{"Name": "sub", "Value": "s-old"}, {"Name": "email", "Value": "e@x.io"}],
    }
    idp = FakeIdp(exists=pending)
    account = await CognitoDirectory(POOL, PARTNER_ISSUER, client=idp).create_or_find("e@x.io")
    assert account.sub == "s-old" and account.temporary_password and account.reissued
    name, kw = idp.calls[-1]
    assert name == "admin_create_user" and kw == {
        "UserPoolId": POOL,
        "Username": "u-1",
        "TemporaryPassword": account.temporary_password,
        "MessageAction": "RESEND",
        "DesiredDeliveryMediums": ["EMAIL"],
    }
    assert [c[1]["MessageAction"] for c in idp.calls if c[0] == "admin_create_user"] == ["SUPPRESS", "RESEND"]


@pytest.mark.asyncio
async def test_cognito_directory_reissue_by_sub() -> None:
    pending = {
        "Username": "u-1",
        "UserStatus": "FORCE_CHANGE_PASSWORD",
        "Attributes": [{"Name": "sub", "Value": "s-old"}, {"Name": "email", "Value": "E@x.io"}],
    }
    idp = FakeIdp(exists=pending)
    password = await CognitoDirectory(POOL, PARTNER_ISSUER, client=idp).reissue_temporary_password("s-old", "e@x.io")
    assert password and idp.calls[0] == ("list_users", {"UserPoolId": POOL, "Filter": 'sub = "s-old"', "Limit": 1})
    assert idp.calls[1][1]["MessageAction"] == "RESEND" and idp.calls[1][1]["TemporaryPassword"] == password
    for user, email in (
        ({**pending, "UserStatus": "CONFIRMED"}, "e@x.io"),  # finished the first sign-in
        (pending, "other@x.io"),  # the bound account has another address
        (None, "e@x.io"),  # the account is gone
    ):
        quiet = FakeIdp(exists=user)
        directory = CognitoDirectory(POOL, PARTNER_ISSUER, client=quiet)
        assert await directory.reissue_temporary_password("s-old", email) is None
        assert [c[0] for c in quiet.calls] == ["list_users"]
    no_username = FakeIdp(exists={k: v for k, v in pending.items() if k != "Username"})
    directory = CognitoDirectory(POOL, PARTNER_ISSUER, client=no_username)
    assert await directory.reissue_temporary_password("s-old", "e@x.io")
    assert no_username.calls[-1][1]["Username"] == "e@x.io"


def test_directory_helpers() -> None:
    for _ in range(50):
        value = temporary_password()
        assert len(value) == 24 and any(c.isupper() for c in value) and any(c.islower() for c in value)
        assert any(c.isdigit() for c in value) and any(not c.isalnum() for c in value)
    with pytest.raises(ValueError):
        _quoted('a"b')
    with raises(Unavailable, "directory_not_configured"):
        require_directory(None)
    assert CognitoDirectory(POOL, PARTNER_ISSUER)._region == "us-west-1"


@pytest.mark.asyncio
async def test_memory_directory() -> None:
    directory = MemoryDirectory()
    created = await directory.create_or_find("A@B.io")
    assert created.temporary_password and await directory.email_for(created.sub) == "a@b.io"
    again = await directory.create_or_find("a@b.io")  # created but never signed in: a new password
    assert again.sub == created.sub and again.reissued and again.temporary_password != created.temporary_password
    assert await directory.reissue_temporary_password(created.sub, "a@b.io")
    assert await directory.reissue_temporary_password("other", "a@b.io") is None
    assert await directory.reissue_temporary_password(created.sub, "nobody@b.io") is None
    directory.accounts["a@b.io"]["status"] = "CONFIRMED"
    assert await directory.reissue_temporary_password(created.sub, "a@b.io") is None
    assert not (await directory.create_or_find("a@b.io")).reissued
    assert await directory.email_for("nobody") is None


# ── mail ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_messages_are_bilingual_and_carry_no_client_data() -> None:
    inv = invitation_message("x@y.io", agency="Sen Vàng", role="assistant", link="https://p/join?t=abc", expires_at=NOW)
    assert "Assistant" in inv.text and "Trợ lý" in inv.text and "https://p/join?t=abc" in inv.text
    assert "24 Oct 2026" in inv.text and inv.subject.startswith("Invitation to Sen Vàng")
    rec = recovery_message("x@y.io", agency="Sen Vàng", eligible_at=NOW, link="https://p")
    assert "khôi phục" in rec.text and rec.kind == "recovery_notice"


@pytest.mark.asyncio
async def test_ses_mailer_sends_as_the_aa_sender_through_the_configuration_set() -> None:
    sent: list[dict[str, Any]] = []

    class FakeSes:
        def send_email(self, **kw: Any) -> dict[str, Any]:
            sent.append(kw)
            return {"MessageId": "m-1"}

    mailer = SesMailer("no-reply@example.test", "aa-email-dev-transactional", client=FakeSes())
    message = invitation_message("x@y.io", agency="A", role="agent", link="https://p", expires_at=NOW)
    assert await mailer.send(message) == "m-1"
    [kw] = sent
    assert (
        kw["FromEmailAddress"] == "no-reply@example.test" and kw["ConfigurationSetName"] == "aa-email-dev-transactional"
    )
    assert kw["Destination"] == {"ToAddresses": ["x@y.io"]}
    assert {"Name": "kind", "Value": "invitation"} in kw["EmailTags"]
    with raises(Unavailable, "mail_not_configured"):
        require_mailer(None)


# ── tenancy: the matrix in tenant_tx, invitation_tx ──────────────────────────────────────────────
def _member_conn(role: str) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM crm.tenant", "active")
    conn.on("FROM crm.member", {"member_id": MEMBER, "role": role})
    return conn


@pytest.mark.asyncio
async def test_tenant_tx_refuses_a_denied_action_and_commits_the_denial() -> None:
    conn = _member_conn("agent")
    with raises(Forbidden, "role_forbidden"):
        async with tenant_tx(conn, CALLER, TENANT, action=Action.CLIENT_EXPORT):  # type: ignore[arg-type]
            raise AssertionError("the route's work must not run")
    [row] = conn.sql("INSERT INTO crm.audit_event")
    assert row[2:5] == ("client_export", "denied", "permission") and row[7] == "role_forbidden"
    allowed = _member_conn("owner")
    async with tenant_tx(allowed, CALLER, TENANT, action=Action.CLIENT_EXPORT) as ctx:  # type: ignore[arg-type]
        assert ctx.member_role == "owner"
    assert allowed.sql("INSERT INTO crm.audit_event") == []


@pytest.mark.asyncio
async def test_invitation_tx_finds_the_row_by_hash_then_sets_the_agency() -> None:
    digest = hashlib.sha256(TOKEN.encode()).hexdigest()
    conn = ScriptedConn()
    conn.on(
        "FROM crm.member_invitation",
        {
            "invitation_id": INV,
            "tenant_id": TENANT,
            "role": "agent",
            "status": "pending",
            "expires_at": NOW,
            "cognito_iss": None,
            "cognito_sub": None,
            "member_id": None,
            "email_enc": b"x",
            "expired": False,
        },
    )
    conn.on("FROM crm.tenant", "active")
    async with invitation_tx(conn, digest) as (ctx, invitation):  # type: ignore[arg-type]
        assert invitation.invitation_id == INV and ctx.actor == f"invitation:{INV}"
        ctx.audit("invitation_claim", "allowed", "member_invitation", str(INV))
    assert conn.sql("'app.invite_token_hash'") == [(digest,), ()]
    assert conn.sql("'app.actor'") == [(f"invitation:{INV}",)]
    [row] = conn.sql("INSERT INTO crm.audit_event")
    assert row[0] == "user"
    with pytest.raises(NotFound):
        async with invitation_tx(conn, "short"):  # type: ignore[arg-type]
            pass
    with pytest.raises(NotFound):
        async with invitation_tx(ScriptedConn(), digest):  # type: ignore[arg-type]
            pass
    locked = (
        ScriptedConn()
        .on("FROM crm.member_invitation", {"invitation_id": INV, "tenant_id": TENANT})
        .on("FROM crm.tenant", "locked")
    )
    with pytest.raises(NotFound):
        async with invitation_tx(locked, digest):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_invitation_tx_locks_the_row_once_the_agency_is_set() -> None:
    digest = hashlib.sha256(TOKEN.encode()).hexdigest()
    row = {
        "invitation_id": INV,
        "tenant_id": TENANT,
        "role": "agent",
        "status": "pending",
        "expires_at": NOW,
        "cognito_iss": None,
        "cognito_sub": None,
        "member_id": None,
        "email_enc": b"x",
        "expired": False,
    }
    conn = ScriptedConn().on("FROM crm.member_invitation", row).on("FROM crm.tenant", "active")
    async with invitation_tx(conn, digest):  # type: ignore[arg-type]
        pass
    flat = [sql for sql, _ in conn.calls]
    lock = next(i for i, sql in enumerate(flat) if "FOR UPDATE" in sql)
    agency = next(i for i, sql in enumerate(flat) if "'app.tenant_id'" in sql)
    assert agency < lock and conn.calls[lock][1] == (INV,)
    # The row is gone (revoked and trashed meanwhile, or another agency): 404 like an unknown token.
    gone = ScriptedConn().on("FOR UPDATE", None).on("token_hash = decode", row).on("FROM crm.tenant", "active")
    with pytest.raises(NotFound):
        async with invitation_tx(gone, digest):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_system_and_platform_contexts_take_their_own_actors_only() -> None:
    conn = ScriptedConn().on("FROM crm.tenant", "active")
    with pytest.raises(ValueError, match="system actor"):
        async with tenancy.system_tx(conn, TENANT, actor="platform:staff"):  # type: ignore[arg-type]
            pass
    with pytest.raises(ValueError, match="platform actor"):
        async with tenancy.platform_tx(conn, TENANT, actor="job:crm_intake_process"):  # type: ignore[arg-type]
            pass
    with pytest.raises(TypeError):
        async with tenancy.platform_tx(conn, "not-a-uuid", actor="platform:staff"):  # type: ignore[arg-type]
            pass
    async with tenancy.platform_tx(conn, TENANT, actor="platform:staff") as ctx:  # type: ignore[arg-type]
        ctx.audit("member_deactivate", "allowed", "member", "m")
    assert conn.sql("'app.actor'") == [("platform:staff",)]
    [event] = conn.sql("INSERT INTO crm.audit_event")
    assert event[0] == "aa_ops"


@pytest.mark.asyncio
async def test_platform_tx_takes_the_statuses_the_call_may_meet() -> None:
    """AA-941: default active only; a missing agency is 404 tenant_not_found, a refused status 409."""
    for status, cls, code in ((None, NotFound, "tenant_not_found"), ("locked", Conflict, "tenant_locked"),
                              ("offboarding", Conflict, "tenant_status")):  # fmt: skip
        conn = ScriptedConn().on("FROM crm.tenant", status)
        with raises(cls, code):
            async with tenancy.platform_tx(conn, TENANT, actor="platform:staff"):  # type: ignore[arg-type]
                pass
        assert not conn.sql("'app.actor'")
    conn = ScriptedConn().on("FROM crm.tenant", "offboarded")
    async with tenancy.platform_tx(conn, TENANT, actor="platform:s", statuses=tenancy.TENANT_STATUSES) as ctx:  # type: ignore[arg-type]
        assert ctx.status == "offboarded"
    for bad in (frozenset(), frozenset({"trashed"})):
        with pytest.raises(ValueError, match="statuses"):
            async with tenancy.platform_tx(conn, TENANT, actor="platform:s", statuses=bad):  # type: ignore[arg-type]
                pass
    # A job's context keeps its 403 answers.
    with raises(Forbidden, "tenant_locked"):
        async with tenancy.system_tx(ScriptedConn().on("FROM crm.tenant", "locked"), TENANT, actor="job:x"):  # type: ignore[arg-type]
            pass
    with raises(Forbidden, "not_a_member"):
        async with tenancy.system_tx(ScriptedConn(), TENANT, actor="job:x"):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_recovery_writes_carry_the_l1_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn()
    conn.on("FROM crm.member WHERE member_id = $1", {**_member(), "member_id": OTHER_MEMBER})
    conn.on("INSERT INTO crm.owner_recovery_request", {"request_id": REQ, "eligible_at": NOW + dt.timedelta(days=14)})
    ctx = _platform(conn)
    monkeypatch.setattr(recovery, "platform_tx", fake_tx(ctx))
    await recovery.open_request(
        conn,  # type: ignore[arg-type]
        TENANT,
        candidate_member_id=OTHER_MEMBER,
        reason="no_owner",
        staff_sub="staff-1",
        directory=None,
        mailer=None,
        web_origin="https://x.test",
        ticket_ref="SUP-3",
    )
    assert ctx.audit_events[-1].object_ids == ("ticket:SUP-3",)
    disable = ScriptedConn().on("FROM crm.member WHERE member_id = $1", _member())
    dctx = _platform(disable)
    monkeypatch.setattr(recovery, "platform_tx", fake_tx(dctx))
    await recovery.deactivate_member(disable, TENANT, OTHER_MEMBER, staff_sub="s", directory=None, ticket_ref="SUP-4")  # type: ignore[arg-type]
    assert dctx.audit_events[-1].object_ids == ("ticket:SUP-4",)
    assert recovery.ticket_ids(None) == ()


@pytest.mark.asyncio
async def test_invitation_tx_commits_denials_and_reraises() -> None:
    digest = hashlib.sha256(TOKEN.encode()).hexdigest()
    conn = ScriptedConn()
    conn.on(
        "FROM crm.member_invitation",
        {
            "invitation_id": INV,
            "tenant_id": TENANT,
            "role": "agent",
            "status": "claimed",
            "expires_at": NOW,
            "cognito_iss": PARTNER_ISSUER,
            "cognito_sub": "s",
            "member_id": None,
            "email_enc": b"x",
            "expired": False,
        },
    )
    conn.on("FROM crm.tenant", "active")
    with pytest.raises(Forbidden):
        async with invitation_tx(conn, digest) as (ctx, _):  # type: ignore[arg-type]
            ctx.audit("invitation_accept", "denied", "member_invitation", str(INV))
            raise Forbidden("invitation_identity_mismatch")
    assert len(conn.sql("INSERT INTO crm.audit_event")) == 1
    with pytest.raises(RuntimeError):
        async with invitation_tx(conn, digest):  # type: ignore[arg-type]
            raise RuntimeError("no denial: plain rollback")
    assert len(conn.sql("INSERT INTO crm.audit_event")) == 1


@pytest.mark.asyncio
async def test_system_actor_kinds() -> None:
    assert tenancy._system_actor_kind("platform:s") == "aa_ops"
    assert tenancy._system_actor_kind("invitation:x") == "user"
    assert tenancy._system_actor_kind("job:crm_intake_process") == "system"
