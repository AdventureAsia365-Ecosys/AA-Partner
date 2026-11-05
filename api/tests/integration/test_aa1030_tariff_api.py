"""AA-1030: ``GET /crm/v1/aa/tariff`` end to end on PostgreSQL 15 as crm_app (real verifier, real ``tenant_tx()``, real
margin route class, the audit table), against the in-memory Booking of ``tests/aa_fakes.py``.

- owner and agent read the JSON and download Booking's CSV (200, ``no-store``); the assistant gets 403
  ``role_forbidden`` and Booking is never asked;
- each read leaves one ``aa_tariff_view`` audit event (format and currency, never an amount); a refusal leaves a
  ``denied`` one that survives the rollback;
- ``/crm/v1/me`` answers ``permissions.aa_tariff_view`` per role (the web hides the AA prices links by it);
- an agency Booking does not know (or suspended) is 409 ``aa_partner_not_active``.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import httpx
import pytest

from app.main import create_app
from tests.aa_fakes import FakeBooking, booking_client, tariff_csv
from tests.integration.test_crm005_intake import _agency
from tests.tokens import LocalPools

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ROLES = ("owner", "agent", "assistant")


async def _events(pool: asyncpg.Pool, tenant: Any) -> list[tuple[str, str, str | None]]:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        await conn.execute("SELECT set_config('app.actor', 'job:test', true)")
        rows = await conn.fetch(
            "SELECT outcome, reason AS reason_code, object_id FROM crm.audit_event "
            "WHERE action = 'aa_tariff_view' ORDER BY at, event_id"
        )
    return [(r["outcome"], r["reason_code"], r["object_id"]) for r in rows]


async def test_the_aa_tariff_route_by_role_with_its_audit(app_pool: asyncpg.Pool, migrated_app_dsn: str) -> None:
    tenant, people = await _agency(app_pool, *ROLES)
    who = {role: people[i][0] for i, role in enumerate(ROLES)}
    fake = FakeBooking(tier_code="gold")
    pools = LocalPools()
    settings = pools.settings(database_url=migrated_app_dsn)
    app = create_app(settings, verifier=pools.verifier(settings), keys=object(), booking_client=booking_client(fake))
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://crm.test") as client:

            def headers(role: str) -> dict[str, str]:
                token = pools.partner_web(sub=who[role].sub)
                return {"Authorization": f"Bearer {token}", "X-AA-Tenant-Id": str(tenant)}

            for role in ("owner", "agent"):
                r = await client.get("/crm/v1/aa/tariff?currency=EUR", headers=headers(role))
                assert r.status_code == 200, r.text
                assert r.headers["cache-control"] == "no-store"
                body = r.json()
                assert body["tier_code"] == "gold" and len(body["items"]) == 2
                assert body["items"][0]["b2b_price"] == {"amount": "776.00", "currency": "EUR"}
                me = await client.get("/crm/v1/me", headers=headers(role))
                assert me.json()["permissions"] == {"aa_tariff_view": True}
            r = await client.get("/crm/v1/aa/tariff?format=csv", headers=headers("owner"))
            assert r.status_code == 200 and r.content == tariff_csv("USD")
            assert r.headers["content-type"] == "text/csv; charset=utf-8"
            assert 'filename="aa-tariff-invented-agency-usd-2031-01-05.csv"' in r.headers["content-disposition"]
            assert r.headers["cache-control"] == "no-store"
            asked = fake.calls["GET tariff"]

            for fmt in ("json", "csv"):
                r = await client.get(f"/crm/v1/aa/tariff?format={fmt}", headers=headers("assistant"))
                assert (r.status_code, r.json()["detail"]) == (403, "role_forbidden")
            assert fake.calls["GET tariff"] == asked == 3  # the assistant's calls never reached Booking
            me = await client.get("/crm/v1/me", headers=headers("assistant"))
            assert me.status_code == 200 and me.json()["permissions"] == {"aa_tariff_view": False}

            fake.tariff_refusal = "unknown_partner"  # no partner row, or AA suspended the agency
            r = await client.get("/crm/v1/aa/tariff", headers=headers("agent"))
            assert (r.status_code, r.json()["detail"]) == (409, "aa_partner_not_active")
    finally:
        await app.state.db.close()

    events = await _events(app_pool, tenant)
    assert events == [
        ("allowed", "format_json", "EUR"),
        ("allowed", "format_json", "EUR"),
        ("allowed", "format_csv", "USD"),
        ("denied", "role_forbidden", None),
        ("denied", "role_forbidden", None),
        ("allowed", "format_json", "USD"),
    ]
