"""AA-1092 (S278 pre-PII review of the public CRM routes added since the AA-969 review): the fixes, offline.

1. VNPay IPN: a notice without a ``vnp_SecureHash`` of the SHA-512 hex shape is answered ``97`` before any lookup
   (the session is never looked up, no secret is opened) and counts as a miss of its address; malformed notices
   (``99``) count too; a well-formed but wrong signature counts against (its address, the merchant reference it
   names), and past :data:`app.ratelimit.IPN_REF_REFUSALS_PER_MINUTE` that address is 429 for that reference before
   the database, while another reference from the same address, and the same reference from another address (VNPay's
   genuine notice), still go through.
2. "Book now": the agency's hourly ceiling (``RATE_LIMIT_INTAKE_FORM_PER_HOUR``, bucket ``book:<form key>``) is charged
   only once the origin, honeypot and answers passed, and a booking over it is 429 ``form_rate_limited`` before any
   attempt row, Booking call or e-mail.
All names, keys and amounts are invented.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.errors import RateLimited
from app.intake import booking
from app.main import create_app
from app.payments import gateway, vnpay
from app.ratelimit import HOOK_MISSES_PER_MINUTE, IPN_REF_REFUSALS_PER_MINUTE
from app.routers import public as public_router
from tests.tokens import LocalPools
from tests.unit import test_aa1072_book_now_payments as bnp
from tests.unit.test_aa1072_book_now_payments import agency as _agency_fixture  # noqa: F401 — the fixture, renamed

KEY = "q" * 22
REF_A = "b1c2d3e4f5a6b7c8d9e0f1a2"
REF_B = "c1d2e3f4a5b6c7d8e9f0a1b2"
WELL_FORMED = "ab" * 64  # the shape of an HMAC-SHA512 hex digest, signed by nobody
IPN = "/crm/public/v1/hooks/vnpay/ipn"


# ── 1. the IPN ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_signature_shape() -> None:
    assert vnpay.signature_shaped({"vnp_SecureHash": WELL_FORMED})
    assert vnpay.signature_shaped({"vnp_SecureHash": WELL_FORMED.upper()})
    for bad in ("", "ab" * 63, "ab" * 65, "zz" * 64, WELL_FORMED + "\n"):
        assert not vnpay.signature_shaped({"vnp_SecureHash": bad}), bad
    assert not vnpay.signature_shaped({})


@pytest.mark.asyncio
async def test_an_unsigned_notice_is_refused_without_a_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def never(pool: Any, ref: str) -> Any:
        raise AssertionError("an unsigned notice must not look the session up")
        yield  # pragma: no cover

    monkeypatch.setattr(gateway, "payment_session_tx", never)
    signed = bnp._ipn(txn_ref=REF_A)
    for fields in ({k: v for k, v in signed.items() if k != "vnp_SecureHash"}, {**signed, "vnp_SecureHash": "x1"}):
        assert (await gateway.handle_ipn(None, None, fields))["RspCode"] == vnpay.BAD_SIGNATURE  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


@pytest.fixture
def client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, list[dict[str, str]]]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())

    async def pool(_request: Any) -> object:
        return object()

    monkeypatch.setattr(public_router, "request_pool", pool)
    monkeypatch.setattr(public_router, "require_keys", lambda keys: keys)
    seen: list[dict[str, str]] = []

    async def handle(_pool: Any, _keys: Any, fields: dict[str, str]) -> dict[str, str]:
        seen.append(fields)
        code = {REF_A: vnpay.BAD_SIGNATURE, REF_B: vnpay.CONFIRMED}.get(fields.get("vnp_TxnRef", ""), vnpay.OTHER)
        return vnpay.ipn_answer(code)

    monkeypatch.setattr(gateway, "handle_ipn", handle)
    with TestClient(app, base_url="https://go.test") as c:
        yield c, seen


def _notice(ref: str, signature: str | None = WELL_FORMED) -> dict[str, str]:
    out = {"vnp_TxnRef": ref, "vnp_Amount": "360000000"}
    if signature is not None:
        out["vnp_SecureHash"] = signature
    return out


def test_unsigned_notices_never_reach_the_handler_and_spend_the_address(client: tuple[TestClient, list]) -> None:
    c, seen = client
    answers = [c.get(IPN, params=_notice(REF_B, None)) for _ in range(HOOK_MISSES_PER_MINUTE)]
    assert {r.status_code for r in answers} == {200}
    assert {r.json()["RspCode"] for r in answers} == {vnpay.BAD_SIGNATURE}
    assert seen == []
    # The address has used up its misses: even a well-formed notice from it now waits.
    assert c.get(IPN, params=_notice(REF_B)).status_code == 429


def test_malformed_notices_spend_the_address(client: tuple[TestClient, list]) -> None:
    c, seen = client
    codes = [c.get(IPN, params=_notice("not-a-ref")).status_code for _ in range(HOOK_MISSES_PER_MINUTE + 1)]
    assert codes[0] == 200 and codes[-1] == 429
    assert len(seen) == HOOK_MISSES_PER_MINUTE


def test_forged_notices_lock_their_reference_only(client: tuple[TestClient, list]) -> None:
    c, seen = client
    for _ in range(IPN_REF_REFUSALS_PER_MINUTE):
        res = c.get(IPN, params=_notice(REF_A))
        assert res.status_code == 200 and res.json()["RspCode"] == vnpay.BAD_SIGNATURE
    calls = len(seen)
    locked = c.get(IPN, params=_notice(REF_A))
    assert locked.status_code == 429 and locked.headers["retry-after"]
    assert len(seen) == calls  # refused before the handler (no database work)
    # Same address, another reference: not affected (VNPay's addresses carry every agency's notices).
    other = c.get(IPN, params=_notice(REF_B))
    assert other.status_code == 200 and other.json()["RspCode"] == vnpay.CONFIRMED


def _from(address: str) -> dict[str, str]:
    return {"cloudfront-viewer-address": f"{address}:443"}


def test_a_forger_cannot_lock_a_reference_for_another_address(client: tuple[TestClient, list]) -> None:
    c, seen = client
    forger, gateway_address = "198.51.100.7", "203.0.113.20"

    # Every minute the forger spends the (address, reference) budget again; here it runs over by several notices.
    for _ in range(IPN_REF_REFUSALS_PER_MINUTE + 5):
        c.get(IPN, params=_notice(REF_A), headers=_from(forger))
    assert c.get(IPN, params=_notice(REF_A), headers=_from(forger)).status_code == 429
    calls = len(seen)
    # The gateway's notice for the same reference, from its own address, still reaches the handler.
    genuine = c.get(IPN, params=_notice(REF_A), headers=_from(gateway_address))
    assert genuine.status_code == 200
    assert len(seen) == calls + 1 and seen[-1]["vnp_TxnRef"] == REF_A


def test_the_genuine_notice_is_confirmed_after_a_forged_flood(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())

    async def pool(_request: Any) -> object:
        return object()

    monkeypatch.setattr(public_router, "request_pool", pool)
    monkeypatch.setattr(public_router, "require_keys", lambda keys: keys)

    async def handle(_pool: Any, _keys: Any, fields: dict[str, str]) -> dict[str, str]:
        # Only the gateway's own secret signs a notice that verifies (stand-in for the HMAC check).
        good = fields.get("vnp_SecureHash") == "cd" * 64
        return vnpay.ipn_answer(vnpay.CONFIRMED if good else vnpay.BAD_SIGNATURE)

    monkeypatch.setattr(gateway, "handle_ipn", handle)
    with TestClient(app, base_url="https://go.test") as c:
        for _ in range(IPN_REF_REFUSALS_PER_MINUTE * 3):
            c.get(IPN, params=_notice(REF_A), headers=_from("198.51.100.7"))
        res = c.get(IPN, params=_notice(REF_A, "cd" * 64), headers=_from("203.0.113.20"))
    assert res.status_code == 200 and res.json()["RspCode"] == vnpay.CONFIRMED


def test_the_answers_carry_no_detail(client: tuple[TestClient, list]) -> None:
    c, _ = client
    body = c.get(IPN, params=_notice(REF_A, None)).json()
    assert body == {"RspCode": vnpay.BAD_SIGNATURE, "Message": "Invalid signature"}


# ── 2. "Book now": the agency's hourly ceiling ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def book_now_agency(request: pytest.FixtureRequest) -> Any:
    """The scripted agency of the AA-1072 Book now tests (its fixture ``agency``)."""
    return request.getfixturevalue("_agency_fixture")


async def _submit(quota: Any, body: dict[str, Any], **kw: Any) -> dict[str, Any]:
    fake = bnp._fake()
    client_ = bnp.booking_client(fake)
    return await booking.submit(
        None,
        None,
        kw.pop("settings", bnp.TEST),
        bnp.ProductCache(client_),
        client_,
        KEY,
        body,  # type: ignore[arg-type]
        origin=kw.pop("origin", None),
        client_ip="198.51.100.9",
        form_quota=quota,
        quota_window_s=3600,
    )


@pytest.mark.asyncio
async def test_a_booking_over_the_agency_ceiling_is_429_before_any_attempt(book_now_agency: Any) -> None:
    charged: list[int] = []

    def full(limit: int | None = None) -> bool:
        charged.append(1)
        return False

    with pytest.raises(RateLimited) as err:
        await _submit(full, bnp._press())
    assert err.value.code == "form_rate_limited" and err.value.retry_after_s == 3600
    assert charged == [1]
    assert not book_now_agency.sql("INSERT INTO crm.public_booking_attempt")


@pytest.mark.asyncio
async def test_the_ceiling_is_not_charged_by_a_bot_or_a_bad_answer(book_now_agency: Any) -> None:
    charged: list[int] = []

    def quota(limit: int | None = None) -> bool:
        charged.append(1)
        return True

    assert await _submit(quota, bnp._press(website="spam")) == {"status": "received"}  # honeypot
    with pytest.raises(Exception):  # noqa: B017, PT011 — any refusal of the answers
        await _submit(quota, bnp._press(adults=0))
    dev = bnp.Settings(environment="dev", intake_page_origins=("https://go-dev.example.test",))
    with pytest.raises(Exception):  # noqa: B017, PT011 — the origin refusal
        await _submit(quota, bnp._press(), settings=dev, origin="https://elsewhere.example.test")
    assert charged == []


def test_the_route_charges_its_own_bucket(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())

    async def pool(_request: Any) -> object:
        return object()

    monkeypatch.setattr(public_router, "request_pool", pool)
    monkeypatch.setattr(public_router, "require_keys", lambda keys: keys)
    used: list[str] = []

    async def submit(*_: Any, form_quota: Any, quota_window_s: int, **__: Any) -> dict[str, str]:
        assert quota_window_s == int(app.state.intake_form_limiter.window_s)
        assert form_quota() is True
        used.append("ok")
        return {"status": "created", "link_path": "/c/p/x"}

    monkeypatch.setattr(booking, "submit", submit)
    with TestClient(app, base_url="https://go.test") as c:
        res = c.post(f"/crm/public/v1/book/{KEY}/bookings", json={"tour": "T-1"}, headers={"Origin": "https://go.test"})
        assert res.status_code == 201 and used == ["ok"]
        limiter = app.state.intake_form_limiter
        # The booking bucket is its own: the inquiry form's bucket for the same key was not charged.
        assert not limiter.exhausted(KEY)
        assert limiter.tracked() >= 1
