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
import unicodedata
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.cis.http import HttpResponse
from app.errors import Forbidden, NotFound
from app.intake import stage1
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job
from app.observability import ops_alerts
from app.tripplanner import leads
from app.vault import blind_index

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
    assert "Phone as typed (not checked): not a phone" in (answers.message or "")


# TripPlanner's Traveller.phone rule (AA-TripPlanner-Web backend/assembly/leads.py _PHONE_RE = the contract pattern).
TP_PHONE_PATTERN = r"^\+[1-9][0-9 ()-]{5,39}$"


@pytest.mark.parametrize(
    "phone",
    ["+84 90 000 0000", "+44 (20) 7946-0000", "+1-202-555-0100", "+8490000000", "+999 1234 5678", "+1 2345"],
)
def test_a_phone_tripplanner_accepts_is_never_lost(phone):
    """Cross-repo rule: every phone the producer accepts reaches the agent, normalised or as typed."""
    import re

    assert re.match(TP_PHONE_PATTERN, phone)
    answers = leads.answers_of(lead(traveller={**lead()["traveller"], "phone": phone}), today=TODAY)
    if answers.phone is not None:
        assert answers.phone.startswith("+") and answers.phone[1:].isdigit()
        assert "Phone as typed" not in (answers.message or "")
    else:
        assert f"Phone as typed (not checked): {phone}" in (answers.message or "")
    assert answers.name == "Ada Fernwick"


def test_a_normalised_phone_is_e164():
    answers = leads.answers_of(lead(traveller={**lead()["traveller"], "phone": "+84 90 000 0000"}), today=TODAY)
    assert answers.phone == "+84900000000"


def test_an_unchecked_phone_survives_a_long_message():
    trip = {
        **lead()["trip"],
        "message": "x" * 1000,
        "day_plan": [{"day": d, "stops": [f"Stop {d}" * 5]} for d in range(1, 60)],
    }
    text = leads.message_text(lead(trip=trip), unchecked_phone="+999 1234 5678")
    assert len(text) <= 2000 and "Phone as typed (not checked): +999 1234 5678" in text


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


WHEELCHAIR = "One of us uses a wheelchair; please plan step-free hotels and call us on WhatsApp after 6 pm."


@pytest.mark.parametrize("phone", [None, "+84 912 345 678"])
def test_review_repro_a_long_catalog_never_cuts_the_travellers_message(phone):
    """S272 review: the traveller's message came LAST, after the Tour/Day lines, and the cut to 2000 took it away for
    good on a lead well inside the contract (4 tours, 14 days x 5 stops). The traveller's part now comes first."""
    trip = {
        **lead()["trip"],
        "days": 14,
        "message": WHEELCHAIR,
        "tours": [
            {"source_id": f"SYN-{i}", "title": "Northern Vietnam Highlights and Halong Bay Cruise i"} for i in range(4)
        ],
        "day_plan": [{"day": d, "stops": [f"Hanoi Old Quarter Stop {s:02d}" for s in range(5)]} for d in range(1, 15)],
    }
    answers = leads.answers_of(lead(trip=trip, traveller={**lead()["traveller"], "phone": phone}), today=TODAY)
    message = answers.message or ""
    assert f"Message from the traveller:\n{WHEELCHAIR}" in message
    assert message.index("Message from the traveller:") < message.index("Tour: ")
    assert len(message) <= stage1.MAX_MESSAGE and message.endswith("\u2026")


def test_the_largest_catalog_the_contract_allows_keeps_the_whole_message_and_the_phone():
    """Contract maxima: 12 countries x 80, 30 tours x 300-char titles, 90 days x 12 stops x 120 chars, a 1000-char
    message, and a contract E.164 phone that phonenumbers rejects (kept as text). Only catalog lines are cut."""
    own = ("Step-free rooms please. " * 50)[:999] + "!"
    trip = {
        **lead()["trip"],
        "days": 90,
        "countries": [f"Country {i:02d} " + "n" * 69 for i in range(12)],
        "message": own,
        "tours": [{"source_id": f"SYN-{i}", "title": f"T{i:02d}" + "t" * 297} for i in range(30)],
        "day_plan": [{"day": d, "stops": [f"S{d}-{s}-" + "s" * 112 for s in range(12)]} for d in range(1, 91)],
    }
    assert len(own) == 1000 and all(len(c) == 80 for c in trip["countries"])
    traveller = {**lead()["traveller"], "phone": "+999 1234 5678"}
    answers = leads.answers_of(lead(trip=trip, traveller=traveller), today=TODAY)
    message = answers.message or ""
    assert answers.phone is None
    assert "Phone as typed (not checked): +999 1234 5678" in message
    assert f"Message from the traveller:\n{own}" in message
    assert len(message) <= stage1.MAX_MESSAGE and stage1.clean_text(message, stage1.MAX_MESSAGE) == message


