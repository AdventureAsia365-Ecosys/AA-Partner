"""The client import (AA-962, card CRM-31 "phased CSV import with dry run"): preview, start, progress, and the
``crm_client_import`` job.

- **Preview** (``POST /crm/v1/client-imports/preview?entity=``, raw file): reads the file, refuses a missing column
  before anything else (**R**: 422 ``missing_columns``), runs :func:`app.imports.engine.run` with ``apply=False``
  inside the owner's ``tenant_tx()`` and answers the counts per outcome, the row issues (row numbers and codes, never a
  value) and the file's SHA-256. Writes nothing but one ``import_preview`` audit event.
- **Start** (``POST /crm/v1/client-imports?entity=&sha256=``, the same file): the SHA-256 must be the previewed one
  (409 ``file_changed``), the columns are checked again, the bytes go to the ingest bucket under
  ``imports/<tenant>/client-<import id>`` (the bucket's 1-day rule is the backstop) and the first
  ``crm_client_import`` job is queued in the request's transaction (ids, codes and counts only:
  ``app.jobs.payload_guard``).
- **The job** handles one slice of :data:`SLICE` rows in chunks of :data:`CHUNK` rows, one ``system_tx()`` per chunk
  (the engine with ``apply=True``), then queues the next slice as a new job (so an inquiry or a send queued meanwhile
  runs between two slices, like the mail history import) or, after the last slice, deletes the object. **Re-running the
  same file creates nothing** (the engine's ``already_imported``); a retried slice skips the rows its failed attempt
  committed the same way.
- **Progress** (``GET /crm/v1/client-imports/{id}``, ``GET /crm/v1/client-imports``) is read from the import's job rows
  (``payload.import_id``, this agency only): there is no import table, so a run leaves no personal data behind (the
  counts sit in ``crm.job.result``, kept by the job retention).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections import Counter
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound, Unavailable
from app.imports import engine, spec
from app.imports.formats import Table, read_table
from app.jobs import queue as jobs
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.mail.store import IMPORTS_PREFIX, IngestStore
from app.tenancy import TenantContext, system_tx
from app.vault import KeyProvider, Vault

log = logging.getLogger(__name__)

KIND = "crm_client_import"
ACTOR = "job:crm_client_import"
SLICE = 500
CHUNK = 100
MAX_ATTEMPTS = 3
LIST_LIMIT = 20
LIST_DAYS = 30
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REGION = re.compile(r"^[A-Z]{2}$")

JOBS_SQL = """
SELECT id::text AS job_id, status, payload, result, created_at, finished_at
  FROM crm.job WHERE kind = $1 AND payload->>'tenant_id' = $2 AND payload->>'import_id' = $3
 ORDER BY created_at, id
"""
RECENT_SQL = """
SELECT id::text AS job_id, status, payload, result, created_at, finished_at
  FROM crm.job
 WHERE kind = $1 AND payload->>'tenant_id' = $2 AND created_at > now() - make_interval(days => $3)
 ORDER BY created_at DESC, id
 LIMIT 500
