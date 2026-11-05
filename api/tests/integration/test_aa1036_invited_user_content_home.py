"""AA-1036 Done-when (root ADR 0019 d7): "An invited user lands on Content and sees CRM only with the entitlement."

End to end through the existing invitation flow on PostgreSQL 15 (crm_app, real row security) and the live app over HTTP
(real verifier, real ``tenant_tx()``):

1. AA staff invite a person to the agency's Content studio (``crm.studio_invitation``, the CIS invitation of Tenant
   360 › Members); the person claims (a new partner-pool account) and accepts; CIS activates the Content studio
   membership (fake CIS answering 200). That is the person's only access: the invitation writes no ``crm.member``
   (ADR 0006b d4: no CIS event grants a CRM role), so the agency app lands them on Content.
2. Their ``GET /crm/v1/me`` in that agency answers 403: the app switcher (web ``lib/apps.ts`` ``crmAccessFrom``) shows
   the CRM as an add-on to request, not as an app to open.
3. The owner gives them a CRM role while CIS says the agency has NO CRM add-on (projected plan free): 200 with
   ``crm_entitled`` false, still "request".
4. CIS attaches ``crm.core`` (the entitlement feed projects it): ``crm_entitled`` true, the CRM opens.

The web half (proxy "/" -> /portal/dashboard, the switcher states, the Add-ons page) is in web/tests/studio.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.auth.route_guard import Caller
from app.cis.activation import ActivationConfig
from app.cis.http import HttpResponse
from app.llm import app_profile
from app.main import create_app
from app.members import studio
from app.members.directory import MemoryDirectory
from app.members.mail import MemoryMailer
from tests.integration.test_crm005_intake import ISS, _agency
from tests.integration.test_crm020_schema import PLATFORM, _as
from tests.integration.test_crm043_entitlement_feed import JOB, Cis, _job
from tests.tokens import PARTNER_ISSUER, LocalPools

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

STAFF = "platform:00000000-0000-4000-8000-00000000a036"


class _Tokens:
    async def token(self, scope: str) -> str:
        return "t"

    def invalidate(self, scope: str) -> None:
        return None


def _cis_ok() -> ActivationConfig:
    async def call(method: str, url: str, headers: Any, body: bytes | None, timeout_s: float) -> HttpResponse:
        return HttpResponse(200, json.dumps({}).encode())

    return ActivationConfig("https://cis.example.test", _Tokens(), call)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def feed(admin: asyncpg.Connection) -> Cis:
    """The CIS account feed stand-in, from a clean feed cursor (as test_crm043's own fixture)."""
    await admin.execute(
        "UPDATE crm.entitlement_feed_state SET feed_cursor = NULL, failures = 0, last_error_class = NULL, "
        "last_error_at = NULL, last_reconcile_at = now(), reconcile_failures = 0"
    )
    return Cis()


async def _invite_and_join(pool: asyncpg.Pool, tenant: uuid.UUID) -> str:
    """The studio invitation of one CIS invitation, claimed and accepted; returns the new person's sub."""
    mailer, directory = MemoryMailer(), MemoryDirectory(issuer=ISS)
    cis_invitation = uuid.uuid4()
    await studio.issue(
        pool,
        cis_tenant_id=tenant,
        cis_invitation_id=cis_invitation,
        email="newcomer@lantern-agency.example",
        locale="en",
        staff_sub="staff-1",
        mailer=mailer,
        web_origin="https://partner.example.test",
        ttl_hours=72,
    )
    token = mailer.sent[-1].text.split("/join?t=")[1].split()[0]
    claimed = await studio.claim(pool, directory, token)
    assert claimed.state == "created"
    sub = directory.accounts["newcomer@lantern-agency.example"]["sub"]
    accepted = await studio.accept(pool, Caller("agency", "partner", "partner-web", ISS, sub, ()), token, _cis_ok())
    assert accepted["kind"] == "studio" and accepted["tenant_id"] == str(tenant)
    assert accepted["status"] == "activated"  # CIS made the Content studio membership: the person lands on Content
    return sub


async def _crm_members_of(pool: asyncpg.Pool, tenant: uuid.UUID, sub: str) -> int:
    async with pool.acquire() as conn, conn.transaction():
        await _as(conn, PLATFORM)
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        return int(await conn.fetchval("SELECT count(*) FROM crm.member WHERE cognito_sub = $1", sub))


async def _give_crm_role(pool: asyncpg.Pool, tenant: uuid.UUID, sub: str) -> None:
    """The owner's own action in the CRM Team page (ADR 0019 d7: the CRM role is the owner's or admin's act)."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        member = await conn.fetchval(
            "INSERT INTO crm.member (cognito_iss, cognito_sub, role, status) VALUES ($1, $2, 'agent', 'invited') "
            "RETURNING member_id",
            ISS,
            sub,
        )
        await conn.execute("UPDATE crm.member SET status = 'active' WHERE member_id = $1", member)


async def test_an_invited_user_lands_on_content_and_sees_the_crm_only_with_the_entitlement(
    app_pool: asyncpg.Pool, migrated_app_dsn: str, feed: Cis
) -> None:
    cis = feed
    assert ISS == PARTNER_ISSUER  # the seeded identities are the partner pool the HTTP tokens come from
    tenant, _people = await _agency(app_pool)
    sub = await _invite_and_join(app_pool, tenant)
    assert await _crm_members_of(app_pool, tenant, sub) == 0  # the invitation grants no CRM role

    pools = LocalPools()
    settings = pools.settings(database_url=migrated_app_dsn)
    app = create_app(settings, verifier=pools.verifier(settings))
    headers = {"Authorization": f"Bearer {pools.partner_web(sub=sub)}", "X-AA-Tenant-Id": str(tenant)}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://crm.test") as client:
        # 2. Content only: the CRM refuses the person (the switcher shows "Request add-on").
        first = await client.get("/crm/v1/me", headers=headers)
        assert first.status_code == 403

        # 3. A CRM role, but CIS says the agency has no CRM add-on: still not entitled.
        cis.account(tenant)  # projected with crm.core first (the feed adopts the agency) ...
        assert (await _job(cis)(app_pool, JOB))["applied_count"] == 1
        cis.account(tenant)
        cis.accounts[str(tenant)]["subscriptions"][1]["status"] = "cancelled"  # ... then the add-on is cancelled
        assert (await _job(cis)(app_pool, JOB))["applied_count"] == 1
        await _give_crm_role(app_pool, tenant, sub)
        member = await client.get("/crm/v1/me", headers=headers)
        assert member.status_code == 200
        assert member.json()["crm_entitled"] is False and member.json()["member_role"] == "agent"

        # 4. CIS attaches crm.core again: the CRM opens.
        cis.account(tenant)
        assert (await _job(cis)(app_pool, JOB))["applied_count"] == 1
        entitled = await client.get("/crm/v1/me", headers=headers)
        assert entitled.status_code == 200 and entitled.json()["crm_entitled"] is True
        assert entitled.json()["ai_paused"] is False  # no app profile row: AI on


@pytest_asyncio.fixture
async def no_profile_after(admin: asyncpg.Connection) -> AsyncIterator[None]:
    yield
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("DELETE FROM crm.llm_app_profile")


async def test_me_says_ai_is_paused_while_aa_switched_the_crm_ai_off(
    app_pool: asyncpg.Pool, migrated_app_dsn: str, no_profile_after: None
) -> None:
    """The CRM banner's signal (AA-1036, S265 follow-up): /me ai_paused follows crm.llm_app_profile (written by the CIS
    admin's App profiles through crm.ops.v1), and the staff reason never reaches the agency."""
    tenant, people = await _agency(app_pool)
    owner_sub = people[0][0].sub
    pools = LocalPools()
    settings = pools.settings(database_url=migrated_app_dsn)
    app = create_app(settings, verifier=pools.verifier(settings))
    headers = {"Authorization": f"Bearer {pools.partner_web(sub=owner_sub)}", "X-AA-Tenant-Id": str(tenant)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://crm.test") as client:
        assert (await client.get("/crm/v1/me", headers=headers)).json()["ai_paused"] is False
        await app_profile.write(app_pool, {"ai_enabled": False, "disabled_reason": "zz provider outage"}, actor=STAFF)
        paused = await client.get("/crm/v1/me", headers=headers)
        assert paused.status_code == 200 and paused.json()["ai_paused"] is True
        assert "zz provider outage" not in paused.text
        await app_profile.write(app_pool, {"ai_enabled": True}, actor=STAFF)
        assert (await client.get("/crm/v1/me", headers=headers)).json()["ai_paused"] is False
