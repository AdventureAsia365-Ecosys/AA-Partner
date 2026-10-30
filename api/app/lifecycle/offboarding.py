"""Agency offboarding, end to end: export, then erase (AA-942; lane §5.1 "Tenant offboarding", §5.8, design §4.10).

Owner steps (``/crm/v1/lifecycle/offboarding``, :mod:`app.routers.lifecycle`):

1. **Request**: the dry-run counts of everything the erase would delete are stored on a new ``crm.erasure_request``
   (scope ``tenant``); refused while ANY legal hold of the agency is active (agency, trip or client hold: 409
   ``legal_hold``) or while one is open (409 ``offboarding_open``).
   Unless the owner waives it, a ``crm_offboard`` job builds the **export**: every row of the agency's tenant tables,
   personal fields decrypted, as JSON lines (``*_bidx`` index columns, the F2 pointer and integration secrets left
   out; document files are listed, their content comes with CRM-35), sealed with the agency's ``export`` DEK and put
   in the documents bucket under ``exports/tenants/<tenant>/<request>.jsonl.enc`` (bucket rule: gone after 7 days).
   Status ``export_ready``; the owner downloads it (audited).
2. **Erase** (owner, typed confirmation = the agency id): refused in DRY RUN (409 ``deletion_dry_run`` with the
   counts), without an export unless waived, under any active legal hold. The agency moves to ``offboarding`` (no
   member context opens any more) and a ``crm_offboard`` job erases: ``crm.erase_subject('tenant')`` as
   ``crm_eraser`` (every tenant table except the keep list: the agency row, the destroyed key rows, the audit and its
   export registry / chain, the receipts, lifecycle counts and holds), then :meth:`KeyProvider.destroy`
   (**crypto-shred**: wrapped DEKs nulled, ``destroyed_at`` set, this process's cache evicted) and the canary verify,
   the receipt, and last the bucket objects (the documents, the offboarding export, the portability exports; AA-967
   review: after every step that can refuse), all in one transaction. The receipt (counts, ``backup_purge_by`` =
   today + 30 days, the time by which every other task's DEK cache has expired = destroy + ``DEK_CACHE_TTL_SECONDS``,
   the copies outside AA's control) goes on the request; the agency becomes ``offboarded``.

If the database refuses the erase (a hold placed after the confirmation, CRM16 a table the erase does not know) or the
verify fails, nothing was deleted (one transaction, and no object is deleted before the last step): the request
becomes ``failed`` with the code and the agency goes back to ``active``, so the owner can read why, cancel the request
and start again later. An erase job that meets DRY RUN (``LIFECYCLE_DRY_RUN`` switched on after the confirmation) is
the same refusal: ``failed`` with ``deletion_dry_run``, the agency ``active`` again, the job failed at once (review
S249). A job that fails for another reason (a bucket error on the final object deletes is retried; retries exhausted)
leaves the agency ``offboarding``; the platform recovery is in ``docs/implementation-notes/AA-942.md``.

**AA-raised requests** (AA-941, crm-011): the L1 console raises an agency export (``purpose = export_only``) or deletion
(``offboard``) request with :func:`raise_request` (no counts, no job). It waits for the owner: the owner confirms it
with :func:`accept` (counts for a deletion, then the export job, unless a deletion's export is waived) or declines it
with :func:`cancel`. Meanwhile the owner's own :func:`request` answers 409 ``data_request_awaiting`` and
:func:`confirm_erase` 409 ``data_request_awaiting``; an export-only request never erases (409 ``export_only_request``).
The crm-011 trigger (CRM18) holds the same rules in the database: AA can never accept, confirm or run a request.
:func:`data_request` is the L1 console's view of a request (ids, kind, a coarse status; no counts, no receipt).

The audit is not erased: it has no personal data, the hourly export keeps running for an offboarded agency until its
partitions drop (crm-007 offboarding path), and the Object Lock archive keeps it 7 years.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, NoReturn

import asyncpg

from app.documents.store import DocumentStore
from app.errors import Conflict, Forbidden, Invalid, NotFound, ServiceError, Unavailable
from app.jobs import queue as jobs
from app.jobs.kinds import OFFBOARD
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.lifecycle import erase
from app.lifecycle.rules import BACKUP_PURGE_DAYS, EXPORT_FILE_DAYS
from app.portability import builder as portability
from app.portability import exports as portability_exports
from app.tenancy import AnyContext, LifecycleContext, TenantContext, lifecycle_tx
from app.vault import Vault
from app.vault.errors import DecryptionDenied, KeyDestroyed, UnwrapDenied

if TYPE_CHECKING:  # pragma: no cover
    from app.vault import KeyProvider

log = logging.getLogger(__name__)

ACTOR = "job:crm_offboard"
MAX_ATTEMPTS = 3
EXPORT_FORMAT = 2  # AA-967: every exported table of the catalog (app.portability.tables), not a fixed list
# What stays outside AA's reach after the erase (lane §5.8 receipt): named as codes, worded by the web.
COPIES_OUTSIDE = ("agency_mailboxes_and_messaging_accounts", "supplier_manifests_already_sent", "agency_downloads")

# The export holds every tenant table the login role reads, minus the never-exported ones (keys, vendor secrets, short-
# lived codes, the audit with its own download): app.portability.tables (AA-967; AA-942 listed 24 tables by hand).
CANARY = b"crm-offboarding-canary"

OPEN_SQL = """
SELECT request_id, status, requested_by, owner_accepted_at FROM crm.erasure_request
 WHERE scope = 'tenant' AND status IN ('requested', 'export_ready', 'erasing', 'failed')
