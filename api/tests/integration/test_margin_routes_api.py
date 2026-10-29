"""Milestone C-M2 "field-filter test on every route" on real data (AA-939 card R, design v3 §4.6 Fix B12 / B13).

``tests/unit/test_margin_filter.py`` proves the margin serializer is on every ``/crm/v1`` route with synthetic bodies;
the cards that added money (AA-944 ledger, AA-947 AA lines, AA-949 builder) re-assert it on their own services. This
test closes the gap between the two: ONE agency on PostgreSQL holds every kind of margin-bearing money the CRM stores
today, each with a value that appears nowhere else, and EVERY ``GET /crm/v1`` route of the live app is called over
HTTP (real verifier, real ``tenant_tx()``, real serializers, real route class) as the owner and as the assistant:

- an agency supplier with a default commission (``agency_supplier.default_commission_pct``);
- a confirmed agency line with cost, sell and expected commission (``booking_line_money``; margin derived);
- an AA line quoted by Booking (cost = the quote's b2b price at the quote lock, written by the sync job);
- a day-by-day proposal with an agency option (typed net) and an AA option (net = the live quote's b2b price).

Card R: the assistant's bodies carry no margin-family key (``app.margin.MARGIN_KEY``) and none of the planted
cost-side values (value-based, so a ``{"kind": "cost", "amount": ...}`` row the key filter cannot see still fails).
The owner's bodies must carry every planted value somewhere (the positive control: otherwise the assistant check
proves nothing). A new ``GET /crm/v1`` route with a path parameter this seed cannot fill fails until it is classified
in :data:`UNSEEDED`.
"""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.booking import aa_lines
from app.ledger import lines, suppliers
from app.main import create_app
from app.margin import MARGIN_KEY
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.aa_fakes import FakeBooking, booking_client
from tests.integration.test_aa_lines_api import _sync
from tests.integration.test_builder_api import _aa, _block, _catalog_tour, _create, _option, _pid
from tests.integration.test_builder_api import _setup as _builder_setup
from tests.tokens import LocalPools

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Cost-side values, each unique in the seed. Sell-side values (the assistant may see them) are chosen apart.
AGENCY_NET = "4321.17"  # builder agency option: net_amount
AA_OPTION_NET = "765.29"  # builder AA option: net = the live quote's b2b price
LINE_COST = "2468.13"  # agency line: cost
LINE_COMMISSION = "135.79"  # agency line: commission_expected
LINE_MARGIN = "1111.11"  # agency line: sell 3579.24 - cost 2468.13 (derived by the ledger)
AA_LINE_COST = "876.43"  # AA line: cost from Booking's quote
SUPPLIER_COMMISSION_PCT = "7.25"  # agency supplier: default_commission_pct
COST_SIDE = (AGENCY_NET, AA_OPTION_NET, LINE_COST, LINE_COMMISSION, LINE_MARGIN, AA_LINE_COST, SUPPLIER_COMMISSION_PCT)

# GET /crm/v1 routes whose path parameter names an object this seed does not create, with why none of them can carry
# margin-bearing money (a new money route must be seeded here instead).
UNSEEDED = {
    "/crm/v1/conversations/{conversation_id}/messages": "mail messages: no money table behind them",
    "/crm/v1/mail/imports/{import_id}": "mailbox history import status: counts only",
    "/crm/v1/lifecycle/offboarding/{request_id}/export": "owner-only agency export: never the assistant",
}


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def _walk(routes: list[Any]) -> list[Any]:
    out: list[Any] = []
    for r in routes:
        inner = getattr(r, "original_router", None)  # FastAPI >= 0.140 wraps an included router
        out.extend(_walk(inner.routes) if inner is not None else [r])
    return out


def _get_routes(app: FastAPI) -> list[str]:
    return sorted(
        r.path
        for r in _walk(app.routes)
        if isinstance(r, APIRoute) and r.path.startswith("/crm/v1") and "GET" in r.methods
    )


