"""Slice 1 routes (AA-936): the public intake routes, the agency pipeline routes, the two ops reads, and the platform
contexts ``system_tx`` / ``intake_tx`` (Python branches; the SQL and RLS run on PostgreSQL in the integration tests).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app import tenancy
from app.errors import Forbidden, NotFound, RateLimited
from app.intake import public as intake_public
from app.main import create_app
from app.routers import ops as ops_router
from app.routers import pipeline as pipeline_router
from app.routers import public as public_router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, TENANT, FakeVault, fake_tx, tenant_ctx

KEY = "k" * 22
TRIP = "00000000-0000-4000-8000-0000000000e1"


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


@pytest.fixture
def client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = pools.settings(rate_limit_intake_per_minute=3, rate_limit_intake_form_per_hour=0)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    for module in (pipeline_router, public_router, ops_router):
        monkeypatch.setattr(module, "request_pool", _pool)
    with TestClient(app) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-AA-Tenant-Id": str(TENANT)}


# ── /crm/public/v1 ──────────────────────────────────────────────────────────────────────────────────
def test_public_form_and_submission(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def read_form(pool: Any, key: str) -> dict[str, Any]:
        return {"schema_key": "stage1.v1", "key": key}

    async def submit(pool: Any, keys: Any, settings: Any, key: str, raw: Any, *, origin: str | None, **_: Any) -> Any:
        seen.append({"raw": raw, "origin": origin})
        return intake_public.Submitted(stored=True)

    monkeypatch.setattr(intake_public, "read_form", read_form)
    monkeypatch.setattr(intake_public, "submit", submit)
    got = client.get(f"/crm/public/v1/forms/{KEY}")
    assert got.status_code == 200 and got.json()["key"] == KEY and "max-age=60" in got.headers["cache-control"]
    assert client.get("/crm/public/v1/forms/short").status_code == 422
    posted = client.post(f"/crm/public/v1/forms/{KEY}/submissions", json={"a": 1}, headers={"Origin": "https://go.x"})
    assert posted.status_code == 202 and posted.json() == {"status": "received"}
    assert seen[-1] == {"raw": {"a": 1}, "origin": "https://go.x"} and posted.headers["cache-control"] == "no-store"
    bad = client.post(
        f"/crm/public/v1/forms/{KEY}/submissions", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert bad.status_code == 400
    big = client.post(f"/crm/public/v1/forms/{KEY}/submissions", content=b"x" * (33 * 1024))
    assert big.status_code == 413 and big.json()["detail"] == "body_too_large"
    # Three per address per minute (this client's limit): the next one is refused.
    assert client.post(f"/crm/public/v1/forms/{KEY}/submissions", json={}).status_code == 429


def test_chunked_body_over_the_cap_is_refused_without_a_content_length(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(40):
            yield b"x" * 1024  # 40 KiB, sent with Transfer-Encoding: chunked (no Content-Length)

    res = client.post(f"/crm/public/v1/forms/{KEY}/submissions", content=chunks())
    assert res.status_code == 413 and res.json()["detail"] == "body_too_large"


def test_lying_content_length_is_capped_too(client: TestClient) -> None:
    res = client.post(
        f"/crm/public/v1/forms/{KEY}/submissions", content=b"x" * (40 * 1024), headers={"Content-Length": "10"}
    )
    assert res.status_code in (400, 413)  # the server never trusts the header to size its read


@pytest.mark.asyncio
async def test_read_capped_stops_reading_at_the_cap() -> None:
    sent = 0

    async def receive() -> dict[str, Any]:
        nonlocal sent
        sent += 1
        return {"type": "http.request", "body": b"x" * 4096, "more_body": sent < 1000}  # a 4 MB body

    request = Request({"type": "http", "method": "POST", "headers": [], "path": "/"}, receive)
    with pytest.raises(public_router.BodyTooLarge):
        await public_router.read_capped(request)
    assert sent == public_router.MAX_BODY_BYTES // 4096 + 1  # stopped right after the cap, not after 4 MB

    small = Request(
        {"type": "http", "method": "POST", "headers": [], "path": "/"},
        _once(b'{"a": 1}'),
    )
    assert await public_router.read_capped(small) == b'{"a": 1}'


def _once(data: bytes) -> Any:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": data, "more_body": False}

    return receive


def test_form_quota_answers_429_with_retry_after(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = pools.settings(rate_limit_intake_per_minute=0, rate_limit_intake_form_per_hour=1)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(public_router, "request_pool", _pool)

    async def submit(pool: Any, keys: Any, settings: Any, key: str, raw: Any, **kw: Any) -> Any:
        if not kw["form_quota"]():
            raise RateLimited("form_rate_limited", kw["quota_window_s"])
        return intake_public.Submitted(stored=True)

    monkeypatch.setattr(intake_public, "submit", submit)
    with TestClient(app) as c:
        assert c.post(f"/crm/public/v1/forms/{KEY}/submissions", json={}).status_code == 202
        res = c.post(f"/crm/public/v1/forms/{KEY}/submissions", json={})
    assert (
        res.status_code == 429 and res.headers["retry-after"] == "3600" and res.json()["detail"] == "form_rate_limited"
    )


def test_public_routes_need_no_token_and_carry_no_agency_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def gone(pool: Any, key: str) -> dict[str, Any]:
        raise NotFound("form_not_found")

    monkeypatch.setattr(intake_public, "read_form", gone)
    res = client.get(f"/crm/public/v1/forms/{KEY}")
    assert res.status_code == 404 and res.json()["detail"] == "form_not_found"


# ── /crm/v1 pipeline ────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def agency(
    client: TestClient, pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, dict[str, str], ScriptedConn]:
    conn = ScriptedConn()
    monkeypatch.setattr(pipeline_router, "tenant_tx", fake_tx(tenant_ctx(conn)))
    monkeypatch.setattr(pipeline_router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(pipeline_router, "require_keys", lambda keys: keys)
    return client, _bearer(pools.partner_web()), conn


def test_pipeline_routes_reach_their_helpers(agency: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers, _ = agency
    calls: list[tuple[str, Any]] = []

    def stub(name: str) -> Any:
        async def fn(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append((name, {k: v for k, v in kwargs.items()} or args[2:]))
            return {"ok": name}

        return fn

    for name in (
        "board",
        "trip_detail",
        "update_trip",
        "move_stage",
        "assign",
        "add_note",
        "set_task_status",
    ):
        monkeypatch.setattr(pipeline_router, name, stub(name))

    async def members(ctx: Any) -> list[dict[str, Any]]:
        return [{"member_id": str(MEMBER)}]

    monkeypatch.setattr(pipeline_router, "members", members)
    assert client.get("/crm/v1/pipeline?waiting_on=client&owner=me", headers=headers).json() == {"ok": "board"}
    assert client.get("/crm/v1/pipeline?waiting_on=nobody", headers=headers).status_code == 422
    assert client.get(f"/crm/v1/trips/{TRIP}", headers=headers).json() == {"ok": "trip_detail"}
    assert client.patch(f"/crm/v1/trips/{TRIP}", json={"waiting_on": "client"}, headers=headers).status_code == 200
    assert client.patch(f"/crm/v1/trips/{TRIP}", json={"margin": 1}, headers=headers).status_code == 422
    assert (
        client.post(
            f"/crm/v1/trips/{TRIP}/stage", json={"to": "lost", "lost_reason": "price"}, headers=headers
        ).status_code
        == 200
    )
    assert client.post(f"/crm/v1/trips/{TRIP}/stage", json={"to": "mars"}, headers=headers).status_code == 422
    assert client.post(f"/crm/v1/trips/{TRIP}/owner", json={"member_id": None}, headers=headers).status_code == 200
    assert client.post(f"/crm/v1/trips/{TRIP}/notes", json={"body": "Called"}, headers=headers).status_code == 201
    assert client.post(f"/crm/v1/tasks/{TRIP}/status", json={"status": "done"}, headers=headers).status_code == 200
    assert client.get("/crm/v1/members", headers=headers).json() == {"members": [{"member_id": str(MEMBER)}]}
    assert [c[0] for c in calls] == [
        "board",
        "trip_detail",
        "update_trip",
        "move_stage",
        "assign",
        "add_note",
        "set_task_status",
    ]


def test_forms_routes_owner_only_for_writes(agency: Any, monkeypatch: pytest.MonkeyPatch, pools: LocalPools) -> None:
    client, headers, _ = agency
    from app.intake import forms

    view = forms.FormView(
        "f", "p" * 22, "Website", [], None, "website_form", None, "active", "2026-10-24T00:00:00+00:00"
    )

    async def listed(ctx: Any) -> list[Any]:
        return [view]

    async def made(ctx: Any, **kw: Any) -> Any:
        return view

    async def changed(ctx: Any, form_id: Any, changes: Any) -> Any:
        return view

    monkeypatch.setattr(forms, "list_forms", listed)
    monkeypatch.setattr(forms, "create_form", made)
    monkeypatch.setattr(forms, "update_form", changed)
    assert client.get("/crm/v1/intake-forms", headers=headers).json()["forms"][0]["public_key"] == "p" * 22
    assert client.post("/crm/v1/intake-forms", json={"label": "Website"}, headers=headers).status_code == 201
    assert client.patch(f"/crm/v1/intake-forms/{TRIP}", json={"status": "disabled"}, headers=headers).status_code == 200
    monkeypatch.setattr(pipeline_router, "tenant_tx", fake_tx(tenant_ctx(ScriptedConn(), role="agent")))
    refused = client.post("/crm/v1/intake-forms", json={"label": "Website"}, headers=headers)
    assert refused.status_code == 403 and refused.json()["detail"] == "role_forbidden"
    assert client.patch(f"/crm/v1/intake-forms/{TRIP}", json={"label": "x"}, headers=headers).status_code == 403


def test_pipeline_routes_refuse_a_workforce_token(client: TestClient, pools: LocalPools) -> None:
    res = client.get("/crm/v1/pipeline", headers=_bearer(pools.cis_admin(groups=["cis-admin"])))
    assert res.status_code == 403


# ── /crm/ops/v1 ─────────────────────────────────────────────────────────────────────────────────────
def test_ops_reads(client: TestClient, pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    async def summary(pool: Any) -> list[dict[str, Any]]:
        return [{"kind": "crm_intake_process", "queued": 1, "running": 0, "failed_24h": 0, "oldest_queued_age_s": 3}]

    async def usage(pool: Any, start: Any, end: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(ops_router.queue, "kind_summary", summary)
    monkeypatch.setattr(ops_router, "llm_usage", usage)
    headers = {"Authorization": f"Bearer {pools.cis_admin(groups=['cis-admin'])}"}
    jobs = client.get("/crm/ops/v1/jobs/summary", headers=headers)
    assert jobs.status_code == 200 and jobs.json()["kinds"][0]["kind"] == "crm_intake_process"
    ok = client.get("/crm/ops/v1/llm/usage?from=2026-10-01&to=2026-10-24", headers=headers)
    assert ok.json() == {"from": "2026-10-01", "to": "2026-10-24", "rows": []}
    assert client.get("/crm/ops/v1/llm/usage?from=2026-10-24&to=2026-10-01", headers=headers).status_code == 422
    assert client.get("/crm/ops/v1/llm/usage?from=2026-01-01&to=2026-10-01", headers=headers).status_code == 422
    partner = {"Authorization": f"Bearer {pools.partner_web()}"}
    assert client.get("/crm/ops/v1/jobs/summary", headers=partner).status_code == 403


# ── platform contexts ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_system_tx_checks_the_agency_and_audits() -> None:
    conn = ScriptedConn().on("SELECT status FROM crm.tenant", "active")
    async with tenancy.system_tx(conn, TENANT, actor="job:crm_intake_process") as ctx:  # type: ignore[arg-type]
        ctx.audit("intake_created", "allowed", "trip", TRIP)
    assert conn.sql("INSERT INTO crm.audit_event")[0][:2] == ("system", "job:crm_intake_process")
    with pytest.raises(ValueError):
        async with tenancy.system_tx(conn, TENANT, actor="member:x"):  # type: ignore[arg-type]
            pass
    with pytest.raises(TypeError):
        async with tenancy.system_tx(conn, str(TENANT), actor="job:x"):  # type: ignore[arg-type]
            pass
    conn.on("SELECT status FROM crm.tenant", "locked")
    with pytest.raises(Forbidden):
        async with tenancy.system_tx(conn, TENANT, actor="job:x"):  # type: ignore[arg-type]
            pass
    conn.on("SELECT status FROM crm.tenant", None)
    with pytest.raises(Forbidden):
        async with tenancy.system_tx(conn, TENANT, actor="job:x"):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_system_tx_commits_denials_and_reraises() -> None:
    conn = ScriptedConn().on("SELECT status FROM crm.tenant", "active")
    with pytest.raises(RuntimeError):
        async with tenancy.system_tx(conn, TENANT, actor="job:x") as ctx:  # type: ignore[arg-type]
            ctx.audit("decrypt", "denied", "traveler", TRIP, reason="integrity_check_failed")
            ctx.audit("intake_created", "allowed", "trip", TRIP)
            raise RuntimeError("x")
    assert [a[3] for a in conn.sql("INSERT INTO crm.audit_event")] == ["denied"]
    plain = ScriptedConn().on("SELECT status FROM crm.tenant", "active")
    with pytest.raises(RuntimeError):
        async with tenancy.system_tx(plain, TENANT, actor="job:x"):  # type: ignore[arg-type]
            raise RuntimeError("x")
    assert plain.sql("INSERT INTO crm.audit_event") == []


def _form_row() -> dict[str, Any]:
    return {
        "intake_form_id": uuid.UUID(TRIP),
        "tenant_id": TENANT,
        "label": "Website",
        "form_definition_id": uuid.uuid4(),
        "allowed_origins": ["https://a.example"],
        "honeypot_field": "website",
        "default_owner_member_id": None,
        "lead_source": "website_form",
        "privacy_notice_url": None,
    }


@pytest.mark.asyncio
async def test_intake_tx_finds_the_form_and_hides_everything_else() -> None:
    conn = ScriptedConn().on("FROM crm.intake_form f", _form_row()).on("SELECT status FROM crm.tenant", "active")
    async with tenancy.intake_tx(conn, KEY) as (ctx, form):  # type: ignore[arg-type]
        assert form.tenant_id == TENANT and form.allowed_origins == ("https://a.example",)
        assert ctx.actor == f"intake_form:{TRIP}"
        ctx.audit("intake_received", "allowed", "form_submission", TRIP)
    assert conn.sql("set_config('app.intake_key'")[0] == (KEY,)
    assert conn.sql("INSERT INTO crm.audit_event")
    for bad in ("short", 5):
        with pytest.raises(NotFound):
            async with tenancy.intake_tx(conn, bad):  # type: ignore[arg-type]
                pass
    conn.on("SELECT status FROM crm.tenant", "locked")
    with pytest.raises(NotFound):
        async with tenancy.intake_tx(conn, KEY):  # type: ignore[arg-type]
            pass
    with pytest.raises(NotFound):
        async with tenancy.intake_tx(ScriptedConn(), KEY):  # type: ignore[arg-type]
            pass
    denied = ScriptedConn().on("FROM crm.intake_form f", _form_row()).on("SELECT status FROM crm.tenant", "active")
    with pytest.raises(RuntimeError):
        async with tenancy.intake_tx(denied, KEY) as (ctx, _):  # type: ignore[arg-type]
            ctx.audit("decrypt", "denied", "form_submission", TRIP, reason="x")
            raise RuntimeError("x")
    assert len(denied.sql("INSERT INTO crm.audit_event")) == 1
    with pytest.raises(RuntimeError):
        async with tenancy.intake_tx(denied, KEY):  # type: ignore[arg-type]
            raise RuntimeError("plain")
