"""One client's erasure on behalf of the agency (AA-967, design §4.10: "the owner confirms -> DRY RUN counts -> run ->
verify"; Fix B16; DM-7 exceptions).

``POST /crm/v1/travelers/{id}/erasure`` (owner, :mod:`app.routers.lifecycle`) calls :func:`erase_client`:

- **Dry run** (default): the counts ``crm.erase_subject`` would delete, what it would keep (the signed evidence inside
  the claim period, crm-025: ``terms_acceptance_kept`` / ``signature_event_kept``), the exceptions the receipt will
  name, and the exports it would withdraw. The holds (CRM10), the delete block, a client on a live trip (CRM12) refuse
  here already.
- **Run** (``dry_run`` false, deletion switched on): the exports that may hold the client's data are withdrawn
  (:func:`app.portability.exports.withdraw`), the erase runs (trash skipped), then the **verification**: the same erase
  as a dry run must find no such client any more (``subject_not_found``). The receipt (``crm.erasure_request``, status
  ``erased``, ``verified_at``) lists the counts, the kept rows and the exceptions (``signed_agreements_claim_period``
  when evidence was kept, ``accounting_records`` when invoices or payments of the client's trips stay with their trip),
  ``backup_purge_by`` (30 days) and the copies outside AA. A logged data request (``data_request_id``) is closed
  ``done`` / ``fulfilled`` with the receipt.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from app.documents.store import DocumentStore
from app.errors import Conflict, Forbidden, NotFound, Unavailable
from app.lifecycle import erase
from app.lifecycle.rules import BACKUP_PURGE_DAYS
from app.portability import exports, requests
from app.tenancy import TenantContext
from app.vault import Vault

COPIES_OUTSIDE = ("agency_mailboxes_and_messaging_accounts", "supplier_manifests_already_sent", "agency_downloads")
KEPT_SUFFIX = "_kept"
ACCOUNTING_SQL = """
SELECT (SELECT count(*) FROM crm.invoice i WHERE i.trip_id = ANY ($1::uuid[]))
     + (SELECT count(*) FROM crm.payment p WHERE p.trip_id = ANY ($1::uuid[]))
"""
TRIPS_SQL = "SELECT coalesce(array_agg(DISTINCT trip_id), '{}') FROM crm.trip_traveler WHERE traveler_id = $1"
RECEIPT_SQL = """
INSERT INTO crm.erasure_request (scope, subject_id, status, confirmed_by, dry_run_counts, executed_at, backup_purge_by,
                                 receipt, verified_at)
VALUES ('traveler', $1, 'erased', $2, $3::jsonb, now(), $4, $5::jsonb, now())
RETURNING request_id
"""


def split(counts: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """(deleted counts, kept counts) of an erase result (crm-025 reports kept evidence as ``<table>_kept``)."""
    kept = {k[: -len(KEPT_SUFFIX)]: v for k, v in counts.items() if k.endswith(KEPT_SUFFIX)}
    deleted = {k: v for k, v in counts.items() if not k.endswith(KEPT_SUFFIX)}
    return deleted, kept


async def _accounting(ctx: TenantContext, traveler_id: uuid.UUID) -> int:
    trips = list(await ctx.conn.fetchval(TRIPS_SQL, traveler_id) or [])
    if not trips:
        return 0
    return int(await ctx.conn.fetchval(ACCOUNTING_SQL, trips) or 0)


def exceptions(kept: dict[str, int], accounting: int) -> list[str]:
    out = []
    if any(v > 0 for v in kept.values()):
        out.append("signed_agreements_claim_period")
    if accounting > 0:
        out.append("accounting_records")
    return out


async def erase_client(
    ctx: TenantContext,
    vault: Vault,
    store: DocumentStore | None,
    traveler_id: uuid.UUID,
    *,
    dry_run: bool,
    deletion_dry_run: bool,
    data_request_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    if ctx.member_role != "owner":
        ctx.audit("erasure", "denied", "traveler", str(traveler_id), reason="owner_only")
        raise Forbidden("owner_only", "only the agency owner erases a client")
    if data_request_id is not None and not await ctx.conn.fetchval(requests.ROW_SQL, data_request_id):
        raise NotFound("data_request_not_found")
    counts = await erase.erase(ctx, "traveler", traveler_id, dry_run=True, vault=vault)
    accounting = await _accounting(ctx, traveler_id)
    deleted, kept = split(counts)
    preview = {
        "traveler_id": str(traveler_id),
        "counts": counts,
        "kept": kept,
        "exceptions": exceptions(kept, accounting),
        "exports_withdrawn": await exports.withdraw(ctx, store, traveler_id, dry_run=True),
    }
    if dry_run:
        return {**preview, "dry_run": True}
    if deletion_dry_run:
        raise Conflict(
            "deletion_dry_run", "deletion is switched off on this environment (LIFECYCLE_DRY_RUN)", counts=counts
        )
    withdrawn = await exports.withdraw(ctx, store, traveler_id, dry_run=False)
    counts = await erase.erase(ctx, "traveler", traveler_id, dry_run=False, vault=vault, store=store)
    deleted, kept = split(counts)
    # Verify: the same erase finds no such client any more (rows, F2 pointer and profile gone in this transaction).
    try:
        await erase.erase(ctx, "traveler", traveler_id, dry_run=True, vault=vault)
    except NotFound:
        gone = True
    else:
        gone = False
    if not gone:
        raise Unavailable("verify_failed", "the client is still found after the erase")
    purge_by = (dt.datetime.now(dt.UTC) + dt.timedelta(days=BACKUP_PURGE_DAYS)).date()
    receipt = {
        "format": 2,
        "counts": deleted,
        "kept": kept,
        "exceptions": exceptions(kept, accounting),
        "exports_withdrawn": withdrawn,
        "backup_purge_by": purge_by.isoformat(),
        "copies_outside_aa": list(COPIES_OUTSIDE),
        "verify": {"subject_gone": True},
    }
    request_id = await ctx.conn.fetchval(
        RECEIPT_SQL,
        traveler_id,
        f"member:{ctx.member_id}",
        json.dumps({"counts": counts}),
        purge_by,
        json.dumps(receipt),
    )
    ctx.audit("erasure", "allowed", "traveler", str(traveler_id), reason="bypass_trash")
    if data_request_id is not None:
        await requests.close_erased(ctx, data_request_id, request_id)
    return {
        **preview,
        "dry_run": False,
        "counts": counts,
        "kept": kept,
        "exports_withdrawn": withdrawn,
        "request_id": str(request_id),
        "backup_purge_by": purge_by.isoformat(),
        "receipt": receipt,
    }
