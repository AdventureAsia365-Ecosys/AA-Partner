"""app.cis.entitlement_feed without a database (AA-1028 R1 slice 2): the snapshot contract, the projection rules, the
lock decision, the run (configuration, backoff, HTTP error classes, the cursor, one snapshot per tenant, the hourly
reconcile, refusals), the 2-minute schedule and the shared feature gate. A ScriptedConn stands in for the pool (it
hands itself out as a connection), so the projection's statements run through the real entitlement_feed_tx(). The
same rules on PostgreSQL: tests/integration/test_crm043_entitlement_feed.py."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app import entitlements
from app.cis import entitlement_feed as feed
from app.cis import feed_state
from app.cis.http import HttpError, HttpResponse
from app.cis.m2m import M2mCredentials, TokenError, TokenProvider
from app.cis.membership_feed import FeedConfig
from app.errors import Forbidden
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import tenant_ctx

NOW = dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.UTC)
BASE = "https://api-cis.example.invalid"
A = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
B = uuid.UUID("00000000-0000-4000-8000-0000000000b2")
JOB = Job(id="00000000-0000-4000-8000-000000000001", kind="crm_entitlement_pull", payload={}, attempt=1, max_attempts=3)


def account(tenant: uuid.UUID = A, *, in_service: bool = True, crm: tuple[str, str] | None = ("crm.core", "active"),
            ents: dict[str, str | None] | None = None) -> dict[str, Any]:  # fmt: skip
    subs = [{"product": "content", "plan_key": "content.growth", "status": "active", "trial_ends_at": None,
             "seats": None}]  # fmt: skip
    if crm is not None:
        subs.append({"product": "crm", "plan_key": crm[0], "status": crm[1], "trial_ends_at": None, "seats": None})
    if ents is None:
        ents = {"crm.core": None, "crm.ai_assist": None, "crm.seats": "10.00", "content.rewrite": "200.00"}
    return {
        "tenant_id": str(tenant), "account_status": "active" if in_service else "suspended", "suspend_reason": None,
        "in_service": in_service, "kind": "agency", "is_pilot": False, "data_region": "us", "time_zone": "UTC",
        "subscriptions": subs,
        "entitlements": [{"feature_key": k, "product": k.split(".")[0], "kind": "flag", "unit": None, "limit": v,
                          "source": "plan", "expires_at": None} for k, v in ents.items()],
        "as_of": "2026-11-03T12:00:00Z",
    }  # fmt: skip


def ok(body: Any) -> HttpResponse:
    return HttpResponse(200, json.dumps(body).encode())


def page(tenants: list[uuid.UUID], next_cursor: str) -> HttpResponse:
    items = [{"event_id": str(uuid.uuid4()), "cursor": "c", "type": "entitlement.changed", "occurred_at": "x",
              "tenant_id": str(t), "change": {}} for t in tenants]  # fmt: skip
    return ok({"items": items, "next_cursor": next_cursor})


class FakeTokens(TokenProvider):
    def __init__(self, error: str | None = None) -> None:
        super().__init__(M2mCredentials("abc123", "x", "https://m2m-auth.example.invalid/oauth2/token"))
        self.error = error
        self.invalidated: list[str] = []

    async def token(self, scope: str) -> str:
        if self.error:
            raise TokenError(self.error)
        return "tok"

    def invalidate(self, scope: str) -> None:
        self.invalidated.append(scope)


class Transport:
    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    async def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout_s: float
    ) -> HttpResponse:
        assert method == "GET" and body is None and headers["Authorization"] == "Bearer tok"
        self.urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def pool(*, statuses: dict[uuid.UUID, str | None] | None = None, cursor: str | None = None, failures: int = 0,
         last_error_at: dt.datetime | None = None, reconciled_at: dt.datetime | None = NOW,
         entitlement: dict[str, Any] | None = None, lock: dict[str, Any] | None = None,
         agencies: list[uuid.UUID] = ()) -> ScriptedConn:  # fmt: skip
    statuses = statuses or {}
    conn = ScriptedConn()
    conn.on("FROM crm.entitlement_feed_state", {"feed_cursor": cursor, "failures": failures,
                                                "last_error_at": last_error_at, "last_ok_at": None})  # fmt: skip
    conn.on("SET feed_cursor", 1)
    conn.on("RETURNING failures", failures + 1)
    conn.on("SELECT last_reconcile_at", reconciled_at)
    conn.on("FROM crm.audit_export_agencies()", [
        {"tenant_id": t, "exported_until": NOW, "verified_until": None, "last_seq": 0} for t in agencies])  # fmt: skip
    # entitlement_feed_tx: the agency's status (the GUC set just before names the tenant)
    state = {"tenant": None}
    conn.on("set_config('app.tenant_id'", lambda tid: state.update(tenant=uuid.UUID(tid)))
    conn.on("AND deleted_at IS NULL", lambda *_: statuses.get(state["tenant"]))
    conn.on("FROM crm.tenant_entitlement WHERE deleted_at IS NULL", entitlement)
    conn.on("FOR UPDATE", lock or {"status": "active", "lock_source": None})
    return conn


def make(transport: Transport, tokens: FakeTokens | None = None) -> feed.EntitlementFeed:
    return feed.EntitlementFeed(FeedConfig(base_url=BASE, tokens=tokens or FakeTokens(), transport=transport),
                                now=lambda: NOW)  # fmt: skip


# ── the contract and the pure rules ──────────────────────────────────────────────────────────────────────────────


def test_parse_account_reads_what_the_projection_needs() -> None:
    parsed = feed.parse_account(account() | {"unknown_minor_field": 1}, A)
    assert parsed["in_service"] is True and parsed["subscriptions"]["crm"]["plan_key"] == "crm.core"
    assert parsed["entitlements"]["crm.seats"] == Decimal("10.00") and parsed["entitlements"]["crm.core"] is None


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ([], "account_shape"),
        (account(B), "account_tenant"),
        (account() | {"in_service": "yes"}, "account_in_service"),
        (account() | {"entitlements": {}}, "account_lists"),
        (account() | {"subscriptions": [{"product": "crm"}]}, "subscription_shape"),
        (account() | {"subscriptions": [{"product": "crm", "status": "active", "plan_key": 3}]}, "subscription_shape"),
        (account() | {"entitlements": [{"limit": None}]}, "entitlement_shape"),
        (account() | {"entitlements": [{"feature_key": "crm.seats", "limit": 10}]}, "limit_not_decimal"),
        (account() | {"entitlements": [{"feature_key": "crm.seats", "limit": "ten"}]}, "limit_not_decimal"),
        (account() | {"entitlements": [{"feature_key": "crm.seats", "limit": "-1"}]}, "limit_not_decimal"),
        (account() | {"entitlements": [{"feature_key": "crm.seats", "limit": "NaN"}]}, "limit_not_decimal"),
    ],
)
def test_parse_account_refuses(body: Any, code: str) -> None:
    with pytest.raises(feed.AccountError, match=code):
        feed.parse_account(body, A)


def _proj(**kw: Any) -> feed.Projection:
    return feed.projection(feed.parse_account(account(**kw), A))


def test_projection_rules() -> None:
    assert _proj() == feed.Projection("core", 10, ("ai_assist",), True)
    pro = {f"crm.{c}": None for c in entitlements.FEATURES} | {"crm.seats": "600.00"}
    assert _proj(crm=("crm.pro", "trialing"), ents=pro) == feed.Projection("pro", 500, entitlements.FEATURES, True)
    assert _proj(crm=("crm.enterprise", "past_due")).plan == "core"  # another live plan
    assert _proj(crm=("crm.pro", "canceled"), ents={}) == feed.Projection("free", None, (), True)
    assert _proj(crm=None, ents={"content.rewrite": "50.00"}) == feed.Projection("free", None, (), True)
    assert _proj(in_service=False).in_service is False
    assert _proj(ents={"crm.seats": "0.00"}).member_limit == 1


@pytest.mark.parametrize(
    ("in_service", "status", "source", "change"),
    [
        (False, "active", None, "lock"),
        (False, "locked", "aa_ops", None),
        (True, "locked", "cis_tenant", "unlock"),
        (True, "locked", "aa_ops", None),
        (True, "locked", "cis_membership", None),
        (True, "active", None, None),
    ],
)
def test_status_change(in_service: bool, status: str, source: str | None, change: str | None) -> None:
    assert feed.status_change(in_service, status, source) == change


def test_urls_and_page_tenants() -> None:
    assert feed.events_url(BASE, None) == f"{BASE}/tenant/v1/account-events?limit=200"
    assert feed.events_url(BASE, "c 1").endswith("limit=200&after=c+1")
    assert feed.account_url(BASE, A) == f"{BASE}/tenant/v1/tenants/{A}/account"
    items = [{"tenant_id": str(A)}, {"tenant_id": str(B)}, {"tenant_id": str(A)}, {"tenant_id": "x"}, "junk"]
    assert feed.page_tenants(items) == [A, B]


# ── apply (the real entitlement_feed_tx on a scripted connection) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_writes_a_new_projection_and_audits() -> None:
    conn = pool(statuses={A: "active"})
    counts = feed.RunCounts()
    assert await feed.apply(conn, A, feed.Projection("core", 5, ("campaigns", "ai_assist"), True), counts) == "applied"  # type: ignore[arg-type]
    assert conn.sql("INSERT INTO crm.tenant_entitlement") == [("core", 5, ["ai_assist", "campaigns"])]
    audit = [a for a in conn.sql("INSERT INTO crm.audit_event")]
    assert audit and audit[0][2] == "entitlements_projected" and audit[0][7] == "cis_account"
    assert counts.applied == 1


@pytest.mark.asyncio
async def test_apply_keeps_member_limit_and_writes_nothing_when_unchanged() -> None:
    row = {"plan": "core", "member_limit": 7, "allowed_features": ["ai_assist"], "projected_at": NOW}
    conn = pool(statuses={A: "locked"}, entitlement=row, lock={"status": "locked", "lock_source": "aa_ops"})
    counts = feed.RunCounts()
    assert await feed.apply(conn, A, feed.Projection("core", None, ("ai_assist",), True), counts) == "unchanged"  # type: ignore[arg-type]
    assert conn.sql("INSERT INTO crm.tenant_entitlement") == [] and conn.sql("UPDATE crm.tenant SET") == []
    # never projected before (projected_at NULL): written, with the agency's own member limit
    conn = pool(statuses={A: "active"}, entitlement=row | {"projected_at": None})
    await feed.apply(conn, A, feed.Projection("core", None, ("ai_assist",), True), counts)  # type: ignore[arg-type]
    assert conn.sql("INSERT INTO crm.tenant_entitlement") == [("core", 7, ["ai_assist"])]


@pytest.mark.asyncio
async def test_apply_locks_and_unlocks_on_in_service() -> None:
    counts = feed.RunCounts()
    conn = pool(statuses={A: "active"})
    await feed.apply(conn, A, feed.Projection("free", None, (), False), counts)  # type: ignore[arg-type]
    assert conn.sql("SET status = 'locked', lock_source = 'cis_tenant'") == [(A,)]
    conn = pool(statuses={A: "locked"}, lock={"status": "locked", "lock_source": "cis_tenant"})
    await feed.apply(conn, A, feed.Projection("free", None, (), True), counts)  # type: ignore[arg-type]
    assert conn.sql("SET status = 'active'") == [(A,)]
    assert (counts.locked, counts.unlocked, counts.applied) == (1, 1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "offboarding", "offboarded"])
async def test_apply_leaves_unknown_and_offboarding_agencies_alone(status: str | None) -> None:
    conn = pool(statuses={A: status})
    counts = feed.RunCounts()
    assert await feed.apply(conn, A, feed.Projection("pro", 9, (), True), counts) == "ignored"  # type: ignore[arg-type]
    assert conn.sql("INSERT INTO crm.tenant_entitlement") == [] and counts.ignored == 1
    assert await feed.held(conn, A) is False  # type: ignore[arg-type]


def _pg_error(sqlstate: str) -> asyncpg.PostgresError:
    exc = asyncpg.PostgresError("refused")
    exc.sqlstate = sqlstate  # type: ignore[attr-defined]
    return exc


@pytest.mark.asyncio
async def test_apply_counts_a_refusal_and_raises_a_systemic_error() -> None:
    conn = pool(statuses={A: "active"}).on("INSERT INTO crm.tenant_entitlement", _pg_error("CRM04"))
    counts = feed.RunCounts()
    assert await feed.apply(conn, A, feed.Projection("core", 5, (), True), counts) == "rejected"  # type: ignore[arg-type]
    conn = pool(statuses={A: "active"}).on("INSERT INTO crm.tenant_entitlement", _pg_error("40P01"))
    with pytest.raises(asyncpg.PostgresError):
        await feed.apply(conn, A, feed.Projection("core", 5, (), True), counts)  # type: ignore[arg-type]


# ── the run ──────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_not_configured_does_nothing_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    conn = pool()
    job = feed.EntitlementFeed(None)
    for _ in range(2):
        result = await job(conn, JOB)  # type: ignore[arg-type]
        assert result["status"] == "not_configured"
        check_payload(result, what="result")
    assert conn.calls == []
    assert sum("not_configured" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_backoff_after_failures() -> None:
    conn = pool(failures=2, last_error_at=NOW - dt.timedelta(seconds=30))
    transport = Transport()
    assert (await make(transport)(conn, JOB))["status"] == "backoff"  # type: ignore[arg-type]
    assert transport.urls == []


@pytest.mark.asyncio
async def test_a_page_projects_each_held_agency_once_and_moves_the_cursor() -> None:
    conn = pool(statuses={A: "active"}, cursor="c0")
    transport = Transport(page([A, B, A], "c1"), ok(account(A)))
    result = await make(transport)(conn, JOB)  # type: ignore[arg-type]
    check_payload(result, what="result")
    assert result["status"] == "ok" and result["applied_count"] == 1 and result["ignored_count"] == 1
    assert result["tenant_count"] == 2 and result["page_count"] == 1 and result["reconciled_count"] == 0
    assert transport.urls == [feed.events_url(BASE, "c0"), feed.account_url(BASE, A)]  # no snapshot call for B
    assert conn.sql("SET feed_cursor") == [("c0", "c1")]
    assert conn.sql("INSERT INTO crm.tenant_entitlement") == [("core", 10, ["ai_assist"])]


@pytest.mark.asyncio
async def test_an_empty_page_records_a_success_and_a_full_page_reads_the_next() -> None:
    conn = pool()
    assert (await make(Transport(page([], "")))(conn, JOB))["status"] == "ok"  # type: ignore[arg-type]
    assert conn.sql("SET last_ok_at = now(), failures = 0") == [()]
    conn = pool(statuses={})
    full = page([uuid.uuid4() for _ in range(feed.PAGE_LIMIT)], "c1")
    result = await make(Transport(full, page([], "c1")))(conn, JOB)  # type: ignore[arg-type]
    assert result["page_count"] == 2 and result["ignored_count"] == feed.PAGE_LIMIT


@pytest.mark.asyncio
async def test_cursor_moved_by_another_run_stops() -> None:
    conn = pool(statuses={}).on("SET feed_cursor", None)
    full = page([uuid.uuid4() for _ in range(feed.PAGE_LIMIT)], "c1")
    result = await make(Transport(full))(conn, JOB)  # type: ignore[arg-type]
    assert result["status"] == "ok" and result["page_count"] == 1


@pytest.mark.asyncio
async def test_hourly_reconcile_projects_every_agency() -> None:
    conn = pool(statuses={A: "active", B: "active"}, reconciled_at=NOW - dt.timedelta(hours=2), agencies=[A, B])
    transport = Transport(page([], ""), ok(account(A)), HttpResponse(404, b"{}"))
    result = await make(transport)(conn, JOB)  # type: ignore[arg-type]
    assert result["reconciled_count"] == 1 and result["applied_count"] == 1 and result["ignored_count"] == 1
    assert conn.sql("SET last_reconcile_at = now()") == [()]
    conn = pool(reconciled_at=None, agencies=[])
    assert (await make(Transport(page([], "")))(conn, JOB))["reconciled_count"] == 1  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "error"),
    [
        ([HttpResponse(401, b"{}")], "cis_unauthorized"),
        ([HttpResponse(403, b"{}")], "cis_forbidden"),
        ([HttpResponse(503, b"{}")], "cis_unavailable"),
        ([HttpError("timeout")], "cis_timeout"),
        ([HttpResponse(200, b"not json")], "cis_contract"),
        ([ok({"items": []})], "cis_contract"),
        ([page([A], "c1"), HttpResponse(500, b"{}")], "cis_unavailable"),
        ([page([A], "c1"), ok(account(B))], "cis_contract"),
        ([page([A], "c1"), HttpResponse(200, b"[")], "cis_contract"),
    ],
)
async def test_errors_end_the_run_with_their_class_and_keep_the_cursor(
    responses: list[HttpResponse | Exception], error: str
) -> None:
    conn = pool(statuses={A: "active"}, cursor="c0")
    tokens = FakeTokens()
    result = await make(Transport(*responses), tokens)(conn, JOB)  # type: ignore[arg-type]
    assert result["status"] == "error" and result["reason_code"] == error
    assert conn.sql("SET feed_cursor") == []
    assert conn.sql("SET last_error_class") == [(error,)]
    if error == "cis_unauthorized":
        assert tokens.invalidated == [feed.SCOPE]


@pytest.mark.asyncio
async def test_a_token_error_and_a_database_error_back_off(caplog: pytest.LogCaptureFixture) -> None:
    conn = pool(failures=feed.STUCK_FAILURES)
    result = await make(Transport(), FakeTokens("token_endpoint_unavailable"))(conn, JOB)  # type: ignore[arg-type]
    assert result["reason_code"] == "token_endpoint_unavailable"
    assert any("crm_entitlement_feed_stuck" in r.message for r in caplog.records)
    conn = pool(statuses={A: "active"}).on("INSERT INTO crm.tenant_entitlement", _pg_error("08006"))
    result = await make(Transport(page([A], ""), ok(account(A))))(conn, JOB)  # type: ignore[arg-type]
    assert result["reason_code"] == "apply_error"
    conn = pool(statuses={A: "active"}).on("INSERT INTO crm.tenant_entitlement", RuntimeError("bug"))
    result = await make(Transport(page([A], ""), ok(account(A))))(conn, JOB)  # type: ignore[arg-type]
    assert result["reason_code"] == "apply_error"


# ── the schedule, the state table, the gate ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_queues_one_due_run_per_slot() -> None:
    conn = ScriptedConn().on("INSERT INTO crm.job", {"id": "j1"}).on("RETURNING id", "j1")
    clock = {"now": NOW}
    hook = feed.EntitlementPullSchedule(lambda: clock["now"])
    await hook(conn)  # type: ignore[arg-type]
    assert await hook(conn) is None  # type: ignore[arg-type]
    clock["now"] = NOW + dt.timedelta(seconds=feed.SLOT_SECONDS)
    await hook(conn)  # type: ignore[arg-type]
    keys = [args for args in conn.sql("INSERT INTO crm.job")]
    assert len(keys) == 2 and feed.schedule_key(NOW) != feed.schedule_key(clock["now"])
    assert feed.schedule_key(NOW).startswith("crm_entitlement_pull:slot:")


@pytest.mark.asyncio
async def test_feed_state_names_only_its_two_tables() -> None:
    conn = ScriptedConn()
    with pytest.raises(ValueError, match="unknown feed state table"):
        await feed_state.load(conn, "crm.member")  # type: ignore[arg-type]
    assert await feed_state.health(conn, configured=True, table=feed_state.ENTITLEMENT) == "unknown"  # type: ignore[arg-type]
    assert await feed_state.last_reconcile(conn) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_require_feature_refuses_and_audits() -> None:
    ctx = tenant_ctx(ScriptedConn().on("FROM crm.tenant_entitlement", False))
    with pytest.raises(Forbidden) as info:
        await entitlements.require_feature(ctx, "whatsapp_sms", action="message_send", object_kind="channel",
                                           object_id=None)  # fmt: skip
    assert info.value.code == "feature_not_allowed" and "SMS and WhatsApp" in info.value.message
    assert ctx.audit_events[-1].outcome == "denied" and ctx.audit_events[-1].reason == "feature_not_allowed"
    ctx = tenant_ctx(ScriptedConn().on("FROM crm.tenant_entitlement", True))
    await entitlements.require_feature(ctx, "campaigns", action="x", object_kind="y", object_id=None)
    with pytest.raises(ValueError, match="unknown CRM feature"):
        await entitlements.feature_allowed(ctx, "teleport")
