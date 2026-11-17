"""AA-1113 (S282 audit R28, G12): each CRM AI path, run through the REAL gateway, writes its ledger row and meets its
caps and the F3 scrub — with a fake provider (no AWS, no paid call) and the real F3 scrubber (passes 1-2; the Bedrock
detector is a stub that finds nothing).

The three model call sites of the CRM (``agency_ai.draft``, ``supplier_confirm.run``, ``notes_options.run``) each get:

- **ok**: the provider is called once and never sees a planted e-mail or phone; one ``crm.llm_call`` row (stage, role,
  ``ok``, the provider's cost) and one ``crm.llm_usage_daily`` rollup;
- **agency budget used up**, **CRM daily cap reached**, **scrub unavailable**: one 0 USD refused row each, the provider
  is never called, the result says "reply by hand";
- **provider error**: one row with the failure, "reply by hand".

The service tests with a recording gateway (``test_aa965_crm_ai.py``) cover the prompts and the drafts; the
database-backed budget stop is in ``tests/integration``. Fixtures use invented names and addresses only.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest

from app import platform_settings as ps
from app.booking.contract import Component, Product
from app.llm import agency_ai, notes_options, structured, supplier_confirm
from app.llm import gateway as gateway_module
from app.llm.gateway import MANUAL_REPLY, Completion, LlmGateway
from app.llm.providers.bedrock import ProviderCallError
from app.privacy.guardrails import UnavailableDetector
from app.privacy.placeholder_store import SealedMap
from app.privacy.scrubber import KnownValues, PiiScrubber
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import FakeVault, tenant_ctx

pytestmark = pytest.mark.asyncio

CALL_ID = "00000000-0000-4000-8000-000000001113"
MAP_ID = uuid.UUID("00000000-0000-4000-8000-00000000111a")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000011d1")
SUPPLIER = uuid.UUID("00000000-0000-4000-8000-0000000011a1")
LINE = uuid.UUID("00000000-0000-4000-8000-0000000011b1")
DAY = uuid.UUID("00000000-0000-4000-8000-000000001101")
PROPOSAL = uuid.UUID("00000000-0000-4000-8000-000000001102")
# Planted personal data (invented): the provider must never receive either.
EMAIL = "lumi.tester@harbour-lantern.test"
PHONE = "+84 912 345 678"
CLIENT_TEXT = f"Please write to {EMAIL} or call {PHONE} about the river day."


class FakeProvider:
    """A model that records what it is sent; answers ``text`` or raises ``error``. Priced like a real one."""

    name = "fake"
    connection_key = "fake"
    family = "vendor"

    def __init__(self, text: str = "ok", error: BaseException | None = None) -> None:
        self.model = "vendor.small-model-v1:0"
        self.text = text
        self.error = error
        self.prompts: list[str] = []

    async def complete(self, prompt: str, *, system: str | None = None, max_tokens: int | None = None) -> Completion:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return Completion(text=self.text, input_tokens=400, output_tokens=60, cost_usd=Decimal("0.0012"))


class _NoFindings:
    name = "bedrock_guardrails"

    async def detect(self, text: str) -> list[Any]:
        return []


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Compiled settings only; no trip identity values; the placeholder map sealed and restored in memory."""
    ps.invalidate()
    ps.READER._snapshot = {}

    async def _known(*_: Any, **__: Any) -> KnownValues:
        return KnownValues(())

    async def _seal(_vault: Any, placeholders: dict[str, str]) -> SealedMap:
        return SealedMap(placeholder_map_id=MAP_ID, map_enc=b"sealed", count=len(placeholders))

    async def _store(*_: Any, **__: Any) -> None:
        return None

    async def _restore(_ctx: Any, _vault: Any, _map_id: Any, text: str) -> str:
        return text

    for module in (agency_ai, supplier_confirm, notes_options):
        monkeypatch.setattr(module, "known_values_for_trip", _known)
    monkeypatch.setattr(gateway_module, "seal_map", _seal)
    monkeypatch.setattr(gateway_module, "store_map", _store)
    monkeypatch.setattr(agency_ai, "restore_placeholders", _restore)
    monkeypatch.setattr(structured, "restore_placeholders", _restore)
    yield
    ps.invalidate()
    ps.READER._snapshot = None


def gateway_conn(
    *, budget: str = "2", spent: str = "0", app_cap: str | None = None, day_spent: str = "0"
) -> ScriptedConn:
    """What the gateway reads and writes: the agency settings and month, the app profile and day, the ledger."""
    app_row = None
    if app_cap is not None:
        app_row = {
            "ai_enabled": True,
            "bulk_paused": False,
            "default_daily_budget_usd": Decimal(app_cap),
            "degrade_mode": "manual_reply_needed",
            "disabled_reason": None,
            "updated_at": None,
            "updated_by": "ops.tester",
        }
    return (
        ScriptedConn()
        .on(
            "SELECT ai_enabled, ai_budget_usd, ai_alert_month",
            {"ai_enabled": True, "ai_budget_usd": Decimal(budget), "ai_alert_month": None, "ai_alert_pct": None},
        )
        .on("sum(cost_usd_est)", Decimal(spent))
        .on("FROM crm.llm_app_profile", app_row)
        .on("FROM crm.llm_usage_daily", Decimal(day_spent))
        .on("INSERT INTO crm.llm_call", CALL_ID)
        .on("FROM crm.tenant_entitlement", True)
    )


# ── The three paths: (connection rules, the call, the provider's answer) ─────────────────────────


def _draft_rules(c: ScriptedConn) -> ScriptedConn:
    return c.on("SELECT 1 FROM crm.trip", 1)


