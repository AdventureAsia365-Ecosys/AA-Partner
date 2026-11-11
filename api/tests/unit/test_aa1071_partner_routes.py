"""AA-1071 ``/crm/v1`` sales partner routes and the ``/crm/public/v1`` partner pages: each route reaches its helper
with the parsed body, the permission matrix refuses what a role may not do (partners and approvals are the owner's,
commissions are margin fields), and the view's session cookie is set and cleared on its own path (the services run on
PostgreSQL in tests/integration/test_aa1071_partners_api.py)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.main import create_app
from app.partners import public as partner_public
from app.partners import service
from app.routers import partner_pages
from app.routers import partners as router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx

ID = "00000000-0000-4000-8000-0000000071a1"
TOKEN = "A" * 43


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _stub(calls: list[tuple[str, Any]], name: str, result: Any = None) -> Any:
    async def fn(*args: Any, **kwargs: Any) -> Any:
        calls.append((name, kwargs or args[2:] if len(args) > 2 else kwargs))
        return result if result is not None else {"ok": name}

    return fn


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> tuple[TestClient, dict[str, str]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    ctx = tenant_ctx(ScriptedConn(), role=role)

    def tx(*a: Any, **kw: Any) -> Any:
        margin.current_member_role.set(role)
        return fake_tx(ctx)(*a, **kw)

    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", tx)
    monkeypatch.setattr(router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for name in (
        "list_partners", "create_partner", "get_partner", "update_partner", "delete_partner", "issue_link",
        "revoke_link", "set_trip_partner", "recalculate", "list_commissions", "approve", "unapprove", "pay", "cancel",
    ):  # fmt: skip
        monkeypatch.setattr(service, name, _stub(out, name))
    monkeypatch.setattr(service, "statement_csv", _stub(out, "statement_csv", ("statement.csv", "trip_ref\r\n")))
    return out


ROUTES = [
    ("GET", "/crm/v1/sales-partners", None, 200),
    ("POST", "/crm/v1/sales-partners", {"kind": "referrer", "name": "Blue Kite", "rate": "7.5"}, 201),
    ("GET", f"/crm/v1/sales-partners/{ID}", None, 200),
    ("PATCH", f"/crm/v1/sales-partners/{ID}", {"status": "inactive", "contact_email": None}, 200),
    ("DELETE", f"/crm/v1/sales-partners/{ID}", None, 200),
    ("POST", f"/crm/v1/sales-partners/{ID}/links", {"purpose": "view", "days": 30}, 201),
    ("POST", f"/crm/v1/sales-partners/{ID}/links/{ID}/revoke", None, 200),
    ("GET", f"/crm/v1/sales-partners/{ID}/statement", None, 200),
    ("PUT", f"/crm/v1/trips/{ID}/sales-partner", {"sales_partner_id": ID}, 200),
    ("POST", f"/crm/v1/trips/{ID}/partner-commission/sync", None, 200),
    ("GET", "/crm/v1/partner-commissions?status=open", None, 200),
    ("POST", f"/crm/v1/partner-commissions/{ID}/approve", None, 200),
    ("POST", f"/crm/v1/partner-commissions/{ID}/unapprove", None, 200),
    ("POST", f"/crm/v1/partner-commissions/{ID}/pay", {"reference": "TT-1"}, 200),
    ("POST", f"/crm/v1/partner-commissions/{ID}/cancel", None, 200),
]


def test_every_route_reaches_its_helper(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "owner")
    with client:
        for method, path, body, status in ROUTES:
            res = client.request(method, path, headers=h, json=body)
            assert res.status_code == status, (method, path, res.text)
    assert [n for n, _ in calls] == [
        "list_partners", "create_partner", "get_partner", "update_partner", "delete_partner", "issue_link",
        "revoke_link", "statement_csv", "set_trip_partner", "recalculate", "list_commissions", "approve", "unapprove",
        "pay", "cancel",
    ]  # fmt: skip
    patched = dict(calls)["update_partner"]
    assert patched[-1] == {"status": "inactive", "contact_email": None}  # unset fields are not sent


def test_the_statement_is_a_csv_download(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "agent")
    with client:
        res = client.get(f"/crm/v1/sales-partners/{ID}/statement", headers=h)
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    assert 'filename="statement.csv"' in res.headers["content-disposition"] and res.text == "trip_ref\r\n"


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("agent", {"GET /crm/v1/sales-partners", f"GET /crm/v1/sales-partners/{ID}/statement",
                   f"PUT /crm/v1/trips/{ID}/sales-partner", f"POST /crm/v1/trips/{ID}/partner-commission/sync",
                   "GET /crm/v1/partner-commissions?status=open"}),
        ("assistant", {"GET /crm/v1/sales-partners", f"PUT /crm/v1/trips/{ID}/sales-partner"}),
    ],
)  # fmt: skip
def test_the_matrix_keeps_partners_and_money_from_other_roles(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list, role: str, allowed: set[str]
) -> None:
    client, h = _client(pools, monkeypatch, role)
    with client:
        for method, path, body, _ in ROUTES:
            res = client.request(method, path, headers=h, json=body)
            want = f"{method} {path}" in allowed
            assert (res.status_code < 400) == want, (role, method, path, res.status_code)
            if not want:
                assert res.status_code == 403 and res.json()["detail"] == "role_forbidden"


def test_a_partner_body_is_checked(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    client, h = _client(pools, monkeypatch, "owner")
    with client:
        for body in (
            {"kind": "cousin", "name": "X"},
            {"kind": "referrer", "name": ""},
            {"kind": "referrer", "name": "X", "rate": "-1"},
            {"kind": "referrer", "name": "X", "payout_due_days": 400},
            {"kind": "referrer", "name": "X", "extra": 1},
        ):
            assert client.post("/crm/v1/sales-partners", headers=h, json=body).status_code == 422, body
    assert calls == []


# ── /crm/public/v1 partner pages ─────────────────────────────────────────────────────────────────


@pytest.fixture
def public(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, list]]:
    settings = pools.settings(rate_limit_intake_per_minute=50)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(partner_pages, "request_pool", _pool)
    monkeypatch.setattr(partner_pages, "require_keys", lambda keys: keys)
    out: list[tuple[str, Any]] = []
    monkeypatch.setattr(partner_public, "read_form", _stub(out, "read_form", {"fields": []}))
    monkeypatch.setattr(partner_public, "home", _stub(out, "home", {"signed_in": True}))
    monkeypatch.setattr(partner_public, "request_code", _stub(out, "request_code", {"sent": True}))
    monkeypatch.setattr(partner_public, "sign_out", _stub(out, "sign_out", {"signed_in": False}))

    async def submit(*args: Any, form_quota: Any = None, **kw: Any) -> bool:
        out.append(("submit", {"quota": form_quota("f1")}))
        return True

    async def verify(_pool: Any, _settings: Any, token: str, code: Any, **_: Any) -> tuple[Any, dict]:
        out.append(("verify_code", {"token": token, "code": code}))
        if code == "123456":
            return "session-token", {"signed_in": True}
        return None, {"detail": "code_invalid", "attempts_left": 4}

    monkeypatch.setattr(partner_public, "submit", submit)
    monkeypatch.setattr(partner_public, "verify_code", verify)
    with TestClient(app, base_url="https://go.test") as c:
        yield c, out


def test_the_intake_link_routes(public: tuple[TestClient, list]) -> None:
    client, out = public
    res = client.get(f"/crm/public/v1/r/{TOKEN}")
    assert res.status_code == 200 and res.headers["referrer-policy"] == "no-referrer"
    sent = client.post(f"/crm/public/v1/r/{TOKEN}/submissions", json={"name": "Lan"})
    assert sent.status_code == 202 and sent.json() == {"status": "received"}
    assert client.get("/crm/public/v1/r/short").status_code == 422
    big = client.post(f"/crm/public/v1/r/{TOKEN}/submissions", content=b"x" * 40_000,
                      headers={"content-type": "application/json"})  # fmt: skip
    assert big.status_code == 413
    assert [n for n, _ in out] == ["read_form", "submit"] and out[1][1] == {"quota": True}


def test_the_view_needs_its_cookie_and_sets_the_session_on_its_path(public: tuple[TestClient, list]) -> None:
    client, out = public
    assert client.get("/crm/public/v1/partner").status_code == 404  # no link cookie
    client.cookies.set(partner_public.LINK_COOKIE, TOKEN, path="/crm/public/v1/partner")
    assert client.get("/crm/public/v1/partner").json() == {"signed_in": True}
    assert client.post("/crm/public/v1/partner/code").status_code == 202
    wrong = client.post("/crm/public/v1/partner/session", json={"code": "000000"})
    assert wrong.status_code == 422 and "set-cookie" not in wrong.headers
    ok = client.post("/crm/public/v1/partner/session", json={"code": "123456"})
    cookie = ok.headers["set-cookie"]
    assert ok.status_code == 200 and cookie.startswith(f"{partner_public.SESSION_COOKIE}=session-token")
    for part in ("HttpOnly", "Secure", "SameSite=strict", "Path=/crm/public/v1/partner"):
        assert part.lower() in cookie.lower(), part
    gone = client.post("/crm/public/v1/partner/sign-out")
    assert gone.status_code == 200 and "max-age=0" in gone.headers["set-cookie"].lower()
    assert [n for n, _ in out] == ["home", "request_code", "verify_code", "verify_code", "sign_out"]
    client.cookies.set(partner_public.LINK_COOKIE, "bad token", path="/crm/public/v1/partner")
    assert client.get("/crm/public/v1/partner").status_code == 404