"""
INSERT_SQL = """
INSERT INTO crm.erasure_request (scope, subject_id, dry_run_counts, export_waived)
VALUES ('tenant', $1, $2::jsonb, $3)
RETURNING request_id, created_at
"""
REQUEST_SQL = "SELECT * FROM crm.erasure_request WHERE request_id = $1 AND scope = 'tenant' FOR UPDATE"
LATEST_SQL = """
SELECT * FROM crm.erasure_request WHERE scope = 'tenant' ORDER BY created_at DESC, request_id LIMIT 1
"""
CANCEL_SQL = "UPDATE crm.erasure_request SET status = 'cancelled' WHERE request_id = $1"
EXPORT_DONE_SQL = """
UPDATE crm.erasure_request
   SET status = 'export_ready', export_object_key = $2, export_sha256 = $3, failure_class = NULL,
       export_expires_at = now() + make_interval(days => $4)
 WHERE request_id = $1 AND status = 'requested'
"""
FAILURE_SQL = "UPDATE crm.erasure_request SET failure_class = $2 WHERE request_id = $1"
DOWNLOADED_SQL = "UPDATE crm.erasure_request SET export_downloaded_at = now() WHERE request_id = $1"
CONFIRM_SQL = "UPDATE crm.erasure_request SET status = 'erasing', confirmed_by = $2 WHERE request_id = $1"
# AA-967 (crm-026): the confirmation with a cool-off: the erase job waits until erase_not_before; the agency works on.
SCHEDULE_SQL = """
UPDATE crm.erasure_request SET confirmed_by = $2, erase_not_before = now() + make_interval(hours => $3)
 WHERE request_id = $1
"""
START_SQL = "UPDATE crm.erasure_request SET status = 'erasing' WHERE request_id = $1"
TENANT_OFFBOARDING_SQL = "UPDATE crm.tenant SET status = 'offboarding' WHERE tenant_id = $1"
TENANT_OFFBOARDED_SQL = "UPDATE crm.tenant SET status = 'offboarded' WHERE tenant_id = $1"
TENANT_BACK_SQL = "UPDATE crm.tenant SET status = 'active' WHERE tenant_id = $1 AND status = 'offboarding'"
ERASED_SQL = """
UPDATE crm.erasure_request
   SET status = 'erased', executed_at = now(), keys_destroyed_at = now(), backup_purge_by = $2, receipt = $3::jsonb,
       failure_class = NULL, verified_at = now()
 WHERE request_id = $1
