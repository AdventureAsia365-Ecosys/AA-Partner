"""``crm_mail_ingest``: forwarded mail from the ingest bucket into conversations (AA-945; lane §6.5; cards R and E).

Scheduling: :class:`MailIngestScan` is a worker maintenance hook. About once a minute it lists ``raw/`` (one
``ListObjectsV2``, at most one key) and, only when something is waiting, queues one ``crm_mail_ingest`` job for that
minute (idempotency key per UTC minute, payload ``{"purpose": "mail_ingest"}``: the object keys never enter
``crm.job``). Nothing is queued ahead, so an idle mailbox never holds a queued row (the deploy gate and DB Migrate count
queued rows as busy).

The job handles up to :data:`BATCH` objects, each on its own, paging past objects that failed in this run (at most
:data:`MAX_PAGES` listings), so a few bad objects never starve new mail:

1. read and parse it (:mod:`app.mail.parse`); not a mail, or any error the parser raises -> deleted, ``unparsable``;
2. SES's spam or virus scan said FAIL -> deleted, counted as dropped. The forward-in recipient's local part opens
   :func:`app.tenancy.ingest_tx`. Unknown, inactive or an agency that is not active -> the object is **deleted**,
   nothing is stored, one count-only log line ``mail_ingest_dropped`` and the ``MailIngestDropped`` metric (card R);
3. Google's forwarding confirmation -> the code goes to ``ingest_address.forwarding_confirmation_enc`` (mail DEK) and
   nothing else is kept;
4. otherwise the mail is threaded (:mod:`app.mail.threads`) and stored as one ``crm.message`` under the agency's
   ``mail`` DEK (subject, text, HTML, From, To / Cc, the Message-ID), From's blind index (the traveller e-mail index),
   the header hashes. ``ON CONFLICT`` on the Message-ID hash: a mail that arrives twice (BCC + forward) is stored
   once. A copy of the agency's own outgoing mail (From = one of its mailboxes **and** SES authenticated the From
   domain, ``ParsedMail.sender_authenticated``) is stored as a sent outbound message; a mail that only claims such a
   From is stored as an ordinary inbound mail (no one can plant "sent" mail in a client's timeline);
5. attachments are kept as ``crm.document`` rows (docs DEK, ``uploaded_by_kind = mail``) when the conversation has a
   client or a trip and the file is an allowed type of at most 7 MiB; otherwise a code in ``attachment_notes``
   (``no_subject``, ``too_large``, ``type_not_allowed``) the UI shows. The rows go in with the message in one
   savepoint and the encrypted files are put to S3 only after the message row is in, so a duplicate leaves nothing;
6. the raw object is deleted after the commit. A read, database or key error leaves it for the next run and counts
   ``failed``; once the object is older than :data:`POISON_AGE` it is deleted instead (``failed_permanent``, the
   ``MailIngestDropped`` metric), so a mail that can never be stored does not keep ``crm.job`` busy every minute until
   the 1-day bucket lifecycle.

One audit event per stored mail (action ``mail_ingest``, ids and the match code). The result is counts only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app.documents.service import CONTENT_TYPES, MAX_BYTES, object_key, sniff
from app.documents.store import DocumentStore
from app.errors import NotFound
from app.jobs import queue
from app.jobs.kinds import MAIL_INGEST
from app.jobs.queue import Job
from app.jobs.worker import Handler
from app.mail import threads
from app.mail.metrics import DROPPED
from app.mail.parse import ParsedMail, UnparsableMail, parse
from app.mail.store import RAW_PREFIX, SES_SETUP_KEY_SUFFIX, IngestStore, StoredObject
from app.tenancy import IngestAddress, SystemContext, ingest_tx
from app.vault import KeyProvider, Vault
from app.vault.blind_index import NormalisationError

log = logging.getLogger(__name__)

BATCH = 50
MAX_PAGES = 5
POISON_AGE = dt.timedelta(hours=1)
SCAN_EVERY_S = 55.0
SCHEDULER = "scheduler:crm-mail-ingest"
MAX_ATTEMPTS = 3
HISTORY_DAYS = 2
MAX_ATTACHMENTS = 20
PURPOSE = "mail"

INSERT_MESSAGE_SQL = """
INSERT INTO crm.message (message_id, conversation_id, direction, status, sender_kind, channel_account_id,
                         subject_enc, body_enc, body_html_enc, from_enc, from_bidx, to_enc, rfc_message_id_hash,
                         rfc_message_id_enc, in_reply_to_hash, references_hashes, match_method, occurred_at, sent_at,
                         imported, attachments, attachment_notes)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22)
