"""AA-979 / root ADR 0006a A10b-1 (S252 security review): the Dev-only E2E CIS admin on ``/crm/ops/v1``.

The identity holds exactly ``cis-admin`` (like a real CIS admin) and is told apart by its access token's ``email``
claim. It is read-only on every ``crm-platform-admin`` write and may use the studio-invitation relay only for a
synthetic CRM agency, an E2E mailbox address and an invited ``viewer`` / ``editor`` membership CIS confirms, in an
answer where CIS marks the tenant synthetic too (contract 1.2 ``is_synthetic``). A real CIS admin is unchanged (no CIS
lookup, no synthetic read). Scripted connection and a fake CIS transport: no network, no DB.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import e2e_staff
from app.cis import invitation_lookup
from app.cis.activation import ActivationConfig
from app.cis.http import HttpError, HttpResponse
from app.cis.m2m import TokenError
from app.errors import Forbidden
from app.main import create_app
from app.members import studio
from app.members.mail import MemoryMailer
from app.ops import console
from app.routers import ops as ops_router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
CIS_INV = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
SID = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
NOW = dt.datetime(2026, 10, 26, 9, 0, tzinfo=dt.UTC)
PREFIX = "/crm/ops/v1"
PATH = f"{PREFIX}/tenants/{TENANT}/studio-invitations"
A10B = e2e_staff.E2E_CIS_ADMIN_EMAIL
E2E_INVITEE = "e2e-test-studio-viewer@e2e-mail.lumiguides.it.com"
CIS_BASE = "https://cis.example.test"
SYNTHETIC_SQL = "SELECT is_synthetic IS TRUE FROM crm.tenant"
SAMPLES = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "samples" / "tenant.membership.v1"


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


def member(role: str = "viewer", status: str = "invited", invitation: uuid.UUID = CIS_INV) -> dict[str, Any]:
    return {
        "membership_id": str(uuid.uuid4()),
        "tenant_id": str(TENANT),
        "identity": None,
        "content_studio_role": role,
        "status": status,
        "invitation_id": str(invitation),
        "updated_at": NOW.isoformat(),
    }


OMIT = object()


def snapshot(*members: dict[str, Any], synthetic: Any = True) -> dict[str, Any]:
    """A getTenantMembers 200 body; ``synthetic`` is the 1.2 ``is_synthetic`` (``OMIT`` = a 1.1 body without it)."""
    body = {"tenant_id": str(TENANT), "tenant_state": "active", "members": list(members), "as_of": NOW.isoformat()}
    if synthetic is not OMIT:
        body["is_synthetic"] = synthetic
    return body


class FakeCis:
    """tenant.membership.v1 getTenantMembers: a status and a body, or a transport failure."""

    def __init__(self, status: int = 200, body: Any = None, raises: bool = False) -> None:
        self.status, self.body, self.raises = status, body, raises
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def __call__(self, method: str, url: str, headers: Any, data: Any, timeout_s: float) -> HttpResponse:
        self.calls.append((method, url, dict(headers)))
        if self.raises:
            raise HttpError("URLError")
        return HttpResponse(self.status, json.dumps(self.body if self.body is not None else {}).encode())


def conn(synthetic: bool | None = True) -> ScriptedConn:
    return ScriptedConn(
        [
            (SYNTHETIC_SQL, synthetic),
            ("FROM crm.studio_invitation WHERE cis_invitation_id = $1", None),
            ("INSERT INTO crm.studio_invitation", {"studio_invitation_id": SID, "expires_at": NOW}),
        ]
    )


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


@pytest.fixture
def env(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    settings = pools.settings()  # environment "test": a synthetic-only environment, like dev
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), mailer=MemoryMailer())
    state: dict[str, Any] = {"conn": conn(), "cis": FakeCis(body=snapshot(member()))}
    state["tokens"] = FakeTokens()

    async def pool(_request: Any) -> ScriptedConn:
        return state["conn"]

    monkeypatch.setattr(ops_router, "request_pool", pool)
    monkeypatch.setattr(console, "Vault", lambda keys, ctx: FakeVault())
    with TestClient(app) as client:
        app.state.studio_activation = ActivationConfig(
            base_url=CIS_BASE, tokens=state["tokens"], transport=state["cis"]
        )
        state.update(client=client, app=app, pools=pools)
        yield state


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def a10b(pools: LocalPools, **kw: Any) -> dict[str, str]:
    return bearer(pools.cis_admin(email=kw.pop("email", A10B), **kw))


def send(env: dict[str, Any], headers: dict[str, str], email: str = E2E_INVITEE) -> Any:
    return env["client"].post(PATH, headers=headers, json={"email": email, "cis_invitation_id": str(CIS_INV)})


def refused(r: Any) -> bool:
    return (r.status_code, r.json()["detail"]) == (403, "e2e_read_only")


def wrote_nothing(env: dict[str, Any]) -> bool:
    return not env["conn"].sql("INSERT INTO crm.studio_invitation") and not env["app"].state.mailer.sent


# ── a real CIS admin: unchanged ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("claim", [None, "staff@aa.example"], ids=["no-email-claim", "staff-email"])
def test_a_real_cis_admin_is_unchanged(env: dict[str, Any], claim: str | None) -> None:
    pools = env["pools"]
    headers = bearer(pools.cis_admin(**({"email": claim} if claim else {})))
    r = send(env, headers, email="owner@agency.example")
    assert r.status_code == 202, r.json()
    assert env["conn"].sql("INSERT INTO crm.studio_invitation")
    assert env["cis"].calls == [] and env["conn"].sql(SYNTHETIC_SQL) == []  # no lookup, no synthetic read
    assert env["client"].get(f"{PREFIX}/console", headers=headers).json() == {"can_write": False}
    admin = bearer(pools.cis_admin(groups=["crm-platform-admin"], email="staff@aa.example"))
    assert env["client"].get(f"{PREFIX}/console", headers=admin).json() == {"can_write": True}


# ── the A10b identity: allowed path ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_a10b_may_send_a_viewer_or_editor_invitation_of_a_synthetic_agency(env: dict[str, Any], role: str) -> None:
    env["cis"].body = snapshot(member("owner", invitation=uuid.uuid4()), member(role))
    r = send(env, a10b(env["pools"]))
    assert r.status_code == 202, r.json()
    assert r.json()["invitation_id"] == str(CIS_INV)
    assert env["conn"].sql(SYNTHETIC_SQL) == [(TENANT,)]
    assert env["conn"].sql("set_config('app.tenant_id'")[0] == (str(TENANT),)
    ((method, url, headers),) = env["cis"].calls
    assert (method, url) == ("GET", f"{CIS_BASE}/tenant/membership/v1/tenants/{TENANT}/members")
    assert headers["Authorization"] == "Bearer m2m-token" and env["tokens"].scopes == ["cis.tenant/read"]
    (mail,) = env["app"].state.mailer.sent
    assert mail.to == E2E_INVITEE


def test_a10b_send_again_of_an_expired_viewer_invitation_is_allowed(env: dict[str, Any]) -> None:
    env["conn"].on(
        "FROM crm.studio_invitation WHERE cis_invitation_id = $1",
        {"studio_invitation_id": SID, "cis_tenant_id": TENANT, "status": "expired"},
    ).on("UPDATE crm.studio_invitation SET status = 'pending'", {"studio_invitation_id": SID, "expires_at": NOW})
    r = send(env, a10b(env["pools"]))
    assert r.status_code == 202 and r.json()["reissued"] is True


# ── the A10b identity: refusals ─────────────────────────────────────────────────────────────────────────────────────


def test_a10b_may_not_send_an_owner_invitation(env: dict[str, Any]) -> None:
    """The finding's worst case: re-sealing an owner invitation to the E2E mailbox and claiming it as owner."""
    env["cis"].body = snapshot(member("owner"))
    assert refused(send(env, a10b(env["pools"])))
    assert wrote_nothing(env)


