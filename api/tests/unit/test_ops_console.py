"""The L1 console's agency operations (AA-941, card CRM-13a): app.ops.console through the real platform_tx() on a
scripted connection, so the agency checks, the audit rows and the SQL arguments are the ones the code really writes.
The SQL, RLS and the crm-008 / crm-010 / crm-011 triggers run on PostgreSQL in tests/integration."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from app import agencies
from app.audit_export.registry import Agency
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.members.directory import MemoryDirectory
from app.members.mail import MemoryMailer
from app.ops import console, serializer
from app.tenancy import ProvisioningContext
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
OTHER = uuid.UUID("00000000-0000-4000-8000-0000000000a2")
JOB = uuid.UUID("00000000-0000-4000-8000-00000000j0b0".replace("j", "a"))
REQ = uuid.UUID("00000000-0000-4000-8000-00000000bbbb")
NOW = dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)
STAFF = "staff-1"
ISS = "https://cognito-idp.local.test/local_partner"


@contextmanager
def raises(cls: type[BaseException], code: str) -> Iterator[None]:
    with pytest.raises(cls) as exc:
        yield
    assert exc.value.code == code  # type: ignore[attr-defined]


def summary_row(tenant: uuid.UUID = TENANT, status: str = "active", **over: Any) -> dict[str, Any]:
    row = {
        "tenant_id": tenant,
        "display_name": "Lữ hành Sen Vàng",
        "status": status,
        "is_synthetic": True,
        "created_at": NOW,
        "has_owner": False,
        "holds_travellers": False,
        # AA-978 follow-up (crm.ops.v1 1.2): the lock source and the CIS feed singleton joined into the summary.
        "lock_source": None,
        "feed_failures": 0,
        "feed_last_ok_at": NOW - dt.timedelta(seconds=30),
        "feed_last_error_at": None,
        "read_at": NOW,
    }
    row.update(over)
    return row


def agency_conn(status: str | None = "active", **over: Any) -> ScriptedConn:
    """A scripted pool for one agency: platform_tx's status check, the summary, then per-test rules (``on``)."""
    conn = ScriptedConn()
    conn.on("SELECT status FROM crm.tenant WHERE tenant_id = $1 AND deleted_at IS NULL", status)
    conn.on("FROM crm.tenant t", summary_row(status=status or "active", **over))
    return conn


def audits(conn: ScriptedConn) -> list[dict[str, Any]]:
    keys = ("actor_kind", "actor_ref", "action", "outcome", "object_kind", "object_id", "fields", "reason")
    return [
        dict(zip(keys, args[:8], strict=True)) | {"object_ids": args[9]}
        for args in conn.sql("INSERT INTO crm.audit_event")
    ]


# ── platform_tx answers (the real one) ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_missing_agency_is_404_and_a_refused_status_409() -> None:
    with raises(NotFound, "tenant_not_found"):
        await console.agency_summary(agency_conn(None), TENANT, staff_sub=STAFF)  # type: ignore[arg-type]
    with raises(Conflict, "tenant_locked"):
        await console.invite_first_owner(
            agency_conn("locked"),  # type: ignore[arg-type]
            TENANT,
            staff_sub=STAFF,
            owner_email="owner@agency.example",
            ticket_ref="SUP-1",
            keys=object(),  # type: ignore[arg-type]
            mailer=MemoryMailer(),
            web_origin="https://partner.example.test",
            ttl_hours=168,
        )
    with raises(Conflict, "tenant_status"):
        await console.lock_agency(
            agency_conn("offboarding"),
            TENANT,
            staff_sub=STAFF,
            reason_code="test",
            ticket_ref="SUP-1",  # type: ignore[arg-type]
        )


# ── Reads ────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_list_agencies_pages_by_id_and_audits_each_agency(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = sorted(uuid.UUID(f"00000000-0000-4000-8000-00000000000{i}") for i in range(1, 6))
    gone = ids[2]

    async def registered(pool: Any) -> list[Agency]:
        return [Agency(tenant_id=i, exported_until=NOW, verified_until=None, last_seq=0) for i in reversed(ids)]

    monkeypatch.setattr(console.registry, "agencies", registered)
    conn = ScriptedConn()
    conn.on(
        "SELECT status FROM crm.tenant WHERE tenant_id = $1 AND deleted_at IS NULL",
        lambda tid: None if tid == gone else "locked",
    )
    conn.on("FROM crm.tenant t", lambda tid: summary_row(tid, status="locked", holds_travellers=True))
    page = await console.list_agencies(conn, staff_sub=STAFF, cursor=None, limit=3)  # type: ignore[arg-type]
    assert [i["tenant_id"] for i in page["items"]] == [str(ids[0]), str(ids[1])]  # the trashed one is skipped
    assert page["next_cursor"] == str(ids[2])
    assert page["items"][0] == {
        "tenant_id": str(ids[0]),
        "agency_name": "Lữ hành Sen Vàng",
        "state": "locked",
        "is_synthetic": True,
        "has_owner": False,
        "holds_travellers": True,
        "created_at": NOW.isoformat(),
        "lock_source": "aa_ops",  # locked before crm-013 (NULL): an AA lock
        "membership_feed_state": "ok",
        "membership_feed_lag_s": 30,
    }
    events = audits(conn)
    assert [(e["action"], e["reason"], e["actor_kind"]) for e in events] == [("ops_read", "list", "aa_ops")] * 2
    rest = await console.list_agencies(conn, staff_sub=STAFF, cursor=ids[2], limit=500)  # type: ignore[arg-type]
    assert [i["tenant_id"] for i in rest["items"]] == [str(ids[3]), str(ids[4])] and rest["next_cursor"] is None


