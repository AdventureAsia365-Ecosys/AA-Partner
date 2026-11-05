# ruff: noqa: F811 — the ops_app / pools fixtures of test_ops_contract are reused by name
"""AA-1035 R2 close-out (crm.ops.v1 1.12, root ADR 0019 d4): the desk roles on /crm/ops/v1.

- :mod:`app.ops.staff_scope` alone: who is a desk caller, the CIS answer (tenant.membership.v1 1.3 getStaffTenantScope)
  parsed strictly, the cache, fail closed when CIS cannot answer.
- Through the real app: a ``cis-customer_success`` / ``cis-support`` token reaches only the agencies of the scope CIS
  reports (403 ``tenant_outside_scope`` otherwise), the list holds only those (an agency outside is never opened, so it
  gets no ``ops_read`` event), ``/console`` says ``can_write`` false and the scope, every platform-wide read and every
  write is 403, and the studio invitation relay is scoped too (support: to the kept address only).
- The kept address (crm-045): an issue keeps it, ``keep_address`` re-issues to it, a row without one is 409.
"""

from __future__ import annotations

import json
import urllib.parse
import uuid
from typing import Any

import pytest
from fastapi import HTTPException

from app.auth.route_guard import Caller
from app.cis.http import HttpError, HttpResponse
from app.cis.m2m import TokenError
from app.errors import Conflict
from app.members import studio
from app.ops import staff_scope
from app.vault.wrapping import LocalKeyWrapper
from tests.tokens import LocalPools
from tests.unit.test_ops_contract import JOB, PREFIX, REQ, SCENARIOS, TENANT, base_conn, ops_app, pools  # noqa: F401

OTHER = uuid.UUID("00000000-0000-4000-8000-0000000000b2")
T = f"{PREFIX}/tenants/{TENANT}"
OUT = f"{PREFIX}/tenants/{OTHER}"
CIS = "https://cis.example.test"


class FakeTokens:
    def __init__(self, fail: str | None = None) -> None:
        self.fail, self.asked, self.invalidated = fail, [], []

    async def token(self, scope: str) -> str:
        self.asked.append(scope)
        if self.fail:
            raise TokenError(self.fail)
        return "m2m-test-token"

    def invalidate(self, scope: str) -> None:
        self.invalidated.append(scope)


class FakeCis:
    """The studio activation config as staff_scope reads it, answering getStaffTenantScope."""

    def __init__(self, scope: str = "book", tenants: tuple[uuid.UUID, ...] = (TENANT,), status: int = 200) -> None:
        self.base_url = CIS
        self.tokens = FakeTokens()
        self.timeout_s = 2.0
        self.scope, self.tenants, self.status = scope, tenants, status
        self.calls: list[dict[str, str]] = []
        self.raise_error: HttpError | None = None
        self.body: bytes | None = None
        # getTenantMembers (the desk invitation check): the invited editor membership of REQ on TENANT
        self.members: dict[uuid.UUID, list[dict[str, Any]]] = {TENANT: [_member(TENANT, REQ, "editor")]}
        self.member_calls: list[str] = []
        self.members_error: HttpError | None = None

    async def transport(self, method: str, url: str, headers: Any, body: Any, _timeout_s: float) -> HttpResponse:
        assert headers["Authorization"] == "Bearer m2m-test-token"
        if not url.startswith(f"{CIS}{staff_scope.PATH}?"):
            return self._members(method, url)
        assert method == "GET"
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.calls.append(query)
        if self.raise_error:
            raise self.raise_error
        payload = {
            "identity": {"iss": query["iss"], "sub": query["sub"]},
            "desk_role": query["desk_role"],
            "scope": self.scope,
            "tenant_ids": [str(t) for t in self.tenants],
            "as_of": "2026-11-05T00:00:00Z",
        }
        return HttpResponse(self.status, self.body if self.body is not None else json.dumps(payload).encode(), {})

    def _members(self, method: str, url: str) -> HttpResponse:
        assert method == "GET"
        path = urllib.parse.urlsplit(url).path
        prefix, suffix = "/tenant/membership/v1/tenants/", "/members"
        assert path.startswith(prefix) and path.endswith(suffix), url
        tenant = uuid.UUID(path[len(prefix) : -len(suffix)])
        self.member_calls.append(str(tenant))
        if self.members_error:
            raise self.members_error
        if tenant not in self.members:
            return HttpResponse(404, b"{}", {})
        payload = {"tenant_id": str(tenant), "is_synthetic": False, "members": self.members[tenant]}
        return HttpResponse(200, json.dumps(payload).encode(), {})


