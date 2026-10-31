"""The L1 response contract over every ``/crm/ops/v1`` route (AA-941, card CRM-13a R test: "an L1 response contains no
trip, pipeline or revenue keys"; design v3 S-01 "never trips or revenue", "no PII").

1. Dynamic: every ops route of the live app is called through the real router, console, recovery code and
   ``platform_tx()`` on a scripted connection, on its success path and its main 4xx paths; every JSON key of every
   answer (errors included), at any depth, must be on the allow-list and match no forbidden pattern. The test fails
   when a served ops route is not exercised (the scenario set must equal the route set).
2. Static: every ops route declares a response model with ``extra='forbid'`` (nested models too), and every property
   of its OpenAPI response schema passes the same key rules.
3. Structural: every ops route is an :class:`app.ops.serializer.OpsAllowListRoute`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.main import create_app
from app.ops import console
from app.ops.serializer import OpsAllowListRoute, bad_keys, key_allowed
from app.routers import ops as ops_router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
MEMBER = uuid.UUID("00000000-0000-4000-8000-00000000c0de")
JOB = uuid.UUID("00000000-0000-4000-8000-00000000a0b0")
REQ = uuid.UUID("00000000-0000-4000-8000-00000000bbbb")
NOW = dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)
PREFIX = "/crm/ops/v1"
TICKET = "SUP-1234"


class PgError(asyncpg.PostgresError):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def base_conn(status: str | None = "active") -> ScriptedConn:
    """One scripted pool answering every query the ops routes run, with a plausible row."""
    rules: list[tuple[str, Any]] = [
        ("SELECT status FROM crm.tenant WHERE tenant_id = $1 AND deleted_at IS NULL", status),
        ("FROM crm.tenant WHERE tenant_id = $1 FOR UPDATE", {"status": status, "lock_source": None}),
        (
            "FROM crm.tenant t",
            {
                "tenant_id": TENANT,
                "display_name": "Lữ hành Sen Vàng",
                "status": status or "active",
                "is_synthetic": True,
                "created_at": NOW,
                "has_owner": False,
                "holds_travellers": False,
                "lock_source": "cis_membership" if status == "locked" else None,
                "feed_failures": 0,
                "feed_last_ok_at": NOW,
                "feed_last_error_at": None,
                "read_at": NOW,
            },
        ),  # fmt: skip
        (
            "crm.audit_export_agencies()",
            [{"tenant_id": TENANT, "exported_until": NOW, "verified_until": None, "last_seq": 0, "registered_at": NOW}],
        ),  # fmt: skip
        ("SELECT (SELECT count(*) FROM crm.member", 1),
        ("FROM crm.tenant_entitlement WHERE deleted_at IS NULL", None),
        ("SELECT ai_budget_usd FROM crm.tenant_settings", None),
        ("count(DISTINCT actor_ref)", 1),
        ("SELECT max(at) FROM crm.audit_event", NOW),
        ("sum(size_bytes)", 10),
        ("sum(cost_usd_est)", Decimal("0.5")),
        ("payload->>'tenant_id' = $1 AND finished_at > now() - interval '24 hours'", 0),
        (
            "SELECT id::text AS job_id",
            [
                {
                    "job_id": str(JOB),
                    "kind": "crm_dispatch",
                    "attempt": 5,
                    "max_attempts": 5,
                    "error": "PermanentError",
                    "finished_at": NOW,
                }
            ],
        ),  # fmt: skip
        ("UPDATE crm.job SET status = 'queued'", str(JOB)),
        ("INSERT INTO crm.tenant (tenant_id", TENANT),
        ("SELECT display_name, default_locale, is_synthetic FROM crm.tenant", None),
        ("WHERE role = 'owner' AND status IN ('active', 'invited')", None),
        ("FROM crm.member_invitation WHERE role = 'owner'", None),
        ("SELECT EXISTS (SELECT 1 FROM crm.traveler) OR EXISTS", False),
        ("INSERT INTO crm.member_invitation", NOW + dt.timedelta(days=7)),
        ("FROM crm.studio_invitation WHERE cis_invitation_id = $1", None),
        ("INSERT INTO crm.studio_invitation", {"studio_invitation_id": JOB, "expires_at": NOW + dt.timedelta(days=7)}),
        ("SELECT display_name FROM crm.tenant", "Lữ hành Sen Vàng"),
        (
            "INSERT INTO crm.tenant_entitlement (allowed_features)",
            {"plan": "free", "member_limit": 10, "allowed_features": ["ai_assist"], "insights_opt_out": False},
        ),  # fmt: skip
        ("INSERT INTO crm.tenant_entitlement (insights_opt_out)", True),
        ("status IN ('requested', 'export_ready', 'erasing', 'failed')", None),
        ("crm.legal_hold_blocks", False),
        ("INSERT INTO crm.erasure_request (scope, subject_id, purpose)", REQ),
        ("FROM crm.erasure_request WHERE", [_request_row()]),
        ("FROM crm.erasure_request WHERE request_id = $1", _request_row()),
        ("WHERE role = 'owner' AND status = 'active' AND deleted_at IS NULL", []),
        (
            "ORDER BY created_at, member_id",
            [{"member_id": MEMBER, "role": "agent", "status": "active", "created_at": NOW}],
        ),  # fmt: skip
        ("WHERE status = 'pending' AND deleted_at IS NULL", None),
        (
            "FROM crm.member WHERE member_id = $1",
            {
                "member_id": MEMBER,
                "role": "agent",
                "status": "active",
                "cognito_iss": "https://x/y",
                "cognito_sub": "s",
            },
        ),  # fmt: skip
        ("INSERT INTO crm.owner_recovery_request", {"request_id": REQ, "eligible_at": NOW + dt.timedelta(days=14)}),
        (
            "FROM crm.owner_recovery_request WHERE request_id = $1 AND deleted_at IS NULL FOR UPDATE",
            {"request_id": REQ, "candidate_member_id": MEMBER, "status": "pending", "eligible_at": NOW},
        ),  # fmt: skip
        (
            "GROUP BY kind",
            [{"kind": "crm_intake_process", "queued": 0, "running": 0, "failed_24h": 1, "oldest_queued_age_s": None}],
        ),  # fmt: skip
        # crm.ops.v1 1.4 (AA-992): /data-coverage
        ("SELECT is_synthetic FROM crm.tenant WHERE tenant_id = $1", True),
        ("FROM crm.member WHERE deleted_at IS NULL GROUP BY status", [{"status": "active", "n": 2}]),
        (
            "FROM crm.aa_catalog_state s",
            {
                "served": 892,
                "paused": 0,
                "retired": 1,
                "last_full_pass_at": NOW,
                "last_ok_at": NOW,
                "failures": 0,
                "last_error_class": None,
            },
        ),  # fmt: skip
        (
            "FROM crm.llm_usage_daily",
            [
                {
                    "day": NOW.date(),
                    "stage": "crm_stage1",
                    "model": "m",
                    "calls": 1,
                    "failed_calls": 0,
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "cost_usd": Decimal("0.01"),
                }
            ],
        ),  # fmt: skip
    ]
    conn = ScriptedConn()
    for fragment, answer in rules:
        conn.on(fragment, answer)
    # Longer fragments first: "FROM crm.erasure_request WHERE request_id = $1" before its prefix.
    conn.rules.sort(key=lambda r: -len(r[0]))
    return conn


def _request_row() -> dict[str, Any]:
    return {
        "request_id": REQ,
        "tenant_id": TENANT,
        "purpose": "offboard",
        "status": "requested",
        "requested_by": "platform:staff",
        "owner_accepted_at": None,
        "export_downloaded_at": None,
        "created_at": NOW,
    }


def _ops_routes(app: FastAPI) -> list[APIRoute]:
    def walk(routes: list[Any]) -> Iterator[Any]:
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                yield from walk(inner.routes)
            else:
                yield r

    routes = [r for r in walk(app.routes) if isinstance(r, APIRoute) and r.path.startswith(PREFIX + "/")]
    served = {(m.upper(), p) for p, ops in app.openapi()["paths"].items() if p.startswith(PREFIX) for m in ops}
    assert {(m, r.path) for r in routes for m in r.methods} == served
    return routes


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


@pytest.fixture
def ops_app(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, dict[str, Any]]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    state: dict[str, Any] = {"conn": base_conn()}

    async def pool(_request: Any) -> ScriptedConn:
        return state["conn"]

    monkeypatch.setattr(ops_router, "request_pool", pool)
    monkeypatch.setattr(console, "Vault", lambda keys, ctx: FakeVault())
    with TestClient(app) as client:
        state["app"] = app
        yield client, state


# (method, route path, concrete path, body, adjust(conn) or None, token kind, expected status, expected detail)
T = f"{PREFIX}/tenants/{TENANT}"
SCENARIOS: list[tuple[str, str, str, Any, Any, str, int, str | None]] = [
    ("GET", "/jobs/summary", f"{PREFIX}/jobs/summary", None, None, "reader", 200, None),
    ("GET", "/llm/usage", f"{PREFIX}/llm/usage?from=2026-10-01&to=2026-10-24", None, None, "reader", 200, None),
    ("GET", "/llm/usage", f"{PREFIX}/llm/usage?from=2026-10-24&to=2026-10-01", None, None, "reader", 422,
     "invalid_range"),
    ("GET", "/data-coverage", f"{PREFIX}/data-coverage", None, None, "reader", 200, None),
    # crm.ops.v1 1.5 (AA-966): AA insights; one agency's facts are always suppressed (fewer than 3 agencies).
    ("GET", "/insights", f"{PREFIX}/insights?dimension=destination&from=2026-01-01&to=2026-12-31", None,
     lambda c: c.on("FROM crm.v_insight_fact", [
         {"month": dt.date(2026, 3, 1), "destination": "laos", "budget_band": None, "age_band": None,
          "party_size": 12, "stage_reached": "inquiry", "lost_reason_code": None}]),
     "reader", 200, None),
    ("GET", "/insights", f"{PREFIX}/insights?dimension=budget_band&from=2026-12-01&to=2026-01-01", None, None,
     "reader", 422, "invalid_range"),
    ("GET", "/insights", f"{PREFIX}/insights?dimension=tenant_id&from=2026-01-01&to=2026-02-01", None, None,
     "reader", 422, "invalid_request"),
    ("GET", "/console", f"{PREFIX}/console", None, None, "reader", 200, None),
    ("GET", "/console", f"{PREFIX}/console", None, None, "admin", 200, None),
    ("GET", "/tenants", f"{PREFIX}/tenants?limit=10", None, None, "reader", 200, None),
    ("GET", "/tenants", f"{PREFIX}/tenants?limit=0", None, None, "reader", 422, "invalid_request"),
    ("POST", "/tenants", f"{PREFIX}/tenants",
     {"tenant_id": str(TENANT), "agency_name": "Sen Vàng", "is_synthetic": True, "default_locale": "vi-VN",
      "ticket_ref": TICKET}, None, "admin", 201, None),
    ("POST", "/tenants", f"{PREFIX}/tenants",
     {"tenant_id": str(TENANT), "agency_name": "Sen Vàng", "is_synthetic": True}, None, "admin", 422,
     "invalid_request"),
    ("POST", "/tenants", f"{PREFIX}/tenants",
     {"tenant_id": str(TENANT), "agency_name": "Sen Vàng", "is_synthetic": False, "ticket_ref": TICKET}, None,
     "admin", 422, "synthetic_required"),
    ("POST", "/tenants", f"{PREFIX}/tenants",
     {"tenant_id": str(TENANT), "agency_name": "Sen Vàng", "is_synthetic": True}, None, "reader", 403,
     "missing_group"),
    ("POST", "/tenants", f"{PREFIX}/tenants",
     {"tenant_id": str(TENANT), "agency_name": "Other Name", "is_synthetic": True, "ticket_ref": TICKET},
     lambda c: (c.on("INSERT INTO crm.tenant (tenant_id", None),
                c.on("SELECT display_name, default_locale, is_synthetic FROM crm.tenant",
                     {"display_name": "Sen Vàng", "default_locale": "en-AU", "is_synthetic": True})),
     "admin", 409, "tenant_exists"),
    ("GET", "/tenants/{tenant_id}", T, None, None, "reader", 200, None),
    ("GET", "/tenants/{tenant_id}", T, None, lambda c: c.on(
        "SELECT status FROM crm.tenant WHERE tenant_id = $1 AND deleted_at IS NULL", None), "reader", 404,
     "tenant_not_found"),
    ("GET", "/tenants/{tenant_id}/usage", f"{T}/usage", None, None, "reader", 200, None),
    ("POST", "/tenants/{tenant_id}/lock", f"{T}/lock", {"ticket_ref": TICKET, "reason_code": "payment"}, None,
     "admin", 200, None),
    ("POST", "/tenants/{tenant_id}/lock", f"{T}/lock", {"ticket_ref": "free text", "reason_code": "payment"}, None,
     "admin", 422, "invalid_request"),
    ("POST", "/tenants/{tenant_id}/lock", f"{T}/lock", {"ticket_ref": TICKET, "reason_code": "payment"},
     lambda c: c.on("SELECT status FROM crm.tenant WHERE tenant_id = $1 AND deleted_at IS NULL", "offboarding"),
     "admin", 409, "tenant_status"),
    ("POST", "/tenants/{tenant_id}/unlock", f"{T}/unlock", {"ticket_ref": TICKET},
     lambda c: (c.on("SELECT status FROM crm.tenant WHERE tenant_id = $1 AND deleted_at IS NULL", "locked"),
                c.on("FROM crm.tenant WHERE tenant_id = $1 FOR UPDATE", {"status": "locked", "lock_source": "aa_ops"})),
     "admin", 200, None),
    ("POST", "/tenants/{tenant_id}/owner-invite", f"{T}/owner-invite",
     {"owner_email": "owner@agency.example", "ticket_ref": TICKET}, None, "admin", 202, None),
    ("POST", "/tenants/{tenant_id}/owner-invite", f"{T}/owner-invite",
     {"owner_email": "owner@agency.example", "ticket_ref": TICKET},
     lambda c: c.on("SELECT EXISTS (SELECT 1 FROM crm.traveler) OR EXISTS", True), "admin", 409, "has_travellers"),
    ("POST", "/tenants/{tenant_id}/owner-invite", f"{T}/owner-invite",
     {"owner_email": "owner@agency.example", "ticket_ref": TICKET},
     lambda c: c.on("WHERE role = 'owner' AND status IN ('active', 'invited')", 1), "admin", 409, "owner_exists"),
    ("POST", "/tenants/{tenant_id}/owner-invite", f"{T}/owner-invite",
     {"owner_email": "not an address", "ticket_ref": TICKET}, None, "admin", 422, "invalid_email"),
    ("GET", "/tenants/{tenant_id}/entitlements", f"{T}/entitlements", None, None, "reader", 200, None),
    ("PUT", "/tenants/{tenant_id}/entitlements-allowed", f"{T}/entitlements-allowed",
     {"features_allowed": ["ai_assist"], "ticket_ref": TICKET}, None, "admin", 200, None),
    ("PUT", "/tenants/{tenant_id}/entitlements-allowed", f"{T}/entitlements-allowed",
     {"features_allowed": ["ai"], "ticket_ref": TICKET}, None, "admin", 422, "invalid_feature"),
    ("PUT", "/tenants/{tenant_id}/insights-opt-out", f"{T}/insights-opt-out",
     {"opt_out": True, "owner_request_ref": "OWN-1"}, None, "admin", 200, None),
    ("PUT", "/tenants/{tenant_id}/insights-opt-out", f"{T}/insights-opt-out", {"opt_out": True}, None, "admin",
     422, "invalid_request"),
    ("GET", "/tenants/{tenant_id}/data-requests", f"{T}/data-requests", None, None, "reader", 200, None),
    ("POST", "/tenants/{tenant_id}/data-requests", f"{T}/data-requests",
     {"kind": "agency_deletion", "ticket_ref": TICKET}, None, "admin", 202, None),
    ("POST", "/tenants/{tenant_id}/data-requests", f"{T}/data-requests",
     {"kind": "agency_deletion", "ticket_ref": TICKET},
     lambda c: c.on("status IN ('requested', 'export_ready', 'erasing', 'failed')", {"request_id": REQ}),
     "admin", 409, "offboarding_open"),
    ("GET", "/tenants/{tenant_id}/jobs", f"{T}/jobs", None, None, "reader", 200, None),
    ("POST", "/tenants/{tenant_id}/jobs/{job_id}/retry", f"{T}/jobs/{JOB}/retry", {"ticket_ref": TICKET}, None,
     "admin", 202, None),
    ("POST", "/tenants/{tenant_id}/jobs/{job_id}/retry", f"{T}/jobs/{JOB}/retry", {"ticket_ref": TICKET},
     lambda c: c.on("UPDATE crm.job SET status = 'queued'", None), "admin", 404, "job_not_found"),
    ("POST", "/tenants/{tenant_id}/jobs/{job_id}/retry", f"{T}/jobs/{JOB}/retry", {"ticket_ref": TICKET},
     lambda c: c.on("SELECT status FROM crm.tenant WHERE tenant_id = $1 AND deleted_at IS NULL", "locked"),
     "admin", 409, "tenant_locked"),
    ("GET", "/tenants/{tenant_id}/members", f"{T}/members", None, None, "reader", 200, None),
    ("POST", "/tenants/{tenant_id}/members/{member_id}/deactivate", f"{T}/members/{MEMBER}/deactivate", None, None,
     "admin", 200, None),
    ("POST", "/tenants/{tenant_id}/members/{member_id}/deactivate", f"{T}/members/{MEMBER}/deactivate",
     {"ticket_ref": TICKET}, lambda c: c.on("FROM crm.member WHERE member_id = $1", None), "admin", 404,
     "member_not_found"),
    ("POST", "/tenants/{tenant_id}/owner-recovery", f"{T}/owner-recovery",
     {"candidate_member_id": str(MEMBER), "reason": "owner_left", "ticket_ref": TICKET}, None, "admin", 201, None),
    ("POST", "/tenants/{tenant_id}/owner-recovery", f"{T}/owner-recovery",
     {"candidate_member_id": str(MEMBER), "reason": "owner_left", "ticket_ref": TICKET},
     lambda c: c.on("INSERT INTO crm.owner_recovery_request", asyncpg.UniqueViolationError("dup")), "admin", 409,
     "recovery_pending"),
    ("POST", "/tenants/{tenant_id}/owner-recovery/{request_id}/complete", f"{T}/owner-recovery/{REQ}/complete",
     None, None, "admin", 200, None),
    ("POST", "/tenants/{tenant_id}/owner-recovery/{request_id}/complete", f"{T}/owner-recovery/{REQ}/complete",
     {"ticket_ref": TICKET}, lambda c: c.on("SET status = 'completed'", PgError("CRM03")), "admin", 409,
     "recovery_not_due"),
    # AA-979 P2b: the Content studio invitation relay; a cis-admin token may write it (Q4).
    ("POST", "/tenants/{tenant_id}/studio-invitations", f"{T}/studio-invitations",
     {"email": "editor@agency.example", "cis_invitation_id": str(REQ), "locale": "vi"}, None, "reader", 202, None),
    ("POST", "/tenants/{tenant_id}/studio-invitations", f"{T}/studio-invitations",
     {"email": "editor@agency.example", "cis_invitation_id": str(REQ)},
     lambda c: c.on("FROM crm.studio_invitation WHERE cis_invitation_id = $1",
                    {"studio_invitation_id": JOB, "cis_tenant_id": TENANT, "status": "claimed"}),
     "reader", 409, "invitation_claimed"),
    ("POST", "/tenants/{tenant_id}/studio-invitations", f"{T}/studio-invitations",
     {"email": "not an address", "cis_invitation_id": str(REQ)}, None, "admin", 422, "invalid_email"),
]  # fmt: skip
# AA-979: routes that write no agency audit (the CIS tenant of a studio invitation may have no CRM agency).
NO_AGENCY_AUDIT = {"/tenants/{tenant_id}/studio-invitations"}


def test_every_ops_route_answers_inside_the_allow_list(ops_app: Any, pools: LocalPools) -> None:
    client, state = ops_app
    tokens = {"reader": pools.cis_admin(), "admin": pools.cis_admin(["cis-admin", "crm-platform-admin"])}
    exercised: set[tuple[str, str]] = set()
    for method, route, path, body, adjust, who, status, detail in SCENARIOS:
        conn = base_conn()
        if adjust is not None:
            adjust(conn)
        state["conn"] = conn
        r = client.request(method, path, json=body, headers={"Authorization": f"Bearer {tokens[who]}"})
        label = f"{method} {path} -> {r.status_code} {r.text[:300]}"
        assert r.status_code == status, label
        data = r.json()
        assert bad_keys(data) == set(), label
        if detail is not None:
            assert data["detail"] == detail and set(data) <= {"detail", "message", "fields", "state", "member_limit"}
        if status < 300 and method != "GET" and route not in NO_AGENCY_AUDIT:
            events = [args for args in conn.sql("INSERT INTO crm.audit_event")]
            assert events and all(e[0] == "aa_ops" for e in events), label  # every write is on record
        if status >= 400 and method != "GET":
            assert not conn.sql("UPDATE crm.tenant SET display_name"), label  # a refused write renames nothing
        if status == 200 and method == "GET" and "/tenants/" in path:
            actions = [args[2] for args in conn.sql("INSERT INTO crm.audit_event")]
            assert actions and set(actions) <= {"ops_read", "list_view"}, label  # every agency read is on record
        exercised.add((method, PREFIX + route))
    served = {(m, r.path) for r in _ops_routes(state["app"]) for m in r.methods}
    assert exercised == served, f"not exercised: {sorted(served - exercised)}"


def test_the_first_owner_invite_never_echoes_the_address(ops_app: Any, pools: LocalPools) -> None:
    client, state = ops_app
    admin = {"Authorization": f"Bearer {pools.cis_admin(['cis-admin', 'crm-platform-admin'])}"}
    for body in (
        {"owner_email": "owner@agency.example", "ticket_ref": TICKET},
        {"owner_email": "owner@agency.example", "ticket_ref": "bad ref"},
        {"owner_email": "owner@agency.example"},
        {"owner_email": "owner@agency.example", "ticket_ref": TICKET, "extra": "owner@agency.example"},
    ):
        state["conn"] = base_conn()
        r = client.post(f"{T}/owner-invite", json=body, headers=admin)
        assert "agency.example" not in r.text and "o***" not in r.text, r.text
    sent = state["app"].state.mailer.sent
    assert len(sent) == 1 and sent[0].to == "owner@agency.example"


def test_console_tells_the_page_whether_it_may_write(ops_app: Any, pools: LocalPools) -> None:
    client, _ = ops_app
    reader = client.get(f"{PREFIX}/console", headers={"Authorization": f"Bearer {pools.cis_admin()}"})
    admin = client.get(
        f"{PREFIX}/console",
        headers={"Authorization": f"Bearer {pools.cis_admin(['crm-platform-admin'])}"},
    )
    assert reader.json() == {"can_write": False} and admin.json() == {"can_write": True}


def test_every_ops_route_is_an_allow_list_route_with_a_closed_response_model(ops_app: Any) -> None:
    _, state = ops_app
    for route in _ops_routes(state["app"]):
        assert isinstance(route, OpsAllowListRoute), route.path
        model = route.response_model
        assert isinstance(model, type) and issubclass(model, BaseModel), route.path

        def closed(m: type[BaseModel], seen: set[type]) -> None:
            if m in seen:
                return
            seen.add(m)
            assert m.model_config.get("extra") == "forbid", m.__name__
            for field in m.model_fields.values():
                for arg in _model_args(field.annotation):
                    closed(arg, seen)

        closed(model, set())


def _model_args(annotation: Any) -> list[type[BaseModel]]:
    out: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    for arg in getattr(annotation, "__args__", ()) or ():
        out += _model_args(arg)
    return out


def test_every_response_schema_key_passes_the_rules(ops_app: Any) -> None:
    _, state = ops_app
    spec = state["app"].openapi()
    schemas = spec.get("components", {}).get("schemas", {})

    def props(schema: dict[str, Any], seen: set[str]) -> set[str]:
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[1]
            if name in seen:
                return set()
            seen.add(name)
            return props(schemas[name], seen)
        found = set(schema.get("properties", {}))
        subs = list(schema.get("properties", {}).values()) + ([schema["items"]] if "items" in schema else [])
        for sub in subs:
            if isinstance(sub, dict):
                found |= props(sub, seen)
        for key in ("anyOf", "allOf", "oneOf"):
            for sub in schema.get(key, []):
                found |= props(sub, seen)
        return found

    keys: set[str] = set()
    for path, ops in spec["paths"].items():
        if not path.startswith(PREFIX):
            continue
        for op in ops.values():
            for response in op.get("responses", {}).values():
                for content in response.get("content", {}).values():
                    keys |= props(content.get("schema", {}), set())
    assert keys, "the walk saw no response keys"
    assert sorted(k for k in keys if not key_allowed(k)) == []
