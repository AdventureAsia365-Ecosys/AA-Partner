"""Agency documents of a traveller or a trip (AA-937; design v3 §3.3 group B, §4.1 Fix A7; crm-006 ``crm.document``).

Upload: the bytes are checked (type by magic number, at most 10 MiB), encrypted with the tenant's ``docs`` DEK
(AES-256-GCM, AAD = tenant, ``document``, ``content``, document id) and written to the store under
``tenants/<tenant>/documents/<document>`` (no name, no traveller in the key); the row keeps the SHA-256 of that stored
object, the encrypted file name (vault DEK) and the flags. A passport scan is always write-only and never visible to
travellers (the CHECKs repeat it). The row is inserted first and the object written second, inside the request's
transaction: a failed write rolls the row back.

Download is a reveal: a reason code is required and one ``reveal`` event (field ``document``) is written; the digest
is checked before decrypting. Trash keeps the object until the trash purge (CRM-14 deletes both).
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from typing import Any

from app.documents.store import DocumentStore
from app.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from app.intake import stage1
from app.tenancy import TenantContext
from app.travellers.fields import REVEAL_REASONS
from app.vault import Vault

MAX_BYTES = 10 * 1024 * 1024
KINDS = (
    "passport_scan",
    "waiver_pdf",
    "terms_pdf",
    "ticket",
    "voucher",
    "invoice_pdf",
    "supplier_confirmation",
    "other",
)
CONTENT_TYPES = ("application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic")
TRASH_DAYS = 7
MAX_FILENAME = 200

INSERT_SQL = """
INSERT INTO crm.document (document_id, trip_id, traveler_id, kind, s3_key, sha256, size_bytes, content_type,
                          filename_enc, visible_to_travelers, write_only)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
RETURNING created_at
"""
ROW_SQL = """
SELECT document_id, trip_id, traveler_id, kind, s3_key, sha256, size_bytes, content_type, filename_enc,
       visible_to_travelers, write_only, created_at
  FROM crm.document WHERE document_id = $1 AND deleted_at IS NULL
"""
TRIP_DOCS_SQL = """
SELECT document_id, trip_id, traveler_id, kind, size_bytes, content_type, filename_enc, visible_to_travelers,
       write_only, created_at
  FROM crm.document WHERE trip_id = $1 AND deleted_at IS NULL ORDER BY created_at DESC, document_id
"""
TRAVELER_EXISTS_SQL = "SELECT 1 FROM crm.traveler WHERE traveler_id = $1 AND deleted_at IS NULL"
TRIP_EXISTS_SQL = "SELECT 1 FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
TRASH_SQL = """
UPDATE crm.document SET deleted_at = now(), deleted_by = $2, purge_after = now() + make_interval(days => $3)
 WHERE document_id = $1
