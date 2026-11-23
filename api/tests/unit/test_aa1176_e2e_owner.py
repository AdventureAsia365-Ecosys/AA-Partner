"""AA-1176 (root ADR 0006a A10h): the Dev-only E2E owner as the CRM sees it.

A WORKFORCE ``cis-admin`` client token with exactly ``cis-admin`` + ``booking-admin`` whose ``email`` claim is
:data:`app.auth.e2e_staff.E2E_OWNER_EMAIL`. On ``/crm/ops/v1`` it is a plain ``cis-admin`` reader (not read-only, not
no-write: CIS relays its token for the Compliance Refresh evidence), exempt from the administrator MFA switch, refused
outside ``dev`` / ``test`` / ``local``, with any other group set and as a non-ASCII look-alike (403 ``e2e_read_only``);
it holds no ``crm-platform-admin``, so a CRM write needing it is refused like for any CIS admin. Never a studio invitee.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import platform_settings as ps
from app.auth import admin_mfa, e2e_staff, identity_config
from app.auth.identity_config import GROUP_CRM_PLATFORM_ADMIN, OPS
from app.auth.route_guard import Caller, class_router, current_caller, require_group
from tests.tokens import LocalPools

OWNER = e2e_staff.E2E_OWNER_EMAIL
GROUPS = ["cis-admin", "booking-admin"]
EMAIL_CODE_AMR = ["email_otp"]


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    state = {"on": False}
    real_get = ps.get

    def fake_get(key: str, tenant_id: Any = None) -> Any:
        if key == ps.ADMIN_MFA_REQUIRED:
            return state["on"]
        return real_get(key, tenant_id)

    monkeypatch.setattr(ps, "get", fake_get)
    admin_mfa.clear_log_throttle()
    return state


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(pools: LocalPools, groups: list[str] | None = None, email: str = OWNER, **extra: Any) -> str:
    return pools.cis_admin(GROUPS if groups is None else groups, email=email, **extra)


def _probe_app(pools: LocalPools, environment: str, seen: list | None = None) -> FastAPI:
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


def test_the_address_and_the_invitee_rule() -> None:
    assert OWNER == "e2e-owner@e2e-mail.lumiguides.it.com"
    assert e2e_staff.E2E_OWNER_GROUPS == frozenset(GROUPS)
    assert e2e_staff.is_e2e_owner_like(OWNER) and e2e_staff.is_e2e_owner_like(f" {OWNER.upper()} ")
    assert not e2e_staff.is_e2e_owner_like("e2e-owner2@e2e-mail.lumiguides.it.com")
    assert not e2e_staff.is_e2e_mailbox_address(OWNER)
    assert not e2e_staff.is_e2e_cis_admin(OWNER) and not e2e_staff.is_e2e_cis_cs(OWNER)


@pytest.mark.parametrize("environment", ["dev", "test", "local"])
def test_on_dev_it_is_a_plain_cis_admin_reader(pools: LocalPools, environment: str, switch: dict[str, bool]) -> None:
    seen: list = []
    with TestClient(_probe_app(pools, environment, seen)) as c:
        assert c.get(f"{OPS.prefix}/probe", headers=_bearer(_token(pools))).status_code == 200
        res = c.post(f"{OPS.prefix}/write", headers=_bearer(_token(pools)))
        assert (res.status_code, res.json()["detail"]) == (403, "missing_group")
    assert seen == [{"e2e_no_write": False, "e2e_read_only": False}]


@pytest.mark.parametrize("environment", ["stg", "prod"])  # Settings refuses any other value
def test_it_is_refused_anywhere_else(pools: LocalPools, environment: str, switch: dict[str, bool]) -> None:
    with TestClient(_probe_app(pools, environment)) as c:
        res = c.get(f"{OPS.prefix}/probe", headers=_bearer(_token(pools)))
        assert (res.status_code, res.json()["detail"]) == (403, "e2e_read_only")
        assert (
            c.get(f"{OPS.prefix}/probe", headers=_bearer(_token(pools, email="boss@example.test"))).status_code == 200
        )


@pytest.mark.parametrize(
    "groups",
    [["cis-admin"], [*GROUPS, GROUP_CRM_PLATFORM_ADMIN], [*GROUPS, "cis-content"], ["cis-admin", "booking-finance"]],
)
def test_any_other_group_set_is_refused(pools: LocalPools, groups: list[str], switch: dict[str, bool]) -> None:
    with TestClient(_probe_app(pools, "dev")) as c:
        res = c.get(f"{OPS.prefix}/probe", headers=_bearer(_token(pools, groups)))
        assert (res.status_code, res.json()["detail"]) == (403, "e2e_read_only")


def test_the_scope_check_needs_ascii_and_the_exact_groups() -> None:
    assert e2e_staff.e2e_owner_allowed(OWNER, GROUPS) and e2e_staff.e2e_owner_allowed(f" {OWNER} ", set(GROUPS))
    assert not e2e_staff.e2e_owner_allowed(OWNER + "\u200b", GROUPS)
    assert not e2e_staff.e2e_owner_allowed(OWNER, ["cis-admin"])
    assert not e2e_staff.e2e_owner_allowed(None, GROUPS)


def test_the_mfa_switch_on_does_not_refuse_it(pools: LocalPools, switch: dict[str, bool]) -> None:
    switch["on"] = True
    with TestClient(_probe_app(pools, "dev")) as c:
        assert c.get(f"{OPS.prefix}/probe", headers=_bearer(_token(pools, amr=EMAIL_CODE_AMR))).status_code == 200
        res = c.get(
            f"{OPS.prefix}/probe",
            headers=_bearer(_token(pools, ["cis-admin"], email="boss@example.test", amr=EMAIL_CODE_AMR)),
        )
        assert (res.status_code, res.json()["detail"]) == (403, "mfa_required")
