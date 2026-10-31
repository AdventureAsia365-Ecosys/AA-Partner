"""Liveness / readiness routes and the public rate limit (ADR 0010 decision 2)."""

from __future__ import annotations

import ipaddress

from fastapi.testclient import TestClient

from app.main import create_app
from app.ratelimit import FixedWindowLimiter, address_key, client_address
from app.settings import Settings


def _client(**kw: object) -> TestClient:
    settings = Settings(environment="test", db_ssl="disable", git_sha="abc123", app_version="0.1.0", **kw)
    return TestClient(create_app(settings))


def test_health_is_liveness_only() -> None:
    with _client() as c:
        r = c.get("/crm/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "aa-crm-api", "version": "0.1.0", "git_sha": "abc123"}


def test_ready_without_database_is_503() -> None:
    with _client() as c:
        r = c.get("/crm/health/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "db": "not_configured", "schema_version": None, "keys": "not_configured"}


def test_no_docs_or_schema_on_the_public_host() -> None:
    with _client() as c:
        for path in ("/docs", "/redoc", "/openapi.json", "/crm/docs"):
            assert c.get(path).status_code == 404


def test_public_routes_are_rate_limited_per_client_address() -> None:
    with _client(rate_limit_public_per_minute=3) as c:
        viewer_a = {"X-Forwarded-For": "198.51.100.7, 10.0.1.5"}
        viewer_b = {"X-Forwarded-For": "198.51.100.8, 10.0.1.5"}
        assert [c.get("/crm/health", headers=viewer_a).status_code for _ in range(4)] == [200, 200, 200, 429]
        assert c.get("/crm/health/ready", headers=viewer_a).status_code == 429
        assert c.get("/crm/health", headers=viewer_b).status_code == 200
        r = c.get("/crm/health", headers=viewer_a)
    assert r.headers["Retry-After"] == "60"
    assert r.json() == {"detail": "rate_limited"}


def test_a_spoofed_left_entry_does_not_dodge_the_limit() -> None:
    """Only the entry RATE_LIMIT_XFF_HOPS from the right counts: what the viewer prepends is ignored."""
    with _client(rate_limit_public_per_minute=2) as c:
        codes = [
            c.get("/crm/health", headers={"X-Forwarded-For": f"203.0.113.{i}, 198.51.100.7, 10.0.1.5"}).status_code
            for i in range(3)
        ]
    assert codes == [200, 200, 429]


def test_limit_zero_turns_it_off_and_the_window_resets() -> None:
    assert all(FixedWindowLimiter(0).allow("a") for _ in range(1000))
    now = [0.0]
    limiter = FixedWindowLimiter(1, clock=lambda: now[0])
    assert limiter.allow("a") and not limiter.allow("a")
    now[0] = 61.0
    assert limiter.allow("a")


def test_a_released_slot_is_free_again_only_in_its_own_window() -> None:
    """AA-953 follow-up: the client pages reserve a slot and give it back once the session proved valid."""
    now = [0.0]
    limiter = FixedWindowLimiter(1, clock=lambda: now[0])
    assert limiter.allow("a")
    window = limiter.window
    limiter.release("a", window)
    assert limiter.allow("a") and not limiter.allow("a")  # freed once; the refused allow counted too
    limiter.release("a", window)
    assert not limiter.allow("a")  # one release frees one slot, never the refusals beyond the limit
    now[0] = 61.0
    assert limiter.allow("a") and limiter.window == window + 1
    limiter.release("a", window)  # a late release of the old window
    assert not limiter.allow("a")  # never frees a slot of the new one
    limiter.release("never-seen", limiter.window)  # an unknown or evicted key: nothing to give back
    assert limiter.tracked() == 1
    off = FixedWindowLimiter(0)
    off.release("a", off.window)
    assert off.allow("a")


def test_cloudfront_viewer_address_wins_over_x_forwarded_for() -> None:
    """CloudFront sets CloudFront-Viewer-Address and a viewer cannot: it is the key when present, port stripped, and a
    different X-Forwarded-For per request does not dodge the limit (review round, AA-933)."""
    with _client(rate_limit_public_per_minute=2) as c:
        codes = [
            c.get(
                "/crm/health",
                headers={"CloudFront-Viewer-Address": f"198.51.100.7:{40000 + i}", "X-Forwarded-For": f"203.0.113.{i}"},
            ).status_code
            for i in range(3)
        ]
        other = c.get("/crm/health", headers={"CloudFront-Viewer-Address": "198.51.100.8:40000"}).status_code
    assert codes == [200, 200, 429] and other == 200


def test_ipv6_viewers_are_keyed_by_their_64() -> None:
    with _client(rate_limit_public_per_minute=2) as c:
        codes = [
            c.get("/crm/health", headers={"CloudFront-Viewer-Address": f"2001:db8:aa:1::{i:x}:443"}).status_code
            for i in range(1, 4)
        ]
        other_64 = c.get("/crm/health", headers={"CloudFront-Viewer-Address": "2001:db8:aa:2::1:443"}).status_code
    assert codes == [200, 200, 429] and other_64 == 200


class _Req:
    def __init__(self, headers: dict[str, str], peer: str | None = "127.0.0.1") -> None:
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.client = type("Peer", (), {"host": peer})() if peer is not None else None


def test_client_address_sources_and_fallbacks() -> None:
    assert client_address(_Req({"CloudFront-Viewer-Address": "203.0.113.9:1234"}), 2) == "203.0.113.9"
    assert client_address(_Req({"CloudFront-Viewer-Address": "2001:db8::5:80"}), 2) == "2001:db8::/64"
    # Not an address (or no port): fall back to the X-Forwarded-For hop, then the socket peer.
    for bad in ("garbage", "203.0.113.9", "not-an-ip:80", ""):
        req = _Req({"CloudFront-Viewer-Address": bad, "X-Forwarded-For": "1.1.1.1, 198.51.100.7, 10.0.1.5"})
        assert client_address(req, 2) == "198.51.100.7", bad
    assert client_address(_Req({"X-Forwarded-For": "2001:db8:1:2:3::9, 10.0.1.5"}), 2) == "2001:db8:1:2::/64"
    assert client_address(_Req({}), 2) == "127.0.0.1"
    assert client_address(_Req({}, peer=None), 2) == "unknown"
    assert address_key(ipaddress.ip_address("::ffff:198.51.100.7")) == "198.51.100.7"


def test_a_full_table_evicts_only_the_least_recently_seen_key() -> None:
    """A flood of new keys must not reset the count of a busy address (the old limiter cleared the whole table)."""
    limiter = FixedWindowLimiter(2, clock=lambda: 0.0, max_keys=3)
    assert limiter.allow("busy") and limiter.allow("busy")
    for i in range(10):
        limiter.allow(f"new-{i}")
        assert not limiter.allow("busy")  # still counted: busy is re-seen each round, so never the eviction victim
    assert limiter.tracked() == 3
