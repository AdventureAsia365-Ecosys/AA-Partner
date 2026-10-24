"""tenant_tx() order and refusals with a scripted connection (the RLS behaviour itself: tests/integration)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.route_guard import Caller
from app.db import Database
from app.errors import Forbidden
from app.main import create_app
from app.tenancy import MAX_MEMBERSHIPS, READER_ROLES, caller_memberships, tenant_tx
from tests.tokens import PARTNER_ISSUER, SUB, LocalPools
from tests.unit.fakes import ScriptedConn

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
MEMBER = uuid.UUID("00000000-0000-4000-8000-0000000000b1")
CALLER = Caller("agency", "partner", "partner-web", PARTNER_ISSUER, SUB, ())


def _conn(status: str | None = "active", member: dict | None = None) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM crm.tenant", status)
    conn.on("FROM crm.member", member if member is not None else {"member_id": MEMBER, "role": "agent"})
    return conn


@pytest.mark.asyncio
async def test_sets_the_tenant_first_then_checks_membership_then_role_and_actor() -> None:
    conn = _conn()
    async with tenant_tx(conn, CALLER, TENANT) as ctx:
        assert (ctx.tenant_id, ctx.member_id, ctx.member_role) == (TENANT, MEMBER, "agent")
    sqls = [flat for flat, _ in conn.calls]
    assert sqls[0].startswith("SELECT set_config('app.tenant_id', $1, true)")
    assert "FROM crm.tenant" in sqls[1] and "FROM crm.member" in sqls[2]
    assert conn.calls[0][1] == (str(TENANT),)
    assert conn.sql("FROM crm.member")[0] == (TENANT, PARTNER_ISSUER, SUB)
    assert conn.sql("'app.member_role'") == [("agent",)]
    assert conn.sql("'app.actor'") == [(f"member:{MEMBER}",)]
    assert not any("SET LOCAL ROLE" in s for s in sqls)


@pytest.mark.parametrize(("status", "detail"), [(None, "not_a_member"), ("locked", "tenant_locked")])
@pytest.mark.asyncio
async def test_unknown_or_locked_agency_is_refused(status: str | None, detail: str) -> None:
    with pytest.raises(Forbidden) as info:
        async with tenant_tx(_conn(status=status), CALLER, TENANT):
            pass
    assert info.value.code == detail


@pytest.mark.asyncio
async def test_no_membership_is_refused_before_any_role_is_set() -> None:
    conn = ScriptedConn().on("FROM crm.tenant", "active")
    with pytest.raises(Forbidden, match="not_a_member"):
        async with tenant_tx(conn, CALLER, TENANT):
            pass
    assert conn.sql("'app.member_role'") == []


@pytest.mark.asyncio
async def test_reader_role_only_from_the_allow_list() -> None:
    conn = _conn()
    async with tenant_tx(conn, CALLER, TENANT, reader_role="crm_insights"):
        pass
    assert any(flat == "SET LOCAL ROLE crm_insights" for flat, _ in conn.calls)
    with pytest.raises(ValueError, match="reader role"):
        async with tenant_tx(_conn(), CALLER, TENANT, reader_role="crm_owner; DROP SCHEMA crm"):
            pass
    assert READER_ROLES == {"crm_ai_reader", "crm_insights", "crm_support_reader", "crm_eraser"}


@pytest.mark.asyncio
async def test_tenant_id_must_be_a_uuid() -> None:
    with pytest.raises(TypeError):
        async with tenant_tx(_conn(), CALLER, str(TENANT)):  # type: ignore[arg-type]
            pass


class FakeDatabase(Database):
    def __init__(self, conn: ScriptedConn, settings) -> None:  # noqa: ANN001
        super().__init__(settings)
        self.conn = conn

    @property
    def configured(self) -> bool:
        return True

    async def pool(self):  # type: ignore[override]
        return self.conn


def test_me_route_goes_through_tenant_tx() -> None:
    pools = LocalPools()
    settings = pools.settings()
    conn = _conn(member={"member_id": MEMBER, "role": "owner"})
    conn.on("SELECT display_name, default_locale", {"display_name": "Lữ hành Sen Vàng", "default_locale": "vi-VN"})
    app = create_app(settings, FakeDatabase(conn, settings), pools.verifier(settings))
    headers = {"Authorization": f"Bearer {pools.partner_web()}"}
    with TestClient(app) as c:
        assert c.get("/crm/v1/me", headers=headers).json() == {"detail": "missing_tenant"}
        assert c.get("/crm/v1/me", headers={**headers, "X-AA-Tenant-Id": "nope"}).json() == {"detail": "invalid_tenant"}
        r = c.get("/crm/v1/me", headers={**headers, "X-AA-Tenant-Id": str(TENANT)})
    assert r.status_code == 200
    assert r.json() == {
        "tenant_id": str(TENANT),
        "member_id": str(MEMBER),
        "member_role": "owner",
        "display_name": "Lữ hành Sen Vàng",
        "default_locale": "vi-VN",
    }


def test_me_for_a_non_member_is_403() -> None:
    pools = LocalPools()
    settings = pools.settings()
    conn = ScriptedConn().on("FROM crm.tenant", "active")
    app = create_app(settings, FakeDatabase(conn, settings), pools.verifier(settings))
    with TestClient(app) as c:
        r = c.get(
            "/crm/v1/me", headers={"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
        )
    assert (r.status_code, r.json()["detail"]) == (403, "not_a_member")


# ── Audit queue (AA-934): written at the end, denials survive the rollback ──────────────────────────


@pytest.mark.asyncio
async def test_queued_events_are_written_when_the_block_succeeds() -> None:
    conn = _conn()
    async with tenant_tx(conn, CALLER, TENANT) as ctx:
        ctx.audit("reveal", "allowed", "traveler", "t-1", fields=("email_enc",), reason="client_call")
    rows = conn.sql("INSERT INTO crm.audit_event")
    assert rows == [
        ("user", f"member:{MEMBER}", "reveal", "allowed", "traveler", "t-1", ["email_enc"], "client_call", None)
    ]


@pytest.mark.asyncio
async def test_on_error_only_denials_are_written_and_the_error_goes_on() -> None:
    conn = _conn()
    with pytest.raises(Forbidden, match="decrypt_denied"):
        async with tenant_tx(conn, CALLER, TENANT) as ctx:
            ctx.audit("reveal", "allowed", "traveler", "t-1", fields=("email_enc",))
            ctx.audit("decrypt", "denied", "traveler", "t-2", fields=("email_enc",), reason="integrity_check_failed")
            raise Forbidden("decrypt_denied")
    rows = conn.sql("INSERT INTO crm.audit_event")
    assert [r[2:4] for r in rows] == [("decrypt", "denied")]


@pytest.mark.asyncio
async def test_on_error_without_denials_nothing_is_written() -> None:
    conn = _conn()
    with pytest.raises(RuntimeError):
        async with tenant_tx(conn, CALLER, TENANT) as ctx:
            ctx.audit("reveal", "allowed", "traveler", "t-1")
            raise RuntimeError("boom")
    assert conn.sql("INSERT INTO crm.audit_event") == []


@pytest.mark.asyncio
async def test_reader_role_is_dropped_before_writing_events() -> None:
    conn = _conn()
    async with tenant_tx(conn, CALLER, TENANT, reader_role="crm_support_reader") as ctx:
        assert ctx.reader_role == "crm_support_reader"
        ctx.audit("decrypt", "denied", "traveler", "t-1", reason="tenant_mismatch")
    sqls = [flat for flat, _ in conn.calls]
    assert sqls.index("SET LOCAL ROLE NONE") < next(i for i, s in enumerate(sqls) if "crm.audit_event" in s)


def test_audit_events_carry_codes_only() -> None:
    from app.audit import AuditEvent

    ok = AuditEvent("user", "member:1", "decrypt", "denied", "traveler", "t-1", ("email_enc",), "tenant_mismatch", "r1")
    assert ok.row()[6] == ["email_enc"]
    assert AuditEvent("system", "system:x", "a", "allowed", "b", request_id="bad id with spaces").request_id is None
    bad = [
        {"actor_kind": "hacker"},
        {"outcome": "maybe"},
        {"actor_ref": "an@example.test"},
        {"action": "Reveal Email"},
        {"object_id": "Nguyễn"},
        {"fields": ("Email",)},
        {"reason": "the client asked me on the phone"},
    ]
    base = {"actor_kind": "user", "actor_ref": "member:1", "action": "decrypt", "outcome": "denied", "object_kind": "t"}
    for change in bad:
        with pytest.raises(ValueError):
            AuditEvent(**{**base, **change})


# ── Caller memberships (AA-935): the agency list before tenant_tx() ─────────────────────────────────


MEMBERSHIP_ROW = {
    "tenant_id": TENANT,
    "display_name": "Lữ hành Sen Vàng",
    "default_locale": "vi-VN",
    "tenant_status": "active",
    "member_id": MEMBER,
    "role": "agent",
}


@pytest.mark.asyncio
async def test_caller_memberships_sets_the_caller_identity_and_never_a_tenant() -> None:
    conn = ScriptedConn().on("FROM crm.member m", [MEMBERSHIP_ROW, {**MEMBERSHIP_ROW, "role": "nonsense"}])
    found = await caller_memberships(conn, CALLER)
    assert [(m.tenant_id, m.member_role, m.display_name) for m in found] == [(TENANT, "agent", "Lữ hành Sen Vàng")]
    assert conn.sql("'app.caller_iss'") == [(PARTNER_ISSUER,)]
    assert conn.sql("'app.caller_sub'") == [(SUB,)]
    assert conn.sql("'app.tenant_id'") == []
    assert conn.sql("FROM crm.member m")[0] == (PARTNER_ISSUER, SUB, MAX_MEMBERSHIPS)
    with pytest.raises(Forbidden):
        await caller_memberships(conn, Caller("agency", "partner", "partner-web", PARTNER_ISSUER, "", ()))


def test_memberships_route_needs_no_tenant_header() -> None:
    pools = LocalPools()
    settings = pools.settings()
    conn = ScriptedConn().on("FROM crm.member m", [MEMBERSHIP_ROW])
    app = create_app(settings, FakeDatabase(conn, settings), pools.verifier(settings))
    with TestClient(app) as c:
        r = c.get("/crm/v1/memberships", headers={"Authorization": f"Bearer {pools.cis_portal()}"})
        foreign = c.get("/crm/v1/memberships", headers={"Authorization": f"Bearer {pools.cis_admin()}"})
        anonymous = c.get("/crm/v1/memberships")
    assert r.status_code == 200
    assert r.json() == {
        "memberships": [
            {
                "tenant_id": str(TENANT),
                "member_id": str(MEMBER),
                "member_role": "agent",
                "display_name": "Lữ hành Sen Vàng",
                "default_locale": "vi-VN",
                "tenant_status": "active",
            }
        ]
    }
    assert (foreign.status_code, anonymous.status_code) == (403, 401)