async def _seed(pool: asyncpg.Pool) -> dict[str, Any]:
    s = await _builder_setup(pool)  # agency (owner, agent, assistant), a Nepal trip at Proposal with its lead
    agent = s["who"]["agent"]

    # The day-by-day proposal: an agency option with a typed net, an AA option priced from a live quote.
    await _catalog_tour(pool)
    fake = FakeBooking(b2b_price=AA_OPTION_NET)
    v = await _create(pool, s)
    v = await _option(pool, s, v, _block(v, 1), title="Guesthouse", sell_amount=Decimal("5432.19"),
                      net_amount=Decimal(AGENCY_NET))  # fmt: skip
    v = await _aa(pool, s, fake, v, sell_amount=Decimal("1234.56"))

    # The ledger: an own supplier with a default commission and a confirmed line with cost, sell and commission.
    async with tenant_tx(pool, agent, s["tenant"]) as ctx:
        own = await suppliers.create_supplier(
            ctx,
            Vault(s["keys"], ctx),
            {"name": "Kathmandu Lodge", "category": "accommodation", "default_commission_pct": SUPPLIER_COMMISSION_PCT},
        )
        line = await lines.create_line(
            ctx,
            s["trip"],
            {"agency_supplier_id": uuid.UUID(own["agency_supplier_id"]), "title": "Lodge 2 nights",
             "confirmation_no": "KL-4410"},
        )  # fmt: skip
        line_id = uuid.UUID(line["booking_line_id"])
        await lines.put_money(
            ctx,
            line_id,
            {
                "cost": {"amount": LINE_COST, "currency": "USD"},
                "sell": {"amount": "3579.24", "currency": "USD"},
                "commission_expected": {"amount": LINE_COMMISSION, "currency": "USD"},
            },
        )
        confirmed = await lines.confirm(ctx, line_id)
    assert confirmed["status"] == "confirmed" and confirmed["money"]["margin_base"] == LINE_MARGIN

    # An AA line: requested by the agent, quoted by Booking through the sync job (cost at the quote lock). The tour is
    # the builder's own catalog row (the AA lines tests retire T-100 and leave the shared catalog sync in backoff).
    async with tenant_tx(pool, agent, s["tenant"]) as ctx:
        aa_line = await aa_lines.request_line(
            ctx,
            s["trip"],
            {"tour_source_id": "T-949", "component_id": "tp:1", "component_kind": "trip_plan",
             "service_date": s["start"].isoformat(), "pax": {"adult": 2, "child": 0, "infant": 0},
             "currency": "USD", "sell_amount": "1900"},
        )  # fmt: skip
    line_fake = FakeBooking(b2b_price=AA_LINE_COST)
    await _sync(pool, line_fake, s["tenant"])
    s.update(
        proposal=_pid(v),
        supplier=uuid.UUID(own["agency_supplier_id"]),
        line=line_id,
        aa_line=uuid.UUID(aa_line["booking_line_id"]),
        fake=fake,
    )
    return s


def _params(s: dict[str, Any], lead: str) -> dict[str, str]:
    return {
        "trip_id": str(s["trip"]),
        "proposal_id": str(s["proposal"]),
        "supplier_id": str(s["supplier"]),
        "line_id": str(s["line"]),
        "traveler_id": lead,
        "source_id": "T-949",
    }


def _fill(path: str, params: dict[str, str]) -> str | None:
    names = re.findall(r"\{([^}]+)\}", path)
    if any(n not in params for n in names):
        return None
    return re.sub(r"\{([^}]+)\}", lambda m: params[m.group(1)], path)


async def test_card_r_the_assistant_never_receives_margin_on_any_route_with_real_money(
    app_pool: asyncpg.Pool, migrated_app_dsn: str
) -> None:
    s = await _seed(app_pool)
    pools = LocalPools()
    settings = pools.settings(database_url=migrated_app_dsn)
    app = create_app(
        settings,
        verifier=pools.verifier(settings),
        keys=s["keys"],
        booking_client=booking_client(s["fake"]),
    )
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        lead = str(await ctx.conn.fetchval("SELECT traveler_id FROM crm.trip_traveler WHERE trip_id = $1", s["trip"]))
    params = _params(s, lead)

    routes = _get_routes(app)
    unfilled = {p for p in routes if _fill(p, params) is None}
    assert unfilled == set(UNSEEDED), "classify the new GET route: seed its object here or add it to UNSEEDED"
    called = [p for p in routes if p not in unfilled]
    assert len(called) >= 40

    owner_text: dict[str, str] = {}
    failures: list[str] = []
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://crm.test") as client:
            for role in ("owner", "assistant"):
                token = pools.partner_web(sub=s["who"][role].sub)
                headers = {"Authorization": f"Bearer {token}", "X-AA-Tenant-Id": str(s["tenant"])}
                for path in called:
                    res = await client.get(_fill(path, params) or path, headers=headers)
                    if res.status_code >= 500:
                        failures.append(f"{role} GET {path}: {res.status_code} {res.text[:200]}")
                        continue
                    if role == "owner":
                        owner_text[path] = res.text
                        continue
                    body = res.json() if "json" in res.headers.get("content-type", "") else res.text
                    leaked_keys = sorted(k for k in _keys(body) if MARGIN_KEY.search(k.lower()))
                    leaked_values = [v for v in COST_SIDE if v in res.text]
                    if leaked_keys or leaked_values:
                        failures.append(
                            f"assistant GET {path}: {res.status_code} keys={leaked_keys} values={leaked_values}"
                        )
    finally:
        await app.state.db.close()
    assert failures == []

    # Positive control: the owner reads every planted value through these routes, so the check above is not empty.
    seen_by_owner = {v: sorted(p for p, text in owner_text.items() if v in text) for v in COST_SIDE}
    assert {v: bool(where) for v, where in seen_by_owner.items()} == {v: True for v in COST_SIDE}, json.dumps(
        seen_by_owner
    )
