"""AA-953 follow-up: the two per-address write buckets of the client pages (``app.routers.client_pages``).

Anonymous and pre-sign-in writes keep the intake limiter (``RATE_LIMIT_INTAKE_PER_MINUTE``, 5 per address: the
inquiry form's anti-abuse ceiling). Writes only a signed-in traveller can make take the portal write limiter
(``RATE_LIMIT_PORTAL_WRITE_PER_MINUTE``, default 30) when the request carries a session cookie, so a family filling the
portal from one home address is not stopped at the sixth write. The services are stubbed: only the limit is tested.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

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


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, **limits: int) -> Iterator[TestClient]:
    settings = pools.settings(rate_limit_intake_per_minute=INTAKE, **limits)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    app.state.documents = object()
    monkeypatch.setattr(client_pages, "request_pool", _pool)
    monkeypatch.setattr(client_pages, "require_keys", lambda keys: keys)
    monkeypatch.setattr(client_pages, "require_store", lambda store: store)
    calls: list[tuple[str, Any]] = []
    for name in ("submit_stage3", "sign_out", "request_code", "switch", "save_flights", "upload_passport_scan",
                 "invite_companion"):  # fmt: skip
        monkeypatch.setattr(portal, name, _stub(calls, name))
    for name in ("accept", "reaccept_terms", "pick", "change_request", "claim_payment", "card_payment"):
        monkeypatch.setattr(proposal_page, name, _stub(calls, name))

    async def hold(*_: Any, **__: Any) -> tuple[int, dict]:
        return 200, {"status": "held"}

    async def sign(*_: Any, **__: Any) -> tuple[bool, dict]:
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
