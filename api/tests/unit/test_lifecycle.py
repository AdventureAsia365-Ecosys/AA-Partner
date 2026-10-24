"""AA-942 (card CRM-14) Python branches: rules, trash, holds, archive, erase mapping, the dispatcher gate, the daily
run, offboarding. The SQL, RLS, triggers and the deletion function run on PostgreSQL in
tests/integration/test_crm010_*.py; these fakes drive the code around them."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from app.dispatch import queue as dispatch_queue
from app.dispatch import worker as dispatch_worker
from app.documents.store import MemoryDocumentStore
from app.errors import Conflict, Forbidden, Invalid, NotFound, ServiceError, Unavailable
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.lifecycle import booking, erase, offboarding, policy, rules, trash
from app.lifecycle import job as lifecycle_job
from app.pipeline import LOST_REASONS
from app.tenancy import LifecycleContext
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import MEMBER, OTHER_MEMBER, TENANT, FakeVault, enc, fake_tx, system_ctx, tenant_ctx

ID = uuid.UUID("00000000-0000-4000-8000-0000000000b1")
REQ = uuid.UUID("00000000-0000-4000-8000-0000000000b2")
NOW = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.UTC)
LATER = dt.datetime(2099, 1, 1, tzinfo=dt.UTC)
WEB = Path(__file__).resolve().parents[3] / "web"


def lifecycle_ctx(conn: ScriptedConn | None = None, status: str = "active") -> LifecycleContext:
    return LifecycleContext(conn=conn or ScriptedConn(), tenant_id=TENANT, actor="job:crm_lifecycle", status=status)


def refused(code: str, message: str) -> asyncpg.PostgresError:
    err = asyncpg.PostgresError(message)
    err.sqlstate = code  # type: ignore[misc]
    return err


def codes(cls: type[Exception], code: str):  # noqa: ANN201
    class _Ctx:
        def __enter__(self) -> _Ctx:
            self.raises = pytest.raises(cls)
            self.info = self.raises.__enter__()
            return self

        def __exit__(self, *exc: Any) -> bool:
            handled = self.raises.__exit__(*exc)
            assert getattr(self.info.value, "code", None) == code
            return bool(handled)

    return _Ctx()


# ── Rules and Lost codes ─────────────────────────────────────────────────────────────────────────
def test_rules_name_their_mechanism_and_enforcer() -> None:
    view = rules.rules_view()
    assert {r["code"] for r in view} >= {"traveler_pii", "trash", "form_submissions", "llm_calls", "audit_in_app"}
    assert all(r["mechanism"] and r["enforced_by"] for r in view)
    assert {r["setting"] for r in view if r["window"] == "setting"} == {"trash_days", "auto_archive_months"}


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([], None),
        (["hold.expired"], None),
        (["availability.sold_out", "hold.expired"], "aa_sold_out"),
        (["line.declined"], "aa_on_request_declined"),
        (["quote.price_changed", "line.confirmed"], None),
        (["departure.cancelled"], "aa_sold_out"),
    ],
)
def test_lost_reason_from_the_booking_feed(events: list[str], expected: str | None) -> None:
    assert rules.suggest_lost_reason(events) == expected


def test_lost_codes_are_one_list_in_api_web_and_database() -> None:
    web = (WEB / "lib" / "pipeline.ts").read_text(encoding="utf-8")
    block = web.split("export const LOST_REASONS = [", 1)[1].split("]", 1)[0]
    assert tuple(re.findall(r'"([a-z_]+)"', block)) == LOST_REASONS
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "005_crm_intake_pipeline.sql").read_text()
    check = sql.split("trip_lost_reason_chk", 1)[1].split(")", 3)
    assert set(re.findall(r"'([a-z_]+)'", "".join(check))) >= set(LOST_REASONS)
    assert set(rules.AA_EVENT_LOST_REASON.values()) <= set(LOST_REASONS)


# ── Erase ────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_erase_counts_and_deletes_document_objects() -> None:
    store = MemoryDocumentStore()
    store.objects["tenants/t/documents/d"] = b"x"
    conn = (
        ScriptedConn()
        .on("SELECT s3_key FROM crm.document", [{"s3_key": "tenants/t/documents/d"}])
        .on("crm.erase_subject", json.dumps({"counts": {"trip": 1, "document": 1}}))
    )
    counts = await erase.erase(tenant_ctx(conn), "trip", ID, dry_run=False, store=store)
    assert counts == {"trip": 1, "document": 1, "document_object": 1}
    assert store.objects == {}
    assert conn.sql("SET LOCAL ROLE crm_eraser") and conn.sql("SET LOCAL ROLE NONE")


@pytest.mark.asyncio
async def test_erase_needs_the_bucket_for_documents_and_the_vault_for_travellers() -> None:
    conn = ScriptedConn().on("SELECT s3_key FROM crm.document", [{"s3_key": "k"}])
    with codes(Unavailable, "documents_not_configured"):
        await erase.erase(tenant_ctx(conn), "trip", ID, dry_run=False)
    with pytest.raises(ValueError, match="vault"):
        await erase.erase(tenant_ctx(), "traveler", ID, dry_run=True)
    with pytest.raises(ValueError, match="scope"):
        await erase.erase(tenant_ctx(), "audit_event", ID, dry_run=True)


@pytest.mark.asyncio
async def test_erase_decrypts_the_profile_pointer_for_a_traveller() -> None:
    token = uuid.uuid4()
    seen: list[Any] = []

    def run(scope: str, subject: uuid.UUID, tok: uuid.UUID, dry: bool) -> str:
        seen.append((scope, tok, dry))
        return json.dumps({"counts": {"traveler": 1}})

    conn = (
        ScriptedConn()
        .on("SELECT profile_token_enc", enc("traveler", "profile_token_enc", str(token)))
        .on("crm.erase_subject", run)
    )
    assert (await erase.erase(tenant_ctx(conn), "traveler", ID, dry_run=True, vault=FakeVault()))["traveler"] == 1
    assert seen == [("traveler", token, True)]
    assert await erase.profile_token(tenant_ctx(ScriptedConn()), FakeVault(), ID) is None


@pytest.mark.parametrize(
    ("state", "cls", "code"),
    [
        ("CRM10", Conflict, "legal_hold"),
        ("CRM12", Conflict, "traveler_on_trip"),
        ("CRM13", NotFound, "subject_not_found"),
        ("CRM14", Conflict, "tenant_not_offboarding"),
        ("CRM15", Conflict, "profile_token_mismatch"),
        ("CRM16", Unavailable, "erase_incomplete"),
    ],
)
@pytest.mark.asyncio
async def test_erase_refusals_map_to_service_errors(state: str, cls: type[Exception], code: str) -> None:
    conn = ScriptedConn().on("crm.erase_subject", refused(state, "crm: refused"))
    with codes(cls, code):
        await erase.erase(tenant_ctx(conn), "note", ID, dry_run=False)


@pytest.mark.asyncio
async def test_a_blocked_trip_names_its_blockers_and_other_errors_pass() -> None:
    conn = ScriptedConn().on("crm.erase_subject", refused("CRM11", "crm: delete_blocked has_money,has_aa_line"))
    with codes(Conflict, "delete_blocked") as ctx:
        await erase.erase(tenant_ctx(conn), "trip", ID, dry_run=True)
    assert ctx.info.value.extra == {"blockers": ["has_money", "has_aa_line"]}
    conn = ScriptedConn().on("crm.erase_subject", refused("23505", "unique"))
    with pytest.raises(asyncpg.PostgresError):
        await erase.erase(tenant_ctx(conn), "note", ID, dry_run=True)
    assert erase.refusal(refused("42501", "x")) is None


@pytest.mark.asyncio
async def test_purge_expired() -> None:
    conn = ScriptedConn().on("crm.lifecycle_purge_expired", '{"llm_call": 3, "held": 0}')
    assert await erase.purge_expired(lifecycle_ctx(conn), dry_run=True) == {"llm_call": 3, "held": 0}


# ── Trash ────────────────────────────────────────────────────────────────────────────────────────
def _row(**over: Any) -> dict[str, Any]:
    base = {
        "deleted_at": None,
        "deleted_by": None,
        "purge_after": None,
        "created_by": f"member:{MEMBER}",
        "owner_member_id": None,
        "trip_id": None,
    }
    return base | over


@pytest.mark.asyncio
async def test_move_to_trash_refusals() -> None:
    with codes(Invalid, "invalid_kind"):
        await trash.move_to_trash(tenant_ctx(), "audit_event", ID)
    ctx = tenant_ctx(role="assistant")
    with codes(Forbidden, "not_allowed_for_role"):
        await trash.move_to_trash(ctx, "trip", ID)
    assert ctx.audit_events[0].outcome == "denied"
    with codes(Forbidden, "owner_only"):
        await trash.move_to_trash(tenant_ctx(role="agent"), "traveler", ID)
    with codes(NotFound, "trip_not_found"):
        await trash.move_to_trash(tenant_ctx(), "trip", ID)
    conn = ScriptedConn().on("FROM crm.trip WHERE", _row(deleted_at=NOW))
    with codes(NotFound, "trip_not_found"):
        await trash.move_to_trash(tenant_ctx(conn), "trip", ID)
    conn = ScriptedConn().on("FROM crm.trip WHERE", _row(owner_member_id=OTHER_MEMBER))
    with codes(Forbidden, "not_your_trip"):
        await trash.move_to_trash(tenant_ctx(conn, role="agent"), "trip", ID)
    conn = ScriptedConn().on("FROM crm.note WHERE", _row(created_by="member:someone"))
    with codes(Forbidden, "not_your_note"):
        await trash.move_to_trash(tenant_ctx(conn, role="assistant"), "note", ID)
    conn = ScriptedConn().on("FROM crm.trip WHERE", _row()).on("trip_delete_blockers", ["has_aa_line", "has_money"])
    with codes(Conflict, "has_money") as err:
        await trash.move_to_trash(tenant_ctx(conn), "trip", ID)
    assert err.info.value.extra == {"blockers": ["has_money", "has_aa_line"], "alternative": "cancel_trip"}
    conn = ScriptedConn().on("FROM crm.traveler WHERE", _row()).on("SELECT count(*) FROM crm.trip_traveler", 1)
    with codes(Conflict, "traveler_on_trip"):
        await trash.move_to_trash(tenant_ctx(conn), "traveler", ID)


@pytest.mark.asyncio
async def test_move_to_trash_uses_the_agency_window() -> None:
    conn = (
        ScriptedConn()
        .on("FROM crm.task WHERE", _row())
        .on("SELECT trash_days", 3)
        .on("UPDATE crm.task SET deleted_at", NOW)
    )
    ctx = tenant_ctx(conn, role="agent")
    assert await trash.move_to_trash(ctx, "task", ID) == {"kind": "task", "id": str(ID), "purge_after": NOW.isoformat()}
    assert conn.sql("UPDATE crm.task SET deleted_at")[0] == (ID, f"member:{MEMBER}", 3)
    assert ctx.audit_events[-1].reason == "to_trash"


@pytest.mark.asyncio
async def test_restore_branches() -> None:
    with codes(NotFound, "not_in_trash"):
        await trash.restore(tenant_ctx(ScriptedConn().on("FROM crm.task WHERE", _row())), "task", ID, None)
    mine = _row(deleted_at=NOW, deleted_by="member:x", purge_after=LATER)
    with codes(Forbidden, "owner_only"):
        await trash.restore(tenant_ctx(ScriptedConn().on("FROM crm.task", mine), role="agent"), "task", ID, None)
    old = _row(deleted_at=NOW, deleted_by=f"member:{MEMBER}", purge_after=NOW - dt.timedelta(days=1))
    with codes(Conflict, "trash_window_passed"):
        await trash.restore(tenant_ctx(ScriptedConn().on("FROM crm.task", old)), "task", ID, None)
    child = _row(deleted_at=NOW, deleted_by=f"member:{MEMBER}", purge_after=LATER, trip_id=ID)
    conn = ScriptedConn().on("FROM crm.task WHERE", child).on("SELECT deleted_at IS NOT NULL FROM crm.trip", True)
    with codes(Conflict, "parent_in_trash"):
        await trash.restore(tenant_ctx(conn, role="agent"), "task", ID, None)
    conn = ScriptedConn().on("FROM crm.task WHERE", child).on("SELECT deleted_at IS NOT NULL FROM crm.trip", False)
    assert (await trash.restore(tenant_ctx(conn, role="agent"), "task", ID, None))["restored"] is True


class Checker:
    def __init__(self, answer: dict[str, str]) -> None:
        self.answer = answer

    async def line_statuses(self, tenant_id: uuid.UUID, refs: list[str]) -> dict[str, str]:
        return {r: self.answer[r] for r in refs if r in self.answer}


@pytest.mark.asyncio
async def test_recheck_booking_branches() -> None:
    lines = [
        {"commitment_id": uuid.uuid4(), "source_ref": "h1", "line_status": "held"},
        {"commitment_id": uuid.uuid4(), "source_ref": "h2", "line_status": "requested"},
    ]
    conn = ScriptedConn().on("FROM crm.trip_commitment", lines)
    with codes(booking.BookingUnavailable, "booking_not_configured"):
        await trash.recheck_booking(tenant_ctx(conn), ID, None)
    with codes(booking.BookingUnavailable, "booking_line_unknown"):
        await trash.recheck_booking(tenant_ctx(conn), ID, Checker({"h1": "held"}))
    with codes(booking.BookingUnavailable, "booking_status_unknown"):
        await trash.recheck_booking(tenant_ctx(conn), ID, Checker({"h1": "lost", "h2": "held"}))
    conn = ScriptedConn().on("FROM crm.trip_commitment", lines)
    got = await trash.recheck_booking(tenant_ctx(conn), ID, Checker({"h1": "held", "h2": "expired"}))
    assert got == {"checked_count": 2, "changed_count": 1}
    assert conn.sql("SET line_status")[0][1] == "expired" and len(conn.sql("SET checked_at = now()")) == 1
    assert await trash.recheck_booking(tenant_ctx(ScriptedConn()), ID, None) == {}


@pytest.mark.asyncio
async def test_list_trash_decrypts_names_and_logs_one_list_view() -> None:
    rows = [
        {
            "kind": "trip",
            "id": ID,
            "label": "Vietnam 10 nights",
            "label_enc": None,
            "deleted_at": NOW,
            "deleted_by": "member:x",
            "purge_after": LATER,
        },
        {
            "kind": "traveler",
            "id": REQ,
            "label": None,
            "label_enc": enc("traveler", "display_name_enc", "Hoa"),
            "deleted_at": NOW,
            "deleted_by": "member:x",
            "purge_after": LATER,
        },
        {
            "kind": "document",
            "id": MEMBER,
            "label": None,
            "label_enc": enc("document", "filename_enc", "a.pdf"),
            "deleted_at": NOW,
            "deleted_by": "member:x",
            "purge_after": LATER,
        },
    ]
    conn = ScriptedConn().on("UNION ALL", rows)
    ctx = tenant_ctx(conn, role="agent")
    got = await trash.list_trash(ctx, FakeVault())  # type: ignore[arg-type]
    assert [i["label"] for i in got["items"]] == ["Vietnam 10 nights", "Hoa", "a.pdf"] and got["trash_days"] == 7
    assert conn.sql("UNION ALL")[0][0] == f"member:{MEMBER}"  # not the owner: own deletions only
    [event] = ctx.audit_events
    assert event.action == "list_view" and event.object_ids == (str(REQ), str(MEMBER))


# ── Policy ───────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_settings_and_their_bounds() -> None:
    assert await policy.settings(tenant_ctx()) == {"trash_days": 7, "auto_archive_months": 36}
    with codes(Forbidden, "owner_only"):
        await policy.update_settings(tenant_ctx(role="agent"), trash_days=3, auto_archive_months=None)
    with codes(Invalid, "invalid_trash_days"):
        await policy.update_settings(tenant_ctx(), trash_days=31, auto_archive_months=None)
    with codes(Invalid, "invalid_auto_archive_months"):
        await policy.update_settings(tenant_ctx(), trash_days=None, auto_archive_months=6)
    conn = ScriptedConn().on("INSERT INTO crm.tenant_settings", {"trash_days": 3, "auto_archive_months": 36})
    ctx = tenant_ctx(conn)
    assert await policy.update_settings(ctx, trash_days=3, auto_archive_months=None) == {
        "trash_days": 3,
        "auto_archive_months": 36,
    }
    assert ctx.audit_events[-1].fields == ("trash_days",)


def _hold(**over: Any) -> dict[str, Any]:
    base = {
        "hold_id": ID,
        "scope": "trip",
        "subject_id": REQ,
        "reason_code": "dispute",
        "status": "active",
        "placed_by": f"member:{MEMBER}",
        "created_at": NOW,
        "released_at": None,
        "released_by": None,
    }
    return base | over


@pytest.mark.asyncio
async def test_holds() -> None:
    conn = ScriptedConn().on("FROM crm.legal_hold WHERE deleted_at", [_hold(placed_by="platform:l1")])
    [view] = await policy.holds(tenant_ctx(conn))
    assert view["placed_by_aa"] is True and view["subject_id"] == str(REQ)
    with codes(Forbidden, "owner_only"):
        await policy.place_hold(tenant_ctx(role="agent"), "trip", REQ, "dispute")
    with codes(Invalid, "invalid_scope"):
        await policy.place_hold(tenant_ctx(), "audit", REQ, "dispute")
    with codes(Invalid, "invalid_reason"):
        await policy.place_hold(tenant_ctx(), "trip", REQ, "because")
    with codes(Invalid, "invalid_subject"):
        await policy.place_hold(tenant_ctx(), "tenant", REQ, "dispute")
    with codes(NotFound, "trip_not_found"):
        await policy.place_hold(tenant_ctx(), "trip", REQ, "dispute")
    conn = ScriptedConn().on("INSERT INTO crm.legal_hold", asyncpg.UniqueViolationError("dup"))
    with codes(Conflict, "hold_exists"):
        await policy.place_hold(tenant_ctx(conn), "tenant", None, "litigation")
    conn = ScriptedConn().on("INSERT INTO crm.legal_hold", _hold(scope="tenant", subject_id=None))
    assert (await policy.place_hold(tenant_ctx(conn), "tenant", None, "litigation"))["scope"] == "tenant"
    conn = ScriptedConn().on("UPDATE crm.legal_hold", refused("CRM17", "crm: hold_platform_only"))
    ctx = tenant_ctx(conn)
    with codes(Forbidden, "hold_platform_only"):
        await policy.release_hold(ctx, ID)
    assert ctx.audit_events[-1].outcome == "denied"
    with codes(NotFound, "hold_not_found"):
        await policy.release_hold(tenant_ctx(), ID)
    conn = ScriptedConn().on("UPDATE crm.legal_hold", refused("23514", "check"))
    with pytest.raises(asyncpg.PostgresError):
        await policy.release_hold(tenant_ctx(conn), ID)
    conn = ScriptedConn().on("UPDATE crm.legal_hold", _hold(status="released", released_at=NOW))
    assert (await policy.release_hold(tenant_ctx(conn), ID))["status"] == "released"
    assert await policy.held(tenant_ctx(ScriptedConn().on("legal_hold_blocks", True)), "trip", ID) is True


@pytest.mark.asyncio
async def test_archive_and_unarchive() -> None:
    with codes(Forbidden, "not_allowed_for_role"):
        await policy.archive(tenant_ctx(role="assistant"), ID, "agent")
    with codes(Invalid, "invalid_reason"):
        await policy.archive(tenant_ctx(), ID, "auto_inactive")
    with codes(NotFound, "traveler_not_found"):
        await policy.archive(tenant_ctx(), ID, "agent")
    conn = ScriptedConn().on("SELECT archived_at FROM crm.traveler", {"archived_at": NOW})
    with codes(Conflict, "already_archived"):
        await policy.archive(tenant_ctx(conn), ID, "agent")
    conn = ScriptedConn().on("UPDATE crm.traveler SET archived_at = now()", NOW)
    assert (await policy.archive(tenant_ctx(conn), ID, "duplicate"))["archive_reason"] == "duplicate"
    with codes(Forbidden, "not_allowed_for_role"):
        await policy.unarchive(tenant_ctx(role="assistant"), ID)
    with codes(NotFound, "not_archived"):
        await policy.unarchive(tenant_ctx(), ID)
    conn = ScriptedConn().on("SET archived_at = NULL", ID)
    assert (await policy.unarchive(tenant_ctx(conn), ID))["archived_at"] is None


# ── Dispatcher ───────────────────────────────────────────────────────────────────────────────────
def _dispatch_row(**over: Any) -> dict[str, Any]:
    base = {
        "dispatch_id": ID,
        "status": "queued",
        "channel": "email",
        "purpose": "transactional",
        "message_ref": None,
        "traveler_id": REQ,
        "trip_id": None,
        "archived_at": None,
        "traveler_deleted_at": None,
        "trip_deleted_at": None,
        "marketing_ok": None,
    }
    return base | over


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"traveler_deleted_at": NOW}, "traveler_trashed"),
        ({"trip_deleted_at": NOW}, "trip_trashed"),
        ({"archived_at": NOW}, "archived"),
        ({"purpose": "marketing"}, "no_consent"),
        ({"purpose": "marketing", "marketing_ok": False}, "no_consent"),
        ({"purpose": "marketing", "marketing_ok": True}, None),
        ({}, None),
    ],
)
def test_the_gate(over: dict[str, Any], code: str | None) -> None:
    assert dispatch_worker.gate(_dispatch_row(**over)) == code


class Sender:
    def __init__(self) -> None:
        self.sent: list[uuid.UUID] = []

    async def send(self, ctx: Any, dispatch_id: uuid.UUID, channel: str, message_ref: Any) -> None:
        self.sent.append(dispatch_id)


@pytest.mark.asyncio
async def test_dispatch_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job("j", "crm_dispatch", {"tenant_id": str(TENANT), "dispatch_id": str(ID)}, 1, 5)
    with pytest.raises(PermanentError):
        await dispatch_worker.build_handler()(object(), Job("j", "crm_dispatch", {}, 1, 5))  # type: ignore[arg-type]

    for row, sender, expected in (
        (None, None, "skipped"),
        (_dispatch_row(status="cancelled"), None, "skipped"),
        (_dispatch_row(archived_at=NOW), None, "dropped"),
        (_dispatch_row(), None, "held"),
        (_dispatch_row(), Sender(), "sent"),
    ):
        conn = ScriptedConn().on("FROM crm.dispatch_queue d", row)
        monkeypatch.setattr(dispatch_worker, "system_tx", fake_tx(system_ctx(conn)))
        result = await dispatch_worker.build_handler(sender)(object(), job)  # type: ignore[arg-type]
        assert result["status"] == expected
        check_payload(result, what="result")

    @asynccontextmanager
    async def locked(*_: Any, **__: Any):  # noqa: ANN202
        raise Forbidden("tenant_locked")
        yield  # pragma: no cover

    monkeypatch.setattr(dispatch_worker, "system_tx", locked)
    assert (await dispatch_worker.build_handler()(object(), job))["reason_code"] == "tenant_inactive"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_dispatch_queue() -> None:
    with codes(Invalid, "invalid_dispatch"):
        await dispatch_queue.enqueue_message(
            tenant_ctx(), traveler_id=ID, channel="pigeon", purpose="transactional", origin="system"
        )
    with codes(NotFound, "traveler_not_found"):
        await dispatch_queue.enqueue_message(
            tenant_ctx(), traveler_id=ID, channel="email", purpose="transactional", origin="system"
        )
    conn = ScriptedConn().on("SELECT archived_at, deleted_at", {"archived_at": NOW, "deleted_at": None})
    with codes(Conflict, "traveler_archived"):
        await dispatch_queue.enqueue_message(
            tenant_ctx(conn), traveler_id=ID, channel="email", purpose="transactional", origin="system"
        )
    conn = (
        ScriptedConn()
        .on("SELECT archived_at, deleted_at", {"archived_at": None, "deleted_at": None})
        .on("INSERT INTO crm.dispatch_queue", {"dispatch_id": REQ, "not_before": NOW})
        .on("INSERT INTO crm.job", "job-1")
    )
    got = await dispatch_queue.enqueue_message(
        tenant_ctx(conn), traveler_id=ID, channel="sms", purpose="transactional", origin="user_action"
    )
    assert got["dispatch_id"] == str(REQ)
    assert conn.sql("INSERT INTO crm.dispatch_queue")[0][-1] == dispatch_queue.UNDO_SECONDS
    with codes(Conflict, "already_dispatched"):
        await dispatch_queue.undo(tenant_ctx(), REQ)
    assert (await dispatch_queue.undo(tenant_ctx(ScriptedConn().on("SET status = 'cancelled'", REQ)), REQ))[
        "status"
    ] == "cancelled"


# ── The daily run ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_run_agency_dry_and_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    erased: list[tuple[str, bool]] = []

    async def fake_erase(ctx: Any, kind: str, subject: uuid.UUID, *, dry_run: bool, **_: Any) -> dict[str, int]:
        if kind == "note":
            raise Conflict("legal_hold")
        erased.append((kind, dry_run))
        return {kind: 1}

    async def fake_purge(ctx: Any, *, dry_run: bool) -> dict[str, int]:
        return {"llm_call": 2, "held": 0}

    monkeypatch.setattr(lifecycle_job.erase, "erase", fake_erase)
    monkeypatch.setattr(lifecycle_job.erase, "purge_expired", fake_purge)
    for dry in (True, False):
        conn = (
            ScriptedConn()
            .on(
                "FROM crm.form_submission s",
                [{"submission_id": ID, "held": False}, {"submission_id": REQ, "held": True}],
            )
            .on("FROM crm.trip WHERE deleted_at", [{"id": ID}])
            .on("FROM crm.note WHERE deleted_at", [{"id": ID}])
            .on("FROM crm.traveler WHERE deleted_at", [{"id": REQ}])
            .on("SELECT t.traveler_id FROM crm.traveler t", [{"traveler_id": REQ}])
        )
        ctx = lifecycle_ctx(conn)
        counts = await lifecycle_job.run_agency(ctx, dry_run=dry, keys=None, store=None)
        assert counts["submission_redacted"] == 1 and counts["submission_held"] == 1
        assert counts["trash_trip_purged"] == 1 and counts["trash_kept_legal_hold"] == 1
        assert counts["trash_kept_no_keys"] == 1 and counts["traveler_auto_archived"] == 1
        assert counts["llm_call_purged"] == 2
        assert bool(conn.sql("UPDATE crm.form_submission")) is not dry
        assert bool(conn.sql("archive_reason = 'auto_inactive'")) is not dry
        assert conn.sql("INSERT INTO crm.lifecycle_run")[0][1] is dry
        assert ctx.audit_events[-1].reason == ("dry_run" if dry else "applied")
    assert erased == [("trip", True), ("trip", False)]


@pytest.mark.asyncio
async def test_the_daily_handler_and_its_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    agencies = [type("A", (), {"tenant_id": t})() for t in (TENANT, ID, REQ)]

    async def list_agencies(pool: Any) -> list[Any]:
        return agencies

    @asynccontextmanager
    async def tx(pool: Any, tenant_id: uuid.UUID, **_: Any):  # noqa: ANN202
        if tenant_id == ID:
            raise Forbidden("tenant_status")
        yield lifecycle_ctx()

    async def run(ctx: Any, **_: Any) -> dict[str, int]:
        if ctx.tenant_id != TENANT:
            raise ServiceError("boom")
        return {"trash_trip_purged": 2}

    agencies.extend(type("A", (), {"tenant_id": uuid.uuid4()})() for _ in range(2))
    # TENANT ok, ID skipped (tx), REQ a service error, then an unexpected DB error that must not stop the run
    # (review S249), then one more agency that still runs.
    outcomes = iter(
        [
            {"trash_trip_purged": 2},
            Unavailable("key_unwrap_refused"),
            asyncpg.ForeignKeyViolationError("fk"),
            {"trash_trip_purged": 1},
        ]
    )

    async def run_once(ctx: Any, **_: Any) -> dict[str, int]:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(lifecycle_job.registry, "agencies", list_agencies)
    monkeypatch.setattr(lifecycle_job, "lifecycle_tx", tx)
    monkeypatch.setattr(lifecycle_job, "run_agency", run_once)
    result = await lifecycle_job.build_handler(dry_run=True)(object(), Job("j", "crm_lifecycle", {}, 1, 3))  # type: ignore[arg-type]
    assert result == {
        "dry_run": True,
        "agency_count": 2,
        "skipped_count": 1,
        "failed_count": 2,
        "trash_trip_purged_count": 3,
    }
    check_payload(result, what="result")
    assert run  # (kept for readability of the branches above)

    clock = [dt.datetime(2026, 10, 24, 5, 0, tzinfo=dt.UTC)]
    schedule = lifecycle_job.DailyLifecycleSchedule(19, lambda: clock[0])
    pool = ScriptedConn().on("INSERT INTO crm.job", "job-1")
    assert await schedule(pool) is None
    clock[0] = dt.datetime(2026, 10, 24, 19, 1, tzinfo=dt.UTC)
    assert await schedule(pool) == "job-1"
    assert await schedule(pool) is None  # once a day
    assert pool.sql("INSERT INTO crm.job")[0][2] == "crm_lifecycle:schedule:2026-10-24"
    with pytest.raises(ValueError):
        lifecycle_job.DailyLifecycleSchedule(24)


# ── Offboarding ──────────────────────────────────────────────────────────────────────────────────
def _request(**over: Any) -> dict[str, Any]:
    base = {
        "request_id": REQ,
        "status": "requested",
        "dry_run_counts": '{"traveler": 2}',
        "export_waived": False,
        "export_object_key": None,
        "export_sha256": None,
        "export_expires_at": None,
        "export_downloaded_at": None,
        "executed_at": None,
        "keys_destroyed_at": None,
        "backup_purge_by": None,
        "receipt": "{}",
        "failure_class": None,
        "created_at": NOW,
    }
    return base | over


@pytest.mark.asyncio
async def test_offboarding_request_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    async def counts(*_: Any, **__: Any) -> dict[str, int]:
        return {"traveler": 2}

    monkeypatch.setattr(offboarding.erase, "erase", counts)
    with codes(Forbidden, "owner_only"):
        await offboarding.request(tenant_ctx(role="agent"), export_waived=False)
    with codes(Conflict, "offboarding_open"):
        await offboarding.request(
            tenant_ctx(ScriptedConn().on("status IN ('requested'", {"x": 1})), export_waived=False
        )
    with codes(Conflict, "legal_hold"):
        await offboarding.request(tenant_ctx(ScriptedConn().on("legal_hold_blocks", True)), export_waived=False)
    for waived in (True, False):
        conn = (
            ScriptedConn()
            .on("INSERT INTO crm.erasure_request", {"request_id": REQ, "created_at": NOW})
            .on("FOR UPDATE", _request(export_waived=waived))
        )
        got = await offboarding.request(tenant_ctx(conn), export_waived=waived)
        assert got["dry_run_counts"] == {"traveler": 2}
        assert bool(conn.sql("INSERT INTO crm.job")) is not waived
    assert offboarding.view(None) is None
    assert await offboarding.latest(tenant_ctx()) is None


@pytest.mark.asyncio
async def test_offboarding_cancel_download_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryDocumentStore()
    with codes(NotFound, "request_not_found"):
        await offboarding.cancel(tenant_ctx(), REQ, store)
    conn = ScriptedConn().on("FOR UPDATE", _request(status="erasing"))
    with codes(Conflict, "not_cancellable"):
        await offboarding.cancel(tenant_ctx(conn), REQ, store)
    conn = ScriptedConn().on("FOR UPDATE", _request(status="failed", failure_class="legal_hold"))
    await offboarding.cancel(tenant_ctx(conn), REQ, store)  # a refused erase: the owner cancels it
    assert conn.sql("SET status = 'cancelled'")
    store.objects["exports/x"] = b"z"
    conn = ScriptedConn().on("FOR UPDATE", _request(export_object_key="exports/x"))
    await offboarding.cancel(tenant_ctx(conn), REQ, store)
    assert store.objects == {}

    vault = FakeVault()
    with codes(Conflict, "export_not_ready"):
        await offboarding.download(tenant_ctx(ScriptedConn().on("FOR UPDATE", _request())), vault, store, REQ)  # type: ignore[arg-type]
    ready = _request(status="export_ready", export_object_key="exports/y", export_sha256="0" * 64)
    with codes(Unavailable, "documents_not_configured"):
        await offboarding.download(tenant_ctx(ScriptedConn().on("FOR UPDATE", ready)), vault, None, REQ)  # type: ignore[arg-type]
    store.objects["exports/y"] = b"sealed"
    with codes(Unavailable, "export_integrity"):
        await offboarding.download(tenant_ctx(ScriptedConn().on("FOR UPDATE", ready)), vault, store, REQ)  # type: ignore[arg-type]

    class BytesVault:
        async def decrypt_bytes(self, *_: Any, **__: Any) -> bytes:
            return b'{"format": 1}\n'

    ok = ready | {"export_sha256": hashlib.sha256(b"sealed").hexdigest()}
    conn = ScriptedConn().on("FOR UPDATE", ok)
    assert await offboarding.download(tenant_ctx(conn), BytesVault(), store, REQ) == b'{"format": 1}\n'  # type: ignore[arg-type]
    assert conn.sql("export_downloaded_at = now()")

    async def counts(*_: Any, **__: Any) -> dict[str, int]:
        return {"member": 1}

    monkeypatch.setattr(offboarding.erase, "erase", counts)
    with codes(Invalid, "confirmation_mismatch"):
        await offboarding.confirm_erase(tenant_ctx(), REQ, confirm_tenant_id=ID, dry_run=False)
    with codes(Conflict, "not_confirmable"):
        conn = ScriptedConn().on("FOR UPDATE", _request(status="erased"))
        await offboarding.confirm_erase(tenant_ctx(conn), REQ, confirm_tenant_id=TENANT, dry_run=False)
    with codes(Conflict, "export_not_ready"):
        conn = ScriptedConn().on("FOR UPDATE", _request())
        await offboarding.confirm_erase(tenant_ctx(conn), REQ, confirm_tenant_id=TENANT, dry_run=False)
    with codes(Conflict, "legal_hold"):
        conn = ScriptedConn().on("FOR UPDATE", _request(export_waived=True)).on("legal_hold_blocks", True)
        await offboarding.confirm_erase(tenant_ctx(conn), REQ, confirm_tenant_id=TENANT, dry_run=False)
    with codes(Conflict, "deletion_dry_run") as err:
        conn = ScriptedConn().on("FOR UPDATE", _request(export_waived=True))
        await offboarding.confirm_erase(tenant_ctx(conn), REQ, confirm_tenant_id=TENANT, dry_run=True)
    assert err.info.value.extra == {"counts": {"member": 1}}
    conn = ScriptedConn().on("FOR UPDATE", _request(export_waived=True))
    await offboarding.confirm_erase(tenant_ctx(conn), REQ, confirm_tenant_id=TENANT, dry_run=False)
    assert conn.sql("SET status = 'offboarding'") and conn.sql("INSERT INTO crm.job")


@pytest.mark.asyncio
async def test_build_export_decrypts_and_drops_indexes() -> None:
    rows = [
        {
            "traveler_id": ID,
            "display_name_enc": enc("traveler", "display_name_enc", "Hoa"),
            "email_bidx": b"i",
            "profile_token_enc": b"p",
            "phone_enc": None,
            "created_at": NOW,
            "tags": [ID],
            "raw": b"\x01",
        }
    ]
    conn = ScriptedConn().on("to_regclass", "crm.x").on("FROM crm.traveler ORDER BY", rows)
    content = await offboarding.build_export(lifecycle_ctx(conn), FakeVault())  # type: ignore[arg-type]
    header, line = [json.loads(x) for x in content.decode().splitlines()]
    assert header["format"] == 1
    assert line == {
        "table": "traveler",
        "row": {
            "traveler_id": str(ID),
            "display_name": "Hoa",
            "created_at": NOW.isoformat(),
            "tags": [str(ID)],
            "raw": "AQ==",
        },
    }


class Keys:
    def __init__(self) -> None:
        self.destroyed = 0

    async def destroy(self, ctx: Any) -> int:
        self.destroyed += 1
        return 7


@pytest.mark.asyncio
async def test_offboard_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryDocumentStore()
    keys = Keys()
    handler = offboarding.build_handler(keys=keys, store=store, dry_run=False)  # type: ignore[arg-type]
    job = lambda mode: Job("j", "crm_offboard", {"tenant_id": str(TENANT), "request_id": str(REQ), "mode": mode}, 1, 3)  # noqa: E731
    with pytest.raises(PermanentError):
        await handler(object(), Job("j", "crm_offboard", {}, 1, 3))  # type: ignore[arg-type]
    with codes(Unavailable, "key_provider_not_configured"):
        await offboarding.build_handler(keys=None, store=store, dry_run=False)(object(), job("export"))  # type: ignore[arg-type]
    with pytest.raises(PermanentError, match="deletion_dry_run"):
        await offboarding.build_handler(keys=keys, store=store, dry_run=True)(object(), job("erase"))  # type: ignore[arg-type]
    with pytest.raises(PermanentError, match="bad_mode"):
        await handler(object(), job("other"))  # type: ignore[arg-type]

    # export: skipped / not configured / ready
    conn = ScriptedConn()
    monkeypatch.setattr(offboarding, "lifecycle_tx", fake_tx(lifecycle_ctx(conn)))
    assert (await handler(object(), job("export")))["status"] == "skipped"  # type: ignore[arg-type]
    conn.on("FOR UPDATE", _request())
    no_store = offboarding.build_handler(keys=keys, store=None, dry_run=False)  # type: ignore[arg-type]
    assert (await no_store(object(), job("export")))["status"] == "export_not_configured"  # type: ignore[arg-type]

    class SealVault:
        def __init__(self, *_: Any) -> None:
            pass

        async def encrypt(self, *_: Any, **__: Any) -> bytes:
            return b"sealed-export"

    async def content(ctx: Any, vault: Any) -> bytes:
        return b"{}\n"

    monkeypatch.setattr(offboarding, "Vault", SealVault)
    monkeypatch.setattr(offboarding, "build_export", content)
    assert (await handler(object(), job("export")))["status"] == "export_ready"  # type: ignore[arg-type]
    assert store.objects[offboarding.export_key(TENANT, REQ)] == b"sealed-export"

    # erase: skipped, erased, refused
    async def erased(*_: Any, **__: Any) -> dict[str, int]:
        return {"traveler": 2}

    monkeypatch.setattr(offboarding.erase, "erase", erased)
    conn = ScriptedConn().on("FOR UPDATE", _request(status="requested"))
    monkeypatch.setattr(offboarding, "lifecycle_tx", fake_tx(lifecycle_ctx(conn, status="offboarding")))
    assert (await handler(object(), job("erase")))["status"] == "skipped"  # type: ignore[arg-type]
    conn = ScriptedConn().on(
        "FOR UPDATE", _request(status="failed", export_object_key=offboarding.export_key(TENANT, REQ))
    )
    monkeypatch.setattr(offboarding, "lifecycle_tx", fake_tx(lifecycle_ctx(conn, status="offboarding")))
    result = await handler(object(), job("erase"))  # type: ignore[arg-type]
    assert result == {"request_id": str(REQ), "status": "erased", "keys_destroyed_count": 7}
    check_payload(result, what="result")
    assert keys.destroyed == 1 and store.objects == {}
    receipt = json.loads(conn.sql("SET status = 'erased'")[0][2])
    assert receipt["counts"] == {"traveler": 2} and receipt["keys_destroyed"] == 7
    assert conn.sql("SET status = 'offboarded'") and conn.sql("SET status = 'erasing', failure_class = NULL")

    async def held(*_: Any, **__: Any) -> dict[str, int]:
        raise Conflict("legal_hold")

    monkeypatch.setattr(offboarding.erase, "erase", held)
    conn = ScriptedConn().on("FOR UPDATE", _request(status="erasing"))
    monkeypatch.setattr(offboarding, "lifecycle_tx", fake_tx(lifecycle_ctx(conn, status="offboarding")))
    with pytest.raises(PermanentError, match="legal_hold"):
        await handler(object(), job("erase"))  # type: ignore[arg-type]
    assert conn.sql("SET status = 'failed'")[0][1] == "legal_hold"
    assert conn.sql("SET status = 'active'"), "a refused erase deleted nothing: the agency is given back"


def test_the_web_words_every_rule_and_mechanism() -> None:
    """Settings > Data and privacy words each rule the API returns (web/lib/i18n.ts, EN and VI blocks)."""
    text = (WEB / "lib" / "i18n.ts").read_text(encoding="utf-8")
    for r in rules.RULES:
        for key in (f"data.rule.{r.code}", f"data.how.{r.mechanism}"):
            assert text.count(f'"{key}"') == 2, key  # once in EN, once in VI
