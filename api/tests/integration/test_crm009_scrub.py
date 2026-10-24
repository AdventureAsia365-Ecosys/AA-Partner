"""AA-938 (card CRM-10) on PostgreSQL 15 with the RDS role shape, as crm_app, with real field encryption.

- The crm-009 shape (RLS, write once, 24 h, readers, receipt keys, the purge policy) is
  tests/integration/test_crm009_schema.py; here the job handler ``purge_expired_maps`` runs against it.
- End to end: the known values of a real inquiry are scrubbed before the (stub-named, real-shaped) provider sees the
  prompt; the map is sealed under the ``scrub`` DEK and restored only in the agency member's session; another agency
  cannot read it; a Guardrails timeout makes no call (card R test).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Sequence
from decimal import Decimal

import asyncpg
import pytest

from app.errors import Forbidden, Gone
from app.llm.gateway import Completion, LlmGateway
from app.privacy.known import known_values_for_trip
from app.privacy.placeholder_store import restore_placeholders
from app.privacy.purge import purge_expired_maps
from app.privacy.scrubber import Detection, PiiScrubber
from app.tenancy import system_tx, tenant_tx
from app.travellers.service import add_relation, create_traveler
from app.vault import Vault
from tests.integration.test_crm005_intake import _agency, _keys, _owner_fetch
from tests.integration.test_crm006_travellers import _inquiry

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class FakeGuardrails:
    """Pass 3 stand-in: flags one stranger's name, or sleeps past the scrubber's timeout."""

    name = "bedrock_guardrails"

    def __init__(self, slow: bool = False) -> None:
        self.slow = slow
        self.texts: list[str] = []

    async def detect(self, text: str) -> Sequence[Detection]:
        self.texts.append(text)
        if self.slow:
            await asyncio.sleep(2)
        return [Detection("NAME", "Peter Lawson")] if "Peter Lawson" in text else []


class Model:
    name = "bedrock"
    model = "us.example-model-v1"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> Completion:
        self.prompts.append(prompt)
        return Completion("Dear <PERSON_1>, we will call <PHONE_1>.", 20, 10, Decimal("0.0005"))


async def _enable_ai(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> None:
    async with system_tx(pool, tenant_id, actor="job:crm_test") as ctx:
        await ctx.conn.execute("INSERT INTO crm.tenant_settings (ai_enabled, ai_budget_usd) VALUES (true, 2)")


async def test_e_known_values_scrubbed_map_sealed_and_restored_only_in_the_agency_session(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    tenant_id, owner, trip_id, lead = await _inquiry(app_pool, keys)
    await _enable_ai(app_pool, tenant_id)
    model, guard = Model(), FakeGuardrails()
    gateway = LlmGateway(PiiScrubber(guard), model, keys=keys)
    prompt = (
        "Draft a reply to Nguyen Thi Hoa (hoa.nguyen@example.test, 0912 345 678) about Sapa; "
        "her friend Peter Lawson recommended us. Component 3f2b8c1e-7d4a-4f6b-9a2e-0c5d8e1f2a3b."
    )
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        known = await known_values_for_trip(ctx, Vault(keys, ctx), trip_id)
        result = await gateway.call(ctx, stage="crm_reply", role="writer", prompt=prompt, known=known)
    assert result.outcome == "ok" and result.placeholder_map_id and result.fallback is None
    [sent] = model.prompts
    for value in ("Hoa", "hoa.nguyen", "345 678", "Peter", "Lawson"):
        assert value.lower() not in sent.lower(), value
    assert "Sapa" in sent and "3f2b8c1e-7d4a-4f6b-9a2e-0c5d8e1f2a3b" in sent  # places and ids survive
    assert "Nguyen Thi Hoa" not in guard.texts[0]  # Guardrails sees only what passes 1 and 2 left
    assert result.text == "Dear <PERSON_1>, we will call <PHONE_1>."  # the output keeps its placeholders

    [row] = await _owner_fetch(
        admin,
        tenant_id,
        "SELECT p.placeholder_count, p.scrubber_version, p.expires_at - p.created_at AS ttl, c.scrub_receipt, "
        "       octet_length(p.map_enc) AS n, p.map_enc FROM crm.placeholder_map p "
        "JOIN crm.llm_call c ON c.llm_call_id = p.llm_call_id WHERE p.placeholder_map_id = $1",
        uuid.UUID(result.placeholder_map_id),
    )
    assert row["placeholder_count"] >= 3 and row["scrubber_version"] == "f3-1"
    assert row["ttl"].total_seconds() == 24 * 3600
    assert b"Hoa" not in bytes(row["map_enc"]) and b"Lawson" not in bytes(row["map_enc"])  # encrypted
    receipt = json.loads(row["scrub_receipt"])
    assert receipt["passes"]["known"] >= 2 and receipt["passes"]["detector"] == 1 and "Hoa" not in str(receipt)

    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        restored = await restore_placeholders(ctx, Vault(keys, ctx), uuid.UUID(result.placeholder_map_id), result.text)
    assert restored.startswith("Dear Nguyễn Thị Hoa,") and "0912 345 678" in restored
    events = await _owner_fetch(
        admin,
        tenant_id,
        "SELECT action, outcome, object_ids FROM crm.audit_event WHERE action IN "
        "('scrub_known_values', 'placeholder_restore') ORDER BY at, event_id",
    )
    assert [(e["action"], e["outcome"]) for e in events] == [
        ("scrub_known_values", "allowed"),
        ("placeholder_restore", "allowed"),
    ]
    assert str(lead) in events[0]["object_ids"]

    # A job's context never restores; another agency cannot even see the row.
    async with system_tx(app_pool, tenant_id, actor="job:crm_test") as ctx:
        with pytest.raises(Forbidden):
            await restore_placeholders(ctx, Vault(keys, ctx), uuid.UUID(result.placeholder_map_id), "x")  # type: ignore[arg-type]
    other_id, [(other, _)] = await _agency(app_pool)
    with pytest.raises(Gone):
        async with tenant_tx(app_pool, other, other_id) as ctx:
            await restore_placeholders(ctx, Vault(keys, ctx), uuid.UUID(result.placeholder_map_id), "x")


async def test_r_guardrails_timeout_means_no_call_and_manual_reply(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    tenant_id, _ = await _agency(app_pool)
    await _enable_ai(app_pool, tenant_id)
    model = Model()
    gateway = LlmGateway(PiiScrubber(FakeGuardrails(slow=True), timeout_s=0.2), model, keys=keys)
    async with system_tx(app_pool, tenant_id, actor="job:crm_test") as ctx:
        result = await gateway.call(ctx, stage="crm_reply", role="writer", prompt="Reply to Peter Lawson please")
        rows = await ctx.conn.fetch("SELECT outcome, refusal_code, cost_usd_est FROM crm.llm_call")
        maps = await ctx.conn.fetchval("SELECT count(*) FROM crm.placeholder_map")
    assert (result.outcome, result.refusal_code, result.fallback) == ("refused", "scrub_timeout", "manual_reply_needed")
    assert model.prompts == []  # no call
    assert [(r["outcome"], r["refusal_code"], r["cost_usd_est"]) for r in rows] == [("refused", "scrub_timeout", 0)]
    assert maps == 0


async def test_purge_deletes_expired_maps_of_every_agency_only_with_the_scope(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    ids = {}
    for label in ("a", "b"):
        tenant_id, _ = await _agency(app_pool)
        async with system_tx(app_pool, tenant_id, actor="job:crm_test") as ctx:
            calls = [
                await ctx.conn.fetchval(
                    "INSERT INTO crm.llm_call (stage, role, provider, model, outcome) "
                    "VALUES ('crm_x', 'writer', 'stub', 'stub-echo-1', 'ok') RETURNING llm_call_id"
                )
                for _ in range(2)
            ]
            for call_id, expires in zip(
                calls, ("now() + interval '1 second'", "now() + interval '23 hours'"), strict=True
            ):
                await ctx.conn.execute(
                    "INSERT INTO crm.placeholder_map (placeholder_map_id, llm_call_id, map_enc, placeholder_count, "
                    f"scrubber_version, expires_at) VALUES ($1, $2, $3, 1, 'f3-1', {expires})",
                    uuid.uuid4(),
                    call_id,
                    b"\x00" * 40,
                )
        ids[label] = tenant_id
    await asyncio.sleep(1.2)
    # Without the scope (and without a tenant) crm_app deletes nothing.
    async with app_pool.acquire() as conn:
        assert await conn.execute("DELETE FROM crm.placeholder_map") == "DELETE 0"
    result = await purge_expired_maps(app_pool, None)  # type: ignore[arg-type]
    assert result["deleted_count"] >= 2
    for tenant_id in ids.values():
        [row] = await _owner_fetch(admin, tenant_id, "SELECT count(*) AS n FROM crm.placeholder_map")
        assert row["n"] == 1  # the live map stays
    # The scope gives no read: the purge transaction cannot SELECT a map.
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.purge_scope', 'placeholder_map', true)")
        assert await conn.fetchval("SELECT count(*) FROM crm.placeholder_map") == 0


async def test_related_traveller_off_the_trip_is_a_known_value(app_pool: asyncpg.Pool) -> None:
    # Review AA-938 (non-blocking): crm-006 relations of the party who are not on the trip are loaded too.
    keys = _keys()
    tenant_id, owner, trip_id, lead = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, owner, tenant_id) as ctx:
        vault = Vault(keys, ctx)
        spouse = await create_traveler(ctx, vault, {"display_name": "Phạm Quang Khải", "phone": "+84 987 111 222"})
        await add_relation(ctx, uuid.UUID(str(spouse["traveler_id"])), lead, "spouse")
        known = await known_values_for_trip(ctx, vault, trip_id)
        scrubbed = await PiiScrubber(FakeGuardrails()).scrub("Chồng chị là anh Pham Quang Khai, 0987 111 222.", known)
    related = [p for p in known.persons if p.role == "related"]
    assert [p.display_name for p in related] == ["Phạm Quang Khải"]
    assert "Khai" not in scrubbed.text and "111 222" not in scrubbed.text
