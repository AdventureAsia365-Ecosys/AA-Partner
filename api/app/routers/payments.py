"""``/crm/v1`` payments against invoices, refunds and payment methods (route class AGENCY; card CRM-23 slice 3, AA-952:
the trip's Invoices card, A-10 Payments, Settings > Payments "How clients pay you").

==============================================  ===============================================  =====================
Route                                           What                                             Who
==============================================  ===============================================  =====================
``POST /invoices/{id}/payments``                record money received (partial allowed)          owner, agent
``POST /payments/{id}/refund-requests``         ask to refund (part of) a payment                owner, agent
``GET  /refund-requests``                       to act on (default: proposed, approved)          every role
``POST /refund-requests/{id}/approve``          approve (agent: up to the agency threshold)      owner, agent
``POST /refund-requests/{id}/reject``           reject (the same rule)                           owner, agent
``POST /refund-requests/{id}/execute``          record the money returned                        owner, agent
``GET  /settings/payment-methods``              the agency's own methods + providers             owner
``PUT  /settings/payment-methods/manual``       the manual method's name and pay-by instructions owner
``POST /settings/payment-methods``              connect a provider account (none has code yet)   owner
``POST /settings/payment-methods/{id}/disconnect``  stop using a method                          owner
==============================================  ===============================================  =====================

Amounts are the client's sell amounts; nothing here is margin-bearing. No route moves money: the agency's own account
does, and the CRM records it.
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
from app.payments import service
from app.permissions import Action, require
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["payments"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Money = Annotated[Decimal, Field(gt=0, le=Decimal("999999999999.99"), decimal_places=2)]
Method = Literal["bank_transfer", "cash", "card_offline", "other"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaymentIn(_Body):
    amount: Money
    method: Method
    received_on: dt.date | None = None
    reference: str | None = Field(default=None, max_length=service.MAX_REFERENCE)


class RefundIn(_Body):
    amount: Money
    reason: str = Field(min_length=1, max_length=service.MAX_NOTE)


class DecisionIn(_Body):
    note: str | None = Field(default=None, max_length=service.MAX_NOTE)


class ExecuteIn(_Body):
    method: Method
    refunded_on: dt.date | None = None
    reference: str | None = Field(default=None, max_length=service.MAX_REFERENCE)


class ManualIn(_Body):
    display_name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=2000)


class ConnectIn(_Body):
    provider: str = Field(min_length=1, max_length=24)


@router.post("/invoices/{invoice_id}/payments", status_code=201)
async def post_payment(
    request: Request, caller: CallerDep, tenant_id: TenantDep, invoice_id: uuid.UUID, body: PaymentIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        return await service.record_payment(ctx, Vault(keys, ctx), invoice_id, amount=body.amount, method=body.method,
                                            received_on=body.received_on, reference=body.reference)  # fmt: skip


@router.post("/payments/{payment_id}/refund-requests", status_code=201)
async def post_refund_request(
    request: Request, caller: CallerDep, tenant_id: TenantDep, payment_id: uuid.UUID, body: RefundIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        return await service.request_refund(ctx, payment_id, amount=body.amount, reason=body.reason)


@router.get("/refund-requests")
async def get_refund_requests(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    status: Annotated[str | None, Query(max_length=24)] = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await service.list_refunds(ctx, status)


@router.post("/refund-requests/{refund_id}/approve")
async def post_refund_approve(
    request: Request, caller: CallerDep, tenant_id: TenantDep, refund_id: uuid.UUID, body: DecisionIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.REFUND_APPROVE) as ctx:
        return await service.decide_refund(ctx, refund_id, approve=True, note=body.note)


@router.post("/refund-requests/{refund_id}/reject")
async def post_refund_reject(
    request: Request, caller: CallerDep, tenant_id: TenantDep, refund_id: uuid.UUID, body: DecisionIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.REFUND_APPROVE) as ctx:
        return await service.decide_refund(ctx, refund_id, approve=False, note=body.note)


@router.post("/refund-requests/{refund_id}/execute")
async def post_refund_execute(
    request: Request, caller: CallerDep, tenant_id: TenantDep, refund_id: uuid.UUID, body: ExecuteIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_MARK_RECEIVED) as ctx:
        return await service.execute_refund(ctx, refund_id, method=body.method, refunded_on=body.refunded_on,
                                            reference=body.reference)  # fmt: skip


@router.get("/settings/payment-methods")
async def get_payment_methods(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        return await service.payment_methods(ctx)


@router.put("/settings/payment-methods/manual")
async def put_manual_method(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: ManualIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        require(ctx, Action.PAYMENT_SETTINGS, object_kind="payment_provider_account")
        return await service.save_manual(ctx, display_name=body.display_name, instructions=body.instructions)


@router.post("/settings/payment-methods", status_code=201)
async def post_payment_method(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: ConnectIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        require(ctx, Action.PAYMENT_SETTINGS, object_kind="payment_provider_account")
        return await service.connect(ctx, body.provider)


@router.post("/settings/payment-methods/{account_id}/disconnect")
async def post_payment_method_disconnect(
    request: Request, caller: CallerDep, tenant_id: TenantDep, account_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        require(ctx, Action.PAYMENT_SETTINGS, object_kind="payment_provider_account")
        return await service.disconnect(ctx, account_id)
