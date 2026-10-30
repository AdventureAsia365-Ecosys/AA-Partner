"""AA-967 (CRM-35) portability and deletion: the ZIP builder, the export service and its job, the short-lived link, the
data-request log, the client erasure receipt, and the ``/crm/v1/data`` routes (the SQL runs on PostgreSQL in
tests/integration/test_portability_api.py).

Card R: an AA staff token on the download route is 403 (route class AGENCY); an agent is 403 ``role_forbidden``.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import hashlib
import io
import json
import uuid
import zipfile
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.documents.store import MemoryDocumentStore
from app.errors import Conflict, Forbidden, Gone, Invalid, NotFound
from app.jobs.payload_guard import check_payload
from app.jobs.queue import Job
from app.main import create_app
from app.portability import builder, exports, requests, subject
from app.portability import tables as T
from app.routers import data as router
from tests.tokens import LocalPools
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, enc, fake_tx, tenant_ctx
from tests.unit.test_lifecycle import codes

EXPORT = uuid.UUID("00000000-0000-4000-8000-0000000000e7")
CLIENT = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
DOC = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
NOW = dt.datetime(2026, 10, 30, 9, 0, tzinfo=dt.UTC)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class DocVault(FakeVault):
    async def decrypt_bytes(self, table: str, column: str, row_id: Any, blob: bytes, **_: Any) -> bytes:
        return bytes(blob).split(b":", 2)[2]

    async def encrypt(self, table: str, column: str, row_id: Any, value: str | bytes, **_: Any) -> bytes:
        raw = value.encode() if isinstance(value, str) else bytes(value)
        return f"enc:{table}.{column}:".encode() + raw


def _catalog() -> list[dict[str, Any]]:
    return [
        {
            "name": "traveler",
            "pk": ["traveler_id"],
            "columns": ["traveler_id", "tenant_id", "display_name_enc", "email_bidx", "profile_token_enc", "notes"],
            "json_columns": [],
        },
        {
            "name": "document",
            "pk": ["document_id"],
            "columns": [
                "document_id",
                "traveler_id",
                "kind",
                "s3_key",
                "sha256",
                "size_bytes",
                "content_type",
                "filename_enc",
            ],  # fmt: skip
            "json_columns": [],
        },
        {"name": "settings_like", "pk": ["tenant_id"], "columns": ["tenant_id", "meta"], "json_columns": ["meta"]},
        {
            "name": "integration_credential",
            "pk": ["integration_credential_id"],
            "columns": ["secret_enc"],
            "json_columns": [],
        },  # fmt: skip
    ]


def _conn(sealed: bytes) -> ScriptedConn:
    conn = ScriptedConn().on("pg_catalog.pg_class c", _catalog()).on("v_schema_versions", ["crm-001", "crm-026"])
    conn.on(
        "FROM crm.traveler",
        [
            {
                "traveler_id": CLIENT,
                "tenant_id": TENANT,
                "display_name_enc": enc("traveler", "display_name_enc", "Hoa"),
                "email_bidx": b"x",
                "profile_token_enc": b"p",
                "notes": "=SUM(A1)",
            }
        ],  # fmt: skip
    )
    conn.on(
        "FROM crm.document ORDER BY",
        [
            {
                "document_id": DOC,
                "traveler_id": CLIENT,
                "kind": "passport",
                "s3_key": "documents/k",
                "sha256": None,
                "size_bytes": 5,
                "content_type": "text/plain",
                "filename_enc": enc("document", "filename_enc", "../pass port.txt"),
            }
        ],  # fmt: skip
    )
    conn.on("SELECT document_id, s3_key, sha256", [{"document_id": DOC, "s3_key": "documents/k",
                                                   "sha256": hashlib.sha256(sealed).digest()}])  # fmt: skip
    conn.on("FROM crm.settings_like", [{"tenant_id": TENANT, "meta": '{"a": 1}'}])
    return conn


def _lifecycle_ctx(conn: ScriptedConn) -> Any:
    from app.tenancy import LifecycleContext

    return LifecycleContext(conn=conn, tenant_id=TENANT, actor="job:crm_data_export", status="active")


def test_the_zip_holds_json_csv_documents_manifest_and_checksums() -> None:
    store = MemoryDocumentStore()
    sealed = b"enc:document.content:hello"
    run(store.put("documents/k", sealed))
    conn = _conn(sealed)
    steps: list[tuple[int, int]] = []

    async def progress(done: int, total: int) -> None:
        steps.append((done, total))

    built = run(builder.build(_lifecycle_ctx(conn), DocVault(), store, scope="tenant", subject_id=TENANT,  # type: ignore[arg-type]
                              export_id=EXPORT, progress=progress))  # fmt: skip
    assert built.sha256 == hashlib.sha256(built.content).hexdigest()
    zf = zipfile.ZipFile(io.BytesIO(built.content))
    names = set(zf.namelist())
    assert {"manifest.json", "SHA256SUMS", "json/traveler.json", "csv/traveler.csv", "documents/manifest.csv"} <= names
    assert not any("integration_credential" in n for n in names), "vendor secrets are never exported"
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["schema_version"] == 1 and manifest["format"] == "aa-partner-crm-export"
    assert manifest["crm_schema_versions"] == ["crm-001", "crm-026"]
    assert manifest["documents"] == {"manifest": "documents/manifest.csv", "included": 1, "skipped": 0}
    [traveler] = json.loads(zf.read("json/traveler.json"))
    assert traveler == {"traveler_id": str(CLIENT), "tenant_id": str(TENANT), "display_name": "Hoa",
                        "notes": "=SUM(A1)"}  # fmt: skip
    assert json.loads(zf.read("json/settings_like.json")) == [{"tenant_id": str(TENANT), "meta": {"a": 1}}]
    rows = list(csv.reader(io.StringIO(zf.read("csv/traveler.csv").decode("utf-8-sig"))))
    assert rows[0] == ["traveler_id", "tenant_id", "display_name", "notes"] and rows[1][3] == "'=SUM(A1)"
    [doc] = list(csv.DictReader(io.StringIO(zf.read("documents/manifest.csv").decode("utf-8-sig"))))
    assert doc["included"] == "true" and doc["path"] == f"documents/files/{DOC}/_pass port.txt"
    assert zf.read(doc["path"]) == b"hello" and doc["content_sha256"] == hashlib.sha256(b"hello").hexdigest()
    # SHA256SUMS covers every other file, in sha256sum -c format.
    sums = dict(line.split("  ", 1)[::-1] for line in zf.read("SHA256SUMS").decode().splitlines())
    assert set(sums) == names - {"SHA256SUMS"}
    assert all(hashlib.sha256(zf.read(p)).hexdigest() == d for p, d in sums.items())
    assert steps and steps[-1][0] == steps[-1][1]
    assert built.row_counts == {"document": 1, "settings_like": 1, "traveler": 1}


def test_documents_over_the_cap_or_not_matching_their_digest_are_listed_not_included() -> None:
    store = MemoryDocumentStore()
    run(store.put("documents/k", b"tampered"))
    conn = _conn(b"enc:document.content:hello")  # the stored digest is of another object
    built = run(builder.build(_lifecycle_ctx(conn), DocVault(), store, scope="tenant", subject_id=TENANT,  # type: ignore[arg-type]
                              export_id=EXPORT))  # fmt: skip
    zf = zipfile.ZipFile(io.BytesIO(built.content))
    [doc] = list(csv.DictReader(io.StringIO(zf.read("documents/manifest.csv").decode("utf-8-sig"))))
    assert doc["included"] == "false" and doc["skipped_reason"] == "skipped_integrity"
    capped = run(builder.build(_lifecycle_ctx(_conn(b"x")), DocVault(), store, scope="tenant", subject_id=TENANT,  # type: ignore[arg-type]
                               export_id=EXPORT, documents_max_bytes=1))  # fmt: skip
    assert capped.documents_skipped == 1 and capped.documents_included == 0


def test_a_client_export_reads_only_the_rows_about_the_client() -> None:
    conn = _conn(b"x")
    run(builder.build(_lifecycle_ctx(conn), DocVault(), None, scope="traveler", subject_id=CLIENT,  # type: ignore[arg-type]
                      export_id=EXPORT, profile_token=uuid.uuid4()))  # fmt: skip
    reads = [c for c in conn.calls if c[0].startswith("SELECT * FROM crm.")]
    assert reads and all("WHERE traveler_id = $1" in sql and args == (CLIENT,) for sql, args in reads)
    assert not conn.sql("FROM crm.settings_like"), "a table naming no client is left out"


def test_traveler_query_covers_tokens_relations_trips_and_messages() -> None:
    table = lambda name, *cols: T.Table(name, (f"{name}_id",), tuple(cols))  # noqa: E731
    assert T.traveler_query(table("traveler_profile", "traveler_token")) == ("traveler_token = $1", "token")
    assert T.traveler_query(table("trip_party", "traveler_token"))[1] == "token"  # type: ignore[index]
    assert "to_traveler_id" in T.traveler_query(table("traveler_relation"))[0]  # type: ignore[index]
    assert "trip_traveler" in T.traveler_query(table("trip", "trip_id"))[0]  # type: ignore[index]
    assert "crm.conversation" in T.traveler_query(table("message"))[0]  # type: ignore[index]
    assert T.traveler_query(table("consent", "traveler_id")) == ("traveler_id = $1", "traveler")
    assert T.traveler_query(table("saved_reply", "body_enc")) is None
    assert {"tenant_key", "integration_credential", "placeholder_map", "otp_challenge", "audit_event"} <= set(
        T.NEVER_EXPORT_TABLES
    )
    assert T.Table("message", ("message_id",), ()).purpose("body_enc") == "mail"
    assert T.Table("signature_event", ("event_id",), ()).purpose("envelope_enc") == "docs"
    assert T.Table("traveler", ("traveler_id",), ()).purpose("email_enc") == "vault"


# ── The export service ────────────────────────────────────────────────────────────────────────────
def _row(**over: Any) -> dict[str, Any]:
    base = {
        "export_job_id": EXPORT, "scope": "tenant", "subject_id": TENANT, "status": "ready", "schema_version": 1,
        "progress_done": 3, "progress_total": 3, "object_key": exports.object_key(TENANT, EXPORT),
        "object_sha256": None, "content_sha256": None, "size_bytes": 4, "row_counts": '{"trip": 2, "traveler": 1}',
        "documents_included": 0, "documents_skipped": 0, "completed_at": NOW, "expires_at": NOW + dt.timedelta(days=7),
        "downloaded_at": None, "download_count": 0, "link_sha256": None, "link_expires_at": None,
        "failure_class": None, "data_request_id": None, "created_at": NOW,
    }  # fmt: skip
    base.update(over)
    return base


def test_view_counts_rows_and_reads_a_late_ready_export_as_expired() -> None:
    v = exports.view(_row(), NOW)
    assert (v["status"], v["rows"], v["tables"], v["progress"]) == ("ready", 3, 2, {"done": 3, "total": 3})
    assert exports.view(_row(), NOW + dt.timedelta(days=8))["status"] == "expired"


def test_request_is_owner_only_checks_the_subject_and_queues_one_job() -> None:
    ctx = tenant_ctx(role="agent")
    with pytest.raises(Forbidden):
        run(exports.request(ctx, "tenant", None))
    assert (ctx.audit_events[-1].action, ctx.audit_events[-1].outcome) == ("export_request", "denied")
    conn = ScriptedConn()
    with codes(NotFound, "client_not_found"):
        run(exports.request(tenant_ctx(conn), "traveler", CLIENT))
    conn.on("FROM crm.traveler WHERE", 1).on("INSERT INTO crm.export_job", EXPORT).on(
        "FROM crm.export_job WHERE", _row(status="queued")
    )
    conn.on("INSERT INTO crm.job", "job-1")
    ctx = tenant_ctx(conn)
    got = run(exports.request(ctx, "traveler", CLIENT))
    assert got["status"] == "queued" and conn.sql("INSERT INTO crm.export_job")[0][:2] == ("traveler", CLIENT)
    [(kind, payload, *_)] = conn.sql("INSERT INTO crm.job")
    assert kind == "crm_data_export" and json.loads(payload) == {"tenant_id": str(TENANT), "export_id": str(EXPORT)}
    check_payload(json.loads(payload))
    busy = ScriptedConn().on("INSERT INTO crm.export_job", asyncpg.UniqueViolationError("open"))
    with codes(Conflict, "export_running"):
        run(exports.request(tenant_ctx(busy), "tenant", None))


def test_the_link_is_single_use_owner_only_and_checks_both_digests() -> None:
    store = MemoryDocumentStore()
    content = b"PK-zip"
    vault = DocVault()
    sealed = run(vault.encrypt("export_job", "object", EXPORT, content))
    run(store.put(exports.object_key(TENANT, EXPORT), sealed))
    digests = {
        "object_sha256": hashlib.sha256(sealed).hexdigest(),
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }
    conn = ScriptedConn().on("FROM crm.export_job WHERE export_job_id = $1 FOR UPDATE", _row(**digests))
    link = run(exports.create_link(tenant_ctx(conn), EXPORT, store))
    assert link["path"].startswith(f"data/exports/{EXPORT}/download?token=")
    [(_, stored, expires)] = conn.sql("SET link_sha256")
    assert stored == hashlib.sha256(link["token"].encode()).digest() and expires > dt.datetime.now(dt.UTC)
    # Download with the token: the ZIP, the link cleared, counted.
    conn.on("FOR UPDATE", _row(**digests, link_sha256=stored, link_expires_at=expires))
    ctx = tenant_ctx(conn)
    got, name, digest = run(exports.download(ctx, vault, store, EXPORT, link["token"]))  # type: ignore[arg-type]
    assert got == content and name.endswith(".zip") and digest == digests["content_sha256"]
    assert conn.sql("download_count = download_count + 1")
    assert (ctx.audit_events[-1].action, ctx.audit_events[-1].outcome) == ("export_download", "allowed")
    # A wrong or late token: 403 link_invalid with a denied event.
    for row in (_row(**digests, link_sha256=stored, link_expires_at=NOW - dt.timedelta(minutes=1)),
                _row(**digests, link_sha256=hashlib.sha256(b"other").digest(), link_expires_at=expires),
                _row(**digests)):  # fmt: skip
        ctx = tenant_ctx(ScriptedConn().on("FOR UPDATE", row))
        with codes(Forbidden, "link_invalid"):
            run(exports.download(ctx, vault, store, EXPORT, link["token"]))  # type: ignore[arg-type]
        assert ctx.audit_events[-1].reason == "link_invalid"
    # An agent never downloads, even with a token.
    with codes(Forbidden, "owner_only"):
        run(exports.download(tenant_ctx(role="agent"), vault, store, EXPORT, link["token"]))  # type: ignore[arg-type]
    # Past its 7 days: 410, the object deleted.
    late = ScriptedConn().on("FOR UPDATE", _row(expires_at=NOW - dt.timedelta(days=1)))
    with codes(Gone, "export_expired"):
        run(exports.create_link(tenant_ctx(late), EXPORT, store))
    assert late.sql("SET status = 'expired'") and store.objects == {}
    with codes(Conflict, "export_not_ready"):
        run(exports.create_link(tenant_ctx(ScriptedConn().on("FOR UPDATE", _row(status="building"))), EXPORT, store))


def test_withdraw_cancels_and_deletes_the_objects() -> None:
    store = MemoryDocumentStore()
    run(store.put("exports/a", b"x"))
    returned = [{"export_job_id": EXPORT, "object_key": "exports/a"}, {"export_job_id": CLIENT, "object_key": None}]
    conn = ScriptedConn().on("RETURNING stale.export_job_id, stale.object_key", returned)
    assert run(exports.withdraw(tenant_ctx(conn), store, CLIENT, dry_run=False)) == 2
    assert store.objects == {} and conn.sql("SET status = 'cancelled'")[0] == (CLIENT,)
    peek = ScriptedConn().on("SELECT export_job_id, object_key", [{"export_job_id": EXPORT, "object_key": "k"}])
    assert run(exports.withdraw(tenant_ctx(peek), None, None, dry_run=True)) == 1
    assert not peek.sql("SET status")


def test_the_job_builds_seals_stores_and_marks_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryDocumentStore()
    conn = ScriptedConn().on("FOR UPDATE", _row(status="queued", object_key=None))
    ctx = _lifecycle_ctx(conn)
    monkeypatch.setattr(exports, "lifecycle_tx", fake_tx(ctx))
    monkeypatch.setattr(exports, "Vault", lambda keys, ctx: DocVault())

    async def built(*_: Any, **kw: Any) -> builder.Built:
        await kw["progress"](1, 2)
        return builder.Built(b"zip!", hashlib.sha256(b"zip!").hexdigest(), {"trip": 1}, 0, 0)

    monkeypatch.setattr(exports.builder, "build", built)
    handler = exports.build_handler(keys=object(), store=store)  # type: ignore[arg-type]
    job = Job("j", "crm_data_export", {"tenant_id": str(TENANT), "export_id": str(EXPORT)}, 1, 3)
    result = run(handler(object(), job))  # type: ignore[arg-type]
    assert result == {"export_id": str(EXPORT), "status": "ready"}
    check_payload(result, what="result")
    assert store.objects[exports.object_key(TENANT, EXPORT)] == b"enc:export_job.object:zip!"
    [ready] = conn.sql("SET status = 'ready'")
    assert ready[3] == hashlib.sha256(b"zip!").hexdigest() and json.loads(ready[5]) == {"trip": 1}
    assert conn.sql("SET status = 'building'") and conn.sql("SET progress_done = $2")[0][1:] == (1, 2)
    # No bucket: failed export_not_configured; a bad payload: permanent.
    conn2 = ScriptedConn().on("FOR UPDATE", _row(status="queued", object_key=None))
    monkeypatch.setattr(exports, "lifecycle_tx", fake_tx(_lifecycle_ctx(conn2)))
    no_store = exports.build_handler(keys=object(), store=None)  # type: ignore[arg-type]
    assert run(no_store(object(), job))["status"] == "export_not_configured"  # type: ignore[arg-type]
    assert conn2.sql("SET status = 'failed'")[0][1] == "export_not_configured"
    from app.jobs.worker import PermanentError

    with pytest.raises(PermanentError):
        run(handler(object(), Job("j", "crm_data_export", {}, 1, 3)))  # type: ignore[arg-type]


# ── The data-request log ──────────────────────────────────────────────────────────────────────────
def _request_row(**over: Any) -> dict[str, Any]:
    base = {
        "data_request_id": EXPORT, "kind": "erasure", "channel": "email", "traveler_id": CLIENT,
        "received_on": dt.date(2026, 10, 1), "due_on": dt.date(2026, 10, 31), "status": "open", "outcome": None,
        "closed_at": None, "erasure_request_id": None, "created_at": NOW,
    }  # fmt: skip
    base.update(over)
    return base


def test_the_log_defaults_the_due_date_and_refuses_bad_dates() -> None:
    conn = ScriptedConn().on("FROM crm.traveler WHERE", 1).on("INSERT INTO crm.data_request", EXPORT)
    conn.on("FROM crm.data_request WHERE", _request_row())
    got = run(requests.create(tenant_ctx(conn), kind="erasure", channel="email", received_on=dt.date(2026, 10, 1),
                              traveler_id=CLIENT))  # fmt: skip
    assert conn.sql("INSERT INTO crm.data_request")[0][3:] == (dt.date(2026, 10, 1), dt.date(2026, 10, 31))
    assert got["max_due_on"] == "2027-01-01" and got["status"] == "open"
    future = dt.date.today() + dt.timedelta(days=2)
    with codes(Invalid, "received_in_future"):
        run(requests.create(tenant_ctx(), kind="access", channel="email", received_on=future))
    with codes(Invalid, "due_out_of_range"):
        run(requests.create(tenant_ctx(), kind="access", channel="email", received_on=dt.date(2026, 10, 1),
                            due_on=dt.date(2027, 3, 1)))  # fmt: skip
    with pytest.raises(Forbidden):
        run(requests.list_requests(tenant_ctx(role="assistant")))
    assert requests.view(_request_row(), on=dt.date(2026, 11, 2))["overdue"] is True


def test_extend_and_close_check_the_dates_and_outcomes() -> None:
    conn = ScriptedConn().on("FOR UPDATE", _request_row()).on("FROM crm.data_request WHERE", _request_row())
    run(requests.extend(tenant_ctx(conn), EXPORT, dt.date(2026, 12, 15)))
    assert conn.sql("SET status = 'extended'")[0] == (EXPORT, dt.date(2026, 12, 15))
    with codes(Invalid, "due_out_of_range"):
        run(requests.extend(tenant_ctx(conn), EXPORT, dt.date(2027, 2, 1)))
    with codes(Invalid, "invalid_outcome"):
        run(requests.close(tenant_ctx(conn), EXPORT, "done", "exempt"))
    run(requests.close(tenant_ctx(conn), EXPORT, "refused", "identity_not_verified"))
    assert conn.sql("SET status = $2")[0][1:3] == ("refused", "identity_not_verified")
    closed = ScriptedConn().on("FOR UPDATE", _request_row(status="done"))
    with codes(Conflict, "data_request_closed"):
        run(requests.close(tenant_ctx(closed), EXPORT, "done", "fulfilled"))


def test_the_erasure_receipt_names_what_is_kept_and_why() -> None:
    deleted, kept = subject.split({"traveler": 1, "consent": 2, "signature_event_kept": 1, "terms_acceptance_kept": 0})
    assert deleted == {"traveler": 1, "consent": 2} and kept == {"signature_event": 1, "terms_acceptance": 0}
    assert subject.exceptions(kept, 0) == ["signed_agreements_claim_period"]
    assert subject.exceptions({"signature_event": 0}, 2) == ["accounting_records"]
    assert subject.exceptions({}, 0) == []


# ── Routes ────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def pools() -> LocalPools:
    return LocalPools()


async def _pool(_request: Any) -> object:
    return object()


def _client(pools: LocalPools, monkeypatch: pytest.MonkeyPatch, role: str = "owner") -> Any:
    settings = pools.settings()
    app = create_app(settings, verifier=pools.verifier(settings), keys=object())
    ctx = tenant_ctx(ScriptedConn(), role=role)
    monkeypatch.setattr(router, "request_pool", _pool)
    monkeypatch.setattr(router, "tenant_tx", fake_tx(ctx))
    monkeypatch.setattr(router, "Vault", lambda keys, ctx: DocVault())
    monkeypatch.setattr(router, "require_keys", lambda keys: keys)
    return app, {"Authorization": f"Bearer {pools.partner_web()}", "X-AA-Tenant-Id": str(TENANT)}


def _stub(calls: list[str], name: str, result: Any) -> Any:
    async def fn(*args: Any, **kwargs: Any) -> Any:
        calls.append(name)
        return result

    return fn


def test_every_data_route_reaches_its_helper(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    for name in ("list_exports", "request", "create_link"):
        monkeypatch.setattr(exports, name, _stub(calls, name, {"ok": name}))
    monkeypatch.setattr(exports, "download", _stub(calls, "download", (b"PK", "crm-export.zip", "ab" * 32)))
    for name in ("list_requests", "create", "extend", "close"):
        monkeypatch.setattr(requests, name, _stub(calls, name, {"ok": name}))
    app, headers = _client(pools, monkeypatch)
    token = "t" * 43
    with TestClient(app) as c:
        assert c.get("/crm/v1/data/exports", headers=headers).json() == {"ok": "list_exports"}
        assert c.post("/crm/v1/data/exports", json={}, headers=headers).status_code == 201
        assert c.post("/crm/v1/data/exports", json={"scope": "traveler", "traveler_id": str(CLIENT)},
                      headers=headers).status_code == 201  # fmt: skip
        assert c.post(f"/crm/v1/data/exports/{EXPORT}/link", headers=headers).json() == {"ok": "create_link"}
        res = c.get(f"/crm/v1/data/exports/{EXPORT}/download", params={"token": token}, headers=headers)
        assert res.content == b"PK" and res.headers["content-type"] == "application/zip"
        assert res.headers["x-content-sha256"] == "ab" * 32 and res.headers["cache-control"] == "no-store"
        assert (
            c.get(f"/crm/v1/data/exports/{EXPORT}/download", params={"token": "x"}, headers=headers).status_code == 422
        )
        assert c.get("/crm/v1/data/requests", headers=headers).status_code == 200
        body = {"kind": "erasure", "channel": "aa_forwarded", "received_on": "2026-10-28"}
        assert c.post("/crm/v1/data/requests", json=body, headers=headers).status_code == 201
        assert c.post("/crm/v1/data/requests", json={**body, "kind": "complaint"}, headers=headers).status_code == 422
        assert c.post(f"/crm/v1/data/requests/{EXPORT}/extend", json={"due_on": "2026-12-01"},
                      headers=headers).status_code == 200  # fmt: skip
        assert c.post(f"/crm/v1/data/requests/{EXPORT}/close", json={"status": "done", "outcome": "fulfilled"},
                      headers=headers).status_code == 200  # fmt: skip
    assert calls == ["list_exports", "request", "request", "create_link", "download", "list_requests", "create",
                     "extend", "close"]  # fmt: skip


def test_r_agents_and_aa_staff_never_download_an_export(pools: LocalPools, monkeypatch: pytest.MonkeyPatch) -> None:
    app, headers = _client(pools, monkeypatch, role="agent")
    path = f"/crm/v1/data/exports/{EXPORT}/download"
    params = {"token": "t" * 43}
    with TestClient(app) as c:
        # The permission matrix: agency_export / client_export / data_settings are owner-only.
        for res in (
            c.get(path, params=params, headers=headers),
            c.post("/crm/v1/data/exports", json={}, headers=headers),
            c.post("/crm/v1/data/exports", json={"scope": "traveler", "traveler_id": str(CLIENT)}, headers=headers),
            c.get("/crm/v1/data/requests", headers=headers),
        ):
            assert (res.status_code, res.json()["detail"]) == (403, "role_forbidden")
        # AA staff: a CIS admin, a platform admin, a CRM support token: route class AGENCY refuses them (403).
        for token in (pools.cis_admin(), pools.cis_admin(groups=["crm-platform-admin"]),
                      pools.crm_staff(groups=["crm-support"])):  # fmt: skip
            res = c.get(
                path, params=params, headers={"Authorization": f"Bearer {token}", "X-AA-Tenant-Id": str(TENANT)}
            )
            assert res.status_code == 403
        assert c.get(path, params=params, headers={"X-AA-Tenant-Id": str(TENANT)}).status_code == 401
    # The L1 console has no export download route at all.
    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/crm/ops/") and "export" in r.path
                and "download" in r.path]  # fmt: skip
