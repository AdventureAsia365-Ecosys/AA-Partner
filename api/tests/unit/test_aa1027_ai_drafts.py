"""AA-1027 slice F3 (gap CRM-4; design v3 M14, R39): the AI drafts of the trip workspace — the plan's ``ai_assist``
entitlement in front of the draft endpoint, one ``crm.ai_draft`` row per answered draft (placeholders kept, under the
vault DEK), and the member's one accept / discard decision with a feedback code. Fixtures use invented names only.
"""

from __future__ import annotations

import datetime as dt
import typing
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.llm import agency_ai, ai_drafts
from app.llm import gateway as gateway_module
from app.main import create_app
from app.routers import ai as ai_router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, OTHER_MEMBER, TENANT, FakeVault, fake_tx, tenant_ctx

pytestmark = pytest.mark.asyncio

CALL_ID = "00000000-0000-4000-8000-000000000001"
MAP_ID = "00000000-0000-4000-8000-0000000000e1"
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
DRAFT = uuid.UUID("00000000-0000-4000-8000-0000000000f1")
DECIDED_AT = dt.datetime(2026, 11, 2, 9, 30, tzinfo=dt.UTC)


def draft_conn(*, entitled: bool = True, enabled: bool = True) -> ScriptedConn:
    return (
        ScriptedConn()
        .on("FROM crm.tenant_entitlement", entitled)
        .on(
            "SELECT ai_enabled, ai_budget_usd, ai_alert_month",
            {"ai_enabled": enabled, "ai_budget_usd": Decimal(2), "ai_alert_month": None, "ai_alert_pct": None},
        )
        .on("SELECT 1 FROM crm.trip", 1)
    )


class ScriptedGateway:
    """The gateway's answer without a scrub or a provider; records that it was called."""

    def __init__(self, result: gateway_module.GatewayResult) -> None:
        self.result = result
        self.calls = 0

    async def call(self, *_: Any, **__: Any) -> gateway_module.GatewayResult:
        self.calls += 1
        return self.result


def ok_gateway(text: str = "Dear <PERSON_1>, the day in Fakeville is free.") -> ScriptedGateway:
    return ScriptedGateway(
        gateway_module.GatewayResult(outcome="ok", text=text, llm_call_id=CALL_ID, placeholder_map_id=MAP_ID)
    )


async def _restore(_ctx: Any, _vault: Any, _map_id: uuid.UUID, text: str) -> str:
    return text.replace("<PERSON_1>", "Odile Brank")


# ── Entitlement ─────────────────────────────────────────────────────────────────────────────────


async def test_an_agency_without_ai_assist_gets_403_before_anything_is_read_or_paid() -> None:
    c = draft_conn(entitled=False, enabled=False)  # refused on the plan first, not on the switch
    ctx = tenant_ctx(c, role="agent")
    gateway = ok_gateway()
    with pytest.raises(Forbidden) as refused:
        await agency_ai.draft(ctx, gateway, FakeVault(), TRIP, kind="reply", text="Is the boat ride on day two?")
    assert refused.value.code == "feature_not_allowed" and refused.value.status_code == 403
    assert gateway.calls == 0 and c.sql("INSERT INTO crm.ai_draft") == []
    [event] = ctx.audit_events
    assert (event.action, event.outcome, event.object_kind, event.reason) == (
        "ai_use", "denied", "trip", "feature_not_allowed",
    )  # fmt: skip


async def test_the_entitlement_reads_the_plan_row_and_a_missing_row_is_no() -> None:
    assert await ai_drafts.ai_assist_allowed(tenant_ctx(ScriptedConn().on("FROM crm.tenant_entitlement", True)))
    assert not await ai_drafts.ai_assist_allowed(tenant_ctx(ScriptedConn()))  # coalesce(..., false) -> None in fakes
    c = ScriptedConn().on("FROM crm.tenant_entitlement", True)
    await ai_drafts.ai_assist_allowed(tenant_ctx(c))
    assert c.sql("FROM crm.tenant_entitlement") == [("ai_assist",)]


async def test_the_settings_view_tells_the_web_whether_the_plan_has_the_assistant() -> None:
    c = draft_conn(entitled=True).on("sum(cost_usd_est)", Decimal(0))
    view = await agency_ai.ai_settings(tenant_ctx(c), budget_max=Decimal(2), provider="stub", rate_per_minute=6)
    assert view["ai_assist_allowed"] is True and view["ai_enabled"] is True


# ── Storing a draft ─────────────────────────────────────────────────────────────────────────────


