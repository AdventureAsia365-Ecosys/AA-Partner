"""Mail history import (AA-945; card CRM-17 checklist "history import"): an mbox file (Google Takeout, Thunderbird) or
a CSV (``date, from, to, subject, body``) of a member's past mail, at most 25 MB.

- ``POST /crm/v1/mail/imports?source_kind=mbox|csv`` (every role, ``OWN_MAILBOX_CONNECT``): the body is read in chunks
  and refused above :data:`MAX_BYTES` (413 ``import_too_large``); a ``crm.mail_import`` row (counts only) is written,
  the bytes go to the ingest bucket under ``imports/<tenant>/<import id>`` and one ``crm_mail_history_import`` job is
  queued (ids only).
- Each job handles one **slice** of :data:`SLICE` messages (payload ``batch`` = the slice number), then queues the
  next slice as a new job. The worker runs one job at a time, so a large import never holds it for long: a client's
  send (``crm_dispatch``, 10 s Undo) or forwarded mail (``crm_mail_ingest``) queued meanwhile runs between two slices.
  A slice reads the object, skips the messages of the earlier slices, parses each message
  (:func:`app.mail.parse.parse`; a CSV row becomes the same structure) and stores it through the forward-in path
  (:func:`app.mail.ingest.store_message`: thread, encrypt, attachments) with ``imported = true``, :data:`CHUNK`
  messages per transaction. A message without a Message-ID gets a stable one from its content hash, so **importing the
  same file again adds nothing** (the per-agency Message-ID hash is unique; a retried slice adds nothing either). The
  counts are added to the row; the object is deleted after the last slice (the bucket's 1-day lifecycle is the
  backstop). A slice whose last attempt fails marks the import ``failed`` (``import_failed``) instead of leaving it
  ``running``.
"""

from __future__ import annotations

import csv
import datetime as dt
import email.utils
import hashlib
import io
import logging
import mailbox
import os
import tempfile
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import asyncpg

from app.documents.store import DocumentStore
from app.errors import Invalid, NotFound
from app.jobs import queue as jobs
from app.jobs.kinds import MAIL_IMPORT
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.mail.ingest import store_message
from app.mail.parse import ParsedMail, UnparsableMail, id_hash, normalise_address, parse
from app.mail.store import IMPORTS_PREFIX, IngestStore
from app.tenancy import SystemContext, TenantContext, system_tx
from app.vault import KeyProvider, Vault

log = logging.getLogger(__name__)

MAX_BYTES = 25 * 1024 * 1024
CHUNK = 100
SLICE = 500
MAX_ATTEMPTS = 3
MAX_MESSAGES = 20_000
ACTOR = "job:crm_mail_history_import"
SOURCE_KINDS = ("mbox", "csv")
SYNTHETIC_DOMAIN = "import.crm.invalid"

INSERT_SQL = """
INSERT INTO crm.mail_import (mail_import_id, member_id, source_kind, size_bytes) VALUES ($1, $2, $3, $4)
RETURNING created_at
"""
ROW_SQL = """
SELECT mail_import_id, member_id, source_kind, size_bytes, status, total_count, imported_count, duplicate_count,
       skipped_count, failed_count, error_code, created_at, finished_at
  FROM crm.mail_import WHERE mail_import_id = $1 AND deleted_at IS NULL
"""
RUNNING_SQL = "UPDATE crm.mail_import SET status = 'running' WHERE mail_import_id = $1 AND status = 'queued'"
COUNTS_SQL = """
UPDATE crm.mail_import SET total_count = total_count + $2, imported_count = imported_count + $3,
                           duplicate_count = duplicate_count + $4, skipped_count = skipped_count + $5,
                           failed_count = failed_count + $6
 WHERE mail_import_id = $1
"""
FINISH_SQL = """
UPDATE crm.mail_import SET status = $2, error_code = $3, finished_at = now() WHERE mail_import_id = $1
   AND status IN ('queued', 'running')
"""


def object_key(tenant_id: uuid.UUID, import_id: uuid.UUID) -> str:
    return f"{IMPORTS_PREFIX}{tenant_id}/{import_id}"


def slice_key(import_id: uuid.UUID, batch: int) -> str:
    # Slice 0 keeps the key of the single-job version, so a job queued before this change is not queued twice.
    return f"{MAIL_IMPORT}:{import_id}" if batch == 0 else f"{MAIL_IMPORT}:{import_id}:{batch}"


