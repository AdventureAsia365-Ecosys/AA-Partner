"""``/crm/v1`` data lifecycle: rules, trash, delete, archive, legal holds, erasure, offboarding (AA-942, card CRM-14).

==============================================  =========================================================  ============
Route                                           What                                                       Who
==============================================  =========================================================  ============
``GET  /lifecycle``                             rules per data class, the agency's settings, Lost codes;   every role
                                                owner: holds, last runs, archived-inactive clients,
                                                the offboarding request
``PATCH /lifecycle/settings``                   trash_days, auto_archive_months                            owner
``POST /lifecycle/holds``                       place a legal hold (agency, trip, client)                  owner
``POST /lifecycle/holds/{id}/release``          release it (not one AA placed)                             owner
``GET  /trash``                                 the trash (owner: all; others: what they deleted)          every role
``POST /trash/{kind}/{id}/restore``             restore (a trip re-checks Booking first)                   owner, who
``DELETE /trips/{id}``                          to the trash; 409 has_money / has_signature / ...          owner, agent
``DELETE /travelers/{id}``                      to the trash; 409 traveler_on_trip                         owner
``DELETE /tasks/{id}``, ``DELETE /notes/{id}``  to the trash                                               see trash
``POST /travelers/{id}/archive`` / ``unarchive``  archive with a reason / bring back                       owner, agent
``POST /travelers/{id}/erasure``                erase now, bypassing the trash (dry run by default)        owner
``GET  /lifecycle/offboarding``                 the latest offboarding request                             owner
``POST /lifecycle/offboarding``                 request (dry-run counts, export queued unless waived)      owner
``POST /lifecycle/offboarding/{id}/cancel``     cancel before the erase                                    owner
``GET  /lifecycle/offboarding/{id}/export``     download the export (JSON lines)                           owner
``POST /lifecycle/offboarding/{id}/erase``      confirm the erase (typed agency id)                        owner
``POST /lifecycle/offboarding/{id}/accept``     confirm a request AA raised (AA-941; decline = cancel)     owner
==============================================  =========================================================  ============

``DELETE /documents/{id}`` stays in :mod:`app.routers.travellers` and uses the same trash. Every refusal of a role is
a committed ``denied`` audit event. Each route names its permission-matrix action (AA-939, ``app.permissions``):
``session_view`` (rules), ``data_settings`` (settings, holds; owner), ``trip_view`` (the trash list), the kind's delete
action for delete / restore (``trip_delete``, ``client_delete``, ``task_update``, ``note_add``, ``document_delete``),
``client_edit`` (archive), ``client_delete`` (erasure), ``agency_export`` (offboarding); the services keep their
object checks (own trip, own note, owner-only paths).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.errors import Conflict, Forbidden
from app.lifecycle import erase, offboarding, policy, rules, trash
from app.lifecycle.rules import BACKUP_PURGE_DAYS
from app.permissions import Action
from app.pipeline import LOST_REASONS
from app.routers.agency import tenant_header
from app.tenancy import TenantContext, tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["lifecycle"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]

# The permission-matrix action of each trash kind (route level; the trash module still checks own trip / own note).
TRASH_ACTIONS: dict[str, Action] = {
    "trip": Action.TRIP_DELETE,
    "traveler": Action.CLIENT_DELETE,
    "task": Action.TASK_UPDATE,
    "note": Action.NOTE_ADD,
    "document": Action.DOCUMENT_DELETE,
    "booking_line": Action.TRIP_EDIT,  # AA-944: the DELETE routes are in app.routers.ledger
    "agency_supplier": Action.TRIP_EDIT,
}

RUNS_SQL = """
SELECT kind, dry_run, counts, created_at FROM crm.lifecycle_run ORDER BY created_at DESC, run_id LIMIT 10
"""
ARCHIVED_INACTIVE_SQL = """
SELECT traveler_id, display_name_enc, archived_at FROM crm.traveler
 WHERE deleted_at IS NULL AND archive_reason = 'auto_inactive' ORDER BY archived_at DESC, traveler_id LIMIT 50
"""
ERASED_TRAVELER_SQL = """
INSERT INTO crm.erasure_request (scope, subject_id, status, confirmed_by, dry_run_counts, executed_at, backup_purge_by,
                                 receipt)