@pytest.mark.parametrize("synthetic", [False, None], ids=["real-agency", "no-crm-agency"])
def test_a10b_on_a_real_tenant_is_refused_before_any_cis_call(env: dict[str, Any], synthetic: bool | None) -> None:
    env["conn"] = conn(synthetic)
    assert refused(send(env, a10b(env["pools"])))
    assert env["cis"].calls == [] and wrote_nothing(env)


@pytest.mark.parametrize(
    "address",
    ["owner@agency.example", "x@sub.e2e-mail.lumiguides.it.com", A10B, "x@e2e-mail.lumiguides.it.com.evil.example"],
)
def test_a10b_may_send_only_to_an_e2e_mailbox(env: dict[str, Any], address: str) -> None:
    assert refused(send(env, a10b(env["pools"]), email=address))
    assert env["conn"].sql(SYNTHETIC_SQL) == [] and env["cis"].calls == [] and wrote_nothing(env)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, snapshot()),  # CIS has no such invitation in this tenant
        (200, snapshot(member(invitation=uuid.uuid4()))),
        (200, snapshot(member(status="active"))),  # already used
        (200, snapshot(member(status="removed"))),
        (404, {"error": "not_found", "message": "No agency has this id."}),
    ],
    ids=["no-members", "other-invitation", "active", "removed", "cis-404"],
)
def test_a10b_needs_an_invited_membership_at_cis(env: dict[str, Any], status: int, body: Any) -> None:
    env["cis"].status, env["cis"].body = status, body
    assert refused(send(env, a10b(env["pools"])))
    assert wrote_nothing(env)


