"""AA-1038 R5 slice 3 (offline): the CRM consumer of lead.submitted.v1 (app.tripplanner.leads).

The mapping onto the Stage 1 inquiry (countries -> destination codes, days -> nights, the trip summary as the message,
an optional answer that does not validate dropped, a required one rejecting the lead); another tenant's lead is refused
before anything is read or written; the pull: not configured, per agency with a fake TripPlanner (stored -> ack stored,
duplicate -> ack stored, rejected -> ack rejected with its reason, foreign -> no ack), one agency's HTTP failure does
not stop the others; the result passes the job payload guard; the schedule queues one run per minute slot. Invented
names only.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from app.cis.http import HttpResponse
from app.errors import Forbidden
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job
from app.tripplanner import leads

TENANT_A = uuid.UUID("3e4f5a6b-7c8d-4e9f-a0b1-c2d3e4f5a6b7")
TENANT_B = uuid.UUID("9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d")
TODAY = dt.date(2026, 11, 7)
JOB = Job(id="j-1", kind="crm_tp_lead_pull", payload={"purpose": "slot_schedule"}, attempt=1, max_attempts=1)


def lead(tenant: uuid.UUID = TENANT_A, **over: Any) -> dict[str, Any]:
    lid = str(uuid.uuid4())
    out: dict[str, Any] = {
        "contract": "lead.submitted.v1",
        "version": "1.0",
        "lead_id": lid,
        "idempotency_key": f"tp-lead:{lid}",
        "submitted_at": "2026-11-07T09:00:00Z",
        "tenant": {"tenant_id": str(tenant), "slug": "quillmere-voyages"},
        "source": {"app": "tripplanner", "trip_ref": str(uuid.uuid4())},
        "traveller": {"name": "Ada Fernwick", "email": "ada@example.invalid", "phone": None, "locale": "en"},
        "party": {"adults": 2, "child_ages": [9]},
        "trip": {
            "days": 6,
            "countries": ["Vietnam", "Laos", "Atlantis"],
            "start_date": "2027-03-10",
            "budget_band": "2000_3500",
            "message": "A slow pace, please.",
            "tours": [{"source_system": "aa_master", "source_id": "SYN-1", "title": "Lantern Coast Explorer"}],
            "day_plan": [{"day": 1, "stops": ["Harbour Old Quarter"]}, {"day": 2, "stops": ["Jade Bay"]}],
        },
        "consent": {"contact": True, "marketing": False, "given_at": "2026-11-07T09:00:00Z"},
    }
    out.update(over)
    return out


def test_mapping_onto_the_stage1_inquiry():
    answers = leads.answers_of(lead(), today=TODAY)
    assert answers.destinations == ("vietnam", "laos")
    assert answers.nights == 5 and answers.adults == 2 and answers.child_ages == (9,)
    assert answers.budget_band == "2000_3500" and answers.start_date == "2027-03-10"
    assert answers.name == "Ada Fernwick" and answers.email == "ada@example.invalid"
    assert answers.consent_contact is True and answers.consent_marketing is False
    assert "Lantern Coast Explorer (SYN-1)" in (answers.message or "")
    assert "Day 2: Jade Bay" in (answers.message or "") and "A slow pace, please." in (answers.message or "")


def test_unknown_countries_and_no_budget_become_not_sure():
    trip = {**lead()["trip"], "countries": ["Atlantis"], "budget_band": None, "days": 1}
    answers = leads.answers_of(lead(trip=trip), today=TODAY)
    assert answers.destinations == ("not_sure",) and answers.budget_band == "not_sure" and answers.nights is None


def test_optional_answers_that_do_not_validate_are_dropped():
    past = {**lead()["trip"], "start_date": "2026-11-01"}  # already past on TODAY
    answers = leads.answers_of(lead(trip=past, traveller={**lead()["traveller"], "phone": "not a phone"}), today=TODAY)
    assert answers.start_date is None and answers.phone is None


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"consent": {"contact": False, "marketing": False}}, "consent_missing"),
        ({"traveller": {"name": "", "email": "ada@example.invalid", "phone": None, "locale": "en"}}, "invalid_lead"),
        ({"traveller": {"name": "Ada", "email": "not-an-address", "phone": None, "locale": "en"}}, "invalid_lead"),
    ],
)
def test_required_answers_reject_the_lead(over, reason):
    with pytest.raises(leads.LeadRejected) as e:
        leads.answers_of(lead(**over), today=TODAY)
    assert e.value.reason == reason


def test_a_long_trip_message_is_cut():
    trip = {
        **lead()["trip"],
        "message": "x" * 1000,
        "day_plan": [{"day": d, "stops": [f"Stop {d}" * 5]} for d in range(1, 60)],
    }
    assert len(leads.message_text(lead(trip=trip))) <= 2000


@dataclass
class FakeCtx:
    tenant_id: uuid.UUID


@pytest.mark.asyncio
async def test_another_tenants_lead_is_refused_before_any_read():
    with pytest.raises(leads.ForeignLead):
        await leads.ingest(FakeCtx(TENANT_B), vault=None, lead=lead(TENANT_A), today=TODAY)  # type: ignore[arg-type]
    with pytest.raises(leads.LeadRejected):
        await leads.ingest(FakeCtx(TENANT_A), None, lead(contract="other.v1"), today=TODAY)  # type: ignore[arg-type]
    with pytest.raises(leads.LeadRejected):
        await leads.ingest(FakeCtx(TENANT_A), None, lead(lead_id="nope"), today=TODAY)  # type: ignore[arg-type]


class FakeTokens:
    def __init__(self) -> None:
        self.invalidated: list[str] = []

    async def token(self, scope: str) -> str:
        assert scope == "tripplanner.leads/deliver"
        return "synthetic-token"

    def invalidate(self, scope: str) -> None:
        self.invalidated.append(scope)


class FakeTripPlanner:
    """The two routes of the contract, per tenant; records the acks."""

    def __init__(self, pages: dict[uuid.UUID, list[dict[str, Any]]], fail: set[uuid.UUID] | None = None) -> None:
        self.pages, self.fail, self.acks = pages, fail or set(), []

    async def __call__(self, method, url, headers, body, timeout_s):
        assert headers["Authorization"] == "Bearer synthetic-token"
        parts = url.split("/")
        tenant = uuid.UUID(parts[parts.index("tenants") + 1])
        if tenant in self.fail:
            return HttpResponse(503, b"{}")
        if method == "GET":
            assert url.endswith(f"/leads?limit={leads.PAGE}")
            return HttpResponse(200, json.dumps({"leads": self.pages.get(tenant, []), "more": False}).encode())
        self.acks.append((tenant, parts[-2], json.loads(body)))
        return HttpResponse(200, b'{"state": "delivered", "already": false}')


@dataclass
class Agency:
    tenant_id: uuid.UUID


def _patch(monkeypatch, agencies, outcomes):
    async def fake_agencies(_pool):
        return [Agency(t) for t in agencies]

    @asynccontextmanager
    async def fake_system_tx(_pool, tenant_id, *, actor):
        assert actor == "job:crm_tp_lead_pull"
        if tenant_id in outcomes.get("locked", set()):
            raise Forbidden("tenant_locked")
        yield FakeCtx(tenant_id)

    async def fake_ingest(ctx, vault, item, *, today=None):
        if item["tenant"]["tenant_id"] != str(ctx.tenant_id):
            raise leads.ForeignLead("foreign_lead")
        if item.get("version") == "bad":
            raise leads.LeadRejected("invalid_lead")
        return outcomes.get(item["lead_id"], "stored")

    monkeypatch.setattr(leads.registry, "agencies", fake_agencies)
    monkeypatch.setattr(leads, "system_tx", fake_system_tx)
    monkeypatch.setattr(leads, "ingest", fake_ingest)
    monkeypatch.setattr(leads, "Vault", lambda keys, ctx: None)


@pytest.mark.asyncio
async def test_not_configured():
    assert await leads.LeadPull(None, object())(None, JOB) == {  # type: ignore[arg-type]
        "status": "not_configured"
    }


@pytest.mark.asyncio
async def test_pull_stores_and_acknowledges_per_agency(monkeypatch):
    stored, dup, bad, foreign = lead(), lead(), lead(version="bad"), lead(TENANT_B)
    tp = FakeTripPlanner({TENANT_A: [stored, dup, bad, foreign]}, fail={TENANT_B})
    locked = uuid.uuid4()
    tp.pages[locked] = [lead(locked)]
    _patch(monkeypatch, [TENANT_A, TENANT_B, locked], {dup["lead_id"]: "duplicate", "locked": {locked}})
    cfg = leads.LeadPullConfig(base_url="https://tp.example.invalid", tokens=FakeTokens(), transport=tp)  # type: ignore[arg-type]
    result = await leads.LeadPull(cfg, object(), today=lambda: TODAY)(None, JOB)  # type: ignore[arg-type]
    check_payload(result, what="result")
    assert result["agency_count"] == 3 and result["stored_count"] == 1 and result["duplicate_count"] == 1
    assert result["rejected_count"] == 1 and result["foreign_count"] == 1 and result["acknowledged_count"] == 2
    assert result["skipped_agency_count"] == 1
    assert result["error_count"] == 1 and result["reason_code"] == "tripplanner_http_503"
    assert result["status"] == "partial"
    acks = {(str(t), lid): body for t, lid, body in tp.acks}
    assert acks[(str(TENANT_A), stored["lead_id"])] == {"outcome": "stored", "reason": None}
    assert acks[(str(TENANT_A), dup["lead_id"])] == {"outcome": "stored", "reason": None}
    assert acks[(str(TENANT_A), bad["lead_id"])] == {"outcome": "rejected", "reason": "invalid_lead"}
    assert (str(TENANT_A), foreign["lead_id"]) not in acks  # never acknowledged under the wrong tenant


@pytest.mark.asyncio
async def test_a_401_drops_the_cached_token(monkeypatch):
    tokens = FakeTokens()

    async def unauthorized(method, url, headers, body, timeout_s):
        return HttpResponse(401, b"{}")

    cfg = leads.LeadPullConfig(base_url="https://tp.example.invalid", tokens=tokens, transport=unauthorized)  # type: ignore[arg-type]
    with pytest.raises(leads.HttpError) as e:
        await leads.fetch_leads(cfg, TENANT_A)
    assert e.value.error_class == "tripplanner_http_401" and tokens.invalidated == ["tripplanner.leads/deliver"]

    async def garbage(method, url, headers, body, timeout_s):
        return HttpResponse(200, b"not json")

    with pytest.raises(leads.HttpError):
        await leads.fetch_leads(leads.LeadPullConfig("https://tp.example.invalid", tokens, garbage), TENANT_A)  # type: ignore[arg-type]

    async def no_list(method, url, headers, body, timeout_s):
        return HttpResponse(200, b'{"leads": "x"}')

    with pytest.raises(leads.HttpError):
        await leads.fetch_leads(leads.LeadPullConfig("https://tp.example.invalid", tokens, no_list), TENANT_A)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_one_agency_error_does_not_stop_the_others(monkeypatch):
    tp = FakeTripPlanner({TENANT_B: [lead(TENANT_B)]})
    _patch(monkeypatch, [TENANT_A, TENANT_B], {})

    async def boom(ctx, vault, item, *, today=None):
        if ctx.tenant_id == TENANT_A:
            raise RuntimeError("synthetic")
        return "stored"

    tp.pages[TENANT_A] = [lead(TENANT_A)]
    monkeypatch.setattr(leads, "ingest", boom)
    cfg = leads.LeadPullConfig(base_url="https://tp.example.invalid", tokens=FakeTokens(), transport=tp)  # type: ignore[arg-type]
    result = await leads.LeadPull(cfg, object())(None, JOB)  # type: ignore[arg-type]
    assert result["stored_count"] == 1 and result["reason_code"] == "RuntimeError"


@pytest.mark.asyncio
async def test_the_schedule_queues_one_run_per_slot(monkeypatch):
    queued: list[tuple[str, str]] = []

    async def enqueue(pool, kind, payload, *, idempotency_key, max_attempts, created_by):
        queued.append((kind, idempotency_key))
        return uuid.uuid4()

    async def prune(pool, kind, days):
        return 0

    monkeypatch.setattr(leads.queue, "enqueue", enqueue)
    monkeypatch.setattr(leads.queue, "prune", prune)
    now = dt.datetime(2026, 11, 7, 9, 0, 30, tzinfo=dt.UTC)
    hook = leads.LeadPullSchedule(lambda: now)
    assert await hook(None) is not None  # type: ignore[arg-type]
    assert await hook(None) is None  # type: ignore[arg-type]
    assert queued == [("crm_tp_lead_pull", leads.schedule_key(now))]
    assert leads.schedule_key(now) == f"crm_tp_lead_pull:slot:{int(now.timestamp()) // 60}"


def test_counter_keys_are_payload_safe():
    check_payload({f"{k}_count": 1 for k in Counter(["stored", "duplicate", "rejected", "foreign"])}, what="result")


def test_tripplanner_api_url_is_an_origin_and_configures_the_pull() -> None:
    from app.main import build_tp_leads
    from app.settings import SettingsError, load_settings

    ok = load_settings({"ENVIRONMENT": "test", "TRIPPLANNER_API_URL": "https://TP-Api.example.test/"})
    assert ok.tripplanner_api_url == "https://tp-api.example.test"
    for bad in ("https://tp-api.example.test/trip", "http://tp-api.example.test"):
        with pytest.raises(SettingsError):
            load_settings({"ENVIRONMENT": "dev", "TRIPPLANNER_API_URL": bad})
    assert build_tp_leads(ok, None) is None  # no M2M client: not configured
    assert build_tp_leads(load_settings({"ENVIRONMENT": "test"}), FakeTokens()) is None  # type: ignore[arg-type]
    cfg = build_tp_leads(ok, FakeTokens())  # type: ignore[arg-type]
    assert cfg is not None and cfg.base_url == "https://tp-api.example.test"
