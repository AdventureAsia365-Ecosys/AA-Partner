"""The only caller of ``crm.erase_subject`` (crm-010, design §4.10, Fix B16): the trash purge, a traveller erasure and
the agency offboarding all delete through :func:`erase`.

Order of one deletion, in the caller's transaction: decrypt the traveller's F2 pointer when the scope needs it, list
the document objects the scope reaches, run ``crm.erase_subject`` as ``crm_eraser`` (rows deleted, not yet committed),
then delete those objects from the documents bucket; a failed object delete raises, so the rows roll back and the next
run retries (a missing object counts as deleted). The DB function raises a coded SQLSTATE for every refusal; they map
to the service errors below.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg

from app.documents.store import DocumentStore
from app.errors import Conflict, NotFound, ServiceError, Unavailable
from app.tenancy import LifecycleContext, TenantContext, as_eraser
from app.vault import Vault

ERASE_SQL = "SELECT crm.erase_subject($1, $2, $3, $4)"
PURGE_EXPIRED_SQL = "SELECT crm.lifecycle_purge_expired($1)"
SCOPES = ("trip", "traveler", "task", "note", "document", "relation", "tenant")
POINTER_SQL = "SELECT profile_token_enc FROM crm.traveler WHERE traveler_id = $1"
DOC_KEYS_SQL: dict[str, str] = {
    "trip": "SELECT s3_key FROM crm.document WHERE trip_id = $1",
    "traveler": "SELECT s3_key FROM crm.document WHERE traveler_id = $1",
    "document": "SELECT s3_key FROM crm.document WHERE document_id = $1",
    "tenant": "SELECT s3_key FROM crm.document WHERE $1::uuid IS NOT NULL",
}

# SQLSTATE of crm.erase_subject -> (error class, code). CRM16 (rows left after an agency erase) is a programming
# error (a new table not named in the function): 503, the run rolls back and is retried after the fix.
REFUSALS: dict[str, tuple[type[ServiceError], str]] = {
    "CRM10": (Conflict, "legal_hold"),
    "CRM11": (Conflict, "delete_blocked"),
    "CRM12": (Conflict, "traveler_on_trip"),
    "CRM13": (NotFound, "subject_not_found"),
    "CRM14": (Conflict, "tenant_not_offboarding"),
    "CRM15": (Conflict, "profile_token_mismatch"),
    "CRM16": (Unavailable, "erase_incomplete"),
}


def refusal(exc: asyncpg.PostgresError) -> ServiceError | None:
    mapped = REFUSALS.get(getattr(exc, "sqlstate", "") or "")
    if mapped is None:
        return None
    cls, code = mapped
    return cls(code)


async def profile_token(
    ctx: TenantContext | LifecycleContext, vault: Vault, traveler_id: uuid.UUID
) -> uuid.UUID | None:
    blob = await ctx.conn.fetchval(POINTER_SQL, traveler_id)
    if blob is None:
        return None
    return uuid.UUID(await vault.decrypt("traveler", "profile_token_enc", traveler_id, blob))


async def erase(
    ctx: TenantContext | LifecycleContext,
    scope: str,
    subject: uuid.UUID,
    *,
    dry_run: bool,
    vault: Vault | None = None,
    store: DocumentStore | None = None,
) -> dict[str, int]:
    """Delete (or count, ``dry_run``) one subject and everything that hangs off it. Returns the per-table counts."""
    if scope not in SCOPES:
        raise ValueError(f"unknown erase scope {scope!r}")
    token = None
    if scope == "traveler":
        if vault is None:
            raise ValueError("a traveller erase needs the vault (the F2 pointer)")
        token = await profile_token(ctx, vault, subject)
    keys: list[str] = []
    if scope in DOC_KEYS_SQL:
        keys = [r["s3_key"] for r in await ctx.conn.fetch(DOC_KEYS_SQL[scope], subject)]
        if keys and not dry_run and store is None:
            raise Unavailable("documents_not_configured", "the documents bucket is needed to delete their files")
    try:
        async with as_eraser(ctx):
            raw = await ctx.conn.fetchval(ERASE_SQL, scope, subject, token, dry_run)
    except asyncpg.PostgresError as exc:
        mapped = refusal(exc)
        if mapped is None:
            raise
        if mapped.code == "delete_blocked":
            mapped.extra["blockers"] = str(exc).rsplit(" ", 1)[-1].split(",")
        raise mapped from None
    result = json.loads(raw) if isinstance(raw, str) else dict(raw)
    counts = {str(k): int(v) for k, v in result["counts"].items()}
    if not dry_run and store is not None:
        for key in keys:
            await store.delete(key)
    counts["document_object"] = len(keys)
    return counts


async def purge_expired(ctx: TenantContext | LifecycleContext, *, dry_run: bool) -> dict[str, int]:
    """The agency's LLM calls older than 180 days (``crm.lifecycle_purge_expired``)."""
    async with as_eraser(ctx):
        raw = await ctx.conn.fetchval(PURGE_EXPIRED_SQL, dry_run)
    result: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else dict(raw)
    return {str(k): int(v) for k, v in result.items()}