async def _draft(c: ScriptedConn, gw: LlmGateway) -> dict[str, Any]:
    return await agency_ai.draft(tenant_ctx(c), gw, FakeVault(), TRIP, kind="reply", text=CLIENT_TEXT)


def _supplier_rules(c: ScriptedConn) -> ScriptedConn:
    return (
        c.on("SELECT destinations FROM crm.trip", ["vietnam"])
        .on("SELECT ai_enabled FROM crm.tenant_settings", True)
        .on("FROM crm.agency_supplier", {"agency_supplier_id": SUPPLIER, "kind": "own", "name": "Kite Air",
                                         "category": "flight"})
        .on("FROM crm.booking_line", [{"booking_line_id": LINE, "title": "Flight leg 1", "service_from": None,
                                       "service_to": None, "status": "requested"}])
    )  # fmt: skip


async def _supplier(c: ScriptedConn, gw: LlmGateway) -> dict[str, Any]:
    return await supplier_confirm.run(
        tenant_ctx(c, role="agent"), gw, FakeVault(), TRIP, agency_supplier_id=SUPPLIER, text=CLIENT_TEXT
    )


PRODUCTS = {
    "tour-lantern": Product(
        "tour-lantern", "tp-tour-lantern", True, False, (Component("tour-lantern-c0", "trip_plan", 1),)
    )
}
DAY_ROW = {"proposal_day_id": DAY, "trip_id": TRIP, "proposal_id": PROPOSAL, "day_no": 2, "title": "Free day",
           "place": "Lantern Bay"}  # fmt: skip


def _notes_rules(c: ScriptedConn) -> ScriptedConn:
    return c.on(
        "FROM crm.aa_catalog_tour", [{"source_id": "tour-lantern", "title": "Lantern Walk", "country_code": "VN"}]
    )


async def _notes(c: ScriptedConn, gw: LlmGateway) -> dict[str, Any]:
    return await notes_options.run(
        tenant_ctx(c), gw, FakeVault(), DAY_ROW, PRODUCTS, destinations=["vietnam"], notes=CLIENT_TEXT
    )


AiPath = tuple[str, Callable[[ScriptedConn], ScriptedConn], Callable[[ScriptedConn, LlmGateway], Awaitable[Any]], str]
PATHS: list[AiPath] = [
    ("crm_draft_reply", _draft_rules, _draft, "Thanks, we will write back today."),
    ("crm_supplier_confirm", _supplier_rules, _supplier, '{"lines": [{"line": "L1", "status": "confirmed"}]}'),
    ("crm_notes_options", _notes_rules, _notes, '{"options": [{"code": "A1", "why": "Fits the day."}]}'),
]
IDS = [p[0] for p in PATHS]


def gateway(provider: FakeProvider, detector: Any = None) -> LlmGateway:
    return LlmGateway(PiiScrubber(detector or _NoFindings()), provider, keys=object())


def ledger(c: ScriptedConn) -> list[tuple[Any, ...]]:
    return c.sql("INSERT INTO crm.llm_call")


@pytest.mark.parametrize(("stage", "rules", "run", "answer"), PATHS, ids=IDS)
async def test_an_answered_call_is_scrubbed_and_writes_one_row_and_one_rollup(
    stage: str, rules: Any, run: Any, answer: str
) -> None:
    c = rules(gateway_conn())
    provider = FakeProvider(answer)
    out = await run(c, gateway(provider))
    [sent] = provider.prompts
    assert EMAIL not in sent and PHONE not in sent and "the river day" in sent
    assert "<EMAIL_1>" in sent and "<PHONE_1>" in sent
    [row] = ledger(c)
    assert (row[0], row[1], row[8], row[6]) == (stage, "writer", "ok", Decimal("0.0012"))
    assert '"scrubber"' in row[11]  # the scrub receipt (counts only) is on the row
    assert len(c.sql("INSERT INTO crm.llm_usage_daily")) == 1
    assert out["outcome"] == "ok" and out["llm_call_id"] == CALL_ID


@pytest.mark.parametrize(("stage", "rules", "run", "answer"), PATHS, ids=IDS)
@pytest.mark.parametrize(
    ("conn_args", "detector", "code"),
    [
        ({"budget": "2", "spent": "2"}, None, "budget_exhausted"),
        ({"app_cap": "1", "day_spent": "1"}, None, "app_budget_exhausted"),
        ({}, UnavailableDetector(), "scrub_failed"),
    ],
    ids=["agency-budget", "crm-daily-cap", "scrub-unavailable"],
)
async def test_a_refusal_is_one_free_row_and_the_provider_is_never_called(
    stage: str, rules: Any, run: Any, answer: str, conn_args: dict[str, str], detector: Any, code: str
) -> None:
    c = rules(gateway_conn(**conn_args))
    provider = FakeProvider(answer)
    out = await run(c, gateway(provider, detector))
    assert provider.prompts == []
    [row] = ledger(c)
    assert (row[0], row[8], row[9], row[6]) == (stage, "refused", code, Decimal(0))
    assert (out["outcome"], out["refusal_code"], out["fallback"]) == ("refused", code, MANUAL_REPLY)


@pytest.mark.parametrize(("stage", "rules", "run", "answer"), PATHS, ids=IDS)
async def test_a_provider_error_is_one_row_and_falls_back_to_reply_by_hand(
    stage: str, rules: Any, run: Any, answer: str
) -> None:
    c = rules(gateway_conn())
    provider = FakeProvider(answer, error=ProviderCallError("ServiceUnavailableException"))
    out = await run(c, gateway(provider))
    assert len(provider.prompts) == 1
    [row] = ledger(c)
    assert row[0] == stage and row[8] != "ok" and row[10]  # the failure class, never a message
    assert out["outcome"] != "ok" and out["fallback"] == MANUAL_REPLY