@pytest.mark.asyncio
async def test_summary_usage_entitlements_requests_and_jobs_are_audited_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = agency_conn("offboarded", has_owner=True)
    conn.on("SELECT (SELECT count(*) FROM crm.member", 4)
    conn.on("FROM crm.tenant_entitlement WHERE deleted_at IS NULL", None)
    conn.on("SELECT ai_budget_usd FROM crm.tenant_settings", Decimal("5.00"))
    conn.on("count(DISTINCT actor_ref)", 2)
    conn.on("SELECT max(at) FROM crm.audit_event", NOW)
    conn.on("sum(size_bytes)", 2048)
    conn.on("sum(cost_usd_est)", Decimal("0.123456"))
    conn.on("payload->>'tenant_id' = $1 AND finished_at > now() - interval '24 hours'", 1)
    summary = await console.agency_summary(conn, TENANT, staff_sub=STAFF)  # type: ignore[arg-type]
    assert summary["state"] == "offboarded" and summary["has_owner"] is True
    usage = await console.agency_usage(conn, TENANT, staff_sub=STAFF)  # type: ignore[arg-type]
    assert usage["seats_used"] == 4 and usage["seats_allowed"] == 10 and usage["active_members_30d"] == 2
    assert usage["storage_bytes"] == 2048 and usage["inbound_mail_30d"] == 0 and usage["failed_jobs_24h"] == 1
    assert usage["ai_cost_usd_month"] == "0.1235" and usage["ai_budget_usd_month"] == "5.0000"
    assert usage["last_activity_at"] == NOW.isoformat()
    assert usage["period_start"].endswith("-01") and usage["period_end"] > usage["period_start"]
    conn.on("SELECT ai_budget_usd FROM crm.tenant_settings", None)
    assert (await console.agency_usage(conn, TENANT, staff_sub=STAFF))["ai_budget_usd_month"] == "2.0000"  # type: ignore[arg-type]
    assert await console.agency_entitlements(conn, TENANT, staff_sub=STAFF) == {  # type: ignore[arg-type]
        "plan": "free",
        "seats_allowed": 10,
        "features_allowed": [],
        "insights_opt_out": False,
    }
    conn.on(
        "FROM crm.tenant_entitlement WHERE deleted_at IS NULL",
        {"plan": "core", "member_limit": 25, "allowed_features": ["campaigns", "ai_assist"], "insights_opt_out": True},
    )
    assert (await console.agency_entitlements(conn, TENANT, staff_sub=STAFF))["features_allowed"] == [  # type: ignore[arg-type]
        "ai_assist",
        "campaigns",
    ]
    conn.on(
        "FROM crm.erasure_request WHERE scope = 'tenant' ORDER BY",
        [
            {
                "request_id": REQ,
                "tenant_id": TENANT,
                "purpose": "export_only",
                "status": "requested",
                "requested_by": "platform:staff-1",
                "owner_accepted_at": None,
                "export_downloaded_at": None,
                "created_at": NOW,
            }
        ],
    )
    requests = await console.agency_data_requests(conn, TENANT, staff_sub=STAFF)  # type: ignore[arg-type]
    assert requests == {
        "items": [
            {
                "request_id": str(REQ),
                "tenant_id": str(TENANT),
                "kind": "agency_export",
                "status": "awaiting_owner_confirmation",
                "raised_at": NOW.isoformat(),
            }
        ]
    }
    conn.on(
        "SELECT id::text AS job_id",
        [
            {"job_id": str(JOB), "kind": "crm_intake_process", "attempt": 5, "max_attempts": 5, "error": "x" * 200,
             "finished_at": NOW},
        ],
    )  # fmt: skip
    jobs = await console.agency_failed_jobs(conn, TENANT, staff_sub=STAFF)  # type: ignore[arg-type]
    assert jobs["items"][0]["error_class"] == "x" * 80 and "payload" not in jobs["items"][0]
    (args,) = conn.sql("SELECT id::text AS job_id")
    assert args[0] == str(TENANT) and args[1] == ["crm_intake_process", "crm_dispatch", "crm_offboard"]
    reasons = [e["reason"] for e in audits(conn)]
    assert reasons == ["summary", "usage", "usage", "entitlements", "entitlements", "data_requests", "jobs"]
    assert {e["action"] for e in audits(conn)} == {"ops_read"}


