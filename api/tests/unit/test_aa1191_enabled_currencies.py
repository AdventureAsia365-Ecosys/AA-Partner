"""AA-1191 — USD only in the CRM: the enabled currencies read from CIS (``GET /catalog/v1/currencies``, catalog.http.v1
1.5), their process cache, and every write the CRM refuses in a currency that is not enabled (422
``currency_not_enabled``): a new working copy, a text proposal, the agency's base currency, a planner price step, an AA
prices read. The lists the web shows (``GET /currencies``, the AA prices currencies, the planner price steps) leave the
other currencies out. No DB, no network; invented names only."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import margin
from app.catalog import currencies
from app.catalog.client import CatalogClient, CatalogUnavailable
from app.errors import Invalid
from app.ledger import fx
from app.main import create_app
from app.proposals import service
from app.routers import aa as aa_router
from app.routers import ledger as ledger_router
from tests.aa_fakes import FakeCis, catalog_config
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, fake_tx, tenant_ctx

TRIP = uuid.UUID("a0000000-0000-4000-8000-000000001191")


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


# ── pure helpers ─────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (None, ("USD",)),
        ([], ("USD",)),
        (["VND"], ("USD", "VND")),
        (["vnd", "USD", "VND"], ("USD", "VND")),
        (["USD", 7, ""], ("USD",)),
    ],  # fmt: skip
)
def test_normalise(raw: Any, want: tuple[str, ...]) -> None:
    assert currencies.normalise(raw) == want


def test_problem_and_refusal() -> None:
    assert currencies.problem("USD", ("USD",)) is None
    assert currencies.problem(None, ("USD",)) is None
    why = currencies.problem("VND", ("USD",), "base_currency")
    assert why is not None and why.startswith("base_currency: VND is not an enabled currency")
    with pytest.raises(Invalid) as e:
        currencies.refuse_unless_enabled("EUR", ("USD", "VND"))
    assert e.value.code == "currency_not_enabled"


# ── the CIS read and its cache ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_client_reads_the_contract_answer() -> None:
    cis = FakeCis(currencies={"enabled": ["USD", "VND"], "base": "USD", "cache_s": 60})
    assert await CatalogClient(catalog_config(cis)).get_currencies() == (("USD", "VND"), 60)


@pytest.mark.parametrize(
    "body",
    [
        {"enabled": ["VND"], "base": "USD", "cache_s": 60},
        {"enabled": [], "base": "USD", "cache_s": 60},
        {"enabled": ["USD", "vnd"], "base": "USD", "cache_s": 60},
        {"enabled": ["USD"], "base": "USD", "cache_s": 0},
        {"enabled": ["USD"], "base": "USD", "cache_s": True},
    ],  # fmt: skip
)
@pytest.mark.asyncio
async def test_the_client_refuses_a_broken_answer(body: dict[str, Any]) -> None:
    with pytest.raises(CatalogUnavailable):
        await CatalogClient(catalog_config(FakeCis(currencies=body))).get_currencies()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_the_cache_keeps_the_answer_for_cache_s_and_the_last_answer_when_cis_is_down() -> None:
    cis = FakeCis(currencies={"enabled": ["USD", "VND"], "base": "USD", "cache_s": 120})
    clock = _Clock()
    reader = currencies.EnabledCurrencies(catalog_config(cis), clock=clock)
    assert await reader.get() == ("USD", "VND")
    assert await reader.get() == ("USD", "VND") and cis.calls["currencies"] == 1
    clock.now += 121
    cis.down = True
    assert await reader.get() == ("USD", "VND")  # CIS down: the last answer stays
    cis.down = False
    cis.currencies = {"enabled": ["USD"], "base": "USD", "cache_s": 120}
    assert await reader.get() == ("USD", "VND")  # not retried inside the retry window
    clock.now += currencies.RETRY_S + 1
    assert await reader.get() == ("USD",)


@pytest.mark.asyncio
async def test_usd_only_before_any_answer_and_without_cis() -> None:
    assert await currencies.EnabledCurrencies(None).get() == ("USD",)
    down = currencies.EnabledCurrencies(catalog_config(FakeCis(down=True)), clock=_Clock())
    assert await down.get() == ("USD",)

    class _State:
        pass

    assert await currencies.enabled(_State()) == ("USD",)


# ── services ─────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_base_currency_is_refused_while_not_enabled_and_kept_when_sent_back() -> None:
    conn = ScriptedConn()
    ctx = tenant_ctx(conn)
    with pytest.raises(Invalid) as e:
        await fx.update_payment_settings(ctx, {"base_currency": "VND"}, ("USD",))
    assert e.value.code == "currency_not_enabled" and not conn.sql("INSERT INTO crm.tenant_settings")
    out = await fx.update_payment_settings(ctx, {"base_currency": "USD", "fx_mode": "manual"}, ("USD",))
    assert out["enabled_currencies"] == ["USD"]
    stored = ScriptedConn().on(
        "SELECT base_currency, booking_validation_level",
        {"base_currency": "VND", "booking_validation_level": "basic", "fx_mode": "automatic"},
    )
    await fx.update_payment_settings(tenant_ctx(stored), {"base_currency": "VND", "fx_mode": "manual"}, ("USD",))
    assert stored.sql("INSERT INTO crm.tenant_settings")[0][0] == "VND"


@pytest.mark.asyncio
async def test_a_text_proposal_in_a_disabled_currency_is_refused() -> None:
    with pytest.raises(Invalid) as e:
        await service.create_draft(
            tenant_ctx(ScriptedConn()), object(), TRIP, body="Zz invented trip", currency="VND",
            total=Decimal("10"), deposit=Decimal("1"), balance_due_on=None, terms_doc_id=None, allowed=("USD",),
        )  # fmt: skip
    assert e.value.code == "currency_not_enabled"


# ── routes ───────────────────────────────────────────────────────────────────────────────────────────────────────


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, *modules: Any) -> tuple[TestClient, dict[str, str]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    ctx = tenant_ctx(ScriptedConn(), role="owner")

    def tx(*a: Any, **kw: Any) -> Any:
        margin.current_member_role.set("owner")
        return fake_tx(ctx)(*a, **kw)

    for module in modules:
        monkeypatch.setattr(module, "request_pool", _pool)
        monkeypatch.setattr(module, "tenant_tx", tx)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    return TestClient(app), headers


def test_the_currencies_route_answers_usd_only_by_default(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(pools, monkeypatch, aa_router)
    r = client.get("/crm/v1/currencies", headers=headers)
    assert r.status_code == 200 and r.json() == {"enabled": ["USD"], "base": "USD"}


def test_a_new_working_copy_in_vnd_is_refused(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(pools, monkeypatch)
    r = client.post(f"/crm/v1/trips/{TRIP}/builder", json={"currency": "VND"}, headers=headers)
    assert r.status_code == 422 and r.json()["detail"] == "currency_not_enabled"


def test_aa_prices_list_and_read_only_enabled_currencies(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(pools, monkeypatch, aa_router)
    r = client.get("/crm/v1/aa/tariff/currencies", headers=headers)
    assert r.status_code == 200 and r.json()["currencies"] == ["USD"]
    r = client.get("/crm/v1/aa/tariff", params={"currency": "VND"}, headers=headers)
    assert r.status_code == 422 and r.json()["detail"] == "currency_not_enabled"


def test_planner_price_steps_list_and_accept_enabled_currencies_only(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _read(ctx: Any) -> dict[str, Any]:
        return {"show_planner_prices": True, "base_currency": "USD",
                "currencies": [{"currency": "USD", "step": "1"}, {"currency": "VND", "step": "10000"}]}  # fmt: skip

    monkeypatch.setattr(ledger_router.rounding, "read", _read)
    client, headers = _client(pools, monkeypatch, ledger_router)
    r = client.get("/crm/v1/settings/planner-prices", headers=headers)
    assert r.status_code == 200 and [c["currency"] for c in r.json()["currencies"]] == ["USD"]
    r = client.put("/crm/v1/settings/planner-prices", json={"steps": {"VND": "50000"}}, headers=headers)
    assert r.status_code == 422 and r.json()["detail"] == "currency_not_enabled"
