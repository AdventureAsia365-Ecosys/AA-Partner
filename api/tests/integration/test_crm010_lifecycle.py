"""AA-942 (card CRM-14) end to end on PostgreSQL 15 with the RDS role shape, as crm_app, with real field encryption.

Card CRM-14:
- **R**: delete a trip with a paid invoice -> 409 ``has_money`` (and the refusal is on record); an archived contact's
  queued message is dropped at the dispatcher.
- **E**: a trip whose AA lines are all expired can be deleted (and is purged by the daily run once its window
  passed); restore re-checks Booking (no checker -> 503 and the trip stays in the trash; with Booking -> the lines take
  Booking's status).
- The daily ``crm_lifecycle`` run: DRY RUN counts only; applied: submission payloads redacted after 90 days (not
  under a legal hold), LLM calls older than 180 days purged, trash purged, inactive clients archived.
- Erasure bypasses the trash; offboarding = export, then erase (crypto-shred with ``KeyProvider.destroy``; the audit
  stays).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from typing import Any

import asyncpg
import pytest

from app.dispatch import queue as dispatch
from app.dispatch.worker import build_handler as build_dispatch
from app.documents import service as documents
from app.documents.store import MemoryDocumentStore
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.intake import public as intake
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.lifecycle import booking, erase, offboarding, policy, trash
from app.lifecycle.job import build_handler as build_lifecycle
from app.tenancy import tenant_tx
from app.vault import KeyProvider, Vault
from app.vault.errors import KeyDestroyed
from tests.integration.test_crm005_intake import SETTINGS, _agency, _form, _keys, _owner_fetch, _process_all, answers

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

JPEG = b"\xff\xd8\xff\xe0" + b"synthetic boarding pass" + b"\x00" * 64


async def _inquiry(pool: asyncpg.Pool, keys: KeyProvider, *roles: str) -> dict[str, Any]:
    tenant_id, people = await _agency(pool, *(roles or ("owner",)))
    owner, owner_id = people[0]
    form = await _form(pool, tenant_id, owner, default_owner_member_id=owner_id)
    assert (await intake.submit(pool, keys, SETTINGS, form.public_key, answers(), origin=None)).stored
    [result] = [r for r in await _process_all(pool, keys, tenant_id) if r["status"] == "created"]
    return {
        "tenant": tenant_id,
        "owner": owner,
        "owner_id": owner_id,
        "people": people,
        "trip": uuid.UUID(result["trip_id"]),
        "lead": uuid.UUID(result["traveler_id"]),
        "form": form,
    }


async def _sql(pool: asyncpg.Pool, tenant_id: uuid.UUID, sql: str, *args: Any) -> Any:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        return await conn.fetch(sql, *args)


async def _commit(pool: asyncpg.Pool, tenant_id: uuid.UUID, trip: uuid.UUID, kind: str, status: str, ref: str) -> None:
    await _sql(
        pool,
        tenant_id,
        "INSERT INTO crm.trip_commitment (trip_id, kind, line_status, source_ref) VALUES ($1, $2, $3, $4)",
        trip,
        kind,
        status,
        ref,
    )


@contextlib.contextmanager
def _raises(cls: type[Exception], code: str) -> Iterator[Any]:
    """A service error of ``cls`` with this ``code`` (the message is for people; the code is the contract)."""
    with pytest.raises(cls) as err:
        yield err
    assert getattr(err.value, "code", None) == code


class FakeBooking:
    def __init__(self, answer: dict[str, str]) -> None:
        self.answer = answer
        self.calls: list[list[str]] = []

    async def line_statuses(self, tenant_id: uuid.UUID, refs: list[str]) -> dict[str, str]:
        self.calls.append(list(refs))
        return {r: self.answer[r] for r in refs if r in self.answer}


class _KeyCtx:
    """The parts of a context the key provider and the vault touch (an offboarded agency opens no real context)."""

    def __init__(self, conn: asyncpg.Connection, tenant_id: uuid.UUID) -> None:
        self.conn, self.tenant_id, self.dek_memo, self.dek_current = conn, tenant_id, {}, {}
        self.events: list[tuple[Any, ...]] = []

    def audit(self, *args: Any, **kwargs: Any) -> None:
        self.events.append(args)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[uuid.UUID] = []

    async def send(self, ctx: Any, dispatch_id: uuid.UUID, channel: str, message_ref: uuid.UUID | None) -> None:
        self.sent.append(dispatch_id)


async def _jobs(pool: asyncpg.Pool, kind: str, tenant_id: uuid.UUID) -> list[Job]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text, payload FROM crm.job WHERE kind = $1 AND status = 'queued' "
            "AND payload->>'tenant_id' = $2 ORDER BY created_at, id",
            kind,
            str(tenant_id),
        )
    out = []
    for r in rows:
        payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"])
        out.append(Job(r["id"], kind, payload, 1, 5))
    return out


async def _done(pool: asyncpg.Pool, job: Job) -> None:
    async with pool.acquire() as conn:
        await conn.execute("UPDATE crm.job SET status = 'cancelled', finished_at = now() WHERE id = $1::uuid", job.id)


async def _events(admin: asyncpg.Connection, tenant_id: uuid.UUID, action: str) -> list[asyncpg.Record]:
    return await _owner_fetch(
        admin,
        tenant_id,
        "SELECT action, outcome, object_kind, object_id, reason FROM crm.audit_event WHERE action = $1 "
        "ORDER BY at, event_id",
        action,
    )


# ── Card R ────────────────────────────────────────────────────────────────────────────────────────
async def test_r_delete_a_trip_with_a_paid_invoice_is_409_has_money(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    a = await _inquiry(app_pool, keys)
    await _commit(app_pool, a["tenant"], a["trip"], "invoice", "paid", "inv-1")
    await _commit(app_pool, a["tenant"], a["trip"], "aa_line", "confirmed", "bk-hold-1")
    with pytest.raises(Conflict) as err:
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            await trash.move_to_trash(ctx, "trip", a["trip"])
    assert err.value.code == "has_money" and err.value.status_code == 409
    assert err.value.extra == {"blockers": ["has_money", "has_aa_line"], "alternative": "cancel_trip"}
    [event] = await _events(admin, a["tenant"], "delete")
    assert (event["outcome"], event["reason"], event["object_id"]) == ("denied", "has_money", str(a["trip"]))
    rows = await _sql(app_pool, a["tenant"], "SELECT deleted_at FROM crm.trip WHERE trip_id = $1", a["trip"])
    assert rows[0]["deleted_at"] is None


async def test_r_an_archived_contacts_queued_message_is_dropped(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    a = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        queued = await dispatch.enqueue_message(
            ctx, traveler_id=a["lead"], channel="email", purpose="transactional", origin="system", trip_id=a["trip"]
        )
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await policy.archive(ctx, a["lead"], "client_request")
    sender = FakeSender()
    [job] = await _jobs(app_pool, "crm_dispatch", a["tenant"])
    result = await build_dispatch(sender)(app_pool, job)
    assert result == {"dispatch_id": queued["dispatch_id"], "status": "dropped", "reason_code": "archived"}
    assert sender.sent == []
    [row] = await _sql(
        app_pool,
        a["tenant"],
        "SELECT status, gate_result, decided_at FROM crm.dispatch_queue WHERE dispatch_id = $1",
        uuid.UUID(queued["dispatch_id"]),
    )
    assert (row["status"], row["gate_result"]) == ("dropped", "archived") and row["decided_at"] is not None
    [event] = await _events(admin, a["tenant"], "dispatch")
    assert (event["outcome"], event["reason"]) == ("denied", "archived")
    # Queueing for an archived client is refused at once; unarchived, the next message passes the gate.
    with _raises(Conflict, "traveler_archived"):
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            await dispatch.enqueue_message(
                ctx, traveler_id=a["lead"], channel="email", purpose="transactional", origin="system"
            )
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await policy.unarchive(ctx, a["lead"])
        again = await dispatch.enqueue_message(
            ctx, traveler_id=a["lead"], channel="email", purpose="transactional", origin="system"
        )
    await _done(app_pool, job)
    [job2] = await _jobs(app_pool, "crm_dispatch", a["tenant"])
    assert (await build_dispatch(sender)(app_pool, job2))["status"] == "sent"
    assert sender.sent == [uuid.UUID(again["dispatch_id"])]


async def test_dispatch_gate_consent_held_without_sender_and_undo(app_pool: asyncpg.Pool) -> None:
    keys = _keys()
    a = await _inquiry(app_pool, keys)  # the form gave contact consent, no marketing consent
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        marketing = await dispatch.enqueue_message(
            ctx, traveler_id=a["lead"], channel="email", purpose="marketing", origin="workflow"
        )
        reply = await dispatch.enqueue_message(
            ctx, traveler_id=a["lead"], channel="whatsapp", purpose="transactional", origin="workflow"
        )
        undone = await dispatch.enqueue_message(
            ctx, traveler_id=a["lead"], channel="email", purpose="transactional", origin="user_action"
        )
        await dispatch.undo(ctx, uuid.UUID(undone["dispatch_id"]))
        with _raises(Conflict, "already_dispatched"):
            await dispatch.undo(ctx, uuid.UUID(undone["dispatch_id"]))
    results = {}
    for job in await _jobs(app_pool, "crm_dispatch", a["tenant"]):
        r = await build_dispatch(None)(app_pool, job)
        results[r["dispatch_id"]] = (r["status"], r.get("reason_code"))
    assert results[marketing["dispatch_id"]] == ("dropped", "no_consent")
    assert results[reply["dispatch_id"]] == ("held", "no_sender")
    assert results[undone["dispatch_id"]] == ("skipped", None)


# ── Card E ────────────────────────────────────────────────────────────────────────────────────────
async def test_e_a_trip_whose_aa_lines_all_expired_can_be_deleted_and_is_purged(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    store = MemoryDocumentStore()
    a = await _inquiry(app_pool, keys)
    await _commit(app_pool, a["tenant"], a["trip"], "aa_line", "expired", "bk-hold-2")
    await _commit(app_pool, a["tenant"], a["trip"], "aa_line", "released", "bk-hold-3")
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        doc = await documents.upload(
            ctx,
            Vault(keys, ctx),
            store,
            traveler_id=None,
            trip_id=a["trip"],
            kind="ticket",
            filename="boarding.jpg",
            content_type="image/jpeg",
            content=JPEG,
        )
        moved = await trash.move_to_trash(ctx, "trip", a["trip"])
    assert moved["kind"] == "trip" and store.objects
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        listed = await trash.list_trash(ctx, Vault(keys, ctx))
    assert [(i["kind"], i["id"]) for i in listed["items"]] == [("trip", str(a["trip"]))]
    assert listed["trash_days"] == 7
    await _sql(app_pool, a["tenant"], "UPDATE crm.trip SET purge_after = now() - interval '1 minute'")
    # DRY RUN (the default): counted, nothing purged.
    # The job totals span every agency in the database (other tests leave trashed rows behind): assert on this
    # agency's own crm.lifecycle_run row, never on the job-wide totals.
    dry = await build_lifecycle(dry_run=True, keys=keys, store=store)(app_pool, Job("j1", "crm_lifecycle", {}, 1, 3))
    assert dry["dry_run"] is True and dry["trash_trip_purged_count"] >= 1
    [dry_run_row] = await _sql(app_pool, a["tenant"], "SELECT dry_run, counts FROM crm.lifecycle_run")
    assert dry_run_row["dry_run"] is True and json.loads(dry_run_row["counts"])["trash_trip_purged"] == 1
    assert len(await _sql(app_pool, a["tenant"], "SELECT 1 FROM crm.trip")) == 1 and store.objects
    done = await build_lifecycle(dry_run=False, keys=keys, store=store)(app_pool, Job("j2", "crm_lifecycle", {}, 1, 3))
    assert done["trash_trip_purged_count"] >= 1
    for table in ("trip", "trip_stage_history", "trip_commitment", "task", "note", "document"):
        assert await _sql(app_pool, a["tenant"], f"SELECT 1 FROM crm.{table}") == [], table  # noqa: S608
    assert store.objects == {}, "the document object goes with its trip"
    assert doc["document_id"]
    runs = await _sql(app_pool, a["tenant"], "SELECT dry_run, counts FROM crm.lifecycle_run ORDER BY created_at")
    assert [r["dry_run"] for r in runs] == [True, False]
    assert json.loads(runs[1]["counts"])["trash_trip_purged"] == 1
    assert [e["reason"] for e in await _events(admin, a["tenant"], "lifecycle_run")] == ["dry_run", "applied"]


async def test_e_restore_rechecks_booking(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    a = await _inquiry(app_pool, keys)
    await _commit(app_pool, a["tenant"], a["trip"], "aa_line", "requested", "bk-hold-9")
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await trash.move_to_trash(ctx, "trip", a["trip"])
    with _raises(booking.BookingUnavailable, "booking_not_configured"):
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            await trash.restore(ctx, "trip", a["trip"], None)
    rows = await _sql(app_pool, a["tenant"], "SELECT deleted_at FROM crm.trip WHERE trip_id = $1", a["trip"])
    assert rows[0]["deleted_at"] is not None, "Booking unreachable: the trip stays in the trash"
    checker = FakeBooking({"bk-hold-9": "expired"})
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        restored = await trash.restore(ctx, "trip", a["trip"], checker)
    assert checker.calls == [["bk-hold-9"]]
    assert restored["aa_lines"] == {"checked_count": 1, "changed_count": 1}
    [line] = await _sql(app_pool, a["tenant"], "SELECT line_status, checked_at FROM crm.trip_commitment")
    assert line["line_status"] == "expired" and line["checked_at"] is not None
    [event] = await _events(admin, a["tenant"], "restore")
    assert event["reason"] == "booking_rechecked"


# ── Trash rules ───────────────────────────────────────────────────────────────────────────────────
async def test_who_deletes_and_restores(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    a = await _inquiry(app_pool, keys, "owner", "agent", "assistant")
    (_, _), (agent, _), (assistant, _) = a["people"]
    with _raises(Forbidden, "not_allowed_for_role"):
        async with tenant_tx(app_pool, assistant, a["tenant"]) as ctx:
            await trash.move_to_trash(ctx, "trip", a["trip"])
    with _raises(Forbidden, "not_your_trip"):  # the trip belongs to the owner
        async with tenant_tx(app_pool, agent, a["tenant"]) as ctx:
            await trash.move_to_trash(ctx, "trip", a["trip"])
    with _raises(Forbidden, "owner_only"):  # R8: clients are deleted by the owner only
        async with tenant_tx(app_pool, agent, a["tenant"]) as ctx:
            await trash.move_to_trash(ctx, "traveler", a["lead"])
    with _raises(Conflict, "traveler_on_trip") as err:
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            await trash.move_to_trash(ctx, "traveler", a["lead"])
    assert err.value.extra == {"alternative": "archive"}
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await policy.update_settings(ctx, trash_days=3, auto_archive_months=None)
        moved = await trash.move_to_trash(ctx, "trip", a["trip"])
        await trash.move_to_trash(ctx, "traveler", a["lead"])  # no live trip any more
    rows = await _sql(app_pool, a["tenant"], "SELECT purge_after - deleted_at AS w FROM crm.trip")
    assert rows[0]["w"].days == 3 and moved["purge_after"]
    with _raises(Forbidden, "owner_only"):  # the agent did not delete it
        async with tenant_tx(app_pool, agent, a["tenant"]) as ctx:
            await trash.restore(ctx, "trip", a["trip"], None)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        assert (await trash.restore(ctx, "trip", a["trip"], None))["restored"] is True  # no AA line: no Booking call
        with _raises(NotFound, "not_in_trash"):
            await trash.restore(ctx, "trip", a["trip"], None)
    denied = [e for e in await _events(admin, a["tenant"], "delete") if e["outcome"] == "denied"]
    assert sorted(e["reason"] for e in denied) == ["not_allowed_for_role", "not_your_trip", "owner_only"]


# ── Erasure, holds ────────────────────────────────────────────────────────────────────────────────
async def test_erasure_bypasses_the_trash_and_respects_holds(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    store = MemoryDocumentStore()
    a = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await trash.move_to_trash(ctx, "trip", a["trip"])
        await documents.upload(
            ctx,
            Vault(keys, ctx),
            store,
            traveler_id=a["lead"],
            trip_id=None,
            kind="other",
            filename="id.jpg",
            content_type="image/jpeg",
            content=JPEG,
        )
        hold = await policy.place_hold(ctx, "traveler", a["lead"], "dispute")
        with _raises(Conflict, "hold_exists"):
            await policy.place_hold(ctx, "traveler", a["lead"], "dispute")
    with _raises(Conflict, "legal_hold"):
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            await erase.erase(ctx, "traveler", a["lead"], dry_run=False, vault=Vault(keys, ctx), store=store)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await policy.release_hold(ctx, uuid.UUID(hold["hold_id"]))
        # Not in the trash itself (only its trip is): erasure does not wait for any window.
        counts = await erase.erase(ctx, "traveler", a["lead"], dry_run=False, vault=Vault(keys, ctx), store=store)
    assert counts["traveler"] == 1 and counts["consent"] >= 1 and counts["document_object"] == 1
    assert store.objects == {}
    assert await _sql(app_pool, a["tenant"], "SELECT 1 FROM crm.consent WHERE traveler_id = $1", a["lead"]) == []


async def test_a_held_trip_or_client_is_not_moved_to_the_trash(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """Review S249: a trip or client a hold reaches answers 409 legal_hold (a committed denied event) and stays live;
    released, it goes to the trash as usual."""
    keys = _keys()
    a = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        hold = await policy.place_hold(ctx, "traveler", a["lead"], "dispute")  # the lead's hold reaches their trip
    for kind, subject in (("trip", a["trip"]), ("traveler", a["lead"])):
        with _raises(Conflict, "legal_hold") as err:
            async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
                await trash.move_to_trash(ctx, kind, subject)
        assert err.value.status_code == 409
    events = await _events(admin, a["tenant"], "delete")
    assert [(e["object_kind"], e["outcome"], e["reason"]) for e in events] == [
        ("trip", "denied", "legal_hold"),
        ("traveler", "denied", "legal_hold"),
    ]
    rows = await _sql(app_pool, a["tenant"], "SELECT deleted_at FROM crm.trip WHERE trip_id = $1", a["trip"])
    assert rows[0]["deleted_at"] is None
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await policy.release_hold(ctx, uuid.UUID(hold["hold_id"]))
        await trash.move_to_trash(ctx, "trip", a["trip"])


async def test_daily_run_redacts_purges_and_archives(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    a = await _inquiry(app_pool, keys)
    b = await _inquiry(app_pool, keys)
    for t in (a["tenant"], b["tenant"]):
        await _sql(app_pool, t, "UPDATE crm.form_submission SET redact_after = now() - interval '1 day'")
        await _sql(
            app_pool,
            t,
            "INSERT INTO crm.llm_call (at, stage, role, provider, model, outcome) VALUES "
            "(now() - interval '200 days', 'crm_test', 'writer', 'stub', 'stub-1', 'ok')",
        )
    old = uuid.uuid4()
    await _sql(
        app_pool,
        a["tenant"],
        "INSERT INTO crm.traveler (traveler_id, display_name_enc, updated_at) "
        "VALUES ($1, $2, now() - interval '4 years')",
        old,
        b"\x00\x01" + b"x" * 40,
    )
    async with tenant_tx(app_pool, b["owner"], b["tenant"]) as ctx:  # agency b is on hold: nothing deleted there
        await policy.place_hold(ctx, "tenant", None, "litigation")
    result = await build_lifecycle(dry_run=False, keys=keys)(app_pool, Job("j3", "crm_lifecycle", {}, 1, 3))
    assert result["failed_count"] == 0
    [sub] = await _sql(app_pool, a["tenant"], "SELECT payload_enc, payload_redacted_at FROM crm.form_submission")
    assert sub["payload_enc"] is None and sub["payload_redacted_at"] is not None
    [kept] = await _sql(app_pool, b["tenant"], "SELECT payload_enc FROM crm.form_submission")
    assert kept["payload_enc"] is not None
    assert len(await _sql(app_pool, a["tenant"], "SELECT 1 FROM crm.llm_call")) == 0
    assert len(await _sql(app_pool, b["tenant"], "SELECT 1 FROM crm.llm_call")) == 1
    [row] = await _sql(
        app_pool, a["tenant"], "SELECT archive_reason, archived_by FROM crm.traveler WHERE traveler_id = $1", old
    )
    assert (row["archive_reason"], row["archived_by"]) == ("auto_inactive", "job:crm_lifecycle")
    [lead] = await _sql(app_pool, a["tenant"], "SELECT archived_at FROM crm.traveler WHERE traveler_id = $1", a["lead"])
    assert lead["archived_at"] is None  # active client on an open trip
    [run_b] = await _sql(app_pool, b["tenant"], "SELECT counts FROM crm.lifecycle_run")
    counts_b = json.loads(run_b["counts"])
    assert counts_b["submission_held"] == 1 and counts_b["llm_call_held"] == 1
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        view = await policy.settings(ctx)
    assert view == {"trash_days": 7, "auto_archive_months": 36}


async def test_a_repeat_inquiry_unarchives_the_client(app_pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    keys = _keys()
    a = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await policy.archive(ctx, a["lead"], "other")
    assert (await intake.submit(app_pool, keys, SETTINGS, a["form"].public_key, answers(), origin=None)).stored
    await _process_all(app_pool, keys, a["tenant"])
    [lead] = await _sql(app_pool, a["tenant"], "SELECT archived_at FROM crm.traveler WHERE traveler_id = $1", a["lead"])
    assert lead["archived_at"] is None
    assert [e["reason"] for e in await _events(admin, a["tenant"], "unarchive")] == ["repeat_inquiry"]


# ── Offboarding ───────────────────────────────────────────────────────────────────────────────────
async def test_offboarding_exports_then_erases_and_shreds_the_keys(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    keys = _keys()
    store = MemoryDocumentStore()
    a = await _inquiry(app_pool, keys, "owner", "agent")
    other = await _inquiry(app_pool, keys)
    (_, _), (agent, _) = a["people"]
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await documents.upload(
            ctx,
            Vault(keys, ctx),
            store,
            traveler_id=a["lead"],
            trip_id=a["trip"],
            kind="other",
            filename="ticket.jpg",
            content_type="image/jpeg",
            content=JPEG,
        )
    with _raises(Forbidden, "owner_only"):
        async with tenant_tx(app_pool, agent, a["tenant"]) as ctx:
            await offboarding.request(ctx, export_waived=False)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        req = await offboarding.request(ctx, export_waived=False)
        with _raises(Conflict, "offboarding_open"):
            await offboarding.request(ctx, export_waived=False)
    assert req["status"] == "requested" and req["dry_run_counts"]["traveler"] == 2
    request_id = uuid.UUID(req["request_id"])
    with _raises(Conflict, "export_not_ready"):
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            await offboarding.confirm_erase(ctx, request_id, confirm_tenant_id=a["tenant"], dry_run=False)

    handler = offboarding.build_handler(keys=keys, store=store, dry_run=False, dek_cache_ttl_s=300)
    [export_job] = await _jobs(app_pool, "crm_offboard", a["tenant"])
    assert (await handler(app_pool, export_job))["status"] == "export_ready"
    await _done(app_pool, export_job)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        content = await offboarding.download(ctx, Vault(keys, ctx), store, request_id)
    lines = [json.loads(x) for x in content.decode("utf-8").splitlines()]
    assert lines[0]["format"] == 1 and lines[0]["tenant_id"] == str(a["tenant"])
    names = {line["row"].get("display_name") for line in lines[1:] if line["table"] == "traveler"}
    assert "Nguyễn Thị Hoa" in names
    assert not any(k.endswith("_bidx") or k.endswith("_enc") for line in lines[1:] for k in line["row"])
    export_key = offboarding.export_key(a["tenant"], request_id)
    sealed_export = store.objects[export_key]

    with _raises(Invalid, "confirmation_mismatch"):
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            await offboarding.confirm_erase(ctx, request_id, confirm_tenant_id=other["tenant"], dry_run=False)
    with _raises(Conflict, "deletion_dry_run") as err:
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            await offboarding.confirm_erase(ctx, request_id, confirm_tenant_id=a["tenant"], dry_run=True)
    assert err.value.extra["counts"]["member"] == 2
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        confirmed = await offboarding.confirm_erase(ctx, request_id, confirm_tenant_id=a["tenant"], dry_run=False)
    assert confirmed["status"] == "erasing"
    with _raises(Forbidden, "tenant_locked"):  # members are out from the confirmation on
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            pass

    [erase_job] = await _jobs(app_pool, "crm_offboard", a["tenant"])
    result = await handler(app_pool, erase_job)
    assert result["status"] == "erased" and result["keys_destroyed_count"] >= 1
    rows = await _owner_fetch(admin, a["tenant"], "SELECT status FROM crm.tenant")
    assert rows[0]["status"] == "offboarded"
    [req_row] = await _owner_fetch(
        admin, a["tenant"], "SELECT status, receipt, backup_purge_by FROM crm.erasure_request"
    )
    receipt = json.loads(req_row["receipt"])
    assert req_row["status"] == "erased" and receipt["counts"]["traveler"] == 2
    assert receipt["backup_purge_by"] == req_row["backup_purge_by"].isoformat() and receipt["dek_cache_expired_by"]
    for table in ("traveler", "trip", "member", "consent", "form_submission", "document", "note", "llm_call"):
        assert await _owner_fetch(admin, a["tenant"], f"SELECT 1 FROM crm.{table}") == [], table  # noqa: S608
    keys_left = await _owner_fetch(admin, a["tenant"], "SELECT wrapped_dek, destroyed_at FROM crm.tenant_key")
    assert keys_left and all(k["wrapped_dek"] is None and k["destroyed_at"] is not None for k in keys_left)
    assert not [k for k in store.objects if k.startswith(f"tenants/{a['tenant']}/")], "documents erased"
    assert export_key not in store.objects
    assert await _owner_fetch(admin, a["tenant"], "SELECT 1 FROM crm.audit_event") != []  # the audit stays
    # Crypto-shred: the agency's keys no longer open, so a copy of the export (or of any value) is unreadable.
    with pytest.raises(KeyDestroyed):
        async with app_pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(a["tenant"]))
            await Vault(keys, _KeyCtx(conn, a["tenant"])).decrypt_bytes(  # type: ignore[arg-type]
                "erasure_request", "export", request_id, sealed_export, purpose="export"
            )
    assert sealed_export
    # Another agency is untouched.
    assert len(await _sql(app_pool, other["tenant"], "SELECT 1 FROM crm.traveler")) == 2


async def test_any_hold_suspends_offboarding_and_a_refused_erase_gives_the_agency_back(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """Review S249: a trip or client hold (not only an agency hold) refuses the request, the confirmation and the erase
    job; a refused erase deleted nothing, so the agency is active again and the owner cancels the failed request."""
    keys = _keys()
    store = MemoryDocumentStore()
    a = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        trip_hold = await policy.place_hold(ctx, "trip", a["trip"], "dispute")
    with _raises(Conflict, "legal_hold"):
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            await offboarding.request(ctx, export_waived=True)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await policy.release_hold(ctx, uuid.UUID(trip_hold["hold_id"]))
        req = await offboarding.request(ctx, export_waived=True)
        client_hold = await policy.place_hold(ctx, "traveler", a["lead"], "litigation")
    request_id = uuid.UUID(req["request_id"])
    with _raises(Conflict, "legal_hold"):
        async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
            await offboarding.confirm_erase(ctx, request_id, confirm_tenant_id=a["tenant"], dry_run=False)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await policy.release_hold(ctx, uuid.UUID(client_hold["hold_id"]))
        assert (await offboarding.confirm_erase(ctx, request_id, confirm_tenant_id=a["tenant"], dry_run=False))[
            "status"
        ] == "erasing"
    # AA places a client hold after the confirmation (no member context opens on an offboarding agency).
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(a["tenant"]))
        await conn.execute("SELECT set_config('app.actor', 'platform:l1', true)")
        await conn.execute(
            "INSERT INTO crm.legal_hold (scope, subject_id, reason_code) VALUES ('traveler', $1, 'regulator_request')",
            a["lead"],
        )
    handler = offboarding.build_handler(keys=keys, store=store, dry_run=False, dek_cache_ttl_s=300)
    [erase_job] = await _jobs(app_pool, "crm_offboard", a["tenant"])
    with pytest.raises(PermanentError, match="legal_hold"):
        await handler(app_pool, erase_job)
    [tenant_row] = await _owner_fetch(admin, a["tenant"], "SELECT status FROM crm.tenant")
    assert tenant_row["status"] == "active", "nothing was deleted: the agency is given back"
    [req_row] = await _owner_fetch(admin, a["tenant"], "SELECT status, failure_class FROM crm.erasure_request")
    assert (req_row["status"], req_row["failure_class"]) == ("failed", "legal_hold")
    assert len(await _sql(app_pool, a["tenant"], "SELECT 1 FROM crm.traveler")) == req["dry_run_counts"]["traveler"]
    keys_left = await _owner_fetch(admin, a["tenant"], "SELECT destroyed_at FROM crm.tenant_key")
    assert keys_left and all(k["destroyed_at"] is None for k in keys_left), "no crypto-shred on a refused erase"
    denied = [e for e in await _events(admin, a["tenant"], "offboarding_erase") if e["outcome"] == "denied"]
    assert [e["reason"] for e in denied] == ["legal_hold", "legal_hold"]  # the confirmation, then the job
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        cancelled = await offboarding.cancel(ctx, request_id, store)
    assert cancelled["status"] == "cancelled"


async def test_an_erase_job_in_dry_run_is_refused_and_gives_the_agency_back(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """Review S249: LIFECYCLE_DRY_RUN switched on after the confirmation. The erase job deletes nothing and treats it
    like a refusal: request failed (deletion_dry_run), agency active again, the owner cancels."""
    keys = _keys()
    store = MemoryDocumentStore()
    a = await _inquiry(app_pool, keys)
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        req = await offboarding.request(ctx, export_waived=True)
        request_id = uuid.UUID(req["request_id"])
        await offboarding.confirm_erase(ctx, request_id, confirm_tenant_id=a["tenant"], dry_run=False)
    handler = offboarding.build_handler(keys=keys, store=store, dry_run=True, dek_cache_ttl_s=300)
    [erase_job] = await _jobs(app_pool, "crm_offboard", a["tenant"])
    with pytest.raises(PermanentError, match="deletion_dry_run"):
        await handler(app_pool, erase_job)
    [tenant_row] = await _owner_fetch(admin, a["tenant"], "SELECT status FROM crm.tenant")
    assert tenant_row["status"] == "active"
    [req_row] = await _owner_fetch(admin, a["tenant"], "SELECT status, failure_class FROM crm.erasure_request")
    assert (req_row["status"], req_row["failure_class"]) == ("failed", "deletion_dry_run")
    assert len(await _sql(app_pool, a["tenant"], "SELECT 1 FROM crm.traveler")) == req["dry_run_counts"]["traveler"]
    denied = [e for e in await _events(admin, a["tenant"], "offboarding_erase") if e["outcome"] == "denied"]
    assert [e["reason"] for e in denied] == ["deletion_dry_run"]
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        assert (await offboarding.cancel(ctx, request_id, store))["status"] == "cancelled"


@pytest.mark.parametrize("held_by", ["client_hold_on_the_lead", "trip_hold"])
async def test_n1_removing_someone_from_a_held_party_is_409_legal_hold(
    app_pool: asyncpg.Pool, admin: asyncpg.Connection, held_by: str
) -> None:
    """Re-review S249 round 4 (n1 / n1b): remove_from_trip is a plain DELETE of party rows; the hold guard trigger
    refuses it on a held trip (a client hold on a party member, or a trip hold) and the API answers 409 legal_hold
    with a denied event; the trip stays held. Released, the removal goes through. Deleting one of its documents is
    refused the same way."""
    from app.travellers import service as travellers

    keys = _keys()
    store = MemoryDocumentStore()
    a = await _inquiry(app_pool, keys)
    companion = uuid.uuid4()
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await ctx.conn.execute(
            "INSERT INTO crm.traveler (traveler_id, display_name_enc) VALUES ($1, $2)", companion, b"\x00" * 40
        )
        await ctx.conn.execute(
            "INSERT INTO crm.trip_traveler (trip_id, traveler_id, role) VALUES ($1, $2, 'companion')",
            a["trip"],
            companion,
        )
        doc = await documents.upload(
            ctx,
            Vault(keys, ctx),
            store,
            traveler_id=companion,
            trip_id=a["trip"],
            kind="ticket",
            filename="ticket.jpg",
            content_type="image/jpeg",
            content=JPEG,
        )
        scope, subject = ("traveler", a["lead"]) if held_by == "client_hold_on_the_lead" else ("trip", a["trip"])
        hold = await policy.place_hold(ctx, scope, subject, "dispute")
    for attempt in ("party", "document"):
        with _raises(Conflict, "legal_hold") as err:
            async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
                if attempt == "party":
                    await travellers.remove_from_trip(ctx, Vault(keys, ctx), a["trip"], companion)
                else:
                    await documents.trash(ctx, uuid.UUID(doc["document_id"]))
        assert err.value.status_code == 409
    rows = await _sql(app_pool, a["tenant"], "SELECT 1 FROM crm.trip_traveler WHERE traveler_id = $1", companion)
    assert len(rows) == 1
    denied = [e for e in await _events(admin, a["tenant"], "change") if e["outcome"] == "denied"]
    assert [e["reason"] for e in denied] == ["legal_hold", "legal_hold"]
    async with tenant_tx(app_pool, a["owner"], a["tenant"]) as ctx:
        await policy.release_hold(ctx, uuid.UUID(hold["hold_id"]))
        assert (await travellers.remove_from_trip(ctx, Vault(keys, ctx), a["trip"], companion))["removed"]