@pytest.mark.parametrize(
    "synthetic",
    [False, None, OMIT],
    ids=["cis-real-tenant", "cis-null", "cis-1.1-without-the-field"],
)
def test_a10b_needs_cis_to_mark_the_tenant_synthetic(env: dict[str, Any], synthetic: Any) -> None:
    """Contract 1.2: the CRM agency is synthetic (term 3, every Dev agency is) but CIS must say so too, in the same
    getTenantMembers answer; a 1.1 answer without ``is_synthetic`` is refused (fail closed)."""
    env["cis"].body = snapshot(member(), synthetic=synthetic)
    assert refused(send(env, a10b(env["pools"])))
    assert env["conn"].sql(SYNTHETIC_SQL) == [(TENANT,)] and len(env["cis"].calls) == 1
    assert wrote_nothing(env)


def test_a_real_cis_admin_does_not_need_cis_synthetic(env: dict[str, Any]) -> None:
    """The new term binds the A10b identity only: a real admin on a real tenant never asks CIS."""
    env["cis"].body = snapshot(member(), synthetic=False)
    r = send(env, bearer(env["pools"].cis_admin(email="staff@aa.example")), email="owner@agency.example")
    assert r.status_code == 202, r.json()
    assert env["cis"].calls == []


@pytest.mark.parametrize(
    "cis",
    [
        FakeCis(raises=True),
        FakeCis(status=503),
        FakeCis(body={"members": "nope"}),
        FakeCis(status=401),
        FakeCis(body=snapshot(member(), synthetic="true")),
    ],
    ids=["network", "5xx", "bad-shape", "401", "is-synthetic-not-a-boolean"],
)
def test_a10b_fails_closed_when_cis_cannot_answer(env: dict[str, Any], cis: FakeCis) -> None:
    env["app"].state.studio_activation = ActivationConfig(base_url=CIS_BASE, tokens=env["tokens"], transport=cis)
    r = send(env, a10b(env["pools"]))
    assert (r.status_code, r.json()["detail"]) == (503, "cis_unavailable")
    assert wrote_nothing(env)
    if cis.status == 401:
        assert env["tokens"].invalidated == ["cis.tenant/read"]


def test_a10b_fails_closed_without_cis_configuration(env: dict[str, Any]) -> None:
    env["app"].state.studio_activation = None
    r = send(env, a10b(env["pools"]))
    assert (r.status_code, r.json()["detail"]) == (503, "cis_unavailable")
    assert wrote_nothing(env)


def test_the_e2e_claim_is_matched_trimmed_and_case_blind(env: dict[str, Any]) -> None:
    env["cis"].body = snapshot(member("owner"))
    assert refused(send(env, a10b(env["pools"], email=" E2E-CIS-Admin@E2E-Mail.Lumiguides.it.com ")))


def test_a10b_is_read_only_on_every_platform_admin_write(env: dict[str, Any]) -> None:
    """Every crm-platform-admin write refuses the A10b identity, even with a token that (against the trigger's rule)
    held crm-platform-admin; the console offers it no write button. Reads stay open."""
    client = env["client"]
    spec = client.app.openapi()
    writes = [
        (m.upper(), p.replace("{tenant_id}", str(TENANT)).replace("{job_id}", str(SID)).replace(
            "{member_id}", str(SID)).replace("{request_id}", str(SID)))
        for p, ops in spec["paths"].items() if p.startswith(PREFIX) for m in ops
        if m.upper() in ("POST", "PUT", "PATCH", "DELETE") and not p.endswith("/studio-invitations")
    ]  # fmt: skip
    assert len(writes) >= 11
    for groups in (["cis-admin"], ["cis-admin", "crm-platform-admin"]):
        headers = a10b(env["pools"], groups=groups)
        for method, path in writes:
            assert refused(client.request(method, path, headers=headers)), (method, path)
        assert client.get(f"{PREFIX}/console", headers=headers).json() == {"can_write": False}
    assert client.get(f"{PREFIX}/jobs/summary", headers=a10b(env["pools"])).status_code == 200