VALUES ('traveler', $1, 'erased', $2, $3::jsonb, now(), $4, $3::jsonb)
RETURNING request_id
"""


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingsChange(_Body):
    trash_days: int | None = Field(default=None, ge=rules.MIN_TRASH_DAYS, le=rules.MAX_TRASH_DAYS)
    auto_archive_months: int | None = Field(
        default=None, ge=rules.MIN_AUTO_ARCHIVE_MONTHS, le=rules.MAX_AUTO_ARCHIVE_MONTHS
    )


class HoldIn(_Body):
    scope: Literal["tenant", "trip", "traveler"]
    subject_id: uuid.UUID | None = None
    reason_code: Literal["litigation", "regulator_request", "dispute", "insurance_claim", "other"]


class ArchiveIn(_Body):
    reason: Literal["agent", "client_request", "duplicate", "other"] = "agent"


class ErasureIn(_Body):
    dry_run: bool = True


class OffboardingIn(_Body):
    export_waived: bool = False


class EraseConfirm(_Body):
    confirm_tenant_id: uuid.UUID


class AcceptIn(_Body):
    export_waived: bool = False


def _dry_run(request: Request) -> bool:
    return bool(request.app.state.settings.lifecycle_dry_run)


async def _owner_extras(ctx: TenantContext, vault: Vault | None) -> dict[str, Any]:
    runs = [
        {
            "kind": r["kind"],
            "dry_run": r["dry_run"],
            "counts": json.loads(r["counts"]) if isinstance(r["counts"], str) else dict(r["counts"]),
            "at": r["created_at"].isoformat(),
        }
        for r in await ctx.conn.fetch(RUNS_SQL)
    ]
    archived: list[dict[str, Any]] = []
    if vault is not None:
        for r in await ctx.conn.fetch(ARCHIVED_INACTIVE_SQL):
            archived.append(
                {
                    "traveler_id": str(r["traveler_id"]),
                    "display_name": await vault.decrypt(
                        "traveler", "display_name_enc", r["traveler_id"], r["display_name_enc"]
                    ),
                    "archived_at": r["archived_at"].isoformat(),
                }
            )
        if archived:
            ctx.audit(
                "list_view",
                "allowed",
                "traveler",
                None,
                fields=("display_name",),
                reason="archived_inactive",
                object_ids=tuple(a["traveler_id"] for a in archived),
            )
    return {
        "holds": await policy.holds(ctx),
        "runs": runs,
        "archived_inactive": archived,
        "offboarding": await offboarding.latest(ctx),
    }


@router.get("/lifecycle")
async def get_lifecycle(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = request.app.state.keys
    async with tenant_tx(pool, caller, tenant_id, action=Action.SESSION_VIEW) as ctx:
        out: dict[str, Any] = {
            "rules": rules.rules_view(),
            "settings": await policy.settings(ctx),
            "lost_reasons": list(LOST_REASONS),
            "archive_reasons": [r for r in rules.ARCHIVE_REASONS if r != "auto_inactive"],
            "hold_reasons": list(rules.HOLD_REASONS),
            "deletion_dry_run": _dry_run(request),
            "backup_purge_days": BACKUP_PURGE_DAYS,
            "role": ctx.member_role,
        }
        if ctx.member_role == "owner":
            out |= await _owner_extras(ctx, Vault(keys, ctx) if keys is not None else None)
        return out


@router.patch("/lifecycle/settings")
async def patch_settings(request: Request, caller: CallerDep, tenant_id: TenantDep, body: SettingsChange) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DATA_SETTINGS) as ctx:
        return await policy.update_settings(
            ctx, trash_days=body.trash_days, auto_archive_months=body.auto_archive_months
        )


@router.post("/lifecycle/holds", status_code=201)
async def post_hold(request: Request, caller: CallerDep, tenant_id: TenantDep, body: HoldIn) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DATA_SETTINGS) as ctx:
        return await policy.place_hold(ctx, body.scope, body.subject_id, body.reason_code)


@router.post("/lifecycle/holds/{hold_id}/release")
async def post_release(request: Request, caller: CallerDep, tenant_id: TenantDep, hold_id: uuid.UUID) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DATA_SETTINGS) as ctx:
        return await policy.release_hold(ctx, hold_id)


@router.get("/trash")
async def get_trash(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await trash.list_trash(ctx, Vault(keys, ctx))


@router.post("/trash/{kind}/{object_id}/restore")
async def post_restore(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    kind: Literal["trip", "traveler", "task", "note", "document", "booking_line", "agency_supplier"],
    object_id: uuid.UUID,
) -> dict[str, Any]:
    pool = await request_pool(request)
    checker = getattr(request.app.state, "booking_checker", None)
    async with tenant_tx(pool, caller, tenant_id, action=TRASH_ACTIONS[kind]) as ctx:
        return await trash.restore(ctx, kind, object_id, checker)


async def _delete(request: Request, caller: Caller, tenant_id: uuid.UUID, kind: str, object_id: uuid.UUID) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=TRASH_ACTIONS[kind]) as ctx:
        return await trash.move_to_trash(ctx, kind, object_id)


@router.delete("/trips/{trip_id}")
async def delete_trip(request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID) -> dict:
    return await _delete(request, caller, tenant_id, "trip", trip_id)


@router.delete("/travelers/{traveler_id}")
async def delete_traveler(request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID) -> dict:
    return await _delete(request, caller, tenant_id, "traveler", traveler_id)


@router.delete("/tasks/{task_id}")
async def delete_task(request: Request, caller: CallerDep, tenant_id: TenantDep, task_id: uuid.UUID) -> dict:
    return await _delete(request, caller, tenant_id, "task", task_id)


@router.delete("/notes/{note_id}")
async def delete_note(request: Request, caller: CallerDep, tenant_id: TenantDep, note_id: uuid.UUID) -> dict:
    return await _delete(request, caller, tenant_id, "note", note_id)


@router.post("/travelers/{traveler_id}/archive")
async def post_archive(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID, body: ArchiveIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_EDIT) as ctx:
        return await policy.archive(ctx, traveler_id, body.reason)


@router.post("/travelers/{traveler_id}/unarchive")
async def post_unarchive(request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_EDIT) as ctx:
        return await policy.unarchive(ctx, traveler_id)


@router.post("/travelers/{traveler_id}/erasure")
async def post_erasure(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID, body: ErasureIn
) -> dict[str, Any]:
    """Erase one client now (design §4.10, "erasure bypasses trash"): a dry run returns the counts; a real run needs
    deletion switched on (``LIFECYCLE_DRY_RUN`` off) and leaves an ``erasure_request`` receipt."""
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    store = request.app.state.documents
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_DELETE) as ctx:
        if ctx.member_role != "owner":
            ctx.audit("erasure", "denied", "traveler", str(traveler_id), reason="owner_only")
            raise Forbidden("owner_only", "only the agency owner erases a client")
        vault = Vault(keys, ctx)
        counts = await erase.erase(ctx, "traveler", traveler_id, dry_run=True, vault=vault)
        if body.dry_run:
            return {"traveler_id": str(traveler_id), "dry_run": True, "counts": counts}
        if _dry_run(request):
            raise Conflict(
                "deletion_dry_run", "deletion is switched off on this environment (LIFECYCLE_DRY_RUN)", counts=counts
            )
        counts = await erase.erase(ctx, "traveler", traveler_id, dry_run=False, vault=vault, store=store)
        purge_by = (dt.datetime.now(dt.UTC) + dt.timedelta(days=BACKUP_PURGE_DAYS)).date()
        request_id = await ctx.conn.fetchval(
            ERASED_TRAVELER_SQL, traveler_id, f"member:{ctx.member_id}", json.dumps({"counts": counts}), purge_by
        )
        ctx.audit("erasure", "allowed", "traveler", str(traveler_id), reason="bypass_trash")
        return {
            "traveler_id": str(traveler_id),
            "dry_run": False,
            "counts": counts,
            "request_id": str(request_id),
            "backup_purge_by": purge_by.isoformat(),
        }


@router.get("/lifecycle/offboarding")
async def get_offboarding(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
        if ctx.member_role != "owner":
            ctx.audit("view", "denied", "erasure_request", reason="owner_only")
            raise Forbidden("owner_only", "only the agency owner sees the offboarding")
        return {"offboarding": await offboarding.latest(ctx)}


@router.post("/lifecycle/offboarding", status_code=201)
async def post_offboarding(request: Request, caller: CallerDep, tenant_id: TenantDep, body: OffboardingIn) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
        return await offboarding.request(ctx, export_waived=body.export_waived)


@router.post("/lifecycle/offboarding/{request_id}/cancel")
async def post_offboarding_cancel(
    request: Request, caller: CallerDep, tenant_id: TenantDep, request_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
        return await offboarding.cancel(ctx, request_id, request.app.state.documents)


@router.get("/lifecycle/offboarding/{request_id}/export")
async def get_offboarding_export(
    request: Request, caller: CallerDep, tenant_id: TenantDep, request_id: uuid.UUID
) -> Response:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
        content = await offboarding.download(ctx, Vault(keys, ctx), request.app.state.documents, request_id)
    return Response(
        content=content,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="agency-export-{request_id}.jsonl"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/lifecycle/offboarding/{request_id}/erase")
async def post_offboarding_erase(
    request: Request, caller: CallerDep, tenant_id: TenantDep, request_id: uuid.UUID, body: EraseConfirm
) -> dict[str, Any]:
    pool = await request_pool(request)
    require_keys(request.app.state.keys)  # the erase ends with the crypto-shred: no provider, no start
    async with tenant_tx(pool, caller, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
        return await offboarding.confirm_erase(
            ctx, request_id, confirm_tenant_id=body.confirm_tenant_id, dry_run=_dry_run(request)
        )


@router.post("/lifecycle/offboarding/{request_id}/accept")
async def post_offboarding_accept(
    request: Request, caller: CallerDep, tenant_id: TenantDep, request_id: uuid.UUID, body: AcceptIn
) -> dict[str, Any]:
    """AA-941: the owner confirms an agency export or deletion request AA raised (decline = ``/cancel``)."""
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AGENCY_EXPORT) as ctx:
        return await offboarding.accept(ctx, request_id, export_waived=body.export_waived)
