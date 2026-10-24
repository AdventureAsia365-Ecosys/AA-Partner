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
   export registry / chain, the receipts, lifecycle counts and holds), the documents' objects and the export object,
   then :meth:`KeyProvider.destroy` (**crypto-shred**: wrapped DEKs nulled, ``destroyed_at`` set, this process's cache
   evicted), all in one transaction. The receipt (counts, ``backup_purge_by`` = today + 30 days, the time by which
   every other task's DEK cache has expired = destroy + ``DEK_CACHE_TTL_SECONDS``, the copies outside AA's control)
   goes on the request; the agency becomes ``offboarded``.

If the database refuses the erase (a hold placed after the confirmation, CRM16 a table the erase does not know),
nothing was deleted (one transaction): the request becomes ``failed`` with the code and the agency goes back to
``active``, so the owner can read why, cancel the request and start again later. An erase job that meets DRY RUN
(``LIFECYCLE_DRY_RUN`` switched on after the confirmation) is the same refusal: ``failed`` with
``deletion_dry_run``, the agency ``active`` again, the job failed at once (review S249). A job that fails for another
reason (retries exhausted) leaves the agency ``offboarding``; the platform recovery is in
``docs/implementation-notes/AA-942.md``.

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

import base64
import datetime as dt
import decimal
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
from app.tenancy import AnyContext, LifecycleContext, TenantContext, lifecycle_tx
from app.vault import Vault

if TYPE_CHECKING:  # pragma: no cover
    from app.vault import KeyProvider

log = logging.getLogger(__name__)

ACTOR = "job:crm_offboard"
MAX_ATTEMPTS = 3
EXPORT_FORMAT = 1
# What stays outside AA's reach after the erase (lane §5.8 receipt): named as codes, worded by the web.
COPIES_OUTSIDE = ("agency_mailboxes_and_messaging_accounts", "supplier_manifests_already_sent", "agency_downloads")

# Exported tables and their primary key (the AAD row id of their *_enc columns). Integration credentials are never
# exported (secrets); the audit has its own download (Settings > Access log).
EXPORT_TABLES: dict[str, str] = {
    "tenant_settings": "tenant_id",
    "member": "member_id",
    "traveler": "traveler_id",
    "traveler_profile": "traveler_token",
    "profile_history": "history_id",
    "traveler_relation": "relation_id",
    "traveler_health": "health_id",
    "consent": "consent_id",
    "trip": "trip_id",
    "trip_traveler": "trip_traveler_id",
    "trip_party": "trip_party_id",
    "trip_stage_history": "history_id",
    "trip_commitment": "commitment_id",
    "task": "task_id",
    "note": "note_id",
    "document": "document_id",
    "form_definition": "form_definition_id",
    "intake_form": "intake_form_id",
    "form_submission": "submission_id",
    "dispatch_queue": "dispatch_id",
    "legal_hold": "hold_id",
    "llm_call": "llm_call_id",
}
NEVER_EXPORT_COLUMNS = frozenset({"profile_token_enc"})

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
TENANT_OFFBOARDING_SQL = "UPDATE crm.tenant SET status = 'offboarding' WHERE tenant_id = $1"
TENANT_OFFBOARDED_SQL = "UPDATE crm.tenant SET status = 'offboarded' WHERE tenant_id = $1"
TENANT_BACK_SQL = "UPDATE crm.tenant SET status = 'active' WHERE tenant_id = $1 AND status = 'offboarding'"
ERASED_SQL = """
UPDATE crm.erasure_request
   SET status = 'erased', executed_at = now(), keys_destroyed_at = now(), backup_purge_by = $2, receipt = $3::jsonb,
       failure_class = NULL
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
    ctx: TenantContext, request_id: uuid.UUID, *, confirm_tenant_id: uuid.UUID, dry_run: bool
) -> dict[str, Any]:
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
    if status == "erased" or (purpose == "export_only" and row["export_downloaded_at"] is not None):
        state = "done"
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
def _json_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, list):
        return [_json_value(v) for v in value]
    return value


async def build_export(ctx: LifecycleContext, vault: Vault) -> bytes:
    """Every exported table of the agency as JSON lines: a header line, then one line per row."""
    lines = [
        json.dumps(
            {
                "format": EXPORT_FORMAT,
                "tenant_id": str(ctx.tenant_id),
                "created_at": dt.datetime.now(dt.UTC).isoformat(),
            }
        )
    ]
    for table, key in EXPORT_TABLES.items():
        if await ctx.conn.fetchval("SELECT to_regclass($1)", f"crm.{table}") is None:
            continue
        rows = await ctx.conn.fetch(f"SELECT * FROM crm.{table} ORDER BY {key}")  # noqa: S608 — fixed names
        for r in rows:
            out: dict[str, Any] = {}
            for column, value in r.items():
                if column.endswith("_bidx") or column in NEVER_EXPORT_COLUMNS:
                    continue
                if column.endswith("_enc"):
                    if value is not None:
                        out[column[: -len("_enc")]] = await vault.decrypt(table, column, r[key], value)
                    continue
                out[column] = _json_value(value)
            lines.append(json.dumps({"table": table, "row": out}, ensure_ascii=False, sort_keys=True))
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
            counts = await erase.erase(ctx, "tenant", tenant_id, dry_run=False, store=store)
            if row["export_object_key"] and store is not None:
                await store.delete(row["export_object_key"])
            destroyed = await keys.destroy(ctx)  # crypto-shred: after the rows, before the commit
            now = dt.datetime.now(dt.UTC)
            backup_purge_by = (now + dt.timedelta(days=BACKUP_PURGE_DAYS)).date()
            receipt = {
                "format": 1,
                "counts": counts,
                "keys_destroyed": destroyed,
                "dek_cache_expired_by": (now + dt.timedelta(seconds=dek_cache_ttl_s)).isoformat(),
                "backup_purge_by": backup_purge_by.isoformat(),
                "kept": ["audit_event", "erasure_request", "lifecycle_run", "legal_hold"],
                "copies_outside_aa": list(COPIES_OUTSIDE),
            }
            await ctx.conn.execute(ERASED_SQL, request_id, backup_purge_by, json.dumps(receipt))
            await ctx.conn.execute(TENANT_OFFBOARDED_SQL, tenant_id)
            ctx.audit("offboarding_erase", "allowed", "tenant", str(tenant_id), reason="erased")
    except ServiceError as exc:
        # A refusal of the database (a hold placed after the confirmation, a table the erase does not know): nothing
        # was deleted (one transaction); the request shows why, the agency is given back (active: the owner reads the
        # code and cancels) and the job is not retried.
        await _refused(pool, tenant_id, request_id, exc.code)
    log.info("offboarding_erased tenant_id=%s request_id=%s keys=%d", tenant_id, request_id, destroyed)
    return {"request_id": str(request_id), "status": "erased", "keys_destroyed_count": destroyed}


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
            if dry_run:
                await _refused(pool, tenant_id, request_id, "deletion_dry_run")
            return await _erase(pool, tenant_id, request_id, keys, store, dek_cache_ttl_s)
        raise PermanentError("bad_mode")

    return offboard
