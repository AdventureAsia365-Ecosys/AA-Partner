"""AA-1138 (crm-078) on PostgreSQL with the RDS role shape: the CRM's month spend per connection, read as ``crm_app``
in the call's ``tenant_tx()``, and a route entry on a capped connection left out of the route.

- ``crm.llm_usage_daily`` rows of a (stage, model) pair that a route entry runs on a named connection
  (``crm.llm_stage_route.connection_key``) count for that connection only; every other row (another model, the
  guardrail rows) counts for the process's own connection.
- Through the gateway with the real profile source: the named connection at its cap -> the draft is answered by the
  entry on the process's own connection, one row, attempt 1; under the cap the named entry answers first.
Invented stage, models and amounts; the rows are removed after each test.
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
from app.llm.profiles import DbProfileSource, RouteEntry
from app.tenancy import tenant_tx
from tests.integration.test_aa964_gateway_profiles import _enable, _rows
from tests.integration.test_crm005_intake import _agency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CAP_KEY = "crm.ai.connection_monthly_cap_usd"
ZZ_STAGE = "crm_zz_aa1138_route"
FAST = "global.maker.zz-fast-v1"
LITE = "us.maker.zz-lite-v1:0"
OTHER = "zz-guard-op"


class _Router(StubProvider):
    """The stub on the task role's own connection; a route entry with a connection key plays on that connection."""

    connection_key = "bedrock"

    def for_route(self, entry: RouteEntry) -> StubProvider:
        played = StubProvider(entry.model, entry.family, entry.position)
        played.connection_key = entry.connection_key or "bedrock"  # type: ignore[misc]
        played.route_connection = entry.connection_key  # type: ignore[attr-defined]
        return played


@pytest.fixture
def caps(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    value: dict[str, Any] = {}
    real = ps.get
    monkeypatch.setattr(ps, "get", lambda key, tenant_id=None: value if key == CAP_KEY else real(key, tenant_id))
    return value


@pytest_asyncio.fixture
async def zz_route(admin: asyncpg.Connection) -> AsyncIterator[None]:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute(
            "INSERT INTO crm.llm_stage_profile (stage, role, is_active, max_attempts) VALUES ($1, 'writer', true, 2)",
            ZZ_STAGE,
        )
        await admin.execute(
            "INSERT INTO crm.llm_stage_route (stage, position, model, family, connection_key, sends_temperature) "
            "VALUES ($1, 0, $2, 'maker', 'bedrock_acc1', false), ($1, 1, $3, 'maker', NULL, true)",
            ZZ_STAGE,
            FAST,
            LITE,
        )
    yield
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("DELETE FROM crm.llm_usage_daily WHERE stage = $1 OR model = $2", ZZ_STAGE, OTHER)
        await admin.execute("DELETE FROM crm.llm_stage_profile WHERE stage = $1", ZZ_STAGE)


async def _spent(admin: asyncpg.Connection, stage: str, model: str, usd: str) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute(
            "INSERT INTO crm.llm_usage_daily (day, stage, model, calls, failed_calls, input_tokens, output_tokens, "
            "cost_usd, updated_at) VALUES ((now() AT TIME ZONE 'UTC')::date, $1, $2, 1, 0, 1, 1, $3, now())",
            stage,
            model,
            Decimal(usd),
        )


async def _spend_by_connection(app_pool: asyncpg.Pool) -> tuple[Decimal, Decimal]:
    async with app_pool.acquire() as c:
        own = await connection_cap.month_spend(c)
        named = await connection_cap.month_spend(c, route_key="bedrock_acc1")
    return own, named


async def test_each_connection_counts_its_own_rows(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, zz_route: None
) -> None:
    own_before, named_before = await _spend_by_connection(app_pool)
    await _spent(admin, ZZ_STAGE, FAST, "0.40")  # the entry on bedrock_acc1
    await _spent(admin, ZZ_STAGE, LITE, "0.07")  # the entry on the process's own connection
    await _spent(admin, "crm_scrub", OTHER, "0.02")  # a guardrail row: on no route
    own, named = await _spend_by_connection(app_pool)
    assert named - named_before == Decimal("0.40")
    assert own - own_before == Decimal("0.09")


async def _draft(app_pool: asyncpg.Pool, owner: object, tenant_id: uuid.UUID):
    gateway = LlmGateway(StubScrubber(), _Router(), profiles=DbProfileSource())
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        return await gateway.call(ctx, stage=ZZ_STAGE, role="writer", prompt="hello")


async def test_the_named_connection_at_its_cap_sends_the_call_to_the_next_entry(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, caps: dict[str, Any], zz_route: None
) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await _enable(app_pool, owner, tenant_id)
    _own, named = await _spend_by_connection(app_pool)
    caps.update({"acc1": float(named) + 0.30, "acc2": 1000})
    await _spent(admin, ZZ_STAGE, FAST, "0.30")
    assert (await _draft(app_pool, owner, tenant_id)).outcome == "ok"
    [row] = await _rows(admin, tenant_id)
    assert (row["model"], row["attempt_no"], row["outcome"]) == (LITE, 1, "ok")


async def test_under_the_cap_the_named_entry_answers_first(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, caps: dict[str, Any], zz_route: None
) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await _enable(app_pool, owner, tenant_id)
    caps.update({"acc1": 1000, "acc2": 1000})
    assert (await _draft(app_pool, owner, tenant_id)).outcome == "ok"
    [row] = await _rows(admin, tenant_id)
    assert (row["model"], row["attempt_no"]) == (FAST, 1)
