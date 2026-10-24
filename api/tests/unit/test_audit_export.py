"""AA-940 audit export without a database: the object format and chain, the archive stores, the export / verify /
retention logic on scripted connections, the schedules and the settings (SQL and RLS: tests/integration)."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.audit_export import chain, export, registry, retention, verify
from app.audit_export.archive import (
    ArchiveMissing,
    MemoryAuditArchive,
    S3AuditArchive,
    build_audit_archive,
)
from app.errors import NotFound
from app.jobs.queue import Job
from app.settings import Settings, SettingsError
from app.tenancy import ExportContext
from tests.unit.fakes import ScriptedConn

TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
START = datetime(2026, 10, 24, 1, tzinfo=UTC)


def _event(i: int, at: datetime) -> dict[str, Any]:
    return {
        "event_id": uuid.UUID(int=i),
        "tenant_id": TENANT,
        "at": at,
        "actor_kind": "user",
        "actor_ref": "member:m",
        "action": "view",
        "outcome": "allowed",
        "object_kind": "trip",
        "object_id": f"t{i}",
        "fields": ["display_name"],
        "reason": None,
        "request_id": None,
        "object_ids": (),
        "created_by": "member:m",
    }


def test_build_object_is_deterministic_and_self_describing() -> None:
    events = [_event(1, START + timedelta(minutes=1)), _event(2, START + timedelta(minutes=2))]
    a = chain.build_object(TENANT, 1, START, START + chain.PERIOD, events, chain.ZERO_HASH)
    b = chain.build_object(TENANT, 1, START, START + chain.PERIOD, events, chain.ZERO_HASH)
    assert a == b and a.event_count == 2
    assert a.object_key == f"audit/v1/tenants/{TENANT}/2026/10/24/0100Z-0000000001.jsonl"
    assert chain.object_problems(a.body) == []
    parsed = chain.parse_object(a.body)
    assert parsed.manifest["seq"] == 1 and parsed.manifest_sha256 == a.manifest_sha256
    assert parsed.manifest["period_start"] == "2026-10-24T01:00:00.000000Z"
    nxt = chain.build_object(TENANT, 2, START + chain.PERIOD, START + 2 * chain.PERIOD, [], a.manifest_sha256)
    assert nxt.event_count == 0 and nxt.content_sha256 == chain.sha256_hex(b"")
    assert chain.parse_object(nxt.body).manifest["prev_manifest_sha256"] == a.manifest_sha256


def test_build_object_refuses_a_bad_chain_start() -> None:
    with pytest.raises(ValueError):
        chain.build_object(TENANT, 1, START, START + chain.PERIOD, [], "1" * 64)
    with pytest.raises(ValueError):
        chain.build_object(TENANT, 2, START, START + chain.PERIOD, [], chain.ZERO_HASH)
    with pytest.raises(ValueError):
        chain.iso(datetime(2026, 1, 1))


def test_object_problems_name_what_changed() -> None:
    obj = chain.build_object(TENANT, 1, START, START + chain.PERIOD, [_event(1, START)], chain.ZERO_HASH)
    head, _, content = obj.body.partition(b"\n")
    assert chain.object_problems(head + b"\n") == ["content_hash", "event_count"]  # the event line removed
    forged = head.replace(b'"seq":1', b'"seq":7') + b"\n" + content
    assert chain.object_problems(forged) == ["manifest_hash"]
    assert chain.object_problems(b"not json") == ["object_unreadable"]
    assert chain.object_problems(b'{"x":1}\n') == ["object_unreadable"]
    assert chain.hour_floor(START + timedelta(minutes=59, seconds=3)) == START


@pytest.mark.asyncio
async def test_memory_archive_keeps_versions_and_simulates_an_outage() -> None:
    archive = MemoryAuditArchive()
    archive.fail_next = {"governance"}
    with pytest.raises(ConnectionError):
        await archive.put("governance", "k", b"1")
    await archive.put("governance", "k", b"1")
    await archive.put("governance", "k", b"2")
    assert await archive.get("governance", "k") == b"2"
    assert archive.versions[("governance", "k")] == [b"1", b"2"]
    with pytest.raises(ArchiveMissing):
        await archive.get("compliance", "k")
    with pytest.raises(ValueError):
        await archive.put("other", "k", b"")


class FakeS3:
    def __init__(self, error: Exception | None = None) -> None:
        self.puts: list[dict[str, Any]] = []
        self.error = error

    def put_object(self, **kw: Any) -> dict[str, Any]:
        self.puts.append(kw)
        return {}

    def get_object(self, **kw: Any) -> dict[str, Any]:
        if self.error:
            raise self.error
        body = self.puts[-1]["Body"]

        class _Body:
            def read(self) -> bytes:
                return body

        return {"Body": _Body()}


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}


@pytest.mark.asyncio
async def test_s3_archive_sends_a_checksum_and_maps_missing_objects() -> None:
    s3 = FakeS3()
    archive = S3AuditArchive("bucket-c", "bucket-g", client=s3)
    await archive.put("compliance", "audit/v1/x", b"body")
    put = s3.puts[0]
    assert (put["Bucket"], put["Key"], put["ChecksumAlgorithm"]) == ("bucket-c", "audit/v1/x", "SHA256")
    assert "ObjectLockMode" not in put and "ServerSideEncryption" not in put  # the bucket defaults apply
    assert await archive.get("compliance", "audit/v1/x") == b"body"
    with pytest.raises(ArchiveMissing):
        await S3AuditArchive("c", "g", client=FakeS3(ClientError("AccessDenied"))).get("governance", "k")
    with pytest.raises(ClientError):
        await S3AuditArchive("c", "g", client=FakeS3(ClientError("SlowDown"))).get("governance", "k")


def test_settings_take_both_audit_buckets_or_none() -> None:
    both = Settings(
        environment="dev",
        audit_bucket_compliance="aa-crm-dev-audit-c-x",
        audit_bucket_governance="aa-crm-dev-audit-g-x",
    )
    assert isinstance(build_audit_archive(both), S3AuditArchive)
    assert build_audit_archive(Settings(environment="dev")) is None
    assert isinstance(build_audit_archive(Settings(environment="test", db_ssl="disable")), MemoryAuditArchive)
    with pytest.raises(SettingsError, match="together"):
        Settings(environment="dev", audit_bucket_compliance="aa-crm-dev-audit-c-x")
    with pytest.raises(SettingsError, match="differ"):
        Settings(environment="dev", audit_bucket_compliance="same-bucket", audit_bucket_governance="same-bucket")
    with pytest.raises(SettingsError, match="S3 bucket name"):
        Settings(environment="dev", audit_bucket_compliance="Bad_Name", audit_bucket_governance="ok-bucket")


def _export_conn(
    exported_until: datetime, last: dict[str, Any] | None, last_seq: int, events: list[Any]
) -> ScriptedConn:
    return ScriptedConn(
        [
            (
                "FROM crm.audit_export_tenant WHERE tenant_id = $1 FOR UPDATE",
                {"exported_until": exported_until, "last_seq": last_seq},
            ),
            ("FROM crm.audit_export_manifest ORDER BY seq DESC", last),
            ("FROM crm.audit_event WHERE at >= $1", events),
        ]
    )


@pytest.mark.asyncio
async def test_export_next_hour_writes_both_tiers_then_the_manifest() -> None:
    conn = _export_conn(START, None, 0, [_event(1, START + timedelta(minutes=3))])
    archive = MemoryAuditArchive()
    obj = await export.export_next_hour(ExportContext(conn, TENANT), archive, START + timedelta(hours=1, minutes=16))
    assert obj is not None and obj.seq == 1
    assert {t for (t, _k) in archive.versions} == {"compliance", "governance"}
    [manifest] = conn.sql("INSERT INTO crm.audit_export_manifest")
    assert manifest[:4] == (1, START, START + chain.PERIOD, 1)
    assert conn.sql("UPDATE crm.audit_export_tenant") == [(TENANT, START + chain.PERIOD, 1)]


@pytest.mark.asyncio
async def test_export_next_hour_waits_for_the_lag_and_refuses_a_disagreement() -> None:
    ctx = ExportContext(_export_conn(START, None, 0, []), TENANT)
    assert await export.export_next_hour(ctx, MemoryAuditArchive(), START + timedelta(hours=1, minutes=14)) is None
    assert await export.export_next_hour(ExportContext(ScriptedConn(), TENANT), MemoryAuditArchive(), START) is None
    last = {"seq": 4, "manifest_sha256": "a" * 64, "period_end": START}
    with pytest.raises(export.ChainStateMismatch):
        await export.export_next_hour(
            ExportContext(_export_conn(START, last, 3, []), TENANT), MemoryAuditArchive(), START + timedelta(hours=2)
        )
    gap = {"seq": 3, "manifest_sha256": "a" * 64, "period_end": START - chain.PERIOD}
    with pytest.raises(export.ChainStateMismatch):
        await export.export_next_hour(
            ExportContext(_export_conn(START, gap, 3, []), TENANT), MemoryAuditArchive(), START + timedelta(hours=2)
        )


def _fake_export_tx(conns: list[ScriptedConn]) -> Any:
    @asynccontextmanager
    async def tx(_pool: Any, tenant_id: uuid.UUID, **_: Any):  # noqa: ANN202
        yield ExportContext(conns.pop(0) if len(conns) > 1 else conns[0], tenant_id)

    return tx


@pytest.mark.asyncio
async def test_export_tenant_and_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def next_hour(ctx: ExportContext, archive: Any, now: datetime) -> Any:
        calls["n"] += 1
        return object() if calls["n"] <= 2 else None

    monkeypatch.setattr(export, "audit_export_tx", _fake_export_tx([ScriptedConn()]))
    monkeypatch.setattr(export, "export_next_hour", next_hour)
    assert await export.export_tenant(object(), MemoryAuditArchive(), TENANT, now=START) == 2
    calls["n"] = 0
    assert await export.export_tenant(object(), MemoryAuditArchive(), TENANT, now=START, max_hours=1) == 1

    agencies = [registry.Agency(TENANT, START, None, 0), registry.Agency(uuid.uuid4(), START, None, 0)]

    async def ensure(_pool: Any) -> int:
        return 1

    async def listed(_pool: Any) -> list[registry.Agency]:
        return agencies

    async def export_one(_pool: Any, _archive: Any, tenant_id: uuid.UUID, **_: Any) -> int:
        if tenant_id != TENANT:
            raise ConnectionError("down")
        return 3

    monkeypatch.setattr(registry, "ensure_partitions", ensure)
    monkeypatch.setattr(registry, "agencies", listed)
    monkeypatch.setattr(export, "export_tenant", export_one)
    job = Job("j", export.KIND, {}, 1, 3)
    assert await export.build_handler(None)(object(), job) == {"status": "not_configured", "partition_count": 1}
    with pytest.raises(RuntimeError, match="1 agencies"):
        await export.build_handler(MemoryAuditArchive())(object(), job)
    agencies.pop()
    assert (await export.build_handler(MemoryAuditArchive())(object(), job))["exported_count"] == 3


@pytest.mark.asyncio
async def test_the_hourly_schedule_is_due_now_once_per_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    queued: list[tuple[str, str]] = []

    async def enqueue(_pool: Any, kind: str, payload: dict[str, Any], **kw: Any) -> str:
        queued.append((kind, kw["idempotency_key"]))
        return "id"

    async def prune(*_: Any) -> int:
        return 0

    monkeypatch.setattr(export.queue, "enqueue", enqueue)
    monkeypatch.setattr(export.queue, "prune", prune)
    clock = {"now": START + timedelta(minutes=10)}
    hook = export.HourlyAuditExportSchedule(lambda: clock["now"])
    assert await hook(object()) is None  # before minute 16 the previous hour is not past the lag
    clock["now"] = START + timedelta(minutes=16)
    assert await hook(object()) == "id"
    assert await hook(object()) is None  # once per hour in this process
    clock["now"] = START + timedelta(hours=1, minutes=20)
    await hook(object())
    assert queued == [
        ("crm_audit_export", "crm_audit_export:schedule:2026-10-24T01"),
        ("crm_audit_export", "crm_audit_export:schedule:2026-10-24T02"),
    ]


def _manifest_row(obj: chain.ExportObject) -> dict[str, Any]:
    return {
        "seq": obj.seq,
        "period_start": obj.period_start,
        "period_end": obj.period_end,
        "event_count": obj.event_count,
        "content_sha256": obj.content_sha256,
        "prev_manifest_sha256": obj.prev_manifest_sha256,
        "manifest_sha256": obj.manifest_sha256,
        "object_key": obj.object_key,
        "object_sha256": obj.object_sha256,
    }


async def _chain_of_two(archive: MemoryAuditArchive) -> tuple[list[chain.ExportObject], dict[datetime, list[Any]]]:
    events = {START: [_event(1, START)], START + chain.PERIOD: []}
    a = chain.build_object(TENANT, 1, START, START + chain.PERIOD, events[START], chain.ZERO_HASH)
    b = chain.build_object(TENANT, 2, START + chain.PERIOD, START + 2 * chain.PERIOD, [], a.manifest_sha256)
    for obj in (a, b):
        for tier in ("compliance", "governance"):
            await archive.put(tier, obj.object_key, obj.body)
    return [a, b], events


def _verify_conn(rows: list[dict[str, Any]], events: dict[datetime, list[Any]], **over: Any) -> ScriptedConn:
    last = rows[-1] if rows else None
    return ScriptedConn(
        [
            ("FROM crm.audit_export_manifest WHERE period_end > $1", rows),
            ("FROM crm.audit_partition_bounds()", over.get("floor", (None, True))),
            (
                "FROM crm.audit_export_tenant WHERE tenant_id = $1",
                over.get(
                    "registry",
                    {"exported_until": last["period_end"] if last else START, "last_seq": last["seq"] if last else 0},
                ),
            ),
            ("WHERE at < $1", over.get("before", 0)),
            ("FROM crm.audit_event WHERE at >= $1", lambda s, e: over.get("db_events", events).get(s, [])),
        ]
    )


async def _run_verify(
    monkeypatch: pytest.MonkeyPatch, conn: ScriptedConn, archive: Any, since: Any = None
) -> verify.VerifyReport:
    monkeypatch.setattr(verify, "audit_export_tx", _fake_export_tx([conn]))
    return await verify.verify_tenant(object(), archive, TENANT, since=since)


@pytest.mark.asyncio
async def test_verify_accepts_a_clean_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    archive = MemoryAuditArchive()
    objs, events = await _chain_of_two(archive)
    report = await _run_verify(monkeypatch, _verify_conn([_manifest_row(o) for o in objs], events), archive)
    assert report.ok and report.checked == 2 and report.checked_until == START + 2 * chain.PERIOD
    assert report.as_json()["ok"] is True and report.as_json()["checked_count"] == 2


@pytest.mark.asyncio
async def test_verify_names_each_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    archive = MemoryAuditArchive()
    objs, events = await _chain_of_two(archive)
    rows = [_manifest_row(o) for o in objs]
    # A row deleted from the database after the export (card E test).
    deleted = await _run_verify(monkeypatch, _verify_conn(rows, events, db_events={START: []}), archive)
    assert {"code": "content_mismatch", "seq": 1} in deleted.problems
    # Chain breaks.
    broken = [
        rows[0],
        {**rows[1], "seq": 3, "prev_manifest_sha256": "b" * 64, "period_start": START + 2 * chain.PERIOD},
    ]
    codes = {
        p["code"]
        for p in (
            await _run_verify(
                monkeypatch, _verify_conn(broken, events, registry={"exported_until": START, "last_seq": 9}), archive
            )
        ).problems
    }
    assert {"seq_gap", "chain_broken", "period_gap", "manifest_hash", "registry_mismatch"} <= codes
    first_wrong = [{**rows[0], "seq": 2}]
    codes = {p["code"] for p in (await _run_verify(monkeypatch, _verify_conn(first_wrong, events), archive)).problems}
    assert "chain_broken" in codes
    # Rows older than the first exported hour, and archive damage per tier.
    archive.versions[("governance", objs[1].object_key)].append(b"garbage")
    del archive.versions[("compliance", objs[0].object_key)]
    report = await _run_verify(monkeypatch, _verify_conn(rows, events, before=2), archive)
    codes = {p["code"] for p in report.problems}
    assert {
        "unexported_rows",
        "object_missing:compliance",
        "object_mismatch:governance",
        "object_object_unreadable:governance",
    } <= codes
    # Hours whose partition is gone are checked against the archive only; without an archive, chain only.
    gone = await _run_verify(
        monkeypatch, _verify_conn(rows, events, floor=(START + 5 * chain.PERIOD, True), db_events={START: []}), None
    )
    assert gone.ok
    # Incremental (since): no link check for the first row, no registry check.
    later = await _run_verify(monkeypatch, _verify_conn(rows[1:], events), archive, since=START + chain.PERIOD)
    assert {p["code"] for p in later.problems} == {"object_mismatch:governance", "object_object_unreadable:governance"}


@pytest.mark.asyncio
async def test_verify_cli_prints_one_line_per_agency(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeDb:
        def __init__(self, _settings: Any) -> None:
            pass

        async def pool(self) -> object:
            return object()

        async def close(self) -> None:
            pass

    async def listed(_pool: Any) -> list[registry.Agency]:
        return [registry.Agency(TENANT, START, None, 0)]

    async def fake_verify(_pool: Any, _archive: Any, tenant_id: uuid.UUID, **_: Any) -> verify.VerifyReport:
        report = verify.VerifyReport(tenant_id)
        report.add("content_mismatch", 1)
        return report

    import app.db

    monkeypatch.setattr(app.db, "Database", FakeDb)
    monkeypatch.setattr(registry, "agencies", listed)
    monkeypatch.setattr(verify, "verify_tenant", fake_verify)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DB_SSL", "disable")
    assert await verify._main(["--no-objects"]) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and '"code": "content_mismatch"' in out[0] and str(TENANT) in out[0]


@pytest.mark.asyncio
async def test_retention_verifies_marks_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    other = uuid.uuid4()

    async def ensure(_pool: Any) -> int:
        return 0

    async def listed(_pool: Any) -> list[registry.Agency]:
        return [registry.Agency(TENANT, START, None, 1), registry.Agency(other, START, None, 1)]

    async def fake_verify(_pool: Any, _archive: Any, tenant_id: uuid.UUID, **_: Any) -> verify.VerifyReport:
        report = verify.VerifyReport(tenant_id, checked=1, checked_until=START)
        if tenant_id == other:
            report.add("content_mismatch", 1)
        return report

    async def drop(_pool: Any, *, dry_run: bool) -> list[registry.PartitionAction]:
        return [registry.PartitionAction("audit_event_p202401", START, "would_drop" if dry_run else "dropped")]

    # TENANT is active, `other` is locked or trashed (no run event: offboarding path).
    conn = ScriptedConn().on("status = 'active'", lambda tid: 1 if tid == TENANT else None)
    monkeypatch.setattr(registry, "ensure_partitions", ensure)
    monkeypatch.setattr(registry, "agencies", listed)
    monkeypatch.setattr(registry, "drop_expired_partitions", drop)
    monkeypatch.setattr(retention, "verify_tenant", fake_verify)
    monkeypatch.setattr(retention, "audit_export_tx", _fake_export_tx([conn]))
    job = Job("r", retention.KIND, {}, 1, 3)
    result = await retention.build_handler(MemoryAuditArchive(), dry_run=True)(object(), job)
    assert result["verified_count"] == 1 and result["problem_count"] == 1 and result["would_drop_count"] == 1
    assert result["status"] == "problems" and conn.sql("crm.audit_mark_verified") == [(START,)]
    assert conn.sql("UPDATE crm.audit_export_tenant") == []  # verified_until never moves by a plain UPDATE
    events = conn.sql("INSERT INTO crm.audit_event")
    assert result["event_count"] == 1 and len(events) == 1
    assert events[0][:5] == ("system", "job:crm_audit_retention", "audit_retention", "allowed", "audit_export")
    assert events[0][7] == "verified" and events[0][9] == []  # dry run: nothing dropped
    result = await retention.build_handler(None, dry_run=False)(object(), job)
    assert result["verified_count"] == 0 and result["dropped_count"] == 1
    events = conn.sql("INSERT INTO crm.audit_event")
    assert events[-1][7] == "database_only" and events[-1][9] == ["audit_event_p202401"]


@pytest.mark.asyncio
async def test_the_run_event_skips_a_deleted_agency(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def gone(_pool: Any, _tenant_id: uuid.UUID, **_: Any):  # noqa: ANN202
        raise NotFound("tenant_not_found")
        yield  # pragma: no cover

    monkeypatch.setattr(retention, "audit_export_tx", gone)
    assert await retention.record_run(object(), TENANT, "verified", ()) is False
    with pytest.raises(ValueError):
        retention.run_event("verified", ("not a partition name",))


@pytest.mark.asyncio
async def test_the_daily_retention_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    queued: list[str] = []

    async def enqueue(_pool: Any, kind: str, payload: dict[str, Any], **kw: Any) -> str:
        queued.append(kw["idempotency_key"])
        return "id"

    async def prune(*_: Any) -> int:
        return 0

    monkeypatch.setattr(retention.queue, "enqueue", enqueue)
    monkeypatch.setattr(retention.queue, "prune", prune)
    clock = {"now": START}
    hook = retention.DailyAuditRetentionSchedule(19, lambda: clock["now"])
    assert await hook(object()) is None
    clock["now"] = START.replace(hour=19)
    assert await hook(object()) == "id"
    assert await hook(object()) is None
    assert queued == ["crm_audit_retention:schedule:2026-10-24"]
    with pytest.raises(ValueError):
        retention.DailyAuditRetentionSchedule(24)


@pytest.mark.asyncio
async def test_registry_calls_the_crm007_functions() -> None:
    pool = ScriptedConn(
        [
            (
                "crm.audit_export_agencies()",
                [{"tenant_id": TENANT, "exported_until": START, "verified_until": None, "last_seq": 2}],
            ),
            ("crm.audit_ensure_partitions()", 2),
            (
                "crm.audit_drop_expired_partitions($1)",
                [{"partition_name": "p", "range_end": START, "action": "would_drop"}],
            ),
        ]
    )
    assert await registry.agencies(pool) == [registry.Agency(TENANT, START, None, 2)]
    assert await registry.ensure_partitions(pool) == 2
    assert await registry.drop_expired_partitions(pool, dry_run=True) == [
        registry.PartitionAction("p", START, "would_drop")
    ]
    assert pool.sql("crm.audit_drop_expired_partitions") == [(True,)]


@pytest.mark.asyncio
async def test_audit_export_tx_takes_the_two_job_actors_only() -> None:
    from app.tenancy import audit_export_tx

    with pytest.raises(ValueError):
        async with audit_export_tx(ScriptedConn(), TENANT, actor="job:other"):
            pass  # pragma: no cover
    conn = ScriptedConn().on("FROM crm.tenant WHERE tenant_id", 1)
    async with audit_export_tx(conn, TENANT, actor="job:crm_audit_retention") as ctx:  # type: ignore[arg-type]
        assert ctx.actor == "job:crm_audit_retention"
    assert conn.sql("'app.actor'") == [("job:crm_audit_retention",)]
