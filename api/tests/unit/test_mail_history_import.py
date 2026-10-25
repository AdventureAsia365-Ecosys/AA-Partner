"""AA-945 mail history import: mbox and CSV parsing, stable ids (a re-import adds nothing), the job's counts and the
upload record."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.errors import Invalid, NotFound
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.mail import history_import
from app.mail.store import MemoryIngestStore
from tests.unit.fakes import ScriptedConn
from tests.unit.mail_fakes import INGEST_DOMAIN, MailVault, raw_mail
from tests.unit.slice_fakes import MEMBER, OTHER_MEMBER, TENANT, system_ctx, tenant_ctx

IMPORT = uuid.UUID("00000000-0000-4000-8000-00000000f001")
NOW = __import__("datetime").datetime(2026, 10, 25, tzinfo=__import__("datetime").UTC)
CSV = (
    "date,from,to,subject,body\n"
    '"Sat, 24 Oct 2026 09:00:00 +0700",lan@example.test,hello@sunshine.example,Hoi An,"Hello, March?"\n'
    "2026-10-24,not-an-address,,x,y\n"
    ",minh@example.test,,Chào,Xin chào\n"
).encode()


def _mbox() -> bytes:
    one = raw_mail(message_id="<m1@x.test>")
    two = raw_mail(message_id=None, subject="No id")
    return b"From a@x Sat Oct 24 09:00:00 2026\n" + one + b"\nFrom b@x Sat Oct 24 10:00:00 2026\n" + two + b"\n"


def test_mbox_messages_get_stable_ids() -> None:
    first = list(history_import.mbox_messages(_mbox(), INGEST_DOMAIN))
    again = list(history_import.mbox_messages(_mbox(), INGEST_DOMAIN))
    assert len(first) == 2 and all(m is not None for m in first)
    assert first[0].message_id == "m1@x.test"  # type: ignore[union-attr]
    assert first[1].message_id.endswith("@import.crm.invalid")  # type: ignore[union-attr]
    assert [m.message_id_hash for m in first] == [m.message_id_hash for m in again]  # type: ignore[union-attr]


def test_csv_messages() -> None:
    rows = list(history_import.csv_messages(CSV))
    assert rows[1] is None and rows[0] is not None and rows[2] is not None
    assert rows[0].from_address == "lan@example.test" and rows[0].to_addresses == ("hello@sunshine.example",)
    assert rows[0].date is not None and rows[2].date is None and rows[2].subject == "Chào"
    assert rows[0].message_id == list(history_import.csv_messages(CSV))[0].message_id  # type: ignore[union-attr]
    with pytest.raises(PermanentError):
        list(history_import.csv_messages(b"a,b\n1,2\n"))


@pytest.mark.asyncio
async def test_create_and_status() -> None:
    store = MemoryIngestStore()
    conn = ScriptedConn().on("INSERT INTO crm.mail_import", NOW)
    out = await history_import.create_import(tenant_ctx(conn), store, source_kind="csv", data=CSV)
    import_id = uuid.UUID(out["mail_import_id"])
    assert store.objects[f"imports/{TENANT}/{import_id}"] == CSV
    (job,) = conn.sql("INSERT INTO crm.job")
    assert job[0] == "crm_mail_history_import"
    for kind, data in (("pst", CSV), ("csv", b"")):
        with pytest.raises(Invalid):
            await history_import.create_import(tenant_ctx(), store, source_kind=kind, data=data)
    row = {
        "mail_import_id": IMPORT,
        "member_id": OTHER_MEMBER,
        "source_kind": "csv",
        "size_bytes": 10,
        "status": "done",
        "total_count": 3,
        "imported_count": 2,
        "duplicate_count": 0,
        "skipped_count": 1,
        "failed_count": 0,
        "error_code": None,
        "created_at": NOW,
        "finished_at": NOW,
    }
    owner = ScriptedConn().on("FROM crm.mail_import WHERE", row)
    assert (await history_import.import_status(tenant_ctx(owner), IMPORT))["imported_count"] == 2
    with pytest.raises(NotFound):
        await history_import.import_status(tenant_ctx(owner, role="agent", member=MEMBER), IMPORT)


@pytest.mark.asyncio
async def test_the_job_imports_counts_and_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryIngestStore()
    key = history_import.object_key(TENANT, IMPORT)
    store.objects[key] = CSV
    conn = ScriptedConn()
    conn.on("FROM crm.mail_import WHERE", {"status": "queued", "source_kind": "csv"})
    conn.on("INSERT INTO crm.conversation", uuid.uuid4())
    answers = iter([uuid.uuid4(), None])  # the first row is new, the third a duplicate
    conn.on("INSERT INTO crm.message", lambda *a: next(answers))

    @asynccontextmanager
    async def fake_tx(*_: Any, **__: Any):  # noqa: ANN202
        yield system_ctx(conn)

    monkeypatch.setattr(history_import, "system_tx", fake_tx)
    monkeypatch.setattr(history_import, "Vault", lambda keys, ctx: MailVault())
    job = Job("j", "crm_mail_history_import", {"tenant_id": str(TENANT), "mail_import_id": str(IMPORT)}, 1, 3)
    handler = history_import.build_handler(store, None, object(), INGEST_DOMAIN)  # type: ignore[arg-type]
    result = await handler(object(), job)  # type: ignore[arg-type]
    check_payload(result, what="result")
    assert result["status"] == "done" and result["total_count"] == 3 and result["stored_count"] == 1
    assert result["duplicate_count"] == 1 and result["skipped_count"] == 1 and key not in store.objects
    assert conn.sql("SET status = $2")[0][1] == "done"
    with pytest.raises(PermanentError):
        await handler(object(), Job("j", "crm_mail_history_import", {}, 1, 3))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await history_import.build_handler(None, None, None, INGEST_DOMAIN)(object(), job)  # type: ignore[arg-type]
    conn.on("FROM crm.mail_import WHERE", {"status": "done", "source_kind": "csv"})
    assert (await handler(object(), job))["status"] == "skipped"  # type: ignore[arg-type]
    store.objects[key] = b"a,b\n1,2\n"
    conn.on("FROM crm.mail_import WHERE", {"status": "running", "source_kind": "csv"})
    failed = await handler(object(), job)  # type: ignore[arg-type]
    assert failed == {"mail_import_id": str(IMPORT), "status": "failed", "reason_code": "csv_columns"}
    store.objects[key] = _mbox()
    conn.on("FROM crm.mail_import WHERE", {"status": "queued", "source_kind": "mbox"})
    conn.on("INSERT INTO crm.message", RuntimeError("db"))
    done = await handler(object(), job)  # type: ignore[arg-type]
    assert done["failed_count"] == 2


def _csv_rows(n: int) -> bytes:
    lines = ["date,from,to,subject,body"] + [f",lan{i}@example.test,,S{i},Body {i}" for i in range(n)]
    return ("\n".join(lines) + "\n").encode()


@pytest.mark.asyncio
async def test_a_large_import_runs_one_slice_per_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review S250: the worker runs one job at a time, so an import of thousands of messages must not hold it: each
    job stores one slice and queues the next (ids and the slice number only); the object goes with the last slice."""
    monkeypatch.setattr(history_import, "SLICE", 3)
    monkeypatch.setattr(history_import, "CHUNK", 2)
    store = MemoryIngestStore()
    key = history_import.object_key(TENANT, IMPORT)
    store.objects[key] = _csv_rows(7)
    conn = ScriptedConn()
    conn.on("FROM crm.mail_import WHERE", {"status": "running", "source_kind": "csv"})
    conn.on("INSERT INTO crm.conversation", uuid.uuid4())
    conn.on("INSERT INTO crm.message", lambda *a: a[0])
    conn.on("INSERT INTO crm.job", "job-next")

    @asynccontextmanager
    async def fake_tx(*_: Any, **__: Any):  # noqa: ANN202
        yield system_ctx(conn)

    monkeypatch.setattr(history_import, "system_tx", fake_tx)
    monkeypatch.setattr(history_import, "Vault", lambda keys, ctx: MailVault())
    handler = history_import.build_handler(store, None, object(), INGEST_DOMAIN)  # type: ignore[arg-type]
    payload = {"tenant_id": str(TENANT), "mail_import_id": str(IMPORT)}
    results = []
    for batch in range(3):
        job = Job("j", "crm_mail_history_import", payload | ({"batch": batch} if batch else {}), 1, 3)
        result = await handler(conn, job)  # type: ignore[arg-type]
        check_payload(result, what="result")
        results.append(result)
    assert [r["status"] for r in results] == ["running", "running", "done"]
    assert [r["stored_count"] for r in results] == [3, 3, 1]
    jobs = conn.sql("INSERT INTO crm.job")
    assert [j[2] for j in jobs] == [f"crm_mail_history_import:{IMPORT}:1", f"crm_mail_history_import:{IMPORT}:2"]
    check_payload(__import__("json").loads(jobs[0][1]))
    # The counts are added per chunk (deltas), never overwritten by a later slice.
    assert sum(c[1] for c in conn.sql("SET total_count = total_count + $2")) == 7
    assert key not in store.objects