def test_a_caller_without_an_ops_group_is_refused(env: dict[str, Any]) -> None:
    r = send(env, bearer(env["pools"].cis_admin(groups=[], email=A10B)))
    assert (r.status_code, r.json()["detail"]) == (403, "missing_group")
    assert wrote_nothing(env)


# ── the check on its own ────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["prod", "stg"])
async def test_a10b_is_refused_outside_the_synthetic_only_environments(environment: str) -> None:
    cis = FakeCis(body=snapshot(member()))
    with pytest.raises(Forbidden) as exc:
        await studio.check_e2e_invitation(
            conn(),
            environment=environment,
            cis_tenant_id=TENANT,
            cis_invitation_id=CIS_INV,
            email=E2E_INVITEE,
            cis=ActivationConfig(base_url=CIS_BASE, tokens=FakeTokens(), transport=cis),
        )
    assert exc.value.code == "e2e_read_only" and cis.calls == []


@pytest.mark.asyncio
async def test_the_lookup_reads_ids_in_any_case_and_refuses_a_foreign_tenant_row() -> None:
    upper = snapshot(member("editor"))
    upper["members"][0]["invitation_id"] = str(CIS_INV).upper()
    cfg = ActivationConfig(base_url=CIS_BASE, tokens=FakeTokens(), transport=FakeCis(body=upper))
    found = await invitation_lookup.invitation(cfg, TENANT, CIS_INV)
    assert (found.kind, found.role, found.status, found.synthetic) == ("found", "editor", "invited", True)
    foreign = snapshot(member())
    foreign["members"][0]["tenant_id"] = str(uuid.uuid4())
    cfg = ActivationConfig(base_url=CIS_BASE, tokens=FakeTokens(), transport=FakeCis(body=foreign))
    assert (await invitation_lookup.invitation(cfg, TENANT, CIS_INV)).kind == "error"
    cfg = ActivationConfig(base_url=CIS_BASE, tokens=FakeTokens(fail=True), transport=FakeCis(body=snapshot()))
    assert (await invitation_lookup.invitation(cfg, TENANT, CIS_INV)).kind == "error"


def test_e2e_address_rules() -> None:
    assert e2e_staff.is_e2e_cis_admin(A10B) and e2e_staff.is_e2e_cis_admin(f" {A10B.upper()} ")
    for other in (None, "", "staff@aa.example", "e2e-cis-admin@e2e-mail.lumiguides.it.com.x", 7):
        assert not e2e_staff.is_e2e_cis_admin(other)
    assert e2e_staff.is_e2e_mailbox_address(E2E_INVITEE)
    for bad in (A10B, "@e2e-mail.lumiguides.it.com", "a@b@e2e-mail.lumiguides.it.com", "K@e2e-mail.lumiguides.it.com",
                "x@sub.e2e-mail.lumiguides.it.com", "x@lumiguides.it.com", None):  # fmt: skip
        assert not e2e_staff.is_e2e_mailbox_address(bad), bad


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sample", "kind", "synthetic"),
    [
        ("tenant-members-synthetic-valid.json", "found", True),
        ("tenant-members-valid.json", "missing", False),  # a real agency, and not this invitation
        ("tenant-members-synthetic-null-invalid.json", "found", None),
        ("tenant-members-synthetic-missing-invalid.json", "found", None),
    ],
)
async def test_the_lookup_reads_is_synthetic_from_the_contract_samples(sample: str, kind: str, synthetic: Any) -> None:
    """The root samples of tenant.membership.v1 1.2 (vendored in docs/contracts/samples): only an explicit true is
    synthetic; null and a missing field come back None, which check_e2e_invitation refuses."""
    body = json.loads((SAMPLES / sample).read_text(encoding="utf-8"))["payload"]
    tenant = uuid.UUID(body["tenant_id"])
    invited = next((m for m in body["members"] if m["status"] == "invited"), None)
    invitation = uuid.UUID(invited["invitation_id"]) if invited else CIS_INV
    cfg = ActivationConfig(base_url=CIS_BASE, tokens=FakeTokens(), transport=FakeCis(body=body))
    found = await invitation_lookup.invitation(cfg, tenant, invitation)
    assert (found.kind, found.synthetic) == (kind, synthetic)
