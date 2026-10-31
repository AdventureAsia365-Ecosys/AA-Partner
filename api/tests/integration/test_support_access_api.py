"""AA-971 (card CRM-13b, design v3 §4.8 L2 / L3) on PostgreSQL with the RDS role shape, after crm-033: the real
``tenant_tx()`` / ``staff_tx()`` / ``support_tx()``, guards, reader grants and audit.

Card tests:
- R: a read after the grant's window passed -> 403; the support reader cannot write (export / send have no route,
  ``tests/unit/test_support_routes.py``; the database refuses any write here).
- E: a revoke in the middle of a session -> the next read is 403; a break-glass session in agency A cannot read or
  decrypt agency B.
Plus the live-check story: grant -> support read -> the read in the owner's access log (actor "AA support") -> revoke
-> 403; break-glass with a reason -> the owners mailed, the ops alert event, the audit.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import pytest
from structlog.testing import capture_logs

from app.audit_log import AuditFilter, list_events
from app.auth.route_guard import Caller
from app.errors import Conflict, Forbidden
from app.members.directory import MemoryDirectory
from app.members.mail import MemoryMailer
from app.support import grants, reads, sessions
from app.tenancy import support_tx, tenant_tx
from app.vault import Vault
from app.vault.errors import DecryptionDenied
from tests.integration.test_crm005_intake import ISS, _keys, _owner_fetch
from tests.integration.test_crm006_travellers import _inquiry

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

STAFF_ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTWORKF"
WEB = "https://partner.example.test"


def staff(group: str = "crm-support", sub: str | None = None) -> Caller:
    return Caller("staff", "workforce", "crm-staff", STAFF_ISS, sub or str(uuid.uuid4()), (group,))


async def _audit(admin: asyncpg.Connection, tenant_id: uuid.UUID, action: str) -> list[asyncpg.Record]:
    return await _owner_fetch(
        admin,
        tenant_id,
        "SELECT actor_kind, actor_ref, action, outcome, object_kind, object_id, reason, fields, object_ids "
        "FROM crm.audit_event WHERE action = $1 ORDER BY at, event_id",
        action,
    )


async def _grant(pool: asyncpg.Pool, owner: Caller, tenant_id: uuid.UUID, **kw: Any) -> dict[str, Any]:
    async with tenant_tx(pool, owner, tenant_id) as ctx:
        return await grants.grant_access(ctx, days=kw.get("days", 1), show_identity=kw.get("show_identity", False))


async def test_grant_read_audit_revoke_then_403(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    tenant_id, owner, trip_id, _lead = await _inquiry(app_pool, keys)
    sup = staff()
    # No grant: the staff member cannot start, and the attempt is on the agency's record.
    with pytest.raises(Forbidden) as no_grant:
        await sessions.start_support(app_pool, sup, tenant_id, ticket_ref="SUP-1042")
    assert no_grant.value.code == "no_support_grant"
    with pytest.raises(Forbidden) as no_session:
        await reads.overview(app_pool, sup, tenant_id)
    assert no_session.value.code == "no_support_session"

    view = await _grant(app_pool, owner, tenant_id)
    assert view["grant"] is not None and view["grant"]["show_identity"] is False and view["can_manage"] is True
    with pytest.raises(Conflict):
        await _grant(app_pool, owner, tenant_id)  # one live grant at a time

    started = await sessions.start_support(app_pool, sup, tenant_id, ticket_ref="SUP-1042")
    assert (started["kind"], started["status"], started["show_identity"]) == ("support", "active", False)
    # Starting again returns the same session; another staff member waits (one at a time).
    again = await sessions.start_support(app_pool, sup, tenant_id, ticket_ref="SUP-1042")
    assert again["session_id"] == started["session_id"]
    with pytest.raises(Conflict) as busy:
        await sessions.start_support(app_pool, staff(), tenant_id, ticket_ref="SUP-1043")
    assert busy.value.code == "support_session_in_use"

    overview = await reads.overview(app_pool, sup, tenant_id)
    assert overview["pipeline"]["inquiry"] >= 1 and overview["session"]["kind"] == "support"
    page = await reads.trips(app_pool, sup, tenant_id, stage=None, cursor=None, limit=10)
    assert str(trip_id) in {t["trip_id"] for t in page["items"]}
    assert all("title" not in t for t in page["items"])
    detail = await reads.trip(app_pool, sup, tenant_id, trip_id, keys=keys)
    assert detail["party"] is None and detail["identity_hidden"] is True and detail["stage_history"]

    # Every read is the agency's: actor kind support, one list event naming the trips shown.
    events = await _audit(admin, tenant_id, "support_read")
    allowed = [e for e in events if e["outcome"] == "allowed"]
    assert [e["reason"] for e in allowed] == ["overview", "trips", "trip"]
    assert {e["actor_kind"] for e in allowed} == {"support"} and allowed[0]["actor_ref"] == f"support:{sup.sub}"
    assert str(trip_id) in allowed[1]["object_ids"]
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        log = await list_events(ctx, AuditFilter(actor="aa_support"), cursor=None, limit=50)
    assert {"support_read", "support_session_start"} <= {e["action"] for e in log["events"]}

    # The owner sees the live session (banner), then ends it: the very next read is 403.
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        live = await grants.support_access(ctx)
    assert [s["session_id"] for s in live["active_sessions"]] == [started["session_id"]]
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        after = await grants.revoke_access(ctx)
    assert after["grant"] is None and after["active_sessions"] == []
    assert after["recent_sessions"][0]["status"] == "revoked"
    with pytest.raises(Forbidden) as revoked:
        await reads.trips(app_pool, sup, tenant_id, stage=None, cursor=None, limit=10)
    assert revoked.value.code == "support_session_ended"
    denied = [e for e in await _audit(admin, tenant_id, "support_read") if e["outcome"] == "denied"]
    assert [e["reason"] for e in denied][-1] == "support_session_ended"
    [revoke] = await _audit(admin, tenant_id, "support_revoke")
    assert started["session_id"] in revoke["object_ids"]


async def test_names_only_when_the_owner_ticked_show_identity(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    tenant_id, owner, trip_id, lead = await _inquiry(app_pool, keys)
    await _grant(app_pool, owner, tenant_id, days=7, show_identity=True)
    sup = staff()
    assert (await sessions.start_support(app_pool, sup, tenant_id, ticket_ref="SUP-7"))["show_identity"] is True
    detail = await reads.trip(app_pool, sup, tenant_id, trip_id, keys=keys)
    assert detail["identity_hidden"] is False
    person = detail["party"][0]  # the lead first, then the companions the inquiry named
    assert person["traveler_id"] == str(lead) and person["role"] == "lead" and person["display_name"]
    assert all(p["display_name"] for p in detail["party"])
    [reveal] = await _audit(admin, tenant_id, "reveal")
    assert (reveal["actor_kind"], list(reveal["fields"]), reveal["reason"]) == ("support", ["display_name"], "support")
    assert list(reveal["object_ids"]) == [p["traveler_id"] for p in detail["party"]]


async def test_a_read_after_the_window_passed_is_403(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    tenant_id, owner, _trip, _lead = await _inquiry(app_pool, keys)
    await _grant(app_pool, owner, tenant_id)
    sup = staff()
    await sessions.start_support(app_pool, sup, tenant_id, ticket_ref="SUP-8")
    await reads.overview(app_pool, sup, tenant_id)
    async with admin.transaction():  # crm_owner moves the grant's window into the past
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await admin.execute(
            "UPDATE crm.support_grant SET starts_at = starts_at - interval '3 days', "
            "expires_at = expires_at - interval '3 days'"
        )
    with pytest.raises(Forbidden) as expired:
        await reads.overview(app_pool, sup, tenant_id)
    assert expired.value.code == "support_access_expired"
    # The owner's page shows no live grant; a new grant marks the old one expired first.
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        assert (await grants.support_access(ctx))["grant"] is None
    assert (await _grant(app_pool, owner, tenant_id))["grant"] is not None
    statuses = await _owner_fetch(admin, tenant_id, "SELECT status FROM crm.support_grant ORDER BY created_at")
    assert [r["status"] for r in statuses] == ["expired", "active"]


async def test_the_support_reader_cannot_write(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant_id, owner, trip_id, _lead = await _inquiry(app_pool, keys)
    await _grant(app_pool, owner, tenant_id)
    sup = staff()
    await sessions.start_support(app_pool, sup, tenant_id, ticket_ref="SUP-9")
    for sql in (
        "UPDATE crm.trip SET stage = 'lost' WHERE trip_id = $1",
        "DELETE FROM crm.trip WHERE trip_id = $1",
        "SELECT title FROM crm.trip WHERE trip_id = $1",
        "SELECT count(*) FROM crm.trip_traveler WHERE trip_id = $1",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with support_tx(app_pool, sup, tenant_id) as ctx:
                await ctx.conn.execute(sql, trip_id)


async def test_break_glass_alerts_audits_and_stays_in_one_agency(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    tenant_a, owner_a, trip_a, lead_a = await _inquiry(app_pool, keys)
    tenant_b, owner_b, trip_b, lead_b = await _inquiry(app_pool, keys)
    ops = staff("crm-ops")
    directory = MemoryDirectory(issuer=ISS)
    directory.accounts["owner.a@example.test"] = {"sub": owner_a.sub, "status": "CONFIRMED"}
    mailer = MemoryMailer()
    with capture_logs() as logs:
        opened = await sessions.start_breakglass(
            app_pool,
            ops,
            tenant_a,
            ticket_ref="INC-7",
            reason_code="data_corruption",
            minutes=30,
            directory=directory,
            mailer=mailer,
            web_origin=WEB,
        )
    assert (opened["kind"], opened["show_identity"], opened["owners_notified"]) == ("breakglass", True, 1)
    [alert] = [e for e in logs if e["event"] == "crm_breakglass_opened"]
    assert alert["tenant_id"] == str(tenant_a) and alert["reason_code"] == "data_corruption"
    [mail] = mailer.sent
    assert (
        mail.to == "owner.a@example.test" and mail.kind == "breakglass_notice" and "/settings/access-log" in mail.text
    )
    [opened_event] = await _audit(admin, tenant_a, "breakglass_open")
    assert (opened_event["actor_kind"], opened_event["reason"]) == ("breakglass", "data_corruption")
    assert "ticket:INC-7" in opened_event["object_ids"]
    assert await _audit(admin, tenant_a, "breakglass_owner_notice")

    # Identity is shown in break-glass, and audited as the AA emergency actor.
    detail = await reads.trip(app_pool, ops, tenant_a, trip_a, keys=keys)
    assert detail["party"][0]["traveler_id"] == str(lead_a)
    async with tenant_tx(app_pool, owner_a, tenant_a) as ctx:
        log = await list_events(ctx, AuditFilter(actor="aa_emergency"), cursor=None, limit=50)
        banner = await grants.support_access(ctx)
    assert {"breakglass_open", "support_read", "reveal"} <= {e["action"] for e in log["events"]}
    assert [s["kind"] for s in banner["active_sessions"]] == ["breakglass"]
    # The owner cannot end an emergency session (no grant to revoke).
    with pytest.raises(Conflict):
        async with tenant_tx(app_pool, owner_a, tenant_a) as ctx:
            await grants.revoke_access(ctx)

    # Agency B: no session there, so nothing is read; and a value of B never opens in A's session.
    with pytest.raises(Forbidden) as other:
        await reads.trip(app_pool, ops, tenant_b, trip_b, keys=keys)
    assert other.value.code == "no_support_session"
    [b_row] = await _owner_fetch(
        admin, tenant_b, "SELECT display_name_enc FROM crm.traveler WHERE traveler_id = $1", lead_b
    )
    with pytest.raises(DecryptionDenied):
        async with support_tx(app_pool, ops, tenant_a) as ctx:
            vault = Vault(keys, ctx)  # type: ignore[arg-type]
            await vault.decrypt("traveler", "display_name_enc", lead_b, b_row["display_name_enc"], tenant_id=tenant_b)
    denied = [e for e in await _audit(admin, tenant_a, "decrypt") if e["outcome"] == "denied"]
    assert denied and denied[-1]["reason"] == "tenant_mismatch" and denied[-1]["actor_kind"] == "breakglass"

    ended = await sessions.end_session(app_pool, ops, tenant_a, kind="breakglass")
    assert ended["ended"] == [opened["session_id"]]
    with pytest.raises(Forbidden) as closed:
        await reads.overview(app_pool, ops, tenant_a)
    assert closed.value.code == "support_session_ended"


async def test_granted_agencies_and_my_sessions(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant_id, owner, _trip, _lead = await _inquiry(app_pool, keys)
    sup = staff()
    assert str(tenant_id) not in {a["tenant_id"] for a in (await sessions.granted_agencies(app_pool, sup))["items"]}
    await _grant(app_pool, owner, tenant_id, show_identity=True)
    [mine] = [a for a in (await sessions.granted_agencies(app_pool, sup))["items"] if a["tenant_id"] == str(tenant_id)]
    assert (mine["show_identity"], mine["in_use"], mine["mine"]) == (True, False, False)
    await sessions.start_support(app_pool, sup, tenant_id, ticket_ref="SUP-10")
    [mine] = [a for a in (await sessions.granted_agencies(app_pool, sup))["items"] if a["tenant_id"] == str(tenant_id)]
    assert (mine["in_use"], mine["mine"]) == (True, True)
    current = await sessions.my_sessions(app_pool, sup, tenant_id)
    assert [s["kind"] for s in current["sessions"]] == ["support"] and current["agency_status"] == "active"
    await sessions.end_session(app_pool, sup, tenant_id, kind="support")
    assert (await sessions.my_sessions(app_pool, sup, tenant_id))["sessions"] == []
