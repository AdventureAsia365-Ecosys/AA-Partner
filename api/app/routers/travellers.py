"""``/crm/v1`` clients, the party of a trip and documents (route class AGENCY; card CRM-09, A-06 / A-07 / A-03).

Every route opens :func:`app.tenancy.tenant_tx` for the agency in ``X-AA-Tenant-Id`` and works through
:mod:`app.travellers.service`, :mod:`app.travellers.reveal` and :mod:`app.documents.service`. Personal data is
decrypted only through :class:`app.vault.Vault`; the audit events are queued on the context.

==============================================  =================================================  ================
Route                                           What                                               Who
==============================================  =================================================  ================
``GET    /travelers``                           directory, no search (one list_view)               every role
``POST   /travelers/search``                    exact search, the term in the body (one list_view) every role
``POST   /travelers``                           new client (409 duplicate_traveler, R6)            every role
``GET    /travelers/{id}``                      profile: masked F1, F2 bands, relations, trips     every role
``PATCH  /travelers/{id}``                      F1 fields (encrypted; DOB -> age bands)            every role
``PATCH  /travelers/{id}/profile``              F2 bands and codes                                 every role
``PUT    /travelers/{id}/health``               health codes, with the health_data consent         every role
``POST   /travelers/{id}/reveal``               one F1 field with a reason (one reveal event)      every role
``POST   /travelers/{id}/relations``            relationship (the DB writes the inverse, R7)       every role
``DELETE /travelers/{id}/relations/{rel}``      remove a relationship (both sides)                 every role
``POST   /trips/{trip_id}/travelers``           add a client or a new companion (name only)        every role
``DELETE /trips/{trip_id}/travelers/{id}``      take a companion off the trip                      every role
``GET    /trips/{trip_id}/documents``           the trip's documents (one list_view)               every role
``POST   /documents``                           upload (docs DEK, then the bucket)                 every role
``POST   /documents/{id}/download``             the file, with a reason (one reveal event)         every role
``PATCH  /documents/{id}``                      visible to travellers (never a passport)           every role
``DELETE /documents/{id}``                      to the trash                                       owner, agent
==============================================  =================================================  ================

Search is a POST so an e-mail, phone or name never sits in a URL (request logs of the CDN / load balancer, browser
history); ``GET /travelers?q=`` answers 422 ``search_by_post``.

Design §4.6: every role may reveal (each logged); export and delete of a client stay with CRM-14 / CRM-35.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.documents import service as documents
from app.documents.store import require_store
from app.errors import Invalid
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.travellers import service
from app.travellers.fields import RELATION_KINDS, REVEAL_FIELDS, REVEAL_REASONS
from app.travellers.reveal import reveal
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["travellers"])

CONTACT_FIELDS = frozenset({"given_name", "family_name", "email", "phone"})


def reveal_action(field: str) -> Action:
    """Contact fields vs passport / DOB / health / emergency contact (two rows of design §4.6; both every role)."""
    return Action.REVEAL_CONTACT if field in CONTACT_FIELDS else Action.REVEAL_SENSITIVE


CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Code = Annotated[str, Field(max_length=40)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchIn(_Body):
    q: str = Field(min_length=1, max_length=120)
    archived: bool = False
    owner: Literal["anyone", "me"] = "anyone"
    page: int = Field(default=1, ge=1, le=service.MAX_PAGE)


class TravelerIn(_Body):
    display_name: str = Field(min_length=1, max_length=200)
    given_name: str | None = Field(default=None, max_length=200)
    family_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    preferred_channel: Literal["email", "phone", "sms", "whatsapp"] | None = None
    lead_source: str | None = Field(default=None, max_length=40)


class TravelerChange(_Body):
    display_name: str | None = Field(default=None, max_length=200)
    given_name: str | None = Field(default=None, max_length=200)
    family_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    dob: str | None = Field(default=None, max_length=10)
    nationality: str | None = Field(default=None, max_length=2)
    passport_no: str | None = Field(default=None, max_length=30)
    passport_expiry: str | None = Field(default=None, max_length=10)
    passport_country: str | None = Field(default=None, max_length=2)
    emergency_contact: str | None = Field(default=None, max_length=300)
    preferred_channel: Literal["email", "phone", "sms", "whatsapp"] | None = None


class ProfileChange(_Body):
    residence_country: str | None = Field(default=None, max_length=2)
    budget_band: Code | None = None
    pace: Code | None = None
    group_pref: Code | None = None
    fitness_band: Code | None = None
    trek_experience: Code | None = None
    altitude_ok: bool | None = None
    accommodation_tier: Code | None = None
    interests: list[Code] | None = Field(default=None, max_length=20)
    must_do_codes: list[Code] | None = Field(default=None, max_length=20)
    must_avoid_codes: list[Code] | None = Field(default=None, max_length=20)
    occasion: Code | None = None
    budget_flex: Code | None = None
    decision_maker: Code | None = None
    booking_timeline: Code | None = None
    competing_quotes: bool | None = None


class HealthIn(_Body):
    dietary: list[Code] = Field(default_factory=list, max_length=20)
    mobility: list[Code] = Field(default_factory=list, max_length=20)
    medical: list[Code] = Field(default_factory=list, max_length=20)
    consent_recorded: bool = False


class RevealIn(_Body):
    field: Literal[REVEAL_FIELDS]  # type: ignore[valid-type]
    reason: Literal[REVEAL_REASONS]  # type: ignore[valid-type]


class RelationIn(_Body):
    to_traveler_id: uuid.UUID
    kind: Literal[RELATION_KINDS]  # type: ignore[valid-type]


class CompanionIn(_Body):
    display_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)


class PartyIn(_Body):
    traveler_id: uuid.UUID | None = None
    new: CompanionIn | None = None


class DocumentIn(_Body):
    traveler_id: uuid.UUID | None = None
    trip_id: uuid.UUID | None = None
    kind: Literal[documents.KINDS]  # type: ignore[valid-type]
    filename: str = Field(min_length=1, max_length=300)
    content_type: Literal[documents.CONTENT_TYPES]  # type: ignore[valid-type]
    # base64 of at most 10 MiB
    content_b64: str = Field(min_length=4, max_length=(documents.MAX_BYTES * 4) // 3 + 8)
    visible_to_travelers: bool = False


class DownloadIn(_Body):
    reason: Literal[REVEAL_REASONS]  # type: ignore[valid-type]


class DocumentChange(_Body):
    visible_to_travelers: bool


# ── clients ────────────────────────────────────────────────────────────────────────────────────────
@router.get("/travelers")
async def get_travelers(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    q: Annotated[str | None, Query(max_length=120)] = None,
    archived: bool = False,
    owner: Annotated[Literal["anyone", "me"], Query()] = "anyone",
    page: Annotated[int, Query(ge=1, le=service.MAX_PAGE)] = 1,
) -> dict[str, Any]:
    if q is not None:
        raise Invalid("search_by_post", "search with POST /crm/v1/travelers/search (the term stays out of URLs)")
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_VIEW) as ctx:
        return await service.directory(ctx, Vault(keys, ctx), archived=archived, owner=owner, page=page)


@router.post("/travelers/search")
async def post_travelers_search(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: SearchIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_VIEW) as ctx:
        return await service.directory(
            ctx, Vault(keys, ctx), q=body.q, archived=body.archived, owner=body.owner, page=body.page
        )


@router.post("/travelers", status_code=201)
async def post_traveler(request: Request, caller: CallerDep, tenant_id: TenantDep, body: TravelerIn) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_EDIT) as ctx:
        return await service.create_traveler(ctx, Vault(keys, ctx), body.model_dump())


@router.get("/travelers/{traveler_id}")
async def get_traveler(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_VIEW) as ctx:
        return await service.profile(ctx, Vault(keys, ctx), traveler_id)


@router.patch("/travelers/{traveler_id}")
async def patch_traveler(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID, body: TravelerChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_EDIT) as ctx:
        return await service.update_traveler(ctx, Vault(keys, ctx), traveler_id, body.model_dump(exclude_unset=True))


@router.patch("/travelers/{traveler_id}/profile")
async def patch_profile(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID, body: ProfileChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_EDIT) as ctx:
        return await service.update_profile(ctx, Vault(keys, ctx), traveler_id, body.model_dump(exclude_unset=True))


@router.put("/travelers/{traveler_id}/health")
async def put_health(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID, body: HealthIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_EDIT) as ctx:
        return await service.set_health(
            ctx,
            Vault(keys, ctx),
            traveler_id,
            dietary=body.dietary,
            mobility=body.mobility,
            medical=body.medical,
            consent_recorded=body.consent_recorded,
        )


@router.post("/travelers/{traveler_id}/reveal")
async def post_reveal(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID, body: RevealIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=reveal_action(body.field)) as ctx:
        return await reveal(ctx, Vault(keys, ctx), traveler_id, body.field, body.reason)


@router.post("/travelers/{traveler_id}/relations", status_code=201)
async def post_relation(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID, body: RelationIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_EDIT) as ctx:
        return await service.add_relation(ctx, traveler_id, body.to_traveler_id, body.kind)


@router.delete("/travelers/{traveler_id}/relations/{relation_id}")
async def delete_relation(
    request: Request, caller: CallerDep, tenant_id: TenantDep, traveler_id: uuid.UUID, relation_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.CLIENT_EDIT) as ctx:
        return await service.remove_relation(ctx, traveler_id, relation_id)


# ── the party of a trip ────────────────────────────────────────────────────────────────────────────
@router.post("/trips/{trip_id}/travelers", status_code=201)
async def post_party(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: PartyIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await service.add_to_trip(
            ctx,
            Vault(keys, ctx),
            trip_id,
            traveler_id=body.traveler_id,
            new=body.new.model_dump() if body.new is not None else None,
        )


@router.delete("/trips/{trip_id}/travelers/{traveler_id}")
async def delete_party(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, traveler_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await service.remove_from_trip(ctx, Vault(keys, ctx), trip_id, traveler_id)


# ── documents ──────────────────────────────────────────────────────────────────────────────────────
@router.get("/trips/{trip_id}/documents")
async def get_trip_documents(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await documents.trip_documents(ctx, Vault(keys, ctx), trip_id)


@router.post("/documents", status_code=201)
async def post_document(request: Request, caller: CallerDep, tenant_id: TenantDep, body: DocumentIn) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    store = require_store(request.app.state.documents)
    content = documents.decode(body.content_b64)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DOCUMENT_UPLOAD) as ctx:
        return await documents.upload(
            ctx,
            Vault(keys, ctx),
            store,
            traveler_id=body.traveler_id,
            trip_id=body.trip_id,
            kind=body.kind,
            filename=body.filename,
            content_type=body.content_type,
            content=content,
            visible_to_travelers=body.visible_to_travelers,
        )


@router.post("/documents/{document_id}/download")
async def post_download(
    request: Request, caller: CallerDep, tenant_id: TenantDep, document_id: uuid.UUID, body: DownloadIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    store = require_store(request.app.state.documents)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DOCUMENT_DOWNLOAD) as ctx:
        return await documents.download(ctx, Vault(keys, ctx), store, document_id, body.reason)


@router.patch("/documents/{document_id}")
async def patch_document(
    request: Request, caller: CallerDep, tenant_id: TenantDep, document_id: uuid.UUID, body: DocumentChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DOCUMENT_UPLOAD) as ctx:
        return await documents.set_visibility(ctx, document_id, body.visible_to_travelers)


@router.delete("/documents/{document_id}")
async def delete_document(
    request: Request, caller: CallerDep, tenant_id: TenantDep, document_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DOCUMENT_DELETE) as ctx:
        return await documents.trash(ctx, document_id)
