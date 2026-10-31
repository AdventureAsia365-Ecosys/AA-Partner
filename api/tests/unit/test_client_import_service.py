"""AA-962 (card CRM-31): the client import service (preview, start, progress from the job rows, the
``crm_client_import`` job's slices) and the onboarding checklist, controller settings and acknowledgement, on a
scripted connection and the in-memory ingest store. The SQL is covered by
tests/integration/test_onboarding_import_api.py. Invented data only."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Any

import pytest

from app import onboarding
from app.errors import Conflict, Invalid, NotFound, Unavailable
from app.imports import engine
from app.imports import service as imports
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.mail.store import MemoryIngestStore
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, TENANT, fake_tx, system_ctx, tenant_ctx

IMPORT = uuid.UUID("00000000-0000-4000-8000-000000000e01")
T0 = dt.datetime(2026, 10, 1, 9, tzinfo=dt.UTC)
CSV = b"client_ref,name,email\nC-1,Ana Moss,ana@example.test\nC-2,Ben Moss,ben@example.test\nC-3,Cy Moss,\n"
SHA = hashlib.sha256(CSV).hexdigest()


class RefVault:
    async def ref_index(self, table: str, column: str, value: str) -> bytes:
        return f"r:{table}:{value}".encode()

    async def email_index(self, table: str, column: str, value: str) -> bytes:
        return f"e:{value}".encode()

    async def name_index(self, table: str, column: str, value: str) -> bytes:
        return f"n:{value}".encode()


def test_checks() -> None:
    assert imports.check_region(None) is None and imports.check_region("") is None
    assert imports.check_region(" vn ") == "VN"
    with pytest.raises(Invalid):
        imports.check_region("VNM")
    with pytest.raises(Invalid):
        imports.check_entity("suppliers")
    assert imports.object_key(TENANT, IMPORT).endswith(f"{TENANT}/client-{IMPORT}")
    assert imports.slice_key(IMPORT, 2) == f"crm_client_import:{IMPORT}:2"


def test_a_missing_object_is_recognised() -> None:
    class S3Error(Exception):
        def __init__(self, code: str) -> None:
            self.response = {"Error": {"Code": code}}

    assert imports._missing(Unavailable("gone")) and imports._missing(KeyError("k"))
    assert imports._missing(S3Error("NoSuchKey")) and not imports._missing(S3Error("AccessDenied"))
    assert not imports._missing(RuntimeError("network"))


# ── preview / start ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_preview_counts_and_audits_without_writing() -> None:
    conn = ScriptedConn()
    ctx = tenant_ctx(conn)
    out = await imports.preview(ctx, RefVault(), entity="clients", data=CSV, region="au")  # type: ignore[arg-type]
    assert out["sha256"] == SHA and out["format"] == "csv" and out["columns"] == ["client_ref", "email", "name"]
    assert out["counts"]["create"] == 3 and out["can_apply"] is True and out["row_count"] == 3
    assert [e.action for e in ctx.audit_events] == ["import_preview"]
    assert not conn.sql("INSERT")


@pytest.mark.asyncio
async def test_start_checks_the_file_stores_it_and_queues_the_first_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    queued: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def enqueue(_conn: Any, kind: str, payload: dict[str, Any], **kw: Any) -> str:
        queued.append((kind, payload, kw))
        return "job-1"

    monkeypatch.setattr(imports.jobs, "enqueue", enqueue)
    store = MemoryIngestStore()
    ctx = tenant_ctx()
    with pytest.raises(Invalid) as err:
        await imports.start(ctx, store, entity="clients", data=CSV, sha256="abc", region=None)
    assert err.value.code == "invalid_sha256"
    with pytest.raises(Conflict) as err:
        await imports.start(ctx, store, entity="clients", data=CSV + b"C-4,Di Moss,\n", sha256=SHA, region=None)
    assert err.value.code == "file_changed"
    with pytest.raises(Invalid) as err:
        await imports.start(ctx, store, entity="trips", data=CSV, sha256=SHA, region=None)
    assert err.value.code == "missing_columns" and not store.objects and not queued

    out = await imports.start(ctx, store, entity="clients", data=CSV, sha256=SHA, region="vn")
    assert out["status"] == "queued" and out["row_count"] == 3
    import_id = uuid.UUID(out["import_id"])
    assert store.objects == {imports.object_key(TENANT, import_id): CSV}
    ((kind, payload, kw),) = queued
    assert kind == imports.KIND and payload["batch"] == 0 and payload["region"] == "VN"
    assert payload["member_id"] == str(MEMBER) and kw["idempotency_key"] == imports.slice_key(import_id, 0)
    assert [e.action for e in ctx.audit_events] == ["import_start"]


# ── progress ───────────────────────────────────────────────────────────────────────────────────────
def job_row(status: str, batch: int, *, result: dict[str, Any] | None = None, minutes: int = 0, **payload: Any) -> dict:
    body = {"tenant_id": str(TENANT), "import_id": str(IMPORT), "kind": "clients", "batch": batch, "row_count": 700}
    body.update(payload)
    return {
        "job_id": f"j{batch}",
        "status": status,
        "payload": json.dumps(body),
        "result": result,
        "created_at": T0 + dt.timedelta(minutes=minutes),
        "finished_at": T0 + dt.timedelta(minutes=minutes + 1) if status in ("succeeded", "failed") else None,
    }


SLICE_0 = {
    "status": "running",
    "create_count": 400,
    "match_count": 90,
    "invalid_count": 10,
    "possible_duplicate_count": 4,
}
SLICE_1 = {"status": "done", "create_count": 150, "already_imported_count": 50}


@pytest.mark.parametrize(
    ("jobs", "status", "processed", "finished"),
    [
        ([job_row("queued", 0)], "queued", 0, None),
        ([job_row("running", 0)], "running", 0, None),
        ([job_row("succeeded", 0, result=SLICE_0), job_row("queued", 1, minutes=2)], "running", 500, None),
        ([job_row("succeeded", 0, result=SLICE_0), job_row("succeeded", 1, result=SLICE_1, minutes=2)], "done", 700, 3),
        ([job_row("succeeded", 0, result=SLICE_0), job_row("failed", 1, minutes=2)], "failed", 500, 3),
    ],
)
@pytest.mark.asyncio
async def test_status_adds_up_the_slices(
    jobs: list[dict[str, Any]], status: str, processed: int, finished: int | None
) -> None:
    out = await imports.status(tenant_ctx(ScriptedConn([("payload->>'import_id' = $3", jobs)])), IMPORT)
    assert out["status"] == status and out["processed_count"] == processed and out["row_count"] == 700
    assert out["finished_at"] == ((T0 + dt.timedelta(minutes=finished)).isoformat() if finished else None)
    assert out["entity"] == "clients" and out["created_at"] == T0.isoformat()
    if processed:
        assert out["counts"]["create"] >= 400 and out["possible_duplicate_count"] == 4


@pytest.mark.asyncio
async def test_status_of_an_unknown_import_is_not_found() -> None:
    with pytest.raises(NotFound):
        await imports.status(tenant_ctx(), IMPORT)


@pytest.mark.asyncio
async def test_recent_groups_jobs_per_import_newest_first() -> None:
    other = str(uuid.UUID(int=99))
    rows = [
        job_row("succeeded", 1, result=SLICE_1, minutes=2),
        job_row("queued", 0, minutes=30, import_id=other),
        job_row("succeeded", 0, result=SLICE_0),
    ]
    out = await imports.recent(tenant_ctx(ScriptedConn([("make_interval(days => $3)", rows)])))
    assert [(i["import_id"], i["status"]) for i in out["imports"]] == [(other, "queued"), (str(IMPORT), "done")]
    assert imports._decoded({"payload": None, "result": '{"a": 1}'}) == {"payload": {}, "result": {"a": 1}}


# ── the job ────────────────────────────────────────────────────────────────────────────────────────
def job(attempt: int = 1, **payload: Any) -> Job:
    body: dict[str, Any] = {
        "tenant_id": str(TENANT),
        "import_id": str(IMPORT),
        "member_id": str(MEMBER),
        "kind": "clients",
        "batch": 0,
        "region": None,
    }
    body.update(payload)
    return Job(id="j", kind=imports.KIND, payload=body, attempt=attempt, max_attempts=imports.MAX_ATTEMPTS)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"runs": [], "queued": []}
    ctx = system_ctx()

    async def run(_ctx: Any, _vault: Any, entity: str, rows: Any, **kw: Any) -> list[engine.Result]:
        if seen.get("fail"):
            raise RuntimeError("database went away")
        seen["runs"].append((entity, [r.number for r in rows], kw["state"], kw["apply"]))
        return [engine.Result(r.number, "create", ("possible_duplicate",) if r.number == 2 else ()) for r in rows]

    async def enqueue(_pool: Any, kind: str, payload: dict[str, Any], **kw: Any) -> str:
        seen["queued"].append((payload, kw["idempotency_key"]))
        return "next"

    monkeypatch.setattr(imports, "system_tx", fake_tx(ctx))
    monkeypatch.setattr(imports, "Vault", lambda keys, ctx: object())
    monkeypatch.setattr(imports.engine, "run", run)
    monkeypatch.setattr(imports.jobs, "enqueue", enqueue)
    monkeypatch.setattr(imports, "SLICE", 2)
    monkeypatch.setattr(imports, "CHUNK", 1)
    return seen


@pytest.mark.asyncio
async def test_the_job_applies_a_slice_in_chunks_then_queues_the_next(wired: dict[str, Any]) -> None:
    store = MemoryIngestStore()
    key = imports.object_key(TENANT, IMPORT)
    await store.put(key, CSV)
    handler = imports.build_handler(store, keys=object())  # type: ignore[arg-type]
    first = await handler(object(), job())  # type: ignore[arg-type]
    assert first["status"] == "running" and first["create_count"] == 2 and first["possible_duplicate_count"] == 1
    assert [(r[1], r[3]) for r in wired["runs"]] == [([2], True), ([3], True)]
    assert wired["runs"][0][2] is wired["runs"][1][2]  # one in-file state across the chunks of a slice
    ((payload, idem),) = wired["queued"]
    assert payload["batch"] == 1 and idem == imports.slice_key(IMPORT, 1) and key in store.objects
    last = await handler(object(), job(batch=1))  # type: ignore[arg-type]
    assert last["status"] == "done" and last["batch"] == 1 and last["create_count"] == 1
    assert key not in store.objects and len(wired["queued"]) == 1


@pytest.mark.parametrize(
    "payload",
    [{"tenant_id": "not-a-uuid"}, {"kind": "suppliers"}, {"region": "VNM"}, {"batch": -1}, {"member_id": None}],
)
@pytest.mark.asyncio
async def test_a_bad_payload_fails_for_good(wired: dict[str, Any], payload: dict[str, Any]) -> None:
    handler = imports.build_handler(MemoryIngestStore(), keys=object())  # type: ignore[arg-type]
    with pytest.raises(PermanentError) as err:
        await handler(object(), job(**payload))  # type: ignore[arg-type]
    assert err.value.code == "bad_payload"


@pytest.mark.asyncio
async def test_the_job_without_a_store_is_retried(wired: dict[str, Any]) -> None:
    with pytest.raises(RuntimeError):
        await imports.build_handler(None, None)(object(), job())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_gone_file_fails_for_good_and_other_store_errors_retry(wired: dict[str, Any]) -> None:
    handler = imports.build_handler(MemoryIngestStore(), keys=object())  # type: ignore[arg-type]
    with pytest.raises(PermanentError) as err:
        await handler(object(), job())  # type: ignore[arg-type]
    assert err.value.code == "file_gone"

    class Flaky(MemoryIngestStore):
        async def get(self, key: str) -> bytes:
            raise ConnectionError("timeout")

    with pytest.raises(ConnectionError):
        await imports.build_handler(Flaky(), keys=object())(object(), job())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_file_that_no_longer_reads_is_deleted(wired: dict[str, Any]) -> None:
    store = MemoryIngestStore()
    key = imports.object_key(TENANT, IMPORT)
    await store.put(key, b"text\nhello\n")
    with pytest.raises(PermanentError) as err:
        await imports.build_handler(store, keys=object())(object(), job())  # type: ignore[arg-type]
    assert err.value.code == "missing_columns" and key not in store.objects


@pytest.mark.parametrize(("attempt", "kept"), [(1, True), (imports.MAX_ATTEMPTS, False)])
@pytest.mark.asyncio
async def test_a_failed_slice_keeps_the_file_until_the_last_attempt(
    wired: dict[str, Any], attempt: int, kept: bool
) -> None:
    store = MemoryIngestStore()
    key = imports.object_key(TENANT, IMPORT)
    await store.put(key, CSV)
    wired["fail"] = True
    with pytest.raises(RuntimeError):
        await imports.build_handler(store, keys=object())(object(), job(attempt=attempt))  # type: ignore[arg-type]
    assert (key in store.objects) is kept


# ── onboarding ─────────────────────────────────────────────────────────────────────────────────────
def settings_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "onboarding_later": None,
        "onboarding_done": None,
        "privacy_notice_url": None,
        "data_request_contact": None,
        "consent_text": None,
        "responsibility_ack_version": None,
        "responsibility_ack_at": None,
        "responsibility_ack_member_id": None,
    }
    row.update(over)
    return row


def facts_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "mailboxes": None,
        "members": 1,
        "invitations": 0,
        "forms": 0,
        "inquiries": False,
        "imported": False,
        "payment_methods": None,
        "templates": False,
        "credentials": None,
        "calendar_feeds": 0,
    }
    row.update(over)
    return row


def onboarding_conn(settings: dict[str, Any] | None, facts: dict[str, Any] | None = None) -> ScriptedConn:
    state = {"row": settings}

    def ensure() -> None:
        state["row"] = state["row"] or settings_row()

    return ScriptedConn(
        [
            ("FROM crm.tenant_settings", lambda: state["row"]),
            ("DEFAULT VALUES", ensure),
            ("FROM crm.channel_account", facts or facts_row()),
            ("RETURNING responsibility_ack_at", T0),
        ]
    )


@pytest.mark.asyncio
async def test_a_new_agency_gets_its_settings_row_and_an_open_checklist() -> None:
    conn = onboarding_conn(None)
    out = await onboarding.overview(tenant_ctx(conn), duplicate_count=0)
    assert conn.sql("DEFAULT VALUES") == [()]
    assert out["required_done"] == 0 and out["required_total"] == 3 and out["complete"] is False
    assert out["optional_open"] == 8 and out["acknowledgement"]["accepted_at"] is None
    assert {c["key"]: c["status"] for c in out["connections"]} == {
        "mailbox": "not_set_up",
        "card_payments": "not_set_up",
        "sms_whatsapp": "not_set_up",
        "calendar": "not_set_up",
    }


@pytest.mark.asyncio
async def test_a_set_up_agency_is_complete() -> None:
    settings = settings_row(
        onboarding_later=["templates", "kickoff_call"],
        onboarding_done=["help_centre", "kickoff_call"],
        privacy_notice_url="https://agency.example.test/privacy",
        data_request_contact="privacy@agency.example.test",
        responsibility_ack_version=onboarding.ACK_VERSION,
        responsibility_ack_at=T0,
    )
    facts = facts_row(
        mailboxes=["needs_attention", "connected"],
        members=1,
        invitations=1,
        forms=1,
        inquiries=True,
        imported=True,
        payment_methods=["stripe:active"],
        credentials=["esign:disconnected"],
        calendar_feeds=1,
    )
    out = await onboarding.overview(tenant_ctx(onboarding_conn(settings, facts)), duplicate_count=0)
    steps = {s["key"]: s for s in out["steps"]}
    assert out["complete"] is True and out["required_done"] == 3
    assert steps["import_clients"]["done"] and steps["review_duplicates"]["done"] and steps["payments"]["done"]
    assert steps["privacy_settings"]["done"] and steps["help_centre"]["marked_done"]
    assert steps["templates"]["later"] and not steps["kickoff_call"]["later"]  # done wins over later
    assert out["acknowledgement"]["accepted_at"] == T0.isoformat()
    conns = {c["key"]: c["status"] for c in out["connections"]}
    assert (
        conns["card_payments"] == "connected" and conns["calendar"] == "connected" and conns["esign"] == "disconnected"
    )


@pytest.mark.parametrize(
    ("key", "state", "code"),
    [
        ("dance", "done", "invalid_step"),
        ("kickoff_call", "maybe", "invalid_state"),
        ("connect_mailbox", "later", "step_required"),
        ("payments", "done", "step_not_manual"),
    ],
)
@pytest.mark.asyncio
async def test_step_changes_are_checked(key: str, state: str, code: str) -> None:
    with pytest.raises(Invalid) as err:
        await onboarding.set_step(tenant_ctx(), key, state)
    assert err.value.code == code


@pytest.mark.parametrize(
    ("key", "state", "later", "done"),
    [
        ("kickoff_call", "later", ["kickoff_call", "templates"], []),
        ("kickoff_call", "done", ["templates"], ["kickoff_call"]),
        ("templates", "open", [], ["kickoff_call"]),
    ],
)
@pytest.mark.asyncio
async def test_step_changes_rewrite_the_lists(key: str, state: str, later: list[str], done: list[str]) -> None:
    settings = settings_row(onboarding_later=["templates", "gone_step"], onboarding_done=["kickoff_call"])
    if key == "kickoff_call":
        settings["onboarding_done"] = []
    conn = onboarding_conn(settings)
    ctx = tenant_ctx(conn)
    assert await onboarding.set_step(ctx, key, state) == {"key": key, "state": state}
    assert conn.sql("SET onboarding_later") == [(sorted(later), sorted(done))]
    assert ctx.audit_events[0].reason == f"step_{state}"


@pytest.mark.asyncio
async def test_controller_settings_and_acknowledgement_are_saved_and_audited() -> None:
    conn = onboarding_conn(settings_row())
    ctx = tenant_ctx(conn)
    out = await onboarding.set_controller(ctx, "https://agency.example.test/p", " privacy@agency.example.test ", None)
    assert out == {
        "privacy_notice_url": "https://agency.example.test/p",
        "data_request_contact": "privacy@agency.example.test",
        "consent_text": None,
    }
    assert conn.sql("SET privacy_notice_url") == [tuple(out.values())]
    with pytest.raises(Invalid):
        onboarding.clean_controller(42, None, None)
    with pytest.raises(Conflict) as err:
        await onboarding.acknowledge(ctx, "2020-01-01.1")
    assert err.value.code == "version_changed" and err.value.extra == {"version": onboarding.ACK_VERSION}
    ack = await onboarding.acknowledge(ctx, onboarding.ACK_VERSION)
    assert ack == {"version": onboarding.ACK_VERSION, "accepted": True, "accepted_at": T0.isoformat()}
    assert conn.sql("SET responsibility_ack_version") == [(onboarding.ACK_VERSION, MEMBER)]
    assert [e.action for e in ctx.audit_events] == ["update", "acknowledge"]
