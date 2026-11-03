"""crm_entitlement_pull on PostgreSQL 15 with the RDS role shape (AA-1028 R1 slice 2), through the real handler,
``entitlement_feed_tx`` and the crm-008 / crm-013 / crm-043 triggers; CIS is a fake transport serving the
tenant.entitlements.v1 feed and snapshots.

- A CIS account with the CRM add-on: the agency gets the plan, seats and allowed features (projected_at set); the L1
  read says ``managed_by`` cis; a member's AI draft gate follows the feature (an add-on change removes ai_assist -> 403
  ``feature_not_allowed``).
- Suspension in CIS (in_service false) locks the agency with ``lock_source = cis_tenant``; reinstatement lifts it; an
  AA lock is never lifted by CIS.
- A tenant the CRM has no agency for is ignored (no snapshot call); the cursor moves and a second run with nothing new
  writes nothing; the hourly reconcile projects an agency no event named.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app import entitlements
from app.cis.entitlement_feed import EntitlementFeed
from app.cis.http import HttpResponse
from app.cis.m2m import M2mCredentials, TokenProvider
from app.cis.membership_feed import FeedConfig
from app.errors import Forbidden
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job
from app.ops import console
from app.tenancy import tenant_tx
from tests.integration.test_crm005_intake import _agency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

JOB = Job(id=str(uuid.uuid4()), kind="crm_entitlement_pull", payload={}, attempt=1, max_attempts=3)
BASE = "https://api-cis.example.invalid"
ALL = {f"crm.{c}": None for c in entitlements.FEATURES}


class Tokens(TokenProvider):
    def __init__(self) -> None:
        super().__init__(M2mCredentials("abc123", "x", "https://m2m-auth.example.invalid/oauth2/token"))

    async def token(self, scope: str) -> str:
        assert scope == "cis.tenant/read"
        return "tok"


class Cis:
    """The CIS account feed (every event after the cursor) and the snapshots (404 for an unknown tenant)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.accounts: dict[str, dict[str, Any]] = {}
        self.snapshots: list[str] = []

    def account(self, tenant: uuid.UUID, *, in_service: bool = True, plan: str | None = "crm.core",
                ents: dict[str, str | None] | None = None) -> None:  # fmt: skip
        subs = [{"product": "content", "plan_key": "content.growth", "status": "active", "trial_ends_at": None,
                 "seats": None}]  # fmt: skip
        if plan:
            subs.append({"product": "crm", "plan_key": plan, "status": "active", "trial_ends_at": None, "seats": 3})
        ents = {"crm.core": None, "crm.ai_assist": None, "crm.seats": "3.00"} if ents is None else ents
        self.accounts[str(tenant)] = {
            "tenant_id": str(tenant), "account_status": "active" if in_service else "suspended",
            "suspend_reason": None if in_service else "billing", "in_service": in_service, "kind": "agency",
            "is_pilot": False, "data_region": "us", "time_zone": "UTC", "subscriptions": subs,
            "entitlements": [{"feature_key": k, "product": "crm", "kind": "flag", "unit": None, "limit": v,
                              "source": "plan", "expires_at": None} for k, v in ents.items()],
            "as_of": "2026-11-03T12:00:00Z",
        }  # fmt: skip
        self.events.append({"event_id": str(uuid.uuid4()), "cursor": f"a_{len(self.events) + 1:06d}",
                            "type": "entitlement.changed", "occurred_at": "2026-11-03T12:00:00Z",
                            "tenant_id": str(tenant), "change": {}})  # fmt: skip

    async def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout_s: float
    ) -> HttpResponse:
        path, _, query = url[len(BASE) :].partition("?")
        if path.startswith("/tenant/v1/tenants/"):
            tenant = path.split("/")[4]
            self.snapshots.append(tenant)
            if tenant not in self.accounts:
                return HttpResponse(404, b'{"error":"not_found","message":"x"}')
            return HttpResponse(200, json.dumps(self.accounts[tenant]).encode())
        params = dict(p.split("=", 1) for p in query.split("&"))
        after = params.get("after")
        start = int(after[2:]) if after else 0
        items = self.events[start : start + int(params["limit"])]
        next_cursor = items[-1]["cursor"] if items else (after or "")
        return HttpResponse(200, json.dumps({"items": items, "next_cursor": next_cursor}).encode())


@pytest_asyncio.fixture
async def cis(admin: asyncpg.Connection) -> Cis:
    # The singleton is shared by every test of the session: start from no cursor, no failure, a fresh reconcile.
    await admin.execute(
        "UPDATE crm.entitlement_feed_state SET feed_cursor = NULL, failures = 0, last_error_class = NULL, "
        "last_error_at = NULL, last_reconcile_at = now()"
    )
    return Cis()