# ── Create agency ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_agency_created_and_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = agency_conn()
    conn.on("INSERT INTO crm.tenant (tenant_id", TENANT)
    ctx = ProvisioningContext(conn=conn, tenant_id=TENANT, actor="platform:staff-1")  # type: ignore[arg-type]

    @asynccontextmanager
    async def fake_provisioning(*_: Any, **__: Any):  # noqa: ANN202
        yield ctx

    monkeypatch.setattr(agencies, "provisioning_tx", fake_provisioning)
    kwargs: dict[str, Any] = dict(
        tenant_id=TENANT, agency_name=" Sen  Vàng ", is_synthetic=True, default_locale="vi-VN", environment="dev",
        ticket_ref="AA-941",
    )  # fmt: skip
    summary = await console.create_agency(conn, staff_sub=STAFF, **kwargs)  # type: ignore[arg-type]
    assert summary["tenant_id"] == str(TENANT)
    assert conn.sql("INSERT INTO crm.tenant (tenant_id")[0][1:] == ("Sen Vàng", "vi-VN", True)
    event = ctx.audit_events[0]
    assert (event.action, event.actor_kind, event.object_ids) == ("agency_create", "aa_ops", ("ticket:AA-941",))
    assert not conn.sql("INSERT INTO crm.member")  # no member is created here
    with raises(Invalid, "synthetic_required"):
        await console.create_agency(conn, staff_sub=STAFF, **{**kwargs, "is_synthetic": False})  # type: ignore[arg-type]
    with raises(Invalid, "invalid_locale"):
        await console.create_agency(conn, staff_sub=STAFF, **{**kwargs, "default_locale": "fr-FR"})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_create_agency_refuses_an_existing_id_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """AA-941 review: the L1 create on an existing id (any status) is 409 tenant_exists; it never renames the agency
    (no TENANT_UPDATE, no member write, no audit event), unlike the seeder's converge."""
    for status in ("active", "locked", "offboarding", "offboarded"):
        conn = agency_conn(status)
        conn.on("INSERT INTO crm.tenant (tenant_id", None)  # ON CONFLICT DO NOTHING: the id exists
        conn.on("SELECT display_name, default_locale, is_synthetic FROM crm.tenant", {
            "display_name": "Alpha Travel", "default_locale": "en-AU", "is_synthetic": True})  # fmt: skip
        ctx = ProvisioningContext(conn=conn, tenant_id=TENANT, actor="platform:staff-1")  # type: ignore[arg-type]

        @asynccontextmanager
        async def fake_provisioning(*_: Any, _ctx: ProvisioningContext = ctx, **__: Any):  # noqa: ANN202
            yield _ctx

        monkeypatch.setattr(agencies, "provisioning_tx", fake_provisioning)
        with raises(Conflict, "tenant_exists"):
            await console.create_agency(
                conn, staff_sub=STAFF, tenant_id=TENANT, agency_name="Totally Different Name", is_synthetic=True,
                default_locale="vi-VN", environment="dev", ticket_ref="SUP-9",
            )  # type: ignore[arg-type]  # fmt: skip
        assert not conn.sql("UPDATE crm.tenant SET display_name"), status
        assert not conn.sql("SELECT display_name, default_locale, is_synthetic FROM crm.tenant"), status
        assert ctx.audit_events == [] and not audits(conn), status


# ── Lock / unlock ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_lock_and_unlock() -> None:
    conn = agency_conn("active")
    conn.on("FOR UPDATE", {"status": "active", "lock_source": None})
    out = await console.lock_agency(conn, TENANT, staff_sub=STAFF, reason_code="payment", ticket_ref="SUP-77")  # type: ignore[arg-type]
    assert out["tenant_id"] == str(TENANT)
    assert conn.sql("UPDATE crm.tenant SET status = $2") == [(TENANT, "locked")]
    (event,) = audits(conn)
    assert (
        event["action"] == "tenant_lock" and event["reason"] == "payment" and event["object_ids"] == ["ticket:SUP-77"]
    )
    assert event["actor_ref"] == "platform:staff-1" and event["actor_kind"] == "aa_ops"
    # Already locked: a no-op without an audit event.
    locked = agency_conn("locked")
    locked.on("FOR UPDATE", {"status": "locked", "lock_source": "aa_ops"})
    await console.lock_agency(locked, TENANT, staff_sub=STAFF, reason_code="test", ticket_ref="SUP-1")  # type: ignore[arg-type]
    assert not locked.sql("UPDATE crm.tenant") and not audits(locked)
    await console.unlock_agency(locked, TENANT, staff_sub=STAFF, ticket_ref="SUP-2")  # type: ignore[arg-type]
    assert locked.sql("UPDATE crm.tenant SET status = $2") == [(TENANT, "active")]
    assert [(e["action"], e["reason"], e["object_ids"]) for e in audits(locked)] == [
        ("tenant_unlock", None, ["ticket:SUP-2"])
    ]
    active = agency_conn("active")
    active.on("FOR UPDATE", {"status": "active", "lock_source": None})
    await console.unlock_agency(active, TENANT, staff_sub=STAFF, ticket_ref="SUP-3")  # type: ignore[arg-type]
    assert not audits(active)
    with raises(Invalid, "invalid_reason"):
        await console.lock_agency(active, TENANT, staff_sub=STAFF, reason_code="bored", ticket_ref="SUP-1")  # type: ignore[arg-type]
    moved = agency_conn("active")
    moved.on("FOR UPDATE", {"status": "offboarding", "lock_source": None})
    with raises(Conflict, "tenant_status"):
        await console.lock_agency(moved, TENANT, staff_sub=STAFF, reason_code="test", ticket_ref="SUP-1")  # type: ignore[arg-type]


