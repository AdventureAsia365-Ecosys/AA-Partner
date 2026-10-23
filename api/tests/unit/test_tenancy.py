"""tenant_tx() order and refusals with a scripted connection (the RLS behaviour itself: tests/integration)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.route_guard import Caller
from app.db import Database
from app.errors import Forbidden
from app.main import create_app
from app.tenancy import READER_ROLES, tenant_tx
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
    app = create_app(settings, FakeDatabase(conn, settings), pools.verifier(settings))
    headers = {"Authorization": f"Bearer {pools.partner_web()}"}
    with TestClient(app) as c:
        assert c.get("/crm/v1/me", headers=headers).json() == {"detail": "missing_tenant"}
        assert c.get("/crm/v1/me", headers={**headers, "X-AA-Tenant-Id": "nope"}).json() == {"detail": "invalid_tenant"}
        r = c.get("/crm/v1/me", headers={**headers, "X-AA-Tenant-Id": str(TENANT)})
    assert r.status_code == 200
    assert r.json() == {"tenant_id": str(TENANT), "member_id": str(MEMBER), "member_role": "owner"}


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