"""
FAILED_SQL = "UPDATE crm.erasure_request SET status = 'failed', failure_class = $2 WHERE request_id = $1"
RETRY_SQL = "UPDATE crm.erasure_request SET status = 'erasing', failure_class = NULL WHERE request_id = $1"
# Scope tenant = ANY active hold of the agency (agency, trip or client): the erase reaches every subject.
HOLD_SQL = "SELECT crm.legal_hold_blocks('tenant', NULL)"


def export_key(tenant_id: uuid.UUID, request_id: uuid.UUID) -> str:
    return f"exports/tenants/{tenant_id}/{request_id}.jsonl.enc"


def _owner(ctx: TenantContext, action: str) -> None:
    if ctx.member_role != "owner":
        ctx.audit(action, "denied", "erasure_request", reason="owner_only")
        raise Forbidden("owner_only", "only the agency owner offboards the agency")


def raised_by_aa(row: Any) -> bool:
    """AA-941: a request the L1 console raised (crm-011: requested_by platform:*)."""
    return str(_get(row, "requested_by", "") or "").startswith("platform:")


def awaiting_owner(row: Any) -> bool:
    """AA-941: an AA-raised request the owner has not confirmed yet (still requested)."""
    return raised_by_aa(row) and _get(row, "owner_accepted_at") is None and row["status"] == "requested"


def _get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def view(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None

    def ts(v: Any) -> str | None:
        return v.isoformat() if v is not None else None

    counts = row["dry_run_counts"]
    receipt = row["receipt"]
    return {
        "request_id": str(row["request_id"]),
        "status": row["status"],
        # AA-941 (crm-011): what the request is for, who raised it, and whether it waits for the owner's confirmation.
        "purpose": _get(row, "purpose", "offboard"),
        "raised_by": "aa" if raised_by_aa(row) else "owner",
        "awaiting_owner": awaiting_owner(row),
        "dry_run_counts": json.loads(counts) if isinstance(counts, str) else dict(counts),
        "export_waived": row["export_waived"],
        "export_ready": row["export_object_key"] is not None,
        "export_expires_at": ts(row["export_expires_at"]),
        "export_downloaded_at": ts(row["export_downloaded_at"]),
        "executed_at": ts(row["executed_at"]),
        "keys_destroyed_at": ts(row["keys_destroyed_at"]),
        "backup_purge_by": row["backup_purge_by"].isoformat() if row["backup_purge_by"] else None,
        "receipt": json.loads(receipt) if isinstance(receipt, str) else dict(receipt),
        "failure_class": row["failure_class"],
        "created_at": ts(row["created_at"]),
        # AA-967 (crm-026): the confirmed deletion waits until erase_not_before (cool-off; the owner may cancel);
        # verified_at = the post-erase verification passed.
        "confirmed": _get(row, "confirmed_by") is not None,
        "erase_not_before": ts(_get(row, "erase_not_before")),
        "verified_at": ts(_get(row, "verified_at")),
    }


async def latest(ctx: TenantContext) -> dict[str, Any] | None:
    return view(await ctx.conn.fetchrow(LATEST_SQL))


async def request(ctx: TenantContext, *, export_waived: bool) -> dict[str, Any]:
    _owner(ctx, "offboarding_request")
    open_row = await ctx.conn.fetchrow(OPEN_SQL)
    if open_row is not None:
        if awaiting_owner(open_row):
            raise Conflict("data_request_awaiting", "a request AA raised waits for your answer: confirm or decline")
        raise Conflict("offboarding_open", "an offboarding request is already open")
    if await ctx.conn.fetchval(HOLD_SQL):
        ctx.audit("offboarding_request", "denied", "tenant", str(ctx.tenant_id), reason="legal_hold")
        raise Conflict("legal_hold", "an active legal hold in the agency suspends offboarding")
    counts = await erase.erase(ctx, "tenant", ctx.tenant_id, dry_run=True)
    row = await ctx.conn.fetchrow(INSERT_SQL, ctx.tenant_id, json.dumps(counts), export_waived)
    if not export_waived:
        await jobs.enqueue(
            ctx.conn,
            OFFBOARD,
            {"tenant_id": str(ctx.tenant_id), "request_id": str(row["request_id"]), "mode": "export"},
            idempotency_key=f"{OFFBOARD}:export:{row['request_id']}",
            max_attempts=MAX_ATTEMPTS,
            created_by="offboarding",
        )
    ctx.audit(
        "offboarding_request",
        "allowed",
        "erasure_request",
        str(row["request_id"]),
        reason="export_waived" if export_waived else "export_queued",
    )
    return view(await ctx.conn.fetchrow(REQUEST_SQL, row["request_id"])) or {}


async def _open_request(ctx: TenantContext, request_id: uuid.UUID) -> Any:
    row = await ctx.conn.fetchrow(REQUEST_SQL, request_id)
    if row is None:
        raise NotFound("request_not_found")
    return row


async def cancel(ctx: TenantContext, request_id: uuid.UUID, store: DocumentStore | None) -> dict[str, Any]:
    _owner(ctx, "offboarding_cancel")
    row = await _open_request(ctx, request_id)
    # A failed request is cancellable once the agency is active again (the refused erase deleted nothing).
    if row["status"] not in ("requested", "export_ready", "failed"):
        raise Conflict("not_cancellable", "the erase has started")
    declined = awaiting_owner(row)  # AA-941: the owner declines a request AA raised
    await ctx.conn.execute(CANCEL_SQL, request_id)
    if row["export_object_key"] and store is not None:
        await store.delete(row["export_object_key"])
    ctx.audit(
        "offboarding_cancel", "allowed", "erasure_request", str(request_id), reason="declined" if declined else None
    )
    return view(await ctx.conn.fetchrow(REQUEST_SQL, request_id)) or {}


async def download(ctx: TenantContext, vault: Vault, store: DocumentStore | None, request_id: uuid.UUID) -> bytes:
    """The export, opened (JSON lines). Owner only; one ``export_download`` audit event."""
    _owner(ctx, "export_download")
    row = await _open_request(ctx, request_id)
    if row["status"] != "export_ready" or row["export_object_key"] is None:
        raise Conflict("export_not_ready")
    if store is None:
        raise Unavailable("documents_not_configured")
    sealed = await store.get(row["export_object_key"])
    if hashlib.sha256(sealed).hexdigest() != row["export_sha256"]:
        ctx.audit("export_download", "denied", "erasure_request", str(request_id), reason="digest_mismatch")
        raise Unavailable("export_integrity", "the stored export does not match its digest")
    content = await vault.decrypt_bytes("erasure_request", "export", request_id, sealed, purpose="export")
    await ctx.conn.execute(DOWNLOADED_SQL, request_id)
    ctx.audit("export_download", "allowed", "erasure_request", str(request_id))
    return content


async def confirm_erase(
    ctx: TenantContext, request_id: uuid.UUID, *, confirm_tenant_id: uuid.UUID, dry_run: bool, cool_off_hours: int = 0
) -> dict[str, Any]:
    """The owner's typed confirmation. With ``cool_off_hours`` (AA-967, ``OFFBOARDING_COOL_OFF_HOURS``) the erase job is
    queued for the end of the cool-off and the agency keeps working (Cancel stays open); without, it starts now."""
    _owner(ctx, "offboarding_erase")
    if confirm_tenant_id != ctx.tenant_id:
        raise Invalid("confirmation_mismatch", "type the agency id to confirm")
    row = await _open_request(ctx, request_id)
    if _get(row, "purpose", "offboard") == "export_only":
        raise Conflict("export_only_request", "this request is an agency export; it never erases the agency")
    if awaiting_owner(row):
        raise Conflict("data_request_awaiting", "confirm the request AA raised first")
    if row["status"] not in ("requested", "export_ready"):
        raise Conflict("not_confirmable", f"the request is {row['status']}")
    if _get(row, "erase_not_before") is not None:
        raise Conflict("erase_scheduled", "the deletion is already confirmed and waits for its cool-off")
    if row["status"] == "requested" and not row["export_waived"]:
        raise Conflict("export_not_ready", "wait for the export, or waive it")
    if await ctx.conn.fetchval(HOLD_SQL):
        ctx.audit("offboarding_erase", "denied", "tenant", str(ctx.tenant_id), reason="legal_hold")
        raise Conflict("legal_hold", "an active legal hold in the agency suspends offboarding")
    if dry_run:
        counts = await erase.erase(ctx, "tenant", ctx.tenant_id, dry_run=True)
        raise Conflict(
            "deletion_dry_run", "deletion is switched off on this environment (LIFECYCLE_DRY_RUN)", counts=counts
        )
    if cool_off_hours > 0:
        await ctx.conn.execute(SCHEDULE_SQL, request_id, f"member:{ctx.member_id}", cool_off_hours)
        await jobs.enqueue(
            ctx.conn,
            OFFBOARD,
            {"tenant_id": str(ctx.tenant_id), "request_id": str(request_id), "mode": "erase"},
            idempotency_key=f"{OFFBOARD}:erase:{request_id}",
            delay_seconds=cool_off_hours * 3600,
            max_attempts=MAX_ATTEMPTS,
            created_by="offboarding",
        )
        ctx.audit("offboarding_erase", "allowed", "erasure_request", str(request_id), reason="scheduled")
        return view(await ctx.conn.fetchrow(REQUEST_SQL, request_id)) or {}
    await ctx.conn.execute(CONFIRM_SQL, request_id, f"member:{ctx.member_id}")
    await ctx.conn.execute(TENANT_OFFBOARDING_SQL, ctx.tenant_id)
    await jobs.enqueue(
        ctx.conn,
        OFFBOARD,
        {"tenant_id": str(ctx.tenant_id), "request_id": str(request_id), "mode": "erase"},
        idempotency_key=f"{OFFBOARD}:erase:{request_id}",
        max_attempts=MAX_ATTEMPTS,
        created_by="offboarding",
    )
    ctx.audit("offboarding_erase", "allowed", "erasure_request", str(request_id), reason="confirmed")
    return view(await ctx.conn.fetchrow(REQUEST_SQL, request_id)) or {}


# ── AA-raised requests (AA-941, crm-011) ─────────────────────────────────────────────────────────
PURPOSES = ("offboard", "export_only")
RAISE_SQL = """
INSERT INTO crm.erasure_request (scope, subject_id, purpose) VALUES ('tenant', $1, $2)
RETURNING request_id
"""
ACCEPT_SQL = """
UPDATE crm.erasure_request SET owner_accepted_at = now(), export_waived = $2, dry_run_counts = $3::jsonb
 WHERE request_id = $1 AND owner_accepted_at IS NULL