def _job(cis: Cis) -> EntitlementFeed:
    return EntitlementFeed(FeedConfig(base_url=BASE, tokens=Tokens(), transport=cis))


async def _entitlement(pool: asyncpg.Pool, tenant: uuid.UUID) -> dict[str, Any]:
    return await console.agency_entitlements(pool, tenant, staff_sub="staff-sub-1")


async def _tenant(admin: asyncpg.Connection, tenant: uuid.UUID) -> tuple[str, str | None]:
    async with admin.transaction():
        await admin.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        row = await admin.fetchrow("SELECT status, lock_source FROM crm.tenant WHERE tenant_id = $1", tenant)
    return row["status"], row["lock_source"]


async def test_the_crm_add_on_is_projected_and_gates_the_ai_assistant(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, cis: Cis
) -> None:
    tenant, people = await _agency(app_pool)
    stranger = uuid.uuid4()  # a CIS tenant without a CRM agency
    cis.account(tenant)
    cis.account(stranger)
    result = await _job(cis)(app_pool, JOB)
    check_payload(result, what="result")
    assert result["status"] == "ok" and result["applied_count"] == 1 and result["ignored_count"] == 1
    assert cis.snapshots == [str(tenant)]  # no snapshot call for the stranger
    assert await _entitlement(app_pool, tenant) == {
        "plan": "core", "seats_allowed": 3, "features_allowed": ["ai_assist"], "insights_opt_out": False,
        "managed_by": "cis",
    }  # fmt: skip
    owner = people[0][0]
    async with tenant_tx(app_pool, owner, tenant) as ctx:
        assert await entitlements.feature_allowed(ctx, "ai_assist") is True
        assert await entitlements.feature_allowed(ctx, "whatsapp_sms") is False
    # the plan loses the AI assistant in CIS: the next run takes it away, and the gate refuses
    cis.account(tenant, ents={"crm.core": None, "crm.seats": "3.00"})
    assert (await _job(cis)(app_pool, JOB))["applied_count"] == 1
    with pytest.raises(Forbidden) as info:
        async with tenant_tx(app_pool, owner, tenant) as ctx:
            await entitlements.require_feature(ctx, "ai_assist", action="ai_use", object_kind="trip", object_id=None)
    assert info.value.code == "feature_not_allowed"
    # nothing new: nothing written
    again = await _job(cis)(app_pool, JOB)
    assert again["tenant_count"] == 0 and again["applied_count"] == 0


async def test_cis_suspension_locks_and_reinstatement_unlocks(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, cis: Cis
) -> None:
    tenant, _ = await _agency(app_pool)
    cis.account(tenant, in_service=False, plan=None, ents={})
    result = await _job(cis)(app_pool, JOB)
    assert result["locked_count"] == 1
    assert await _tenant(admin, tenant) == ("locked", "cis_tenant")
    assert (await _entitlement(app_pool, tenant))["plan"] == "free"
    cis.account(tenant)
    assert (await _job(cis)(app_pool, JOB))["unlocked_count"] == 1
    assert await _tenant(admin, tenant) == ("active", None)
    # an AA lock stays when CIS says in service
    await console.lock_agency(app_pool, tenant, staff_sub="staff-sub-1", reason_code="payment", ticket_ref="SUP-1")
    cis.account(tenant)
    assert (await _job(cis)(app_pool, JOB))["unlocked_count"] == 0
    assert (await _tenant(admin, tenant))[1] == "aa_ops"


async def test_the_hourly_reconcile_projects_an_agency_no_event_named(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, cis: Cis
) -> None:
    tenant, _ = await _agency(app_pool)
    cis.account(tenant, plan="crm.pro", ents=ALL | {"crm.seats": "8.00"})
    cis.events.clear()  # the account exists in CIS, but no event names it
    await admin.execute("UPDATE crm.entitlement_feed_state SET last_reconcile_at = now() - interval '2 hours'")
    result = await _job(cis)(app_pool, JOB)
    assert result["reconciled_count"] == 1 and result["applied_count"] >= 1
    ent = await _entitlement(app_pool, tenant)
    assert (
        ent["plan"] == "pro" and ent["seats_allowed"] == 8 and ent["features_allowed"] == sorted(entitlements.FEATURES)
    )
    assert await admin.fetchval(
        "SELECT last_reconcile_at > now() - interval '1 minute' FROM crm.entitlement_feed_state"
    )