@pytest.mark.parametrize("source", ["cis_tenant", "cis_membership"])
@pytest.mark.asyncio
async def test_an_aa_lock_takes_over_a_cis_lock(source: str) -> None:
    """AA-978 review round 1: an L1 lock on an agency the CIS feed locked becomes AA's (lock_source aa_ops, audited),
    so a later CIS tenant.unlocked cannot lift it. The status does not change."""
    conn = agency_conn("locked")
    conn.on("FOR UPDATE", {"status": "locked", "lock_source": source})
    await console.lock_agency(conn, TENANT, staff_sub=STAFF, reason_code="abuse", ticket_ref="SUP-9")  # type: ignore[arg-type]
    assert conn.sql("UPDATE crm.tenant SET lock_source = 'aa_ops'") == [(TENANT,)]
    assert not conn.sql("UPDATE crm.tenant SET status = $2")
    assert [(e["action"], e["reason"], e["fields"], e["object_ids"]) for e in audits(conn)] == [
        ("tenant_lock", "abuse", ["lock_source"], ["ticket:SUP-9"])
    ]


# ── First owner ──────────────────────────────────────────────────────────────────────────────────
def _invite_conn(**flags: Any) -> ScriptedConn:
    conn = agency_conn()
    conn.on("WHERE role = 'owner' AND status IN ('active', 'invited')", flags.get("owner"))
    conn.on("FROM crm.member_invitation WHERE role = 'owner'", flags.get("pending"))
    conn.on("SELECT EXISTS (SELECT 1 FROM crm.traveler) OR EXISTS", flags.get("travellers", False))
    conn.on("SELECT (SELECT count(*) FROM crm.member", flags.get("seats", 0))
    conn.on("INSERT INTO crm.member_invitation", NOW + dt.timedelta(days=7))
    conn.on("SELECT display_name FROM crm.tenant", "Sen Vàng")
    return conn


