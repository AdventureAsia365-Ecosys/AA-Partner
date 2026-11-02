"""AA-1027 slice F3 on PostgreSQL with the RDS role shape, after crm-040 (an expand file): the real ``tenant_tx()``,
stub gateway, vault and ``crm.ai_draft`` against the real CHECKs, RLS and guard (CRM47) — the orchestrator's Dev
checks, reproduced here.

- An agency whose plan lacks ``ai_assist`` gets 403 ``feature_not_allowed``: nothing is written but the ``denied``
  audit event.
- With the plan and AI on, a stub draft writes one pending ``crm.ai_draft`` row (ciphertext only, the ledger call id)
  next to its ``crm.llm_call`` row; the member accepts it once (decided_by = the member); a second decision is 409.
- A draft row the database refuses (here its blob CHECK) rolls back only its savepoint: the paid call's ledger row
  stays and the member still gets the text, with no draft id.
- The guard: only a member inserts (pending) and decides, once; nothing else changes; no delete; another agency sees
  no row.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from app.errors import Conflict, Forbidden, NotFound
from app.llm import agency_ai, ai_drafts
from app.llm.gateway import LlmGateway, StubProvider, StubScrubber
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_aa964_ai_gateway import LIMITS, _trip, allow_ai_assist
from tests.integration.test_crm005_intake import _agency, _keys, _owner_fetch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DRAFT_SQL = "SELECT ai_draft_id, trip_id, kind, llm_call_id, prompt_version, output_enc, decision, feedback_code, " \
    "decided_by FROM crm.ai_draft ORDER BY created_at"  # fmt: skip


async def _enable(pool: asyncpg.Pool, owner: object, tenant_id: uuid.UUID) -> None:
    async with tenant_tx(pool, owner, tenant_id) as ctx:
        await agency_ai.update_ai_settings(ctx, {"ai_enabled": True}, **LIMITS)


async def _draft(pool: asyncpg.Pool, owner: object, tenant_id: uuid.UUID, trip_id: uuid.UUID, text: str) -> dict:
    keys = _keys()
    async with tenant_tx(pool, owner, tenant_id) as ctx:
        return await agency_ai.draft(
            ctx, LlmGateway(StubScrubber(), StubProvider()), Vault(keys, ctx), trip_id, kind="reply", text=text
        )


async def test_crm040_is_registered_with_row_security(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 AS ok FROM shared.schema_versions WHERE version = 'crm-040'") == 1
    assert await admin.fetchval(
        "SELECT relrowsecurity AND relforcerowsecurity AS ok FROM pg_class WHERE oid = 'crm.ai_draft'::regclass"
    )


async def test_without_the_plan_a_draft_is_403_and_writes_only_the_denial(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    trip_id = await _trip(app_pool, tenant_id, ai_assist=False)
    await _enable(app_pool, owner, tenant_id)
    with pytest.raises(Forbidden) as refused:
        await _draft(app_pool, owner, tenant_id, trip_id, "Is the night train bookable?")
    assert refused.value.code == "feature_not_allowed"
    assert await _owner_fetch(admin, tenant_id, "SELECT llm_call_id FROM crm.llm_call") == []
    assert await _owner_fetch(admin, tenant_id, DRAFT_SQL) == []
    denials = await _owner_fetch(
        admin, tenant_id, "SELECT action, outcome, reason FROM crm.audit_event WHERE reason = 'feature_not_allowed'"
    )
    assert [tuple(r) for r in denials] == [("ai_use", "denied", "feature_not_allowed")]
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        view = await agency_ai.ai_settings(ctx, **LIMITS)
    assert view["ai_assist_allowed"] is False


async def test_a_stub_draft_is_one_pending_row_the_member_accepts_once(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    tenant_id, [(owner, member_id)] = await _agency(app_pool)
    trip_id = await _trip(app_pool, tenant_id)
    await _enable(app_pool, owner, tenant_id)
    out = await _draft(app_pool, owner, tenant_id, trip_id, "Could we add a pottery class in Fakeville?")
    assert out["outcome"] == "ok" and out["ai_draft_id"]
    [row] = await _owner_fetch(admin, tenant_id, DRAFT_SQL)
    [call] = await _owner_fetch(admin, tenant_id, "SELECT llm_call_id FROM crm.llm_call")
    assert (str(row["ai_draft_id"]), row["trip_id"], row["kind"], row["decision"]) == (
        out["ai_draft_id"], trip_id, "reply", "pending",
    )  # fmt: skip
    assert row["llm_call_id"] == call["llm_call_id"] and row["prompt_version"] == agency_ai.PROMPT_VERSION
    assert b"pottery" not in bytes(row["output_enc"]) and b"stub" not in bytes(row["output_enc"])
    draft_id = row["ai_draft_id"]
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        decided = await ai_drafts.decide(ctx, draft_id, action="accept", feedback_code="good")
    assert (decided["decision"], decided["feedback_code"]) == ("accepted", "good")
    [after] = await _owner_fetch(admin, tenant_id, DRAFT_SQL)
    assert (after["decision"], after["decided_by"]) == ("accepted", f"member:{member_id}")
    with pytest.raises(Conflict):
        async with tenant_tx(app_pool, owner, tenant_id) as ctx:
            await ai_drafts.decide(ctx, draft_id, action="discard")
    other, [(other_owner, _)] = await _agency(app_pool)
    async with tenant_tx(app_pool, other_owner, other) as ctx:
        assert await ctx.conn.fetchval("SELECT count(*) AS n FROM crm.ai_draft") == 0


class _ShortBlobVault(Vault):
    """The real vault for every read, but a draft blob the table's CHECK refuses (a real PostgreSQL error)."""

    async def encrypt(
        self, table: str, column: str, row_id: uuid.UUID | str, value: str | bytes, *, purpose: str = "vault"
    ) -> bytes:
        if table == "ai_draft":
            return b"too short"
        return await super().encrypt(table, column, row_id, value, purpose=purpose)


