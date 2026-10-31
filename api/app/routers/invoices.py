"""``/crm/v1`` invoices (route class AGENCY; card CRM-23 slice 2, AA-952: A-10 Receivables, the trip's Proposal tab,
Settings > Payments "Invoices").

==============================================  ===============================================  =====================
Route                                           What                                             Who
==============================================  ===============================================  =====================
``GET  /trips/{id}/invoices``                   the trip's invoices (newest first)               every role
``POST /trips/{id}/invoices``                   a draft: from a schedule row, or typed lines     owner, agent
``GET  /invoices``                              receivables (default: issued, unpaid)            every role
``GET  /invoices/{id}``                         one invoice with its lines                       every role
``PATCH /invoices/{id}``                        edit a draft                                     owner, agent
``POST /invoices/{id}/issue``                   number, issue date, sealed                       owner, agent
``POST /invoices/{id}/void``                    void a draft or an unpaid invoice (reason)       owner, agent
``POST /invoices/{id}/reject-claim``            the client's transfer did not arrive (slice 4)   owner, agent
``GET  /invoices/{id}/document``                the printable HTML (JSON envelope)               every role
``GET  /settings/invoices``                     prefix, tax, due days, service fees              owner
``PUT  /settings/invoices``                     change them                                      owner
==============================================  ===============================================  =====================

Amounts are the client's sell amounts; nothing here is margin-bearing.
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
from app.errors import Invalid
from app.invoices import service
from app.payments import claims
from app.permissions import Action, require
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["invoices"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Money = Annotated[Decimal, Field(gt=0, le=Decimal("999999999999.99"), decimal_places=2)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LineIn(_Body):
    description: str = Field(min_length=1, max_length=service.MAX_DESCRIPTION)
    quantity: Annotated[Decimal, Field(gt=0, le=service.MAX_QUANTITY, decimal_places=2)] = Decimal(1)
    unit_amount: Money


class InvoiceIn(_Body):
    schedule_id: uuid.UUID | None = None
    kind: Literal["service_fee", "adjustment"] | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    lines: list[LineIn] | None = Field(default=None, min_length=1, max_length=service.MAX_LINES)
    memo: str | None = Field(default=None, max_length=service.MAX_MEMO)
    due_on: dt.date | None = None
    include_trip_lines: bool = False


class InvoiceChange(_Body):
    memo: str | None = Field(default=None, max_length=service.MAX_MEMO)
    due_on: dt.date | None = None
    tax_rate: Annotated[Decimal, Field(ge=0, le=100, decimal_places=3)] | None = None
    tax_label: str | None = Field(default=None, min_length=1, max_length=24)
    tax_inclusive: bool | None = None
    lines: list[LineIn] | None = Field(default=None, min_length=1, max_length=service.MAX_LINES)


class VoidIn(_Body):
    reason: str = Field(min_length=1, max_length=500)


class InvoiceSettings(_Body):
    invoice_prefix: str | None = Field(default=None, pattern=r"^[A-Z0-9][A-Z0-9-]{0,11}$")
    invoice_tax_label: str | None = Field(default=None, min_length=1, max_length=24)
    invoice_tax_rate: Annotated[Decimal, Field(ge=0, le=100, decimal_places=3)] | None = None
    invoice_tax_inclusive: bool | None = None
    invoice_due_days: int | None = Field(default=None, ge=0, le=365)
    service_fee_enabled: bool | None = None


def _lines(lines: list[LineIn] | None) -> list[dict[str, Any]] | None:
    return None if lines is None else [line.model_dump() for line in lines]


@router.get("/trips/{trip_id}/invoices")
async def get_trip_invoices(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.list_for_trip(ctx, trip_id)


@router.post("/trips/{trip_id}/invoices", status_code=201)
async def post_invoice(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: InvoiceIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        if body.schedule_id is not None:
            if body.kind is not None or body.lines is not None or body.currency is not None:
                raise Invalid("schedule_or_lines", "a schedule row, or a kind with currency and lines: not both")
            return await service.create_from_schedule(ctx, trip_id, body.schedule_id)
        return await service.create_typed(
            ctx,
            trip_id,
            kind=body.kind or "",
            currency=body.currency or "",
            lines=_lines(body.lines),
            memo=body.memo,
            due_on=body.due_on,
            include_trip_lines=body.include_trip_lines,
        )


@router.get("/invoices")
async def get_invoices(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    status: Annotated[str | None, Query(max_length=24)] = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.receivables(ctx, status)


@router.get("/invoices/{invoice_id}")
async def get_invoice(
    request: Request, caller: CallerDep, tenant_id: TenantDep, invoice_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.get(ctx, invoice_id)


@router.patch("/invoices/{invoice_id}")
async def patch_invoice(
    request: Request, caller: CallerDep, tenant_id: TenantDep, invoice_id: uuid.UUID, body: InvoiceChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    changes = body.model_dump(exclude_unset=True)
    if "lines" in changes:
        changes["lines"] = _lines(body.lines)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        return await service.update_draft(ctx, invoice_id, changes)


@router.post("/invoices/{invoice_id}/issue")
async def post_invoice_issue(
    request: Request, caller: CallerDep, tenant_id: TenantDep, invoice_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        return await service.issue(ctx, invoice_id)


@router.post("/invoices/{invoice_id}/void")
async def post_invoice_void(
    request: Request, caller: CallerDep, tenant_id: TenantDep, invoice_id: uuid.UUID, body: VoidIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        return await service.void(ctx, invoice_id, body.reason)


@router.post("/invoices/{invoice_id}/reject-claim")
async def post_invoice_reject_claim(
    request: Request, caller: CallerDep, tenant_id: TenantDep, invoice_id: uuid.UUID
) -> dict[str, Any]:
    """AA-952 slice 4: "Not received": a ``payment_claimed`` invoice goes back to ``open`` (409 ``invoice_not_claimed``
    otherwise); its verify task is closed."""
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        await claims.reject_claim(ctx, invoice_id)
        return await service.full(ctx, invoice_id)


@router.get("/invoices/{invoice_id}/document")
async def get_invoice_document(
    request: Request, caller: CallerDep, tenant_id: TenantDep, invoice_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.document(ctx, Vault(keys, ctx), invoice_id)


@router.get("/settings/invoices")
async def get_invoice_settings(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        return await service.get_settings(ctx)


@router.put("/settings/invoices")
async def put_invoice_settings(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: InvoiceSettings
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        require(ctx, Action.PAYMENT_SETTINGS, object_kind="tenant_settings")
        return await service.update_settings(ctx, body.model_dump(exclude_unset=True))
