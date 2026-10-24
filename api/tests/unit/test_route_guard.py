"""Route classes and their token-audience guards (root ADR 0006b decision 6; card CRM-05 R test).

R: a ``cis-admin`` or ``breakglass`` token on any ``/crm/v1`` route -> 403. Checked on every path the app serves under
each class prefix (read from the app's OpenAPI document, so a new route is covered without editing this file), and on
a probe route mounted on each class router.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Annotated, Any

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app.auth.identity_config import AGENCY, OPS, ROUTE_CLASSES, STAFF, identity_from_settings
from app.auth.route_guard import Caller, class_router, current_caller, require_group
from app.main import create_app
from app.settings import Settings, SettingsError
from tests.tokens import LocalPools

PUBLIC_PREFIXES = ("/crm/health", "/crm/ci/", "/crm/public/v1/")


def _probe_routers() -> list[Any]:
    """One probe route per class, mounted only in these tests: proves the router-level guard on a non-empty class."""
    routers = []
    for rc in ROUTE_CLASSES:
        router = class_router(rc)

        @router.get("/_probe")
        async def probe(caller: Annotated[Caller, Depends(current_caller)]) -> dict[str, Any]:
            return {"class": caller.route_class, "client": caller.client_kind, "issuer": caller.issuer_kind}

        @router.post("/_probe_write")
        async def probe_write(caller: Annotated[Caller, Depends(require_group("crm-platform-admin"))]) -> dict:
            return {"ok": True}

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
    with TestClient(app) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _served_paths(client: TestClient) -> list[tuple[str, str]]:
    """(method, concrete path) for every operation in the app, path parameters filled with a dummy UUID."""
    spec = client.app.openapi()
    out = []
    for path, ops in spec["paths"].items():
        concrete = re.sub(r"\{[^}]+\}", "00000000-0000-4000-8000-000000000001", path)
        for method in ops:
            out.append((method.upper(), concrete))
    return out


def test_every_served_path_is_public_or_in_a_route_class(client: TestClient) -> None:
    prefixes = PUBLIC_PREFIXES + tuple(rc.prefix + "/" for rc in ROUTE_CLASSES)
    stray = [p for _m, p in _served_paths(client) if not p.startswith(prefixes)]
    assert stray == []


@pytest.mark.parametrize("rc", ROUTE_CLASSES, ids=lambda rc: rc.name)
def test_every_class_path_refuses_no_token_and_foreign_tokens(client: TestClient, pools: LocalPools, rc: Any) -> None:
    foreign = {
        AGENCY.name: [pools.cis_admin(), pools.breakglass(), pools.crm_staff()],
        OPS.name: [pools.partner_web(), pools.cis_portal(), pools.breakglass(), pools.crm_staff()],
        STAFF.name: [pools.partner_web(), pools.cis_admin(), pools.breakglass()],
    }[rc.name]
    paths = [(m, p) for m, p in _served_paths(client) if p.startswith(rc.prefix + "/")]
    assert paths, f"no path under {rc.prefix}"
    for method, path in paths:
        assert client.request(method, path).status_code == 401, (method, path)
        for token in foreign:
            r = client.request(method, path, headers=_bearer(token))
            assert (r.status_code, r.json()["detail"]) == (403, "wrong_audience"), (method, path)


def test_agency_accepts_partner_web_and_dev_portal_bridge(client: TestClient, pools: LocalPools) -> None:
    for token, issuer, kind in (
        (pools.partner_web(), "partner", "partner-web"),
        (pools.cis_portal(), "external", "cis-portal"),
    ):
        r = client.get("/crm/v1/_probe", headers=_bearer(token))
        assert r.status_code == 200
        assert r.json() == {"class": "agency", "client": kind, "issuer": issuer}


def test_ops_needs_a_group_and_writes_need_platform_admin(client: TestClient, pools: LocalPools) -> None:
    assert client.get("/crm/ops/v1/_probe", headers=_bearer(pools.cis_admin(groups=[]))).json() == {
        "detail": "missing_group"
    }
    assert client.get("/crm/ops/v1/_probe", headers=_bearer(pools.cis_admin())).status_code == 200
    r = client.post("/crm/ops/v1/_probe_write", headers=_bearer(pools.cis_admin()))
    assert (r.status_code, r.json()["detail"]) == (403, "missing_group")
    r = client.post("/crm/ops/v1/_probe_write", headers=_bearer(pools.cis_admin(groups=["crm-platform-admin"])))
    assert r.status_code == 200


def test_staff_needs_crm_ops_or_support(client: TestClient, pools: LocalPools) -> None:
    assert client.get("/crm/staff/v1/_probe", headers=_bearer(pools.crm_staff())).status_code == 200
    assert client.get("/crm/staff/v1/_probe", headers=_bearer(pools.crm_staff(["crm-support"]))).status_code == 200
    r = client.get("/crm/staff/v1/_probe", headers=_bearer(pools.crm_staff(["cis-admin"])))
    assert (r.status_code, r.json()["detail"]) == (403, "missing_group")


def test_bad_tokens_are_401_not_403(client: TestClient, pools: LocalPools) -> None:
    cases = [
        "not-a-jwt",
        pools.partner_web(exp_in=-120),  # expired
        pools.partner.token("unknownclient000000000001"),  # client not configured
        pools.partner.token("partnerwebclient0000000001", token_use="id"),  # ID token
    ]
    for token in cases:
        r = client.get("/crm/v1/_probe", headers=_bearer(token))
        assert (r.status_code, r.json()["detail"]) == (401, "invalid_token")


def test_unconfigured_class_accepts_nothing(pools: LocalPools) -> None:
    """No partner pool and no portal bridge (a non-dev environment before AA-939): /crm/v1 refuses every token."""
    settings = pools.settings(
        cognito_partner_issuer="", cognito_partner_web_client_id="", cognito_external_issuer="",
        cognito_cis_portal_client_id="",
    )  # fmt: skip
    app = create_app(settings, verifier=pools.verifier(settings))
    for router in _probe_routers():
        app.include_router(router)
    with TestClient(app) as c:
        assert c.get("/crm/v1/_probe", headers=_bearer(pools.partner_web())).status_code == 401
        assert c.get("/crm/v1/_probe", headers=_bearer(pools.cis_admin())).status_code == 403


def test_nothing_configured_means_401(pools: LocalPools) -> None:
    app = create_app(Settings(environment="test", db_ssl="disable"))
    assert app.state.verifier is None
    with TestClient(app) as c:
        assert c.get("/crm/v1/me", headers=_bearer(pools.partner_web())).status_code == 401


def test_portal_bridge_is_dev_only() -> None:
    with pytest.raises(SettingsError, match="Dev-only"):
        Settings(environment="prod", cognito_cis_portal_client_id="cisportalclient00000000001")


def test_workforce_client_ids_must_differ(pools: LocalPools) -> None:
    with pytest.raises(SettingsError, match="must differ"):
        pools.settings(cognito_crm_staff_client_id="cisadminclient000000000001")


def test_identity_skips_half_configured_pairs(pools: LocalPools) -> None:
    identity = identity_from_settings(pools.settings(cognito_partner_web_client_id=""))
    assert "partner" not in identity.issuers
    assert identity.client_kind("workforce", "breakglassclient0000000001") == "breakglass"


@pytest.mark.asyncio
async def test_jwks_prefetch_never_raises(pools: LocalPools) -> None:
    import asyncio

    from app.auth import identity_config

    settings = pools.settings(environment="local")
    verifier = pools.verifier(settings)
    counts = await identity_config.prefetch_jwks(verifier)
    assert set(counts.values()) == {1}
    task = identity_config.start_jwks_prefetch(verifier, settings)
    assert task is not None
    await identity_config.stop_jwks_prefetch(task)
    assert identity_config.start_jwks_prefetch(verifier, pools.settings()) is None  # test env: no network
    assert identity_config.start_jwks_prefetch(None, settings) is None

    class Broken:
        async def prefetch(self) -> dict:
            raise OSError("down")

    assert await identity_config.prefetch_jwks(Broken()) == {}  # type: ignore[arg-type]

    async def empty(url: str) -> dict:
        return {"keys": []}

    no_keys = identity_config.build_verifier(identity_config.identity_from_settings(settings), empty)
    assert set((await identity_config.prefetch_jwks(no_keys)).values()) == {0}
    await asyncio.sleep(0)


def test_identity_refuses_one_url_for_two_pools(pools: LocalPools) -> None:
    from app.auth.identity_config import identity_from_settings
    from tests.tokens import WORKFORCE_ISSUER

    with pytest.raises(ValueError, match="share one issuer URL"):
        identity_from_settings(pools.settings(cognito_partner_issuer=WORKFORCE_ISSUER))


def test_require_group_outside_a_class_router_is_401(pools: LocalPools) -> None:
    from fastapi import APIRouter

    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings))
    stray = APIRouter()

    @stray.get("/crm/stray")
    async def stray_route(caller: Annotated[Caller, Depends(current_caller)]) -> dict:
        return {}

    app.include_router(stray)
    with TestClient(app) as c:
        assert c.get("/crm/stray").status_code == 401


def test_unrouted_paths_under_a_class_prefix_are_404_without_a_token(pools: LocalPools) -> None:
    """FastAPI runs a router's dependencies only for a matched route, so a path no route serves answers 404 before any
    guard runs (review round, AA-933). The live checks therefore expect 404 on the still-empty ``/crm/ops/v1`` and
    ``/crm/staff/v1`` (their first routes arrive with CRM-13a / CRM-26) and 401 / 403 only on ``/crm/v1/me``."""
    settings = pools.settings()
    with TestClient(create_app(settings, verifier=pools.verifier(settings))) as c:
        for path in ("/crm/ops/v1/x", "/crm/staff/v1/x", "/crm/v1/x"):
            assert c.get(path).status_code == 404, path
        assert c.get("/crm/v1/me").status_code == 401
        r = c.get("/crm/v1/me", headers=_bearer(pools.cis_admin()))
        assert (r.status_code, r.json()["detail"]) == (403, "wrong_audience")
