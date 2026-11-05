# ruff: noqa: F811 — the ops_app / pools / desk_app fixtures are reused by name
"""AA-1035 (root ADR 0006a A10d): the Dev-only CIS customer_success E2E identity writes nothing in the CRM.

The identity is a WORKFORCE ``cis-admin`` client token with exactly ``cis-customer_success`` whose ``email`` claim is
:data:`app.auth.e2e_staff.E2E_CIS_CS_EMAIL`. The guard flags it ``e2e_no_write`` (never ``e2e_read_only``, which opens
the A10b-1 invitation path) and refuses it outside ``dev`` / ``test`` / ``local``. The studio invitation relay, the one
``/crm/ops/v1`` write a ``cis-customer_success`` token reaches, refuses it with 403 ``e2e_read_only`` before any check,
with or without an address, synthetic agency or not; :func:`require_group` refuses it too. A real customer_success
member and the A10b admin are unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth import e2e_staff, identity_config
from app.auth.identity_config import GROUP_CRM_PLATFORM_ADMIN, OPS
from app.auth.route_guard import Caller, class_router, current_caller, require_group
from tests.tokens import LocalPools
from tests.unit.test_ops_contract import PREFIX, REQ, TENANT, base_conn, ops_app, pools  # noqa: F401
from tests.unit.test_ops_staff_scope import T, _no_invitation_written, desk_app  # noqa: F401

CS = e2e_staff.E2E_CIS_CS_EMAIL
GROUP = "cis-customer_success"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _cs_token(pools: LocalPools, groups: list[str] | None = None, email: str = CS) -> str:
    return pools.cis_admin([GROUP] if groups is None else groups, email=email)


# ── e2e_staff ────────────────────────────────────────────────────────────────────────────────────────────────────


def test_the_address_matches_exactly() -> None:
    assert CS == "e2e-cis-cs@e2e-mail.lumiguides.it.com"
    assert e2e_staff.is_e2e_cis_cs(CS) and e2e_staff.is_e2e_cis_cs(f" {CS.upper()} ")
    for other in (None, "", CS + ".evil", "x" + CS, e2e_staff.E2E_CIS_ADMIN_EMAIL, e2e_staff.E2E_CRM_STAFF_EMAIL):
        assert not e2e_staff.is_e2e_cis_cs(other)
    assert not e2e_staff.is_e2e_cis_admin(CS) and not e2e_staff.is_e2e_crm_staff(CS)
    # Not an invitee address of the studio relay either.
    assert not e2e_staff.is_e2e_mailbox_address(CS)


# ── the guard ────────────────────────────────────────────────────────────────────────────────────────────────────


def _probe_app(pools: LocalPools, environment: str, seen: list | None = None) -> FastAPI:
    """A one-route app on the OPS class: the guard alone; the caller's flags go to ``seen`` (the OPS serializer drops
    keys that are not on the contract, so they are not echoed in the body)."""
    seen = [] if seen is None else seen
    settings = pools.settings(environment=environment, db_ssl="require")
    app = FastAPI()
    app.state.settings = settings
    app.state.verifier = pools.verifier(settings)
    app.state.identity = identity_config.identity_from_settings(settings)
    router = class_router(OPS)

    @router.get("/probe")
    async def probe(caller: Caller = Depends(current_caller)) -> dict[str, bool]:  # noqa: B008
        seen.append({"e2e_no_write": caller.e2e_no_write, "e2e_read_only": caller.e2e_read_only})
        return {}

    @router.post("/write")
    async def write(caller: Caller = Depends(require_group(GROUP_CRM_PLATFORM_ADMIN))) -> dict[str, bool]:  # noqa: B008
        return {"ok": True}

    app.include_router(router)
    return app


@pytest.mark.parametrize("environment", ["dev", "test", "local"])
def test_the_identity_is_flagged_no_write_on_dev(pools: LocalPools, environment: str) -> None:
    seen: list = []
    with TestClient(_probe_app(pools, environment, seen)) as c:
        assert c.get(f"{OPS.prefix}/probe", headers=_bearer(_cs_token(pools))).status_code == 200
        # A real customer_success member and the A10b admin are not flagged no-write.
        assert (
            c.get(f"{OPS.prefix}/probe", headers=_bearer(_cs_token(pools, email="cs@example.test"))).status_code == 200
        )
        a10b = pools.cis_admin(["cis-admin"], email=e2e_staff.E2E_CIS_ADMIN_EMAIL)
        assert c.get(f"{OPS.prefix}/probe", headers=_bearer(a10b)).status_code == 200
    assert seen == [
        {"e2e_no_write": True, "e2e_read_only": False},
        {"e2e_no_write": False, "e2e_read_only": False},
        {"e2e_no_write": False, "e2e_read_only": True},
    ]


@pytest.mark.parametrize("environment", ["stg", "prod"])
def test_the_identity_is_refused_anywhere_else(pools: LocalPools, environment: str) -> None:
    with TestClient(_probe_app(pools, environment)) as c:
        res = c.get(f"{OPS.prefix}/probe", headers=_bearer(_cs_token(pools, email=CS.upper())))
        assert (res.status_code, res.json()["detail"]) == (403, "e2e_read_only")
        res = c.get(f"{OPS.prefix}/probe", headers=_bearer(_cs_token(pools, email="cs@example.test")))
        assert res.status_code == 200


def test_require_group_refuses_it_whatever_its_groups(pools: LocalPools) -> None:
    with TestClient(_probe_app(pools, "dev")) as c:
        # Even a (hand-made) token holding crm-platform-admin: the trigger never issues one, this is the second lock.
        token = _cs_token(pools, groups=[GROUP, GROUP_CRM_PLATFORM_ADMIN])
        res = c.post(f"{OPS.prefix}/write", headers=_bearer(token))
        assert (res.status_code, res.json()["detail"]) == (403, "e2e_read_only")
        res = c.post(
            f"{OPS.prefix}/write",
            headers=_bearer(_cs_token(pools, groups=[GROUP_CRM_PLATFORM_ADMIN], email="admin@example.test")),
        )
        assert res.status_code == 200


def test_require_group_unit() -> None:
    dependency = require_group(GROUP_CRM_PLATFORM_ADMIN)
    caller = Caller(
        route_class="ops",
        issuer_kind="workforce",
        client_kind="cis_admin",
        iss="i",
        sub="s",
        groups=(GROUP_CRM_PLATFORM_ADMIN,),
        e2e_no_write=True,
    )
    with pytest.raises(HTTPException) as exc:
        dependency(caller)
    assert (exc.value.status_code, exc.value.detail) == (403, "e2e_read_only")


# ── the studio invitation relay ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"email": "e2e-test-studio-viewer@e2e-mail.lumiguides.it.com"},  # an E2E mailbox address (the A10b-1 path)
        {"email": "editor@agency.example"},
        {"keep_address": True},
    ],
)
def test_the_studio_relay_refuses_it_before_any_check(desk_app: Any, pools: LocalPools, body: dict) -> None:
    client, state, cis = desk_app
    state["conn"] = conn = base_conn()
    payload = {"cis_invitation_id": str(REQ), "locale": "en", **body}
    r = client.post(f"{T}/studio-invitations", json=payload, headers=_bearer(_cs_token(pools)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only")
    assert cis.calls == []  # not even the staff-scope read: refused before any check
    assert _no_invitation_written(conn)


def test_the_studio_relay_refuses_it_with_and_without_an_address(desk_app: Any, pools: LocalPools) -> None:
    client, state, cis = desk_app
    for extra in ({"email": "e2e-test-studio-editor@e2e-mail.lumiguides.it.com"}, {"keep_address": True}):
        state["conn"] = conn = base_conn()
        payload = {"cis_invitation_id": str(REQ), "locale": "en", **extra}
        r = client.post(f"{T}/studio-invitations", json=payload, headers=_bearer(_cs_token(pools)))
        assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only")
        assert _no_invitation_written(conn)
    assert cis.calls == []


def test_a_real_customer_success_member_still_delivers(desk_app: Any, pools: LocalPools) -> None:
    client, state, _ = desk_app
    state["conn"] = base_conn()
    payload = {"cis_invitation_id": str(REQ), "locale": "en", "email": "editor@agency.example"}
    r = client.post(f"{T}/studio-invitations", json=payload, headers=_bearer(_cs_token(pools, email="cs@example.test")))
    assert r.status_code == 202, r.text


def test_the_console_says_it_cannot_write(desk_app: Any, pools: LocalPools) -> None:
    client, state, _ = desk_app
    state["conn"] = base_conn()
    r = client.get(f"{PREFIX}/console", headers=_bearer(_cs_token(pools)))
    assert r.status_code == 200 and r.json()["can_write"] is False


def test_the_tenant_id_is_the_desk_tenant() -> None:
    assert T.endswith(str(TENANT))
