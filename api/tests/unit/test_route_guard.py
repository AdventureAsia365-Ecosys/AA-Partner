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
from app.settings import Settings, SettingsError, load_settings
from tests.tokens import CIS_PORTAL_CLIENT, EXTERNAL_ISSUER, PARTNER_ISSUER, PARTNER_WEB_CLIENT, LocalPools

PUBLIC_PREFIXES = ("/crm/health", "/crm/ci/", "/crm/public/v1/")


def _probe_routers() -> list[Any]:
    """One probe route per class, mounted only in these tests: proves the router-level guard on a non-empty class."""
    routers = []
    for rc in ROUTE_CLASSES:
        router = class_router(rc)

        @router.get("/_probe")
        async def probe(caller: Annotated[Caller, Depends(current_caller)]) -> dict[str, Any]:
            if caller.route_class == OPS.name:  # AA-941: an ops body passes the L1 key allow-list
                return {"status": "ok", "role": caller.client_kind}
            return {"class": caller.route_class, "client": caller.client_kind, "issuer": caller.issuer_kind}

        @router.post("/_probe_write")
        async def probe_write(caller: Annotated[Caller, Depends(require_group("crm-platform-admin"))]) -> dict:
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
        OPS.name: [pools.partner_web(), pools.breakglass(), pools.crm_staff()],
        STAFF.name: [pools.partner_web(), pools.cis_admin(), pools.breakglass()],
    }[rc.name]
    paths = [(m, p) for m, p in _served_paths(client) if p.startswith(rc.prefix + "/")]
    assert paths, f"no path under {rc.prefix}"
    for method, path in paths:
        assert client.request(method, path).status_code == 401, (method, path)
        for token in foreign:
            r = client.request(method, path, headers=_bearer(token))
            assert (r.status_code, r.json()["detail"]) == (403, "wrong_audience"), (method, path)


def test_agency_accepts_partner_web(client: TestClient, pools: LocalPools) -> None:
    r = client.get("/crm/v1/_probe", headers=_bearer(pools.partner_web()))
    assert r.status_code == 200
    assert r.json() == {"class": "agency", "client": "partner-web", "issuer": "partner"}


@pytest.mark.parametrize("rc", ROUTE_CLASSES, ids=lambda rc: rc.name)
def test_retired_cis_portal_bridge_is_refused_on_every_path(client: TestClient, pools: LocalPools, rc: Any) -> None:
    """AA-979 P2b: the Dev EXTERNAL ``cis-portal`` bridge onto /crm/v1 is gone. A validly signed cis-portal token is
    401 on every served path of every class (its issuer is unknown to the verifier), never accepted."""
    token = pools.cis_portal()
    paths = [(m, p) for m, p in _served_paths(client) if p.startswith(rc.prefix + "/")]
    for method, path in paths:
        r = client.request(method, path, headers=_bearer(token))
        assert (r.status_code, r.json()["detail"]) == (401, "invalid_token"), (method, path)


def test_retired_bridge_env_is_ignored(pools: LocalPools) -> None:
    """The code ships before the Terraform env change: a task that still has COGNITO_EXTERNAL_ISSUER /
    COGNITO_CIS_PORTAL_CLIENT_ID starts (on any environment), ignores them and refuses cis-portal tokens; partner-pool
    tokens keep working."""
    env = {
        "ENVIRONMENT": "dev",
        "COGNITO_PARTNER_ISSUER": PARTNER_ISSUER,
        "COGNITO_PARTNER_WEB_CLIENT_ID": PARTNER_WEB_CLIENT,
        "COGNITO_EXTERNAL_ISSUER": EXTERNAL_ISSUER,
        "COGNITO_CIS_PORTAL_CLIENT_ID": CIS_PORTAL_CLIENT,
    }
    settings = load_settings(env)
    assert not hasattr(settings, "cognito_external_issuer")
    assert not hasattr(settings, "cognito_cis_portal_client_id")
    assert load_settings({**env, "ENVIRONMENT": "prod"}).environment == "prod"  # no "Dev-only bridge" refusal left
    identity = identity_from_settings(settings)
    assert EXTERNAL_ISSUER not in identity.issuers.values()
    assert all(kind != "external" for kind in identity.issuers)
    settings = pools.settings(environment="test")
    app = create_app(settings, verifier=pools.verifier(settings))
    for router in _probe_routers():
        app.include_router(router)
    with TestClient(app) as c:
        r = c.get("/crm/v1/_probe", headers=_bearer(pools.cis_portal()))
        assert (r.status_code, r.json()["detail"]) == (401, "invalid_token")
        assert c.get("/crm/v1/_probe", headers=_bearer(pools.partner_web())).status_code == 200


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
    """No partner pool: /crm/v1 refuses every token."""
    settings = pools.settings(cognito_partner_issuer="", cognito_partner_web_client_id="")
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

    no_keys = identity_config.build_verifier(identity_from_settings(settings), empty)
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


def test_every_ops_write_needs_platform_admin_and_every_read_takes_cis_admin(
    client: TestClient, pools: LocalPools
) -> None:
    """AA-941: on every served /crm/ops/v1 path, a cis-admin-only token reads (never 401 / 403) and is refused every
    POST / PUT with 403 missing_group, before any body is read. AA-979 (Q4): the one exception is the Content studio
    invitation relay, which a cis-admin token may call (CIS requires tenant.write first; it grants no CRM right)."""
    reader = _bearer(pools.cis_admin())
    paths = [(m, p) for m, p in _served_paths(client) if p.startswith(OPS.prefix + "/") and "_probe" not in p]
    studio = [(m, p) for m, p in paths if p.endswith("/studio-invitations")]
    assert studio == [("POST", OPS.prefix + "/tenants/00000000-0000-4000-8000-000000000001/studio-invitations")]
    for method, path in studio:
        r = client.request(method, path, headers=reader)
        assert r.status_code not in (401, 403), (method, path, r.status_code)
    paths = [mp for mp in paths if mp not in studio]
    writes = [(m, p) for m, p in paths if m in ("POST", "PUT", "PATCH", "DELETE")]
    reads = [(m, p) for m, p in paths if m == "GET"]
    assert len(writes) >= 11 and len(reads) >= 10
    for method, path in writes:
        r = client.request(method, path, headers=reader)
        assert (r.status_code, r.json()["detail"]) == (403, "missing_group"), (method, path)
    for method, path in reads:
        r = client.request(method, path, headers=reader)
        assert r.status_code not in (401, 403), (method, path, r.status_code)
