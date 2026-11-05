"""AA-1036 (R3): a CRM-only agency's owner asks for an add-on through the CRM (S269 live gap), on PostgreSQL 15.

The live app over HTTP (real verifier, real ``tenant_tx()``, crm_app with FORCE row security) and a stand-in for CIS
``tenant.addons.v1``: the owner reads the add-ons and sends a request that CIS stores (the CRM keeps no copy); the
agency audit holds the request's CIS id (allowed) or the CIS refusal (denied), and an agent is refused before CIS hears
of it, the denial on record. The CIS half (one store, the studio's rules) is AA-CIS-App
``tests/integration/test_aa1036_addon_requests_pg.py``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg
import httpx
import pytest

from app.cis.activation import ActivationConfig
from app.cis.http import HttpResponse
from app.main import create_app
from tests.integration.test_crm005_intake import _agency, _owner_fetch
from tests.tokens import LocalPools

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

REQUEST_ID = "8a5b6c7d-9e0f-4a1b-82c3-4d5e6f708193"


class _Tokens:
    def __init__(self) -> None:
        self.scopes: list[str] = []

    async def token(self, scope: str) -> str:
        self.scopes.append(scope)
        return "t"

    def invalidate(self, scope: str) -> None:
        return None


def _request(note: str | None) -> dict[str, Any]:
    return {
        "request_id": REQUEST_ID,
        "product": "tripplanner",
        "status": "pending",
        "note": note,
        "requested_at": "2026-11-05T10:00:00.000000Z",
        "decided_at": None,
        "decision_note": None,
        "setup": None,
    }


class FakeCis:
    """CIS tenant.addons.v1: one pending Trip planner request at most, refused while ``in_service`` is false."""

    def __init__(self) -> None:
        self.pending: dict[str, Any] | None = None
        self.in_service = True
        self.bodies: list[dict[str, Any]] = []

    def config(self, tokens: _Tokens) -> ActivationConfig:
        return ActivationConfig("https://cis.example.test", tokens, self.call)  # type: ignore[arg-type]

    async def call(self, method: str, url: str, headers: Any, body: bytes | None, timeout_s: float) -> HttpResponse:
        if method == "GET":
            planner = {"product": "tripplanner", "state": "requested" if self.pending else "available",
                       "subscription_status": None, "request": self.pending, "last_decision": None}  # fmt: skip
            crm = {"product": "crm", "state": "active", "subscription_status": "active", "request": None,
                   "last_decision": None}  # fmt: skip
            return HttpResponse(200, json.dumps({"in_service": self.in_service, "add_ons": [crm, planner]}).encode())
        sent = json.loads(body or b"{}")
        self.bodies.append(sent)
        if not self.in_service:
            problem = {"error": "conflict", "message": "x", "reason": "tenant_not_in_service"}
            return HttpResponse(409, json.dumps(problem).encode())
        created = self.pending is None
        self.pending = self.pending or _request(sent.get("note"))
        return HttpResponse(201 if created else 200, json.dumps({"created": created, "request": self.pending}).encode())


async def _events(admin: asyncpg.Connection, tenant: uuid.UUID) -> list[tuple[Any, ...]]:
    rows = await _owner_fetch(
        admin,
        tenant,
        "SELECT actor_ref, outcome, object_kind, object_id, reason FROM crm.audit_event "
        "WHERE action = 'addon_request' ORDER BY at, event_id",
    )
    return [(r["actor_ref"], r["outcome"], r["object_kind"], r["object_id"], r["reason"]) for r in rows]


async def test_a_crm_only_owner_asks_for_the_trip_planner(
    app_pool: asyncpg.Pool, migrated_app_dsn: str, admin: asyncpg.Connection
) -> None:
    tenant, people = await _agency(app_pool, "owner", "agent")
    (owner, owner_id), (agent, _) = people
    cis, tokens = FakeCis(), _Tokens()
    pools = LocalPools()
    settings = pools.settings(database_url=migrated_app_dsn)
    app = create_app(settings, verifier=pools.verifier(settings))
    app.state.studio_activation = cis.config(tokens)
    as_owner = {"Authorization": f"Bearer {pools.partner_web(sub=owner.sub)}", "X-AA-Tenant-Id": str(tenant)}
    as_agent = {"Authorization": f"Bearer {pools.partner_web(sub=agent.sub)}", "X-AA-Tenant-Id": str(tenant)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://crm.test") as client:
        listed = await client.get("/crm/v1/add-ons", headers=as_owner)
        assert listed.status_code == 200
        states = {a["product"]: a["state"] for a in listed.json()["add_ons"]}
        assert states == {"crm": "active", "tripplanner": "available"}

        refused = await client.post("/crm/v1/add-on-requests", headers=as_agent, json={"product": "tripplanner"})
        assert (refused.status_code, refused.json()["detail"]) == (403, "role_forbidden")
        assert cis.bodies == []

        sent = await client.post(
            "/crm/v1/add-on-requests", headers=as_owner, json={"product": "tripplanner", "note": "For river trips."}
        )
        assert sent.status_code == 201 and sent.json()["request"]["note"] == "For river trips."
        again = await client.post("/crm/v1/add-on-requests", headers=as_owner, json={"product": "tripplanner"})
        assert again.status_code == 200 and again.json()["created"] is False
        assert cis.bodies[0] == {"product": "tripplanner", "note": "For river trips.", "crm_member_id": str(owner_id)}

        cis.in_service = False
        held = await client.post("/crm/v1/add-on-requests", headers=as_owner, json={"product": "crm"})
        assert (held.status_code, held.json()["detail"]) == (409, "tenant_not_in_service")

        after = await client.get("/crm/v1/add-ons", headers=as_owner)
        assert {a["product"]: a["state"] for a in after.json()["add_ons"]}["tripplanner"] == "requested"
    assert set(tokens.scopes) == {"cis.tenant/addons"}

    events = await _events(admin, tenant)
    owner_ref = f"member:{owner_id}"
    assert [e for e in events if e[1] == "allowed"] == [
        (owner_ref, "allowed", "addon_request", REQUEST_ID, "tripplanner_created"),
        (owner_ref, "allowed", "addon_request", REQUEST_ID, "tripplanner_open"),
    ]
    denied = [(e[2], e[4]) for e in events if e[1] == "denied"]
    assert denied == [("permission", "role_forbidden"), ("addon_request", "cis_tenant_not_in_service")]