def test_a_message_beyond_the_contract_is_cut_but_the_phone_and_header_stay():
    trip = {**lead()["trip"], "message": "w" * 5000}
    text = leads.message_text(lead(trip=trip), unchecked_phone="+999 1234 5678")
    assert len(text) <= stage1.MAX_MESSAGE and text.startswith("Trip planner request: 6 day(s)")
    assert "Phone as typed (not checked): +999 1234 5678" in text and text.endswith("w\u2026")


# Code points whose NFC form is longer (composition exclusions that decompose): Devanagari U+0958..U+095F, Tibetan
# U+0F73, Hebrew presentation forms U+FB1D..U+FB4E, combining U+0344.
NFC_LENGTHENING = ["\u0958", "\u095f", "\u0f73", "\ufb2c", "\ufb4e", "\u0344"]


@pytest.mark.parametrize("char", NFC_LENGTHENING)
def test_review_repro_an_nfc_lengthening_stop_name_keeps_the_message_and_the_phone(char):
    """S272 follow-up: message_text cut at 2000 code points BEFORE Stage 1's NFC, so a stop name whose NFC form is
    longer pushed the message past 2000, validate dropped it, the phone retry rebuilt it, the second round dropped it
    again and the lead was rejected (invalid_lead). Now the text is cleaned with Stage 1's own clean_text first and cut
    after; the lead is stored with its message and the phone line."""
    assert len(unicodedata.normalize("NFC", char)) > 1
    trip = {**lead()["trip"], "day_plan": [{"day": d, "stops": [f"Ridge {char * 30}"]} for d in range(1, 60)]}
    traveller = {**lead()["traveller"], "phone": "+999 1234 5678"}  # contract E.164, phonenumbers rejects it
    answers = leads.answers_of(lead(trip=trip, traveller=traveller), today=TODAY)
    message = answers.message or ""
    assert answers.phone is None and answers.name == "Ada Fernwick"
    assert "Phone as typed (not checked): +999 1234 5678" in message
    assert "Lantern Coast Explorer (SYN-1)" in message and message.endswith("\u2026")
    assert len(message) <= stage1.MAX_MESSAGE
    assert stage1.clean_text(message, stage1.MAX_MESSAGE) == message  # what Stage 1 stores is what was built


@pytest.mark.parametrize("char", NFC_LENGTHENING)
def test_message_text_is_already_stage1_clean_and_within_the_limit(char):
    trip = {
        **lead()["trip"],
        "message": f"{char}\x0b{char}" * 900,
        "tours": [{"source_id": "SYN-2", "title": char * 50}],
    }
    text = leads.message_text(lead(trip=trip), unchecked_phone="+999 1234 5678")
    assert len(text) <= stage1.MAX_MESSAGE and stage1.clean_text(text, stage1.MAX_MESSAGE) == text
    assert "\x0b" not in text and unicodedata.is_normalized("NFC", text)
    assert "Phone as typed (not checked): +999 1234 5678" in text


def test_a_short_message_is_cleaned_like_stage1_not_cut():
    trip = {**lead()["trip"], "message": "  Ka\u0958ri  ", "day_plan": [], "tours": []}
    text = leads.message_text(lead(trip=trip))
    assert text.endswith("Ka\u0915\u093cri") and not text.endswith("\u2026")


