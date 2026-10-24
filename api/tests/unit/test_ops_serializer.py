"""The L1 response allow-list (AA-941, card CRM-13a R): app.ops.serializer and its place in class_router()."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from app.auth.identity_config import AGENCY, OPS, STAFF
from app.auth.route_guard import class_router
from app.margin import MarginFilteredRoute
from app.ops.serializer import (
    OPS_ALLOWED_KEYS,
    OpsAllowListRoute,
    bad_keys,
    drop_keys,
    key_allowed,
)
from app.settings import Settings

FORBIDDEN_BODY: dict[str, Any] = {
    "tenant_id": "t",
    "trip_count": 3,
    "items": [{"pipeline_stage": "won", "state": "active", "nested": {"revenue": 1}}],
    "meters": {"email": "x@example.test", "seats_used": 1},
}


@pytest.mark.parametrize(
    "key",
    [
        "trip",
        "trips",
        "trip_count",
        "trips_count",
        "pipeline",
        "pipeline_stage",
        "revenue",
        "revenue_month",
        "gross_margin",
        "commission_due",
        "gmv",
        "bookings_value",
        "booking_value",
        "invoice_total",
        "payment_status",
        "price",
        "sell_price",
        "amount",
        "net",
        "net_total",
        "proposal_id",
        "email",
        "owner_email",
        "phone",
        "display_name",
        "first_name",
        "last_name",
        "full_name",
        "passport",
        "dob",
        "address",
        "email_enc",
        "email_bidx",
    ],
)
def test_forbidden_keys_are_refused_even_if_listed(key: str) -> None:
    assert not key_allowed(key)


def test_every_allowed_key_passes_the_forbidden_patterns() -> None:
    assert all(key_allowed(k) for k in OPS_ALLOWED_KEYS)
    assert not key_allowed("network_name")  # not listed: refused although harmless (explicit list)


def test_nested_walk_and_drop() -> None:
    assert bad_keys(FORBIDDEN_BODY) == {"trip_count", "pipeline_stage", "nested", "revenue", "meters", "email"}
    assert drop_keys(FORBIDDEN_BODY) == {"tenant_id": "t", "items": [{"state": "active"}]}
    assert bad_keys([{"items": [{"status": "ok"}]}]) == set()
    assert bad_keys({1: "x"}) == {"1"}


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_email: str
    ticket_ref: str


def _app(environment: str) -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(environment=environment, db_ssl="disable" if environment == "test" else "require")
    router = class_router(OPS)
    # The class guard is not under test here: mount the same route class on a bare router.
    from fastapi import APIRouter

    bare = APIRouter(prefix="/crm/ops/v1", route_class=OpsAllowListRoute)

    @bare.get("/bad")
    async def bad() -> dict[str, Any]:
        return FORBIDDEN_BODY

    @bare.get("/good")
    async def good() -> dict[str, Any]:
        return {"items": [{"tenant_id": "t", "state": "active"}], "next_cursor": None}

    @bare.get("/text")
    async def text(request: Request) -> Any:
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse("trip_count")

    @bare.post("/validate")
    async def validate(body: In) -> dict[str, Any]:
        return {"status": "ok"}

    app.include_router(bare)
    assert router.route_class is OpsAllowListRoute
    return app


def test_strict_environments_fail_the_response(caplog: pytest.LogCaptureFixture) -> None:
    with TestClient(_app("test")) as client:
        r = client.get("/crm/ops/v1/bad")
        assert r.status_code == 500 and r.json()["detail"] == "ops_key_not_allowed"
        assert "trip_count" in r.json()["fields"] and "x@example.test" not in r.text
        assert client.get("/crm/ops/v1/good").status_code == 200
        assert client.get("/crm/ops/v1/text").text == "trip_count"


def test_dev_drops_the_keys_and_logs_names_only(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.ops.serializer"), TestClient(_app("dev")) as client:
        r = client.get("/crm/ops/v1/bad")
    assert r.status_code == 200 and r.json() == {"tenant_id": "t", "items": [{"state": "active"}]}
    logged = " ".join(rec.getMessage() for rec in caplog.records)
    assert "crm_ops_key_dropped" in logged and "email" in logged and "x@example.test" not in logged


def test_validation_errors_name_fields_never_values() -> None:
    with TestClient(_app("test")) as client:
        r = client.post("/crm/ops/v1/validate", json={"owner_email": "lan@agency.example", "extra": 1})
    assert r.status_code == 422
    assert r.json() == {
        "detail": "invalid_request",
        "message": "the request does not match crm.ops.v1",
        "fields": ["extra", "ticket_ref"],
    }
    assert "lan@agency.example" not in r.text


def test_class_router_sets_the_route_class_per_class() -> None:
    assert class_router(OPS).route_class is OpsAllowListRoute
    assert class_router(AGENCY).route_class is MarginFilteredRoute
    assert class_router(STAFF).route_class is APIRoute