async def _invite(conn: ScriptedConn, mailer: MemoryMailer, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(console, "Vault", lambda keys, ctx: FakeVault())
    return await console.invite_first_owner(
        conn,  # type: ignore[arg-type]
        TENANT,
        staff_sub=STAFF,
        owner_email=" Owner@Agency.example ",
        ticket_ref="SUP-9",
        keys=object(),  # type: ignore[arg-type]
        mailer=mailer,
        web_origin="https://partner.example.test",
        ttl_hours=168,
    )


@pytest.mark.asyncio
async def test_first_owner_invite_success_carries_no_address(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, mailer = _invite_conn(), MemoryMailer()
    out = await _invite(conn, mailer, monkeypatch)
    assert set(out) == {"tenant_id", "invitation_id", "status", "expires_at"} and out["status"] == "pending"
    assert "agency.example" not in str(out) and "***" not in str(out)
    assert len(mailer.sent) == 1 and mailer.sent[0].to == "owner@agency.example" and mailer.sent[0].kind == "invitation"
    (insert,) = conn.sql("INSERT INTO crm.member_invitation")
    assert insert[1] == "owner" and insert[2] == b"enc:member_invitation.email_enc:owner@agency.example"
    assert conn.sql("pg_advisory_xact_lock") == [(str(TENANT),)]
    (event,) = audits(conn)
    assert (event["action"], event["reason"], event["object_ids"]) == ("invite", "role_owner", ["ticket:SUP-9"])
    assert event["actor_kind"] == "aa_ops"


@pytest.mark.parametrize(
    ("flags", "code"),
    [
        ({"owner": 1}, "owner_exists"),
        ({"pending": 1}, "owner_invite_pending"),
        ({"travellers": True}, "has_travellers"),
        ({"seats": 10}, "member_limit_reached"),
    ],
)
@pytest.mark.asyncio
async def test_first_owner_invite_refusals(flags: dict[str, Any], code: str, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, mailer = _invite_conn(**flags), MemoryMailer()
    with raises(Conflict, code):
        await _invite(conn, mailer, monkeypatch)
    assert not mailer.sent and not conn.sql("INSERT INTO crm.member_invitation")


@pytest.mark.asyncio
async def test_first_owner_invite_refuses_a_bad_address_before_any_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _invite_conn()
    with raises(Invalid, "invalid_email"):
        await console.invite_first_owner(
            conn,  # type: ignore[arg-type]
            TENANT,
            staff_sub=STAFF,
            owner_email="not-an-address",
            ticket_ref="SUP-9",
            keys=object(),  # type: ignore[arg-type]
            mailer=MemoryMailer(),
            web_origin="https://x.test",
            ttl_hours=1,
        )
    assert conn.calls == []


# ── Entitlements, insights ───────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_allow_features_upserts_and_never_touches_tenant_settings() -> None:
    conn = agency_conn("locked")
    conn.on(
        "INSERT INTO crm.tenant_entitlement (allowed_features)",
        {"plan": "free", "member_limit": 10, "allowed_features": ["ai_assist", "campaigns"], "insights_opt_out": False},
    )
    out = await console.allow_features(  # type: ignore[arg-type]
        conn, TENANT, staff_sub=STAFF, features=["campaigns", "ai_assist"], ticket_ref="SUP-4"
    )
    assert out["features_allowed"] == ["ai_assist", "campaigns"]
    assert conn.sql("INSERT INTO crm.tenant_entitlement (allowed_features)") == [(["ai_assist", "campaigns"],)]
    assert not [c for c, _ in conn.calls if "tenant_settings" in c]
    (event,) = audits(conn)
    assert event["action"] == "entitlements_allow" and event["fields"] == ["allowed_features"]
    for bad in (["ai"], ["campaigns", "campaigns"]):
        with raises(Invalid, "invalid_feature"):
            await console.allow_features(conn, TENANT, staff_sub=STAFF, features=bad, ticket_ref="SUP-4")  # type: ignore[arg-type]


def test_the_console_never_writes_tenant_settings() -> None:
    """AA allows, the owner switches on: no L1 path writes the agency's own switches."""
    source = Path(console.__file__).read_text(encoding="utf-8")
    assert "UPDATE crm.tenant_settings" not in source and "INTO crm.tenant_settings" not in source


@pytest.mark.asyncio
async def test_insights_opt_out_upsert_and_audit() -> None:
    conn = agency_conn()
    conn.on("INSERT INTO crm.tenant_entitlement (insights_opt_out)", lambda value: value)
    assert await console.set_insights_opt_out(
        conn, TENANT, staff_sub=STAFF, opt_out=True, owner_request_ref="OWN-1"
    ) == {  # type: ignore[arg-type]
        "opt_out": True
    }
    assert await console.set_insights_opt_out(
        conn, TENANT, staff_sub=STAFF, opt_out=False, owner_request_ref="OWN-2"
    ) == {  # type: ignore[arg-type]
        "opt_out": False
    }
    assert [(e["action"], e["reason"], e["object_ids"]) for e in audits(conn)] == [
        ("insights_opt_out", "opt_out", ["ticket:OWN-1"]),
        ("insights_opt_out", "opt_in", ["ticket:OWN-2"]),
    ]


# ── Data requests ────────────────────────────────────────────────────────────────────────────────
def _request_row(purpose: str) -> dict[str, Any]:
    return {
        "request_id": REQ,
        "tenant_id": TENANT,
        "purpose": purpose,
        "status": "requested",
        "requested_by": "platform:staff-1",
        "owner_accepted_at": None,
        "export_downloaded_at": None,
        "created_at": NOW,
    }


@pytest.mark.parametrize(("kind", "purpose"), [("agency_export", "export_only"), ("agency_deletion", "offboard")])
@pytest.mark.asyncio
async def test_raise_data_request_queues_no_job_and_mails_the_owners(kind: str, purpose: str) -> None:
    directory = MemoryDirectory(ISS)
    sub = directory.add("owner@agency.example")
    mailer = MemoryMailer()
    conn = agency_conn()
    conn.on("INSERT INTO crm.erasure_request (scope, subject_id, purpose)", REQ)
    conn.on("FROM crm.erasure_request WHERE request_id = $1", _request_row(purpose))
    conn.on("WHERE role = 'owner' AND status = 'active'", [{"cognito_iss": ISS, "cognito_sub": sub}])
    conn.on("SELECT display_name FROM crm.tenant", "Sen Vàng")
    out = await console.raise_data_request(  # type: ignore[arg-type]
        conn, TENANT, staff_sub=STAFF, kind=kind, ticket_ref="SUP-5", directory=directory, mailer=mailer,
        web_origin="https://partner.example.test",
    )  # fmt: skip
    assert out == {
        "request_id": str(REQ),
        "tenant_id": str(TENANT),
        "kind": kind,
        "status": "awaiting_owner_confirmation",
        "raised_at": NOW.isoformat(),
        "owners_notified": 1,
    }
    assert conn.sql("INSERT INTO crm.erasure_request (scope, subject_id, purpose)") == [(TENANT, purpose)]
    assert not conn.sql("INSERT INTO crm.job")
    (event,) = audits(conn)
    assert (event["action"], event["reason"], event["object_ids"]) == ("data_request_raise", kind, ["ticket:SUP-5"])
    assert mailer.sent[0].kind == "data_request_notice" and "settings/data" in mailer.sent[0].text


@pytest.mark.asyncio
async def test_raise_data_request_refusals() -> None:
    kwargs: dict[str, Any] = dict(
        staff_sub=STAFF, ticket_ref="SUP-5", directory=None, mailer=None, web_origin="https://x.test"
    )
    open_conn = agency_conn()
    open_conn.on("status IN ('requested', 'export_ready', 'erasing', 'failed')", {"request_id": REQ})
    with raises(Conflict, "offboarding_open"):
        await console.raise_data_request(open_conn, TENANT, kind="agency_export", **kwargs)  # type: ignore[arg-type]
    held = agency_conn()
    held.on("crm.legal_hold_blocks", True)
    with raises(Conflict, "legal_hold"):
        await console.raise_data_request(held, TENANT, kind="agency_deletion", **kwargs)  # type: ignore[arg-type]
    held.on("INSERT INTO crm.erasure_request (scope, subject_id, purpose)", REQ)
    held.on("FROM crm.erasure_request WHERE request_id = $1", _request_row("export_only"))
    # An export under a hold is fine (nothing is deleted).
    assert (await console.raise_data_request(held, TENANT, kind="agency_export", **kwargs))["kind"] == "agency_export"  # type: ignore[arg-type]
    race = agency_conn()
    race.on("INSERT INTO crm.erasure_request (scope, subject_id, purpose)", asyncpg.UniqueViolationError("dup"))
    with raises(Conflict, "offboarding_open"):
        await console.raise_data_request(race, TENANT, kind="agency_export", **kwargs)  # type: ignore[arg-type]
    with raises(Invalid, "invalid_kind"):
        await console.raise_data_request(race, TENANT, kind="agency_purge", **kwargs)  # type: ignore[arg-type]


# ── Retry ────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_retry_only_a_failed_job_of_this_agency() -> None:
    conn = agency_conn("offboarding")
    conn.on("UPDATE crm.job SET status = 'queued'", str(JOB))
    out = await console.retry_agency_job(conn, TENANT, JOB, staff_sub=STAFF, ticket_ref="SUP-6")  # type: ignore[arg-type]
    assert out == {"job_id": str(JOB), "status": "queued"}
    (args,) = conn.sql("UPDATE crm.job SET status = 'queued'")
    assert args == (str(JOB), str(TENANT), ["crm_intake_process", "crm_dispatch", "crm_offboard"])
    (event,) = audits(conn)
    assert (event["action"], event["object_kind"], event["object_id"]) == ("job_retry", "job", str(JOB))
    miss = agency_conn()
    with raises(NotFound, "job_not_found"):
        await console.retry_agency_job(miss, TENANT, JOB, staff_sub=STAFF, ticket_ref="SUP-6")  # type: ignore[arg-type]
    assert not audits(miss)
    with raises(Conflict, "tenant_locked"):
        await console.retry_agency_job(agency_conn("locked"), TENANT, JOB, staff_sub=STAFF, ticket_ref="SUP-6")  # type: ignore[arg-type]


# ── S263: withdraw the first-owner invitation; delete a test agency ──────────────────────────────
INV_A = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
INV_B = uuid.UUID("00000000-0000-4000-8000-0000000000c2")


@pytest.mark.asyncio
async def test_withdraw_closes_every_live_first_owner_invitation_with_the_ticket() -> None:
    """Found live on Dev: a claimed-but-not-accepted owner invitation blocked "Invite first owner" (409
    owner_invite_pending) for its 7 days. AA withdraws it; a locked agency's too."""
    for status in ("active", "locked"):
        conn = agency_conn(status)
        conn.on("FROM crm.member_invitation WHERE role = 'owner' AND invited_by LIKE 'platform:%'",
                [{"invitation_id": INV_B}, {"invitation_id": INV_A}])  # fmt: skip
        out = await console.withdraw_owner_invitation(conn, TENANT, staff_sub=STAFF, ticket_ref="SUP-7")  # type: ignore[arg-type]
        assert out == {"tenant_id": str(TENANT), "invitation_id": str(INV_B), "status": "revoked"}
        assert conn.sql("UPDATE crm.member_invitation SET status = 'revoked'") == [(INV_B,), (INV_A,)]
        events = audits(conn)
        assert [(e["action"], e["object_id"], e["object_ids"], e["actor_kind"]) for e in events] == [
            ("owner_invite_withdraw", str(INV_B), ["ticket:SUP-7"], "aa_ops"),
            ("owner_invite_withdraw", str(INV_A), ["ticket:SUP-7"], "aa_ops"),
        ]
    # Only AA's own owner invitations, live ones: the query says so.
    (sql,) = [flat for flat, _ in conn.calls if "invited_by LIKE 'platform:%'" in flat]
    assert "status IN ('pending', 'claimed')" in sql and "role = 'owner'" in sql and "FOR UPDATE" in sql


@pytest.mark.asyncio
async def test_withdraw_without_a_live_invitation_is_404_and_writes_nothing() -> None:
    conn = agency_conn()
    with raises(NotFound, "owner_invite_not_found"):
        await console.withdraw_owner_invitation(conn, TENANT, staff_sub=STAFF, ticket_ref="SUP-7")  # type: ignore[arg-type]
    assert not conn.sql("SET status = 'revoked'") and not audits(conn)
    with raises(Conflict, "tenant_status"):
        await console.withdraw_owner_invitation(agency_conn("offboarded"), TENANT, staff_sub=STAFF, ticket_ref="SUP-7")  # type: ignore[arg-type]


class _Keys:
    def __init__(self) -> None:
        self.destroyed: list[uuid.UUID] = []

    async def destroy(self, ctx: Any) -> int:
        ctx.audit("key_destroy", "allowed", "tenant_key", reason="crypto_shred")
        self.destroyed.append(ctx.tenant_id)
        return 2


def _delete_conn(status: str = "active", **over: Any) -> ScriptedConn:
    conn = agency_conn(status)
    row = {"status": status, "is_synthetic": True, "display_name": "Lữ hành Sen Vàng"} | over.pop("row", {})
    conn.on("SELECT status, is_synthetic, display_name FROM crm.tenant", row)
    conn.on("UPDATE crm.tenant SET deleted_at", NOW + dt.timedelta(days=30))
    conn.on("SELECT invitation_id FROM crm.member_invitation WHERE status IN", [{"invitation_id": INV_A}])
    for fragment, answer in over.items():
        conn.on(fragment, answer)
    return conn


async def _delete(conn: ScriptedConn, keys: _Keys, name: str = "Lữ hành Sen Vàng") -> dict[str, Any]:
    return await console.delete_test_agency(
        conn,  # type: ignore[arg-type]
        TENANT,
        staff_sub=STAFF,
        agency_name=name,
        ticket_ref="SUP-8",
        keys=keys,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("status", ["active", "locked", "offboarded"])
@pytest.mark.asyncio
async def test_delete_test_agency_revokes_shreds_and_trashes_in_that_order(status: str) -> None:
    conn, keys = _delete_conn(status), _Keys()
    assert await _delete(conn, keys) == {"tenant_id": str(TENANT), "status": "deleted"}
    assert keys.destroyed == [TENANT]
    assert conn.sql("SET status = 'revoked'") == [(INV_A,)]
    assert conn.sql("UPDATE crm.tenant SET deleted_at") == [(TENANT, f"platform:{STAFF}", 30)]
    order = [flat.split()[0] + " " + flat.split()[1] for flat, _ in conn.calls if flat.startswith(("UPDATE", "INSERT"))]
    assert order.index("UPDATE crm.member_invitation") < order.index("UPDATE crm.tenant")
    events = [(e["action"], e["reason"], e["object_ids"]) for e in audits(conn)]
    assert events == [
        ("invite_revoke", "agency_delete", []),
        ("key_destroy", "crypto_shred", []),
        ("agency_delete", "synthetic_test_agency", ["ticket:SUP-8"]),
    ]


@pytest.mark.parametrize(
    ("over", "cls", "code"),
    [
        ({"row": {"is_synthetic": False}}, Forbidden, "not_synthetic"),
        ({"FROM crm.member WHERE status IN ('active', 'invited')": 1}, Conflict, "has_members"),
        ({"SELECT EXISTS (SELECT 1 FROM crm.traveler) OR EXISTS": True}, Conflict, "has_travellers"),
        ({"crm.legal_hold_blocks('tenant', NULL)": True}, Conflict, "legal_hold"),
        ({"row": {"status": "offboarding"}}, Conflict, "tenant_status"),
    ],
)
@pytest.mark.asyncio
async def test_delete_test_agency_refusals_change_nothing(over: dict[str, Any], cls: type, code: str) -> None:
    conn, keys = _delete_conn(**over), _Keys()
    with raises(cls, code):
        await _delete(conn, keys)
    assert keys.destroyed == [] and not conn.sql("UPDATE crm.tenant SET deleted_at")
    assert not conn.sql("SET status = 'revoked'")
    denied = [(e["action"], e["outcome"], e["reason"]) for e in audits(conn)]
    # A real agency's refusal is on record (an attempt to delete client data); the others write nothing.
    assert denied == ([("agency_delete", "denied", "not_synthetic")] if code == "not_synthetic" else [])


@pytest.mark.asyncio
async def test_delete_test_agency_needs_the_exact_name_and_a_deletable_status() -> None:
    conn, keys = _delete_conn(), _Keys()
    for typed in ("lữ hành sen vàng", "Lữ hành Sen Vàng ", "Sen Vàng"):
        with raises(Invalid, "agency_name_mismatch"):
            await _delete(conn, keys, typed)
    assert keys.destroyed == []
    with raises(Conflict, "tenant_status"):
        await _delete(_delete_conn("offboarding"), keys)
    gone = _delete_conn()
    gone.on("SELECT status, is_synthetic, display_name FROM crm.tenant", None)
    with raises(NotFound, "tenant_not_found"):
        await _delete(gone, keys)
    raced = _delete_conn()
    raced.on("UPDATE crm.tenant SET deleted_at", None)
    with raises(NotFound, "tenant_not_found"):
        await _delete(raced, keys)


def test_every_write_needs_and_every_read_skips_a_ticket() -> None:
    """The console's write functions take the ticket (or the owner's request ref); its reads do not."""
    import inspect

    writes = {
        "create_agency": "ticket_ref",
        "lock_agency": "ticket_ref",
        "unlock_agency": "ticket_ref",
        "invite_first_owner": "ticket_ref",
        "allow_features": "ticket_ref",
        "set_insights_opt_out": "owner_request_ref",
        "raise_data_request": "ticket_ref",
        "retry_agency_job": "ticket_ref",
        "withdraw_owner_invitation": "ticket_ref",
        "delete_test_agency": "ticket_ref",
    }
    for name, param in writes.items():
        assert param in inspect.signature(getattr(console, name)).parameters, name
    for name in ("list_agencies", "agency_summary", "agency_usage", "agency_entitlements", "agency_data_requests",
                 "agency_failed_jobs"):  # fmt: skip
        assert "ticket_ref" not in inspect.signature(getattr(console, name)).parameters, name


@pytest.mark.parametrize(
    ("over", "lock_source", "feed", "lag"),
    [
        ({"status": "active"}, None, "ok", 30),
        ({"status": "locked", "lock_source": "cis_membership"}, "cis_membership", "ok", 30),
        (
            {"status": "locked", "lock_source": "cis_tenant", "feed_failures": 3, "feed_last_error_at": NOW},
            "cis_tenant",
            "failing",
            30,
        ),  # fmt: skip
        ({"feed_last_ok_at": None}, None, "pending", None),
        ({"feed_last_ok_at": NOW - dt.timedelta(minutes=11)}, None, "stale", 660),
        ({"feed_failures": None, "feed_last_ok_at": None}, None, "unknown", None),  # no crm-013 row
    ],
)
def test_the_summary_carries_the_lock_source_and_the_feed(
    over: dict[str, Any], lock_source: str | None, feed: str, lag: int | None
) -> None:
    """AA-978 follow-up (crm.ops.v1 1.2): who locked the agency, and the CIS membership feed's word and lag."""
    status = over.pop("status", "active")
    got = console._summary(summary_row(status=status, **over))
    assert (got["lock_source"], got["membership_feed_state"], got["membership_feed_lag_s"]) == (lock_source, feed, lag)
    assert all(serializer.key_allowed(k) for k in got)


# ── crm.ops.v1 1.4 (AA-992): platform-wide counts for CIS /admin/data-coverage ─────────────────────────────────────
@pytest.mark.asyncio
async def test_platform_coverage_counts_states_and_members_without_an_agency_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [uuid.UUID(f"00000000-0000-4000-8000-00000000000{i}") for i in range(1, 5)]
    status = {ids[0]: "active", ids[1]: "locked", ids[2]: "active"}  # ids[3]: registered but trashed

    async def registered(pool: Any) -> list[Agency]:
        return [Agency(tenant_id=i, exported_until=NOW, verified_until=None, last_seq=0) for i in ids]

    monkeypatch.setattr(console.registry, "agencies", registered)
    conn = ScriptedConn()
    conn.on("SELECT status FROM crm.tenant WHERE tenant_id = $1 AND deleted_at IS NULL", lambda tid: status.get(tid))
    conn.on("SELECT is_synthetic FROM crm.tenant WHERE tenant_id = $1", lambda tid: tid == ids[2])
    conn.on(
        "FROM crm.member WHERE deleted_at IS NULL GROUP BY status",
        [{"status": "active", "n": 2}, {"status": "invited", "n": 1}, {"status": "disabled", "n": 1}],
    )
    conn.on(
        "FROM crm.aa_catalog_state s",
        {"served": 892, "paused": 1, "retired": 2, "last_full_pass_at": NOW, "last_ok_at": NOW, "failures": 0,
         "last_error_class": None},
    )  # fmt: skip
    out = await console.platform_coverage(conn, staff_sub=STAFF)  # type: ignore[arg-type]
    assert out["agencies"] == {"active": 2, "locked": 1, "offboarding": 0, "offboarded": 0, "synthetic": 1}
    assert out["members"] == {"active": 6, "invited": 3, "disabled": 3}  # three agencies, the trashed one skipped
    assert out["catalog_cache"] == {
        "served": 892,
        "paused": 1,
        "retired": 2,
        "last_full_pass_at": NOW.isoformat(),
        "last_ok_at": NOW.isoformat(),
        "failures": 0,
        "last_error_class": None,
    }
    assert audits(conn) == []  # platform-wide counts: no ops_read event in any agency's audit
    assert serializer.bad_keys(out) == set()
