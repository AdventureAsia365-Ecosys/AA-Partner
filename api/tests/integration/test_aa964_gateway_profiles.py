"""AA-964, S263 gateway plan F1 + F2 on PostgreSQL with the RDS role shape, after crm-040: the real ``tenant_tx()``,
gateway, profile source and ledger against the real CHECKs and RLS (the orchestrator's Dev checks, reproduced here).

- The seeded draft stage routes the stub as its first entry: one ``crm.llm_call`` row with the entry's model and
  ``family = 'anthropic'``, ``attempt_no = 1``.
- A stage switched off (kill switch) refuses with one row at 0 USD, nothing scrubbed or sent.
- A throttled first entry is a refused row, then the second entry answers (``attempt_no = 2``).
- A reply cut at its token limit is a ``truncated`` row on every entry, never ``ok``.
Test stages are written as ``crm_owner`` (the role DB Migrate runs as) and named ``crm_it_*``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import asyncpg
import pytest

from app.llm import agency_ai
from app.llm.gateway import STUB_FAIL_FIRST, STUB_TRUNCATE, LlmGateway, StubProvider, StubScrubber
from app.llm.profiles import DbProfileSource
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_aa964_ai_gateway import LIMITS, _trip
from tests.integration.test_crm005_intake import _agency, _keys, _owner_fetch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

UPSERT_PROFILE = (
    "INSERT INTO crm.llm_stage_profile (stage, role, is_active, max_attempts) VALUES ($1, 'writer', $2, 2) "
    "ON CONFLICT (stage) DO UPDATE SET is_active = EXCLUDED.is_active"
)
UPSERT_ROUTE = (
    "INSERT INTO crm.llm_stage_route (stage, position, model) VALUES ($1, $2, $3) "
    "ON CONFLICT (stage, position) DO UPDATE SET model = EXCLUDED.model"
)


async def _stage(admin: asyncpg.Connection, stage: str, *, active: bool = True) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE crm_owner")
        await admin.execute(UPSERT_PROFILE, stage, active)
        await admin.execute(UPSERT_ROUTE, stage, 0, "us.maker.small-v1:0")
        await admin.execute(UPSERT_ROUTE, stage, 1, "us.maker.large-v1:0")


async def _rows(admin: asyncpg.Connection, tenant_id: uuid.UUID) -> list[asyncpg.Record]:
    return await _owner_fetch(
        admin,
        tenant_id,
        "SELECT stage, provider, model, outcome, refusal_code, error_class, family, attempt_no, cost_usd_est "
        "FROM crm.llm_call ORDER BY at, attempt_no, llm_call_id",
    )


def _gateway() -> LlmGateway:
    return LlmGateway(StubScrubber(), StubProvider(), profiles=DbProfileSource())


async def _enable(app_pool: asyncpg.Pool, owner: object, tenant_id: uuid.UUID) -> None:
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await agency_ai.update_ai_settings(ctx, {"ai_enabled": True}, **LIMITS)


async def test_the_seeded_draft_stage_routes_the_stub_as_anthropic(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    trip_id = await _trip(app_pool, tenant_id)
    await _enable(app_pool, owner, tenant_id)
    keys = _keys()
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        out = await agency_ai.draft(ctx, _gateway(), Vault(keys, ctx), trip_id, kind="reply", text="Any vegan menus?")
    assert out["outcome"] == "ok"
    [row] = await _rows(admin, tenant_id)
    assert (row["stage"], row["provider"], row["outcome"], row["family"], row["attempt_no"]) == (
        "crm_draft_reply",
        "stub",
        "ok",
        "anthropic",
        1,
    )
    assert row["model"].startswith("us.anthropic.") and row["cost_usd_est"] == Decimal(0)


async def test_the_kill_switch_refuses_with_one_zero_cost_row(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    await _stage(admin, "crm_it_off", active=False)
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await _enable(app_pool, owner, tenant_id)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        result = await _gateway().call(ctx, stage="crm_it_off", role="writer", prompt="hello")
    assert (result.outcome, result.refusal_code) == ("refused", "stage_disabled")
    [row] = await _rows(admin, tenant_id)
    assert (row["outcome"], row["refusal_code"], row["cost_usd_est"]) == ("refused", "stage_disabled", Decimal(0))
    await _stage(admin, "crm_it_off", active=True)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        again = await _gateway().call(ctx, stage="crm_it_off", role="writer", prompt="hello")
    assert again.outcome == "ok"


async def test_a_throttled_first_entry_then_the_second_answers(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    await _stage(admin, "crm_it_route")
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await _enable(app_pool, owner, tenant_id)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        result = await _gateway().call(ctx, stage="crm_it_route", role="writer", prompt=f"hi {STUB_FAIL_FIRST}")
    assert result.outcome == "ok"
    first, second = await _rows(admin, tenant_id)
    assert (first["outcome"], first["refusal_code"], first["attempt_no"], first["family"]) == (
        "refused",
        "throttled",
        1,
        "maker",
    )
    assert (second["outcome"], second["model"], second["attempt_no"]) == ("ok", "us.maker.large-v1:0", 2)


async def test_a_truncated_reply_is_a_truncated_row_never_ok(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    await _stage(admin, "crm_it_cut")
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await _enable(app_pool, owner, tenant_id)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        result = await _gateway().call(ctx, stage="crm_it_cut", role="writer", prompt=f"x {STUB_TRUNCATE}")
    assert (result.outcome, result.error_class, result.text) == ("error", "truncated", None)
    rows = await _rows(admin, tenant_id)
    assert [(r["outcome"], r["error_class"]) for r in rows] == [("truncated", "truncated")] * 2
