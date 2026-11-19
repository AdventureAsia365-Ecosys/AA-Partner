"""AA-1139 fix pass: the TripPlanner half of the Content studio's Trip planner page (app.routers.studio_tripplanner),
for an agency WITHOUT the CRM add-on (no CRM agency, no crm.member row). The caller's partner-pool token reaches the
API; CIS ``tenant.membership.v1`` (app.cis.studio_member) says which active studio membership it holds in the tenant.

No database is touched (the fake connection would fail any SQL). Invented names only; no traveller field.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.cis import studio_member
from app.cis.http import HttpError, HttpResponse
from app.main import create_app
from tests.tokens import PARTNER_ISSUER, SUB, LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa1139_tripplanner_module import KEY_INFO, LEADS, USAGE, FakeTokens
from tests.unit.test_tenancy import FakeDatabase

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000022a1")
OTHER = uuid.UUID("00000000-0000-4000-8000-0000000022a2")
MEMBERSHIP = uuid.UUID("00000000-0000-4000-8000-0000000022b1")
KEY = hashlib.sha256(b"synthetic studio rotation").hexdigest()  # a 64-hex value, not a real key


class Remote:
    """A client config (``base_url``, ``tokens``, ``transport``, ``timeout_s``) with a scripted transport."""

    def __init__(self, base: str, *responses: HttpResponse | Exception, fail_token: bool = False) -> None:
        self.base_url = base
        self.tokens = FakeTokens(fail_token)
        self.timeout_s = 5.0
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Any]] = []

    async def transport(self, method: str, url: str, headers: Any, body: bytes | None, timeout_s: float):
        self.calls.append((method, url, json.loads(body) if body else None))
        assert headers["Authorization"] == "Bearer m2m-token" and timeout_s == 5.0
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(status: int, body: Any) -> HttpResponse:
    return HttpResponse(status, json.dumps(body).encode(), {})


def _memberships(*rows: tuple[uuid.UUID, str, str], iss: str = PARTNER_ISSUER, sub: str = SUB) -> HttpResponse:
    items = [
        {
            "membership_id": str(MEMBERSHIP),
            "tenant_id": str(tenant),
            "content_studio_role": role,
            "status": status,
            "updated_at": "2026-11-18T09:00:00+00:00",
        }
        for tenant, role, status in rows
    ]
    return _resp(200, {"identity": {"iss": iss, "sub": sub}, "memberships": items})


def _client(cis: Remote | None, planner: Remote | None) -> tuple[TestClient, dict[str, str], ScriptedConn]:
    pools = LocalPools()
    settings = pools.settings()
    conn = ScriptedConn()  # only the app's settings read at start-up; never a membership or tenant row
    app = create_app(settings, FakeDatabase(conn, settings), pools.verifier(settings))
    app.state.studio_activation = cis
    app.state.tp_planner = planner
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers, conn


def _agency_reads(conn: ScriptedConn) -> list[str]:
    return [flat for flat, _ in conn.calls if "crm.member" in flat or "crm.tenant" in flat or "crm.audit" in flat]


# ── the CIS membership lookup ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_lookup_asks_for_the_identity_with_the_read_scope() -> None:
    cis = Remote("https://cis.example.invalid", _memberships((OTHER, "owner", "active"), (TENANT, "editor", "active")))
    member = await studio_member.lookup(cis, PARTNER_ISSUER, SUB, TENANT)
    assert member == studio_member.StudioMember(MEMBERSHIP, "editor")
    assert cis.tokens.scopes == ["cis.tenant/read"]
    method, url, body = cis.calls[0]
    parsed = urllib.parse.urlparse(url)
    assert (method, parsed.path, body) == ("GET", "/tenant/membership/v1/memberships", None)
    assert urllib.parse.parse_qs(parsed.query) == {"iss": [PARTNER_ISSUER], "sub": [SUB]}


@pytest.mark.asyncio
async def test_only_an_active_membership_of_that_tenant_counts() -> None:
    cis = Remote(
        "https://cis.example.invalid",
        _memberships((TENANT, "owner", "invited"), (OTHER, "owner", "active")),
        _memberships(),
    )
    assert await studio_member.lookup(cis, PARTNER_ISSUER, SUB, TENANT) is None
    assert await studio_member.lookup(cis, PARTNER_ISSUER, SUB, TENANT) is None


@pytest.mark.parametrize(
    "response",
    [
        _memberships((TENANT, "owner", "active"), sub="another-person"),
        _memberships((TENANT, "admin", "active")),
        _resp(200, {"identity": {"iss": PARTNER_ISSUER, "sub": SUB}, "memberships": "all"}),
        _resp(200, ["not", "an", "object"]),
        HttpResponse(200, b"not json", {}),
        _resp(500, {}),
        _resp(403, {"error": "forbidden"}),
        HttpError("timeout"),
    ],
)
@pytest.mark.asyncio
async def test_anything_else_fails_closed(response: Any) -> None:
    cis = Remote("https://cis.example.invalid", response)
    with pytest.raises(HTTPException) as err:
        await studio_member.lookup(cis, PARTNER_ISSUER, SUB, TENANT)
    assert (err.value.status_code, err.value.detail) == (503, "cis_unavailable")


@pytest.mark.asyncio
async def test_a_401_drops_the_token_and_no_config_or_token_is_unavailable() -> None:
    cis = Remote("https://cis.example.invalid", _resp(401, {}))
    with pytest.raises(HTTPException):
        await studio_member.lookup(cis, PARTNER_ISSUER, SUB, TENANT)
    assert cis.tokens.invalidated == ["cis.tenant/read"]
    for config in (None, Remote("https://cis.example.invalid", fail_token=True)):
        with pytest.raises(HTTPException) as err:
            await studio_member.lookup(config, PARTNER_ISSUER, SUB, TENANT)
        assert err.value.status_code == 503


# ── the routes ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _cis(role: str = "owner", status: str = "active") -> Remote:
    return Remote("https://cis.example.invalid", _memberships((TENANT, role, status)))


def _tp(*responses: Any) -> Remote:
    return Remote("https://tp.example.invalid", *responses)


@pytest.mark.parametrize("role", ["owner", "editor"])
def test_the_owner_and_an_editor_read_usage_key_and_leads(role: str) -> None:
    planner = _tp(_resp(200, USAGE), _resp(200, KEY_INFO), _resp(200, LEADS))
    cis = Remote(
        "https://cis.example.invalid", _memberships((TENANT, role, "active")), _memberships((TENANT, role, "active"))
    )
    client, headers, conn = _client(cis, planner)
    with client:
        page = client.get("/crm/v1/studio/trip-planner", headers=headers)
        leads = client.get("/crm/v1/studio/trip-planner/leads", headers=headers)
    assert page.status_code == 200 and page.headers["Cache-Control"] == "no-store"
    assert page.json() == {"usage": USAGE, "webhook_key": KEY_INFO}
    assert leads.status_code == 200 and leads.json() == LEADS
    assert [c[1].rsplit("/planner/", 1)[1] for c in planner.calls] == ["usage", "webhook-key", "leads?limit=50"]
    assert all(f"/tenants/{TENANT}/" in c[1] for c in planner.calls)
    assert _agency_reads(conn) == []  # no CRM agency is needed (there may be none at all)


def test_tripplanner_down_is_null_parts() -> None:
    client, headers, _ = _client(_cis(), _tp(HttpError("timeout"), _resp(500, {})))
    with client:
        r = client.get("/crm/v1/studio/trip-planner", headers=headers)
    assert r.status_code == 200 and r.json() == {"usage": None, "webhook_key": None}


@pytest.mark.parametrize(
    ("cis", "detail"),
    [
        (_cis("viewer"), "role_forbidden"),
        (_cis("owner", "invited"), "not_studio_member"),
        (Remote("https://cis.example.invalid", _memberships((OTHER, "owner", "active"))), "not_studio_member"),
    ],
)
def test_a_viewer_or_a_stranger_reads_nothing_and_tripplanner_is_not_asked(cis: Remote, detail: str) -> None:
    planner = _tp()
    client, headers, _ = _client(cis, planner)
    with client:
        r = client.get("/crm/v1/studio/trip-planner", headers=headers)
    assert (r.status_code, r.json()["detail"]) == (403, detail)
    assert planner.calls == []


def test_cis_down_is_503_and_tripplanner_is_not_asked() -> None:
    planner = _tp()
    client, headers, _ = _client(Remote("https://cis.example.invalid", HttpError("timeout")), planner)
    with client:
        r = client.post("/crm/v1/studio/trip-planner/webhook-key", headers=headers)
    assert (r.status_code, r.json()["detail"]) == (503, "cis_unavailable") and planner.calls == []


def test_the_owner_rotates_as_its_studio_membership_and_the_key_is_answered_once() -> None:
    planner = _tp(_resp(200, {"version": 1, "rotated_at": "2026-11-18T09:00:00+00:00", "key": KEY}))
    client, headers, conn = _client(_cis("owner"), planner)
    with client:
        r = client.post("/crm/v1/studio/trip-planner/webhook-key", headers=headers)
    assert r.status_code == 200 and r.json()["key"] == KEY and r.headers["Cache-Control"] == "no-store"
    assert planner.calls == [
        (
            "POST",
            f"https://tp.example.invalid/trip/m2m/v1/tenants/{TENANT}/planner/webhook-key/rotate",
            {"actor": f"studio-member:{MEMBERSHIP}"},
        )
    ]
    assert _agency_reads(conn) == []


def test_an_editor_rotates_nothing() -> None:
    planner = _tp()
    client, headers, _ = _client(_cis("editor"), planner)
    with client:
        r = client.post("/crm/v1/studio/trip-planner/webhook-key", headers=headers)
    assert (r.status_code, r.json()["detail"]) == (403, "role_forbidden") and planner.calls == []


def test_no_signing_secret_is_503_with_its_code() -> None:
    client, headers, _ = _client(_cis("owner"), _tp(_resp(503, {"error": "webhook_key_unavailable"})))
    with client:
        r = client.post("/crm/v1/studio/trip-planner/webhook-key", headers=headers)
    assert (r.status_code, r.json()["detail"]) == (503, "webhook_key_unavailable")


def test_no_tenant_header_and_a_staff_token_are_refused_before_cis() -> None:
    pools = LocalPools()
    cis = _cis()
    client, headers, _ = _client(cis, _tp())
    with client:
        no_tenant = client.get("/crm/v1/studio/trip-planner", headers={"Authorization": headers["Authorization"]})
        staff = client.get(
            "/crm/v1/studio/trip-planner",
            headers={"Authorization": f"Bearer {pools.cis_admin()}", "X-AA-Tenant-Id": str(TENANT)},
        )
    assert no_tenant.status_code == 400 and staff.status_code in (401, 403)
    assert cis.calls == []
