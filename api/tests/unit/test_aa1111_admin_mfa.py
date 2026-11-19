"""AA-1111 slice S2 (root ADR 0006c, crm-082) — app/auth/admin_mfa.py through the real route guard.

A WORKFORCE session holding an operations group (crm-platform-admin, crm-ops, cis-admin) whose access token's amr lacks
"otp" is refused 403 mfa_required only while crm.platform.admin_mfa_required is on; off it is served and logged once
(would_refuse). The desk groups, agency members (PARTNER pool) and the Dev-only E2E identities are never checked.
Local pools only: no network, no AWS, no DB.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Annotated, Any

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import platform_settings as ps
from app.auth import admin_mfa, e2e_staff
from app.auth.identity_config import ROUTE_CLASSES
from app.auth.route_guard import Caller, class_router, current_caller
from app.main import create_app
from tests.tokens import LocalPools

TOTP_AMR = ["pwd", "otp", "mfa"]
EMAIL_CODE_AMR = ["email_otp"]


def _probe_routers() -> list[Any]:
    routers = []
    for rc in ROUTE_CLASSES:
        router = class_router(rc)

        @router.get("/_mfa_probe")
        async def probe(caller: Annotated[Caller, Depends(current_caller)]) -> dict[str, Any]:
            return {"status": "ok"}

        routers.append(router)
    return routers


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


@pytest.fixture
def client(pools: LocalPools) -> Iterator[TestClient]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings))
    for router in _probe_routers():
        app.include_router(router)
    admin_mfa.clear_log_throttle()
    with TestClient(app) as c:
        yield c
    admin_mfa.clear_log_throttle()


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    state = {"on": False}
    real_get = ps.get

    def fake_get(key: str, tenant_id: Any = None) -> Any:
        if key == ps.ADMIN_MFA_REQUIRED:
            return state["on"]
        return real_get(key, tenant_id)

    monkeypatch.setattr(ps, "get", fake_get)
    return state


def _get(client: TestClient, prefix: str, token: str) -> Any:
    return client.get(f"{prefix}/_mfa_probe", headers={"Authorization": f"Bearer {token}"})


def test_the_setting_is_compiled_off_with_its_guide() -> None:
    d = ps.definition(ps.ADMIN_MFA_REQUIRED)
    assert (d.value_type, d.default_value, d.area, d.approval, d.impact) == (
        "bool",
        False,
        "platform",
        "maker_checker",
        "high",
    )
    assert d.env_override is None and d.enforced is True
    assert admin_mfa.required() is False


@pytest.mark.parametrize(
    "value, expected",
    [(TOTP_AMR, ("pwd", "otp", "mfa")), ("otp", ("otp",)), ([1, "otp"], ("otp",)), ({"otp": True}, ())],
)
def test_token_amr_reads_the_claim_shapes(pools: LocalPools, value: Any, expected: tuple[str, ...]) -> None:
    assert admin_mfa.token_amr(pools.crm_staff(amr=value)) == expected


def test_token_amr_absent_or_malformed_is_empty(pools: LocalPools) -> None:
    assert admin_mfa.token_amr(pools.crm_staff()) == ()
    assert admin_mfa.token_amr("not-a-jwt") == ()
    assert admin_mfa.token_amr(None) == ()


def test_only_otp_counts_as_mfa() -> None:
    assert admin_mfa.mfa_used(("pwd", "otp", "mfa"))
    assert not admin_mfa.mfa_used(("pwd", "email_otp", "mfa"))
    assert not admin_mfa.mfa_used(())


@pytest.mark.parametrize(
    "kind, groups, prefix",
    [
        ("crm_staff", ["crm-ops"], "/crm/staff/v1"),
        ("cis_admin", ["cis-admin"], "/crm/ops/v1"),
        ("cis_admin", ["cis-admin", "crm-platform-admin"], "/crm/ops/v1"),
    ],
)
def test_switch_on_refuses_an_operations_session_without_the_code(
    client: TestClient, pools: LocalPools, switch: dict[str, bool], kind: str, groups: list[str], prefix: str
) -> None:
    switch["on"] = True
    mint = getattr(pools, kind)
    refused = _get(client, prefix, mint(groups, amr=EMAIL_CODE_AMR))
    assert refused.status_code == 403 and refused.json() == {"detail": "mfa_required"}
    assert _get(client, prefix, mint(groups)).status_code == 403  # no amr at all
    assert _get(client, prefix, mint(groups, amr=TOTP_AMR)).status_code == 200


def test_switch_off_serves_and_logs_would_refuse_once(
    client: TestClient, pools: LocalPools, switch: dict[str, bool], caplog: pytest.LogCaptureFixture
) -> None:
    token = pools.crm_staff(amr=EMAIL_CODE_AMR)
    with caplog.at_level(logging.INFO, logger="app.auth.admin_mfa"):
        assert _get(client, "/crm/staff/v1", token).status_code == 200
        assert _get(client, "/crm/staff/v1", token).status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.name == "app.auth.admin_mfa"]
    assert len(lines) == 1 and "outcome=would_refuse" in lines[0] and "email_otp" in lines[0]


def test_switch_on_leaves_the_desk_groups_and_agency_members_alone(
    client: TestClient, pools: LocalPools, switch: dict[str, bool]
) -> None:
    switch["on"] = True
    assert _get(client, "/crm/staff/v1", pools.crm_staff(["crm-support"], amr=EMAIL_CODE_AMR)).status_code == 200
    assert _get(client, "/crm/ops/v1", pools.cis_admin(["cis-support"], amr=EMAIL_CODE_AMR)).status_code == 200
    assert not admin_mfa.is_admin_session("partner", ("crm-ops",), e2e=False)


def test_dev_only_e2e_identities_are_never_checked() -> None:
    assert admin_mfa.is_admin_session("workforce", ("crm-ops",), e2e=False)
    assert not admin_mfa.is_admin_session("workforce", ("crm-ops",), e2e=True)
    admin_mfa.check("workforce", "iss", "sub", ("cis-admin",), "not-a-jwt", e2e=True)  # no raise
    assert e2e_staff.is_e2e_crm_staff  # the guard passes these flags (route_guard.guard_for)
