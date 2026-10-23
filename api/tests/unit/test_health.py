"""Liveness / readiness routes and the public rate limit (ADR 0010 decision 2)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.ratelimit import FixedWindowLimiter
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
    assert r.json() == {"status": "not_ready", "db": "not_configured", "schema_version": None}


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
