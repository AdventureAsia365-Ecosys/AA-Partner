"""AA-953 follow-up: the two per-address write buckets of the client pages (``app.routers.client_pages``).

Anonymous and pre-sign-in writes keep the intake limiter (``RATE_LIMIT_INTAKE_PER_MINUTE``, 5 per address: the
inquiry form's anti-abuse ceiling). Writes only a signed-in traveller can make take the portal write limiter
(``RATE_LIMIT_PORTAL_WRITE_PER_MINUTE``, default 30) when the request carries a session cookie, so a family filling the
portal from one home address is not stopped at the sixth write. A session cookie only lifts a write to that bucket
once the session proves valid: until then the write holds a slot of the "unproven session" bucket (the intake ceiling),
so a forged or stale cookie stays at 5 a minute. The services are stubbed (a valid session marks itself proven, as
``app.portal.public._signed_in`` does): only the limit is tested.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AsyncExitStack
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.errors import Forbidden
from app.main import create_app
from app.portal import public as portal
from app.proposals import public as proposal_page
from app.routers import client_pages
from tests.tokens import LocalPools
from tests.unit.test_thin_lifecycle_routes import _pool, _stub

TOKEN = "t" * 43
SESSION = "S" * 43  # the shape of secrets.token_urlsafe(32), as the session check issues it
INVOICE = "00000000-0000-4000-8000-0000000007a1"
TRAVELLER = "00000000-0000-4000-8000-0000000007b1"
INTAKE = 5
PORTAL = 30

# Writes that need a portal session (the handler answers 403 portal_sign_in_required without one).
SESSION_WRITES: list[tuple[str, str, dict[str, Any]]] = [
    ("PUT", f"/portal/{TOKEN}/stage3", {"nationality": "AU"}),
    ("POST", f"/portal/{TOKEN}/sign", {"code": "654321"}),
    ("POST", f"/portal/{TOKEN}/sign-out", {}),
    ("POST", f"/portal/{TOKEN}/code", {"purpose": "sign"}),
    ("PUT", "/traveller/stage3", {"nationality": "AU"}),
    ("POST", "/traveller/sign", {"code": "654321"}),
    ("POST", "/traveller/sign-out", {}),
    ("POST", "/traveller/code", {"purpose": "sign"}),
    ("POST", "/traveller/switch", {"traveler_id": TRAVELLER}),
    ("PUT", "/traveller/flights", {"flights": []}),
    ("POST", "/traveller/passport-scan", {"content_b64": ""}),
    ("POST", "/traveller/companions", {"traveler_id": TRAVELLER}),
]

# Writes a link holder makes without a session: the proposal pages, the sign-in code and the code check.
ANONYMOUS_WRITES: list[tuple[str, str, dict[str, Any]]] = [
    ("POST", f"/p/{TOKEN}/accept", {"full_name": "Pella Quist"}),
    ("POST", f"/p/{TOKEN}/terms", {}),
    ("POST", "/proposal/pick", {}),
    ("POST", "/proposal/hold", {}),
    ("POST", "/proposal/accept", {"full_name": "Pella Quist"}),
    ("POST", "/proposal/terms", {}),
    ("POST", "/proposal/change-request", {"message": "One more night in Orvane"}),
    ("POST", f"/proposal/invoices/{INVOICE}/claim", {}),
    ("POST", f"/proposal/invoices/{INVOICE}/card", {}),
    ("POST", f"/portal/{TOKEN}/code", {"purpose": "sign_in"}),
    ("POST", f"/portal/{TOKEN}/session", {"code": "123456"}),
    ("POST", "/traveller/code", {"purpose": "sign_in"}),
    ("POST", "/traveller/session", {"code": "123456"}),
]


@pytest.fixture
def pools() -> LocalPools:
    return LocalPools()


def _session_stub(calls: list[tuple[str, Any]], name: str, valid: bool, result: Any = None) -> Any:
    """A portal service: a valid session marks itself proven (as ``_signed_in`` does); a refused one is 403."""
    inner = _stub(calls, name, result)

    async def fn(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("purpose", "sign") == "sign":  # request_code: only a signing code checks the session
            if not valid:
                raise Forbidden("portal_sign_in_required")
            portal.mark_session_proven()
        return await inner(*args, **kwargs)

    return fn


def _client(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, *, valid: bool = True, **limits: int
) -> Iterator[TestClient]:
    settings = pools.settings(rate_limit_intake_per_minute=INTAKE, **limits)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    app.state.documents = object()
    monkeypatch.setattr(client_pages, "request_pool", _pool)
    monkeypatch.setattr(client_pages, "require_keys", lambda keys: keys)
    monkeypatch.setattr(client_pages, "require_store", lambda store: store)
    calls: list[tuple[str, Any]] = []
    for name in ("submit_stage3", "sign_out", "request_code", "switch", "save_flights", "upload_passport_scan",
                 "invite_companion"):  # fmt: skip
        monkeypatch.setattr(portal, name, _session_stub(calls, name, valid))
    for name in ("accept", "reaccept_terms", "pick", "change_request", "claim_payment", "card_payment"):
        monkeypatch.setattr(proposal_page, name, _stub(calls, name))

    async def hold(*_: Any, **__: Any) -> tuple[int, dict]:
        return 200, {"status": "held"}

    async def sign(*_: Any, **__: Any) -> tuple[bool, dict]:
        if not valid:
            raise Forbidden("portal_sign_in_required")
        portal.mark_session_proven()
        return True, {"status": "signed"}

    async def verify(*_: Any, **__: Any) -> tuple[Any, dict]:
        return None, {"detail": "code_invalid", "attempts_left": 4}

    monkeypatch.setattr(proposal_page, "hold", hold)
    monkeypatch.setattr(portal, "sign", sign)
    monkeypatch.setattr(portal, "verify_code", verify)
    with TestClient(app, base_url="https://go.test") as c:
        c.cookies.set(client_pages.PORTAL_LINK_COOKIE, TOKEN, domain="go.test", path="/")
        c.cookies.set(client_pages.PROPOSAL_COOKIE, TOKEN, domain="go.test", path="/")
        yield c


@pytest.fixture
def client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _client(pools, monkeypatch)


def _send(client: TestClient, route: tuple[str, str, dict[str, Any]]) -> Any:
    method, path, body = route
    return client.request(method, f"/crm/public/v1{path}", json=body)


def _signed_in(client: TestClient, session: str = SESSION) -> None:
    client.cookies.set(portal.SESSION_COOKIE, session, domain="go.test", path="/")


def _assert_limited(res: Any) -> None:
    assert res.status_code == 429, res.text
    assert res.json() == {"detail": "rate_limited"}
    assert res.headers["retry-after"] == "60"


def test_the_default_portal_write_limit_is_30_and_its_own_setting() -> None:
    from app.settings import load_settings

    s = load_settings({})
    assert (s.rate_limit_intake_per_minute, s.rate_limit_portal_write_per_minute) == (5, 30)
    assert load_settings({"RATE_LIMIT_PORTAL_WRITE_PER_MINUTE": "0"}).rate_limit_portal_write_per_minute == 0
    assert load_settings({"RATE_LIMIT_PORTAL_WRITE_PER_MINUTE": "45"}).rate_limit_portal_write_per_minute == 45


@pytest.mark.parametrize("route", SESSION_WRITES, ids=lambda r: f"{r[0]} {r[1]}")
def test_a_signed_in_write_is_not_stopped_by_the_intake_ceiling(
    client: TestClient, route: tuple[str, str, dict[str, Any]]
) -> None:
    _signed_in(client)
    for n in range(INTAKE + 1):
        res = _send(client, route)
        assert res.status_code in (200, 202), (n, res.text)


@pytest.mark.parametrize("route", ANONYMOUS_WRITES, ids=lambda r: f"{r[0]} {r[1]}")
def test_an_anonymous_write_keeps_the_intake_ceiling_even_with_a_session(
    client: TestClient, route: tuple[str, str, dict[str, Any]]
) -> None:
    _signed_in(client)  # a session cookie does not lift the anti-abuse ceiling of a pre-sign-in write
    for n in range(INTAKE):
        res = _send(client, route)
        assert res.status_code in (200, 202, 422), (n, res.text)  # 422: the stubbed code check refuses the code
    _assert_limited(_send(client, route))


def test_the_signed_in_bucket_stops_at_31(client: TestClient) -> None:
    _signed_in(client)
    stage3 = SESSION_WRITES[4]
    for n in range(PORTAL):
        assert _send(client, stage3).status_code == 200, n
    _assert_limited(_send(client, stage3))
    # The buckets are separate: the anonymous one is untouched by the 31 portal writes.
    assert _send(client, ("POST", "/traveller/code", {"purpose": "sign_in"})).status_code == 202


def test_a_family_mixes_writes_in_one_minute(client: TestClient) -> None:
    """The Dev e2e shape that 429'd: sign-in writes, then Stage 3, flights, the signing code and the signature."""
    for route in (ANONYMOUS_WRITES[0], ANONYMOUS_WRITES[11], ANONYMOUS_WRITES[12], ANONYMOUS_WRITES[11]):
        assert _send(client, route).status_code != 429
    _signed_in(client)
    for route in SESSION_WRITES[4:]:
        assert _send(client, route).status_code != 429, route
    assert _send(client, ANONYMOUS_WRITES[12]).status_code != 429  # the fifth anonymous write still passes