def test_the_phone_retry_never_rebuilds_a_dropped_message(monkeypatch):
    """A message Stage 1 refuses is dropped once and stays dropped: the phone retry does not write the built message
    back (which made the next round refuse it again and reject the whole lead). The phone still survives: when the
    message and the phone are refused in the same round, the message becomes the phone line alone (S272 review)."""
    real = stage1.validate
    seen: list[Any] = []

    def validate(raw: dict[str, Any], **kw: Any) -> Any:
        message = raw.get("message")
        seen.append(message)
        if message is not None and "Trip planner request" in message:
            raise stage1.Stage1Invalid(["message", "phone"] if raw.get("phone") else ["message"])
        return real(raw, **kw)

    monkeypatch.setattr(stage1, "validate", validate)
    answers = leads.answers_of(lead(traveller={**lead()["traveller"], "phone": "+999 1234 5678"}), today=TODAY)
    assert answers.phone is None and len(seen) == 2
    assert answers.message == "Phone as typed (not checked): +999 1234 5678"
    assert all("Trip planner request" not in (m or "") for m in seen[1:])  # the refused message is never rebuilt


def test_a_phone_refused_after_the_message_was_dropped_still_survives(monkeypatch):
    """The message is refused alone first (dropped), then the phone in a later round: the phone becomes the message."""
    real = stage1.validate
    rounds = iter([["message"], ["phone"]])

    def validate(raw: dict[str, Any], **kw: Any) -> Any:
        fields = next(rounds, None)
        if fields is not None:
            raise stage1.Stage1Invalid(fields)
        return real(raw, **kw)

    monkeypatch.setattr(stage1, "validate", validate)
    monkeypatch.setattr(leads, "traveller_errors", lambda _t: [])
    answers = leads.answers_of(lead(traveller={**lead()["traveller"], "phone": "+84 90 000 0000"}), today=TODAY)
    assert answers.phone is None and answers.message is not None
    assert answers.message.startswith("Phone as typed (not checked): ") and "Trip planner" not in answers.message


def test_every_optional_field_failing_one_by_one_still_stores_the_lead(monkeypatch):
    """The retry loop runs until every optional answer could be dropped (one per round), not a fixed two rounds."""
    real = stage1.validate
    order = ["start_date", "nights", "child_ages", "locale", "message"]

    def validate(raw: dict[str, Any], **kw: Any) -> Any:
        for name in order:
            if raw.get(name) not in (None, []):
                raise stage1.Stage1Invalid([name])
        return real(raw, **kw)

    monkeypatch.setattr(stage1, "validate", validate)
    answers = leads.answers_of(lead(), today=TODAY)
    assert answers.name == "Ada Fernwick" and answers.start_date is None and answers.message is None
    assert answers.nights is None and answers.child_ages == ()


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
        if tenant_id in outcomes.get("gone", set()):
            raise NotFound("not_a_member")
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


# --- S272 review fixes: one Traveller rule on both sides, a rejected lead alerts, a locked agency is never listed ----

