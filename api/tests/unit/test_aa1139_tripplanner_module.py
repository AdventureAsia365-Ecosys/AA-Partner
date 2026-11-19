"""AA-1139: the agency app's TripPlanner module (app.routers.tripplanner_module) relays to CIS ``tenant.addons.v1`` 1.1
(app.cis.tripplanner) and TripPlanner ``lead.submitted.v1`` 1.1 (app.tripplanner.planner).

The CIS contract copy (``docs/contracts/tenant.addons.v1.openapi.yaml``, pinned in test_aa1036_crm_addons.py) gives the
field lists compared here. Invented names only; no traveller field ever crosses.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.cis import tripplanner as cis_tp
from app.cis.http import HttpError, HttpResponse
from app.cis.m2m import TokenError
from app.main import create_app
from app.tripplanner import planner as tp
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.test_tenancy import FakeDatabase

SPEC = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "tenant.addons.v1.openapi.yaml"
TENANT = uuid.UUID("00000000-0000-4000-8000-0000000011a1")
MEMBER = uuid.UUID("00000000-0000-4000-8000-0000000011b1")
LEAD = "00000000-0000-4000-8000-0000000011c1"
KEY = hashlib.sha256(b"synthetic rotation").hexdigest()  # a 64-hex value, not a real key
MODULE: dict[str, Any] = {
    "active": True,
    "in_service": True,
    "planner": {
        "url": "https://planner.example.invalid/brindle-coast/plan",
        "domains": ["plan.brindlecoast.example.com"],
        "launcher": True,
    },
    "domains": [
        {"domain": "plan.brindlecoast.example.com", "status": "active"},
        {"domain": "trips.brindlecoast.example.com", "status": "validating"},
    ],
    "crm_core": False,
    "lead_route": {
        "lead_channel": "email",
        "lead_webhook_url": None,
        "lead_email": "desk@brindlecoast.example.com",
        "effective_channel": "email",
        "updated_at": "2026-11-18T09:00:00+00:00",
    },
    "theme": {"accent": "#2a6f97"},
    "locale": "vi",
    "ai_sessions_monthly": 300,
}
USAGE = {"month": "2026-11", "ai_allowance": {"used": 17, "quota": 300}, "leads_24h": {"count": 2, "cap": 200}}
KEY_INFO = {"version": 0, "rotated_at": None, "available": True}
LEADS = {
    "leads": [
        {
            "lead_id": LEAD,
            "created_at": "2026-11-18T09:00:00+00:00",
            "channel": "email",
            "status": "retrying",
            "attempts": 1,
            "delivered_at": None,
            "error": "email_clienterror",
        }
    ],
    "month": {"total": 1, "pending": 0, "retrying": 1, "delivered": 0, "rejected": 0, "expired": 0},
}


def _schema_block(name: str) -> str:
    match = re.search(rf"^    {name}:\n((?:      .*\n|\n)+)", SPEC.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, name
    return match.group(1)


def _required(block: str) -> set[str]:
    match = re.search(r"required: \[([^\]]*)\]", block)
    assert match
    return {v.strip() for v in match.group(1).split(",")}


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


class Remote:
    """A client config (``base_url``, ``tokens``, ``transport``) with a scripted transport."""

    def __init__(self, base: str, *responses: HttpResponse | Exception, fail_token: bool = False) -> None:
        self.base_url = base
        self.tokens = FakeTokens(fail_token)
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Any]] = []

    async def transport(self, method: str, url: str, headers: Any, body: bytes | None, timeout_s: float):
        self.calls.append((method, url, json.loads(body) if body else None))
        assert headers["Authorization"] == "Bearer m2m-token" and timeout_s == 5.0
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _cis(*responses: Any, **kw: Any) -> Remote:
    return Remote("https://cis.example.invalid", *responses, **kw)


def _tp(*responses: Any, **kw: Any) -> Remote:
    return Remote("https://tp.example.invalid", *responses, **kw)


def _resp(status: int, body: Any) -> HttpResponse:
    return HttpResponse(status, json.dumps(body).encode(), {})


# ── the contract and the parsers ──────────────────────────────────────────────────────────────────────────────────


def test_the_field_lists_match_the_contract() -> None:
    assert _required(_schema_block("TripPlannerModule")) == set(cis_tp.MODULE_FIELDS)
    assert _required(_schema_block("PlannerLeadRoute")) == set(cis_tp.ROUTE_FIELDS)
    assert _required(_schema_block("TripPlannerLeadRouteIn")) == {"lead_channel", "crm_member_id"}
    assert _required(_schema_block("TripPlannerAppearanceIn")) == {"theme", "locale", "crm_member_id"}
    tokens = re.search(r"enum: \[([^\]]*)\]", _schema_block("BrandToken"))
    assert tokens and [t.strip() for t in tokens.group(1).split(",")] == list(cis_tp.THEME_TOKENS)
    reasons = SPEC.read_text(encoding="utf-8").split("reason:", 1)[1].split("message:", 1)[0]
    for reason in cis_tp.REASONS - {"tripplanner_invalid", "tripplanner_refused", "tripplanner_no_account"}:
        tail = reason.removeprefix("lead_webhook_url").removeprefix("lead_email")
        assert tail in reasons, reason


def test_the_module_parses_field_for_field_and_drops_unknown_fields() -> None:
    assert cis_tp.parse_module(MODULE) == MODULE
    sent = copy.deepcopy(MODULE)
    sent["plan_key"] = "tripplanner.hosted"
    sent["lead_route"]["updated_by"] = "crm-member:x"
    assert cis_tp.parse_module(sent) == MODULE


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(active="yes"),
        lambda b: b.update(locale="fr"),
        lambda b: b.update(theme={"background": "#ffffff"}),
        lambda b: b.update(theme={"accent": "red"}),
        lambda b: b["domains"][0].update(status="removing"),
        lambda b: b["lead_route"].update(effective_channel="sms"),
        lambda b: b["planner"].update(url="http://planner.example.invalid/x"),
        lambda b: b.update(ai_sessions_monthly=-1),
    ],
)
def test_a_module_outside_the_contract_is_unavailable(mutate) -> None:  # noqa: ANN001
    body = copy.deepcopy(MODULE)
    mutate(body)
    with pytest.raises(cis_tp.ModuleUnavailable, match="cis_contract"):
        cis_tp.parse_module(body)


def test_the_planner_bodies_parse_and_a_traveller_field_never_passes() -> None:
    sent = copy.deepcopy(LEADS)
    sent["leads"][0]["traveller"] = {"name": "Ada Fernwick"}
    assert tp.parse_leads(sent) == LEADS
    assert tp.parse_usage(USAGE) == USAGE
    assert tp.parse_key_info(KEY_INFO) == KEY_INFO
    assert tp.parse_rotated({"version": 1, "rotated_at": "2026-11-18T09:00:00+00:00", "key": KEY})["key"] == KEY


@pytest.mark.parametrize(
    ("parser", "body"),
    [
        (tp.parse_leads, {**LEADS, "leads": [{**LEADS["leads"][0], "status": "lost"}]}),
        (tp.parse_leads, {**LEADS, "month": {"total": 1}}),
        (tp.parse_usage, {**USAGE, "ai_allowance": {"used": -1, "quota": None}}),
        (tp.parse_key_info, {**KEY_INFO, "key": KEY}),
        (tp.parse_rotated, {"version": 0, "rotated_at": "x", "key": KEY}),
        (tp.parse_rotated, {"version": 1, "rotated_at": "x", "key": "short"}),
    ],
)
def test_a_planner_body_outside_the_contract_is_unavailable(parser: Any, body: Any) -> None:
    with pytest.raises(tp.PlannerUnavailable, match="tripplanner_contract"):
        parser(body)


# ── the calls ──────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_cis_calls_use_the_add_ons_scope_and_name_the_member() -> None:
    cis = _cis(_resp(200, MODULE), _resp(200, MODULE), _resp(200, MODULE))
    await cis_tp.get_module(cis, TENANT)
    await cis_tp.put_lead_route(
        cis, TENANT, MEMBER, channel="webhook", webhook_url="https://h.example.com/x", email=None
    )
    await cis_tp.put_appearance(cis, TENANT, MEMBER, theme={"accent": "#2a6f97"}, locale="en")
    base = f"https://cis.example.invalid/tenant/v1/tenants/{TENANT}/tripplanner"
    assert [(m, u) for m, u, _ in cis.calls] == [
        ("GET", base),
        ("PUT", base + "/lead-route"),
        ("PUT", base + "/appearance"),
    ]
    assert cis.calls[1][2] == {
        "lead_channel": "webhook",
        "lead_webhook_url": "https://h.example.com/x",
        "lead_email": None,
        "crm_member_id": str(MEMBER),
    }
    assert cis.calls[2][2] == {"theme": {"accent": "#2a6f97"}, "locale": "en", "crm_member_id": str(MEMBER)}
    assert set(cis.tokens.scopes) == {"cis.tenant/addons"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "status", "reason"),
    [
        (_resp(422, {"error": "invalid", "message": "x", "reason": "lead_webhook_url_not_public"}), 422,
         "lead_webhook_url_not_public"),
        (_resp(422, {"error": "invalid", "message": "x", "reason": "odd"}), 422, "tripplanner_invalid"),
        (_resp(409, {"error": "conflict", "message": "x", "reason": "tripplanner_not_active"}), 409,
         "tripplanner_not_active"),
        (_resp(404, {"error": "not_found", "message": "x", "reason": "tenant_not_found"}), 404,
         "tripplanner_no_account"),
    ],
)  # fmt: skip
async def test_cis_refusals_keep_their_reason(response: HttpResponse, status: int, reason: str) -> None:
    with pytest.raises(cis_tp.ModuleRefused) as info:
        await cis_tp.put_lead_route(_cis(response), TENANT, MEMBER, channel="none", webhook_url=None, email=None)
    assert (info.value.status, info.value.reason) == (status, reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code"),
    [
        (HttpError("timeout"), "cis_timeout"),
        (_resp(500, {}), "cis_http_500"),
        (_resp(404, {"detail": "Not Found"}), "cis_not_served"),
        (_resp(401, {}), "cis_unauthorized"),
    ],
)
async def test_cis_not_answering_is_unavailable(response: Any, code: str) -> None:
    with pytest.raises(cis_tp.ModuleUnavailable, match=code):
        await cis_tp.get_module(_cis(response), TENANT)
    with pytest.raises(cis_tp.ModuleUnavailable, match="not_configured"):
        await cis_tp.get_module(None, TENANT)
    with pytest.raises(cis_tp.ModuleUnavailable, match="token_unavailable"):
        await cis_tp.get_module(_cis(fail_token=True), TENANT)


@pytest.mark.asyncio
async def test_the_planner_calls_use_the_manage_scope_only() -> None:
    remote = _tp(_resp(200, LEADS), _resp(200, USAGE), _resp(200, KEY_INFO),
                 _resp(200, {"version": 1, "rotated_at": "2026-11-18T09:00:00+00:00", "key": KEY}))  # fmt: skip
    await tp.leads(remote, TENANT)
    await tp.usage(remote, TENANT)
    await tp.key_info(remote, TENANT)
    rotated = await tp.rotate_key(remote, TENANT, MEMBER)
    base = f"https://tp.example.invalid/trip/m2m/v1/tenants/{TENANT}/planner"
    assert [(m, u) for m, u, _ in remote.calls] == [
        ("GET", base + "/leads?limit=50"),
        ("GET", base + "/usage"),
        ("GET", base + "/webhook-key"),
        ("POST", base + "/webhook-key/rotate"),
    ]
    assert remote.calls[3][2] == {"actor": f"crm-member:{MEMBER}"} and rotated["key"] == KEY
    assert set(remote.tokens.scopes) == {"tripplanner.leads/manage"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error", "code"),
    [
        (_resp(404, {"error": "unknown_tenant"}), tp.PlannerRefused, "unknown_tenant"),
        (_resp(503, {"error": "webhook_key_unavailable"}), tp.PlannerRefused, "webhook_key_unavailable"),
        (_resp(403, {"error": "forbidden"}), tp.PlannerUnavailable, "tripplanner_http_403"),
        (HttpError("timeout"), tp.PlannerUnavailable, "tripplanner_timeout"),
        (HttpResponse(200, b"<html>"), tp.PlannerUnavailable, "tripplanner_contract"),
    ],
)
async def test_planner_outcomes(response: Any, error: type[Exception], code: str) -> None:
    with pytest.raises(error) as info:
        await tp.usage(_tp(response), TENANT)
    assert info.value.code == code  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_planner_401_drops_the_token_and_no_config_is_unavailable() -> None:
    remote = _tp(_resp(401, {"error": "unauthorized"}))
    with pytest.raises(tp.PlannerUnavailable, match="tripplanner_http_401"):
        await tp.key_info(remote, TENANT)
    assert remote.tokens.invalidated == ["tripplanner.leads/manage"]
    with pytest.raises(tp.PlannerUnavailable, match="not_configured"):
        await tp.key_info(None, TENANT)
    with pytest.raises(tp.PlannerUnavailable, match="token_unavailable"):
        await tp.key_info(_tp(fail_token=True), TENANT)


# ── the routes ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _conn(role: str = "owner") -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM crm.tenant", "active")
    conn.on("FROM crm.member", {"member_id": MEMBER, "role": role})
    return conn


def _client(conn: ScriptedConn, cis: Remote | None, planner: Remote | None) -> tuple[TestClient, dict[str, str]]:
    pools = LocalPools()
    settings = pools.settings()
    app = create_app(settings, FakeDatabase(conn, settings), pools.verifier(settings))
    app.state.studio_activation = cis
    app.state.tp_planner = planner
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers


def _audits(conn: ScriptedConn) -> list[tuple[Any, ...]]:
    return [a for a in conn.sql("INSERT INTO crm.audit_event") if a[2] in ("tripplanner_manage", "tripplanner_view")]


@pytest.mark.parametrize("role", ["owner", "agent"])
def test_the_owner_and_an_agent_read_the_module(role: str) -> None:
    client, headers = _client(_conn(role), _cis(_resp(200, MODULE)), _tp(_resp(200, USAGE), _resp(200, KEY_INFO)))
    with client:
        r = client.get("/crm/v1/trip-planner", headers=headers)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert r.json() == {"module": MODULE, "usage": USAGE, "webhook_key": KEY_INFO}


def test_tripplanner_down_leaves_the_cis_part() -> None:
    client, headers = _client(_conn(), _cis(_resp(200, MODULE)), _tp(HttpError("timeout"), _resp(500, {})))
    with client:
        r = client.get("/crm/v1/trip-planner", headers=headers)
    assert r.status_code == 200 and r.json()["usage"] is None and r.json()["webhook_key"] is None


def test_no_planner_add_on_asks_tripplanner_nothing() -> None:
    planner = _tp()
    client, headers = _client(_conn(), _cis(_resp(200, {**MODULE, "active": False, "planner": None})), planner)
    with client:
        r = client.get("/crm/v1/trip-planner", headers=headers)
    assert r.status_code == 200 and r.json()["usage"] is None and planner.calls == []


def test_cis_down_is_503() -> None:
    client, headers = _client(_conn(), _cis(HttpError("timeout")), _tp())
    with client:
        r = client.get("/crm/v1/trip-planner", headers=headers)
    assert (r.status_code, r.json()["detail"]) == (503, "tripplanner_unavailable")


def test_the_assistant_sees_nothing_and_nobody_is_asked() -> None:
    cis, planner = _cis(), _tp()
    client, headers = _client(_conn("assistant"), cis, planner)
    with client:
        read = client.get("/crm/v1/trip-planner", headers=headers)
        leads = client.get("/crm/v1/trip-planner/leads", headers=headers)
    assert (read.status_code, leads.status_code) == (403, 403)
    assert cis.calls == [] and planner.calls == []


def test_the_leads_list() -> None:
    client, headers = _client(_conn("agent"), _cis(), _tp(_resp(200, LEADS)))
    with client:
        r = client.get("/crm/v1/trip-planner/leads", headers=headers)
    assert r.status_code == 200 and r.json() == LEADS
    client, headers = _client(_conn(), _cis(), _tp(_resp(404, {"error": "unknown_tenant"})))
    with client:
        r = client.get("/crm/v1/trip-planner/leads", headers=headers)
    assert (r.status_code, r.json()["detail"]) == (404, "tripplanner_not_active")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "lead-route", {"lead_channel": "none"}),
        ("PUT", "appearance", {"theme": {}, "locale": "en"}),
        ("POST", "webhook-key", None),
    ],
)
def test_an_agent_changes_nothing(method: str, path: str, body: Any) -> None:
    cis, planner = _cis(), _tp()
    conn = _conn("agent")
    client, headers = _client(conn, cis, planner)
    with client:
        r = client.request(method, f"/crm/v1/trip-planner/{path}", headers=headers, json=body)
    assert (r.status_code, r.json()["detail"]) == (403, "role_forbidden")
    assert cis.calls == [] and planner.calls == []
    assert [a[3] for a in _audits(conn)] == ["denied"]


def test_the_owner_changes_the_route_relayed_and_audited_without_the_target() -> None:
    cis = _cis(_resp(200, MODULE))
    conn = _conn()
    client, headers = _client(conn, cis, _tp())
    body = {"lead_channel": "email", "lead_email": "  desk@brindlecoast.example.com  ", "lead_webhook_url": " "}
    with client:
        r = client.put("/crm/v1/trip-planner/lead-route", headers=headers, json=body)
    assert r.status_code == 200 and r.json() == {"module": MODULE}
    assert cis.calls[0][2] == {
        "lead_channel": "email",
        "lead_webhook_url": None,
        "lead_email": "desk@brindlecoast.example.com",
        "crm_member_id": str(MEMBER),
    }
    (event,) = [a for a in _audits(conn) if a[3] == "allowed"]
    assert (event[4], event[7]) == ("tripplanner_route", "saved")
    assert "brindlecoast" not in json.dumps([list(map(str, a)) for a in conn.calls])


def test_a_cis_refusal_is_422_with_its_reason_and_on_record() -> None:
    cis = _cis(_resp(422, {"error": "invalid", "message": "x", "reason": "lead_email_not_public"}))
    conn = _conn()
    client, headers = _client(conn, cis, _tp())
    with client:
        r = client.put(
            "/crm/v1/trip-planner/lead-route",
            headers=headers,
            json={"lead_channel": "email", "lead_email": "a@b.local"},
        )
    assert (r.status_code, r.json()["detail"]) == (422, "lead_email_not_public")
    (event,) = [a for a in _audits(conn) if a[3] == "denied"]
    assert event[7] == "cis_lead_email_not_public"


def test_the_appearance_is_checked_here_first_and_relayed_lower_case() -> None:
    cis = _cis(_resp(200, MODULE))
    client, headers = _client(_conn(), cis, _tp())
    with client:
        bad = client.put("/crm/v1/trip-planner/appearance", headers=headers,
                         json={"theme": {"background": "#ffffff"}, "locale": "en"})  # fmt: skip
        ok = client.put("/crm/v1/trip-planner/appearance", headers=headers,
                        json={"theme": {"accent": "#2A6F97"}, "locale": "vi"})  # fmt: skip
    assert bad.status_code == 422 and ok.status_code == 200
    assert cis.calls == [("PUT", f"https://cis.example.invalid/tenant/v1/tenants/{TENANT}/tripplanner/appearance",
                          {"theme": {"accent": "#2a6f97"}, "locale": "vi", "crm_member_id": str(MEMBER)})]  # fmt: skip


def test_the_key_is_answered_once_and_never_written_here() -> None:
    planner = _tp(_resp(200, {"version": 2, "rotated_at": "2026-11-18T09:00:00+00:00", "key": KEY}))
    conn = _conn()
    client, headers = _client(conn, _cis(), planner)
    with client:
        r = client.post("/crm/v1/trip-planner/webhook-key", headers=headers)
    assert r.status_code == 200 and r.json()["key"] == KEY and r.headers["Cache-Control"] == "no-store"
    (event,) = [a for a in _audits(conn) if a[3] == "allowed"]
    assert (event[4], event[7]) == ("tripplanner_webhook_key", "rotated")
    assert KEY not in json.dumps([list(map(str, a)) for a in conn.calls])


def test_no_signing_secret_is_503_and_on_record() -> None:
    conn = _conn()
    client, headers = _client(conn, _cis(), _tp(_resp(503, {"error": "webhook_key_unavailable"})))
    with client:
        r = client.post("/crm/v1/trip-planner/webhook-key", headers=headers)
    assert (r.status_code, r.json()["detail"]) == (503, "webhook_key_unavailable")
    assert [a[7] for a in _audits(conn) if a[3] == "denied"] == ["tp_webhook_key_unavailable"]
