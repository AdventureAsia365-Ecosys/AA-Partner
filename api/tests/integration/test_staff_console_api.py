"""AA-1019 on PostgreSQL with the RDS role shape: what the staff console (S-02, S-04) adds to the AA-971 / AA-956 staff
API.

- ``GET /crm/staff/v1/breakglass/agencies`` (the break-glass picker): active and locked agencies by name, the caller's
  own live break-glass session, nothing of the agency's data and no audit event.
- The Dev-only CRM staff E2E identity (root ADR 0006a A10c, ``Caller.e2e_synthetic_only``) reaches synthetic agencies
  only: every staff context refuses a real agency with 403 ``e2e_synthetic_only`` (support start, break-glass, the
  reads, the ops release and its manifest), and the lists (support agencies, break-glass agencies, the S-02 board)
  leave such an agency out. A normal staff caller is unchanged.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import pytest

from app.auth.route_guard import Caller
from app.errors import Forbidden
from app.members.directory import MemoryDirectory
from app.members.mail import MemoryMailer
from app.ops_release import staff as ops_staff
from app.support import reads, sessions
from tests.integration.test_crm005_intake import _keys, _owner_fetch
from tests.integration.test_crm006_travellers import _inquiry
from tests.integration.test_ops_release_api import _feed, _setup, _sync
from tests.integration.test_support_access_api import STAFF_ISS, WEB, _audit, _grant

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def staff(*groups: str, e2e: bool = False) -> Caller:
    return Caller(
        "staff", "workforce", "crm-staff", STAFF_ISS, str(uuid.uuid4()), groups or ("crm-ops",), e2e_synthetic_only=e2e
    )


async def _set_synthetic(admin: asyncpg.Connection, tenant_id: uuid.UUID, value: bool) -> None:
    rows = await _owner_fetch(
        admin, tenant_id, "UPDATE crm.tenant SET is_synthetic = $2 WHERE tenant_id = $1 RETURNING is_synthetic",
        tenant_id, value,
    )  # fmt: skip
    assert [r["is_synthetic"] for r in rows] == [value]


async def _breakglass(pool: asyncpg.Pool, caller: Caller, tenant_id: uuid.UUID) -> dict[str, Any]:
    return await sessions.start_breakglass(
        pool, caller, tenant_id, ticket_ref="INC-9", reason_code="service_outage", minutes=15,
        directory=MemoryDirectory({}), mailer=MemoryMailer(), web_origin=WEB,
    )  # fmt: skip


async def test_the_breakglass_picker_lists_agencies_by_name_and_my_live_session(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    tenant_id, _owner, _trip, _lead = await _inquiry(app_pool, keys)
    ops = staff("crm-ops")
    listed = {i["tenant_id"]: i for i in (await sessions.breakglass_agencies(app_pool, ops))["items"]}
    mine = listed[str(tenant_id)]
    assert mine == {
        "tenant_id": str(tenant_id),
        "agency_name": "Sen Vàng Travel",
        "agency_status": "active",
        "my_session_expires_at": None,
    }
    opened = await _breakglass(app_pool, ops, tenant_id)
    listed = {i["tenant_id"]: i for i in (await sessions.breakglass_agencies(app_pool, ops))["items"]}
    assert listed[str(tenant_id)]["my_session_expires_at"] == opened["expires_at"]
    # Another staff member sees the agency without a session of their own.
    other = {i["tenant_id"]: i for i in (await sessions.breakglass_agencies(app_pool, staff("crm-ops")))["items"]}
    assert other[str(tenant_id)]["my_session_expires_at"] is None
    # Listing reads nothing of the agency: no audit event of its own (the open above wrote breakglass_open only).
    actions = {r["action"] for r in await _owner_fetch(admin, tenant_id, "SELECT action FROM crm.audit_event")}
    assert "support_read" not in actions and "breakglass_open" in actions


async def test_e2e_staff_identity_reaches_synthetic_agencies_only(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    real, real_owner, _t1, _l1 = await _inquiry(app_pool, keys)
    synthetic, synth_owner, trip, _l2 = await _inquiry(app_pool, keys)
    await _set_synthetic(admin, real, False)
    for tenant, owner in ((real, real_owner), (synthetic, synth_owner)):
        await _grant(app_pool, owner, tenant)
    e2e = staff("crm-ops", "crm-support", e2e=True)

    # A real agency: every staff context refuses it, the lists leave it out.
    for start in (
        lambda: sessions.start_support(app_pool, e2e, real, ticket_ref="SUP-1"),
        lambda: _breakglass(app_pool, e2e, real),
        lambda: sessions.my_sessions(app_pool, e2e, real),
        lambda: sessions.end_session(app_pool, e2e, real, kind="support"),
        lambda: reads.overview(app_pool, e2e, real),
        lambda: ops_staff.read(app_pool, keys, e2e, real, trip),
        lambda: ops_staff.manifest(app_pool, keys, e2e, real, trip, []),
    ):
        with pytest.raises(Forbidden) as refused:
            await start()
        assert refused.value.code == "e2e_synthetic_only"
    granted = {i["tenant_id"] for i in (await sessions.granted_agencies(app_pool, e2e))["items"]}
    picker = {i["tenant_id"] for i in (await sessions.breakglass_agencies(app_pool, e2e))["items"]}
    assert str(real) not in granted | picker and str(synthetic) in granted and str(synthetic) in picker
    assert await _owner_fetch(admin, real, "SELECT 1 AS ok FROM crm.support_session") == []

    # A synthetic agency: the normal rules apply (support inside the owner's grant, then the reads).
    started = await sessions.start_support(app_pool, e2e, synthetic, ticket_ref="SUP-2")
    assert started["kind"] == "support"
    assert (await reads.overview(app_pool, e2e, synthetic))["tenant_id"] == str(synthetic)
    [start_event] = await _audit(admin, synthetic, "support_session_start")
    assert start_event["actor_ref"] == f"support:{e2e.sub}"

    # A normal staff member still reaches the real agency.
    support = staff("crm-support")
    assert (await sessions.start_support(app_pool, support, real, ticket_ref="SUP-3"))["kind"] == "support"


async def test_the_e2e_identity_sees_only_synthetic_agencies_on_the_s02_board(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    s = await _setup(app_pool)
    await _feed(app_pool, s["tenant"], "line.confirmed", s["line"])
    assert (await _sync(app_pool, s["keys"], s["tenant"], s["trip"]))["created"] == 1
    e2e = staff("crm-ops", e2e=True)

    def on_board(board: dict[str, Any]) -> bool:
        return str(s["trip"]) in {i["trip_id"] for i in board["items"]}

    assert on_board(await ops_staff.board(app_pool, e2e))
    got = await ops_staff.read(app_pool, s["keys"], e2e, s["tenant"], s["trip"])
    assert got["view_count"] == 1
    await _set_synthetic(admin, s["tenant"], False)
    assert not on_board(await ops_staff.board(app_pool, e2e))
    assert on_board(await ops_staff.board(app_pool, staff("crm-ops")))  # a normal staff member: unchanged
