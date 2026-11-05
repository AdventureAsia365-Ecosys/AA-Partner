"""AA-1036 (R3): the CRM owner's add-ons page relays to CIS ``tenant.addons.v1`` (app.cis.addons, app.routers.addons).

Found live on Dev (S269): a CRM-only agency owner had no way to ask for an add-on. The contract lives in the root repo;
the byte copy in ``docs/contracts/`` is pinned by hash here and the field lists of :mod:`app.cis.addons` are compared
with it, then run on the root samples (copied with it) and on mutations of them.
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

from app.cis import addons
from app.cis.http import HttpError, HttpResponse
from app.cis.m2m import TokenError
from app.main import create_app
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.test_tenancy import FakeDatabase

DOCS = Path(__file__).resolve().parents[3] / "docs" / "contracts"
SPEC = DOCS / "tenant.addons.v1.openapi.yaml"
SAMPLES = DOCS / "samples" / "tenant.addons.v1"
# sha256 of the root file docs/contracts/tenant.addons.v1.openapi.yaml (1.0, AA-1036 R3).
ROOT_CONTRACT_SHA256 = "2705746e4b47934ff24e3deba45fc9fce36f62ad960b0d4209f3a3cc0a7efc26"

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000010a1")
MEMBER = uuid.UUID("00000000-0000-4000-8000-0000000010b1")
BASE = "https://cis.example.invalid"


def _sample(name: str) -> dict[str, Any]:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))["payload"]


def _schema_block(name: str) -> str:
    text = SPEC.read_text(encoding="utf-8")
    match = re.search(rf"^    {name}:\n((?:      .*\n|\n)+)", text, re.MULTILINE)
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


class Cis:
    """The CRM's CIS client config with a scripted transport (``base_url``, ``tokens``, ``transport``)."""

    def __init__(self, *responses: HttpResponse | Exception, fail_token: bool = False) -> None:
        self.base_url = BASE
        self.tokens = FakeTokens(fail_token)
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], Any, float]] = []

    async def transport(self, method: str, url: str, headers: Any, body: bytes | None, timeout_s: float):
        self.calls.append((method, url, dict(headers), json.loads(body) if body else None, timeout_s))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(status: int, body: Any, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status, json.dumps(body).encode(), headers or {})


def _problem(status: int, reason: str | None = None) -> HttpResponse:
    body: dict[str, Any] = {"error": "conflict", "message": "refused"}
    if reason:
        body["reason"] = reason
    return _resp(status, body)


# ── the contract copy ──────────────────────────────────────────────────────────────────────────────────────────────


def test_the_vendored_copy_is_the_pinned_root_file() -> None:
    assert hashlib.sha256(SPEC.read_bytes()).hexdigest() == ROOT_CONTRACT_SHA256
    assert "x-aa-contract: tenant.addons.v1" in SPEC.read_text(encoding="utf-8")


def test_the_field_lists_match_the_contract() -> None:
    assert _required(_schema_block("AddOnRequest")) == set(addons.REQUEST_FIELDS)
    assert _required(_schema_block("AddOnState")) == set(addons.ADD_ON_FIELDS)
    assert _required(_schema_block("AddOnRequestIn")) == {"product", "crm_member_id"}
    assert "cis.tenant/addons" in SPEC.read_text(encoding="utf-8") and addons.SCOPE_TENANT_ADDONS == "cis.tenant/addons"
    reasons = SPEC.read_text(encoding="utf-8").split("reason:", 1)[1].split("message:", 1)[0]  # Problem.reason
    for reason in addons.REASONS:
        assert reason in reasons


# ── parsing ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_the_sample_list_parses_field_for_field() -> None:
    body = _sample("add-ons-valid.json")
    assert addons.parse_add_ons(body) == body


def test_a_field_cis_adds_later_is_dropped_never_passed_on() -> None:
    body = _sample("add-ons-valid.json")
    sent = copy.deepcopy(body)
    sent["add_ons"][1]["request"]["requested_by"] = "crm-member:x"
    sent["add_ons"][0]["plan_key"] = "crm.core"
    assert addons.parse_add_ons(sent) == body


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(in_service="yes"),
        lambda b: b.update(add_ons=b["add_ons"][:1]),
        lambda b: b["add_ons"][0].update(product="booking"),
        lambda b: b["add_ons"][0].update(state="on"),
        lambda b: b["add_ons"][1].update(product="crm"),
        lambda b: b["add_ons"][1]["request"].update(request_id="r-1"),
        lambda b: b["add_ons"][1]["request"].update(note="x" * 501),
        lambda b: b["add_ons"][1]["request"].update(setup="maybe"),
        lambda b: b["add_ons"][0].update(subscription_status="cancelled"),
        lambda b: b["add_ons"][1].update(request=[]),
    ],
)
def test_a_body_outside_the_contract_is_unavailable(mutate) -> None:  # noqa: ANN001
    body = copy.deepcopy(_sample("add-ons-valid.json"))
    mutate(body)
    with pytest.raises(addons.AddOnsUnavailable) as info:
        addons.parse_add_ons(body)
    assert info.value.code == "cis_contract"