RULES_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "contracts" / "rules" / "lead.submitted.v1.traveller-rules.json"
)
RULES = json.loads(RULES_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", RULES["cases"], ids=lambda c: f"{c['field']}-{c['id']}")
def test_traveller_rule_shared_vectors(case):
    """The same vectors TripPlanner's validate() runs (backend/tests/test_aa1038_lead_review.py)."""
    traveller = {**lead()["traveller"], case["field"]: case["value"]}
    assert (leads.traveller_errors(traveller) == []) is case["valid"]
    if not case["valid"]:
        assert leads.traveller_errors(traveller) == [case["field"]]


@pytest.mark.parametrize(
    "email",
    [
        "\u0130" * 33 + "@example.com",
        "a" * 60 + "\u0130" * 4 + "@example.com",
        "a" * 62 + "\u0130" * 2 + "@" + "b" * 181 + ".example",
    ],
)
def test_review_repro_an_email_lower_casing_lengthens_is_checked_lower_case(email):
    """S272 review 4: the verdict is the producer's (lower-case first, then length and pattern), so the address
    TripPlanner now refuses is the one the CRM would reject, and its lower-case form is rejected too."""
    traveller = {**lead()["traveller"], "email": email}
    assert leads.traveller_errors(traveller) == ["email"]
    assert leads.traveller_errors({**traveller, "email": email.lower()}) == ["email"]


def test_a_dotted_capital_i_email_within_the_limits_is_valid():
    assert leads.traveller_errors({**lead()["traveller"], "email": "\u0130\u0130@Example.Invalid"}) == []


def test_the_rule_constants_are_the_shared_ones():
    assert leads.TRAVELLER_EMAIL.pattern == RULES["email_pattern"]
    assert leads.TRAVELLER_PHONE.pattern == RULES["phone_pattern"]
    assert leads.TRAVELLER_CONTROL.pattern == RULES["control_characters"]
    assert leads.TRAVELLER_MAX == RULES["max_length"]


@pytest.mark.parametrize(
    "case",
    [c for c in RULES["cases"] if c["valid"] and c["field"] in ("name", "email")],
    ids=lambda c: f"{c['field']}-{c['id']}",
)
def test_a_traveller_tripplanner_accepts_is_never_rejected(case):
    """Review repro: a name NFC lengthens past 120 passed TripPlanner and was rejected here (the lead was lost)."""
    traveller = {**lead()["traveller"], case["field"]: case["value"]}
    answers = leads.answers_of(lead(traveller=traveller), today=TODAY)
    assert answers.name and answers.email


@pytest.mark.parametrize(
    "case",
    [c for c in RULES["cases"] if not c["valid"] and c["field"] in ("name", "email")],
    ids=lambda c: f"{c['field']}-{c['id']}",
)
def test_a_traveller_tripplanner_refuses_is_rejected_here_too(case):
    traveller = {**lead()["traveller"], case["field"]: case["value"]}
    with pytest.raises(leads.LeadRejected) as e:
        leads.answers_of(lead(traveller=traveller), today=TODAY)
    assert e.value.reason == "invalid_lead"


def test_a_phone_outside_the_rule_is_kept_as_text_not_rejected():
    answers = leads.answers_of(lead(traveller={**lead()["traveller"], "phone": "0912 345 678"}), today=TODAY)
    assert answers.phone is None and "Phone as typed (not checked): 0912 345 678" in (answers.message or "")


@pytest.mark.asyncio
async def test_review_repro_a_rejected_lead_raises_an_ops_alert_without_personal_data(monkeypatch):
    bad = lead(version="bad")
    tp = FakeTripPlanner({TENANT_A: [bad]})
    _patch(monkeypatch, [TENANT_A], {})
    raised: list[tuple[str, Any, dict[str, Any]]] = []
    monkeypatch.setattr(ops_alerts, "emit", lambda check, scope=None, **d: raised.append((check, scope, d)))
    cfg = leads.LeadPullConfig(base_url="https://tp.example.invalid", tokens=FakeTokens(), transport=tp)  # type: ignore[arg-type]
    result = await leads.LeadPull(cfg, object(), today=lambda: TODAY)(None, JOB)  # type: ignore[arg-type]
    assert result["rejected_count"] == 1
    assert raised == [
        (
            "tp_lead_rejected",
            bad["lead_id"],
            {"tenant_id": TENANT_A, "lead_id": bad["lead_id"], "reason": "invalid_lead"},
        )
    ]
    assert "Ada" not in repr(raised) and "example.invalid" not in repr(raised)
    assert "tp_lead_rejected" in ops_alerts.EVENT_CHECKS


def test_a_lead_ref_never_echoes_a_payload_value():
    assert leads._lead_ref("Ada Fernwick <ada@example.invalid>") == "invalid"  # noqa: SLF001


@pytest.mark.asyncio
async def test_review_repro_a_locked_or_unknown_agency_is_never_listed(monkeypatch):
    """TripPlanner's per-tenant beacon is the list call: a locked agency must not refresh it."""
    locked, gone = uuid.uuid4(), uuid.uuid4()
    tp = FakeTripPlanner({TENANT_A: [lead()], locked: [lead(locked)], gone: [lead(gone)]})
    listed: list[uuid.UUID] = []
    inner = tp.__call__

    async def recording(method, url, headers, body, timeout_s):
        parts = url.split("/")
        if method == "GET":
            listed.append(uuid.UUID(parts[parts.index("tenants") + 1]))
        return await inner(method, url, headers, body, timeout_s)

    _patch(monkeypatch, [TENANT_A, locked, gone], {"locked": {locked}, "gone": {gone}})
    cfg = leads.LeadPullConfig(base_url="https://tp.example.invalid", tokens=FakeTokens(), transport=recording)  # type: ignore[arg-type]
    result = await leads.LeadPull(cfg, object(), today=lambda: TODAY)(None, JOB)  # type: ignore[arg-type]
    assert listed == [TENANT_A] and result["skipped_agency_count"] == 2 and result["stored_count"] == 1


# --- S272 review 2: a name the intake cannot index is rejected at the pull, never 'stored' then lost --------------

NAME_CASES = [c for c in RULES["cases"] if c["field"] == "name"]


@pytest.mark.parametrize("case", NAME_CASES, ids=lambda c: c["id"])
def test_the_name_rule_is_what_the_intake_job_needs(case):
    """Every name the rule accepts is one crm_intake_process can index (vault.name_index -> blind_index.name_tokens),
    and every name it refuses that survives the cleaning is one the intake could not index."""
    traveller = {**lead()["traveller"], "name": case["value"]}
    if case["valid"]:
        answers = leads.answers_of(lead(traveller=traveller), today=TODAY)
        assert blind_index.normalise_name(answers.name)  # what process_submission indexes; raises when it cannot
        assert blind_index.name_token_indexes(b"k" * 32, answers.name)
        return
    cleaned = None
    try:
        cleaned = stage1.clean_text(case["value"], stage1.MAX_NAME)
    except ValueError:
        pass
    if cleaned:
        with pytest.raises(blind_index.NormalisationError):
            blind_index.name_tokens(cleaned)


@pytest.mark.parametrize("field", ["email", "phone"])
def test_no_other_traveller_field_passes_the_pull_and_fails_the_intake(field):
    """The intake also indexes the e-mail and the phone (process_submission): every value the rule accepts reaches it
    in a form its index functions take (a phone the CRM cannot normalise is dropped to the message, never indexed)."""
    for case in [c for c in RULES["cases"] if c["field"] == field and c["valid"]]:
        answers = leads.answers_of(lead(traveller={**lead()["traveller"], field: case["value"]}), today=TODAY)
        assert blind_index.normalise_email(answers.email)
        if answers.phone is not None:
            assert blind_index.normalise_phone(answers.phone)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["-", "...", "—", "\U0001f334"])
