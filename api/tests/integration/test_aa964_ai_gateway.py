"""AA-964 (card CRM-33a) on PostgreSQL with the RDS role shape, after crm-032: the real ``tenant_tx()``, gateway,
ledger, settings and console code against the real CHECKs, RLS and append-only trigger.

- Settings > AI: off by default; the owner switches it on; a draft with AI off writes nothing.
- A stub draft writes exactly one ``crm.llm_call`` row with ``app = 'crm'`` and the ledger v2 columns (the Dev live
  check of the orchestrator, reproduced here).
- A guardrail attempt (fake ``bedrock-runtime`` client: no AWS) is its own ``guard`` row with text units and cost,
  and the budget alert it crosses is stored once in ``crm.tenant_settings`` with one audit event.
- ``crm.ops.v1 /llm/calls``: rows of every agency newest first, scrubbed fields only, keyset paging, one ``ops_read``
  event in each agency read.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.errors import Conflict
from app.llm import agency_ai
from app.llm.gateway import LlmGateway, StubProvider, StubScrubber
from app.ops import console
from app.privacy.guardrails import BedrockGuardrailsDetector
from app.privacy.scrubber import PiiScrubber
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_crm005_intake import _agency, _keys, _owner_fetch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

LIMITS = {"budget_max": Decimal("2"), "provider": "stub", "rate_per_minute": 6}
STAFF = "staff-l1"


class GuardClient:
    def __init__(self, units: int) -> None:
        self.units = units

    def apply_guardrail(self, **_: Any) -> dict[str, Any]:
        return {
            "action": "NONE",
            "assessments": [{"sensitiveInformationPolicy": {"piiEntities": []}}],
            "usage": {"sensitiveInformationPolicyUnits": self.units},
            "ResponseMetadata": {"RequestId": "guard-req-it"},
        }


async def _trip(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> uuid.UUID:
    trip_id = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', 'seed:test', true)")
        await conn.execute("INSERT INTO crm.trip (trip_id, title) VALUES ($1, 'Fakeland 5 nights')", trip_id)
    return trip_id


async def _rows(admin: asyncpg.Connection, tenant_id: uuid.UUID) -> list[asyncpg.Record]:
    return await _owner_fetch(
        admin,
        tenant_id,
        "SELECT stage, role, provider, app, outcome, connection_key, family, prompt_version, prompt_hash, units, "
        "unit_kind, cost_usd_est, pricing_mode, attempt_no, is_shadow FROM crm.llm_call ORDER BY at, llm_call_id",
    )


async def test_settings_off_by_default_then_a_stub_draft_writes_one_crm_row(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    trip_id = await _trip(app_pool, tenant_id)
    keys = _keys()
    gateway = LlmGateway(StubScrubber(), StubProvider())
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        view = await agency_ai.ai_settings(ctx, **LIMITS)
        assert (view["ai_enabled"], view["ai_budget_usd"], view["can_edit"]) == (False, "2.00", True)
    with pytest.raises(Conflict):
        async with tenant_tx(app_pool, owner, tenant_id) as ctx:
            await agency_ai.draft(ctx, gateway, Vault(keys, ctx), trip_id, kind="reply", text="Hello")
    assert await _rows(admin, tenant_id) == []
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await agency_ai.update_ai_settings(ctx, {"ai_enabled": True}, **LIMITS)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        out = await agency_ai.draft(
            ctx, gateway, Vault(keys, ctx), trip_id, kind="reply", text="Could we add a cooking class?"
        )
    assert out["outcome"] == "ok" and out["approval_required"] is True
    [row] = await _rows(admin, tenant_id)
    assert (row["stage"], row["role"], row["app"], row["outcome"]) == ("crm_draft_reply", "writer", "crm", "ok")
    assert (row["connection_key"], row["family"], row["prompt_version"]) == ("stub", "stub", "crm-draft-1")
    assert len(row["prompt_hash"]) == 64 and row["attempt_no"] == 1 and row["is_shadow"] is False
    assert "cooking" not in repr(dict(row))


async def test_a_guardrail_row_counts_against_the_budget_and_raises_the_alert_once(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    trip_id = await _trip(app_pool, tenant_id)
    keys = _keys()
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        await agency_ai.update_ai_settings(ctx, {"ai_enabled": True, "ai_budget_usd": Decimal("0.01")}, **LIMITS)
    detector = BedrockGuardrailsDetector("abc123", "1", region="us-west-1", timeout_s=3, client=GuardClient(80))
    gateway = LlmGateway(PiiScrubber(detector), StubProvider(), keys=keys)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        out = await agency_ai.draft(ctx, gateway, Vault(keys, ctx), trip_id, kind="reply", text="Hello there")
    assert out["outcome"] == "ok" and out["budget_alert"] == 75
    rows = await _rows(admin, tenant_id)  # one transaction: both rows share at = now(); tell them apart by role
    [guard] = [r for r in rows if r["role"] == "guard"]
    [call] = [r for r in rows if r["role"] != "guard"]
    assert (guard["stage"], guard["role"], guard["units"], guard["unit_kind"]) == (
        "crm_scrub",
        "guard",
        80,
        "text_unit",
    )
    assert guard["cost_usd_est"] == Decimal("0.008") and guard["pricing_mode"] == "code_table"
    assert call["role"] == "writer"
    [settings] = await _owner_fetch(admin, tenant_id, "SELECT ai_alert_month, ai_alert_pct FROM crm.tenant_settings")
    assert settings["ai_alert_pct"] == 75 and settings["ai_alert_month"].day == 1
    alerts = await _owner_fetch(
        admin, tenant_id, "SELECT reason FROM crm.audit_event WHERE action = 'ai_budget_alert' ORDER BY at"
    )
    assert [a["reason"] for a in alerts] == ["pct_75"]
    # The next guardrail call takes the month to 160 %: one 100 % alert; the call itself is refused (R: manual).
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        again = await agency_ai.draft(ctx, gateway, Vault(keys, ctx), trip_id, kind="reply", text="Hello again")
    assert (again["outcome"], again["refusal_code"], again["fallback"]) == (
        "refused",
        "budget_exhausted",
        "manual_reply_needed",
    )
    assert again["budget_alert"] == 100
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        third = await agency_ai.draft(ctx, gateway, Vault(keys, ctx), trip_id, kind="reply", text="And again")
    assert third["budget_alert"] is None and third["refusal_code"] == "budget_exhausted"
    # Review AA-964: a used-up month refuses before the scrub, so the third draft pays no guardrail units.
    assert len([r for r in await _rows(admin, tenant_id) if r["role"] == "guard"]) == 2
    alerts = await _owner_fetch(
        admin, tenant_id, "SELECT reason FROM crm.audit_event WHERE action = 'ai_budget_alert' ORDER BY at"
    )
    assert [a["reason"] for a in alerts] == ["pct_75", "pct_100"]


async def test_ops_llm_calls_reads_every_agency_newest_first(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    one, [(owner_one, _)] = await _agency(app_pool)
    two, [(owner_two, _)] = await _agency(app_pool)
    keys = _keys()
    gateway = LlmGateway(StubScrubber(), StubProvider())
    for tenant_id, owner in ((one, owner_one), (two, owner_two)):
        trip_id = await _trip(app_pool, tenant_id)
        async with tenant_tx(app_pool, owner, tenant_id) as ctx:
            await agency_ai.update_ai_settings(ctx, {"ai_enabled": True}, **LIMITS)
        for text in ("first", "second"):
            async with tenant_tx(app_pool, owner, tenant_id) as ctx:
                await agency_ai.draft(ctx, gateway, Vault(keys, ctx), trip_id, kind="reply", text=text)
    today = dt.datetime.now(dt.UTC).date()
    page = await console.platform_llm_calls(app_pool, staff_sub=STAFF, start=today, end=today, tenant_id=one, limit=1)
    assert len(page["rows"]) == 1 and page["next_cursor"]
    row = page["rows"][0]
    assert (row["tenant_id"], row["app"], row["status"], row["stage"]) == (str(one), "crm", "ok", "crm_draft_reply")
    assert "scrub_receipt" not in row and "created_by" not in row
    rest = await console.platform_llm_calls(
        app_pool, staff_sub=STAFF, start=today, end=today, tenant_id=one, limit=5, cursor=page["next_cursor"]
    )
    assert len(rest["rows"]) == 1 and rest["next_cursor"] is None
    assert rest["rows"][0]["call_id"] != row["call_id"] and rest["rows"][0]["at"] <= row["at"]
    everyone = await console.platform_llm_calls(app_pool, staff_sub=STAFF, start=today, end=today, limit=500)
    mine = [r for r in everyone["rows"] if r["tenant_id"] in {str(one), str(two)}]
    assert len(mine) == 4 and [r["at"] for r in everyone["rows"]] == sorted(
        (r["at"] for r in everyone["rows"]), reverse=True
    )
    refused = await console.platform_llm_calls(
        app_pool, staff_sub=STAFF, start=today, end=today, tenant_id=one, status="refused"
    )
    assert refused["rows"] == []
    reads = await _owner_fetch(
        admin, one, "SELECT count(*) AS n FROM crm.audit_event WHERE action = 'ops_read' AND reason = 'llm_calls'"
    )
    assert reads[0]["n"] >= 2