def _member(tenant: uuid.UUID, invitation: uuid.UUID, role: str, status: str = "invited") -> dict[str, Any]:
    return {
        "tenant_id": str(tenant),
        "invitation_id": str(invitation),
        "membership_id": str(uuid.uuid4()),
        "content_studio_role": role,
        "status": status,
    }


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    staff_scope.CACHE.clear()
    yield
    staff_scope.CACHE.clear()


def _caller(*groups: str) -> Caller:
    return Caller("ops", "workforce", "cis-admin", "https://issuer.example.test/pool", "sub-desk", tuple(groups))


# ── the module ───────────────────────────────────────────────────────────────────────────────────────────────────


def test_who_is_a_desk_caller() -> None:
    assert staff_scope.desk_role(_caller("cis-admin")) is None
    assert staff_scope.desk_role(_caller("crm-platform-admin", "cis-support")) is None
    assert staff_scope.desk_role(_caller("cis-support", "cis-customer_success")) == "customer_success"
    assert staff_scope.desk_role(_caller("cis-support")) == "support"
    assert staff_scope.may_readdress(_caller("cis-customer_success"))
    assert not staff_scope.may_readdress(_caller("cis-support"))
    assert staff_scope.may_readdress(_caller("cis-admin", "cis-support"))
    staff_scope.require_full(_caller("cis-admin"))
    with pytest.raises(HTTPException) as exc:
        staff_scope.require_full(_caller("cis-support"))
    assert exc.value.detail == "missing_group"


ISS, SUB = "https://issuer.example.test/pool", "sub-desk"
ME = {"iss": ISS, "sub": SUB}


def _scope_body(**fields: Any) -> bytes:
    body = {"identity": ME, "desk_role": "customer_success", "scope": "book", "tenant_ids": [], "as_of": "x"}
    body.update(fields)
    return json.dumps({k: v for k, v in body.items() if v is not ...}).encode()


def test_an_answer_on_the_contract_is_the_scope() -> None:
    scope = staff_scope.parse_scope(_scope_body(tenant_ids=[str(TENANT)]), "customer_success", ISS, SUB)
    assert scope == staff_scope.StaffScope("book", "customer_success", frozenset({TENANT}))


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        _scope_body(desk_role="support"),  # another role than asked
        _scope_body(scope="all"),  # never "all" from CIS
        _scope_body(tenant_ids="x"),
        _scope_body(tenant_ids=["not-a-uuid"]),
        _scope_body(scope="none", tenant_ids=["00000000-0000-4000-8000-0000000000a1"]),
        # AA-1035 review: the identity CIS echoes must be the caller's own
        _scope_body(identity=...),  # no identity
        _scope_body(identity="sub-desk"),
        _scope_body(identity={"iss": ISS, "sub": "sub-other"}),  # another person's scope
        _scope_body(identity={"iss": "https://issuer.example.test/other-pool", "sub": SUB}),  # another pool
        _scope_body(identity={"sub": SUB}),
        b"not json",
    ],
)
def test_an_answer_off_the_contract_is_refused(body: bytes) -> None:
    with pytest.raises((ValueError, TypeError)):
        staff_scope.parse_scope(body, "customer_success", ISS, SUB)


