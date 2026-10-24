"""AA-937 documents (design v3 §4.1 Fix A7): docs-DEK encryption before the store, write-only passports, download
as an audited reveal, the stores. The real AES-GCM round trip and the table CHECKs run on PostgreSQL in
tests/integration/test_crm006_travellers.py."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import uuid
from typing import Any

import pytest

from app.documents import service as documents
from app.documents.store import MemoryDocumentStore, S3DocumentStore, require_store
from app.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from app.main import build_document_store
from app.settings import Settings, SettingsError
from tests.unit.fakes import ScriptedConn
from tests.unit.slice_fakes import TENANT, FakeVault, enc, tenant_ctx

T1 = uuid.UUID("00000000-0000-4000-8000-0000000000f1")
TRIP = uuid.UUID("00000000-0000-4000-8000-0000000000e1")
PDF = b"%PDF-1.7\nsynthetic\n%%EOF"
JPEG = b"\xff\xd8\xff\xe0synthetic"
NOW = dt.datetime(2026, 10, 24, 8, 0, tzinfo=dt.UTC)


class DocVault(FakeVault):
    """Like FakeVault, but keeps bytes as bytes and records the purpose of every call."""

    def __init__(self) -> None:
        super().__init__()
        self.purposes: list[tuple[str, str]] = []

    async def encrypt(self, table: str, column: str, row_id: Any, value: str | bytes, **kw: Any) -> bytes:
        self.purposes.append((column, kw.get("purpose", "vault")))
        raw = value.encode() if isinstance(value, str) else value
        return f"enc:{table}.{column}:".encode() + raw

    async def decrypt_bytes(self, table: str, column: str, row_id: Any, blob: bytes, **kw: Any) -> bytes:
        self.purposes.append((column, kw.get("purpose", "vault")))
        return bytes(blob).split(b":", 2)[2]


def test_sniff_and_decode() -> None:
    assert documents.sniff(PDF) == "application/pdf" and documents.sniff(JPEG) == "image/jpeg"
    assert documents.sniff(b"\x89PNG\r\n\x1a\nxx") == "image/png"
    assert documents.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert documents.sniff(b"\x00\x00\x00\x18ftypheic") == "image/heic"
    assert documents.sniff(b"<html>") is None
    assert documents.decode(base64.b64encode(PDF).decode()) == PDF
    for bad in ("not base64!", ""):
        with pytest.raises(Invalid):
            documents.decode(bad)
    assert documents.object_key(TENANT, T1) == f"tenants/{TENANT}/documents/{T1}"


@pytest.mark.asyncio
async def test_upload_encrypts_with_the_docs_dek_before_the_store() -> None:
    conn = ScriptedConn().on("SELECT 1 FROM crm.traveler", 1).on("RETURNING created_at", {"created_at": NOW})
    store, vault = MemoryDocumentStore(), DocVault()
    ctx = tenant_ctx(conn)
    got = await documents.upload(
        ctx,
        vault,
        store,
        traveler_id=T1,
        trip_id=None,
        kind="passport_scan",
        filename="Anna passport.jpg",
        content_type="image/jpeg",
        content=JPEG,
    )
    assert got["write_only"] is True and got["visible_to_travelers"] is False and got["filename"] == "Anna passport.jpg"
    [(key, sealed)] = store.objects.items()
    assert key == f"tenants/{TENANT}/documents/{got['document_id']}" and JPEG in sealed and sealed != JPEG
    assert ("content", "docs") in vault.purposes and ("filename_enc", "vault") in vault.purposes
    [args] = conn.sql("INSERT INTO crm.document")
    assert args[5] == hashlib.sha256(sealed).digest() and args[6] == len(JPEG) and args[10] is True
    assert ctx.audit_events[0].fields == ("content", "filename")


@pytest.mark.asyncio
async def test_upload_refusals() -> None:
    conn = ScriptedConn().on("SELECT 1 FROM crm.traveler", 1).on("SELECT 1 FROM crm.trip", 1)
    base: dict[str, Any] = dict(
        traveler_id=T1, trip_id=None, kind="ticket", filename="t.pdf", content_type="application/pdf", content=PDF
    )
    for over, code in (
        ({"kind": "selfie"}, "invalid_kind"),
        ({"content_type": "text/html"}, "invalid_content_type"),
        ({"content": b""}, "invalid_size"),
        ({"content": JPEG}, "content_mismatch"),
        ({"filename": "  "}, "invalid_filename"),
        ({"filename": "x" * 300}, "invalid_filename"),
        ({"traveler_id": None}, "subject_required"),
        (
            {"kind": "passport_scan", "content_type": "image/jpeg", "content": JPEG, "visible_to_travelers": True},
            "passport_never_visible",
        ),
    ):
        with pytest.raises(Invalid) as err:
            await documents.upload(tenant_ctx(conn), DocVault(), MemoryDocumentStore(), **{**base, **over})
        assert err.value.code == code
    with pytest.raises(NotFound):
        await documents.upload(tenant_ctx(), DocVault(), MemoryDocumentStore(), **base)
    with pytest.raises(NotFound):
        await documents.upload(
            tenant_ctx(ScriptedConn()),
            DocVault(),
            MemoryDocumentStore(),
            **{**base, "traveler_id": None, "trip_id": TRIP},
        )


def doc_row(sealed: bytes, **over: Any) -> dict[str, Any]:
    base = {
        "document_id": T1,
        "trip_id": TRIP,
        "traveler_id": None,
        "kind": "ticket",
        "s3_key": f"tenants/{TENANT}/documents/{T1}",
        "sha256": hashlib.sha256(sealed).digest(),
        "size_bytes": len(PDF),
        "content_type": "application/pdf",
        "filename_enc": enc("document", "filename_enc", "ticket.pdf"),
        "visible_to_travelers": False,
        "write_only": False,
        "created_at": NOW,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_download_is_an_audited_reveal_and_checks_the_digest() -> None:
    sealed = b"enc:document.content:" + PDF
    store = MemoryDocumentStore()
    await store.put(f"tenants/{TENANT}/documents/{T1}", sealed)
    conn = ScriptedConn().on("FROM crm.document WHERE document_id", doc_row(sealed))
    ctx = tenant_ctx(conn)
    got = await documents.download(ctx, DocVault(), store, T1, "prepare_booking")
    assert base64.b64decode(got["content_b64"]) == PDF and got["filename"] == "ticket.pdf"
    assert [(e.action, e.outcome, e.reason) for e in ctx.audit_events] == [("reveal", "allowed", "prepare_booking")]
    with pytest.raises(Invalid):
        await documents.download(ctx, DocVault(), store, T1, "curious")
    with pytest.raises(NotFound):
        await documents.download(tenant_ctx(), DocVault(), store, T1, "other")
    await store.put(f"tenants/{TENANT}/documents/{T1}", sealed + b"tampered")
    bad = tenant_ctx(conn)
    with pytest.raises(Unavailable):
        await documents.download(bad, DocVault(), store, T1, "other")
    assert [(e.outcome, e.reason) for e in bad.audit_events] == [("denied", "digest_mismatch")]
    with pytest.raises(Unavailable):
        await documents.download(tenant_ctx(conn), DocVault(), MemoryDocumentStore(), T1, "other")


@pytest.mark.asyncio
async def test_trip_documents_visibility_and_trash() -> None:
    sealed = b"x"
    conn = (
        ScriptedConn()
        .on("SELECT 1 FROM crm.trip", 1)
        .on("FROM crm.document WHERE trip_id", [doc_row(sealed)])
        .on("FROM crm.document WHERE document_id", doc_row(sealed))
    )
    ctx = tenant_ctx(conn)
    got = await documents.trip_documents(ctx, DocVault(), TRIP)
    assert got["documents"][0]["filename"] == "ticket.pdf"
    assert ctx.audit_events[0].action == "list_view" and ctx.audit_events[0].object_ids == (str(T1),)
    assert (await documents.set_visibility(ctx, T1, True))["visible_to_travelers"] is True
    assert (await documents.trash(ctx, T1))["trashed"] is True
    with pytest.raises(Forbidden):
        await documents.trash(tenant_ctx(conn, role="assistant"), T1)
    passport = ScriptedConn().on("FROM crm.document WHERE document_id", doc_row(sealed, write_only=True))
    with pytest.raises(Conflict):
        await documents.set_visibility(tenant_ctx(passport), T1, True)
    for fn in (documents.set_visibility,):
        with pytest.raises(NotFound):
            await fn(tenant_ctx(), T1, False)
    with pytest.raises(NotFound):
        await documents.trash(tenant_ctx(), T1)
    with pytest.raises(NotFound):
        await documents.trip_documents(tenant_ctx(), DocVault(), TRIP)
    empty = tenant_ctx(ScriptedConn().on("SELECT 1 FROM crm.trip", 1))
    assert (await documents.trip_documents(empty, DocVault(), TRIP))["documents"] == [] and empty.audit_events == []


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.calls: list[dict[str, Any]] = []

    def put_object(self, **kw: Any) -> None:
        self.calls.append(kw)
        self.objects[kw["Key"]] = kw["Body"]

    def get_object(self, **kw: Any) -> dict[str, Any]:
        body = self.objects[kw["Key"]]

        class _Body:
            def read(self) -> bytes:
                return body

        return {"Body": _Body()}

    def delete_object(self, **kw: Any) -> None:
        self.objects.pop(kw["Key"], None)


@pytest.mark.asyncio
async def test_stores() -> None:
    s3 = FakeS3()
    store = S3DocumentStore("aa-crm-test-documents", client=s3)
    await store.put("k", b"sealed")
    assert await store.get("k") == b"sealed"
    # No SSE header: the bucket default (SSE-KMS, data key) applies; the bucket policy refuses another key.
    assert "ServerSideEncryption" not in s3.calls[0] and s3.calls[0]["Bucket"] == "aa-crm-test-documents"
    await store.delete("k")
    assert s3.objects == {}
    memory = MemoryDocumentStore()
    await memory.put("k", b"v")
    await memory.delete("k")
    with pytest.raises(Unavailable):
        await memory.get("k")
    with pytest.raises(Unavailable):
        require_store(None)
    assert require_store(memory) is memory


def test_document_store_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(build_document_store(Settings(environment="test", db_ssl="disable")), MemoryDocumentStore)
    s3 = build_document_store(Settings(environment="dev", documents_bucket="aa-crm-dev-documents-x"))
    assert isinstance(s3, S3DocumentStore) and s3.bucket == "aa-crm-dev-documents-x"
    assert build_document_store(Settings(environment="dev")) is None
    with pytest.raises(SettingsError):
        Settings(environment="dev", documents_bucket="Not A Bucket")
    from app.settings import load_settings

    assert (
        load_settings({"ENVIRONMENT": "test", "DB_SSL": "disable", "DOCUMENTS_BUCKET": "b-1x"}).documents_bucket
        == "b-1x"
    )
    lazy = S3DocumentStore("bucket-x", region="us-west-1")
    import boto3

    made: list[Any] = []
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: made.append((a, kw)) or FakeS3())
    assert isinstance(lazy._s3(), FakeS3) and made == [(("s3",), {"region_name": "us-west-1"})]
