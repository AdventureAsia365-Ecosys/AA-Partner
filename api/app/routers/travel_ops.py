"""``/crm/v1`` travel operations of a booked trip (route class AGENCY; AA-1070, migrations crm-054 / crm-055): the
departure roster, the traveller vouchers, the server-side PDFs, the rooming list, the passport check and the visa
tracker.

==========================================  ===============================================  ===========================
Route                                       What                                             Who
==========================================  ===============================================  ===========================
``GET    /departures?scope=``               AA departures with the agency's travellers       every role
``GET    /departures/{ref}/roster``         one departure: trips, travellers, checks         every role
``POST   /departures/{ref}/manifest``       the merged manifest CSV (fields picked, logged)  every role (logged reveal)
``GET    /trips/{id}/passports``            each traveller's passport check                  every role
``GET    /trips/{id}/rooming``              the rooming list                                 every role
``PUT    /trips/{id}/rooming``              replace it                                       every role (trip edit)
``GET    /trips/{id}/vouchers``             every confirmed line's voucher                   every role
``GET    /lines/{id}/voucher``              one line's voucher details                       every role
``PUT    /lines/{id}/voucher``              write them (sealed)                              every role (trip edit)
``POST   /trips/{id}/pdfs``                 queue a server-side PDF (202)                    every role (upload)
``GET    /trips/{id}/visas``                the trip's visa applications                     every role
``GET    /trips/{id}/visas/suggestions``    destination countries per traveller              every role
``POST   /trips/{id}/visas``                a new application (booked trips)                 every role (trip edit)
``GET    /visas/{id}``                      one application, the visa number in clear        every role (logged reveal)
``PATCH  /visas/{id}``                      status, dates, granted visa                      every role (trip edit)
``DELETE /visas/{id}``                      delete it                                        every role (trip edit)
``POST   /visas/{id}/checklist``            add a document                                   every role (trip edit)
``PATCH  /visa-checklist/{id}``             tick / untick                                    every role (trip edit)
``DELETE /visa-checklist/{id}``             delete it                                        every role (trip edit)
``GET    /visa-templates``                  the agency's visa guidance                       every role
``POST   /visa-templates``                  add one                                          owner
``PATCH  /visa-templates/{id}``             change it                                        owner
``DELETE /visa-templates/{id}``             delete it                                        owner
``GET    /settings/travel``                 passport months                                  every role
``PUT    /settings/travel``                 change it                                        owner
==========================================  ===============================================  ===========================

No response carries a passport number, a passport expiry or a nationality; a visa number is masked except in the
one-application read.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.documents import pdf_job
from app.errors import Forbidden
from app.ledger import fx
from app.permissions import Action
from app.routers.agency import tenant_header
from app.tenancy import TenantContext, tenant_tx
from app.travel_ops import departures, passport, rooming, visas, vouchers
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["travel-ops"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
DepartureRef = Annotated[str, Path(pattern=r"^[A-Za-z0-9:_.-]{1,80}$")]
Scope = Annotated[Literal["upcoming", "past", "all"], Query()]
VisaType = Literal["evisa", "visa_on_arrival", "embassy", "visa_exempt", "transit", "other"]
VisaStatus = Literal["to_do", "collecting", "submitted", "approved", "refused", "not_needed", "cancelled"]
Country = Annotated[str, Field(pattern=r"^[A-Za-z]{2}$")]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManifestIn(_Body):
    fields: list[str] | None = Field(default=None, max_length=12)


class RoomTravellerIn(_Body):
    traveler_id: uuid.UUID
    bed_pref: Literal["double_bed", "twin_beds", "extra_bed", "cot"] | None = None


class RoomIn(_Body):
    room_no: int = Field(ge=1, le=50)
    room_type: Literal["single", "double", "twin", "triple", "quad", "family"]
    travellers: list[RoomTravellerIn] = Field(min_length=1, max_length=6)


class RoomingIn(_Body):
    rooms: list[RoomIn] = Field(max_length=50)


class VoucherIn(_Body):
    pickup: str | None = Field(default=None, max_length=200)
    dropoff: str | None = Field(default=None, max_length=200)
    service_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    pnr: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=500)


class PdfIn(_Body):
    kind: Literal["confirmation", "invoice", "proposal", "voucher"]
    invoice_id: uuid.UUID | None = None
    proposal_id: uuid.UUID | None = None
    booking_line_id: uuid.UUID | None = None


class VisaIn(_Body):
    traveler_id: uuid.UUID
    country: Country
    visa_type: VisaType | None = None
    visa_template_id: uuid.UUID | None = None
    deadline: dt.date | None = None


class VisaChange(_Body):
    status: VisaStatus | None = None
    visa_type: VisaType | None = None
    deadline: dt.date | None = None
    submitted_on: dt.date | None = None
    decided_on: dt.date | None = None
    visa_no: str | None = Field(default=None, max_length=40)
    valid_from: dt.date | None = None
    valid_until: dt.date | None = None


class ChecklistItemIn(_Body):
    label: str = Field(min_length=1, max_length=200)


class ChecklistTick(_Body):
    done: bool


class VisaTemplateIn(_Body):
    country: Country
    visa_type: VisaType
    processing_days: int | None = Field(default=None, ge=0, le=365)
    fee: str | None = Field(default=None, max_length=14)
    fee_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    documents: list[str] = Field(default_factory=list, max_length=30)
    official_url: str | None = Field(default=None, max_length=500)
    active: bool = True


class VisaTemplateChange(_Body):
    country: Country | None = None
    visa_type: VisaType | None = None
    processing_days: int | None = Field(default=None, ge=0, le=365)
    fee: str | None = Field(default=None, max_length=14)
    fee_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    documents: list[str] | None = Field(default=None, max_length=30)
    official_url: str | None = Field(default=None, max_length=500)
    active: bool | None = None


class TravelSettings(_Body):
    passport_valid_months: int = Field(ge=0, le=24)


def _owner(ctx: TenantContext) -> None:
    if ctx.member_role != "owner":
        raise Forbidden("owner_only", "only the owner changes this")


def _keys(request: Request) -> Any:
    return require_keys(request.app.state.keys)


# ── departures ───────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/departures")
async def get_departures(
    request: Request, caller: CallerDep, tenant_id: TenantDep, scope: Scope = "upcoming"
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await departures.roster_list(ctx, scope, await fx.agency_today(ctx))


@router.get("/departures/{ref}/roster")
async def get_roster(request: Request, caller: CallerDep, tenant_id: TenantDep, ref: DepartureRef) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = _keys(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await departures.roster(ctx, Vault(keys, ctx), ref)


@router.post("/departures/{ref}/manifest")
async def post_manifest(
    request: Request, caller: CallerDep, tenant_id: TenantDep, ref: DepartureRef, body: ManifestIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = _keys(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.REVEAL_SENSITIVE) as ctx:
        return await departures.manifest(ctx, Vault(keys, ctx), ref, body.fields)


# ── passport check, rooming list ─────────────────────────────────────────────────────────────────────────────────
@router.get("/trips/{trip_id}/passports")
async def get_passports(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = _keys(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        end = await ctx.conn.fetchval(
            "SELECT end_date FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL", trip_id
        )
        return {"trip_id": str(trip_id), **await passport.trip_view(ctx, Vault(keys, ctx), trip_id, end)}


@router.get("/trips/{trip_id}/rooming")
async def get_rooming(request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = _keys(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await rooming.view(ctx, Vault(keys, ctx), trip_id)


@router.put("/trips/{trip_id}/rooming")
async def put_rooming(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: RoomingIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = _keys(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        rooms = body.model_dump(mode="json")["rooms"]
        return await rooming.replace(ctx, Vault(keys, ctx), trip_id, rooms)


# ── vouchers, PDFs ───────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/trips/{trip_id}/vouchers")
async def get_vouchers(request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = _keys(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await vouchers.trip_vouchers(ctx, Vault(keys, ctx), trip_id)


@router.get("/lines/{line_id}/voucher")
async def get_voucher(request: Request, caller: CallerDep, tenant_id: TenantDep, line_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = _keys(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await vouchers.get_details(ctx, Vault(keys, ctx), line_id)


@router.put("/lines/{line_id}/voucher")
async def put_voucher(
    request: Request, caller: CallerDep, tenant_id: TenantDep, line_id: uuid.UUID, body: VoucherIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = _keys(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await vouchers.save_details(ctx, Vault(keys, ctx), line_id, body.model_dump())


@router.post("/trips/{trip_id}/pdfs", status_code=202)
async def post_pdf(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: PdfIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    subject = {"invoice": body.invoice_id, "proposal": body.proposal_id, "voucher": body.booking_line_id}.get(body.kind)
    async with tenant_tx(pool, caller, tenant_id, action=Action.DOCUMENT_UPLOAD) as ctx:
        return await pdf_job.request(ctx, trip_id, body.kind, subject)


# ── visas ────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/trips/{trip_id}/visas")
async def get_trip_visas(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = _keys(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await visas.trip_applications(ctx, Vault(keys, ctx), trip_id)


@router.get("/trips/{trip_id}/visas/suggestions")
async def get_visa_suggestions(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = _keys(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await visas.suggest(ctx, Vault(keys, ctx), trip_id)


@router.post("/trips/{trip_id}/visas", status_code=201)
async def post_visa(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: VisaIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = _keys(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await visas.create(ctx, Vault(keys, ctx), trip_id, body.model_dump(mode="python"))


@router.get("/visas/{app_id}")
async def get_visa(request: Request, caller: CallerDep, tenant_id: TenantDep, app_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = _keys(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.REVEAL_SENSITIVE) as ctx:
        return await visas.get(ctx, Vault(keys, ctx), app_id)


@router.patch("/visas/{app_id}")
async def patch_visa(
    request: Request, caller: CallerDep, tenant_id: TenantDep, app_id: uuid.UUID, body: VisaChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = _keys(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await visas.update(ctx, Vault(keys, ctx), app_id, body.model_dump(mode="python", exclude_unset=True))


@router.delete("/visas/{app_id}")
async def delete_visa(request: Request, caller: CallerDep, tenant_id: TenantDep, app_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await visas.delete(ctx, app_id)


@router.post("/visas/{app_id}/checklist", status_code=201)
async def post_visa_item(
    request: Request, caller: CallerDep, tenant_id: TenantDep, app_id: uuid.UUID, body: ChecklistItemIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await visas.add_item(ctx, app_id, body.label)


@router.patch("/visa-checklist/{item_id}")
async def patch_visa_item(
    request: Request, caller: CallerDep, tenant_id: TenantDep, item_id: uuid.UUID, body: ChecklistTick
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await visas.set_item(ctx, item_id, body.done)


@router.delete("/visa-checklist/{item_id}")
async def delete_visa_item(
    request: Request, caller: CallerDep, tenant_id: TenantDep, item_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await visas.delete_item(ctx, item_id)


@router.get("/visa-templates")
async def get_visa_templates(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await visas.list_templates(ctx)


@router.post("/visa-templates", status_code=201)
async def post_visa_template(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: VisaTemplateIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SETTINGS_FORMS_MANAGE) as ctx:
        _owner(ctx)
        return await visas.create_template(ctx, body.model_dump(mode="json"))


@router.patch("/visa-templates/{template_id}")
async def patch_visa_template(
    request: Request, caller: CallerDep, tenant_id: TenantDep, template_id: uuid.UUID, body: VisaTemplateChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SETTINGS_FORMS_MANAGE) as ctx:
        _owner(ctx)
        return await visas.update_template(ctx, template_id, body.model_dump(mode="json", exclude_unset=True))


@router.delete("/visa-templates/{template_id}")
async def delete_visa_template(
    request: Request, caller: CallerDep, tenant_id: TenantDep, template_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SETTINGS_FORMS_MANAGE) as ctx:
        _owner(ctx)
        return await visas.delete_template(ctx, template_id)


# ── settings ─────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/settings/travel")
async def get_travel_settings(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await passport.settings_view(ctx)


@router.put("/settings/travel")
async def put_travel_settings(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: TravelSettings
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.SETTINGS_FORMS_MANAGE) as ctx:
        _owner(ctx)
        return await passport.update_settings(ctx, body.passport_valid_months)
