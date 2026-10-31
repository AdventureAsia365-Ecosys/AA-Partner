"""``/crm/v1`` onboarding, the client import and duplicate clients (route class AGENCY; AA-962, card CRM-31, A-20).

=================================================  ==================================================  ===========
Route                                              What                                                Who
=================================================  ==================================================  ===========
``GET    /onboarding``                             checklist, Connections, controller settings, ack    every role
``PUT    /onboarding/steps/{key}``                 done (manual steps) / later (optional) / open       owner
``PUT    /onboarding/controller``                  privacy-notice link, data-request contact, consent  owner
``POST   /onboarding/acknowledgement``             the shared-responsibility text version              owner
``POST   /client-imports/preview?entity=``         dry run of a CSV / .xlsx (raw body, <= 5 MB)         owner
``POST   /client-imports?entity=&sha256=``         start the import of the previewed file (a job)      owner
``GET    /client-imports``                         this agency's imports of the last 30 days           owner
``GET    /client-imports/{id}``                    one import's progress and counts                    owner
``GET    /client-duplicates``                      possible duplicate clients (one list_view)          every role
``POST   /travelers/{id}/merge``                   merge another client into this one                 owner
=================================================  ==================================================  ===========

The import routes take the file as the raw request body (like the mail history import), capped while it streams in
(413 ``import_too_large``). A missing column is 422 ``missing_columns`` before any write (card R). Responses carry row
numbers, codes and counts, never a value from the file.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.errors import PayloadTooLarge
from app.imports import formats
from app.imports import service as imports
from app.mail.store import require_ingest_store
from app.onboarding import STATES, STEP_KEYS, acknowledge, overview, set_controller, set_step
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.travellers import merge
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["onboarding"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Entity = Literal["clients", "relationships", "trips", "notes"]
Region = Annotated[str | None, Query(max_length=2, pattern="^[A-Za-z]{2}$")]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StepIn(_Body):
    state: Literal[STATES]  # type: ignore[valid-type]


class ControllerIn(_Body):
    privacy_notice_url: str | None = Field(default=None, max_length=500)
    data_request_contact: str | None = Field(default=None, max_length=320)
    consent_text: str | None = Field(default=None, max_length=2000)


class AckIn(_Body):
    version: str = Field(min_length=1, max_length=40)


class MergeIn(_Body):
    other_traveler_id: uuid.UUID


async def _read_capped(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLarge("import_too_large", f"at most {limit // (1024 * 1024)} MB")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLarge("import_too_large", f"at most {limit // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)


# ── onboarding ─────────────────────────────────────────────────────────────────────────────────────
@router.get("/onboarding")
async def get_onboarding(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SESSION_VIEW) as ctx:
        return await overview(ctx, duplicate_count=await merge.candidate_count(ctx))


@router.put("/onboarding/steps/{key}")
async def put_step(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    key: Literal[STEP_KEYS],
    body: StepIn,  # type: ignore[valid-type]
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.ONBOARDING_MANAGE) as ctx:
        return await set_step(ctx, key, body.state)


@router.put("/onboarding/controller")
async def put_controller(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: ControllerIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DATA_SETTINGS) as ctx:
        return await set_controller(ctx, body.privacy_notice_url, body.data_request_contact, body.consent_text)


@router.post("/onboarding/acknowledgement")
async def post_acknowledgement(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: AckIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DATA_SETTINGS) as ctx:
        return await acknowledge(ctx, body.version)


# ── client import ──────────────────────────────────────────────────────────────────────────────────
@router.post("/client-imports/preview")
async def post_import_preview(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    entity: Annotated[Entity, Query()],
    phone_region: Region = None,
) -> dict[str, Any]:
    data = await _read_capped(request, formats.MAX_BYTES)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_IMPORT) as ctx:
        return await imports.preview(ctx, Vault(keys, ctx), entity=entity, data=data, region=phone_region)


@router.post("/client-imports", status_code=202)
async def post_import(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    entity: Annotated[Entity, Query()],
    sha256: Annotated[str, Query(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")],
    phone_region: Region = None,
) -> dict[str, Any]:
    store = require_ingest_store(getattr(request.app.state, "ingest_store", None))
    data = await _read_capped(request, formats.MAX_BYTES)
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_IMPORT) as ctx:
        return await imports.start(ctx, store, entity=entity, data=data, sha256=sha256, region=phone_region)


@router.get("/client-imports")
async def get_imports(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_IMPORT) as ctx:
        return await imports.recent(ctx)


@router.get("/client-imports/{import_id}")
async def get_import(request: Request, caller: CallerDep, tenant_id: TenantDep, import_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_IMPORT) as ctx:
        return await imports.status(ctx, import_id)


# ── duplicates and merge ───────────────────────────────────────────────────────────────────────────
@router.get("/client-duplicates")
async def get_duplicates(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_VIEW) as ctx:
        return await merge.candidates(ctx, Vault(keys, ctx))


@router.post("/travelers/{traveler_id}/merge")
async def post_merge(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID, body: MergeIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_MERGE) as ctx:
        return await merge.merge(ctx, Vault(keys, ctx), traveler_id, body.other_traveler_id)