ON CONFLICT ON CONSTRAINT message_rfc_id_uq DO NOTHING
RETURNING message_id
"""
SEEN_SQL = "SELECT 1 FROM crm.message WHERE rfc_message_id_hash = $1"
TOUCH_CONVERSATION_SQL = """
UPDATE crm.conversation SET last_message_at = greatest(last_message_at, $2) WHERE conversation_id = $1
"""
OWN_MAILBOX_SQL = """
SELECT channel_account_id FROM crm.channel_account WHERE address_bidx = $1 AND deleted_at IS NULL
 ORDER BY updated_at DESC LIMIT 1
"""
INSERT_DOCUMENT_SQL = """
INSERT INTO crm.document (document_id, trip_id, traveler_id, kind, s3_key, sha256, size_bytes, content_type,
                          filename_enc, uploaded_by_kind)
VALUES ($1, $2, $3, 'other', $4, $5, $6, $7, $8, 'mail')
"""
FORWARDING_CODE_SQL = """
UPDATE crm.ingest_address SET forwarding_confirmation_enc = $2, forwarding_code_at = now()
 WHERE ingest_address_id = $1
"""


async def _bidx(vault: Vault, address: str | None) -> bytes | None:
    if not address:
        return None
    try:
        return await vault.email_index("traveler", "email", address)
    except NormalisationError:
        return None


async def store_message(
    ctx: SystemContext,
    vault: Vault,
    documents: DocumentStore | None,
    mail: ParsedMail,
    *,
    imported: bool = False,
) -> tuple[str, threads.Thread | None]:
    """Thread and store one parsed mail in the open context. Returns (outcome, thread): ``stored`` or ``duplicate``.
    Shared by the forward-in ingest and the history import (:mod:`app.mail.history_import`).

    A stored message is sealed (crm-015 ``crm.guard_message``, CRM29), so its attachments are kept first and written
    with the row; a Message-ID already stored is a duplicate before anything is threaded or kept (the ``ON CONFLICT``
    covers a race between two workers: the savepoint then rolls the document rows back and no file is put).

    A From of one of the agency's mailboxes makes the mail the agency's own sent copy only when SES authenticated the
    From domain, or for an import (the member uploaded their own mailbox export)."""
    if mail.message_id_hash is not None and await ctx.conn.fetchval(SEEN_SQL, mail.message_id_hash):
        return "duplicate", None
    sender_bidx = await _bidx(vault, mail.from_address)
    trusted_from = imported or mail.sender_authenticated
    own_mailbox = await ctx.conn.fetchval(OWN_MAILBOX_SQL, sender_bidx) if sender_bidx and trusted_from else None
    recipients = [b for b in [await _bidx(vault, a) for a in mail.to_addresses] if b]
    refs = list(mail.references_hashes)
    if mail.in_reply_to_hash and mail.in_reply_to_hash not in refs:
        refs.append(mail.in_reply_to_hash)
    message_id = uuid.uuid4()
    occurred = mail.date or datetime.now(UTC)
    if occurred > datetime.now(UTC) + dt.timedelta(days=1):
        occurred = datetime.now(UTC)  # a sender's clock far ahead must not pin the thread on top

    async def seal(column: str, value: str | None) -> bytes | None:
        return await vault.encrypt("message", column, message_id, value, purpose=PURPOSE) if value else None

    outbound = own_mailbox is not None
    try:
        # A savepoint inside the job's transaction: a duplicate found by the ON CONFLICT rolls back the conversation
        # the threading may have opened and the document rows.
        async with ctx.conn.transaction():
            found = await threads.thread(
                ctx,
                reference_hashes=refs,
                trip_token=mail.trip_token,
                client_bidxs=recipients if own_mailbox else ([sender_bidx] if sender_bidx else []),
                sender_bidx=sender_bidx,
            )
            kept, notes, files = (
                await _attachments(ctx, vault, documents, mail, found) if mail.attachments else ([], [], [])
            )
            stored = await _insert_message(
                ctx, message_id, found, outbound, own_mailbox, seal, mail, sender_bidx, occurred, imported, kept, notes
            )
            if stored is None:
                raise _Duplicate
    except _Duplicate:
        return "duplicate", None
    if documents is not None:
        for key, sealed in files:  # only once the message row is in
            await documents.put(key, sealed)
    await ctx.conn.execute(TOUCH_CONVERSATION_SQL, found.conversation_id, occurred)
    ctx.audit("mail_ingest", "allowed", "message", str(message_id), reason=found.match_method)
    return "stored", found


class _Duplicate(Exception):
    """The ``ON CONFLICT`` found the Message-ID stored by another worker: roll the savepoint back."""


async def _insert_message(
    ctx: SystemContext,
    message_id: uuid.UUID,
    found: threads.Thread,
    outbound: bool,
    own_mailbox: uuid.UUID | None,
    seal: Callable[[str, str | None], Awaitable[bytes | None]],
    mail: ParsedMail,
    sender_bidx: bytes | None,
    occurred: datetime,
    imported: bool,
    kept: list[uuid.UUID],
    notes: list[str],
) -> uuid.UUID | None:
    stored: uuid.UUID | None = await ctx.conn.fetchval(
        INSERT_MESSAGE_SQL,
        message_id,
        found.conversation_id,
        "out" if outbound else "in",
        "sent" if outbound else "received",
        "agency_mailbox" if outbound else None,
        own_mailbox,
        await seal("subject_enc", mail.subject),
        await seal("body_enc", mail.text),
        await seal("body_html_enc", mail.html),
        await seal("from_enc", mail.from_address),
        sender_bidx,
        await seal("to_enc", json.dumps(list(mail.to_addresses)) if mail.to_addresses else None),
        mail.message_id_hash,
        await seal("rfc_message_id_enc", mail.message_id),
        mail.in_reply_to_hash,
        list(mail.references_hashes),
        found.match_method,
        occurred,
        occurred if outbound else None,
        imported,
        kept,
        notes,
    )
    return stored


async def _attachments(
    ctx: SystemContext,
    vault: Vault,
    documents: DocumentStore | None,
    mail: ParsedMail,
    found: threads.Thread,
) -> tuple[list[uuid.UUID], list[str], list[tuple[str, bytes]]]:
    """The kept document ids, the notes, and the (S3 key, sealed bytes) to put once the message row is in."""
    kept: list[uuid.UUID] = []
    notes: list[str] = []
    files: list[tuple[str, bytes]] = []
    for item in mail.attachments[:MAX_ATTACHMENTS]:
        if found.trip_id is None and found.traveler_id is None:
            notes.append("no_subject")
        elif documents is None:
            notes.append("not_configured")
        elif not 1 <= len(item.data) <= MAX_BYTES:
            notes.append("too_large")
        elif sniff(item.data) not in CONTENT_TYPES:
            notes.append("type_not_allowed")
        else:
            document_id = uuid.uuid4()
            sealed = await vault.encrypt("document", "content", document_id, item.data, purpose="docs")
            key = object_key(ctx.tenant_id, document_id)
            await ctx.conn.execute(
                INSERT_DOCUMENT_SQL,
                document_id,
                found.trip_id,
                found.traveler_id,
                key,
                hashlib.sha256(sealed).digest(),
                len(item.data),
                sniff(item.data),
                await vault.encrypt("document", "filename_enc", document_id, item.filename or "attachment"),
            )
            files.append((key, sealed))
            kept.append(document_id)
    notes += ["too_many"] * (len(mail.attachments) > MAX_ATTACHMENTS)
    return kept, notes, files


async def ingest_one(
    ctx: SystemContext,
    vault: Vault,
    documents: DocumentStore | None,
    address: IngestAddress,
    mail: ParsedMail,
) -> str:
    if mail.forwarding_code:
        blob = await vault.encrypt(
            "ingest_address",
            "forwarding_confirmation_enc",
            address.ingest_address_id,
            mail.forwarding_code,
            purpose=PURPOSE,
        )
        await ctx.conn.execute(FORWARDING_CODE_SQL, address.ingest_address_id, blob)
        ctx.audit("mail_forwarding_code", "allowed", "ingest_address", str(address.ingest_address_id))
        return "forwarding_code"
    outcome, _ = await store_message(ctx, vault, documents, mail)
    return outcome


def build_handler(
    store: IngestStore | None,
    documents: DocumentStore | None,
    keys: KeyProvider | None,
    ingest_domain: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Handler:
    async def mail_ingest(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        counts: Counter[str] = Counter()
        if store is None or not ingest_domain:
            return {"status": "not_configured"}
        if keys is None:
            raise RuntimeError("key provider not configured")  # retried: the raw mail waits in the bucket
        handled = 0
        start_after: str | None = None
        for _ in range(MAX_PAGES):
            page = await store.list_objects(RAW_PREFIX, BATCH, start_after)
            for item in page:
                if handled >= BATCH:
                    break
                if await _one(pool, store, documents, keys, ingest_domain, item, now(), counts):
                    handled += 1
            if handled >= BATCH or len(page) < BATCH:
                break
            start_after = page[-1].key  # page past the objects that failed in this run
        if counts:
            log.info("mail_ingest %s", " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return {f"{k}_count": int(v) for k, v in sorted(counts.items())}

    return mail_ingest


async def _one(
    pool: asyncpg.Pool,
    store: IngestStore,
    documents: DocumentStore | None,
    keys: KeyProvider,
    ingest_domain: str,
    item: StoredObject,
    now: datetime,
    counts: Counter[str],
) -> bool:
    """One raw object. True when it was handled (stored, dropped or deleted); False when it failed and stays."""
    key = item.key
    if key.endswith(SES_SETUP_KEY_SUFFIX):
        await store.delete(key)
        counts["setup_notice"] += 1
        return True
    try:
        raw = await store.get(key)
    except Exception as exc:  # noqa: BLE001 — one object must not stop the batch
        return await _failed(store, item, now, "read", type(exc).__name__, counts)
    try:
        mail = parse(raw, ingest_domain=ingest_domain)
    except Exception as exc:  # noqa: BLE001 — a parser error on these bytes will never pass: not a mail
        await store.delete(key)
        counts["unparsable"] += 1
        if not isinstance(exc, UnparsableMail):
            log.info("mail_ingest_unparsable error_class=%s", type(exc).__name__)
        return True
    if not mail.local_part:
        await _drop(store, key, "no_recipient", counts)
        return True
    if mail.spam_or_virus:
        await _drop(store, key, "spam_or_virus", counts)
        return True
    try:
        async with ingest_tx(pool, mail.local_part) as (ctx, address):
            outcome = await ingest_one(ctx, Vault(keys, ctx), documents, address, mail)
    except NotFound as exc:
        if exc.code != "address_not_found":
            return await _failed(store, item, now, "store", exc.code, counts)
        await _drop(store, key, "unknown_address", counts)
        return True
    except Exception as exc:  # noqa: BLE001 — kept for the next run until POISON_AGE
        return await _failed(store, item, now, "store", type(exc).__name__, counts)
    await store.delete(key)
    counts[outcome] += 1
    return True


async def _failed(
    store: IngestStore, item: StoredObject, now: datetime, stage: str, error: str, counts: Counter[str]
) -> bool:
    """A failure: kept for the next run, or dropped once older than :data:`POISON_AGE` (it keeps failing)."""
    log.error("mail_ingest_failed stage=%s error=%s", stage, error)
    if now - item.last_modified >= POISON_AGE:
        await _drop(store, item.key, "failed_permanent", counts)
        counts["failed_permanent"] += 1
        return True
    counts["failed"] += 1
    return False


async def _drop(store: IngestStore, key: str, reason: str, counts: Counter[str]) -> None:
    """Card R: discard the raw object, keep nothing, log the count only."""
    await store.delete(key)
    counts["dropped"] += 1
    DROPPED.add()
    log.info("mail_ingest_dropped reason=%s", reason)


def schedule_key(now: datetime) -> str:
    return f"{MAIL_INGEST}:schedule:{now.strftime('%Y-%m-%dT%H:%M')}"


class MailIngestScan:
    """Worker maintenance hook: when ``raw/`` holds an object, one ``crm_mail_ingest`` job for this minute, due now."""

    def __init__(
        self,
        store: IngestStore | None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        every_s: float = SCAN_EVERY_S,
    ) -> None:
        self._store = store
        self._now = now
        self._every_s = every_s
        self._last: datetime | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        if self._store is None:
            return None
        now = self._now()
        if self._last is not None and (now - self._last).total_seconds() < self._every_s:
            return None
        self._last = now
        if not await self._store.list_keys(RAW_PREFIX, 1):
            return None
        job_id = await queue.enqueue(
            pool,
            MAIL_INGEST,
            {"purpose": "mail_ingest"},
            idempotency_key=schedule_key(now),
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, MAIL_INGEST, HISTORY_DAYS)
        return job_id