async def test_an_answered_draft_is_one_pending_row_with_its_placeholders_under_the_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agency_ai, "restore_placeholders", _restore)
    c = draft_conn()
    ctx = tenant_ctx(c)
    out = await agency_ai.draft(ctx, ok_gateway(), FakeVault(), TRIP, kind="follow_up", text="Any news?")
    [row] = c.sql("INSERT INTO crm.ai_draft")
    draft_id, trip_id, kind, call_id, version, blob = row
    assert (trip_id, kind, str(call_id), version) == (TRIP, "follow_up", CALL_ID, agency_ai.PROMPT_VERSION)
    # Stored as the model wrote it (placeholder kept), encrypted; the member's answer is restored.
    assert blob == b"enc:ai_draft.output_enc:Dear <PERSON_1>, the day in Fakeville is free."
    assert out["text"] == "Dear Odile Brank, the day in Fakeville is free."
    assert out["ai_draft_id"] == str(draft_id) and out["approval_required"] is True
    created = [e for e in ctx.audit_events if e.object_kind == "ai_draft"]
    assert [(e.action, e.object_id, e.object_ids) for e in created] == [("create", str(draft_id), (str(TRIP),))]


@pytest.mark.parametrize(
    "result",
    [
        gateway_module.GatewayResult(outcome="refused", refusal_code="budget", llm_call_id=CALL_ID),
        gateway_module.GatewayResult(outcome="error", error_class="other", llm_call_id=CALL_ID),
        gateway_module.GatewayResult(outcome="truncated", llm_call_id=CALL_ID, truncated=True),
    ],
)
async def test_no_row_without_a_usable_answer(result: gateway_module.GatewayResult) -> None:
    c = draft_conn()
    out = await agency_ai.draft(tenant_ctx(c), ScriptedGateway(result), FakeVault(), TRIP, kind="reply", text="Hi")
    assert c.sql("INSERT INTO crm.ai_draft") == [] and out["ai_draft_id"] is None and out["text"] is None


class SavepointConn(ScriptedConn):
    """Records how each ``transaction()`` (a savepoint inside ``tenant_tx``) ended: released or the error class."""

    def __init__(self, rules: list[tuple[str, Any]]) -> None:
        super().__init__(rules)
        self.savepoints: list[str] = []

    @asynccontextmanager
    async def transaction(self, **_: Any) -> AsyncIterator[SavepointConn]:
        try:
            yield self
        except BaseException as exc:
            self.savepoints.append(type(exc).__name__)
            raise
        self.savepoints.append("released")


class KmsUnavailable(Exception):
    pass


class KmsDownVault(FakeVault):
    async def encrypt(self, *_: Any, **__: Any) -> bytes:
        raise KmsUnavailable("no data key")


class CheckViolation(Exception):
    pass


async def test_a_draft_row_that_cannot_be_stored_keeps_the_paid_call_and_the_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The model has answered and the gateway logged the call: a vault (KMS) failure rolls back the draft's savepoint
    # only, never the tenant transaction that holds the ledger row; the member still gets the restored text.
    monkeypatch.setattr(agency_ai, "restore_placeholders", _restore)
    c = SavepointConn(draft_conn().rules)
    ctx = tenant_ctx(c)
    out = await agency_ai.draft(ctx, ok_gateway(), KmsDownVault(), TRIP, kind="reply", text="Is day two free?")
    assert c.savepoints == ["KmsUnavailable"] and c.sql("INSERT INTO crm.ai_draft") == []
    assert (out["outcome"], out["ai_draft_id"], out["llm_call_id"]) == ("ok", None, CALL_ID)
    assert out["text"] == "Dear Odile Brank, the day in Fakeville is free."
    assert [e for e in ctx.audit_events if e.object_kind == "ai_draft"] == []


async def test_a_refused_insert_is_contained_in_its_savepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agency_ai, "restore_placeholders", _restore)
    c = SavepointConn(draft_conn().on("INSERT INTO crm.ai_draft", CheckViolation("ai_draft_enc_chk")).rules)
    out = await agency_ai.draft(tenant_ctx(c), ok_gateway(), FakeVault(), TRIP, kind="reply", text="Hi")
    assert c.savepoints == ["CheckViolation"] and out["ai_draft_id"] is None and out["text"].startswith("Dear Odile")


async def test_a_stored_draft_releases_its_savepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agency_ai, "restore_placeholders", _restore)
    c = SavepointConn(draft_conn().rules)
    out = await agency_ai.draft(tenant_ctx(c), ok_gateway(), FakeVault(), TRIP, kind="reply", text="Hi")
    assert c.savepoints == ["released"] and out["ai_draft_id"]


# ── The decision ────────────────────────────────────────────────────────────────────────────────


def decide_conn(decision: str = "pending", owner: uuid.UUID | None = MEMBER, updated: bool = True) -> ScriptedConn:
    def update(draft_id: uuid.UUID, new: str, feedback: str | None) -> dict[str, Any] | None:
        if not updated:
            return None
        return {"ai_draft_id": draft_id, "trip_id": TRIP, "decision": new, "feedback_code": feedback,
                "decided_at": DECIDED_AT}  # fmt: skip

    return (
        ScriptedConn()
        .on(
            "FROM crm.ai_draft d JOIN crm.trip t",
            {"ai_draft_id": DRAFT, "trip_id": TRIP, "decision": decision, "owner_member_id": owner},
        )
        .on("UPDATE crm.ai_draft", update)
    )


