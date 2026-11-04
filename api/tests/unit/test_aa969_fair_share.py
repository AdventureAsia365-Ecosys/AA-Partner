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
from app.errors import DatabaseBusy, NotFound, RateLimited
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
    gate = tenant_gate.TenantGate(2, 0.05, total_slots=3)
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
    # Another agency takes the free slot at once.
    gate.mark_member("iss", "calm", AGENCY_B)
    async with gate.slot("iss", "calm", AGENCY_B) as key:
        assert key == f"tenant:{AGENCY_B}"
    release.set()
    await asyncio.gather(*holders)
    assert gate.in_use(f"tenant:{AGENCY_A}") == 0 and gate.in_use() == 0
    assert gate.tracked() == 0  # the tables empty when nobody holds or waits


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


# Review round 2 (F2): a total cap keeps one pool connection for the crm.job worker, and a fair hand-out lets a calm
# agency in ahead of every queued request of the busy ones (one busy agency, two, or a public flood).


class _Holders:
    """Tasks that each hold one slot of ``key`` until released one by one, in the order they got the slot."""

    def __init__(self, gate: tenant_gate.TenantGate) -> None:
        self.gate = gate
        self.got: list[str] = []
        self._events: dict[int, asyncio.Event] = {}
        self.tasks: list[asyncio.Task[None]] = []

    def start(self, key: str, *, busy=tenant_gate.tenant_busy) -> int:  # noqa: ANN001
        n = len(self.tasks)
        event = self._events[n] = asyncio.Event()

        async def run() -> None:
            async with self.gate.hold(key, busy=busy):
                self.got.append(f"{key}#{n}")
                await event.wait()

        self.tasks.append(asyncio.create_task(run()))
        return n

    def finish(self, n: int) -> None:
        self._events[n].set()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_request_path_never_holds_the_last_pool_connection() -> None:
    assert tenant_gate.REQUEST_SLOTS == tenant_gate.POOL_SIZE - 1 == 2
    gate = tenant_gate.TenantGate(2, 5.0)
    h = _Holders(gate)
    for key in ("tenant:a", "tenant:b", "tenant:c", tenant_gate.PUBLIC_KEY):
        h.start(key)
    await _settle()
    assert gate.in_use() == 2 and h.got == ["tenant:a#0", "tenant:b#1"]
    for n in range(4):
        h.finish(n)
    await asyncio.gather(*h.tasks)
    assert gate.in_use() == 0 and gate.tracked() == 0


@pytest.mark.asyncio
async def test_a_calm_agency_gets_the_next_free_slot_ahead_of_the_noisy_queue() -> None:
    gate = tenant_gate.TenantGate(2, 5.0)
    h = _Holders(gate)
    noisy = [h.start("tenant:noisy") for _ in range(6)]  # two hold, four queue
    await _settle()
    calm = h.start("tenant:calm")
    await _settle()
    assert gate.in_use("tenant:noisy") == 2 and gate.waiting("tenant:noisy") == 4 and gate.waiting("tenant:calm") == 1
    h.finish(noisy[0])
    await _settle()
    assert h.got[-1] == f"tenant:calm#{calm}"  # not behind the four queued noisy requests
    for n in [*noisy[1:], calm]:
        h.finish(n)
    await asyncio.gather(*h.tasks)


@pytest.mark.asyncio
async def test_two_busy_agencies_share_one_slot_each_and_a_calm_one_still_gets_in() -> None:
    gate = tenant_gate.TenantGate(2, 5.0)
    h = _Holders(gate)
    a = [h.start("tenant:a") for _ in range(4)]
    await _settle()
    b = [h.start("tenant:b") for _ in range(4)]
    await _settle()
    assert gate.in_use("tenant:a") == 2  # alone at first: the whole request share
    h.finish(a[0])
    await _settle()
    assert (gate.in_use("tenant:a"), gate.in_use("tenant:b")) == (1, 1)  # contention: the share shrinks to one each
    h.finish(a[1])
    await _settle()
    assert (gate.in_use("tenant:a"), gate.in_use("tenant:b")) == (1, 1)  # a's next one, not b's second
    calm = h.start("tenant:calm")
    await _settle()
    h.finish(b[0])
    await _settle()
    assert h.got[-1] == f"tenant:calm#{calm}"  # ahead of both busy queues
    for n in [*a[2:], *b[1:], calm]:
        h.finish(n)
    await asyncio.gather(*h.tasks)
    assert gate.tracked() == 0


