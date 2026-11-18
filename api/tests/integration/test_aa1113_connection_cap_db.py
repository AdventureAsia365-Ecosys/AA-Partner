"""AA-1113 (CIS 452) on PostgreSQL with the RDS role shape: the CRM's connection cap read as ``crm_app`` in the call's
``tenant_tx()``.

CIS owns ``catalog.v_llm_connection_cap_v1`` on Dev; this database has no CIS schema, so the module stands in a table of
the contract's columns (created by the admin login, SELECT to ``crm_app`` like CIS 452 grants it) and drops it after.

- The CRM's own month (``crm.llm_usage_daily`` since the row's ``month_start``) is added to the CIS spend: over the
  budget the gateway writes one ``crm.llm_call`` row ``refused`` / ``connection_budget_exhausted`` / ``budget`` at
  0 USD and never calls the model; under it the call answers.
- Without the view (CIS 452 not applied) the call answers.
Invented connection amounts; the rollup rows use an invented stage and are removed after each test.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

from app.llm import connection_cap
from app.llm.gateway import LlmGateway, StubProvider, StubScrubber
from app.tenancy import tenant_tx
from tests.integration.test_aa964_gateway_profiles import _enable, _rows
from tests.integration.test_crm005_intake import _agency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ZZ_STAGE = "crm_zz_aa1113_cap"
VIEW = "catalog.v_llm_connection_cap_v1"


class _OnAcc2(StubProvider):
    """The stub model on the task role's own Bedrock connection (what the CRM's live route uses)."""

    connection_key = "bedrock"


def _month_start() -> dt.datetime:
    now = dt.datetime.now(dt.UTC)
    return dt.datetime(now.year, now.month, 1, tzinfo=dt.UTC)


@pytest_asyncio.fixture
async def cap_view(admin: asyncpg.Connection) -> AsyncIterator[None]:
    await admin.execute("CREATE SCHEMA IF NOT EXISTS catalog")
    await admin.execute(f"DROP TABLE IF EXISTS {VIEW}")
    await admin.execute(
        f"CREATE TABLE {VIEW} (connection_key text PRIMARY KEY, monthly_budget_usd numeric(10,2), "
        "month_start timestamptz, month_spent_usd numeric(14,6), month_calls integer, alert_pct integer, "
        "hard_stop boolean, over_cap boolean, stops boolean)"
    )
    await admin.execute("GRANT USAGE ON SCHEMA catalog TO crm_app")
    await admin.execute(f"GRANT SELECT ON {VIEW} TO crm_app")
    yield
    await admin.execute(f"DROP TABLE IF EXISTS {VIEW}")
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("DELETE FROM crm.llm_usage_daily WHERE stage = $1", ZZ_STAGE)


async def _cap(admin: asyncpg.Connection, budget: str, cis_spent: str, crm_spent: str) -> None:
    await admin.execute(
        f"INSERT INTO {VIEW} VALUES ('acc2', $1, $2, $3, 1, 80, true, false, false)",
        Decimal(budget),
        _month_start(),
        Decimal(cis_spent),
    )
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute(
            "INSERT INTO crm.llm_usage_daily (day, stage, model, calls, failed_calls, input_tokens, output_tokens, "
            "cost_usd, updated_at) VALUES ((now() AT TIME ZONE 'UTC')::date, $1, 'zz-model', 1, 0, 1, 1, $2, now())",
            ZZ_STAGE,
            Decimal(crm_spent),
        )


async def _call(app_pool: asyncpg.Pool, owner: object, tenant_id: uuid.UUID):
    connection_cap.invalidate()  # each test sets its own cap: no read kept from the one before
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        return await LlmGateway(StubScrubber(), _OnAcc2()).call(
            ctx, stage="crm_draft_reply", role="writer", prompt="hello"
        )


async def test_over_the_shared_cap_refuses_free(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, cap_view: None
) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await _enable(app_pool, owner, tenant_id)
    await _cap(admin, budget="5", cis_spent="4.5", crm_spent="0.75")  # 4.5 CIS + 0.75 CRM >= 5
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        state = await connection_cap.read_state(ctx.conn, "acc2")
    assert state is not None and state.crm_spent_usd >= Decimal("0.75") and state.stops
    result = await _call(app_pool, owner, tenant_id)
    assert (result.outcome, result.refusal_code, result.error_class) == (
        "refused",
        "connection_budget_exhausted",
        "budget",
    )
    [row] = await _rows(admin, tenant_id)
    assert (row["outcome"], row["refusal_code"], row["cost_usd_est"]) == (
        "refused",
        "connection_budget_exhausted",
        Decimal(0),
    )


async def test_under_the_cap_answers(app_pool: asyncpg.Pool, admin: asyncpg.Connection, cap_view: None) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await _enable(app_pool, owner, tenant_id)
    await _cap(admin, budget="50", cis_spent="1", crm_spent="0.25")
    assert (await _call(app_pool, owner, tenant_id)).outcome == "ok"


async def test_no_published_view_answers(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT to_regclass($1)", VIEW) is None
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await _enable(app_pool, owner, tenant_id)
    assert (await _call(app_pool, owner, tenant_id)).outcome == "ok"
