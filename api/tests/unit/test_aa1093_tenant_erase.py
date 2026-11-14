"""AA-1093 stage 2: the pure rules of the CRM step of the CIS tenant erasure saga (``app.lifecycle.saga_rules``), the
job's feed / post / schedule logic (``app.lifecycle.tenant_erase``) with the record faked, and the record's SQL calls
(``app.lifecycle.saga_store``) on a fake pool. No database: the work on real tables is in
tests/integration/test_aa1093_tenant_erase_db.py.

The contract is the vendored byte copy ``docs/contracts/tenant.erasure.v1.openapi.yaml`` (pinned by hash): the field
lists the CRM reads and writes are compared with it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.cis.http import HttpError, HttpResponse
from app.cis.m2m import TokenError
from app.cis.membership_feed import FeedConfig
from app.errors import Conflict, Unavailable
from app.jobs.queue import Job
from app.lifecycle import saga_rules as rules
from app.lifecycle import saga_store, tenant_erase

SPEC = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "tenant.erasure.v1.openapi.yaml"
# sha256 of the root file docs/contracts/tenant.erasure.v1.openapi.yaml 1.0 (AA-1093, S279).
ROOT_CONTRACT_SHA256 = "1f0c814e4b2954ddbd386f5f9aed582cffb78b8512748f8926e2a000f4b37d8b"
RID = "6b0f0e1c-5a43-4c1e-9a52-6d0c3b8f1a01"
TENANT = "0b9d6c7e-3f43-4c1e-9a52-6d0c3b8f1a02"
NOW = dt.datetime(2026, 11, 15, 3, 0, tzinfo=dt.UTC)
HASH = "a" * 64


def _item(**kw: Any) -> dict[str, Any]:
    step = {"app": "crm", "status": "active", "not_before": "2026-11-01T00:00:00Z", "approved_plan_hash": None}
    step.update(kw.pop("step", {}))
    item = {
        "request_id": RID,
        "tenant_id": TENANT,
        "phase": "dry_run",
        "round": 2,
        "opened_at": "2026-08-01T00:00:00Z",
        "approval_expires_at": None,
        "is_pilot": False,
        "is_synthetic": True,
        "steps": [step],
        "updated_at": "2026-11-15T02:00:00Z",
        "cursor": "c1",
    }
    item.update(kw)
    return item


def _req(**kw: Any) -> rules.SagaRequest:
    parsed = rules.parse_request(_item(**kw))
    assert isinstance(parsed, rules.SagaRequest)
    return parsed


# ── the contract copy ────────────────────────────────────────────────────────────────────────────────────────────
def _required(schema: str) -> set[str]:
    text = SPEC.read_text(encoding="utf-8")
    block = re.search(rf"^    {schema}:\n((?:      .*\n|\n)+)", text, re.MULTILINE)
    assert block, schema
    found = re.search(r"required: \[([^\]]*)\]", block.group(1), re.DOTALL)
    assert found, schema
    return {v.strip() for v in found.group(1).replace("\n", " ").split(",") if v.strip()}


def test_the_vendored_contract_is_the_root_file() -> None:
    assert hashlib.sha256(SPEC.read_bytes()).hexdigest() == ROOT_CONTRACT_SHA256


def test_field_lists_follow_the_contract() -> None:
    assert _required("ErasureRequest") == rules.REQUEST_FIELDS
    assert _required("Step") == rules.STEP_FIELDS
    assert _required("ErasureReport") == set(rules.REPORT_FIELDS)
    assert _required("ErasureRequestPage") == {"items", "next_cursor"}


# ── parsing the feed ─────────────────────────────────────────────────────────────────────────────────────────────
def test_a_valid_item_is_parsed() -> None:
    req = _req(phase="apply", approval_expires_at="2026-11-16T03:00:00Z", step={"approved_plan_hash": HASH})
    assert req.request_id == uuid.UUID(RID) and req.tenant_id == uuid.UUID(TENANT)
    assert (req.phase, req.round, req.step_status, req.approved_plan_hash) == ("apply", 2, "active", HASH)
    assert req.approval_expires_at == dt.datetime(2026, 11, 16, 3, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"extra": 1}, "request_shape"),
        ({"steps": []}, "step_shape"),
        (
            {"steps": [{"app": "booking", "status": "active", "not_before": "x", "approved_plan_hash": None}]},
            "step_shape",
        ),
        ({"step": {"extra": True}}, "step_shape"),
        ({"request_id": "not-a-uuid"}, "request_value"),
        ({"phase": "scheduled"}, "request_value"),
        ({"round": 0}, "request_value"),
        ({"round": True}, "request_value"),
        ({"step": {"status": "running"}}, "request_value"),
        ({"step": {"not_before": "2026-11-01"}}, "request_value"),
        ({"approval_expires_at": "soon"}, "request_value"),
        ({"step": {"approved_plan_hash": "ABC"}}, "request_value"),
        ({"is_pilot": "no"}, "request_value"),
    ],
)
def test_an_item_outside_the_contract_is_refused(change: dict[str, Any], reason: str) -> None:
    assert rules.parse_request(_item(**change)) == reason


def test_a_non_object_is_refused_and_pages_are_checked() -> None:
    assert rules.parse_request(["x"]) == "request_shape"
    assert rules.parse_page({"items": [], "next_cursor": "e1abc"}) == ([], "e1abc")
    for bad in ({"items": []}, {"items": {}, "next_cursor": ""}, {"items": [], "next_cursor": "a b"}, []):
        with pytest.raises(ValueError):
            rules.parse_page(bad)


# ── the plan ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_plan_names_every_erase_count_the_keys_the_name_and_the_invitations() -> None:
    counts = {"traveler": 2, "trip": 1, "traveler_referrer_unlinked": 0, "document_object": 1}
    tables = rules.plan_entries(counts, live_keys=3, name_to_erase=1, invitations=2)
    assert [(t["table"], t["action"], t["rows"]) for t in tables] == [
        ("crm.studio_invitation", "delete", 2),
        ("crm.tenant", "anonymise", 1),
        ("crm.tenant_key", "shred", 3),
        ("crm.traveler", "delete", 2),
        ("crm.traveler", "detach", 0),
        ("crm.trip", "delete", 1),
    ]
    assert all(t["digest"] is None for t in tables)
    # No CRM agency: the invitations only.
    assert rules.plan_entries(None, live_keys=0, name_to_erase=0, invitations=0) == [
        {"table": "crm.studio_invitation", "action": "delete", "rows": 0, "digest": None}
    ]
    with pytest.raises(ValueError):
        rules.entry("not a table", "delete", 1)


def test_the_plan_hash_binds_tenant_round_and_counts() -> None:
    tables = rules.plan_entries({"trip": 1}, live_keys=1, name_to_erase=1, invitations=0)
    lines = ["tenant.erasure.v1|" + TENANT + "|2"] + sorted(f"{t['table']}|{t['action']}|{t['rows']}|" for t in tables)
    assert rules.plan_hash(TENANT, 2, tables) == hashlib.sha256("\n".join(lines).encode()).hexdigest()
    assert rules.plan_hash(TENANT, 2, list(reversed(tables))) == rules.plan_hash(TENANT, 2, tables)
    assert rules.plan_hash(TENANT, 3, tables) != rules.plan_hash(TENANT, 2, tables)
    more = rules.plan_entries({"trip": 2}, live_keys=1, name_to_erase=1, invitations=0)
    assert rules.plan_hash(TENANT, 2, more) != rules.plan_hash(TENANT, 2, tables)
    assert rules.totals(tables) == {"delete": 1, "shred": 1, "anonymise": 1}
    assert rules.erased_name(TENANT) == "Erased agency 0b9d6c7e"


# ── what is owed ─────────────────────────────────────────────────────────────────────────────────────────────────
def _made(*rows: tuple[str, str, str | None]) -> list[rules.Made]:
    return [rules.Made(*r) for r in rows]


def test_next_mode() -> None:
    assert rules.next_mode(_req(), [], NOW) == "dry_run"
    assert rules.next_mode(_req(), _made(("dry_run", "ok", "accepted")), NOW) is None
    assert rules.next_mode(_req(step={"status": "waiting"}), [], NOW) is None
    assert rules.next_mode(_req(step={"not_before": "2026-12-01T00:00:00Z"}), [], NOW) is None
    assert rules.next_mode(_req(phase="awaiting_approval"), [], NOW) is None
    apply = _req(phase="apply")
    assert rules.next_mode(apply, _made(("dry_run", "ok", "accepted")), NOW) == "apply"
    assert rules.next_mode(apply, _made(("apply", "ok", None)), NOW) is None  # CIS has not recorded it yet
    assert rules.next_mode(apply, _made(("apply", "ok", "accepted")), NOW) == "verify"
    assert rules.next_mode(apply, _made(("apply", "refused", "accepted")), NOW) is None
    assert (
        rules.next_mode(_req(phase="verify"), _made(("apply", "ok", "accepted"), ("verify", "ok", None)), NOW) is None
    )


def test_guards() -> None:
    agency = {"is_pilot": False, "is_synthetic": True}
    ok = _req(phase="apply", approval_expires_at="2026-11-16T03:00:00Z", step={"approved_plan_hash": HASH})

    def g(req: rules.SagaRequest, mode: str = "apply", **kw: Any) -> str | None:
        args = {"environment": "dev", "agency": agency, "apply_enabled": True, "now": NOW, **kw}
        return rules.guard(req, mode, **args)

    assert g(ok) is None
    assert g(ok, agency=None) is None
    assert g(_req(is_pilot=True), "dry_run") == "pilot_refused"
    assert g(ok, "verify", agency={"is_pilot": True}) == "pilot_refused"
    assert g(_req(), "dry_run", apply_enabled=False) is None  # a DRY RUN needs no switch
    assert g(_req(is_synthetic=False), "dry_run") is None
    assert g(ok, agency={"is_synthetic": False}) == "not_synthetic"
    assert g(ok, agency={"is_synthetic": False}, environment="prod") is None
    assert g(ok, apply_enabled=False) == "apply_disabled"
    assert g(_req(phase="apply")) == "approval_missing"
    assert g(_req(phase="apply", step={"approved_plan_hash": HASH})) == "approval_expired"
    late = _req(phase="apply", approval_expires_at="2026-11-15T02:00:00Z", step={"approved_plan_hash": HASH})
    assert g(late) == "approval_expired"


def test_the_report_body_is_the_contract_shape() -> None:
    req = _req()
    rid = uuid.UUID("11111111-2222-4333-8444-555555555555")
    body = rules.report_body(
        req, report_id=rid, mode="verify", outcome="ok", plan=HASH, job_id="not-a-uuid", reported_at=NOW,
        census={"pii_columns_checked": 3, "pii_rows_found": 0},
    )  # fmt: skip
    assert list(body) == list(rules.REPORT_FIELDS)
    assert body["plan_hash"] is None and body["job_id"] is None and body["reported_at"] == "2026-11-15T03:00:00Z"
    assert body["app"] == "crm" and body["round"] == 2 and body["tables"] == [] and body["blockers"] == []
    job = "22222222-3333-4444-8555-666666666666"
    dry = rules.report_body(req, report_id=rid, mode="dry_run", outcome="ok", plan=HASH, job_id=job, reported_at=NOW)
    assert dry["plan_hash"] == HASH and dry["job_id"] == job
    for bad in ({"mode": "erase"}, {"outcome": "done"}, {"error_code": "Bad"}, {"plan": "x"}):
        args: dict[str, Any] = {"mode": "dry_run", "outcome": "ok", **bad}
        with pytest.raises(ValueError):
            rules.report_body(req, report_id=rid, reported_at=NOW, **args)


# ── the job, with the record faked ───────────────────────────────────────────────────────────────────────────────
class Tokens:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.invalidated: list[str] = []

    async def token(self, scope: str) -> str:
        if self.fail:
            raise TokenError("token_unavailable")
        return "t"

    def invalidate(self, scope: str) -> None:
        self.invalidated.append(scope)


class Cis:
    def __init__(self, *answers: HttpResponse | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, method: str, url: str, headers: Any, body: Any, _timeout: float) -> HttpResponse:
        self.calls.append((method, url))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Store:
    """The saga record, in memory."""

    def __init__(self, failures: int = 0, last_error_at: dt.datetime | None = None) -> None:
        self.state = saga_store.FeedState(None, failures, last_error_at, None)
        self.stored: list[rules.SagaRequest] = []
        self.cursor: str | None = None
        self.failed: list[str] = []
        self.requests: list[rules.SagaRequest] = []
        self.made: list[rules.Made] = []
        self.unposted_rows: list[tuple[uuid.UUID, dict[str, Any]]] = []
        self.posted: list[tuple[uuid.UUID, str]] = []
        self.post_failures: list[uuid.UUID] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def load_state(_pool: Any) -> saga_store.FeedState:
            return self.state

        async def upsert_request(_pool: Any, req: rules.SagaRequest) -> bool:
            self.stored.append(req)
            return True

        async def advance(_pool: Any, old: str | None, new: str) -> bool:
            self.cursor = new
            return True

        async def succeed(_pool: Any) -> None:
            return None

        async def fail(_pool: Any, error_class: str) -> int:
            self.failed.append(error_class)
            return len(self.failed)

        async def actionable(_pool: Any, _limit: int) -> list[rules.SagaRequest]:
            return self.requests

        async def unposted(_pool: Any, _rid: uuid.UUID) -> list[tuple[uuid.UUID, dict[str, Any]]]:
            rows, self.unposted_rows = self.unposted_rows, []
            return rows

        async def made(_pool: Any, _rid: uuid.UUID, _round: int) -> list[rules.Made]:
            return self.made

        async def posted(_pool: Any, report_id: uuid.UUID, outcome: str) -> None:
            self.posted.append((report_id, outcome))

        async def post_failed(_pool: Any, report_id: uuid.UUID) -> None:
            self.post_failures.append(report_id)

        for name, fn in locals().items():
            if callable(fn) and name != "self":
                monkeypatch.setattr(saga_store, name, fn)


def _page(*items: Any, cursor: str = "c9") -> HttpResponse:
    return HttpResponse(200, json.dumps({"items": list(items), "next_cursor": cursor}).encode())


def _job() -> Job:
    return Job("33333333-4444-4555-8666-777777777777", "crm_tenant_erase", {"purpose": "slot_schedule"}, 1, 3)


def _handler(cis: Cis | None, tokens: Tokens | None = None, **kw: Any) -> tenant_erase.TenantErase:
    config = None if cis is None else FeedConfig("https://cis.example.test", tokens or Tokens(), cis)  # type: ignore[arg-type]
    return tenant_erase.TenantErase(
        config, keys=kw.get("keys"), store=None, apply_enabled=False, environment="dev", now=lambda: NOW
    )


@pytest.mark.asyncio
async def test_not_configured() -> None:
    handler = _handler(None)
    assert (await handler(None, _job()))["status"] == "not_configured"  # type: ignore[arg-type]
    assert (await handler(None, _job()))["status"] == "not_configured"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_pull_stores_valid_items_and_counts_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store()
    store.install(monkeypatch)
    cis = Cis(_page(_item(), {"bad": 1}))
    result = await _handler(cis)(None, _job())  # type: ignore[arg-type]
    assert result["status"] == "ok" and result["stored_count"] == 1 and result["rejected_count"] == 1
    assert store.cursor == "c9" and cis.calls[0][1].startswith("https://cis.example.test/tenant/v1/erasure-requests?")


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (HttpResponse(503, b""), "cis_unavailable"),
        (HttpResponse(403, b""), "cis_forbidden"),
        (HttpResponse(200, b"not json"), "cis_contract"),
        (HttpError("timeout"), "cis_timeout"),
    ],
)
@pytest.mark.asyncio
async def test_a_feed_error_is_recorded(monkeypatch: pytest.MonkeyPatch, answer: Any, error: str) -> None:
    store = Store()
    store.install(monkeypatch)
    result = await _handler(Cis(answer))(None, _job())  # type: ignore[arg-type]
    assert result["status"] == "error" and store.failed == [error]


@pytest.mark.asyncio
async def test_backoff_and_a_token_error(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(failures=2, last_error_at=NOW)
    store.install(monkeypatch)
    cis = Cis()
    assert (await _handler(cis)(None, _job()))["status"] == "backoff"  # type: ignore[arg-type]
    assert cis.calls == []
    store = Store()
    store.install(monkeypatch)
    assert (await _handler(Cis(), Tokens(fail=True))(None, _job()))["status"] == "error"  # type: ignore[arg-type]
    assert store.failed == ["token_unavailable"]


def _body(mode: str = "dry_run") -> dict[str, Any]:
    return rules.report_body(_req(), report_id=uuid.uuid4(), mode=mode, outcome="ok", plan=HASH, reported_at=NOW)


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (HttpResponse(201, b"{}"), "accepted"),
        (HttpResponse(200, b"{}"), "accepted"),
        (HttpResponse(409, b'{"error": "round_superseded", "message": "m"}'), "refused:round_superseded"),
        (HttpResponse(422, b"<html>"), "refused:http_422"),
        (HttpResponse(503, b""), None),
        (HttpResponse(401, b""), None),
        (HttpError("connect"), None),
    ],
)
@pytest.mark.asyncio
async def test_posting_a_stored_report(monkeypatch: pytest.MonkeyPatch, answer: Any, outcome: str | None) -> None:
    store = Store()
    store.install(monkeypatch)
    body = _body()
    store.requests = [_req()]
    store.made = [rules.Made("dry_run", "ok", None)]
    store.unposted_rows = [(uuid.UUID(body["report_id"]), body)]
    tokens = Tokens()
    cis = Cis(_page(), answer)
    await _handler(cis, tokens)(None, _job())  # type: ignore[arg-type]
    assert cis.calls[1] == ("POST", f"https://cis.example.test/tenant/v1/erasure-requests/{RID}/reports")
    if outcome is None:
        assert store.post_failures == [uuid.UUID(body["report_id"])] and store.posted == []
    else:
        assert store.posted == [(uuid.UUID(body["report_id"]), outcome)]
    assert tokens.invalidated == (["cis.tenant/erasure"] if getattr(answer, "status", 0) == 401 else [])


@pytest.mark.asyncio
async def test_the_owed_report_is_made_then_posted(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store()
    store.install(monkeypatch)
    store.requests = [_req()]
    made: list[str] = []

    async def report(self: Any, pool: Any, req: rules.SagaRequest, mode: str, job: Job) -> dict[str, Any]:
        made.append(mode)
        store.made = [rules.Made(mode, "ok", "accepted")]
        return _body(mode)

    monkeypatch.setattr(tenant_erase.TenantErase, "_report", report)
    result = await _handler(Cis(_page(), HttpResponse(201, b"{}")))(None, _job())  # type: ignore[arg-type]
    assert made == ["dry_run"] and result["reports_count"] == 1 and result["posted_count"] == 1


@pytest.mark.asyncio
async def test_an_apply_without_the_key_provider_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _handler(Cis())
    with pytest.raises(Unavailable):
        await handler._report(None, _req(phase="apply"), "apply", _job())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_refused_work_is_reported_in_its_own_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[dict[str, Any]] = []

    async def agency_status(_pool: Any, _tenant: uuid.UUID) -> str | None:
        return None

    async def without_agency(_pool: Any, _tenant: uuid.UUID, work: Any) -> Any:
        return await work("conn")

    async def step(*_args: Any) -> dict[str, Any]:
        raise Conflict("legal_hold")

    async def insert_report(_conn: Any, body: dict[str, Any]) -> bool:
        recorded.append(body)
        return True

    monkeypatch.setattr(saga_store, "agency_status", agency_status)
    monkeypatch.setattr(saga_store, "without_agency", without_agency)
    monkeypatch.setattr(saga_store, "insert_report", insert_report)
    handler = _handler(Cis())
    monkeypatch.setattr(handler, "_step", step)
    out = await handler._report(None, _req(), "dry_run", _job())  # type: ignore[arg-type]
    assert (out["status"], out["error_code"]) == ("refused", "legal_hold") and recorded == [out]

    async def failing(*_args: Any) -> dict[str, Any]:
        raise Unavailable("Not A Code")

    monkeypatch.setattr(handler, "_step", failing)
    out = await handler._report(None, _req(), "verify", _job())  # type: ignore[arg-type]
    assert (out["status"], out["error_code"]) == ("failed", "internal_error")


@pytest.mark.asyncio
async def test_the_schedule_queues_one_run_per_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    queued: list[tuple[str, str]] = []

    async def enqueue(_pool: Any, kind: str, payload: dict[str, Any], **kw: Any) -> str:
        queued.append((kind, kw["idempotency_key"]))
        return "job-1"

    async def prune(*_args: Any) -> int:
        return 0

    monkeypatch.setattr(tenant_erase.queue, "enqueue", enqueue)
    monkeypatch.setattr(tenant_erase.queue, "prune", prune)
    schedule = tenant_erase.TenantEraseSchedule(lambda: NOW)
    assert await schedule(None) == "job-1"  # type: ignore[arg-type]
    assert await schedule(None) is None  # type: ignore[arg-type]
    assert queued == [("crm_tenant_erase", tenant_erase.schedule_key(NOW))]


# ── the record's SQL on a fake pool ──────────────────────────────────────────────────────────────────────────────
class Pool:
    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.sql: list[str] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.sql.append(sql)
        return "OK"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.sql.append(sql)
        return self.answers.get("fetchval")

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.sql.append(sql)
        return self.answers.get("fetchrow")

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.sql.append(sql)
        return self.answers.get("fetch", [])


@pytest.mark.asyncio
async def test_the_record_calls() -> None:
    pool = Pool(fetchrow={"feed_cursor": "c1", "failures": 0, "last_error_at": None, "last_ok_at": None}, fetchval=1)
    state = await saga_store.load_state(pool)  # type: ignore[arg-type]
    assert state.cursor == "c1" and pool.sql[0] == saga_store.ENSURE_STATE_SQL
    assert await saga_store.advance(pool, None, "c2")  # type: ignore[arg-type]
    assert await saga_store.fail(pool, "cis_unavailable") == 1  # type: ignore[arg-type]
    await saga_store.succeed(pool)  # type: ignore[arg-type]
    assert await saga_store.upsert_request(pool, _req())  # type: ignore[arg-type]
    assert await saga_store.insert_report(pool, _body())  # type: ignore[arg-type]
    await saga_store.posted(pool, uuid.uuid4(), "accepted")  # type: ignore[arg-type]
    await saga_store.post_failed(pool, uuid.uuid4())  # type: ignore[arg-type]
    row = {
        "request_id": uuid.UUID(RID), "subject_tenant_id": uuid.UUID(TENANT), "phase": "apply", "round": 3,
        "step_status": "active", "not_before": NOW, "approved_plan_hash": HASH, "approval_expires_at": NOW,
        "is_pilot": False, "is_synthetic": True, "cis_updated_at": NOW,
    }  # fmt: skip
    rows = Pool(fetch=[row], fetchrow=row)
    [req] = await saga_store.actionable(rows, 5)  # type: ignore[arg-type]
    assert (req.phase, req.round, req.approved_plan_hash) == ("apply", 3, HASH)
    assert (await saga_store.request(rows, req.request_id)) == req  # type: ignore[arg-type]
    assert await saga_store.request(Pool(), req.request_id) is None  # type: ignore[arg-type]
    made = Pool(fetch=[{"mode": "dry_run", "outcome": "ok", "post_outcome": "accepted"}])
    assert await saga_store.made(made, req.request_id, 3) == [rules.Made("dry_run", "ok", "accepted")]  # type: ignore[arg-type]
    body = _body()
    unposted = Pool(fetch=[{"report_id": uuid.UUID(body["report_id"]), "body": json.dumps(body)}])
    assert await saga_store.unposted(unposted, req.request_id) == [(uuid.UUID(body["report_id"]), body)]  # type: ignore[arg-type]
