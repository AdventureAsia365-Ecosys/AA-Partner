"""Portability exports (AA-967, card CRM-35; design §4.10 "Export: full CSV + JSON zip under the export DEK; owner-only
download; 7-day link; audited. AA staff can only raise requests").

Owner steps (``/crm/v1/data/exports``, :mod:`app.routers.data`):

1. **Request** (owner): an agency export (scope ``tenant``) or one client's (scope ``traveler``, also from a logged
   data request) -> a ``crm.export_job`` row ``queued`` + a ``crm_data_export`` job; one open export per subject (409
   ``export_running``). Audit ``export_request``.
2. **The job** (:func:`build_handler`): ``building`` with progress (tables, then documents: ``progress_done`` /
   ``progress_total``, each step its own short transaction so the page sees it), the ZIP of
   :mod:`app.portability.builder`, sealed under the agency ``export`` DEK, stored at
   ``exports/tenants/<tenant>/portability/<export>.zip.enc`` (the 7-day ``exports/`` bucket rule) -> ``ready`` with the
   object's and the ZIP's SHA-256, the size, the row counts and ``expires_at`` (7 days). Audit ``export_create``. A
   coded refusal (a value that does not open, no bucket) fails the row at once (``failed`` + the code); any other error
   is retried, and the last attempt fails the row (``export_error``).
3. **Link** (owner): ``POST .../link`` makes a random token (shown once; only its SHA-256 is stored) valid
   :data:`LINK_MINUTES` minutes. Audit ``export_link``.
4. **Download** (owner, with the token): the object's digest is checked, opened, the ZIP's digest checked, the link
   cleared (single use), ``downloaded_at`` / ``download_count`` set. Audit ``export_download`` (a wrong, used or late
   token: 403 ``link_invalid`` + a ``denied`` event). AA staff never reach it: ``/crm/v1`` refuses every AA token
   (route class AGENCY, 403), and the L1 console has no export route (it only raises requests, AA-941).

**Withdrawal** (:func:`withdraw`): a client erasure cancels every open or ready export of the agency and of that
client and deletes their objects (they may hold the erased data); an agency erase does the same for all exports. The
rows stay (receipts, crm-026 keep list). The erasures delete the objects last, after every step that can refuse.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from typing import TYPE_CHECKING, Any

import asyncpg

from app.documents.store import DocumentStore
from app.errors import Conflict, Forbidden, Gone, NotFound, ServiceError, Unavailable
from app.jobs import queue as jobs
from app.jobs.kinds import DATA_EXPORT
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.lifecycle import erase
from app.lifecycle.rules import EXPORT_FILE_DAYS
from app.portability import builder
from app.tenancy import AnyContext, TenantContext, lifecycle_tx
from app.vault import Vault

if TYPE_CHECKING:  # pragma: no cover
    from app.vault import KeyProvider

log = logging.getLogger(__name__)

ACTOR = "job:crm_data_export"
MAX_ATTEMPTS = 3
LINK_MINUTES = 5
LIST_LIMIT = 20
SCOPES = ("tenant", "traveler")

INSERT_SQL = """
INSERT INTO crm.export_job (scope, subject_id, data_request_id, schema_version)
VALUES ($1, $2, $3, $4) RETURNING export_job_id
"""
ROW_SQL = "SELECT * FROM crm.export_job WHERE export_job_id = $1"
LOCK_SQL = "SELECT * FROM crm.export_job WHERE export_job_id = $1 FOR UPDATE"
LIST_SQL = "SELECT * FROM crm.export_job ORDER BY created_at DESC, export_job_id LIMIT $1"
TRAVELER_SQL = "SELECT 1 FROM crm.traveler WHERE traveler_id = $1"
DATA_REQUEST_SQL = "SELECT 1 FROM crm.data_request WHERE data_request_id = $1"
START_SQL = """
UPDATE crm.export_job SET status = 'building', progress_done = 0, progress_total = 0, failure_class = NULL
 WHERE export_job_id = $1
"""
PROGRESS_SQL = """
UPDATE crm.export_job SET progress_done = $2, progress_total = $3 WHERE export_job_id = $1 AND status = 'building'
"""
READY_SQL = """
UPDATE crm.export_job
   SET status = 'ready', progress_done = progress_total, object_key = $2, object_sha256 = $3, content_sha256 = $4,
       size_bytes = $5, row_counts = $6::jsonb, documents_included = $7, documents_skipped = $8, completed_at = now(),
       expires_at = now() + make_interval(days => $9)
 WHERE export_job_id = $1 AND status = 'building'
"""
FAILED_SQL = """
UPDATE crm.export_job SET status = 'failed', failure_class = $2
 WHERE export_job_id = $1 AND status IN ('queued', 'building')
