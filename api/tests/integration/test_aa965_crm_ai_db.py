"""AA-965 (card CRM-33b) on PostgreSQL with the RDS role shape, after crm-076: the real ``tenant_tx()``, a stub
gateway (no network, cost 0), the vault and the crm.ai_draft / crm.llm_call / crm.voice_profile rows.

- R10: a run on a flight supplier's open lines stores one pending ``supplier_confirmation`` draft next to its
  ``crm.llm_call`` row (stage ``crm_supplier_confirm``) and changes no line; applying it confirms the line with the
  agent's reference and accepts the draft; the AA supplier is never read.
- Budget hard stop: with the month's budget used up the run is refused ``budget_exhausted`` (one 0 USD ledger row),
  nothing is scrubbed, no draft is written.
- R40: the closed list comes from the agency's sellable products whose tour is served; the stored draft keeps only
  listed codes.
- R41: voices are the member's (the setting caps them), a draft in a voice records it.
Fixtures use invented names and places only.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.booking.contract import Component, Product
from app.errors import Conflict, Forbidden
from app.ledger import lines, suppliers
from app.llm import agency_ai, notes_options, supplier_confirm, voices
from app.llm.gateway import Completion, LlmGateway, StubProvider, StubScrubber
from app.tenancy import tenant_tx
from app.vault import Vault
from tests.integration.test_aa964_ai_gateway import LIMITS, allow_ai_assist
from tests.integration.test_builder_api import _create
from tests.integration.test_builder_api import _setup as _builder_setup
from tests.integration.test_crm005_intake import _owner_fetch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class JsonStub(StubProvider):
    """The stub model answering a fixed text (cost 0, no network)."""

    def __init__(self, answer: str, model: str = "stub-echo-1", family: str | None = "stub", position: int = 0):
        super().__init__(model, family, position)
        self.answer = answer
        self.prompts: list[str] = []

    def for_route(self, entry: Any) -> JsonStub:
        return self

    async def complete(self, prompt: str, *, system: str | None = None, max_tokens: int | None = None) -> Completion:
        self.prompts.append(prompt)
        return Completion(text=self.answer, input_tokens=len(prompt.split()), output_tokens=9, cost_usd=Decimal(0))


async def _ready(pool: asyncpg.Pool, s: dict[str, Any], budget: str | None = None) -> None:
    await allow_ai_assist(pool, s["tenant"])
    changes: dict[str, Any] = {"ai_enabled": True}
    if budget is not None:
        changes["ai_budget_usd"] = Decimal(budget)
    async with tenant_tx(pool, s["who"]["owner"], s["tenant"]) as ctx:
        await agency_ai.update_ai_settings(ctx, changes, **LIMITS)
        # validation level off: a confirm needs no money rows (the AA-1069 test does the same)
        await ctx.conn.execute("UPDATE crm.tenant_settings SET booking_validation_level = 'off'")


async def _flight_lines(pool: asyncpg.Pool, s: dict[str, Any]) -> tuple[uuid.UUID, list[uuid.UUID]]:
    async with tenant_tx(pool, s["who"]["agent"], s["tenant"]) as ctx:
        made = await suppliers.create_supplier(
            ctx, Vault(s["keys"], ctx), {"name": "Lantern Air", "category": "flight"}
        )
        supplier = uuid.UUID(made["agency_supplier_id"])
        out = []
        for i, title in enumerate(("Valley hop out", "Valley hop back")):
            day = (s["start"] + dt.timedelta(days=4 * i)).isoformat()  # L1 = the earlier line (closed-list order)
            body = {"agency_supplier_id": supplier, "title": title, "service_from": day}
            line = await lines.create_line(ctx, s["trip"], body)
            out.append(uuid.UUID(line["booking_line_id"]))
    return supplier, out


ANSWER = '{"lines": [{"line": "L1", "status": "confirmed", "confirmation_no": "LNT4K9", "times": "07:40"}, ' \
    '{"line": "L7", "status": "confirmed"}]}'  # fmt: skip


async def test_a_supplier_confirmation_proposes_then_the_agent_applies(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    s = await _builder_setup(app_pool)
    await _ready(app_pool, s)
    supplier, (out_line, back_line) = await _flight_lines(app_pool, s)
    stub = JsonStub(ANSWER)
    gateway = LlmGateway(StubScrubber(), stub)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        run = await supplier_confirm.run(
            ctx, gateway, Vault(s["keys"], ctx), s["trip"], agency_supplier_id=supplier,
            text="Your valley hop out is confirmed, reference LNT4K9, departing 07:40.",
        )  # fmt: skip
    assert run["outcome"] == "ok" and run["rejected"] == 1
    assert [(x["booking_line_id"], x["status"], x["confirmation_no"]) for x in run["lines"]] == [
        (str(out_line), "confirmed", "LNT4K9")
    ]
    [call] = await _owner_fetch(admin, s["tenant"], "SELECT llm_call_id, stage, role, outcome FROM crm.llm_call")
    assert (call["stage"], call["role"], call["outcome"]) == ("crm_supplier_confirm", "writer", "ok")
    [draft] = await _owner_fetch(admin, s["tenant"], "SELECT kind, decision, llm_call_id, validation FROM crm.ai_draft")
    assert (draft["kind"], draft["decision"], draft["llm_call_id"]) == ("supplier_confirmation", "pending",
                                                                        call["llm_call_id"])  # fmt: skip
    validation = json.loads(draft["validation"])
    assert set(validation["closed_list"]) == {str(out_line), str(back_line)} and validation["rejected"] == 1
    statuses = await _owner_fetch(admin, s["tenant"], "SELECT status FROM crm.booking_line WHERE trip_id = $1",
                                  s["trip"])  # fmt: skip
    assert {r["status"] for r in statuses} == {"planning"}  # the run changed nothing
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        applied = await supplier_confirm.apply(
            ctx, uuid.UUID(run["ai_draft_id"]),
            [{"booking_line_id": out_line, "action": "confirm", "confirmation_no": "LNT4K9"}], feedback_code="good",
        )  # fmt: skip
    assert applied["decision"] == "accepted"
    [line] = await _owner_fetch(
        admin, s["tenant"], "SELECT status, confirmation_no FROM crm.booking_line WHERE booking_line_id = $1", out_line
    )
    assert (line["status"], line["confirmation_no"]) == ("confirmed", "LNT4K9")
    with pytest.raises(Conflict):  # once
        async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
            await supplier_confirm.apply(ctx, uuid.UUID(run["ai_draft_id"]),
                                         [{"booking_line_id": back_line, "action": "confirm"}])  # fmt: skip
    aa_row = await _owner_fetch(
        admin, s["tenant"], "SELECT agency_supplier_id FROM crm.agency_supplier WHERE kind = 'aa'"
    )
    if aa_row:  # the agency's AA supplier row is never read by AI
        with pytest.raises(Forbidden) as refused:
            async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
                aa_supplier = aa_row[0]["agency_supplier_id"]
                await supplier_confirm.run(ctx, gateway, Vault(s["keys"], ctx), s["trip"],
                                           agency_supplier_id=aa_supplier, text="Confirmed")  # fmt: skip
        assert refused.value.code == "aa_line_read_only"
    assert len(stub.prompts) == 1


async def test_a_used_up_budget_is_a_hard_stop(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    s = await _builder_setup(app_pool)
    await _ready(app_pool, s, budget="0")
    supplier, _ = await _flight_lines(app_pool, s)
    stub = JsonStub(ANSWER)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        run = await supplier_confirm.run(
            ctx, LlmGateway(StubScrubber(), stub), Vault(s["keys"], ctx), s["trip"], agency_supplier_id=supplier,
            text="Confirmed, reference LNT4K9.",
        )  # fmt: skip
    assert (run["outcome"], run["refusal_code"], run["ai_draft_id"]) == ("refused", "budget_exhausted", None)
    assert stub.prompts == []
    [call] = await _owner_fetch(admin, s["tenant"], "SELECT outcome, refusal_code, cost_usd_est FROM crm.llm_call")
    assert (call["outcome"], call["refusal_code"], call["cost_usd_est"]) == ("refused", "budget_exhausted", 0)
    assert await _owner_fetch(admin, s["tenant"], "SELECT 1 AS ok FROM crm.ai_draft") == []


async def test_notes_to_options_keeps_only_the_closed_list(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    s = await _builder_setup(app_pool)  # a Nepal trip
    await _ready(app_pool, s)
    async with app_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO crm.aa_catalog_tour (source_id, title, country_code, duration_days, served_state) "
            "VALUES ('T-965', 'Prayer flag ridge 2 days', 'NP', 2, 'served'), "
            "('T-966', 'Lakeside paddle 1 day', 'TH', 1, 'served') "
            "ON CONFLICT (source_system, source_id) DO UPDATE SET served_state = 'served'"
        )
    products = {
        "T-965": Product("T-965", "tp-965", True, False, (Component("c-ridge", "trip_plan", 1),
                                                           Component("c-lodge", "hotel_room", 1))),
        "T-966": Product("T-966", "tp-966", True, False, (Component("c-paddle", "activity", 1),)),
    }  # fmt: skip
    v = await _create(app_pool, s)
    day = next(d for d in v["days"] if d["day_no"] == 2)
    stub = JsonStub('{"options": [{"code": "A2", "why": "Wants a cosy lodge."}, {"code": "A3"}, {"code": "c-paddle"}]}')
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        row = await notes_options.day_of(ctx, uuid.UUID(day["proposal_day_id"]))
        out = await notes_options.run(
            ctx, LlmGateway(StubScrubber(), stub), Vault(s["keys"], ctx), row, products, destinations=["nepal"],
            notes="The family wants a cosy lodge after the walk.",
        )  # fmt: skip
    assert "Lakeside paddle" not in stub.prompts[0]  # another country: not in the closed list
    assert [(o["code"], o["component_id"]) for o in out["options"]] == [("A2", "c-lodge")]
    assert out["closed_list_size"] == 2 and out["rejected"] == 2
    [draft] = await _owner_fetch(admin, s["tenant"], "SELECT kind, validation FROM crm.ai_draft")
    stored = json.loads(draft["validation"])
    assert draft["kind"] == "notes_to_options" and [o["component_id"] for o in stored["options"]] == ["c-lodge"]
    [call] = await _owner_fetch(admin, s["tenant"], "SELECT stage FROM crm.llm_call")
    assert call["stage"] == "crm_notes_options"


async def test_voices_are_capped_per_member_and_recorded_on_the_draft(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    s = await _builder_setup(app_pool)
    await _ready(app_pool, s)
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        made = [await voices.create(ctx, {"name": f"Voice {i}", "descriptors": {"warmth": "direct"}}) for i in range(4)]
        with pytest.raises(Conflict) as refused:  # the default of crm.ai.voice_profiles_per_member_max is 4
            await voices.create(ctx, {"name": "Voice five"})
        assert refused.value.code == "voice_limit_reached"
    async with tenant_tx(app_pool, s["who"]["assistant"], s["tenant"]) as ctx:
        listed = await voices.list_voices(ctx)
        assert len(listed["voices"]) == 4 and listed["mine"] == 0 and not listed["voices"][0]["can_edit"]
        with pytest.raises(Forbidden):
            await voices.delete(ctx, uuid.UUID(made[0]["voice_profile_id"]))
    voice = uuid.UUID(made[1]["voice_profile_id"])
    async with tenant_tx(app_pool, s["who"]["agent"], s["tenant"]) as ctx:
        out = await agency_ai.draft(
            ctx, LlmGateway(StubScrubber(), StubProvider()), Vault(s["keys"], ctx), s["trip"], kind="reply",
            text="Can we start later?", voice_profile_id=voice,
        )  # fmt: skip
    assert out["outcome"] == "ok" and out["voice_profile_id"] == str(voice)
    [draft] = await _owner_fetch(admin, s["tenant"], "SELECT voice_profile_id, prompt_version FROM crm.ai_draft")
    assert (draft["voice_profile_id"], draft["prompt_version"]) == (voice, agency_ai.VOICE_PROMPT_VERSION)
    async with tenant_tx(app_pool, s["who"]["owner"], s["tenant"]) as ctx:  # the owner may delete any voice
        await voices.delete(ctx, voice)
    [draft] = await _owner_fetch(admin, s["tenant"], "SELECT voice_profile_id FROM crm.ai_draft")
    assert draft["voice_profile_id"] == voice  # kept as history