@pytest.mark.asyncio
async def test_fetch_asks_cis_with_the_identity_and_the_role() -> None:
    cis = FakeCis(tenants=(TENANT, OTHER))
    scope = await staff_scope.fetch_scope(cis, _caller("cis-customer_success"), "customer_success")
    assert scope == staff_scope.StaffScope("book", "customer_success", frozenset({TENANT, OTHER}))
    assert cis.calls == [
        {"iss": "https://issuer.example.test/pool", "sub": "sub-desk", "desk_role": "customer_success"}
    ]
    assert cis.tokens.asked == [staff_scope.SCOPE_TENANT_READ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trouble", ["status_500", "status_401", "transport", "token", "body", "other_identity", "no_config"]
)
async def test_fetch_fails_closed(trouble: str) -> None:
    cis: Any = FakeCis()
    if trouble == "status_500":
        cis.status = 500
    elif trouble == "status_401":
        cis.status = 401
    elif trouble == "transport":
        cis.raise_error = HttpError("timeout")
    elif trouble == "token":
        cis.tokens = FakeTokens(fail="token_unavailable")
    elif trouble == "body":
        cis.body = b"{}"
    elif trouble == "other_identity":
        cis.body = _scope_body(identity={"iss": ISS, "sub": "sub-other"}, desk_role="support")
    else:
        cis = None
    with pytest.raises(HTTPException) as exc:
        await staff_scope.fetch_scope(cis, _caller("cis-support"), "support")
    assert (exc.value.status_code, exc.value.detail) == (503, staff_scope.UNAVAILABLE)
    if trouble == "status_401":
        assert cis.tokens.invalidated == [staff_scope.SCOPE_TENANT_READ]


def test_the_cache_expires() -> None:
    now = [100.0]
    cache = staff_scope.ScopeCache(clock=lambda: now[0])
    scope = staff_scope.StaffScope("book", "support", frozenset({TENANT}))
    cache.put(("i", "s", "support"), scope)
    assert cache.get(("i", "s", "support")) == scope
    now[0] += staff_scope.CACHE_TTL_S + 1
    assert cache.get(("i", "s", "support")) is None


# ── through the app ──────────────────────────────────────────────────────────────────────────────────────────────