"""
LINK_SQL = "UPDATE crm.export_job SET link_sha256 = $2, link_expires_at = $3 WHERE export_job_id = $1"
DOWNLOADED_SQL = """
UPDATE crm.export_job
   SET link_sha256 = NULL, link_expires_at = NULL, downloaded_at = coalesce(downloaded_at, now()),
       download_count = download_count + 1
 WHERE export_job_id = $1
"""
EXPIRE_SQL = """
UPDATE crm.export_job SET status = 'expired', object_key = NULL, object_sha256 = NULL, link_sha256 = NULL,
       link_expires_at = NULL
 WHERE export_job_id = $1 AND status = 'ready'
"""
WITHDRAW_SQL = """
WITH stale AS (
    SELECT export_job_id, object_key FROM crm.export_job
     WHERE status IN ('queued', 'building', 'ready') AND ($1::uuid IS NULL OR scope = 'tenant' OR subject_id = $1)
       FOR UPDATE
)
UPDATE crm.export_job e
   SET status = 'cancelled', object_key = NULL, object_sha256 = NULL, link_sha256 = NULL, link_expires_at = NULL
  FROM stale WHERE e.export_job_id = stale.export_job_id
RETURNING stale.export_job_id, stale.object_key
"""
WITHDRAW_PEEK_SQL = """
SELECT export_job_id, object_key FROM crm.export_job
 WHERE status IN ('queued', 'building', 'ready') AND ($1::uuid IS NULL OR scope = 'tenant' OR subject_id = $1)
"""


def object_key(tenant_id: uuid.UUID, export_id: uuid.UUID) -> str:
    return f"exports/tenants/{tenant_id}/portability/{export_id}.zip.enc"


def _ts(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def _expired(row: Any, now: dt.datetime) -> bool:
    return row["status"] == "ready" and row["expires_at"] is not None and row["expires_at"] <= now


def view(row: Any, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.UTC)
    counts = row["row_counts"]
    counts = json.loads(counts) if isinstance(counts, str) else dict(counts or {})
    return {
        "export_id": str(row["export_job_id"]),
        "scope": row["scope"],
        "subject_id": str(row["subject_id"]),
        # A ready export past its 7 days reads expired (the bucket rule has taken the object).
        "status": "expired" if _expired(row, now) else row["status"],
        "schema_version": row["schema_version"],
        "progress": {"done": row["progress_done"], "total": row["progress_total"]},
        "rows": sum(int(v) for v in counts.values()),
        "tables": len(counts),
        "documents_included": row["documents_included"],
        "documents_skipped": row["documents_skipped"],
        "size_bytes": row["size_bytes"],
        "content_sha256": row["content_sha256"],
        "created_at": _ts(row["created_at"]),
        "completed_at": _ts(row["completed_at"]),
        "expires_at": _ts(row["expires_at"]),
        "downloaded_at": _ts(row["downloaded_at"]),
        "download_count": row["download_count"],
        "failure_class": row["failure_class"],
        "data_request_id": str(row["data_request_id"]) if row["data_request_id"] else None,
    }


def _owner(ctx: TenantContext, action: str, object_id: str | None = None) -> None:
    if ctx.member_role != "owner":
        ctx.audit(action, "denied", "export_job", object_id, reason="owner_only")
        raise Forbidden("owner_only", "only the agency owner exports the agency's data")


async def list_exports(ctx: TenantContext) -> dict[str, Any]:
    _owner(ctx, "export_list")
    now = dt.datetime.now(dt.UTC)
    return {
        "exports": [view(r, now) for r in await ctx.conn.fetch(LIST_SQL, LIST_LIMIT)],
        "link_minutes": LINK_MINUTES,
        "file_days": EXPORT_FILE_DAYS,
        "schema_version": builder.SCHEMA_VERSION,
    }


async def request(
    ctx: TenantContext, scope: str, subject_id: uuid.UUID | None, *, data_request_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Queue an agency (``tenant``) or client (``traveler``) export."""
    _owner(ctx, "export_request")
    if scope not in SCOPES:
        raise ValueError(f"unknown export scope {scope!r}")
    if scope == "tenant":
        subject = ctx.tenant_id
    else:
        if subject_id is None or not await ctx.conn.fetchval(TRAVELER_SQL, subject_id):
            raise NotFound("client_not_found")
        subject = subject_id
    if data_request_id is not None and not await ctx.conn.fetchval(DATA_REQUEST_SQL, data_request_id):
        raise NotFound("data_request_not_found")
    try:
        async with ctx.conn.transaction():
            export_id = await ctx.conn.fetchval(INSERT_SQL, scope, subject, data_request_id, builder.SCHEMA_VERSION)
    except asyncpg.UniqueViolationError:
        raise Conflict("export_running", "an export of this data is already being built") from None
    await jobs.enqueue(
        ctx.conn,
        DATA_EXPORT,
        {"tenant_id": str(ctx.tenant_id), "export_id": str(export_id)},
        idempotency_key=f"{DATA_EXPORT}:{export_id}",
        max_attempts=MAX_ATTEMPTS,
        created_by="portability",
    )
    ctx.audit("export_request", "allowed", "export_job", str(export_id), reason=scope)
    return view(await ctx.conn.fetchrow(ROW_SQL, export_id))


