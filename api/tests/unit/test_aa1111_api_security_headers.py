"""AA-1111 (S294, ZAP baseline): nosniff + HSTS on every CRM API response, no Server header from uvicorn."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Response
from fastapi.testclient import TestClient

from app.bodylimit import BodyLimitMiddleware
from app.main import create_app
from app.observability.request_id import RequestIdMiddleware
from app.observability.security_headers import SECURITY_HEADERS, SecurityHeadersMiddleware
from app.settings import Settings

HSTS = "max-age=63072000; includeSubDomains"
API = Path(__file__).resolve().parents[2]


def _settings() -> Settings:
    return Settings(environment="test", db_ssl="disable", git_sha="abc123", app_version="0.1.0")


def _app():
    app = create_app(_settings())
    probe = APIRouter()

    @probe.get("/crm/_own_headers")
    async def _own() -> Response:
        return Response("x", media_type="text/plain", headers={"X-Content-Type-Options": "custom"})

    @probe.get("/crm/_boom")
    async def _boom() -> dict:
        raise RuntimeError("boom")

    app.include_router(probe)
    return app


def test_values() -> None:
    assert SECURITY_HEADERS == {"X-Content-Type-Options": "nosniff", "Strict-Transport-Security": HSTS}
    assert "preload" not in HSTS


def test_health_404_and_refusal_carry_the_headers() -> None:
    with TestClient(_app()) as client:
        for path in ("/crm/health", "/crm/health/ready", "/crm/no-such-path", "/crm/v1/clients"):
            r = client.get(path)
            assert r.headers["x-content-type-options"] == "nosniff", (path, r.status_code)
            assert r.headers["strict-transport-security"] == HSTS, (path, r.status_code)
            assert "server" not in r.headers, path


def test_a_header_the_endpoint_set_wins() -> None:
    with TestClient(_app()) as client:
        r = client.get("/crm/_own_headers")
    assert r.headers["x-content-type-options"] == "custom"
    assert r.headers["strict-transport-security"] == HSTS


def test_unhandled_500_carries_the_headers() -> None:
    with TestClient(_app(), raise_server_exceptions=False) as client:
        r = client.get("/crm/_boom")
    assert r.status_code == 500
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["strict-transport-security"] == HSTS
    assert r.headers["x-request-id"]


def test_registered_between_the_request_id_and_the_body_ceiling() -> None:
    classes = [m.cls for m in create_app(_settings()).user_middleware]
    assert classes[:3] == [RequestIdMiddleware, SecurityHeadersMiddleware, BodyLimitMiddleware]


def test_image_starts_uvicorn_without_the_server_header() -> None:
    cmd = next(line for line in (API / "Dockerfile").read_text().splitlines() if line.startswith("CMD ["))
    assert '"uvicorn"' in cmd and '"--no-server-header"' in cmd