@pytest.mark.parametrize("session", [None, "session-token", "S" * 42 + "!"])
def test_without_a_session_cookie_a_portal_write_keeps_the_intake_ceiling(
    client: TestClient, session: str | None
) -> None:
    if session is not None:
        _signed_in(client, session)
    stage3 = SESSION_WRITES[4]
    for n in range(INTAKE):
        assert _send(client, stage3).status_code != 429, n
    _assert_limited(_send(client, stage3))


def test_the_anonymous_ceiling_does_not_block_a_signed_in_write(client: TestClient) -> None:
    for _ in range(INTAKE):
        _send(client, ANONYMOUS_WRITES[2])
    _assert_limited(_send(client, ANONYMOUS_WRITES[2]))
    _signed_in(client)
    assert _send(client, SESSION_WRITES[4]).status_code == 200


def test_a_zero_portal_limit_turns_the_bucket_off(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    for c in _client(pools, monkeypatch, rate_limit_portal_write_per_minute=0):
        _signed_in(c)
        for n in range(PORTAL + 5):
            assert _send(c, SESSION_WRITES[4]).status_code == 200, n


# ── a session cookie that does not prove valid (review of the first pass: the cookie's shape alone is not trust) ──


@pytest.mark.parametrize("route", SESSION_WRITES, ids=lambda r: f"{r[0]} {r[1]}")
def test_a_forged_session_cookie_keeps_the_anonymous_ceiling(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, route: tuple[str, str, dict[str, Any]]
) -> None:
    for c in _client(pools, monkeypatch, valid=False):
        _signed_in(c)  # any 43-character value has the issued shape; the stub refuses it as the session check would
        for n in range(INTAKE):
            res = _send(c, route)
            assert res.status_code == 403 and res.json()["detail"] == "portal_sign_in_required", (n, res.text)
        _assert_limited(_send(c, route))
        # The sixth was stopped before the portal bucket: only the five refused writes took a slot there.
        assert c.app.state.portal_write_limiter._counts.get("testclient", 0) == INTAKE  # type: ignore[attr-defined]


def test_refused_sessions_are_counted_across_routes(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    """The unproven bucket is per address, not per route: 5 refused writes spread over 5 routes, then the 6th 429s."""
    for c in _client(pools, monkeypatch, valid=False):
        _signed_in(c)
        for route in SESSION_WRITES[4:9]:
            assert _send(c, route).status_code == 403, route
        _assert_limited(_send(c, SESSION_WRITES[10]))  # the passport scan: stopped before its body is read
        # An anonymous write still has its own intake budget (separate bucket, as before the portal bucket existed).
        assert _send(c, ANONYMOUS_WRITES[12]).status_code == 422


def test_a_valid_session_gives_its_unproven_slot_back(client: TestClient) -> None:
    _signed_in(client)
    for n in range(PORTAL):
        assert _send(client, SESSION_WRITES[4]).status_code == 200, n
    assert client.app.state.portal_unproven_limiter._counts.get("testclient", 0) == 0  # type: ignore[attr-defined]
    # The 31st is stopped by the portal bucket, and that refusal does not use an unproven slot either.
    _assert_limited(_send(client, SESSION_WRITES[4]))
    assert client.app.state.portal_unproven_limiter._counts.get("testclient", 0) == 0  # type: ignore[attr-defined]


def _request(app: Any, session: str) -> Request:
    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/crm/public/v1/traveller/stage3",
        "headers": [(b"cloudfront-viewer-address", b"198.51.100.7:40000")],
        "client": ("10.0.0.1", 1),
        "app": app,
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_a_parallel_burst_cannot_pass_before_the_first_refusal(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five writes still in flight (session not checked yet) hold the five unproven slots: the sixth is 429 at once."""
    settings = pools.settings(rate_limit_intake_per_minute=INTAKE)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    async with AsyncExitStack() as stack:
        for _ in range(INTAKE):
            await stack.enter_async_context(client_pages._session_write(_request(app, SESSION), SESSION))
        with pytest.raises(HTTPException) as err:
            await stack.enter_async_context(client_pages._session_write(_request(app, SESSION), SESSION))
        assert err.value.status_code == 429 and err.value.detail == "rate_limited"
        assert app.state.portal_write_limiter._counts["198.51.100.7"] == INTAKE  # the stopped one took no portal slot
    # The five ended without proving a session: their slots stay used for the rest of the window.
    with pytest.raises(HTTPException):
        async with client_pages._session_write(_request(app, SESSION), SESSION):
            pass


@pytest.mark.asyncio
async def test_writes_that_prove_their_session_return_the_slot(pools: LocalPools) -> None:
    settings = pools.settings(rate_limit_intake_per_minute=INTAKE)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    for _ in range(INTAKE * 3):
        async with client_pages._session_write(_request(app, SESSION), SESSION):
            portal.mark_session_proven()
    assert app.state.portal_unproven_limiter._counts["198.51.100.7"] == 0
    # A proven write that then fails (403 stage3_locked, 422 ...) still gave its slot back: it was signed in.
    with pytest.raises(Forbidden):
        async with client_pages._session_write(_request(app, SESSION), SESSION):
            portal.mark_session_proven()
            raise Forbidden("stage3_locked")
    assert app.state.portal_unproven_limiter._counts["198.51.100.7"] == 0
