"""AA-1025 H1c on PostgreSQL with the RDS role shape, after crm-041: the ops writer (as ``crm_app``), the gateway's
read in the call's ``tenant_tx()`` and the two refusals, against the real CHECKs and RLS (the orchestrator's Dev check,
reproduced here).

- AI off (written as the CIS relay writes it): one ``crm.llm_call`` row ``refused`` / ``app_disabled`` / ``disabled`` at
  0 USD; switched back on, the next call answers.
- A daily budget of 0 USD: ``app_budget_exhausted`` / ``budget``.
- The job sampler's daily budget read (``read_day_spend``, the ``llm_app_spend_near`` / ``_cap`` ops checks) as
  ``crm_app``: today's rollup of every agency against the budget, None without a budget.
The row is removed as ``crm_owner`` after each test (crm_app may not delete it), so the other modules see no profile.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

from app.llm import app_profile
from app.tenancy import tenant_tx
from tests.integration.test_aa964_gateway_profiles import _enable, _gateway, _rows
from tests.integration.test_crm005_intake import _agency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

STAFF = "platform:00000000-0000-4000-8000-00000000aa01"


ZZ_STAGE = "crm_zz_h1c_spend_check"


@pytest_asyncio.fixture
async def no_profile_after(admin: asyncpg.Connection) -> AsyncIterator[None]:
    yield
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute("DELETE FROM crm.llm_app_profile")
        await admin.execute("DELETE FROM crm.llm_usage_daily WHERE stage = $1", ZZ_STAGE)


async def _call(app_pool: asyncpg.Pool, owner: object, tenant_id: uuid.UUID):
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        return await _gateway().call(ctx, stage="crm_draft_reply", role="writer", prompt="hello")


async def test_ai_off_refuses_then_on_answers(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, no_profile_after: None
) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await _enable(app_pool, owner, tenant_id)
    saved = await app_profile.write(app_pool, {"ai_enabled": False, "disabled_reason": "zz outage"}, actor=STAFF)
    assert saved.ai_enabled is False and (await app_profile.read(app_pool)).disabled_reason == "zz outage"
    result = await _call(app_pool, owner, tenant_id)
    assert (result.outcome, result.refusal_code, result.error_class) == ("refused", "app_disabled", "disabled")
    [row] = await _rows(admin, tenant_id)
    assert (row["outcome"], row["refusal_code"], row["error_class"], row["cost_usd_est"]) == (
        "refused",
        "app_disabled",
        "disabled",
        Decimal(0),
    )
    await app_profile.write(app_pool, {"ai_enabled": True}, actor=STAFF)
    assert (await _call(app_pool, owner, tenant_id)).outcome == "ok"
    stored = await admin.fetchrow("SELECT disabled_reason, updated_by FROM crm.llm_app_profile")
    assert stored["disabled_reason"] is None and stored["updated_by"] == STAFF


async def test_a_zero_daily_budget_refuses_with_class_budget(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, no_profile_after: None
) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await _enable(app_pool, owner, tenant_id)
    await app_profile.write(app_pool, {"ai_enabled": True, "default_daily_budget_usd": 0}, actor=STAFF)
    result = await _call(app_pool, owner, tenant_id)
    assert (result.refusal_code, result.error_class) == ("app_budget_exhausted", "budget")


async def test_a_db_without_a_row_is_ai_on(app_pool: asyncpg.Pool) -> None:
    async with app_pool.acquire() as conn:
        assert await app_profile.for_call(conn) == app_profile.DEFAULT
        assert isinstance(await app_profile.day_spend(conn), Decimal)


async def test_the_sampler_reads_the_daily_budget_and_raises_the_check(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, no_profile_after: None
) -> None:
    from app.jobs.metrics import JobMetricsSampler

    assert await app_profile.read_day_spend(app_pool) is None  # no profile row: no check
    await app_profile.write(app_pool, {"ai_enabled": True}, actor=STAFF)
    assert await app_profile.read_day_spend(app_pool) is None  # a profile without a daily budget: no check
    await app_profile.write(app_pool, {"ai_enabled": True, "default_daily_budget_usd": "1000"}, actor=STAFF)
    before = await app_profile.read_day_spend(app_pool)
    assert before is not None and before.cap_usd == Decimal(1000)
    # One rollup row today, on top of whatever other modules left today: 80 % of the budget in all.
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute(
            "INSERT INTO crm.llm_usage_daily (day, stage, model, calls, failed_calls, input_tokens, output_tokens,"
            " cost_usd) VALUES ((now() AT TIME ZONE 'UTC')::date, $1, 'zz-model', 1, 0, 10, 10, $2)",
            ZZ_STAGE,
            Decimal(800) - before.spent_usd,
        )
    spend = await app_profile.read_day_spend(app_pool)
    assert spend is not None and spend.spent_usd == Decimal(800)

    class Collect:
        def __init__(self) -> None:
            self.found: list[str] = []

        async def alert(self, pool: object, found: list, now: object = None) -> tuple[dict[str, str], list]:
            self.found += [f.check for f in found]
            return {f.check: "sent" for f in found}, []

    async def provide() -> asyncpg.Pool:
        return app_pool

    pub = Collect()
    await JobMetricsSampler(provide, publisher=pub).sample_once()  # type: ignore[arg-type]
    assert "llm_app_spend_near" in pub.found and "llm_app_spend_cap" not in pub.found
