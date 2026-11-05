"""AA-969 part 2 (root ADR 0019 d16) without a database: the pilot marker read, the nightly pilot backup (queue,
busy, the handler, the schedule) and the pure parts of the restore drill (types, checksums, digests). The same rules on
PostgreSQL: tests/integration/test_aa969_pilot.py."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import uuid
import zipfile
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest

from app.errors import Forbidden, Unavailable
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job
from app.lifecycle import pilot
from app.portability import exports
from app.portability import restore_drill as rd
from app.tenancy import LIFECYCLE_ACTORS, LifecycleContext
from tests.unit.fakes import ScriptedConn

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
OTHER = uuid.UUID("00000000-0000-4000-8000-0000000000b2")
EXPORT = uuid.UUID("00000000-0000-4000-8000-0000000000e1")
JOB = Job("j", "crm_pilot_backup", {"purpose": "daily_schedule"}, 1, 3)


def ctx(conn: ScriptedConn | None = None, tenant: uuid.UUID = TENANT) -> LifecycleContext:
    return LifecycleContext(conn=conn or ScriptedConn(), tenant_id=tenant, actor=pilot.ACTOR, status="active")


def test_the_actors_and_the_owner_list() -> None:
    assert {pilot.ACTOR, rd.ACTOR} <= LIFECYCLE_ACTORS
    assert pilot.ACTOR == exports.PILOT_BACKUP_ACTOR == "job:crm_pilot_backup"
    # the nightly backups are AA's restore copies: the owner's list leaves them out
    assert "requested_by <> 'job:crm_pilot_backup'" in " ".join(exports.LIST_SQL.split())


@pytest.mark.asyncio
async def test_is_pilot_reads_the_marker() -> None:
    conn = ScriptedConn().on("SELECT is_pilot FROM crm.tenant", True)
    assert await pilot.is_pilot(ctx(conn)) is True
    assert conn.sql("SELECT is_pilot") == [(TENANT,)]
    assert await pilot.is_pilot(ctx()) is False


@pytest.mark.asyncio
async def test_queue_backup_queues_one_export_and_its_job() -> None:
    conn = ScriptedConn().on("INSERT INTO crm.export_job", EXPORT).on("INSERT INTO crm.job", "job-1")
    c = ctx(conn)
    assert await pilot.queue_backup(c) == "queued"
    assert conn.sql("INSERT INTO crm.export_job") == [("tenant", TENANT, None, 1)]
    (job,) = conn.sql("INSERT INTO crm.job")
    assert job[0] == "crm_data_export" and json.loads(job[1]) == {"tenant_id": str(TENANT), "export_id": str(EXPORT)}
    assert job[2] == f"crm_data_export:{EXPORT}"
    assert [(e.action, e.reason) for e in c.audit_events] == [("export_request", "pilot_backup")]


@pytest.mark.asyncio
async def test_queue_backup_is_busy_while_an_export_is_built() -> None:
    conn = ScriptedConn().on("INSERT INTO crm.export_job", asyncpg.UniqueViolationError("export_job_open_uq"))
    c = ctx(conn)
    assert await pilot.queue_backup(c) == "busy"
    assert not conn.sql("INSERT INTO crm.job") and not c.audit_events


@pytest.mark.asyncio
async def test_pilot_agencies_reads_each_agency_in_its_own_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    gone = uuid.uuid4()
    agencies = [type("A", (), {"tenant_id": t})() for t in (OTHER, gone, TENANT)]

    async def listed(_pool: Any) -> list[Any]:
        return agencies

    @asynccontextmanager
    async def tx(_pool: Any, tenant_id: uuid.UUID, **kw: Any):  # noqa: ANN202
        assert kw == {"actor": pilot.ACTOR}
        if tenant_id == gone:
            raise Forbidden("tenant_status")
        yield ctx(ScriptedConn().on("SELECT is_pilot", tenant_id == TENANT), tenant_id)

    monkeypatch.setattr(pilot.registry, "agencies", listed)
    monkeypatch.setattr(pilot, "lifecycle_tx", tx)
    assert await pilot.pilot_agencies(object()) == [TENANT]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_backup_handler_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = iter(["queued"])

    async def found(_pool: Any) -> list[uuid.UUID]:
        return [TENANT]

    async def queued(_ctx: Any) -> str:
        return next(outcomes)

    @asynccontextmanager
    async def tx(_pool: Any, tenant_id: uuid.UUID, **_: Any):  # noqa: ANN202
        yield ctx(tenant=tenant_id)

    monkeypatch.setattr(pilot, "pilot_agencies", found)
    monkeypatch.setattr(pilot, "queue_backup", queued)
    monkeypatch.setattr(pilot, "lifecycle_tx", tx)
    result = await pilot.build_handler()(object(), JOB)  # type: ignore[arg-type]
    assert result == {"pilot_count": 1, "queued_count": 1, "busy_count": 0}
    check_payload(result, what="result")

    async def none(_pool: Any) -> list[uuid.UUID]:
        return []

    monkeypatch.setattr(pilot, "pilot_agencies", none)
    assert await pilot.build_handler()(object(), JOB) == {"pilot_count": 0, "queued_count": 0, "busy_count": 0}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_nightly_schedule_is_due_now_once_a_day() -> None:
    clock = [dt.datetime(2026, 11, 5, 5, 0, tzinfo=dt.UTC)]
    schedule = pilot.DailyPilotBackupSchedule(19, lambda: clock[0])
    pool = ScriptedConn().on("INSERT INTO crm.job", "job-1")
    assert await schedule(pool) is None  # type: ignore[arg-type]
    clock[0] = dt.datetime(2026, 11, 5, 19, 2, tzinfo=dt.UTC)
    assert await schedule(pool) == "job-1"  # type: ignore[arg-type]
    assert await schedule(pool) is None  # type: ignore[arg-type]
    (job,) = pool.sql("INSERT INTO crm.job")
    assert job[0] == "crm_pilot_backup" and job[2] == "crm_pilot_backup:schedule:2026-11-05"
    with pytest.raises(ValueError):
        pilot.DailyPilotBackupSchedule(24)


# ── the restore drill's pure parts ───────────────────────────────────────────────────────────────────────────────


def _zip(files: dict[str, bytes], *, break_sum: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        sums = []
        for path, data in files.items():
            zf.writestr(path, data)
            digest = hashlib.sha256(data).hexdigest()
            sums.append(f"{'0' * 64 if path == break_sum else digest}  {path}\n")
        zf.writestr("SHA256SUMS", "".join(sums))
    return buf.getvalue()


MANIFEST = json.dumps({"schema_version": 1, "tables": [{"table": "note", "rows": 1, "json": "json/note.json"}]})


def test_types_and_identifiers() -> None:
    assert rd.ident('we"ird') == '"we""ird"'
    assert rd.kept_type("uuid") == "uuid" and rd.kept_type("uuid[]") == "uuid[]"
    assert rd.kept_type("character varying(120)") == "character varying(120)"
    assert rd.kept_type("numeric(12,2)") == "numeric(12,2)"
    assert rd.kept_type("timestamp with time zone") == "timestamp with time zone"
    for other in ("bytea", "interval", "crm.some_enum", None):
        assert rd.kept_type(other) == "text"


def test_verify_zip_checks_every_listed_file() -> None:
    files = {"manifest.json": MANIFEST.encode(), "json/note.json": b"[]"}
    zf, manifest, checked = rd.verify_zip(_zip(files))
    assert checked == 2 and manifest["tables"][0]["table"] == "note" and zf.read("json/note.json") == b"[]"
    with pytest.raises(rd.DrillError, match="checksum_mismatch"):
        rd.verify_zip(_zip(files, break_sum="json/note.json"))
    with pytest.raises(rd.DrillError, match="zip_shape"):
        rd.verify_zip(b"not a zip")
    with pytest.raises(rd.DrillError, match="zip_shape"):
        rd.verify_zip(_zip({"json/note.json": b"[]"}))  # no manifest
    with pytest.raises(rd.DrillError, match="zip_shape"):
        rd.verify_zip(_zip({"manifest.json": b'{"tables": 3}'}))
    broken = io.BytesIO()
    with zipfile.ZipFile(broken, "w") as zf2:
        zf2.writestr("manifest.json", MANIFEST)
        zf2.writestr("SHA256SUMS", f"{'1' * 64}  json/gone.json\n")
    with pytest.raises(rd.DrillError, match="checksum_missing_file"):
        rd.verify_zip(broken.getvalue())


def test_a_table_passes_when_restored_equals_the_manifest() -> None:
    assert rd.TableResult("note", 3, 3, 4, 3).ok
    assert not rd.TableResult("note", 3, 2, 3, 2).ok


class _Store:
    def __init__(self, data: bytes | None) -> None:
        self.data = data

    async def get(self, key: str) -> bytes:
        if self.data is None:
            raise Unavailable("document_missing")
        return self.data


class _Vault:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def decrypt_bytes(self, *_: Any, **__: Any) -> bytes:
        return self.content


@pytest.mark.asyncio
async def test_open_export_checks_both_digests() -> None:
    sealed, content = b"sealed", b"zip"
    row = {
        "export_job_id": EXPORT,
        "object_key": "k",
        "object_sha256": hashlib.sha256(sealed).hexdigest(),
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }
    assert await rd.open_export(ctx(), _Vault(content), _Store(sealed), row) == content  # type: ignore[arg-type]
    with pytest.raises(rd.DrillError, match="object_digest_mismatch"):
        await rd.open_export(ctx(), _Vault(content), _Store(b"other"), row)  # type: ignore[arg-type]
    with pytest.raises(rd.DrillError, match="object_missing"):
        await rd.open_export(ctx(), _Vault(content), _Store(None), row)  # type: ignore[arg-type]
    with pytest.raises(rd.DrillError, match="content_digest_mismatch"):
        await rd.open_export(ctx(), _Vault(b"changed"), _Store(sealed), row)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_export_row_must_be_ready() -> None:
    with pytest.raises(rd.DrillError, match="no_export"):
        await rd._export_row(ctx(), None)
    conn = ScriptedConn().on("WHERE export_job_id = $1", {"status": "expired", "object_key": None})
    with pytest.raises(rd.DrillError, match="export_not_ready"):
        await rd._export_row(ctx(conn), EXPORT)
    ready = {"status": "ready", "object_key": "k"}
    conn = ScriptedConn().on("requested_by = $1", ready)
    assert await rd._export_row(ctx(conn), None) == ready
    assert conn.sql("requested_by = $1") == [(pilot.ACTOR,)]


@pytest.mark.asyncio
async def test_pilot_tenant_needs_a_marked_agency(monkeypatch: pytest.MonkeyPatch) -> None:
    async def none(_pool: Any) -> list[uuid.UUID]:
        return []

    monkeypatch.setattr(rd.pilot, "pilot_agencies", none)
    with pytest.raises(rd.DrillError, match="no_pilot_agency"):
        await rd.pilot_tenant(object())  # type: ignore[arg-type]

    async def one(_pool: Any) -> list[uuid.UUID]:
        return [TENANT]

    monkeypatch.setattr(rd.pilot, "pilot_agencies", one)
    assert await rd.pilot_tenant(object()) == TENANT  # type: ignore[arg-type]
