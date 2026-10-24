"""``/crm/v1`` ledger, suppliers and payment settings (route class AGENCY; card CRM-16, AA-944: A-11, A-13, the trip's
Bookings tab, Settings > Payments).

=============================================  =============================================  ======================
Route                                          What                                           Who
=============================================  =============================================  ======================
``GET    /suppliers``                          the directory, AA built in (one list_view)     every role
``POST   /suppliers``                          add an own supplier (contact encrypted)        every role
``GET    /suppliers/{id}``                     one supplier (one view)                        every role
``PATCH  /suppliers/{id}``                     edit (default commission: owner, agent)        every role
``DELETE /suppliers/{id}``                     to the trash (not AA, not while lines name it) owner, agent
``POST   /suppliers/{id}/reveal``              one contact field with a reason (one reveal)   every role
``GET    /trips/{id}/lines``                   the trip's lines (money: owner, agent)         every role
``POST   /trips/{id}/lines``                   add a planning agency line                     every role
``PATCH  /lines/{id}``                         edit (not an AA line)                          every role
``GET    /lines/{id}/confirm-check``           what a confirm still misses (codes)            every role
``POST   /lines/{id}/confirm``                 confirm (R18 level; money frozen, R17)         every role
``POST   /lines/{id}/cancel``                  cancel                                         every role
``DELETE /lines/{id}``                         a planning line to the trash                   owner, agent
``PUT    /lines/{id}/money``                   cost / sell / commission (new row)             owner, agent
``GET    /bookings``                           A-11 ledger, base-currency totals (margin)     every role
``GET    /settings/payments``                  base currency, validation level, FX mode       owner
``PUT    /settings/payments``                  change them                                    owner
``GET    /fx/overrides``                       the owner's rates                              owner
``POST   /fx/overrides``                       add a rate for a day                           owner
=============================================  =============================================  ======================

Money leaves only as keyed objects (``cost``, ``commission_expected``, ``margin_base``, ``default_commission_pct``):
the margin serializer strips them for the assistant on every route here, and the database returns no money row to the
assistant anyway (crm-014 ``booking_line_money_margin_roles``).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.ledger import fx, lines, suppliers
from app.lifecycle import trash
from app.permissions import Action, require
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["ledger"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SupplierIn(_Body):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(default="other", max_length=20)
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=200)
    default_commission_pct: Decimal | None = Field(default=None, ge=0, le=100)
    confirmation_unique: bool = False


class SupplierChange(_Body):
    name: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=20)
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=200)
    default_commission_pct: Decimal | None = Field(default=None, ge=0, le=100)
    confirmation_unique: bool | None = None


class RevealIn(_Body):
    field: Literal["contact_name", "contact_email"]
    reason: str = Field(max_length=40)


class LineIn(_Body):
    agency_supplier_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    confirmation_no: str | None = Field(default=None, max_length=80)
    service_from: dt.date | None = None
    service_to: dt.date | None = None
    collect: Literal["agency", "on_arrival"] = "agency"
    confirm_class: Literal["confirmed", "aa_on_request", "agency_booking"] | None = None
    visible_to_travelers: bool = False


class LineChange(_Body):
    agency_supplier_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=200)
    confirmation_no: str | None = Field(default=None, max_length=80)
    service_from: dt.date | None = None
    service_to: dt.date | None = None
    collect: Literal["agency", "on_arrival"] | None = None
    confirm_class: Literal["confirmed", "aa_on_request", "agency_booking"] | None = None
    visible_to_travelers: bool | None = None


class MoneyIn(_Body):
    amount: Decimal = Field(ge=0, le=Decimal("999999999999.99"))
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class LineMoney(_Body):
    cost: MoneyIn | None = None
    sell: MoneyIn | None = None
    commission_expected: MoneyIn | None = None


class PaymentSettings(_Body):
    base_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    booking_validation_level: Literal["off", "basic", "strict"] | None = None
    fx_mode: Literal["automatic", "manual"] | None = None


class OverrideIn(_Body):
    base: str = Field(pattern=r"^[A-Z]{3}$")
    quote: str = Field(pattern=r"^[A-Z]{3}$")
    rate: Decimal = Field(gt=0)
    rate_date: dt.date | None = None


def _payments_owner(ctx: Any) -> None:
    # The matrix again (tenant_tx refused already): Settings > Payments is owner-only (design §4.6).
    require(ctx, Action.PAYMENT_SETTINGS, object_kind="tenant_settings")


# ── suppliers ────────────────────────────────────────────────────────────────────────────────────


@router.get("/suppliers")
async def get_suppliers(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await suppliers.list_suppliers(ctx)


@router.post("/suppliers", status_code=201)
async def post_supplier(request: Request, caller: CallerDep, tenant_id: TenantDep, body: SupplierIn) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        if body.default_commission_pct is not None:
            require(ctx, Action.MONEY_FIELDS_EDIT, object_kind="agency_supplier")
        return await suppliers.create_supplier(ctx, Vault(keys, ctx), body.model_dump())


@router.get("/suppliers/{supplier_id}")
async def get_supplier(
    request: Request, caller: CallerDep, tenant_id: TenantDep, supplier_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await suppliers.get_supplier(ctx, supplier_id)


@router.patch("/suppliers/{supplier_id}")
async def patch_supplier(
    request: Request, caller: CallerDep, tenant_id: TenantDep, supplier_id: uuid.UUID, body: SupplierChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    changes = body.model_dump(exclude_unset=True)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        if "default_commission_pct" in changes:
            require(ctx, Action.MONEY_FIELDS_EDIT, object_kind="agency_supplier", object_id=str(supplier_id))
        return await suppliers.update_supplier(ctx, Vault(keys, ctx), supplier_id, changes)


@router.delete("/suppliers/{supplier_id}")
async def delete_supplier(
    request: Request, caller: CallerDep, tenant_id: TenantDep, supplier_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        await suppliers.trash_check(ctx, supplier_id)
        return await trash.move_to_trash(ctx, "agency_supplier", supplier_id)


@router.post("/suppliers/{supplier_id}/reveal")
async def post_supplier_reveal(
    request: Request, caller: CallerDep, tenant_id: TenantDep, supplier_id: uuid.UUID, body: RevealIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.REVEAL_CONTACT) as ctx:
        return await suppliers.reveal_contact(ctx, Vault(keys, ctx), supplier_id, body.field, body.reason)


# ── lines ────────────────────────────────────────────────────────────────────────────────────────


@router.get("/trips/{trip_id}/lines")
async def get_trip_lines(request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await lines.trip_lines(ctx, trip_id)


@router.post("/trips/{trip_id}/lines", status_code=201)
async def post_trip_line(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: LineIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await lines.create_line(ctx, trip_id, body.model_dump())


@router.patch("/lines/{line_id}")
async def patch_line(
    request: Request, caller: CallerDep, tenant_id: TenantDep, line_id: uuid.UUID, body: LineChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await lines.update_line(ctx, line_id, body.model_dump(exclude_unset=True))


@router.get("/lines/{line_id}/confirm-check")
async def get_confirm_check(request: Request, caller: CallerDep, tenant_id: TenantDep, line_id: uuid.UUID) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await lines.confirm_check(ctx, line_id)


@router.post("/lines/{line_id}/confirm")
async def post_confirm(request: Request, caller: CallerDep, tenant_id: TenantDep, line_id: uuid.UUID) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await lines.confirm(ctx, line_id)


@router.post("/lines/{line_id}/cancel")
async def post_cancel(request: Request, caller: CallerDep, tenant_id: TenantDep, line_id: uuid.UUID) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await lines.cancel(ctx, line_id)


@router.delete("/lines/{line_id}")
async def delete_line(request: Request, caller: CallerDep, tenant_id: TenantDep, line_id: uuid.UUID) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        await lines.trash_check(ctx, line_id)
        return await trash.move_to_trash(ctx, "booking_line", line_id)


@router.put("/lines/{line_id}/money")
async def put_line_money(
    request: Request, caller: CallerDep, tenant_id: TenantDep, line_id: uuid.UUID, body: LineMoney
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MONEY_FIELDS_EDIT) as ctx:
        return await lines.put_money(ctx, line_id, body.model_dump(exclude_none=True))


@router.get("/bookings")
async def get_bookings(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    status: Annotated[Literal["planning", "confirmed", "cancelled"] | None, Query()] = None,
    date_from: Annotated[dt.date | None, Query(alias="from")] = None,
    date_to: Annotated[dt.date | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.REPORTS_VIEW) as ctx:
        return await lines.ledger(ctx, status=status, date_from=date_from, date_to=date_to)


# ── Settings > Payments ──────────────────────────────────────────────────────────────────────────


@router.get("/settings/payments")
async def get_payment_settings(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        return await fx.payment_settings(ctx)


@router.put("/settings/payments")
async def put_payment_settings(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: PaymentSettings
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        _payments_owner(ctx)
        return await fx.update_payment_settings(ctx, body.model_dump(exclude_unset=True))


@router.get("/fx/overrides")
async def get_overrides(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        return await fx.list_overrides(ctx)


@router.post("/fx/overrides", status_code=201)
async def post_override(request: Request, caller: CallerDep, tenant_id: TenantDep, body: OverrideIn) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        _payments_owner(ctx)
        return await fx.add_override(ctx, base=body.base, quote=body.quote, rate=body.rate, rate_date=body.rate_date)
