"""AA-1113 (S284 redesign, crm-077) on PostgreSQL with the RDS role shape: the CRM's own connection cap read as
``crm_app`` in the call's ``tenant_tx()``.

- The CRM's month spend (``crm.llm_usage_daily`` since the first day of the UTC month) against the CRM's cap on the
  connection (``crm.ai.connection_monthly_cap_usd``, its value in force set here): at or over the cap the gateway
  writes one ``crm.llm_call`` row ``refused`` / ``connection_budget_exhausted`` / ``budget`` at 0 USD and never calls
  the model; under it the call answers; with no entry for the connection the call answers.
- ``crm_app`` reads no CIS object: the database has no ``catalog`` schema and the call still works.
Invented amounts; the rollup rows use an invented stage and are removed after each test.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app import platform_settings as ps
from app.llm import connection_cap
from app.llm.gateway import LlmGateway, StubProvider, StubScrubber
from app.tenancy import tenant_tx
from tests.integration.test_aa964_gateway_profiles import _enable, _rows
from tests.integration.test_crm005_intake import _agency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ZZ_STAGE = "crm_zz_aa1113_cap"


class _OnAcc2(StubProvider):
    """The stub model on the task role's own Bedrock connection (what the CRM's live route uses)."""

    connection_key = "bedrock"


@pytest.fixture
def caps(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    value: dict[str, Any] = {}
    real = ps.get
    monkeypatch.setattr(
        ps, "get", lambda key, tenant_id=None: value if key == ps.AI_CONNECTION_MONTHLY_CAP else real(key, tenant_id)
    )
    return value


@pytest_asyncio.fixture
async def rollup(admin: asyncpg.Connection) -> AsyncIterator[None]:
    yield
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("DELETE FROM crm.llm_usage_daily WHERE stage = $1", ZZ_STAGE)


async def _spent(admin: asyncpg.Connection, crm_spent: str) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute(
            "INSERT INTO crm.llm_usage_daily (day, stage, model, calls, failed_calls, input_tokens, output_tokens, "
            "cost_usd, updated_at) VALUES ((now() AT TIME ZONE 'UTC')::date, $1, 'zz-model', 1, 0, 1, 1, $2, now())",
            ZZ_STAGE,
            Decimal(crm_spent),
        )


async def _call(app_pool: asyncpg.Pool, owner: object, tenant_id: uuid.UUID):
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        return await LlmGateway(StubScrubber(), _OnAcc2()).call(
            ctx, stage="crm_draft_reply", role="writer", prompt="hello"
        )


async def test_at_the_crm_cap_refuses_free(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, caps: dict[str, Any], rollup: None
) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await _enable(app_pool, owner, tenant_id)
    await _spent(admin, "0.75")
    caps["acc2"] = 0.5
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        state = await connection_cap.read_state(ctx.conn, "acc2")
    assert state is not None and state.month_spent_usd >= Decimal("0.75") and state.stops
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


async def test_under_the_cap_answers(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, caps: dict[str, Any], rollup: None
) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await _enable(app_pool, owner, tenant_id)
    await _spent(admin, "0.25")
    caps["acc2"] = 50
    assert (await _call(app_pool, owner, tenant_id)).outcome == "ok"


async def test_no_cap_on_the_connection_answers_and_no_cis_object_is_needed(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, caps: dict[str, Any]
) -> None:
    assert await admin.fetchval("SELECT to_regnamespace('catalog') AS ns") is None
    caps["zz-other"] = 0
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await _enable(app_pool, owner, tenant_id)
    assert (await _call(app_pool, owner, tenant_id)).outcome == "ok"
