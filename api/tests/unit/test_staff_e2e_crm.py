"""AA-1019 (root ADR 0006a A10c): the Dev-only CRM staff E2E identity at the guard, and the break-glass picker route.

The identity is a WORKFORCE ``crm-staff`` token whose ``email`` claim is :data:`app.auth.e2e_staff.E2E_CRM_STAFF_EMAIL`
(the pre token trigger sets the claim, signed by the pool). Outside ``dev`` / ``test`` / ``local`` the guard refuses it
(403 ``e2e_read_only``) before any route runs; inside, the caller carries ``e2e_synthetic_only`` and the staff contexts
refuse real agencies (integration test ``test_staff_console_api.py``). No database here.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import e2e_staff, identity_config
from app.auth.identity_config import STAFF
from app.auth.route_guard import Caller, class_router, current_caller
from app.main import create_app
from app.routers import staff as staff_router
from tests.tokens import LocalPools

E2E = e2e_staff.E2E_CRM_STAFF_EMAIL


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


def _probe_app(pools: LocalPools, environment: str) -> FastAPI:
    """A one-route app on the STAFF class: the guard alone, the caller echoed back (flags only)."""
    settings = pools.settings(environment=environment, db_ssl="require")
    app = FastAPI()
    app.state.settings = settings
    app.state.verifier = pools.verifier(settings)
    app.state.identity = identity_config.identity_from_settings(settings)
    router = class_router(STAFF)

    @router.get("/probe")
    async def probe(caller: Caller = Depends(current_caller)) -> dict[str, bool]:  # noqa: B008
        return {"e2e_synthetic_only": caller.e2e_synthetic_only, "e2e_read_only": caller.e2e_read_only}

    app.include_router(router)
    return app


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_the_address_matches_exactly() -> None:
    assert e2e_staff.is_e2e_crm_staff(E2E) and e2e_staff.is_e2e_crm_staff(f" {E2E.upper()} ")
    for other in (None, "", "e2e-crm-staff@e2e-mail.lumiguides.it.com.evil", "x" + E2E, e2e_staff.E2E_CIS_ADMIN_EMAIL):
        assert not e2e_staff.is_e2e_crm_staff(other)
    # Not an invitee address of the studio relay either.
    assert not e2e_staff.is_e2e_mailbox_address(E2E)


@pytest.mark.parametrize("environment", ["dev", "test", "local"])
def test_the_identity_is_flagged_synthetic_only_on_dev(pools: LocalPools, environment: str) -> None:
    with TestClient(_probe_app(pools, environment)) as c:
        token = pools.crm_staff(["crm-ops", "crm-support"], email=E2E)
        res = c.get(f"{STAFF.prefix}/probe", headers=_bearer(token))
        assert res.status_code == 200 and res.json() == {"e2e_synthetic_only": True, "e2e_read_only": False}
        # A real staff member is not flagged.
        res = c.get(f"{STAFF.prefix}/probe", headers=_bearer(pools.crm_staff(["crm-ops"], email="ops@example.test")))
        assert res.json() == {"e2e_synthetic_only": False, "e2e_read_only": False}


@pytest.mark.parametrize("environment", ["stg", "prod"])
def test_the_identity_is_refused_anywhere_else(pools: LocalPools, environment: str) -> None:
    with TestClient(_probe_app(pools, environment)) as c:
        res = c.get(f"{STAFF.prefix}/probe", headers=_bearer(pools.crm_staff(["crm-ops"], email=E2E.upper())))
        assert (res.status_code, res.json()["detail"]) == (403, "e2e_read_only")
        res = c.get(f"{STAFF.prefix}/probe", headers=_bearer(pools.crm_staff(["crm-ops"], email="ops@example.test")))
        assert res.status_code == 200


@pytest.fixture
def client(pools: LocalPools) -> Iterator[TestClient]:
    settings = pools.settings()
    with TestClient(create_app(settings, verifier=pools.verifier(settings))) as c:
        yield c


def test_the_breakglass_picker_is_crm_ops_only(client: TestClient, pools: LocalPools) -> None:
    res = client.get("/crm/staff/v1/breakglass/agencies", headers=_bearer(pools.crm_staff(["crm-support"])))
    assert (res.status_code, res.json()["detail"]) == (403, "missing_group")
    res = client.get("/crm/staff/v1/breakglass/agencies", headers=_bearer(pools.partner_web()))
    assert (res.status_code, res.json()["detail"]) == (403, "wrong_audience")


def test_the_picker_answers_ids_names_and_status_only() -> None:
    fields = set(staff_router.BreakglassAgency.model_fields)
    assert fields == {"tenant_id", "agency_name", "agency_status", "my_session_expires_at"}
    assert staff_router.BreakglassAgency.model_config.get("extra") == "forbid"
