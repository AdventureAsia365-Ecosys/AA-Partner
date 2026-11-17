"""AA-1116: the remaining CRM feature gates (S282 add-on audit G10), without a database.

- ``insights_export``: the Reports CSV export (every section, the profit cube too) is 403 ``feature_not_allowed`` with
  a ``denied`` audit event before anything is read; the summary says whether the plan has it (``export_allowed``).
- ``stripe_connect``: connecting Stripe is 403 ``feature_not_allowed`` before the provider check; the payment methods
  list marks a provider whose feature is not on the plan (``on_plan``). The VN gateway and the manual method need none.
- Every CRM feature code is either checked somewhere in ``app/`` or listed in ``entitlements.NOT_BUILT``.

The SQL runs on PostgreSQL in tests/integration/test_reports_api.py.
"""

from __future__ import annotations

import ast
import contextlib
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import entitlements, margin
from app.errors import Conflict, Forbidden
from app.main import create_app
from app.payments import providers
from app.payments import service as pay
from app.reports import export, service
from app.routers import reports as reports_router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, fake_tx, tenant_ctx
from tests.unit.test_reports import report_conn

ALLOWED = "ANY (allowed_features)"
APP = pathlib.Path(__file__).resolve().parents[2] / "app"


@contextlib.contextmanager
def _refused(code: str) -> Iterator[None]:
    with pytest.raises(Forbidden) as caught:
        yield
    assert caught.value.code == code


def _without_feature() -> ScriptedConn:
    return report_conn().on(ALLOWED, False)


# ── insights_export: the CSV export ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("section", ["funnel", "money_by_month", "profit"])
@pytest.mark.asyncio
async def test_export_without_insights_export_is_refused_before_any_read(section: str) -> None:
    conn = _without_feature()
    ctx = tenant_ctx(conn, role="owner")
    with _refused("feature_not_allowed"):
        await export.export(ctx, "90d", None, section)
    assert conn.sql(ALLOWED) == [("insights_export",)]
    assert not conn.sql("WITH cohort AS") and not conn.sql("max(m.amount_base) FILTER")
    assert [(e.action, e.outcome, e.object_kind, e.object_id, e.reason) for e in ctx.audit_events] == [
        ("report_export", "denied", "report", section, "feature_not_allowed")
    ]


@pytest.mark.asyncio
async def test_the_plan_check_comes_before_the_money_check() -> None:
    """An assistant on a plan without the export reads the plan refusal (what the page explains), not the role one."""
    ctx = tenant_ctx(_without_feature(), role="assistant")
    with _refused("feature_not_allowed"):
        await export.export(ctx, "90d", None, "money_by_month")


@pytest.mark.asyncio
async def test_export_with_insights_export_downloads() -> None:
    ctx = tenant_ctx(report_conn(), role="owner")
    _, count = await export.export(ctx, "90d", None, "funnel")
    assert count > 0 and [e.outcome for e in ctx.audit_events] == ["allowed"]


@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.asyncio
async def test_summary_says_whether_the_plan_has_the_export(allowed: bool) -> None:
    conn = report_conn().on(ALLOWED, allowed)
    out = await service.summary(tenant_ctx(conn, role="owner"), "90d", None)
    assert out["export_allowed"] is allowed and out["funnel"]["created"] > 0  # the page itself stays open


@pytest.fixture
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def test_the_export_route_answers_403_with_the_shared_error_shape(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    ctx = tenant_ctx(_without_feature(), role="owner")

    def tx(*a: Any, **kw: Any) -> Any:
        margin.current_member_role.set("owner")
        return fake_tx(ctx)(*a, **kw)

    monkeypatch.setattr(reports_router, "request_pool", _pool)
    monkeypatch.setattr(reports_router, "tenant_tx", tx)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    with TestClient(app) as client:
        res = client.get("/crm/v1/reports/export?section=funnel", headers=headers)
        summary = client.get("/crm/v1/reports/summary", headers=headers)
    assert res.status_code == 403
    assert res.json()["detail"] == "feature_not_allowed" and "insights export" in res.json()["message"]
    assert summary.status_code == 200 and summary.json()["export_allowed"] is False


# ── stripe_connect: card payments on the agency's own Stripe ───────────────────────────────────
@pytest.mark.asyncio
async def test_connecting_stripe_needs_stripe_connect() -> None:
    conn = ScriptedConn().on(ALLOWED, False)
    ctx = tenant_ctx(conn)
    with _refused("feature_not_allowed"):
        await pay.connect(ctx, "stripe")
    assert conn.sql(ALLOWED) == [("stripe_connect",)]
    assert [(e.outcome, e.object_kind, e.reason) for e in ctx.audit_events] == [
        ("denied", "payment_provider_account", "feature_not_allowed")
    ]
    # On the plan, Stripe is still paused (root ADR 0019 b14): the provider answer, as before.
    with pytest.raises(Conflict) as paused:
        await pay.connect(tenant_ctx(ScriptedConn().on(ALLOWED, True)), "stripe")
    assert paused.value.code == "provider_not_available"


@pytest.mark.parametrize(
    ("provider", "code"), [("manual", "use_manual_settings"), ("vn_gateway", "use_gateway_settings")]
)
@pytest.mark.asyncio
async def test_the_other_providers_need_no_feature(provider: str, code: str) -> None:
    conn = ScriptedConn().on(ALLOWED, False)
    with pytest.raises(Conflict) as caught:
        await pay.connect(tenant_ctx(conn), provider)
    assert caught.value.code == code and not conn.sql(ALLOWED)


@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.asyncio
async def test_payment_methods_mark_a_provider_off_the_plan(allowed: bool) -> None:
    out = await pay.payment_methods(tenant_ctx(ScriptedConn().on(ALLOWED, allowed)))
    on_plan = {p["provider"]: p["on_plan"] for p in out["providers"]}
    assert on_plan == {"manual": True, "stripe": allowed, "vn_gateway": True}


def test_every_plan_feature_of_a_provider_is_a_crm_feature() -> None:
    assert set(providers.PLAN_FEATURES) <= set(providers.KNOWN)
    assert set(providers.PLAN_FEATURES.values()) <= set(entitlements.FEATURES)


# ── every feature code is gated somewhere, or declared not built ─────────────────────────────────
# Where a code is only named, not checked: the feature list itself, the L1 console's request body, the CIS feed.
NAMING_ONLY = {"entitlements.py", "routers/ops.py", "cis/entitlement_feed.py"}


def _literal_users(code: str) -> set[str]:
    out: set[str] = set()
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel in NAMING_ONLY or rel.startswith("aa_llm/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(n, ast.Constant) and n.value == code for n in ast.walk(tree)):
            out.add(rel)
    return out


@pytest.mark.parametrize("code", entitlements.FEATURES)
def test_every_crm_feature_is_checked_or_declared_not_built(code: str) -> None:
    users = _literal_users(code)
    if code in entitlements.NOT_BUILT:
        assert not users, f"{code} is used in {sorted(users)}: gate it and remove it from entitlements.NOT_BUILT"
    else:
        assert users, f"{code} is on a CRM plan but nothing checks it: gate its entry or list it in NOT_BUILT"


def test_not_built_names_only_known_features() -> None:
    assert set(entitlements.NOT_BUILT) <= set(entitlements.FEATURES)