def _desk(pools: LocalPools, group: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {pools.cis_admin([group])}"}


@pytest.fixture
def desk_app(ops_app: Any) -> Any:  # noqa: F811
    client, state = ops_app
    cis = FakeCis(scope="book", tenants=(TENANT,))
    state["app"].state.studio_activation = cis
    return client, state, cis


TENANT_READS = ["", "/data-requests", "/jobs", "/members"]
ACCOUNT_READS = ["/usage", "/entitlements"]  # plan, seats, usage: customer_success only (role matrix, CIS 1.12)


@pytest.mark.parametrize(
    ("leaf", "group"),
    [(leaf, group) for leaf in TENANT_READS for group in ("cis-customer_success", "cis-support")]
    + [(leaf, "cis-customer_success") for leaf in ACCOUNT_READS],
)
def test_a_desk_caller_reads_its_scope_only(desk_app: Any, pools: LocalPools, leaf: str, group: str) -> None:
    client, state, cis = desk_app
    state["conn"] = base_conn()
    assert client.get(f"{T}{leaf}", headers=_desk(pools, group)).status_code == 200
    state["conn"] = conn = base_conn()
    r = client.get(f"{OUT}{leaf}", headers=_desk(pools, group))
    assert (r.status_code, r.json()["detail"]) == (403, staff_scope.OUTSIDE)
    assert conn.sql("INSERT INTO crm.audit_event") == []  # never opened, never on record
    assert len(cis.calls) == 1  # one CIS call per identity and role (cached)


@pytest.mark.parametrize("leaf", ACCOUNT_READS)
@pytest.mark.parametrize("tenant", [TENANT, OTHER])
def test_support_never_reads_an_agency_account(desk_app: Any, pools: LocalPools, leaf: str, tenant: uuid.UUID) -> None:
    """AA-1035 review: support sees no plan and no usage (role matrix; CIS: tenant.book.account.read), in its scope or
    not; refused before CIS is asked and before the agency is opened."""
    client, state, cis = desk_app
    state["conn"] = conn = base_conn()
    r = client.get(f"{PREFIX}/tenants/{tenant}{leaf}", headers=_desk(pools, "cis-support"))
    assert (r.status_code, r.json()["detail"]) == (403, "missing_group")
    assert conn.sql("INSERT INTO crm.audit_event") == []
    assert cis.calls == []


@pytest.mark.parametrize("leaf", ACCOUNT_READS)
def test_customer_success_with_support_reads_the_account(desk_app: Any, pools: LocalPools, leaf: str) -> None:
    client, state, _ = desk_app
    state["conn"] = base_conn()
    headers = {"Authorization": f"Bearer {pools.cis_admin(['cis-support', 'cis-customer_success'])}"}
    assert client.get(f"{T}{leaf}", headers=headers).status_code == 200


def test_the_list_holds_the_scope_only(desk_app: Any, pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    client, state, _ = desk_app
    from app.ops import console

    class _Agency:
        def __init__(self, tenant_id: uuid.UUID) -> None:
            self.tenant_id = tenant_id

    async def agencies(_pool: Any) -> list[Any]:
        return [_Agency(OTHER), _Agency(TENANT)]

    monkeypatch.setattr(console.registry, "agencies", agencies)
    state["conn"] = conn = base_conn()
    r = client.get(f"{PREFIX}/tenants", headers=_desk(pools, "cis-customer_success"))
    assert r.status_code == 200, r.text
    assert [i["tenant_id"] for i in r.json()["items"]] == [str(TENANT)]
    # the agency outside the scope is never opened (no platform context, no ops_read event of it)
    assert all(str(OTHER) not in repr(args) for args in conn.sql(""))


def test_the_console_says_the_scope(desk_app: Any, pools: LocalPools) -> None:
    client, _, cis = desk_app
    r = client.get(f"{PREFIX}/console", headers=_desk(pools, "cis-support"))
    assert r.json() == {"can_write": False, "scope": "book"}
    assert client.get(f"{PREFIX}/console", headers={"Authorization": f"Bearer {pools.cis_admin()}"}).json() == {
        "can_write": False,
        "scope": "all",
    }
    assert len(cis.calls) == 1  # the full caller never asks CIS


def test_cis_down_is_503_for_the_desk_caller_only(desk_app: Any, pools: LocalPools) -> None:
    client, state, cis = desk_app
    cis.raise_error = HttpError("timeout")
    state["conn"] = base_conn()
    r = client.get(f"{T}/members", headers=_desk(pools, "cis-customer_success"))
    assert (r.status_code, r.json()["detail"]) == (503, staff_scope.UNAVAILABLE)
    assert client.get(f"{T}/members", headers={"Authorization": f"Bearer {pools.cis_admin()}"}).status_code == 200


def test_every_platform_wide_read_and_every_write_is_refused(desk_app: Any, pools: LocalPools) -> None:
    client, state, _ = desk_app
    refused = 0
    for method, route, path, body, _adjust, _who, _status, _detail in SCENARIOS:
        if route in {"/console", "/tenants"} and method == "GET" or "{tenant_id}" in route and method == "GET":
            continue
        if route == "/tenants/{tenant_id}/studio-invitations":
            continue
        state["conn"] = base_conn()
        r = client.request(method, path, json=body, headers=_desk(pools, "cis-customer_success"))
        assert r.status_code in (403, 422), f"{method} {path} -> {r.status_code}"
        if r.status_code == 403:
            assert r.json()["detail"] == "missing_group", f"{method} {path}"
            refused += 1
    assert refused >= 10


def _invite(client: Any, pools: LocalPools, group: str, base: str = T, **body: Any) -> Any:
    payload = {"cis_invitation_id": str(REQ), "locale": "en", **body}
    return client.post(f"{base}/studio-invitations", json=payload, headers=_desk(pools, group))


def test_the_studio_relay_is_scoped_and_support_keeps_the_address(desk_app: Any, pools: LocalPools) -> None:
    client, state, _ = desk_app
    state["conn"] = base_conn()
    r = _invite(client, pools, "cis-support", email="editor@agency.example")
    assert (r.status_code, r.json()["detail"]) == (403, staff_scope.READDRESS)
    r = _invite(client, pools, "cis-customer_success", base=OUT, email="editor@agency.example")
    assert (r.status_code, r.json()["detail"]) == (403, staff_scope.OUTSIDE)
    state["conn"] = base_conn()
    assert _invite(client, pools, "cis-customer_success", email="editor@agency.example").status_code == 202
    # support, kept address, a row that kept none (base_conn: no row) -> 409 address_not_kept
    state["conn"] = base_conn()
    r = _invite(client, pools, "cis-support", keep_address=True)
    assert (r.status_code, r.json()["detail"]) == (409, studio.NOT_KEPT)


def _no_invitation_written(conn: Any) -> bool:
    return conn.sql("INSERT INTO crm.studio_invitation") == [] and conn.sql("UPDATE crm.studio_invitation") == []


@pytest.mark.parametrize(
    ("group", "body"),
    [
        # customer_success redirecting an owner invitation to an address of its choosing (the owner seat)
        ("cis-customer_success", {"email": "desk.member@agency.example"}),
        ("cis-customer_success", {"keep_address": True}),
        # support sending an owner invitation again to the kept address (CIS refuses it: tenant.write only)
        ("cis-support", {"keep_address": True}),
    ],
)
def test_a_desk_caller_never_delivers_an_owner_invitation(
    desk_app: Any, pools: LocalPools, group: str, body: dict
) -> None:
    """The desk caller holds the token CIS relays, so it can call the CRM without CIS's owner-tier check: the CRM asks
    CIS for the invitation's role itself and refuses the owner seat."""
    client, state, cis = desk_app
    cis.members[TENANT] = [_member(TENANT, REQ, "owner")]
    state["conn"] = conn = base_conn()
    r = _invite(client, pools, group, **body)
    assert (r.status_code, r.json()["detail"]) == (403, studio.OWNER_NOT_ALLOWED)
    assert cis.member_calls == [str(TENANT)]
    assert _no_invitation_written(conn)


def test_a_desk_caller_delivers_only_an_invitation_cis_holds_open_on_this_tenant(
    desk_app: Any, pools: LocalPools
) -> None:
    client, state, cis = desk_app
    # an invitation of a tenant outside the scope, sent under an in-scope tenant: not on this tenant's member list
    cis.members = {TENANT: [], OTHER: [_member(OTHER, REQ, "editor")]}
    state["conn"] = conn = base_conn()
    r = _invite(client, pools, "cis-customer_success", email="desk.member@agency.example")
    assert (r.status_code, r.json()["detail"]) == (403, studio.NOT_INVITED)
    assert _no_invitation_written(conn)
    # a membership already active (or removed) has no invitation to send
    cis.members = {TENANT: [_member(TENANT, REQ, "viewer", status="active")]}
    state["conn"] = conn = base_conn()
    r = _invite(client, pools, "cis-customer_success", email="desk.member@agency.example")
    assert (r.status_code, r.json()["detail"]) == (403, studio.NOT_INVITED)
    assert _no_invitation_written(conn)
    # CIS has no such tenant at all
    cis.members = {}
    state["conn"] = base_conn()
    r = _invite(client, pools, "cis-customer_success", email="desk.member@agency.example")
    assert (r.status_code, r.json()["detail"]) == (403, studio.NOT_INVITED)


def test_the_desk_invitation_check_fails_closed(desk_app: Any, pools: LocalPools) -> None:
    client, state, cis = desk_app
    cis.members_error = HttpError("timeout")
    state["conn"] = conn = base_conn()
    r = _invite(client, pools, "cis-customer_success", email="desk.member@agency.example")
    assert (r.status_code, r.json()["detail"]) == (503, staff_scope.UNAVAILABLE)
    assert _no_invitation_written(conn)


def test_an_administrator_still_sends_an_owner_invitation_without_the_desk_check(
    desk_app: Any, pools: LocalPools
) -> None:
    client, state, cis = desk_app
    cis.members[TENANT] = [_member(TENANT, REQ, "owner")]
    state["conn"] = base_conn()
    r = client.post(
        f"{T}/studio-invitations",
        json={"cis_invitation_id": str(REQ), "email": "owner@agency.example"},
        headers={"Authorization": f"Bearer {pools.cis_admin()}"},
    )
    assert r.status_code == 202, r.text
    assert cis.member_calls == [] and cis.calls == []


@pytest.mark.parametrize(
    "body", [{}, {"email": "editor@agency.example", "keep_address": True}, {"keep_address": False}]
)
def test_exactly_one_way_to_address(desk_app: Any, pools: LocalPools, body: dict) -> None:
    client, state, _ = desk_app
    state["conn"] = base_conn()
    assert _invite(client, pools, "cis-customer_success", **body).status_code == 422


def test_the_e2e_admin_never_uses_the_kept_address(desk_app: Any, pools: LocalPools) -> None:
    from app.auth import e2e_staff

    client, state, _ = desk_app
    state["conn"] = base_conn()
    token = pools.cis_admin(["cis-admin"], email=e2e_staff.E2E_CIS_ADMIN_EMAIL)
    r = client.post(
        f"{T}/studio-invitations",
        json={"cis_invitation_id": str(REQ), "keep_address": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only")


# ── the kept address ─────────────────────────────────────────────────────────────────────────────────────────────


class _Keys:
    active_provider = "local"

    def __init__(self) -> None:
        self.local = LocalKeyWrapper.ephemeral()

    def wrapper(self, provider: str) -> LocalKeyWrapper:
        assert provider == "local"
        return self.local


@pytest.mark.asyncio
async def test_keep_and_open_round_trip() -> None:
    keys = _Keys()
    kept = await studio.keep_address(keys, TENANT, REQ, "someone@agency.example")
    assert kept is not None and kept.provider == "local" and kept.blob[0] == studio.KEPT_VERSION
    assert b"someone@agency.example" not in kept.blob and "agency" not in repr(kept)
    row = dict(
        zip(
            ("email_kept", "email_kept_dek", "email_kept_kek_provider", "email_kept_kek_id"),
            kept.columns(),
            strict=True,
        )
    )
    assert await studio.open_kept(keys, row, TENANT, REQ) == "someone@agency.example"
    # another invitation id or tenant never opens it (AAD and the KMS context)
    for tenant, invitation in ((TENANT, JOB), (OTHER, REQ)):
        with pytest.raises(Conflict):
            await studio.open_kept(keys, row, tenant, invitation)
    assert await studio.keep_address(None, TENANT, REQ, "someone@agency.example") is None


@pytest.mark.asyncio
async def test_a_row_without_a_kept_address_is_409() -> None:
    for row in (None, {"email_kept": None}):
        with pytest.raises(Conflict) as exc:
            await studio.open_kept(_Keys(), row, TENANT, REQ)
        assert exc.value.code == studio.NOT_KEPT
    with pytest.raises(Conflict):
        await studio.open_kept(
            _Keys(),
            {"email_kept": b"\x09" + b"x" * 40, "email_kept_dek": b"x", "email_kept_kek_provider": "local"},
            TENANT,
            REQ,
        )


def test_the_claim_clears_the_kept_address() -> None:
    assert "email_kept = NULL" in studio.CLAIM_SQL and "email_kept_kek_id = NULL" in studio.CLAIM_SQL
    assert "email_kept" in studio.INSERT_SQL and "email_kept" in studio.REISSUE_SQL