async def test_review_repro_a_pulled_lead_named_dash_acks_rejected_not_stored(monkeypatch, name):
    """Review repro: a lead named '-' passed both pull checks, was stored and acknowledged 'stored', TripPlanner marked
    it delivered and redacted it, then crm_intake_process failed on the name index (NormalisationError) with no alert.
    Now the real ingest rejects it: ack 'rejected' (TripPlanner raises lead_rejected_by_crm), tp_lead_rejected alert,
    nothing stored."""
    bad = lead(traveller={**lead()["traveller"], "name": name})
    tp = FakeTripPlanner({TENANT_A: [bad]})

    async def fake_agencies(_pool):
        return [Agency(TENANT_A)]

    @asynccontextmanager
    async def fake_system_tx(_pool, tenant_id, *, actor):
        yield FakeCtx(tenant_id)

    stored: list[Any] = []

    async def never(*a: Any, **k: Any) -> Any:
        stored.append(a)
        raise AssertionError("nothing is stored for a lead the intake cannot process")

    raised: list[tuple[str, Any, dict[str, Any]]] = []
    monkeypatch.setattr(leads.registry, "agencies", fake_agencies)
    monkeypatch.setattr(leads, "system_tx", fake_system_tx)
    monkeypatch.setattr(leads, "Vault", lambda keys, ctx: None)
    monkeypatch.setattr(leads, "planner_form", never)
    monkeypatch.setattr(leads, "store_submission", never)
    monkeypatch.setattr(ops_alerts, "emit", lambda check, scope=None, **d: raised.append((check, scope, d)))
    cfg = leads.LeadPullConfig(base_url="https://tp.example.invalid", tokens=FakeTokens(), transport=tp)  # type: ignore[arg-type]
    result = await leads.LeadPull(cfg, object(), today=lambda: TODAY)(None, JOB)  # type: ignore[arg-type]
    assert tp.acks == [(TENANT_A, bad["lead_id"], {"outcome": "rejected", "reason": "invalid_lead"})]
    assert result["rejected_count"] == 1 and "stored_count" not in result and stored == []
    assert [r[0] for r in raised] == ["tp_lead_rejected"]
