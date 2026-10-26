"""AA-979 P2b — Content studio invitations (app.members.studio, app.cis.activation): the token-sealed address, issue /
re-issue, claim, accept with the activation fast path and the job, and every CIS outcome. Scripted connection only;
the SQL and the crm-020 guards run on PostgreSQL in tests/integration/test_studio_invitations_db.py."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest

from app.auth.route_guard import Caller
from app.cis.activation import SCOPE_TENANT_ACTIVATE, ActivationConfig, Outcome, activate
from app.cis.http import HttpError, HttpResponse
from app.cis.m2m import TokenError
from app.errors import Conflict, Forbidden, Gone, Invalid, NotFound
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.members import studio
from app.members.directory import MemoryDirectory
from app.members.mail import MemoryMailer
from app.members.team import new_token
from tests.unit.fakes import ScriptedConn

pytestmark = pytest.mark.asyncio

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
CIS_INV = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
SID = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
NOW = dt.datetime(2026, 10, 26, 9, 0, tzinfo=dt.UTC)
ISS = "https://cognito-idp.local.test/local_partner"
ADDRESS = "editor@agency.example"


class FakeTokens:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.scopes: list[str] = []
        self.invalidated: list[str] = []

    async def token(self, scope: str) -> str:
        self.scopes.append(scope)
        if self.fail:
            raise TokenError("token_unavailable")
        return "m2m-token"

    def invalidate(self, scope: str) -> None:
        self.invalidated.append(scope)


def transport(status: int = 200, body: Any = None, raises: bool = False, calls: list | None = None):
    async def call(method, url, headers, data, timeout_s):
        if calls is not None:
            calls.append((method, url, headers, json.loads(data)))
        if raises:
            raise HttpError("URLError")
        return HttpResponse(status, json.dumps(body or {}).encode())

    return call


def config(**kw: Any) -> ActivationConfig:
    tokens = kw.pop("tokens", FakeTokens())
    return ActivationConfig(base_url="https://cis.example.test", tokens=tokens, transport=transport(**kw))


def caller(sub: str = "sub-1") -> Caller:
    return Caller("agency", "partner", "partner-web", ISS, sub, ())


def token_row(token: str, status: str = "pending", expired: bool = False, sub: str | None = None) -> dict[str, Any]:
    return {
        "studio_invitation_id": SID,
        "cis_tenant_id": TENANT,
        "cis_invitation_id": CIS_INV,
        "email_enc": studio.seal_email(token, CIS_INV, ADDRESS),
        "status": status,
        "locale": "en",
        "expires_at": NOW,
        "expired": expired,
        "cognito_iss": ISS if sub else None,
        "cognito_sub": sub,
        "activation_attempts": 0,
    }


# ── the sealed address ────────────────────────────────────────────────────────────────────────────────────────────


async def test_the_address_opens_only_with_its_token_and_invitation() -> None:
    token, _ = new_token()
    blob = studio.seal_email(token, CIS_INV, ADDRESS)
    assert ADDRESS.encode() not in blob
    assert studio.open_email(token, CIS_INV, blob) == ADDRESS
    other, _ = new_token()
    for bad in ((other, CIS_INV, blob), (token, uuid.uuid4(), blob), (token, CIS_INV, b"\x02" + blob[1:])):
        with pytest.raises(Forbidden):
            studio.open_email(*bad)
    with pytest.raises(Forbidden):
        studio.open_email(token, CIS_INV, b"\x01short")


# ── issue ───────────────────────────────────────────────────────────────────────────────────────────────────────────


def issue_kwargs(**kw: Any) -> dict[str, Any]:
    out = {
        "cis_tenant_id": TENANT,
        "cis_invitation_id": CIS_INV,
        "email": " Editor@Agency.example ",
        "locale": "vi",
        "staff_sub": "staff-1",
        "mailer": MemoryMailer(),
        "web_origin": "https://partner.example.test",
        "ttl_hours": 168,
    }
    out.update(kw)
    return out


async def test_issue_creates_seals_and_mails_once() -> None:
    conn = ScriptedConn([("INSERT INTO crm.studio_invitation", {"studio_invitation_id": SID, "expires_at": NOW})])
    kw = issue_kwargs()
    body = await studio.issue(conn, **kw)
    assert body == {
        "invitation_id": str(CIS_INV),
        "tenant_id": str(TENANT),
        "status": "pending",
        "expires_at": NOW.isoformat(),
        "reissued": False,
    }
    assert conn.sql("set_config('app.actor'")[0] == ("platform:staff-1",)
    (args,) = conn.sql("INSERT INTO crm.studio_invitation")
    assert args[0] == TENANT and args[1] == CIS_INV and args[4] == "vi" and ADDRESS.encode() not in args[2]
    (mail,) = kw["mailer"].sent
    assert mail.to == ADDRESS and mail.kind == "studio_invitation"
    token = mail.text.split("/join?t=")[1].split()[0]
    assert studio.open_email(token, CIS_INV, args[2]) == ADDRESS  # the mailed token opens the stored copy


async def test_issue_again_reissues_a_pending_or_expired_row() -> None:
    for status in ("pending", "expired"):
        conn = ScriptedConn(
            [
                ("FROM crm.studio_invitation WHERE cis_invitation_id = $1",
                 {"studio_invitation_id": SID, "cis_tenant_id": TENANT, "status": status}),
                ("UPDATE crm.studio_invitation SET status = 'pending'",
                 {"studio_invitation_id": SID, "expires_at": NOW}),
            ]
        )  # fmt: skip
        body = await studio.issue(conn, **issue_kwargs())
        assert body["reissued"] is True and conn.sql("UPDATE crm.studio_invitation SET status = 'pending'")


@pytest.mark.parametrize(
    "status,error,code",
    [
        ("claimed", Conflict, "invitation_claimed"),
        ("accepted", Conflict, "invitation_accepted"),
        ("activated", Conflict, "invitation_accepted"),
        ("revoked", Gone, "invitation_revoked"),
    ],
)
async def test_issue_leaves_a_claimed_or_finished_row_alone(status: str, error: type, code: str) -> None:
    conn = ScriptedConn(
        [("FROM crm.studio_invitation WHERE cis_invitation_id = $1",
          {"studio_invitation_id": SID, "cis_tenant_id": TENANT, "status": status})]
    )  # fmt: skip
    kw = issue_kwargs()
    with pytest.raises(error) as exc:
        await studio.issue(conn, **kw)
    assert exc.value.code == code and kw["mailer"].sent == []


async def test_issue_refuses_another_tenant_bad_input_and_no_origin() -> None:
    conn = ScriptedConn(
        [("FROM crm.studio_invitation WHERE cis_invitation_id = $1",
          {"studio_invitation_id": SID, "cis_tenant_id": uuid.uuid4(), "status": "pending"})]
    )  # fmt: skip
    with pytest.raises(Conflict):
        await studio.issue(conn, **issue_kwargs())
    with pytest.raises(Invalid):
        await studio.issue(ScriptedConn(), **issue_kwargs(email="not an address"))
    with pytest.raises(Invalid):
        await studio.issue(ScriptedConn(), **issue_kwargs(locale="fr"))
    with pytest.raises(Exception, match="PARTNER_WEB_ORIGIN"):
        await studio.issue(ScriptedConn(), **issue_kwargs(web_origin=""))


# ── claim ───────────────────────────────────────────────────────────────────────────────────────────────────────────


async def test_claim_creates_the_account_and_binds_it() -> None:
    token, _ = new_token()
    conn = ScriptedConn([("FROM crm.studio_invitation WHERE token_hash", token_row(token))])
    directory = MemoryDirectory(issuer=ISS)
    claimed = await studio.claim(conn, directory, token)
    assert claimed.state == "created" and claimed.email == ADDRESS and claimed.temporary_password
    (args,) = conn.sql("SET status = 'claimed'")
    assert args == (SID, ISS, directory.accounts[ADDRESS]["sub"])
    assert conn.sql("set_config('app.actor'")[0] == (f"invitation:{SID}",)


async def test_claim_again_shows_no_password_and_finished_rows_are_gone() -> None:
    token, _ = new_token()
    conn = ScriptedConn([("FROM crm.studio_invitation WHERE token_hash", token_row(token, "claimed", sub="s"))])
    claimed = await studio.claim(conn, MemoryDirectory(issuer=ISS), token)
    assert claimed.state == "already_claimed" and claimed.temporary_password is None
    for status, code in (("accepted", "invitation_used"), ("revoked", "invitation_used"),
                         ("expired", "invitation_expired")):  # fmt: skip
        conn = ScriptedConn([("FROM crm.studio_invitation WHERE token_hash", token_row(token, status, sub="s"))])
        with pytest.raises(Gone) as exc:
            await studio.claim(conn, MemoryDirectory(issuer=ISS), token)
        assert exc.value.code == code


async def test_claim_marks_an_overdue_invitation_expired_then_answers_410() -> None:
    token, _ = new_token()
    conn = ScriptedConn([("FROM crm.studio_invitation WHERE token_hash", token_row(token, expired=True))])
    with pytest.raises(Gone) as exc:
        await studio.claim(conn, MemoryDirectory(issuer=ISS), token)
    assert exc.value.code == "invitation_expired" and conn.sql("SET status = 'expired'")


async def test_unknown_or_malformed_token_is_404() -> None:
    token, _ = new_token()
    with pytest.raises(NotFound):
        await studio.claim(ScriptedConn(), MemoryDirectory(issuer=ISS), token)
    with pytest.raises(NotFound):
        await studio.claim(ScriptedConn(), MemoryDirectory(issuer=ISS), "short")


# ── accept + activation fast path ───────────────────────────────────────────────────────────────────────────────


def accept_conn(token: str, status: str = "claimed", sub: str = "sub-1", expired: bool = False) -> ScriptedConn:
    state = {"status": status}

    def job_row(*_: Any) -> dict[str, Any]:
        return {"cis_invitation_id": CIS_INV, "status": state["status"], "cognito_iss": ISS, "cognito_sub": sub}

    def mark(new: str):
        def apply(*_: Any) -> None:
            state["status"] = new

        return apply

    return ScriptedConn(
        [
            ("FROM crm.studio_invitation WHERE token_hash", token_row(token, status, expired=expired, sub=sub)),
            ("SET status = 'accepted', accepted_at", mark("accepted")),
            ("SET status = 'activated', activated_at", mark("activated")),
            ("WHERE studio_invitation_id = $1 FOR UPDATE", job_row),
            ("INSERT INTO crm.job", "job-1"),
        ]
    )


async def test_accept_queues_the_job_and_activates_at_once() -> None:
    token, _ = new_token()
    conn = accept_conn(token)
    calls: list = []
    cfg = ActivationConfig("https://cis.example.test", FakeTokens(), transport(200, {"changed": True}, calls=calls))
    body = await studio.accept(conn, caller(), token, cfg)
    assert body == {"kind": "studio", "tenant_id": str(TENANT), "status": "activated"}
    (job,) = conn.sql("INSERT INTO crm.job")
    assert job[0] == "crm_studio_activation" and json.loads(job[1]) == {"studio_invitation_id": str(SID)}
    ((method, url, headers, sent),) = calls
    assert url == f"https://cis.example.test/tenant/membership/v1/invitations/{CIS_INV}/activate"
    assert sent == {"identity": {"iss": ISS, "sub": "sub-1"}} and headers["Authorization"] == "Bearer m2m-token"


async def test_accept_without_cis_or_during_an_outage_stays_accepted() -> None:
    token, _ = new_token()
    assert (await studio.accept(accept_conn(token), caller(), token, None))["status"] == "accepted"
    conn = accept_conn(token)
    body = await studio.accept(conn, caller(), token, config(status=503))
    assert body["status"] == "accepted" and conn.sql("activation_attempts = activation_attempts + 1")


async def test_accept_refuses_another_identity_and_an_unclaimed_invitation() -> None:
    token, _ = new_token()
    with pytest.raises(Forbidden) as exc:
        await studio.accept(accept_conn(token), caller("someone-else"), token, None)
    assert exc.value.code == "invitation_identity_mismatch"
    with pytest.raises(Gone) as exc:
        await studio.accept(accept_conn(token, status="pending"), caller(), token, None)
    assert exc.value.code == "invitation_not_claimed"


async def test_accept_again_answers_the_current_state() -> None:
    token, _ = new_token()
    body = await studio.accept(accept_conn(token, status="activated"), caller(), token, None)
    assert body == {"kind": "studio", "tenant_id": str(TENANT), "status": "activated"}
    conn = accept_conn(token, status="accepted")
    body = await studio.accept(conn, caller(), token, config(status=200))
    assert body["status"] == "activated"
    (job,) = conn.sql("INSERT INTO crm.job")
    assert job[2].startswith(f"crm_studio_activation:{SID}:")  # one more job, keyed per hour


async def test_accept_marks_an_overdue_invitation_expired() -> None:
    token, _ = new_token()
    conn = accept_conn(token, expired=True)
    with pytest.raises(Gone):
        await studio.accept(conn, caller(), token, None)
    assert conn.sql("SET status = 'expired'")
    with pytest.raises(Gone):
        await studio.accept(accept_conn(token, status="expired"), caller(), token, None)


# ── the job ────────────────────────────────────────────────────────────────────────────────────────────────────────


def job(payload: dict[str, Any] | None = None) -> Job:
    return Job("j1", "crm_studio_activation", payload or {"studio_invitation_id": str(SID)}, 1, 12)


async def test_job_activates_retries_or_fails_permanently() -> None:
    token, _ = new_token()
    ok = await studio.build_handler(config(status=200))(accept_conn(token, status="accepted"), job())
    assert ok == {"studio_invitation_id": str(SID), "status": "activated"}
    with pytest.raises(studio.ActivationRetry):
        await studio.build_handler(config(status=502))(accept_conn(token, status="accepted"), job())
    with pytest.raises(PermanentError):
        await studio.build_handler(config(status=409, body={"reason": "identity_mismatch"}))(
            accept_conn(token, status="accepted"), job()
        )
    with pytest.raises(studio.ActivationRetry):
        await studio.build_handler(None)(accept_conn(token, status="accepted"), job())
    with pytest.raises(PermanentError):
        await studio.build_handler(config())(accept_conn(token), job({"studio_invitation_id": "nope"}))


async def test_job_skips_rows_that_are_no_longer_accepted() -> None:
    token, _ = new_token()
    calls: list = []
    cfg = ActivationConfig("https://cis.example.test", FakeTokens(), transport(calls=calls))
    assert await studio.activate_once(accept_conn(token, status="activated"), cfg, SID) == Outcome("activated", "ok")
    assert (await studio.activate_once(accept_conn(token, status="revoked"), cfg, SID)).kind == "refused"
    assert (await studio.activate_once(ScriptedConn(), cfg, SID)).code == "not_found"
    assert calls == []  # CIS is never called for them


# ── the CIS call ────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,body,kind,code",
    [
        (200, {"changed": False}, "activated", "ok"),
        (401, {}, "retry", "cis_unauthorized"),
        (403, {}, "retry", "cis_forbidden"),
        (404, {}, "refused", "cis_invitation_not_found"),
        (409, {"reason": "membership_removed"}, "refused", "cis_membership_removed"),
        (409, {"reason": "weird"}, "refused", "cis_conflict"),
        (400, {}, "refused", "cis_invalid_request"),
        (500, {}, "retry", "cis_http_500"),
    ],
)
async def test_cis_outcomes(status: int, body: dict, kind: str, code: str) -> None:
    tokens = FakeTokens()
    out = await activate(config(status=status, body=body, tokens=tokens), CIS_INV, ISS, "s")
    assert out == Outcome(kind, code)
    assert tokens.scopes == [SCOPE_TENANT_ACTIVATE]
    assert tokens.invalidated == ([SCOPE_TENANT_ACTIVATE] if status == 401 else [])


async def test_cis_transport_and_token_failures_retry() -> None:
    assert (await activate(config(raises=True), CIS_INV, ISS, "s")).kind == "retry"
    assert await activate(config(tokens=FakeTokens(fail=True)), CIS_INV, ISS, "s") == Outcome(
        "retry", "token_unavailable"
    )
    cfg = config(status=409)
    cfg = ActivationConfig(cfg.base_url, cfg.tokens, _raw(409, b"not json"))
    assert (await activate(cfg, CIS_INV, ISS, "s")).code == "cis_conflict"


def _raw(status: int, body: bytes):
    async def call(method, url, headers, data, timeout_s):
        return HttpResponse(status, body)

    return call