@pytest.mark.asyncio
async def test_the_public_routes_share_one_key_and_are_answered_503() -> None:
    gate = tenant_gate.TenantGate(2, 0.05)
    h = _Holders(gate)
    first = [h.start(tenant_gate.PUBLIC_KEY, busy=tenant_gate.public_busy) for _ in range(2)]
    await _settle()
    with pytest.raises(DatabaseBusy) as info:
        async with gate.hold(tenant_gate.PUBLIC_KEY, busy=tenant_gate.public_busy):
            pass
    assert (info.value.status_code, info.value.code, info.value.retry_after_s) == (503, "db_busy", 2)
    for n in first:
        h.finish(n)
    await asyncio.gather(*h.tasks)


@pytest.mark.asyncio
async def test_a_nested_block_does_not_take_a_second_slot() -> None:
    gate = tenant_gate.TenantGate(1, 0.05, total_slots=1)
    async with gate.hold("tenant:a"):
        async with gate.hold(tenant_gate.PUBLIC_KEY):  # would wait for itself without re-entrancy
            assert gate.in_use() == 1
        # A task started inside the block copies its context but is another request: it takes its own slot.
        with pytest.raises(RateLimited):
            await asyncio.create_task(_enter(gate.hold("tenant:a")))
    assert gate.in_use() == 0


async def _enter(cm) -> None:  # noqa: ANN001
    async with cm:
        pass


@pytest.mark.asyncio
async def test_a_cancelled_waiter_leaves_the_queue_and_never_keeps_a_slot() -> None:
    gate = tenant_gate.TenantGate(1, 5.0, total_slots=1)
    h = _Holders(gate)
    holder = h.start("tenant:a")
    await _settle()
    waiter = asyncio.create_task(h.gate.hold("tenant:b").__aenter__())
    await _settle()
    assert gate.waiting("tenant:b") == 1
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert gate.waiting("tenant:b") == 0
    h.finish(holder)
    await asyncio.gather(*h.tasks)
    assert gate.in_use() == 0 and gate.tracked() == 0


@pytest.mark.asyncio
async def test_a_slot_granted_to_a_cancelled_request_goes_back() -> None:
    gate = tenant_gate.TenantGate(1, 5.0, total_slots=1)
    holder = gate.hold("tenant:a")
    await holder.__aenter__()
    waiter = asyncio.create_task(gate.hold("tenant:b").__aenter__())
    await _settle()
    assert gate.waiting("tenant:b") == 1
    await holder.__aexit__(None, None, None)  # the release hands the slot to b's waiter at once ...
    assert gate.in_use("tenant:b") == 1
    waiter.cancel()  # ... which is cancelled before it runs
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert gate.in_use() == 0 and gate.tracked() == 0


