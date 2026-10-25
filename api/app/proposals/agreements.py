"""The agency's own booking terms and traveller waiver, one row per version (card CRM-18, AA-946; design v3 M12 v0,
A-16 Terms thin). Every member reads them; the owner writes and publishes (``TERMS_PUBLISH``).

- A version starts as a draft (kind ``booking_terms`` or ``waiver``, a label, the text); only a draft changes.
- Publishing retires the published version of the same kind in the same transaction (one published per kind,
  crm-016); the database hashes the text (``body_sha256``) and never lets a published or retired version change.
- A proposal pins its booking terms version; a signature pins the waiver version and its hash.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound
from app.tenancy import TenantContext

KINDS = ("booking_terms", "waiver")
MAX_TEXT = 100_000
MAX_LABEL = 40

LIST_SQL = """
SELECT doc_id, kind, version_label, status, body_text, body_sha256, published_at, updated_at FROM crm.agreement_doc
 WHERE deleted_at IS NULL ORDER BY kind, created_at DESC LIMIT 100
"""
ONE_SQL = "SELECT doc_id, kind, status FROM crm.agreement_doc WHERE doc_id = $1 AND deleted_at IS NULL FOR UPDATE"
INSERT_SQL = """
INSERT INTO crm.agreement_doc (doc_id, kind, version_label, body_text) VALUES ($1, $2, $3, $4)
"""
UPDATE_SQL = """
UPDATE crm.agreement_doc SET version_label = coalesce($2, version_label), body_text = coalesce($3, body_text)
 WHERE doc_id = $1
"""
RETIRE_SQL = """
UPDATE crm.agreement_doc SET status = 'retired' WHERE kind = $1 AND status = 'published' AND deleted_at IS NULL
"""
PUBLISH_SQL = "UPDATE crm.agreement_doc SET status = 'published', published_at = now() WHERE doc_id = $1"


def _label(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_LABEL:
        raise Invalid("invalid_version_label", f"1 to {MAX_LABEL} characters")
    return value.strip()


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Invalid("body_required", "write the text")
    clean = value.replace("\r\n", "\n").strip()
    if len(clean) > MAX_TEXT:
        raise Invalid("body_too_long", f"at most {MAX_TEXT} characters")
    return clean


def _view(r: Any) -> dict[str, Any]:
    return {
        "doc_id": str(r["doc_id"]),
        "kind": r["kind"],
        "version_label": r["version_label"],
        "status": r["status"],
        "body_text": r["body_text"],
        "body_sha256": bytes(r["body_sha256"]).hex(),
        "published_at": r["published_at"].isoformat() if r["published_at"] else None,
    }


async def list_docs(ctx: TenantContext) -> dict[str, Any]:
    return {"agreements": [_view(r) for r in await ctx.conn.fetch(LIST_SQL)]}


async def create(ctx: TenantContext, *, kind: str, version_label: Any, body_text: Any) -> dict[str, Any]:
    if kind not in KINDS:
        raise Invalid("invalid_kind", ", ".join(KINDS))
    doc_id = uuid.uuid4()
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(INSERT_SQL, doc_id, kind, _label(version_label), _text(body_text))
    except asyncpg.UniqueViolationError:
        raise Conflict("version_label_taken", "this label is used by another version of the same kind") from None
    ctx.audit("agreement_draft", "allowed", "agreement_doc", str(doc_id))
    return {"doc_id": str(doc_id), "status": "draft"}


async def update(ctx: TenantContext, doc_id: uuid.UUID, changes: dict[str, Any]) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ONE_SQL, doc_id)
    if row is None:
        raise NotFound("agreement_not_found")
    if row["status"] != "draft":
        raise Conflict("agreement_not_draft", "a published version never changes; draft a new one")
    label = _label(changes["version_label"]) if "version_label" in changes else None
    text = _text(changes["body_text"]) if "body_text" in changes else None
    try:
        async with ctx.conn.transaction():
            await ctx.conn.execute(UPDATE_SQL, doc_id, label, text)
    except asyncpg.UniqueViolationError:
        raise Conflict("version_label_taken") from None
    ctx.audit("agreement_draft", "allowed", "agreement_doc", str(doc_id), reason="edit")
    return {"doc_id": str(doc_id), "status": "draft"}


async def publish(ctx: TenantContext, doc_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ONE_SQL, doc_id)
    if row is None:
        raise NotFound("agreement_not_found")
    if row["status"] != "draft":
        raise Conflict("agreement_not_draft", "only a draft is published")
    await ctx.conn.execute(RETIRE_SQL, row["kind"])
    await ctx.conn.execute(PUBLISH_SQL, doc_id)
    ctx.audit("terms_publish", "allowed", "agreement_doc", str(doc_id), reason=row["kind"])
    return {"doc_id": str(doc_id), "status": "published"}
