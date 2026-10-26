"""AA-979 P2b — app.members.studio end to end on PostgreSQL 15 with the RDS role shape (crm-020): issue, re-issue,
claim, accept (with its job row), the CIS activation and its retry, all as crm_app through the real contexts and the
crm-020 row security and guard. CIS is a fake transport; the partner pool a MemoryDirectory."""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg
import pytest

from app.auth.route_guard import Caller
from app.cis.activation import ActivationConfig
from app.cis.http import HttpResponse
from app.errors import Conflict, Forbidden, Gone
from app.members import studio
from app.members.directory import MemoryDirectory
from app.members.mail import MemoryMailer
from tests.integration.test_crm020_schema import PLATFORM, _as

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ISS = "https://cognito-idp.local.test/local_partner"


class Tokens:
    async def token(self, scope: str) -> str:
        return "t"

    def invalidate(self, scope: str) -> None:
        return None


def cis(*statuses: int) -> ActivationConfig:
    answers = list(statuses)

    async def call(method: str, url: str, headers: Any, body: bytes | None, timeout_s: float) -> HttpResponse:
        return HttpResponse(answers.pop(0), json.dumps({}).encode())

    return ActivationConfig("https://cis.example.test", Tokens(), call)  # type: ignore[arg-type]


TENANT = uuid.uuid4()


async def _issue(pool: asyncpg.Pool, cis_invitation_id: uuid.UUID, mailer: MemoryMailer) -> str:
    await studio.issue(
        pool,
        cis_tenant_id=TENANT,
        cis_invitation_id=cis_invitation_id,
        email="Editor@Agency.example",
        locale="en",
        staff_sub="staff-1",
        mailer=mailer,
        web_origin="https://partner.example.test",
        ttl_hours=72,
    )
    return mailer.sent[-1].text.split("/join?t=")[1].split()[0]


async def _row(pool: asyncpg.Pool, cis_invitation_id: uuid.UUID) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        await _as(conn, PLATFORM)
        row = await conn.fetchrow(
            "SELECT status, activation_attempts, activation_error, cognito_sub FROM crm.studio_invitation "
            "WHERE cis_invitation_id = $1",
            cis_invitation_id,
        )
        jobs = await conn.fetchval("SELECT count(*) FROM crm.job WHERE kind = 'crm_studio_activation'")
    return {**dict(row), "jobs": jobs}


async def test_issue_claim_accept_activate(app_pool: asyncpg.Pool) -> None:
    mailer, directory = MemoryMailer(), MemoryDirectory(issuer=ISS)
    cis_id = uuid.uuid4()
    first = await _issue(app_pool, cis_id, mailer)
    token = await _issue(app_pool, cis_id, mailer)  # send again: same row, new token; the first link is dead
    assert first != token
    with pytest.raises(Exception):  # noqa: B017 — the old token finds nothing (404)
        await studio.claim(app_pool, directory, first)
    claimed = await studio.claim(app_pool, directory, token)
    assert claimed.state == "created" and claimed.email == "editor@agency.example"
    sub = directory.accounts["editor@agency.example"]["sub"]
    with pytest.raises(Forbidden):
        await studio.accept(app_pool, Caller("agency", "partner", "partner-web", ISS, "intruder", ()), token, None)
    jobs_before = (await _row(app_pool, cis_id))["jobs"]
    body = await studio.accept(app_pool, Caller("agency", "partner", "partner-web", ISS, sub, ()), token, cis(503))
    assert body["status"] == "accepted"
    row = await _row(app_pool, cis_id)
    assert row["status"] == "accepted" and row["activation_attempts"] == 1 and row["activation_error"] == "cis_http_503"
    assert row["jobs"] == jobs_before + 1
    async with app_pool.acquire() as conn, conn.transaction():
        await _as(conn, PLATFORM)
        sid = await conn.fetchval(
            "SELECT studio_invitation_id FROM crm.studio_invitation WHERE cis_invitation_id = $1", cis_id
        )
    out = await studio.activate_once(app_pool, cis(200), sid)
    assert out.kind == "activated"
    row = await _row(app_pool, cis_id)
    assert row["status"] == "activated" and row["activation_attempts"] == 2 and row["activation_error"] is None
    # Accepting again answers the state; issuing again is refused (the person is done).
    again = await studio.accept(app_pool, Caller("agency", "partner", "partner-web", ISS, sub, ()), token, None)
    assert again["status"] == "activated"
    with pytest.raises(Conflict) as exc:
        await _issue(app_pool, cis_id, mailer)
    assert exc.value.code == "invitation_accepted"


async def test_an_overdue_invitation_is_marked_expired_and_can_be_reissued(app_pool: asyncpg.Pool) -> None:
    mailer, directory = MemoryMailer(), MemoryDirectory(issuer=ISS)
    cis_id = uuid.uuid4()
    token = await _issue(app_pool, cis_id, mailer)
    # Back-date the row (a platform write the crm-020 guard allows: the expiry of a pending row).
    async with app_pool.acquire() as conn, conn.transaction():
        await _as(conn, PLATFORM)
        await conn.execute(
            "UPDATE crm.studio_invitation SET issued_at = now() - interval '4 days', "
            "expires_at = now() - interval '1 day' WHERE cis_invitation_id = $1",
            cis_id,
        )
    with pytest.raises(Gone):
        await studio.claim(app_pool, directory, token)
    assert (await _row(app_pool, cis_id))["status"] == "expired"
    token = await _issue(app_pool, cis_id, mailer)
    assert (await _row(app_pool, cis_id))["status"] == "pending"
    assert (await studio.claim(app_pool, directory, token)).state == "created"