async def create_import(ctx: TenantContext, store: IngestStore, *, source_kind: str, data: bytes) -> dict[str, Any]:
    if source_kind not in SOURCE_KINDS:
        raise Invalid("invalid_source_kind", "mbox or csv")
    if not data:
        raise Invalid("empty_upload")
    import_id = uuid.uuid4()
    created = await ctx.conn.fetchval(INSERT_SQL, import_id, ctx.member_id, source_kind, len(data))
    await store.put(object_key(ctx.tenant_id, import_id), data)
    await jobs.enqueue(
        ctx.conn,
        MAIL_IMPORT,
        {"tenant_id": str(ctx.tenant_id), "mail_import_id": str(import_id)},
        idempotency_key=slice_key(import_id, 0),
        max_attempts=MAX_ATTEMPTS,
        created_by="mail_import",
    )
    ctx.audit("mail_import", "allowed", "mail_import", str(import_id), reason=source_kind)
    return {"mail_import_id": str(import_id), "status": "queued", "created_at": created.isoformat()}


async def import_status(ctx: TenantContext, import_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ROW_SQL, import_id)
    if row is None or (row["member_id"] != ctx.member_id and ctx.member_role != "owner"):
        raise NotFound("mail_import_not_found")
    return {
        "mail_import_id": str(row["mail_import_id"]),
        "source_kind": row["source_kind"],
        "size_bytes": row["size_bytes"],
        "status": row["status"],
        "total_count": row["total_count"],
        "imported_count": row["imported_count"],
        "duplicate_count": row["duplicate_count"],
        "skipped_count": row["skipped_count"],
        "failed_count": row["failed_count"],
        "error_code": row["error_code"],
        "created_at": row["created_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


def _synthetic_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest() + "@" + SYNTHETIC_DOMAIN


def mbox_messages(data: bytes, ingest_domain: str, skip: int = 0) -> Iterator[ParsedMail | None]:
    """Each message of an mbox after the first ``skip`` (None for one that does not parse; the skipped ones are not
    parsed). ``mailbox.mbox`` needs a path: a private temp file, removed at once."""
    fd, path = tempfile.mkstemp(prefix="crm-import-", suffix=".mbox")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        box = mailbox.mbox(path, create=False)
        try:
            for index, key in enumerate(box.iterkeys()):
                if index < skip:
                    continue
                raw = box.get_bytes(key)
                try:
                    mail = parse(raw, ingest_domain=ingest_domain)
                except UnparsableMail:
                    yield None
                    continue
                if mail.message_id_hash is None:
                    digest = hashlib.sha256(raw).hexdigest()
                    synthetic = f"{digest}@{SYNTHETIC_DOMAIN}"
                    mail = _with_id(mail, synthetic)
                yield mail
        finally:
            box.close()
    finally:
        os.unlink(path)


def _with_id(mail: ParsedMail, message_id: str) -> ParsedMail:
    return replace(mail, message_id=message_id, message_id_hash=id_hash(message_id))


def csv_messages(data: bytes, skip: int = 0) -> Iterator[ParsedMail | None]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fields = {f.strip().lower() for f in reader.fieldnames or []}
    if not {"date", "from", "subject", "body"} <= fields:
        raise PermanentError("csv_columns")
    for index, raw in enumerate(reader):
        if index < skip:
            continue
        row = {(k or "").strip().lower(): (v or "") for k, v in raw.items()}
        sender = normalise_address(row.get("from"))
        if not sender or not (row.get("body") or row.get("subject")):
            yield None
            continue
        try:
            date = email.utils.parsedate_to_datetime(row["date"]) if row.get("date") else None
        except (TypeError, ValueError, IndexError):
            date = None
        if date is not None and date.tzinfo is None:
            date = date.replace(tzinfo=dt.UTC)
        to = tuple(a for a in (normalise_address(x) for x in (row.get("to") or "").replace(";", ",").split(",")) if a)
        message_id = _synthetic_id(row.get("date", ""), sender, row.get("subject", ""), row.get("body", ""))
        yield ParsedMail(
            message_id=message_id,
            message_id_hash=id_hash(message_id),
            in_reply_to_hash=None,
            references_hashes=(),
            subject=" ".join(row.get("subject", "").split())[:500],
            from_address=sender,
            to_addresses=to,
            date=date,
            text=row.get("body", "")[:200_000],
            html=None,
        )


def build_handler(
    store: IngestStore | None, documents: DocumentStore | None, keys: KeyProvider | None, ingest_domain: str
) -> Handler:
    async def history_import(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        try:
            tenant_id = uuid.UUID(str(job.payload["tenant_id"]))
            import_id = uuid.UUID(str(job.payload["mail_import_id"]))
            batch = int(job.payload.get("batch") or 0)
        except (KeyError, ValueError, TypeError):
            raise PermanentError("bad_payload") from None
        if batch < 0:
            raise PermanentError("bad_payload")
        if store is None or keys is None:
            raise RuntimeError("mail import not configured")  # retried
        key = object_key(tenant_id, import_id)
        async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
            row = await ctx.conn.fetchrow(ROW_SQL, import_id)
            if row is None or row["status"] not in ("queued", "running"):
                return {"mail_import_id": str(import_id), "status": "skipped"}
            await ctx.conn.execute(RUNNING_SQL, import_id)
            source_kind = row["source_kind"]
        try:
            data = await store.get(key)
            start = batch * SLICE
            messages = (
                mbox_messages(data, ingest_domain, skip=start)
                if source_kind == "mbox"
                else csv_messages(data, skip=start)
            )
            counts, more = await _import(pool, tenant_id, import_id, keys, documents, messages, start=start)
        except PermanentError as exc:
            await _finish(pool, tenant_id, import_id, "failed", exc.code)
            await store.delete(key)
            return {"mail_import_id": str(import_id), "status": "failed", "reason_code": exc.code}
        except Exception:
            if job.attempt >= job.max_attempts:  # the last attempt: the import must not stay "running"
                await _finish(pool, tenant_id, import_id, "failed", "import_failed")
                await store.delete(key)
            raise
        if more:
            await jobs.enqueue(
                pool,
                MAIL_IMPORT,
                {"tenant_id": str(tenant_id), "mail_import_id": str(import_id), "batch": batch + 1},
                idempotency_key=slice_key(import_id, batch + 1),
                max_attempts=MAX_ATTEMPTS,
                created_by="mail_import",
            )
            return {"mail_import_id": str(import_id), "status": "running", "batch": batch, **_counts(counts)}
        await _finish(pool, tenant_id, import_id, "done", None)
        await store.delete(key)
        return {"mail_import_id": str(import_id), "status": "done", **_counts(counts)}

    return history_import


def _counts(counts: dict[str, int]) -> dict[str, int]:
    return {f"{k}_count": v for k, v in counts.items()}


async def _import(
    pool: asyncpg.Pool,
    tenant_id: uuid.UUID,
    import_id: uuid.UUID,
    keys: KeyProvider,
    documents: DocumentStore | None,
    messages: Iterator[ParsedMail | None],
    *,
    start: int = 0,
) -> tuple[dict[str, int], bool]:
    """Store up to :data:`SLICE` messages from ``messages`` (message number ``start`` on). Returns this slice's counts
    and whether another slice follows. Past :data:`MAX_MESSAGES` the rest is counted as skipped in this slice."""
    counts: Counter[str] = Counter()
    batch: list[ParsedMail | None] = []

    async def flush() -> None:
        before = dict(counts)
        async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
            await _store_batch(ctx, keys, documents, batch, counts)

            def delta(name: str) -> int:
                return counts[name] - before.get(name, 0)

            await ctx.conn.execute(
                COUNTS_SQL,
                import_id,
                delta("total"),
                delta("stored"),
                delta("duplicate"),
                delta("skipped"),
                delta("failed"),
            )
        batch.clear()

    index = start
    more = False
    over_cap = 0
    for mail in messages:
        if index >= MAX_MESSAGES:
            over_cap += 1
            continue
        if index >= start + SLICE:
            more = True
            break
        batch.append(mail)
        index += 1
        if len(batch) >= CHUNK:
            await flush()
    if batch:
        await flush()
    if over_cap:
        counts["skipped"] += over_cap
        async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
            await ctx.conn.execute(COUNTS_SQL, import_id, 0, 0, 0, over_cap, 0)
    return {k: int(v) for k, v in counts.items()}, more


async def _store_batch(
    ctx: SystemContext,
    keys: KeyProvider,
    documents: DocumentStore | None,
    batch: list[ParsedMail | None],
    counts: Counter[str],
) -> None:
    vault = Vault(keys, ctx)
    for mail in batch:
        counts["total"] += 1
        if mail is None:
            counts["skipped"] += 1
            continue
        try:
            async with ctx.conn.transaction():
                outcome, _ = await store_message(ctx, vault, documents, mail, imported=True)
        except Exception as exc:  # noqa: BLE001 — one bad message must not stop the import
            counts["failed"] += 1
            log.warning("mail_import_message_failed error_class=%s", type(exc).__name__)
            continue
        counts[outcome] += 1


async def _finish(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, import_id: uuid.UUID, status: str, code: str | None
) -> None:
    async with system_tx(pool, tenant_id, actor=ACTOR) as ctx:
        await ctx.conn.execute(FINISH_SQL, import_id, status, code)