"""
AGENCY_REQUESTS_SQL = """
SELECT request_id, tenant_id, purpose, status, requested_by, owner_accepted_at, export_downloaded_at, created_at
  FROM crm.erasure_request WHERE scope = 'tenant' ORDER BY created_at DESC, request_id LIMIT $1
"""
AGENCY_REQUEST_SQL = """
SELECT request_id, tenant_id, purpose, status, requested_by, owner_accepted_at, export_downloaded_at, created_at
  FROM crm.erasure_request WHERE request_id = $1
"""
DATA_REQUEST_KINDS = {"offboard": "agency_deletion", "export_only": "agency_export"}


def data_request(row: Any) -> dict[str, Any]:
    """An agency-scope request as the L1 console sees it: ids, kind, a coarse status, when it was raised. No counts,
    no receipt, no export details."""
    purpose = row["purpose"]
    status = row["status"]
    aa = raised_by_aa(row)
    accepted = row["owner_accepted_at"] is not None
    downloaded = purpose == "export_only" and row["export_downloaded_at"] is not None
    if status == "erased" or (downloaded and status == "cancelled"):
        state = "done"  # erased, or an export downloaded and then closed by the owner
    elif downloaded:
        # Downloaded but still open (crm-010: only the owner's "Close request" ends an export_only row): it still
        # holds the agency's one open-request slot, so a new AA request is 409 offboarding_open until it is closed.
        state = "export_downloaded"
    elif status == "failed":
        state = "failed"
    elif status == "cancelled":
        state = "rejected" if aa and not accepted else "cancelled"
    elif aa and not accepted:
        state = "awaiting_owner_confirmation"
    else:
        state = "confirmed"
    return {
        "request_id": str(row["request_id"]),
        "tenant_id": str(row["tenant_id"]),
        "kind": DATA_REQUEST_KINDS.get(purpose, "agency_deletion"),
        "status": state,
        "raised_at": row["created_at"].isoformat(),
    }


async def agency_requests(ctx: AnyContext, limit: int = 10) -> list[dict[str, Any]]:
    """The latest agency-scope requests (owner-made and AA-raised), newest first."""
    return [data_request(r) for r in await ctx.conn.fetch(AGENCY_REQUESTS_SQL, limit)]


async def raise_request(ctx: AnyContext, purpose: str) -> dict[str, Any]:
    """AA raises an agency export (``export_only``) or deletion (``offboard``) request that the owner must confirm
    (L1 console, AA-941). No job is queued and no counts are taken: the owner's confirmation does both. 409
    ``offboarding_open`` while any agency request is open; 409 ``legal_hold`` for a deletion under an active hold.
    The crm-011 trigger refuses anything else a platform actor might write (CRM18)."""
    if purpose not in PURPOSES:
        raise Invalid("invalid_purpose", "offboard or export_only")
    if await ctx.conn.fetchrow(OPEN_SQL) is not None:
        raise Conflict("offboarding_open", "the agency already has an open export or deletion request")
    if purpose == "offboard" and await ctx.conn.fetchval(HOLD_SQL):
        raise Conflict("legal_hold", "an active legal hold in the agency suspends its deletion")
    try:
        request_id = await ctx.conn.fetchval(RAISE_SQL, ctx.tenant_id, purpose)
    except asyncpg.UniqueViolationError:
        raise Conflict("offboarding_open", "the agency already has an open export or deletion request") from None
    return data_request(await ctx.conn.fetchrow(AGENCY_REQUEST_SQL, request_id))


async def accept(ctx: TenantContext, request_id: uuid.UUID, *, export_waived: bool) -> dict[str, Any]:
    """The owner confirms a request AA raised (AA-941): for a deletion the dry-run counts are taken now (refused under a
    legal hold); then, unless waived (never for an export-only request), the ``crm_offboard`` export job is queued.
    From here on the AA-942 owner flow applies (download, typed erase for a deletion, cancel)."""
    _owner(ctx, "offboarding_accept")
    row = await _open_request(ctx, request_id)
    if not awaiting_owner(row):
        raise Conflict("not_awaiting", "this request is not waiting for your confirmation")
    purpose = _get(row, "purpose", "offboard")
    waived = bool(export_waived) and purpose == "offboard"
    counts: dict[str, Any] = {}
    if purpose == "offboard":
        if await ctx.conn.fetchval(HOLD_SQL):
            ctx.audit("offboarding_accept", "denied", "tenant", str(ctx.tenant_id), reason="legal_hold")
            raise Conflict("legal_hold", "an active legal hold in the agency suspends offboarding")
        counts = await erase.erase(ctx, "tenant", ctx.tenant_id, dry_run=True)
    await ctx.conn.execute(ACCEPT_SQL, request_id, waived, json.dumps(counts))
    if not waived:
        await jobs.enqueue(
            ctx.conn,
            OFFBOARD,
            {"tenant_id": str(ctx.tenant_id), "request_id": str(request_id), "mode": "export"},
            idempotency_key=f"{OFFBOARD}:export:{request_id}",
            max_attempts=MAX_ATTEMPTS,
            created_by="offboarding",
        )
    ctx.audit(
        "offboarding_accept",
        "allowed",
        "erasure_request",
        str(request_id),
        reason=f"{purpose}_export_waived" if waived else f"{purpose}_export_queued",
    )
    return view(await ctx.conn.fetchrow(REQUEST_SQL, request_id)) or {}


# ── The job ───────────────────────────────────────────────────────────────────────────────────────
async def build_export(ctx: LifecycleContext, vault: Vault) -> bytes:
    """Every exported table of the agency as JSON lines: a header line, then one line per row (AA-967: every table
    of the catalog, :mod:`app.portability.tables`; documents are listed as rows, their files come with the
    portability ZIP of Settings > Data and privacy)."""
    lines = [
        json.dumps(
            {
                "format": EXPORT_FORMAT,
                "tenant_id": str(ctx.tenant_id),
                "created_at": dt.datetime.now(dt.UTC).isoformat(),
            }
        )
    ]
    for table in portability.T.exported(await portability.catalog(ctx)):
        for row in await portability.table_rows(ctx, vault, table):
            lines.append(json.dumps({"table": table.name, "row": row}, ensure_ascii=False, sort_keys=True))
    return ("\n".join(lines) + "\n").encode("utf-8")


async def _export(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, request_id: uuid.UUID, keys: KeyProvider, store: DocumentStore | None
) -> dict[str, Any]:
    async with lifecycle_tx(pool, tenant_id, actor=ACTOR) as ctx:
        row = await ctx.conn.fetchrow(REQUEST_SQL, request_id)
        if row is None or row["status"] != "requested" or row["export_waived"]:
            return {"request_id": str(request_id), "status": "skipped"}
        if store is None:
            await ctx.conn.execute(FAILURE_SQL, request_id, "export_not_configured")
            return {"request_id": str(request_id), "status": "export_not_configured"}
        vault = Vault(keys, ctx)
        content = await build_export(ctx, vault)
        sealed = await vault.encrypt("erasure_request", "export", request_id, content, purpose="export")
        key = export_key(tenant_id, request_id)
        await store.put(key, sealed)
        await ctx.conn.execute(EXPORT_DONE_SQL, request_id, key, hashlib.sha256(sealed).hexdigest(), EXPORT_FILE_DAYS)
        ctx.audit("export_create", "allowed", "erasure_request", str(request_id), reason="offboarding")
    log.info("offboarding_export tenant_id=%s request_id=%s bytes=%d", tenant_id, request_id, len(sealed))
    return {"request_id": str(request_id), "status": "export_ready"}


async def _erase(
    pool: asyncpg.Pool,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    keys: KeyProvider,
    store: DocumentStore | None,
    dek_cache_ttl_s: int,
) -> dict[str, Any]:
    statuses = frozenset({"offboarding"})
    try:
        async with lifecycle_tx(pool, tenant_id, actor=ACTOR, statuses=statuses) as ctx:
            row = await ctx.conn.fetchrow(REQUEST_SQL, request_id)
            if row is None or row["status"] not in ("erasing", "failed"):
                return {"request_id": str(request_id), "status": "skipped"}
            if row["status"] == "failed":
                await ctx.conn.execute(RETRY_SQL, request_id)
            # AA-967: the agency's portability exports are cancelled with the rows (their objects hold the data; the
            # rows stay). Every bucket object (exports, documents, the offboarding export) is only collected here and
            # deleted last, after the erase, the verify and the receipt: a refusal on the way deletes no file.
            objects: list[str] = []
            withdrawn = await portability_exports.withdraw(ctx, store, None, dry_run=False, pending=objects)
            counts = await erase.erase(ctx, "tenant", tenant_id, dry_run=False, store=store, pending=objects)
            if row["export_object_key"] and store is not None:
                objects.append(row["export_object_key"])
            # AA-967 verify: a canary sealed under the vault DEK before the crypto-shred must not open after it.
            vault = Vault(keys, ctx)  # type: ignore[arg-type]
            canary = await vault.encrypt("erasure_request", "verify_canary", request_id, CANARY)
            destroyed = await keys.destroy(ctx)  # crypto-shred: after the rows, before the commit
            unreadable = await _canary_unreadable(vault, request_id, canary)
            if not unreadable:
                raise Unavailable("verify_failed", "a value sealed before the key destroy still opens")
            now = dt.datetime.now(dt.UTC)
            backup_purge_by = (now + dt.timedelta(days=BACKUP_PURGE_DAYS)).date()
            receipt = {
                "format": 1,
                "counts": counts,
                "keys_destroyed": destroyed,
                "dek_cache_expired_by": (now + dt.timedelta(seconds=dek_cache_ttl_s)).isoformat(),
                "backup_purge_by": backup_purge_by.isoformat(),
                "kept": ["audit_event", "erasure_request", "lifecycle_run", "legal_hold", "export_job", "data_request"],
                "copies_outside_aa": list(COPIES_OUTSIDE),
                # AA-967: what the erase job checked before it reported done.
                "exports_withdrawn": withdrawn,
                "verify": {
                    "rows_left": 0,  # crm.erase_subject refuses (CRM16) while a table outside the keep list has a row
                    "keys_destroyed": destroyed,
                    "canary_unreadable": unreadable,
                    "other_tasks_caches_expire_by": (now + dt.timedelta(seconds=dek_cache_ttl_s)).isoformat(),
                },
            }
            await ctx.conn.execute(ERASED_SQL, request_id, backup_purge_by, json.dumps(receipt))
            await ctx.conn.execute(TENANT_OFFBOARDED_SQL, tenant_id)
            ctx.audit("offboarding_erase", "allowed", "tenant", str(tenant_id), reason="erased")
            # Last, still before the commit: the bucket objects. A bucket error here is not a refusal (not a
            # ServiceError): the rows roll back, the agency stays offboarding and the worker retries (a missing
            # object counts as deleted).
            try:
                await erase.delete_objects(store, objects)
            except ServiceError as exc:
                raise ObjectsNotDeleted(exc.code) from exc
    except ServiceError as exc:
        # A refusal (a hold placed after the confirmation, a table the erase does not know, a failed verify): it comes
        # before any object delete, so nothing was deleted (one transaction, the objects are deleted last); the
        # request shows why, the agency is given back (active: the owner reads the code and cancels) and the job is
        # not retried.
        await _refused(pool, tenant_id, request_id, exc.code)
    log.info("offboarding_erased tenant_id=%s request_id=%s keys=%d", tenant_id, request_id, destroyed)
    return {"request_id": str(request_id), "status": "erased", "keys_destroyed_count": destroyed}


class ObjectsNotDeleted(RuntimeError):
    """The bucket refused an object delete at the end of an agency erase: retried by the worker, not a refusal."""


async def _canary_unreadable(vault: Vault, request_id: uuid.UUID, canary: bytes) -> bool:
    try:
        await vault.decrypt_bytes("erasure_request", "verify_canary", request_id, canary)
    except (KeyDestroyed, DecryptionDenied, UnwrapDenied):
        return True
    return False


async def _start(pool: asyncpg.Pool, tenant_id: uuid.UUID, request_id: uuid.UUID) -> bool:
    """AA-967: the end of the cool-off. A request confirmed with ``erase_not_before`` is still ``requested`` /
    ``export_ready`` and its agency active: move both on (erasing, offboarding). False = nothing to erase (the owner
    cancelled during the cool-off, or the request was never confirmed). True = erase (just started here, or confirmed
    without a cool-off: already erasing / failed and the agency offboarding)."""
    try:
        async with lifecycle_tx(pool, tenant_id, actor=ACTOR) as ctx:
            row = await ctx.conn.fetchrow(REQUEST_SQL, request_id)
            if row is None:
                return False
            if row["status"] in ("erasing", "failed"):
                return True
            if row["status"] not in ("requested", "export_ready") or _get(row, "confirmed_by") is None:
                return False
            not_before = _get(row, "erase_not_before")
            if not_before is not None and not_before > dt.datetime.now(dt.UTC):
                raise Unavailable("cool_off_running")  # queued early (clock skew): retried later
            await ctx.conn.execute(START_SQL, request_id)
            await ctx.conn.execute(TENANT_OFFBOARDING_SQL, tenant_id)
            ctx.audit("offboarding_erase", "allowed", "erasure_request", str(request_id), reason="cool_off_ended")
    except Forbidden:
        return True  # the agency is already offboarding (a confirmation without a cool-off): the erase decides
    except NotFound:
        return False
    return True


async def _refused(pool: asyncpg.Pool, tenant_id: uuid.UUID, request_id: uuid.UUID, code: str) -> NoReturn:
    """An erase that deleted nothing: the request ``failed`` with ``code``, the agency ``active`` again, a ``denied``
    audit event, and the job failed at once (:class:`PermanentError`, no retry)."""
    statuses = frozenset({"offboarding"})
    try:
        async with lifecycle_tx(pool, tenant_id, actor=ACTOR, statuses=statuses) as ctx:
            row = await ctx.conn.fetchrow(REQUEST_SQL, request_id)
            if row is not None and row["status"] in ("erasing", "failed"):
                await ctx.conn.execute(FAILED_SQL, request_id, code)
            await ctx.conn.execute(TENANT_BACK_SQL, tenant_id)
            ctx.audit("offboarding_erase", "denied", "tenant", str(tenant_id), reason=code)
    except Forbidden:
        pass  # the agency is not offboarding (a stale job): nothing to give back
    log.error("offboarding_erase_refused tenant_id=%s code=%s", tenant_id, code)
    raise PermanentError(code)


def build_handler(
    *, keys: KeyProvider | None, store: DocumentStore | None, dry_run: bool, dek_cache_ttl_s: int = 300
) -> Handler:
    async def offboard(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        try:
            tenant_id = uuid.UUID(str(job.payload["tenant_id"]))
            request_id = uuid.UUID(str(job.payload["request_id"]))
            mode = str(job.payload["mode"])
        except (KeyError, ValueError):
            raise PermanentError("bad_payload") from None
        if keys is None:
            raise Unavailable("key_provider_not_configured")  # retried: nothing is erased without the crypto-shred
        if mode == "export":
            return await _export(pool, tenant_id, request_id, keys, store)
        if mode == "erase":
            # AA-967: a scheduled (cool-off) confirmation starts here; one cancelled meanwhile erases nothing.
            if not await _start(pool, tenant_id, request_id):
                return {"request_id": str(request_id), "status": "skipped"}
            if dry_run:
                await _refused(pool, tenant_id, request_id, "deletion_dry_run")
            return await _erase(pool, tenant_id, request_id, keys, store, dek_cache_ttl_s)
        raise PermanentError("bad_mode")

    return offboard