async def _ready_row(ctx: TenantContext, export_id: uuid.UUID, store: DocumentStore | None) -> Any:
    row = await ctx.conn.fetchrow(LOCK_SQL, export_id)
    if row is None:
        raise NotFound("export_not_found")
    if _expired(row, dt.datetime.now(dt.UTC)):
        await ctx.conn.execute(EXPIRE_SQL, export_id)
        if store is not None and row["object_key"]:
            await store.delete(row["object_key"])
        raise Gone("export_expired", "the export is past its 7 days: make a new one")
    if row["status"] != "ready":
        raise Conflict("export_not_ready", f"the export is {row['status']}")
    return row


async def create_link(ctx: TenantContext, export_id: uuid.UUID, store: DocumentStore | None) -> dict[str, Any]:
    """A short-lived, single-use download link of a ready export (the token is returned once, never stored)."""
    _owner(ctx, "export_link", str(export_id))
    await _ready_row(ctx, export_id, store)
    token = secrets.token_urlsafe(32)
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=LINK_MINUTES)
    await ctx.conn.execute(LINK_SQL, export_id, hashlib.sha256(token.encode("ascii")).digest(), expires)
    ctx.audit("export_link", "allowed", "export_job", str(export_id))
    return {
        "export_id": str(export_id),
        "token": token,
        "expires_at": expires.isoformat(),
        "path": f"data/exports/{export_id}/download?token={token}",
    }


async def download(
    ctx: TenantContext, vault: Vault, store: DocumentStore | None, export_id: uuid.UUID, token: str
) -> tuple[bytes, str, str]:
    """(the ZIP, its file name, its SHA-256). Owner only, with the link's token; the link is used up."""
    _owner(ctx, "export_download", str(export_id))
    row = await _ready_row(ctx, export_id, store)
    stored = row["link_sha256"]
    given = hashlib.sha256(token.encode("utf-8", "replace")).digest()
    now = dt.datetime.now(dt.UTC)
    if (
        stored is None
        or row["link_expires_at"] is None
        or row["link_expires_at"] <= now
        or not hmac.compare_digest(bytes(stored), given)
    ):
        ctx.audit("export_download", "denied", "export_job", str(export_id), reason="link_invalid")
        raise Forbidden("link_invalid", "the download link is used up or too old: ask for a new one")
    if store is None:
        raise Unavailable("documents_not_configured")
    sealed = await store.get(row["object_key"])
    if hashlib.sha256(sealed).hexdigest() != row["object_sha256"]:
        ctx.audit("export_download", "denied", "export_job", str(export_id), reason="digest_mismatch")
        raise Unavailable("export_integrity", "the stored export does not match its digest")
    content = await vault.decrypt_bytes("export_job", "object", export_id, sealed, purpose="export")
    del sealed  # memory: the response holds the open ZIP only (builder module docstring)
    if hashlib.sha256(content).hexdigest() != row["content_sha256"]:
        ctx.audit("export_download", "denied", "export_job", str(export_id), reason="digest_mismatch")
        raise Unavailable("export_integrity", "the export does not match its checksum")
    await ctx.conn.execute(DOWNLOADED_SQL, export_id)
    ctx.audit("export_download", "allowed", "export_job", str(export_id), reason=row["scope"])
    stamp = row["completed_at"].strftime("%Y%m%d")
    name = f"crm-export-{row['scope']}-{stamp}-{str(export_id)[:8]}.zip"
    return content, name, row["content_sha256"]


