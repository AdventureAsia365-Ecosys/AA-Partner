"""AA-953 (card CRM-24a) without a database: who reaches whom (Fix E4), the flight rules (R33), the code of a companion
without an address going to the lead's address, and the cookie routes of the traveller portal (§2.9 rule 1)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.errors import Invalid
from app.main import create_app
from app.portal import person
from app.portal import public as portal
from app.routers import client_pages
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import enc
from tests.unit.test_lifecycle_services import ACCESS, ECHO, LEAD, TOKEN, _access_row, _portal
from tests.unit.test_thin_lifecycle_routes import _pool, _stub

CHILD = uuid.UUID("00000000-0000-4000-8000-0000000009c1")
ADULT = uuid.UUID("00000000-0000-4000-8000-0000000009c2")
ORPHAN = uuid.UUID("00000000-0000-4000-8000-0000000009c3")
ACCESS_CHILD = uuid.UUID("00000000-0000-4000-8000-0000000009d1")
ACCESS_ADULT = uuid.UUID("00000000-0000-4000-8000-0000000009d2")
DOC = "00000000-0000-4000-8000-0000000009e1"


def _m(traveler: uuid.UUID, role: str, email: bool, access: uuid.UUID | None) -> person.Member:
    return person.Member(traveler, role, email, None, access, "active" if access else None)


PARTY = [
    _m(LEAD, "lead", True, ACCESS),
    _m(ADULT, "companion", True, ACCESS_ADULT),
    _m(CHILD, "companion", False, ACCESS_CHILD),
    _m(ORPHAN, "companion", False, None),  # no portal access yet
]


def test_who_reaches_whom() -> None:
    assert person.delivery_of(PARTY, LEAD) == LEAD
    assert person.delivery_of(PARTY, ADULT) == ADULT
    assert person.delivery_of(PARTY, CHILD) == LEAD  # Fix E4: through the lead's address
    assert person.delivery_of(PARTY, uuid.uuid4()) is None
    assert [m.traveler_id for m in person.group_of(PARTY, LEAD)] == [LEAD, CHILD]  # ORPHAN has no access
    assert [m.traveler_id for m in person.group_of(PARTY, CHILD)] == [LEAD, CHILD]
    assert [m.traveler_id for m in person.group_of(PARTY, ADULT)] == [ADULT]
    no_lead_address = [_m(LEAD, "lead", False, ACCESS), _m(CHILD, "companion", False, ACCESS_CHILD)]
    assert person.delivery_of(no_lead_address, CHILD) is None and person.group_of(no_lead_address, CHILD) == []
    assert person.delivery_of(no_lead_address, LEAD) is None


def test_flight_rules() -> None:
    raw = {"direction": "arrival", "flight_no": "ra 401", "airport": " ktm", "local_time": "2026-11-30T23:55",
           "tz": "Asia/Kathmandu"}  # fmt: skip
    direction, flight = person.clean_flight(raw)
    assert direction == "arrival" and flight == ("RA401", "KTM", dt.datetime(2026, 11, 30, 23, 55), "Asia/Kathmandu")
    view = person.flight_view("arrival", *flight, dt.date(2026, 12, 1))  # type: ignore[misc]
    # R33: a +05:45 zone, and an arrival late the night before the first day.
    assert view["utc_offset"] == "+05:45" and view["days_from_start"] == -1 and view["local_date"] == "2026-11-30"
    assert person.clean_flight({"direction": "departure", "remove": True}) == ("departure", None)
    for bad, fields in (
        ({**raw, "flight_no": "A"}, ["flight_no"]),
        ({**raw, "airport": "KTMX"}, ["airport"]),
        ({**raw, "local_time": "2026-11-30T23:55+05:45"}, ["local_time"]),  # local time, no zone
        ({**raw, "local_time": "1990-01-01T10:00"}, ["local_time"]),
        ({**raw, "tz": "Asia/Atlantis"}, ["tz"]),
        ({**raw, "tz": "../etc"}, ["tz"]),
    ):
        with pytest.raises(Invalid) as err:
            person.clean_flight(bad)
        assert err.value.extra["fields"] == fields
    with pytest.raises(Invalid):
        person.clean_flight({"direction": "layover"})


@pytest.mark.asyncio
async def test_a_childs_code_goes_to_the_leads_address(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"traveler_id": LEAD, "role": "lead", "has_email": True, "display_name_enc": None, "access_id": ACCESS,
         "access_status": "active"},
        {"traveler_id": CHILD, "role": "companion", "has_email": False, "display_name_enc": None,
         "access_id": ACCESS_CHILD, "access_status": "invited"},
    ]  # fmt: skip
    conn = (
        ScriptedConn()
        .on(
            "FROM crm.traveler_access a JOIN crm.trip t",
            _access_row(access_id=ACCESS_CHILD, traveler_id=CHILD, role="companion"),
        )  # fmt: skip
        .on("AS has_email", rows)
        .on("SELECT email_enc FROM crm.traveler", enc("traveler", "email_enc", "mira.castell@example.test"))
    )
    _portal(monkeypatch, conn)
    out = await portal.request_code(object(), object(), ECHO, TOKEN, purpose="sign_in", session=None, origin=None)  # type: ignore[arg-type]
    assert out["dev_code"]
    assert conn.sql("SELECT email_enc FROM crm.traveler") == [(LEAD,)]  # the lead's address, not the child's
    code_row = conn.sql("INSERT INTO crm.otp_challenge")[0]
    assert code_row[1] == ACCESS_CHILD and code_row[3] == CHILD  # the code still belongs to the child's access


@pytest.mark.asyncio
async def test_a_bad_pick_is_refused_before_anything_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Invalid):
        await portal.verify_code(object(), ECHO, TOKEN, "123456", origin=None, traveler_id="not-an-id")  # type: ignore[arg-type]
    with pytest.raises(Invalid):
        await portal.switch(object(), ECHO, TOKEN, "s", {}, origin=None)  # type: ignore[arg-type]


# ── the cookie routes (/crm/public/v1/traveller) ──────────────────────────────────────────────────────────────


@pytest.fixture
def pools() -> LocalPools:
    return LocalPools()


@pytest.fixture
def traveller(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, list]]:
    settings = pools.settings(rate_limit_intake_per_minute=100)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    app.state.documents = object()
    monkeypatch.setattr(client_pages, "request_pool", _pool)
    monkeypatch.setattr(client_pages, "require_keys", lambda keys: keys)
    out: list[tuple[str, Any]] = []
    for name, result in (
        ("home", {"signed_in": True}),
        ("request_code", {"sent": True}),
        ("submit_stage3", {"status": "received"}),
        ("sign_out", {"signed_in": False}),
        ("switch", {"signed_in": True}),
        ("save_flights", {"flights": []}),
        ("upload_passport_scan", {"received": True}),
        ("open_document", {"document_id": DOC}),
        ("invite_companion", {"reach": "own"}),
    ):
        monkeypatch.setattr(portal, name, _stub(out, f"portal.{name}", result))

    async def verify(_pool: Any, _settings: Any, token: str, code: Any, **kw: Any) -> tuple[Any, dict]:
        out.append(("portal.verify_code", {"token": token, "code": code, "traveler_id": kw.get("traveler_id")}))
        if code == "123456" and kw.get("traveler_id") is None:
            return None, {"signed_in": False, "pick": [{"traveler_id": str(LEAD)}, {"traveler_id": str(CHILD)}]}
        if code == "123456":
            return "session-token", {"signed_in": True}
        return None, {"detail": "code_invalid", "attempts_left": 4}

    async def sign(*args: Any, **kw: Any) -> tuple[bool, dict]:
        out.append(("portal.sign", args[3]))
        return True, {"status": "signed"}

    monkeypatch.setattr(portal, "verify_code", verify)
    monkeypatch.setattr(portal, "sign", sign)
    with TestClient(app, base_url="https://go.test") as c:
        yield c, out


def test_no_link_cookie_is_not_found(traveller: tuple[TestClient, list]) -> None:
    client, out = traveller
    for method, path in (("GET", ""), ("POST", "/code"), ("POST", "/session"), ("PUT", "/flights")):
        res = client.request(method, f"/crm/public/v1/traveller{path}", json={})
        assert res.status_code == 404 and res.json()["detail"] == "link_not_found", path
    client.cookies.set(client_pages.PORTAL_LINK_COOKIE, "short", domain="go.test", path="/crm/public/v1/traveller")
    assert client.get("/crm/public/v1/traveller").status_code == 404
    assert out == []


def test_the_cookie_routes(traveller: tuple[TestClient, list]) -> None:
    client, out = traveller
    client.cookies.set(client_pages.PORTAL_LINK_COOKIE, TOKEN, domain="go.test", path="/crm/public/v1/traveller")
    home = client.get("/crm/public/v1/traveller")
    assert home.status_code == 200 and home.headers["cache-control"] == "no-store"
    assert client.post("/crm/public/v1/traveller/code", json={"purpose": "sign_in"}).status_code == 202
    pick = client.post("/crm/public/v1/traveller/session", json={"code": "123456"})
    assert pick.status_code == 200 and len(pick.json()["pick"]) == 2 and "set-cookie" not in pick.headers
    wrong = client.post("/crm/public/v1/traveller/session", json={"code": "000000"})
    assert wrong.status_code == 422
    ok = client.post("/crm/public/v1/traveller/session", json={"code": "123456", "traveler_id": str(CHILD)})
    cookie = ok.headers["set-cookie"]
    for part in (f"{portal.SESSION_COOKIE}=session-token", "HttpOnly", "Secure", "SameSite=strict",
                 "Path=/crm/public/v1/traveller"):  # fmt: skip
        assert part.lower() in cookie.lower(), part
    verified = [kw for n, kw in out if n == "portal.verify_code"]
    assert verified[-1] == {"token": TOKEN, "code": "123456", "traveler_id": str(CHILD)}
    calls = (
        ("POST", "/switch", {"traveler_id": str(LEAD)}, 200, "portal.switch"),
        ("PUT", "/stage3", {"dob": "2015-04-02"}, 200, "portal.submit_stage3"),
        ("POST", "/sign", {"code": "654321"}, 200, "portal.sign"),
        ("PUT", "/flights", {"flights": []}, 200, "portal.save_flights"),
        ("POST", "/companions", {"traveler_id": str(CHILD)}, 200, "portal.invite_companion"),
        ("GET", f"/documents/{DOC}", None, 200, "portal.open_document"),
    )
    for method, path, body, status, name in calls:
        res = client.request(method, f"/crm/public/v1/traveller{path}", json=body)
        assert res.status_code == status, (path, res.text)
        assert out[-1][0] == name
    # A passport scan may be larger than the 32 KiB of every other client write, up to its own cap.
    big = {"filename": "p.jpg", "content_type": "image/jpeg", "content_b64": "A" * (200 * 1024)}
    assert client.post("/crm/public/v1/traveller/passport-scan", json=big).status_code == 200
    assert client.put("/crm/public/v1/traveller/flights", json={"x": "A" * (40 * 1024)}).status_code == 413
    huge = client.post(
        "/crm/public/v1/traveller/passport-scan",
        content=b"{" + b" " * (client_pages.SCAN_BODY_BYTES + 1) + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert huge.status_code == 413
    out_res = client.post("/crm/public/v1/traveller/sign-out")
    assert out_res.status_code == 200 and "max-age=0" in out_res.headers["set-cookie"].lower()
    assert "path=/crm/public/v1/traveller" in out_res.headers["set-cookie"].lower()