async def test_a_draft_row_the_database_refuses_keeps_the_paid_call(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    # The model answered: the ledger row (cost, budget) commits even though the draft row failed its CHECK.
    tenant_id, [(owner, _)] = await _agency(app_pool)
    trip_id = await _trip(app_pool, tenant_id)
    await _enable(app_pool, owner, tenant_id)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        out = await agency_ai.draft(
            ctx, LlmGateway(StubScrubber(), StubProvider()), _ShortBlobVault(_keys(), ctx), trip_id,
            kind="reply", text="Is the river cruise on day three?",
        )  # fmt: skip
    assert (out["outcome"], out["ai_draft_id"]) == ("ok", None) and out["text"] and out["llm_call_id"]
    [call] = await _owner_fetch(admin, tenant_id, "SELECT llm_call_id FROM crm.llm_call")
    assert str(call["llm_call_id"]) == out["llm_call_id"]
    assert await _owner_fetch(admin, tenant_id, DRAFT_SQL) == []


async def _seed_draft(pool: asyncpg.Pool, admin: asyncpg.Connection) -> tuple[uuid.UUID, object, uuid.UUID]:
    tenant_id, [(owner, _)] = await _agency(pool)
    trip_id = await _trip(pool, tenant_id)
    await _enable(pool, owner, tenant_id)
    out = await _draft(pool, owner, tenant_id, trip_id, "Do you have a quieter hotel?")
    return tenant_id, owner, uuid.UUID(out["ai_draft_id"])


async def _as_actor(pool: asyncpg.Pool, tenant_id: uuid.UUID, actor: str, sql: str, *args: object) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        await conn.execute(sql, *args)


async def test_the_guard_holds_who_writes_and_what_changes(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    tenant_id, _, draft_id = await _seed_draft(app_pool, admin)
    [row] = await _owner_fetch(admin, tenant_id, DRAFT_SQL)
    member = "member:00000000-0000-4000-8000-00000000c0de"
    insert = (
        "INSERT INTO crm.ai_draft (trip_id, kind, llm_call_id, prompt_version, output_enc) "
        "VALUES ($1, 'reply', $2, 'crm-draft-1', $3)"
    )
    with pytest.raises(asyncpg.PostgresError, match="ai_draft_guard"):  # a job never writes a draft
        await _as_actor(app_pool, tenant_id, "job:crm_other", insert, row["trip_id"], uuid.uuid4(), bytes(40))
    with pytest.raises(asyncpg.PostgresError, match="only the decision"):
        await _as_actor(
            app_pool, tenant_id, member,
            "UPDATE crm.ai_draft SET decision = 'discarded', decided_at = now(), decided_by = $2, kind = 'follow_up' "
            "WHERE ai_draft_id = $1", draft_id, member,
        )  # fmt: skip
    with pytest.raises(asyncpg.PostgresError, match="ai_draft_guard"):
        await _as_actor(app_pool, tenant_id, member, "DELETE FROM crm.ai_draft WHERE ai_draft_id = $1", draft_id)
    with pytest.raises(asyncpg.CheckViolationError):  # a decision names who and when
        await _as_actor(
            app_pool, tenant_id, member, "UPDATE crm.ai_draft SET decision = 'discarded' WHERE ai_draft_id = $1",
            draft_id,
        )  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):  # feedback from the fixed list only
        await _as_actor(
            app_pool, tenant_id, member,
            "UPDATE crm.ai_draft SET decision = 'discarded', decided_at = now(), decided_by = $2, "
            "feedback_code = 'meh' WHERE ai_draft_id = $1", draft_id, member,
        )  # fmt: skip
    await _as_actor(
        app_pool, tenant_id, member,
        "UPDATE crm.ai_draft SET decision = 'discarded', decided_at = now(), decided_by = $2, "
        "feedback_code = 'too_long' WHERE ai_draft_id = $1", draft_id, member,
    )  # fmt: skip
    with pytest.raises(asyncpg.PostgresError, match="decided once"):
        await _as_actor(
            app_pool, tenant_id, member,
            "UPDATE crm.ai_draft SET decision = 'accepted' WHERE ai_draft_id = $1", draft_id,
        )  # fmt: skip


async def test_a_decision_of_a_missing_draft_is_404(app_pool: asyncpg.Pool) -> None:
    tenant_id, [(owner, _)] = await _agency(app_pool)
    await allow_ai_assist(app_pool, tenant_id)
    with pytest.raises(NotFound) as missing:
        async with tenant_tx(app_pool, owner, tenant_id) as ctx:
            await ai_drafts.decide(ctx, uuid.uuid4(), action="accept")
    assert missing.value.code == "draft_not_found"
