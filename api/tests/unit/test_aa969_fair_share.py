"""AA-969 (CRM-37a security review): the body ceiling before any route code (F1) and the agency fair share (F2):
the per-agency slots of the pool, the bounded pool wait and the per-member request ceiling.

Card R: "a noisy tenant cannot push another tenant past the SLO" — here as unit checks of each mechanism; the bounded
load test (loadtest/, run against Dev by the orchestrator) measures it end to end."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app import bodylimit, tenant_gate
from app.auth.route_guard import Caller
from app.errors import DatabaseBusy, RateLimited
from app.main import create_app
from app.settings import Settings, SettingsError, load_settings
from app.tenancy import POOL_ACQUIRE_TIMEOUT_S, tenant_tx
from tests.tokens import PARTNER_ISSUER, LocalPools
from tests.unit.fakes import ScriptedConn

AGENCY_A = uuid.UUID("00000000-0000-4000-8000-0000000009a1")
AGENCY_B = uuid.UUID("00000000-0000-4000-8000-0000000009b1")
MEMBER = uuid.UUID("00000000-0000-4000-8000-0000000009c1")


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


@pytest.fixture
def client(pools: LocalPools) -> Iterator[TestClient]:
    settings = pools.settings(rate_limit_member_per_minute=3)
    with TestClient(create_app(settings, verifier=pools.verifier(settings))) as c:
        yield c


# ── F1: the body ceiling ────────────────────────────────────────────────────────────────────────────────────────


def test_ceiling_per_path() -> None:
    assert bodylimit.max_body_bytes("/crm/v1/travelers/search") == bodylimit.DEFAULT_MAX_BYTES
    assert bodylimit.max_body_bytes("/crm/v1/documents") == bodylimit.DOCUMENT_BODY_BYTES
    assert bodylimit.max_body_bytes("/crm/v1/documents/abc/download") == bodylimit.DEFAULT_MAX_BYTES
    assert bodylimit.max_body_bytes("/crm/v1/client-imports/preview") > 5 * bodylimit.MIB
    assert bodylimit.max_body_bytes("/crm/v1/client-imports") > 5 * bodylimit.MIB
    assert bodylimit.max_body_bytes("/crm/v1/client-imports-x") == bodylimit.DEFAULT_MAX_BYTES
    assert bodylimit.max_body_bytes("/crm/v1/mail/imports") > 25 * bodylimit.MIB
    assert bodylimit.max_body_bytes("/crm/public/v1/traveller/passport-scan") > 10 * bodylimit.MIB
    # Every upload ceiling stays above the route's own cap, so the route keeps answering its own code.
    from app.documents.service import MAX_BYTES as DOC
    from app.routers.client_pages import SCAN_BODY_BYTES

    assert bodylimit.DOCUMENT_BODY_BYTES > (DOC * 4) // 3 + 8
    assert bodylimit.PASSPORT_SCAN_BODY_BYTES > SCAN_BODY_BYTES


def test_declared_oversized_body_is_413_before_the_guard(client: TestClient) -> None:
    """No token at all: without the ceiling FastAPI would read and parse the body first, then answer 401."""
    size = bodylimit.DEFAULT_MAX_BYTES + 1
    res = client.post("/crm/v1/travelers/search", content=b"x" * size, headers={"Content-Type": "application/json"})
    assert res.status_code == 413
    assert res.json() == {"detail": "body_too_large"}


def test_streamed_oversized_body_is_413(client: TestClient) -> None:
    """A chunked body (no Content-Length) is counted while it is read."""

    def chunks() -> Iterator[bytes]:
        for _ in range(20):
            yield b"x" * (128 * 1024)

    res = client.post("/crm/v1/travelers/search", content=chunks(), headers={"Content-Type": "application/json"})
    assert res.status_code == 413
    assert res.json()["detail"] == "body_too_large"


def test_body_under_the_ceiling_reaches_the_guard(client: TestClient) -> None:
    res = client.post("/crm/v1/travelers/search", json={"q": "x"})
    assert res.status_code == 401
    body = b"{" + b" " * (2 * bodylimit.MIB) + b"}"
    big = client.post("/crm/v1/documents", content=body, headers={"Content-Type": "application/json"})
    assert big.status_code == 401  # a 2 MiB document body is allowed on the upload route


def test_public_routes_keep_their_own_smaller_cap(client: TestClient) -> None:
    res = client.post(
        "/crm/public/v1/forms/" + "k" * 24 + "/submissions",
        content=b"x" * (64 * 1024),
        headers={"Content-Type": "application/json", "Origin": "https://example.test"},
    )
    assert res.status_code in (413, 429, 503)  # never a parsed 64 KiB body


@pytest.mark.asyncio
async def test_middleware_passes_websocket_and_lifespan_untouched() -> None:
    seen: list[str] = []

    async def app(scope, receive, send) -> None:  # noqa: ANN001
        seen.append(scope["type"])

    mw = bodylimit.BodyLimitMiddleware(app)
    await mw({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]


@pytest.mark.asyncio
async def test_a_non_numeric_content_length_is_counted_instead() -> None:
    messages = [{"type": "http.request", "body": b"x" * 10, "more_body": True}] * 3
    received: list[int] = []

    async def receive() -> dict:
        return messages.pop(0)

    async def app(scope, recv, send) -> None:  # noqa: ANN001
        for _ in range(3):
            received.append(len((await recv())["body"]))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bodylimit, "DEFAULT_MAX_BYTES", 25)
        mw = bodylimit.BodyLimitMiddleware(app)
        scope = {"type": "http", "path": "/crm/v1/x", "headers": [(b"content-length", b"abc")]}
        with pytest.raises(Exception) as info:
            await mw(scope, receive, None)  # type: ignore[arg-type]
    assert getattr(info.value, "status_code", None) == 413
    assert received == [10, 10]


# ── F2: the agency slots ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_agency_holds_at_most_its_share_and_waits_then_429() -> None:
    gate = tenant_gate.TenantGate(2, 0.05)
    gate.mark_member("iss", "noisy", AGENCY_A)
    release = asyncio.Event()

    async def hold() -> None:
        async with gate.slot("iss", "noisy", AGENCY_A):
            await release.wait()

    holders = [asyncio.create_task(hold()) for _ in range(2)]
    await asyncio.sleep(0)
    assert gate.in_use(f"tenant:{AGENCY_A}") == 2
    with pytest.raises(RateLimited) as info:
        async with gate.slot("iss", "noisy", AGENCY_A):
            pass
    assert (info.value.code, info.value.retry_after_s, info.value.status_code) == ("tenant_busy", 1, 429)
    # Another agency is not slowed down at all.
    gate.mark_member("iss", "calm", AGENCY_B)
    async with gate.slot("iss", "calm", AGENCY_B) as key:
        assert key == f"tenant:{AGENCY_B}"
    release.set()
    await asyncio.gather(*holders)
    assert gate.in_use(f"tenant:{AGENCY_A}") == 0
    assert gate._slots == {} and gate._refs == {}  # noqa: SLF001 — the table empties when nobody holds or waits


@pytest.mark.asyncio
async def test_a_waiter_gets_the_slot_when_it_frees_up() -> None:
    gate = tenant_gate.TenantGate(1, 1.0)
    gate.mark_member("iss", "m", AGENCY_A)
    order: list[str] = []

    async def first() -> None:
        async with gate.slot("iss", "m", AGENCY_A):
            order.append("first")
            await asyncio.sleep(0.02)

    async def second() -> None:
        async with gate.slot("iss", "m", AGENCY_A):
            order.append("second")

    await asyncio.gather(first(), second())
    assert order == ["first", "second"]


def test_unverified_caller_uses_its_own_key_never_the_agency() -> None:
    """A member of A naming B's id spends their own slots, never B's (and tenant_tx() then refuses them)."""
    gate = tenant_gate.TenantGate()
    gate.mark_member("iss", "outsider", AGENCY_A)
    assert gate.key("iss", "outsider", AGENCY_B) == "caller:iss|outsider"
    assert gate.key("iss", "outsider", AGENCY_A) == f"tenant:{AGENCY_A}"


def test_membership_memory_expires_and_is_bounded() -> None:
    now = [0.0]
    gate = tenant_gate.TenantGate(clock=lambda: now[0], member_ttl_s=10, max_tracked=2)
    gate.mark_member("iss", "a", AGENCY_A)
    now[0] = 11
    assert gate.key("iss", "a", AGENCY_A) == "caller:iss|a"
    for sub in ("b", "c", "d"):
        gate.mark_member("iss", sub, AGENCY_A)
    assert gate.key("iss", "b", AGENCY_A) == "caller:iss|b"  # evicted, the oldest of three
    assert gate.key("iss", "d", AGENCY_A) == f"tenant:{AGENCY_A}"


def test_gate_arguments_are_checked() -> None:
    with pytest.raises(ValueError):
        tenant_gate.TenantGate(0)
    with pytest.raises(ValueError):
        tenant_gate.TenantGate(1, 0)
    before = tenant_gate.GATE
    try:
        gate = tenant_gate.configure(3, 0.5)
        assert (tenant_gate.GATE, gate.max_concurrency, gate.wait_s) == (gate, 3, 0.5)
    finally:
        tenant_gate.GATE = before


def _conn() -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM crm.tenant", "active")
    conn.on("FROM crm.member", {"member_id": MEMBER, "role": "agent"})
    return conn


@pytest.mark.asyncio
async def test_tenant_tx_marks_the_verified_membership_and_holds_a_slot() -> None:
    caller = Caller("agency", "partner", "partner-web", PARTNER_ISSUER, "sub-x", ())
    before = tenant_gate.GATE
    tenant_gate.GATE = tenant_gate.TenantGate(2, 1.0)
    try:
        assert tenant_gate.GATE.key(caller.iss, caller.sub, AGENCY_A).startswith("caller:")
        async with tenant_tx(_conn(), caller, AGENCY_A):
            assert tenant_gate.GATE.in_use(f"caller:{caller.iss}|{caller.sub}") == 1
        assert tenant_gate.GATE.key(caller.iss, caller.sub, AGENCY_A) == f"tenant:{AGENCY_A}"
        async with tenant_tx(_conn(), caller, AGENCY_A):
            assert tenant_gate.GATE.in_use(f"tenant:{AGENCY_A}") == 1
    finally:
        tenant_gate.GATE = before


class _BusyPool:
    def __init__(self) -> None:
        self.timeouts: list[float | None] = []

    @asynccontextmanager
    async def acquire(self, timeout: float | None = None) -> AsyncIterator[None]:  # noqa: ASYNC109
        self.timeouts.append(timeout)
        raise TimeoutError
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_no_free_connection_in_time_is_503_db_busy() -> None:
    pool = _BusyPool()
    caller = Caller("agency", "partner", "partner-web", PARTNER_ISSUER, "sub-y", ())
    with pytest.raises(DatabaseBusy) as info:
        async with tenant_tx(pool, caller, AGENCY_A):  # type: ignore[arg-type]
            pass
    assert (info.value.status_code, info.value.code, info.value.retry_after_s) == (503, "db_busy", 2)
    assert pool.timeouts == [POOL_ACQUIRE_TIMEOUT_S]
    assert tenant_gate.GATE.in_use(f"caller:{caller.iss}|{caller.sub}") == 0  # the slot went back


@pytest.mark.asyncio
async def test_an_error_inside_the_block_still_returns_the_connection() -> None:
    released: list[str] = []

    class Pool:
        @asynccontextmanager
        async def acquire(self, timeout: float | None = None) -> AsyncIterator[ScriptedConn]:  # noqa: ASYNC109
            try:
                yield _conn()
            finally:
                released.append("back")

    caller = Caller("agency", "partner", "partner-web", PARTNER_ISSUER, "sub-z", ())
    with pytest.raises(KeyError):
        async with tenant_tx(Pool(), caller, AGENCY_A):  # type: ignore[arg-type]
            raise KeyError("boom")
    assert released == ["back"]


# ── F2: the member ceiling on /crm/v1 ───────────────────────────────────────────────────────────────────────────


def _bearer(token: str, tenant: uuid.UUID | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if tenant is not None:
        headers["X-AA-Tenant-Id"] = str(tenant)
    return headers


def test_member_ceiling_is_per_member_and_agency(client: TestClient, pools: LocalPools) -> None:
    token = pools.partner_web(sub="noisy-member")
    for _ in range(3):
        assert client.get("/crm/v1/memberships", headers=_bearer(token, AGENCY_A)).status_code == 503  # no DB here
    refused = client.get("/crm/v1/memberships", headers=_bearer(token, AGENCY_A))
    assert refused.status_code == 429
    assert refused.json()["detail"] == "rate_limited" and refused.headers["Retry-After"] == "60"
    # The same member for another agency, and another member of the same agency, keep their own count.
    assert client.get("/crm/v1/memberships", headers=_bearer(token, AGENCY_B)).status_code == 503
    other = pools.partner_web(sub="calm-member")
    assert client.get("/crm/v1/memberships", headers=_bearer(other, AGENCY_A)).status_code == 503


def test_ops_class_has_no_member_ceiling(client: TestClient, pools: LocalPools) -> None:
    token = pools.cis_admin()
    for _ in range(5):
        assert client.get("/crm/ops/v1/tenants", headers=_bearer(token)).status_code != 429


def test_a_bad_token_never_spends_the_member_count(client: TestClient, pools: LocalPools) -> None:
    for _ in range(5):
        assert client.get("/crm/v1/memberships", headers=_bearer("not-a-token", AGENCY_A)).status_code == 401
    token = pools.partner_web(sub="fresh-member")
    assert client.get("/crm/v1/memberships", headers=_bearer(token, AGENCY_A)).status_code == 503


# ── settings ────────────────────────────────────────────────────────────────────────────────────────────────────


def test_fair_share_settings_defaults_and_bounds() -> None:
    s = load_settings({})
    assert (s.rate_limit_member_per_minute, s.tenant_max_concurrency, s.tenant_slot_wait_ms) == (600, 2, 5000)
    s = load_settings(
        {"RATE_LIMIT_MEMBER_PER_MINUTE": "0", "CRM_TENANT_MAX_CONCURRENCY": "3", "CRM_TENANT_SLOT_WAIT_MS": "100"}
    )
    assert (s.rate_limit_member_per_minute, s.tenant_max_concurrency, s.tenant_slot_wait_ms) == (0, 3, 100)
    for env in (
        {"RATE_LIMIT_MEMBER_PER_MINUTE": "-1"},
        {"CRM_TENANT_MAX_CONCURRENCY": "0"},
        {"CRM_TENANT_MAX_CONCURRENCY": "4"},
        {"CRM_TENANT_SLOT_WAIT_MS": "99"},
        {"CRM_TENANT_SLOT_WAIT_MS": "30001"},
        {"CRM_TENANT_SLOT_WAIT_MS": "soon"},
    ):
        with pytest.raises(SettingsError):
            load_settings(env)


def test_create_app_configures_the_process_gate(pools: LocalPools) -> None:
    before = tenant_gate.GATE
    try:
        settings: Settings = pools.settings(tenant_max_concurrency=1, tenant_slot_wait_ms=250)
        create_app(settings, verifier=pools.verifier(settings))
        assert (tenant_gate.GATE.max_concurrency, tenant_gate.GATE.wait_s) == (1, 0.25)
    finally:
        tenant_gate.GATE = before


# ── F4: no working link in the request log ──────────────────────────────────────────────────────────────────────


def test_bearer_like_path_segments_are_redacted() -> None:
    from app.observability.request_id import redact_path

    token = "Ab" * 21 + "c"  # 43 URL-safe characters, the shape of a signed client link
    assert redact_path(f"/crm/public/v1/s/{token}") == "/crm/public/v1/s/[token]"
    assert redact_path(f"/crm/public/v1/portal/{token}/home") == "/crm/public/v1/portal/[token]/home"
    assert redact_path(f"/crm/public/v1/calendar/{token}.ics") == "/crm/public/v1/calendar/[token].ics"
    assert redact_path("/crm/public/v1/hooks/" + "a" * 32 + "/inbound") == "/crm/public/v1/hooks/[token]/inbound"
    uid = str(uuid.UUID(int=7))
    assert redact_path(f"/crm/v1/trips/{uid}/documents") == f"/crm/v1/trips/{uid}/documents"
    assert redact_path("/crm/public/v1/forms/" + "k" * 24) == "/crm/public/v1/forms/" + "k" * 24
    assert redact_path("") == "" and redact_path(None) is None


def test_request_completed_line_never_holds_the_link(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    token = "Zx" * 21 + "q"
    with caplog.at_level("INFO", logger="app.request"):
        client.get(f"/crm/public/v1/s/{token}")
    lines = [r for r in caplog.records if r.getMessage() == "request_completed"]
    assert lines and all(token not in str(r.__dict__) for r in lines)
    assert lines[-1].__dict__["path"] == "/crm/public/v1/s/[token]"