async def withdraw(
    ctx: AnyContext,
    store: DocumentStore | None,
    traveler_id: uuid.UUID | None,
    *,
    dry_run: bool,
    pending: list[str] | None = None,
) -> int:
    """Cancel the exports an erasure makes stale and delete their objects: for a client (``traveler_id``) the agency
    exports and that client's; for the agency (None) all of them. Returns how many were withdrawn.

    ``pending`` (the erasures, AA-967 review): the object keys are added there instead of deleted, and the caller
    deletes them (:func:`app.lifecycle.erase.delete_objects`) after its last statement that can refuse, so a refused
    erase that rolls the rows back to ``ready`` never leaves them naming deleted files."""
    if dry_run:
        return len(await ctx.conn.fetch(WITHDRAW_PEEK_SQL, traveler_id))
    rows = await ctx.conn.fetch(WITHDRAW_SQL, traveler_id)
    keys = [r["object_key"] for r in rows if r["object_key"]]
    if keys and store is None:
        raise Unavailable("documents_not_configured", "the documents bucket is needed to delete the export files")
    if pending is not None:
        pending.extend(keys)
    else:
        await erase.delete_objects(store, keys)
    return len(rows)


# ── The job ───────────────────────────────────────────────────────────────────────────────────────
async def _fail(pool: asyncpg.Pool, tenant_id: uuid.UUID, export_id: uuid.UUID, code: str) -> None:
    async with lifecycle_tx(pool, tenant_id, actor=ACTOR) as ctx:
        await ctx.conn.execute(FAILED_SQL, export_id, code)
        ctx.audit("export_create", "denied", "export_job", str(export_id), reason=code)


async def _run(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, export_id: uuid.UUID, keys: KeyProvider, store: DocumentStore | None
) -> dict[str, Any]:
    async with lifecycle_tx(pool, tenant_id, actor=ACTOR) as ctx:
        row = await ctx.conn.fetchrow(LOCK_SQL, export_id)
        if row is None or row["status"] not in ("queued", "building"):
            return {"export_id": str(export_id), "status": "skipped"}
        await ctx.conn.execute(START_SQL, export_id)
        scope, subject = row["scope"], row["subject_id"]
    if store is None:
        await _fail(pool, tenant_id, export_id, "export_not_configured")
        return {"export_id": str(export_id), "status": "export_not_configured"}

    async def progress(done: int, total: int) -> None:
        async with lifecycle_tx(pool, tenant_id, actor=ACTOR) as pctx:
            await pctx.conn.execute(PROGRESS_SQL, export_id, done, total)

    async with lifecycle_tx(pool, tenant_id, actor=ACTOR) as ctx:
        vault = Vault(keys, ctx)  # type: ignore[arg-type]
        token = await erase.profile_token(ctx, vault, subject) if scope == "traveler" else None
        built = await builder.build(
            ctx, vault, store, scope=scope, subject_id=subject, export_id=export_id, profile_token=token,
            progress=progress,
        )  # fmt: skip
        sealed = await vault.encrypt("export_job", "object", export_id, built.content, purpose="export")
        size = len(built.content)
        built.content = b""  # memory: only the sealed copy is needed from here on (builder module docstring)
        key = object_key(tenant_id, export_id)
        await store.put(key, sealed)
        sealed_sha256 = hashlib.sha256(sealed).hexdigest()
        del sealed
        status = await ctx.conn.execute(
            READY_SQL,
            export_id,
            key,
            sealed_sha256,
            built.sha256,
            size,
            json.dumps(built.row_counts),
            built.documents_included,
            built.documents_skipped,
            EXPORT_FILE_DAYS,
        )
        if status.endswith(" 0"):  # withdrawn by an erasure while it was being built: keep nothing
            await store.delete(key)
            return {"export_id": str(export_id), "status": "withdrawn"}
        ctx.audit("export_create", "allowed", "export_job", str(export_id), reason=scope)
    log.info("portability_export tenant_id=%s export_id=%s bytes=%d", tenant_id, export_id, size)
    return {"export_id": str(export_id), "status": "ready"}


def build_handler(*, keys: KeyProvider | None, store: DocumentStore | None) -> Handler:
    async def data_export(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        try:
            tenant_id = uuid.UUID(str(job.payload["tenant_id"]))
            export_id = uuid.UUID(str(job.payload["export_id"]))
        except (KeyError, ValueError):
            raise PermanentError("bad_payload") from None
        if keys is None:
            raise Unavailable("key_provider_not_configured")
        try:
            return await _run(pool, tenant_id, export_id, keys, store)
        except ServiceError as exc:
            if exc.code in ("tenant_status", "tenant_not_found"):
                raise PermanentError(exc.code) from None  # the agency is offboarding / gone: its erase withdraws it
            if exc.status_code >= 500 and job.attempt < job.max_attempts:
                raise  # an outage (bucket, KMS): the worker retries with backoff
            await _fail(pool, tenant_id, export_id, exc.code)
            raise PermanentError(exc.code) from None
        except Exception:
            if job.attempt >= job.max_attempts:
                await _fail(pool, tenant_id, export_id, "export_error")
            raise

    return data_export