@pytest.mark.asyncio
async def test_a_grant_at_the_timeout_keeps_the_slot() -> None:
    gate = tenant_gate.TenantGate(1, 5.0, total_slots=1)
    waiter = tenant_gate._Waiter(1, asyncio.get_running_loop().create_future())  # noqa: SLF001
    waiter.future.set_result(None)
    assert gate._withdraw("tenant:a", waiter) is False  # noqa: SLF001 — granted already: the caller keeps it


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
    with pytest.raises(ValueError):
        tenant_gate.TenantGate(1, 1.0, total_slots=0)
    before = tenant_gate.GATE
    try:
        gate = tenant_gate.configure(2, 0.5)
        assert (tenant_gate.GATE, gate.max_concurrency, gate.wait_s, gate.total_slots) == (gate, 2, 0.5, 2)
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
        {"RATE_LIMIT_MEMBER_PER_MINUTE": "0", "CRM_TENANT_MAX_CONCURRENCY": "1", "CRM_TENANT_SLOT_WAIT_MS": "100"}
    )
    assert (s.rate_limit_member_per_minute, s.tenant_max_concurrency, s.tenant_slot_wait_ms) == (0, 1, 100)
    for env in (
        {"RATE_LIMIT_MEMBER_PER_MINUTE": "-1"},
        {"CRM_TENANT_MAX_CONCURRENCY": "0"},
        {"CRM_TENANT_MAX_CONCURRENCY": "3"},  # the request path has 2 of the pool's 3 connections
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


# ── review round 2 ──────────────────────────────────────────────────────────────────────────────────────────────


def test_every_spelling_of_one_agency_shares_one_member_count(client: TestClient, pools: LocalPools) -> None:
    """uuid.UUID() accepts upper case, braces, urn:uuid: and the form without hyphens: one count for all of them."""
    token = pools.partner_web(sub="spelling-member")
    spellings = [str(AGENCY_A).upper(), "{" + str(AGENCY_A) + "}", f"urn:uuid:{AGENCY_A}", AGENCY_A.hex]
    for spelling in spellings[:3]:
        headers = {"Authorization": f"Bearer {token}", "X-AA-Tenant-Id": spelling}
        assert client.get("/crm/v1/memberships", headers=headers).status_code == 503
    headers = {"Authorization": f"Bearer {token}", "X-AA-Tenant-Id": spellings[3]}
    assert client.get("/crm/v1/memberships", headers=headers).status_code == 429
    assert client.get("/crm/v1/memberships", headers=_bearer(token, AGENCY_A)).status_code == 429


def test_junk_or_missing_tenant_headers_share_one_bucket(client: TestClient, pools: LocalPools) -> None:
    token = pools.partner_web(sub="junk-member")
    junk = ["x1", "not-a-uuid", "", "y" * 500]
    for value in junk[:3]:
        headers = {"Authorization": f"Bearer {token}", "X-AA-Tenant-Id": value}
        assert client.get("/crm/v1/memberships", headers=headers).status_code == 503
    refused = client.get("/crm/v1/memberships", headers={"Authorization": f"Bearer {token}", "X-AA-Tenant-Id": junk[3]})
    assert refused.status_code == 429
    assert client.get("/crm/v1/memberships", headers={"Authorization": f"Bearer {token}"}).status_code == 429


@pytest.mark.asyncio
async def test_caller_memberships_takes_the_callers_slot_and_the_public_contexts_the_public_one() -> None:
    from app.tenancy import calendar_feed_tx, caller_memberships

    caller = Caller("agency", "partner", "partner-web", PARTNER_ISSUER, "sub-m", ())
    seen: list[tuple[int, int]] = []
    gate = tenant_gate.TenantGate(2, 0.05)

    class Pool:
        @asynccontextmanager
        async def acquire(self, timeout: float | None = None) -> AsyncIterator[ScriptedConn]:  # noqa: ASYNC109
            assert timeout == POOL_ACQUIRE_TIMEOUT_S
            mine = gate.in_use(tenant_gate.caller_key(caller.iss, caller.sub))
            seen.append((mine, gate.in_use(tenant_gate.PUBLIC_KEY)))
            yield ScriptedConn()

    before = tenant_gate.GATE
    tenant_gate.GATE = gate
    try:
        assert await caller_memberships(Pool(), caller) == []  # type: ignore[arg-type]
        with pytest.raises(NotFound):
            async with calendar_feed_tx(Pool(), "a" * 64):  # type: ignore[arg-type]
                pass
        # The public share full: a public context is 503 db_busy, an agency is not slowed by it.
        hold = asyncio.Event()

        async def flood() -> None:
            async with gate.hold(tenant_gate.PUBLIC_KEY, busy=tenant_gate.public_busy):
                await hold.wait()

        flooding = [asyncio.create_task(flood()) for _ in range(2)]
        await asyncio.sleep(0)
        with pytest.raises(DatabaseBusy):
            async with calendar_feed_tx(Pool(), "b" * 64):  # type: ignore[arg-type]
                pass
        hold.set()
        await asyncio.gather(*flooding)
    finally:
        tenant_gate.GATE = before
    assert seen == [(1, 0), (0, 1)]


# F1, round 2: the process-wide budget of body bytes in flight.


def _scope(path: str = "/crm/v1/x", length: int | None = None) -> dict:
    headers = [] if length is None else [(b"content-length", str(length).encode())]
    return {"type": "http", "path": path, "headers": headers}


def _chunks(*sizes: int) -> list[dict]:
    return [{"type": "http.request", "body": b"x" * n, "more_body": i < len(sizes) - 1} for i, n in enumerate(sizes)]


@pytest.mark.asyncio
async def test_large_bodies_in_flight_share_one_budget_and_the_overflow_is_503() -> None:
    budget = bodylimit.BodyBudget(300 * 1024)
    release = asyncio.Event()
    statuses: list[int] = []

    def app_reading(hold: bool):  # noqa: ANN202
        async def app(scope, receive, send) -> None:  # noqa: ANN001
            try:
                while (await receive()).get("more_body"):
                    pass
            except Exception as exc:  # noqa: BLE001 — the middleware's HTTPException
                statuses.append(exc.status_code)  # type: ignore[attr-defined]
                assert exc.headers == {"Retry-After": "2"}  # type: ignore[attr-defined]
                return
            statuses.append(200)
            if hold:
                await release.wait()

        return app

    def receiver(messages: list[dict]):  # noqa: ANN202
        async def receive() -> dict:
            return messages.pop(0)

        return receive

    first = bodylimit.BodyLimitMiddleware(app_reading(True), budget)
    task = asyncio.create_task(first(_scope(), receiver(_chunks(100 * 1024, 100 * 1024)), None))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert budget.used == 200 * 1024  # charged as the bytes arrived, held while the request runs
    second = bodylimit.BodyLimitMiddleware(app_reading(False), budget)
    await second(_scope(), receiver(_chunks(60 * 1024, 60 * 1024)), None)  # type: ignore[arg-type]
    assert statuses == [200, 503] and budget.used == 200 * 1024  # the refused one charged nothing
    small = bodylimit.BodyLimitMiddleware(app_reading(False), budget)
    for _ in range(3):  # small bodies are never charged, even with the budget nearly used
        await small(_scope(), receiver(_chunks(bodylimit.SMALL_BODY_BYTES)), None)  # type: ignore[arg-type]
    assert statuses[-3:] == [200, 200, 200]
    release.set()
    await task
    assert budget.used == 0  # given back when the request is done


def test_body_budget_never_goes_negative() -> None:
    budget = bodylimit.BodyBudget(10)
    assert budget.take(10) and not budget.take(1)
    budget.give_back(20)
    assert budget.used == 0


def test_an_anonymous_document_upload_is_401_before_its_body_is_read(client: TestClient) -> None:
    """The upload route reads its body after the class guard: an anonymous caller is refused 401 even with a body
    that is not JSON (FastAPI used to parse a declared body first and answer 422)."""
    res = client.post(
        "/crm/v1/documents",
        content=b"not json" + b" " * (2 * bodylimit.MIB),
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 401
    schema = client.app.openapi()["paths"]["/crm/v1/documents"]["post"]["requestBody"]["content"]["application/json"]
    assert "content_b64" in schema["schema"]["properties"]  # the documented body is unchanged