"""


def object_key(tenant_id: uuid.UUID, import_id: uuid.UUID) -> str:
    return f"{IMPORTS_PREFIX}{tenant_id}/client-{import_id}"


def slice_key(import_id: uuid.UUID, batch: int) -> str:
    return f"{KIND}:{import_id}:{batch}"


def check_entity(entity: str) -> str:
    if entity not in spec.SPECS:
        raise Invalid("invalid_entity", ", ".join(spec.ENTITIES))
    return entity


def check_region(region: str | None) -> str | None:
    if region is None or region == "":
        return None
    code = region.strip().upper()
    if not _REGION.match(code):
        raise Invalid("invalid_phone_region", "an ISO 3166-1 alpha-2 code, such as VN or AU")
    return code


def _table(entity: str, data: bytes) -> tuple[Table, dict[str, list[str]]]:
    table = read_table(data)
    columns = spec.check_columns(spec.SPECS[entity], table.headers)
    return table, columns


async def preview(ctx: TenantContext, vault: Vault, *, entity: str, data: bytes, region: str | None) -> dict[str, Any]:
    entity, region = check_entity(entity), check_region(region)
    table, columns = _table(entity, data)
    results = await engine.run(ctx, vault, entity, table.rows, apply=False, member_id=ctx.member_id, region=region)
    summary = engine.summarise(results)
    ctx.audit("import_preview", "allowed", "client_import", None, reason=entity)
    return {
        "entity": entity,
        "format": table.format,
        "sha256": hashlib.sha256(data).hexdigest(),
        **columns,
        **summary,
        "can_apply": summary["counts"]["create"] + summary["counts"]["match"] > 0,
    }


async def start(
    ctx: TenantContext, store: IngestStore, *, entity: str, data: bytes, sha256: str, region: str | None
) -> dict[str, Any]:
    entity, region = check_entity(entity), check_region(region)
    if not _SHA256.match(sha256 or ""):
        raise Invalid("invalid_sha256", "the sha256 of the previewed file")
    table, _ = _table(entity, data)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise Conflict("file_changed", "the file differs from the one previewed: preview it again")
    import_id = uuid.uuid4()
    await store.put(object_key(ctx.tenant_id, import_id), data)
    await jobs.enqueue(
        ctx.conn,
        KIND,
        {
            "tenant_id": str(ctx.tenant_id),
            "member_id": str(ctx.member_id),
            "import_id": str(import_id),
            "kind": entity,
            "batch": 0,
            "row_count": len(table.rows),
            "region": region,
        },
        idempotency_key=slice_key(import_id, 0),
        max_attempts=MAX_ATTEMPTS,
        created_by="import",
    )
    ctx.audit("import_start", "allowed", "client_import", str(import_id), reason=entity)
    return {"import_id": str(import_id), "entity": entity, "status": "queued", "row_count": len(table.rows)}


def _aggregate(import_id: str, rows: list[asyncpg.Record]) -> dict[str, Any]:
    first = rows[0]["payload"]
    counts: Counter[str] = Counter()
    statuses = [r["status"] for r in rows]
    done = False
    finished_at = None
    for r in rows:
        result = r["result"] or {}
        if r["status"] == "succeeded":
            for outcome in engine.OUTCOMES:
                counts[outcome] += int(result.get(f"{outcome}_count") or 0)
            counts["possible_duplicate"] += int(result.get("possible_duplicate_count") or 0)
            if result.get("status") == "done":
                done, finished_at = True, r["finished_at"]
    if any(s in ("failed", "cancelled") for s in statuses):
        status = "failed"
        finished_at = max((r["finished_at"] for r in rows if r["finished_at"]), default=None)
    elif done:
        status = "done"
    elif any(s in ("running", "succeeded") for s in statuses):
        status = "running"
    else:
        status = "queued"
    processed = sum(counts[o] for o in engine.OUTCOMES)
    return {
        "import_id": import_id,
        "entity": first.get("kind"),
        "status": status,
        "row_count": int(first.get("row_count") or 0),
        "processed_count": processed,
        "counts": {o: counts[o] for o in engine.OUTCOMES},
        "possible_duplicate_count": counts["possible_duplicate"],
        "created_at": rows[0]["created_at"].isoformat(),
        "finished_at": finished_at.isoformat() if finished_at else None,
    }


async def status(ctx: TenantContext, import_id: uuid.UUID) -> dict[str, Any]:
    rows = await ctx.conn.fetch(JOBS_SQL, KIND, str(ctx.tenant_id), str(import_id))
    if not rows:
        raise NotFound("import_not_found")
    return _aggregate(str(import_id), [_decoded(r) for r in rows])


async def recent(ctx: TenantContext) -> dict[str, Any]:
    grouped: dict[str, list[Any]] = {}
    for r in await ctx.conn.fetch(RECENT_SQL, KIND, str(ctx.tenant_id), LIST_DAYS):
        row = _decoded(r)
        grouped.setdefault(str(row["payload"].get("import_id")), []).append(row)
    items = [_aggregate(i, sorted(rows, key=lambda x: (x["created_at"], x["job_id"]))) for i, rows in grouped.items()]
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return {"imports": items[:LIST_LIMIT]}


def _decoded(r: asyncpg.Record) -> dict[str, Any]:
    def obj(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        return json.loads(value) if isinstance(value, str) else dict(value)

    return {**dict(r), "payload": obj(r["payload"]), "result": obj(r["result"])}


# ── the job ────────────────────────────────────────────────────────────────────────────────────────
def build_handler(store: IngestStore | None, keys: KeyProvider | None) -> Handler:
    async def client_import(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        try:
            tenant_id = uuid.UUID(str(job.payload["tenant_id"]))
            import_id = uuid.UUID(str(job.payload["import_id"]))
            member_id = uuid.UUID(str(job.payload["member_id"]))
            entity = check_entity(str(job.payload["kind"]))
            batch = int(job.payload.get("batch") or 0)
            region = check_region(job.payload.get("region"))
        except (KeyError, ValueError, TypeError, Invalid):
            raise PermanentError("bad_payload") from None
        if batch < 0:
            raise PermanentError("bad_payload")
        if store is None or keys is None:
            raise RuntimeError("client import not configured")  # retried
        key = object_key(tenant_id, import_id)
        try:
            data = await store.get(key)
        except Exception as exc:
            if _missing(exc):  # applied after the bucket's 1-day rule removed it, or a retry after the last slice
                raise PermanentError("file_gone") from None
            raise
        try:
            table, _ = _table(entity, data)
        except Invalid as exc:
            await store.delete(key)
            raise PermanentError(exc.code) from None
        rows = table.rows[batch * SLICE : (batch + 1) * SLICE]
        results: list[engine.Result] = []
        state = engine.State()
        try:
            for start_at in range(0, len(rows), CHUNK):
                async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
                    results += await engine.run(
                        ctx,
                        Vault(keys, ctx),
                        entity,
                        rows[start_at : start_at + CHUNK],
                        apply=True,
                        member_id=member_id,
                        region=region,
                        state=state,
                    )
        except Exception:
            if job.attempt >= job.max_attempts:  # the last attempt: no object left behind
                await store.delete(key)
            raise
        more = (batch + 1) * SLICE < len(table.rows)
        if more:
            await jobs.enqueue(
                pool,
                KIND,
                {
                    "tenant_id": str(tenant_id),
                    "member_id": str(member_id),
                    "import_id": str(import_id),
                    "kind": entity,
                    "batch": batch + 1,
                    "row_count": len(table.rows),
                    "region": region,
                },
                idempotency_key=slice_key(import_id, batch + 1),
                max_attempts=MAX_ATTEMPTS,
                created_by="import",
            )
        else:
            await store.delete(key)
        summary = engine.summarise(results)
        out: dict[str, Any] = {
            "import_id": str(import_id),
            "status": "running" if more else "done",
            "batch": batch,
            "possible_duplicate_count": summary["possible_duplicate_count"],
        }
        out.update({f"{o}_count": n for o, n in summary["counts"].items()})
        log.info("client_import_slice entity=%s batch=%s rows=%s", entity, batch, len(rows))
        return out

    return client_import


def _missing(exc: Exception) -> bool:
    if isinstance(exc, Unavailable | KeyError | FileNotFoundError):
        return True
    error = getattr(exc, "response", None)
    return isinstance(error, dict) and error.get("Error", {}).get("Code") in ("NoSuchKey", "404")