"""
VISIBLE_SQL = "UPDATE crm.document SET visible_to_travelers = $2 WHERE document_id = $1"


def object_key(tenant_id: uuid.UUID, document_id: uuid.UUID) -> str:
    return f"tenants/{tenant_id}/documents/{document_id}"


def sniff(content: bytes) -> str | None:
    """The content type the bytes really are (magic numbers), or None."""
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[4:8] == b"ftyp" and content[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heif"):
        return "image/heic"
    return None


def decode(content_b64: str) -> bytes:
    try:
        content = base64.b64decode(content_b64, validate=True)
    except (ValueError, TypeError):
        raise Invalid("invalid_content", "base64 file content") from None
    if not 1 <= len(content) <= MAX_BYTES:
        raise Invalid("invalid_size", f"1 byte to {MAX_BYTES // (1024 * 1024)} MiB")
    return content


def _view(row: Any, filename: str) -> dict[str, Any]:
    return {
        "document_id": str(row["document_id"]),
        "trip_id": str(row["trip_id"]) if row["trip_id"] else None,
        "traveler_id": str(row["traveler_id"]) if row["traveler_id"] else None,
        "kind": row["kind"],
        "filename": filename,
        "size_bytes": row["size_bytes"],
        "content_type": row["content_type"],
        "visible_to_travelers": row["visible_to_travelers"],
        "write_only": row["write_only"],
        "created_at": row["created_at"].isoformat(),
    }


async def upload(
    ctx: TenantContext,
    vault: Vault,
    store: DocumentStore,
    *,
    traveler_id: uuid.UUID | None,
    trip_id: uuid.UUID | None,
    kind: str,
    filename: str,
    content_type: str,
    content: bytes,
    visible_to_travelers: bool = False,
) -> dict[str, Any]:
    if kind not in KINDS:
        raise Invalid("invalid_kind", ", ".join(KINDS))
    if content_type not in CONTENT_TYPES:
        raise Invalid("invalid_content_type", ", ".join(CONTENT_TYPES))
    if not 1 <= len(content) <= MAX_BYTES:
        raise Invalid("invalid_size", f"1 byte to {MAX_BYTES // (1024 * 1024)} MiB")
    if sniff(content) != content_type:
        raise Invalid("content_mismatch", "the file is not what its type says")
    try:
        name = stage1.clean_text(filename, MAX_FILENAME)
    except ValueError:
        name = None
    if not name:
        raise Invalid("invalid_filename", f"1 to {MAX_FILENAME} characters")
    if traveler_id is None and trip_id is None:
        raise Invalid("subject_required", "a traveller or a trip")
    if traveler_id is not None and not await ctx.conn.fetchval(TRAVELER_EXISTS_SQL, traveler_id):
        raise NotFound("traveler_not_found")
    if trip_id is not None and not await ctx.conn.fetchval(TRIP_EXISTS_SQL, trip_id):
        raise NotFound("trip_not_found")
    write_only = kind == "passport_scan"
    if write_only and visible_to_travelers:
        raise Invalid("passport_never_visible", "a passport scan is never shown to travellers")
    document_id = uuid.uuid4()
    sealed = await vault.encrypt("document", "content", document_id, content, purpose="docs")
    key = object_key(ctx.tenant_id, document_id)
    row = await ctx.conn.fetchrow(
        INSERT_SQL,
        document_id,
        trip_id,
        traveler_id,
        kind,
        key,
        hashlib.sha256(sealed).digest(),
        len(content),
        content_type,
        await vault.encrypt("document", "filename_enc", document_id, name),
        visible_to_travelers,
        write_only,
    )
    await store.put(key, sealed)
    ctx.audit("create", "allowed", "document", str(document_id), fields=("content", "filename"))
    return {
        "document_id": str(document_id),
        "trip_id": str(trip_id) if trip_id else None,
        "traveler_id": str(traveler_id) if traveler_id else None,
        "kind": kind,
        "filename": name,
        "size_bytes": len(content),
        "content_type": content_type,
        "visible_to_travelers": visible_to_travelers,
        "write_only": write_only,
        "created_at": row["created_at"].isoformat(),
    }


async def trip_documents(ctx: TenantContext, vault: Vault, trip_id: uuid.UUID) -> dict[str, Any]:
    if not await ctx.conn.fetchval(TRIP_EXISTS_SQL, trip_id):
        raise NotFound("trip_not_found")
    rows = await ctx.conn.fetch(TRIP_DOCS_SQL, trip_id)
    items = [
        _view(r, await vault.decrypt("document", "filename_enc", r["document_id"], r["filename_enc"])) for r in rows
    ]
    if items:
        ctx.audit(
            "list_view",
            "allowed",
            "document",
            None,
            fields=("document_filename",),
            reason="trip_documents",
            object_ids=tuple(i["document_id"] for i in items),
        )
    return {"trip_id": str(trip_id), "documents": items}


async def download(
    ctx: TenantContext, vault: Vault, store: DocumentStore, document_id: uuid.UUID, reason: str
) -> dict[str, Any]:
    if reason not in REVEAL_REASONS:
        raise Invalid("reason_required", "pick why you open this document", reasons=list(REVEAL_REASONS))
    row = await ctx.conn.fetchrow(ROW_SQL, document_id)
    if row is None:
        raise NotFound("document_not_found")
    sealed = await store.get(row["s3_key"])
    if hashlib.sha256(sealed).digest() != bytes(row["sha256"]):
        ctx.audit("reveal", "denied", "document", str(document_id), fields=("document",), reason="digest_mismatch")
        raise Unavailable("document_integrity", "the stored document does not match its digest")
    content = await vault.decrypt_bytes("document", "content", document_id, sealed, purpose="docs")
    filename = await vault.decrypt("document", "filename_enc", document_id, row["filename_enc"])
    ctx.audit("reveal", "allowed", "document", str(document_id), fields=("document",), reason=reason)
    return {**_view(row, filename), "content_b64": base64.b64encode(content).decode("ascii")}


async def set_visibility(ctx: TenantContext, document_id: uuid.UUID, visible: bool) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ROW_SQL, document_id)
    if row is None:
        raise NotFound("document_not_found")
    if row["write_only"] and visible:
        raise Conflict("passport_never_visible", "a write-only document is never shown to travellers")
    await ctx.conn.execute(VISIBLE_SQL, document_id, visible)
    ctx.audit("update", "allowed", "document", str(document_id), fields=("visible_to_travelers",))
    return {"document_id": str(document_id), "visible_to_travelers": visible}


async def trash(ctx: TenantContext, document_id: uuid.UUID) -> dict[str, Any]:
    if ctx.member_role == "assistant":
        raise Forbidden("not_allowed_for_role", "owners and agents delete documents")
    row = await ctx.conn.fetchrow(ROW_SQL, document_id)
    if row is None:
        raise NotFound("document_not_found")
    await ctx.conn.execute(TRASH_SQL, document_id, f"member:{ctx.member_id}", TRASH_DAYS)
    ctx.audit("delete", "allowed", "document", str(document_id))
    return {"document_id": str(document_id), "trashed": True}