# ── the calls ──────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_list_call_asks_for_its_own_scope_only() -> None:
    cis = Cis(_resp(200, _sample("add-ons-valid.json")))
    assert (await addons.list_add_ons(cis, TENANT))["in_service"] is True
    method, url, headers, body, timeout = cis.calls[0]
    assert (method, url, body) == ("GET", f"{BASE}/tenant/v1/tenants/{TENANT}/add-ons", None)
    assert headers["Authorization"] == "Bearer m2m-token" and timeout == addons.TIMEOUT_S
    assert cis.tokens.scopes == ["cis.tenant/addons"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "created"), [(201, True), (200, False)])
async def test_the_request_names_the_crm_member_only(status: int, created: bool) -> None:
    payload = dict(_sample("request-created-valid.json"), created=created)
    cis = Cis(_resp(status, payload))
    done = await addons.request_add_on(cis, TENANT, "tripplanner", "For our walking season.", MEMBER)
    assert done.created is created and done.request == payload["request"]
    method, url, _, body, _ = cis.calls[0]
    assert (method, url) == ("POST", f"{BASE}/tenant/v1/tenants/{TENANT}/add-on-requests")
    assert body == {"product": "tripplanner", "note": "For our walking season.", "crm_member_id": str(MEMBER)}


@pytest.mark.asyncio
async def test_a_201_that_says_not_created_is_not_the_contract() -> None:
    cis = Cis(_resp(201, dict(_sample("request-created-valid.json"), created=False)))
    with pytest.raises(addons.AddOnsUnavailable, match="cis_contract"):
        await addons.request_add_on(cis, TENANT, "crm", None, MEMBER)


@pytest.mark.asyncio
async def test_an_unknown_product_never_leaves_the_crm() -> None:
    cis = Cis()
    with pytest.raises(ValueError):
        await addons.request_add_on(cis, TENANT, "booking", None, MEMBER)
    assert cis.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code"),
    [
        (HttpError("timeout"), "cis_timeout"),
        (_resp(500, {}), "cis_http_500"),
        (_resp(403, {"error": "forbidden", "message": "The token lacks the scope"}), "cis_http_403"),
        (_resp(404, {"detail": "Not Found"}), "cis_not_served"),
        (HttpResponse(200, b"<html>"), "cis_contract"),
    ],
)
async def test_cis_not_answering_is_unavailable(response: Any, code: str) -> None:
    with pytest.raises(addons.AddOnsUnavailable) as info:
        await addons.list_add_ons(Cis(response), TENANT)
    assert info.value.code == code


@pytest.mark.asyncio
async def test_a_401_drops_the_cached_token_and_a_token_failure_is_unavailable() -> None:
    cis = Cis(_resp(401, {}))
    with pytest.raises(addons.AddOnsUnavailable, match="cis_unauthorized"):
        await addons.list_add_ons(cis, TENANT)
    assert cis.tokens.invalidated == ["cis.tenant/addons"]
    with pytest.raises(addons.AddOnsUnavailable, match="token_unavailable"):
        await addons.list_add_ons(Cis(fail_token=True), TENANT)
    with pytest.raises(addons.AddOnsUnavailable, match="not_configured"):
        await addons.list_add_ons(None, TENANT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "status", "reason"),
    [
        (_problem(409, "already_subscribed"), 409, "already_subscribed"),
        (_problem(409, "tenant_not_in_service"), 409, "tenant_not_in_service"),
        (_problem(409, "odd_reason"), 409, "addon_refused"),
        (_problem(400, "note_too_long"), 400, "note_too_long"),
        (_problem(400), 400, "addon_invalid"),
        (_problem(404, "tenant_not_found"), 404, "addons_no_account"),
    ],
)
async def test_cis_refusals_keep_their_reason(response: HttpResponse, status: int, reason: str) -> None:
    with pytest.raises(addons.AddOnsRefused) as info:
        await addons.request_add_on(Cis(response), TENANT, "crm", None, MEMBER)
    assert (info.value.status, info.value.reason) == (status, reason)


@pytest.mark.asyncio
async def test_too_many_requests_carries_cis_retry_after() -> None:
    limited = _resp(429, {"error": "too_many_requests", "message": "x", "reason": "too_many_requests"},
                    {"Retry-After": "600"})  # fmt: skip
    with pytest.raises(addons.AddOnsRefused) as info:
        await addons.request_add_on(Cis(limited), TENANT, "crm", None, MEMBER)
    assert (info.value.status, info.value.reason, info.value.retry_after_s) == (429, "too_many_requests", 600)


# ── the routes ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _conn(role: str = "owner") -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM crm.tenant", "active")
    conn.on("FROM crm.member", {"member_id": MEMBER, "role": role})
    return conn


def _client(conn: ScriptedConn, cis: Cis | None) -> tuple[TestClient, dict[str, str]]:
    pools = LocalPools()
    settings = pools.settings()
    app = create_app(settings, FakeDatabase(conn, settings), pools.verifier(settings))
    app.state.studio_activation = cis
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers


def _audits(conn: ScriptedConn) -> list[tuple[Any, ...]]:
    return [args for args in conn.sql("INSERT INTO crm.audit_event") if args[2] == "addon_request"]


def test_the_owner_reads_the_add_ons() -> None:
    cis = Cis(_resp(200, _sample("add-ons-valid.json")))
    client, headers = _client(_conn(), cis)
    with client:
        r = client.get("/crm/v1/add-ons", headers=headers)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert r.json() == _sample("add-ons-valid.json")


@pytest.mark.parametrize("role", ["agent", "assistant"])
def test_only_the_owner_reads_or_asks(role: str) -> None:
    cis = Cis()
    conn = _conn(role)
    client, headers = _client(conn, cis)
    with client:
        read = client.get("/crm/v1/add-ons", headers=headers)
        ask = client.post("/crm/v1/add-on-requests", headers=headers, json={"product": "crm"})
    assert (read.status_code, read.json()["detail"]) == (403, "role_forbidden")
    assert (ask.status_code, ask.json()["detail"]) == (403, "role_forbidden")
    assert cis.calls == []  # CIS never hears of it
    denied = [a for a in _audits(conn) if a[3] == "denied"]
    assert [a[7] for a in denied] == ["role_forbidden"]


def test_the_owners_request_is_relayed_and_audited() -> None:
    payload = _sample("request-created-valid.json")
    cis = Cis(_resp(201, payload))
    conn = _conn()
    client, headers = _client(conn, cis)
    with client:
        r = client.post(
            "/crm/v1/add-on-requests", headers=headers, json={"product": "tripplanner", "note": "  Walking tours.  "}
        )
    assert r.status_code == 201 and r.json() == payload
    assert cis.calls[0][3] == {"product": "tripplanner", "note": "Walking tours.", "crm_member_id": str(MEMBER)}
    (event,) = _audits(conn)
    assert event[:4] == ("user", f"member:{MEMBER}", "addon_request", "allowed")
    assert (event[4], event[5], event[7]) == ("addon_request", payload["request"]["request_id"], "tripplanner_created")
    assert "Walking" not in json.dumps([list(map(str, a)) for a in conn.calls])  # the note is never written here


def test_an_open_request_answers_200_and_a_blank_note_is_none() -> None:
    payload = dict(_sample("request-created-valid.json"), created=False)
    cis = Cis(_resp(200, payload))
    conn = _conn()
    client, headers = _client(conn, cis)
    with client:
        r = client.post("/crm/v1/add-on-requests", headers=headers, json={"product": "tripplanner", "note": "   "})
    assert r.status_code == 200 and r.json()["created"] is False
    assert cis.calls[0][3]["note"] is None
    assert _audits(conn)[0][7] == "tripplanner_open"


@pytest.mark.parametrize(
    ("response", "status", "detail"),
    [
        (_problem(409, "already_subscribed"), 409, "already_subscribed"),
        (_problem(409, "tenant_not_in_service"), 409, "tenant_not_in_service"),
        (_resp(429, {"error": "too_many_requests", "message": "x", "reason": "too_many_requests"}), 429,
         "too_many_requests"),
        (_problem(404, "tenant_not_found"), 404, "addons_no_account"),
        (_problem(400, "note_too_long"), 422, "note_too_long"),
    ],
)  # fmt: skip
def test_a_cis_refusal_is_answered_and_on_record(response: HttpResponse, status: int, detail: str) -> None:
    conn = _conn()
    client, headers = _client(conn, Cis(response))
    with client:
        r = client.post("/crm/v1/add-on-requests", headers=headers, json={"product": "crm"})
    assert (r.status_code, r.json()["detail"]) == (status, detail)
    if status == 429:
        assert r.headers["Retry-After"] == str(3600)
    (event,) = _audits(conn)
    assert (event[3], event[7]) == ("denied", f"cis_{detail}")


@pytest.mark.parametrize("cis", [None, "down"])
def test_cis_unavailable_is_503_and_nothing_is_recorded(cis: Any) -> None:
    conn = _conn()
    client, headers = _client(conn, Cis(HttpError("unreachable")) if cis == "down" else None)
    with client:
        read = client.get("/crm/v1/add-ons", headers=headers)
    assert (read.status_code, read.json()["detail"]) == (503, "addons_unavailable")
    client, headers = _client(conn, Cis(HttpError("unreachable")) if cis == "down" else None)
    with client:
        ask = client.post("/crm/v1/add-on-requests", headers=headers, json={"product": "crm"})
    assert (ask.status_code, ask.json()["detail"]) == (503, "addons_unavailable")
    assert _audits(conn) == []


@pytest.mark.parametrize(
    "body",
    [
        {"product": "booking"},
        {},
        {"product": "crm", "note": "x" * 501},
        {"product": "crm", "crm_member_id": str(MEMBER)},
    ],
)
def test_a_body_outside_the_route_is_refused_before_cis(body: dict[str, Any]) -> None:
    cis = Cis()
    client, headers = _client(_conn(), cis)
    with client:
        r = client.post("/crm/v1/add-on-requests", headers=headers, json=body)
    assert r.status_code == 422 and cis.calls == []