async def test_an_agent_accepts_a_pending_draft_once_with_feedback() -> None:
    c = decide_conn(owner=OTHER_MEMBER)  # an agent may accept on any trip
    ctx = tenant_ctx(c, role="agent")
    out = await ai_drafts.decide(ctx, DRAFT, action="accept", feedback_code="good")
    assert out == {
        "ai_draft_id": str(DRAFT),
        "trip_id": str(TRIP),
        "decision": "accepted",
        "feedback_code": "good",
        "decided_at": DECIDED_AT.isoformat(),
    }
    assert c.sql("UPDATE crm.ai_draft") == [(DRAFT, "accepted", "good")]
    [event] = ctx.audit_events
    assert (event.action, event.outcome, event.reason, event.object_ids) == (
        "ai_draft_accept", "allowed", "good", (str(TRIP),),
    )  # fmt: skip


async def test_an_assistant_accepts_only_on_an_assigned_trip_but_may_discard_any() -> None:
    with pytest.raises(Forbidden) as refused:
        await ai_drafts.decide(tenant_ctx(decide_conn(owner=OTHER_MEMBER), role="assistant"), DRAFT, action="accept")
    assert refused.value.code == "assigned_only"
    mine = await ai_drafts.decide(tenant_ctx(decide_conn(owner=MEMBER), role="assistant"), DRAFT, action="accept")
    assert mine["decision"] == "accepted"
    unassigned = decide_conn(owner=None)
    out = await ai_drafts.decide(
        tenant_ctx(unassigned, role="assistant"), DRAFT, action="discard", feedback_code="wrong_tone"
    )
    assert out["decision"] == "discarded" and unassigned.sql("UPDATE crm.ai_draft") == [
        (DRAFT, "discarded", "wrong_tone")
    ]


async def test_a_decided_missing_or_raced_draft_is_refused() -> None:
    with pytest.raises(Conflict) as decided:
        await ai_drafts.decide(tenant_ctx(decide_conn(decision="discarded")), DRAFT, action="accept")
    assert decided.value.code == "draft_decided"
    with pytest.raises(NotFound):
        await ai_drafts.decide(tenant_ctx(ScriptedConn()), DRAFT, action="discard")
    with pytest.raises(Conflict):  # another request decided between the read and the update
        await ai_drafts.decide(tenant_ctx(decide_conn(updated=False)), DRAFT, action="discard")
    with pytest.raises(Invalid) as bad_code:
        await ai_drafts.decide(tenant_ctx(decide_conn()), DRAFT, action="discard", feedback_code="meh")
    assert bad_code.value.code == "invalid_feedback_code"
    with pytest.raises(Invalid):
        await ai_drafts.decide(tenant_ctx(decide_conn()), DRAFT, action="send")


async def test_the_route_feedback_codes_are_the_service_list() -> None:
    literal = ai_router.DecisionIn.model_fields["feedback_code"].annotation
    [codes, _none] = typing.get_args(literal)
    assert typing.get_args(codes) == ai_drafts.FEEDBACK_CODES


# ── Routes ──────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(
    pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str, c: ScriptedConn
) -> Iterator[tuple[TestClient, dict]]:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    monkeypatch.setattr(ai_router, "request_pool", _pool)
    monkeypatch.setattr(ai_router, "tenant_tx", fake_tx(tenant_ctx(c, role=role)))
    monkeypatch.setattr(ai_router, "Vault", lambda keys, ctx: FakeVault())
    monkeypatch.setattr(ai_router, "require_keys", lambda keys: keys)
    headers = {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}
    with TestClient(app) as client:
        yield client, headers


@pytest.fixture
def unentitled(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, dict]]:
    yield from _client(pools, monkeypatch, "owner", draft_conn(entitled=False))


@pytest.fixture
def deciding(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, dict]]:
    yield from _client(pools, monkeypatch, "agent", decide_conn())


async def test_routes_a_draft_without_the_plan_is_403(unentitled: tuple[TestClient, dict]) -> None:
    client, headers = unentitled
    res = client.post(f"/crm/v1/trips/{TRIP}/ai/draft", headers=headers, json={"text": "Is breakfast included?"})
    assert res.status_code == 403 and res.json()["detail"] == "feature_not_allowed"


async def test_routes_accept_and_discard(deciding: tuple[TestClient, dict]) -> None:
    client, headers = deciding
    ok = client.post(f"/crm/v1/ai/drafts/{DRAFT}/accept", headers=headers)
    assert ok.status_code == 200 and ok.json()["decision"] == "accepted" and ok.json()["feedback_code"] is None
    with_code = client.post(
        f"/crm/v1/ai/drafts/{DRAFT}/discard", headers=headers, json={"feedback_code": "not_relevant"}
    )
    assert with_code.status_code == 200 and with_code.json()["feedback_code"] == "not_relevant"
    bad = client.post(f"/crm/v1/ai/drafts/{DRAFT}/discard", headers=headers, json={"feedback_code": "x"})
    assert bad.status_code == 422
    assert client.post(f"/crm/v1/ai/drafts/{DRAFT}/send", headers=headers).status_code == 422
    assert client.post("/crm/v1/ai/drafts/not-a-uuid/accept", headers=headers).status_code == 422