@pytest.mark.asyncio
async def test_the_last_failed_attempt_marks_the_import_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryIngestStore()
    key = history_import.object_key(TENANT, IMPORT)
    store.objects[key] = _csv_rows(2)
    conn = ScriptedConn().on("FROM crm.mail_import WHERE", {"status": "running", "source_kind": "csv"})

    @asynccontextmanager
    async def fake_tx(*_: Any, **__: Any):  # noqa: ANN202
        yield system_ctx(conn)

    async def boom(*_: Any, **__: Any) -> Any:
        raise ConnectionError("db gone")

    monkeypatch.setattr(history_import, "system_tx", fake_tx)
    monkeypatch.setattr(history_import, "_import", boom)
    handler = history_import.build_handler(store, None, object(), INGEST_DOMAIN)  # type: ignore[arg-type]
    payload = {"tenant_id": str(TENANT), "mail_import_id": str(IMPORT)}
    with pytest.raises(ConnectionError):
        await handler(object(), Job("j", "crm_mail_history_import", payload, 1, 3))  # type: ignore[arg-type]
    assert conn.sql("SET status = $2") == [] and key in store.objects  # retried: nothing decided yet
    with pytest.raises(ConnectionError):
        await handler(object(), Job("j", "crm_mail_history_import", payload, 3, 3))  # type: ignore[arg-type]
    assert conn.sql("SET status = $2") == [(IMPORT, "failed", "import_failed")] and key not in store.objects
    with pytest.raises(PermanentError):
        await handler(object(), Job("j", "crm_mail_history_import", payload | {"batch": -1}, 1, 3))  # type: ignore[arg-type]
