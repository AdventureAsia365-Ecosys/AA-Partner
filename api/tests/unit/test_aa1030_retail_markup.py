"""AA-1030 (crm-046): the agency default retail markup and the suggested sell of an AA option — the suggestion rule
(the agency markup on the B2B price, else AA's RRP for a tour), who sees a markup-based suggestion, the settings read
/ merge, and the owner-only routes. Real SQL (the CHECK, the column) is in tests/integration/test_crm046_schema.py.
"""

from __future__ import annotations

import json
from decimal import Decimal as D
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import margin
from app.errors import Invalid
from app.main import create_app
from app.proposals import retail
from app.routers import ledger as router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, fake_tx, tenant_ctx

# ── The suggestion rule ──────────────────────────────────────────────────────────────────────────────────────────


def test_agency_markup_beats_the_rrp_and_rounds_up() -> None:
    marks = {"trip_plan": D("20"), "hotel_room": D("12.5")}
    # 1,234.56 x 1.20 = 1,481.472 → 1,481.48 (up to the cent).
    assert retail.suggest("trip_plan", b2b_price=D("1234.56"), from_price=D("900"), party=2, agency_markups=marks) == (
        D("1481.48"),
        retail.BASIS_MARKUP,
    )
    assert retail.suggest("hotel_room", b2b_price=D("80"), from_price=None, party=2, agency_markups=marks) == (
        D("90.00"),
        retail.BASIS_MARKUP,
    )


def test_rrp_for_a_tour_without_a_markup() -> None:
    assert retail.suggest("trip_plan", b2b_price=D("1000"), from_price=D("625.50"), party=3, agency_markups={}) == (
        D("1876.50"),
        retail.BASIS_RRP,
    )
    # No RRP for a stand-alone hotel night or transfer, no party, no price: no suggestion.
    assert retail.suggest("transfer", b2b_price=D("40"), from_price=None, party=2, agency_markups={}) is None
    assert retail.suggest("trip_plan", b2b_price=D("1000"), from_price=D("625"), party=0, agency_markups={}) is None
    assert (
        retail.suggest("hotel_room", b2b_price=None, from_price=None, party=2, agency_markups={"hotel_room": D("5")})
        is None
    )
    assert retail.suggest(None, b2b_price=D("10"), from_price=None, party=2, agency_markups={}) is None


def test_a_markup_suggestion_is_hidden_from_roles_without_margin() -> None:
    markup = (D("120.00"), retail.BASIS_MARKUP)
    rrp = (D("150.00"), retail.BASIS_RRP)
    assert retail.suggestion_out(markup, sees_margin=True) == {
        "suggested_sell": "120.00",
        "suggested_basis": "agency_markup",
    }
    assert retail.suggestion_out(markup, sees_margin=False) == {"suggested_sell": None, "suggested_basis": None}
    assert retail.suggestion_out(rrp, sees_margin=False) == {"suggested_sell": "150.00", "suggested_basis": "aa_rrp"}
    assert retail.suggestion_out(None, sees_margin=True) == {"suggested_sell": None, "suggested_basis": None}


def test_parse_keeps_known_kinds_only() -> None:
    assert retail.parse('{"trip_plan": 20, "meal": 7.5, "spa": 3}') == {"trip_plan": D("20"), "meal": D("7.5")}
    assert retail.parse(None) == {}
    assert retail.parse({"guide": "x"}) == {}


# ── Settings read / merge ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_and_merge() -> None:
    conn = ScriptedConn().on("SELECT retail_markup_pct FROM crm.tenant_settings", '{"trip_plan": 20, "meal": 5}')
    ctx = tenant_ctx(conn)
    assert (await retail.read(ctx))["retail_markup_pct"] == {"trip_plan": "20", "meal": "5"}
    await retail.update(ctx, {"hotel_room": D("12.50"), "meal": None})
    [upsert] = conn.sql("INSERT INTO crm.tenant_settings (retail_markup_pct)")
    assert json.loads(upsert[0]) == {"trip_plan": 20, "hotel_room": 12.5}
    assert [e.action for e in ctx.audit_events] == ["update"]


@pytest.mark.asyncio
async def test_update_refuses_bad_values() -> None:
    ctx = tenant_ctx(ScriptedConn())
    for bad in ({"spa": D("5")}, {"trip_plan": D("301")}, {"trip_plan": D("-1")}, {"trip_plan": D("1.234")}):
        with pytest.raises(Invalid):
            await retail.update(ctx, bad)
    refused = ScriptedConn().on("INSERT INTO crm.tenant_settings", asyncpg.CheckViolationError("check"))
    with pytest.raises(Invalid):
        await retail.update(tenant_ctx(refused), {"trip_plan": D("10")})


@pytest.mark.asyncio
async def test_markups_before_the_migration_is_applied() -> None:
    conn = ScriptedConn().on("SELECT retail_markup_pct", asyncpg.UndefinedColumnError("no column"))
    assert await retail.markups(tenant_ctx(conn)) == {}


# ── Routes ───────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str, conn: ScriptedConn
) -> tuple[TestClient, dict]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    ctx = tenant_ctx(conn, role=role)

    def tx(*a: Any, **kw: Any) -> Any:
        margin.current_member_role.set(role)
        return fake_tx(ctx)(*a, **kw)

    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", tx)
    monkeypatch.setattr(router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    return TestClient(app), {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}


def test_owner_reads_and_sets_the_markup(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn().on("SELECT retail_markup_pct FROM crm.tenant_settings", "{}")
    client, h = _client(pools, monkeypatch, "owner", conn)
    with client:
        assert client.get("/crm/v1/settings/retail-markup", headers=h).json() == {
            "markup_max_pct": "300",  # AA-1096 L4: the platform ceiling the owner may set (a setting)
            "retail_markup_pct": {},
            "kinds": list(retail.KINDS),
        }
        res = client.put("/crm/v1/settings/retail-markup", headers=h, json={"retail_markup_pct": {"trip_plan": 18}})
        assert res.status_code == 200, res.text
        bad = client.put("/crm/v1/settings/retail-markup", headers=h, json={"retail_markup_pct": {"spa": 5}})
        assert bad.status_code == 422
        too_much = client.put("/crm/v1/settings/retail-markup", headers=h, json={"retail_markup_pct": {"meal": 301}})
        assert too_much.status_code == 422
    [upsert] = conn.sql("INSERT INTO crm.tenant_settings (retail_markup_pct)")
    assert json.loads(upsert[0]) == {"trip_plan": 18}


@pytest.mark.parametrize("role", ["agent", "assistant"])
def test_only_the_owner_sets_the_markup(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    conn = ScriptedConn()
    client, h = _client(pools, monkeypatch, role, conn)
    with client:
        assert client.get("/crm/v1/settings/retail-markup", headers=h).status_code == 403
        put = client.put("/crm/v1/settings/retail-markup", headers=h, json={"retail_markup_pct": {"trip_plan": 1}})
        assert put.status_code == 403
    assert not conn.sql("INSERT INTO crm.tenant_settings")
