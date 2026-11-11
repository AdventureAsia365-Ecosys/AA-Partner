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
``POST   /lines/{id}/confirm``                 confirm (R18 level; money frozen, R17);        every role
                                               AA-1069: a requested line too, with the
                                               supplier's ``confirmation_no`` in one call
``POST   /lines/{id}/back-to-planning``        AA-1069: a requested line back to planning     every role
``POST   /trips/{id}/supplier-requests``       AA-1069: one e-mail draft per supplier of the  every role
                                               trip's planning agency lines
``POST   /trips/{id}/supplier-requests/send``  AA-1069: send one supplier's draft (agency     MESSAGE_SEND
                                               mailbox) and mark its lines requested
``POST   /lines/{id}/cancel``                  cancel                                         every role
``DELETE /lines/{id}``                         a planning line to the trash                   owner, agent
``PUT    /lines/{id}/money``                   cost / sell / commission (new row)             owner, agent
``GET    /bookings``                           A-11 ledger, base-currency totals (margin)     every role
``GET    /settings/payments``                  base currency, validation level, FX mode       owner
``PUT    /settings/payments``                  change them                                    owner
``GET    /fx/overrides``                       the owner's rates                              owner
``POST   /fx/overrides``                       add a rate for a day                           owner
``GET    /settings/pricing``                   AA-1066: age bands, discount limit, price      owner
                                               rules
``PUT    /settings/pricing``                   AA-1066: the bands and the discount limit      owner
``POST   /settings/price-rules``               AA-1066: add a retail price rule               owner
``PATCH  /settings/price-rules/{id}``          AA-1066: edit one                              owner
``DELETE /settings/price-rules/{id}``          AA-1066: delete one                            owner
``GET    /settings/payment-schedule``          AA-1067: the schedule template (or AA's)       owner
``PUT    /settings/payment-schedule``          AA-1067: set it (null = the AA default)        owner
``GET    /cancellation-policies``              AA-1067: every version + the AA table          every role
``POST   /cancellation-policies``              AA-1067: a draft (AA table, blank, new         owner
                                               version of a published one)
``GET    /cancellation-policies/{id}``         AA-1067: one version                           every role
``PATCH  /cancellation-policies/{id}``         AA-1067: edit a draft (name, currency, tiers)  owner
``DELETE /cancellation-policies/{id}``         AA-1067: delete a draft                        owner
``POST   /cancellation-policies/{id}/publish`` AA-1067: freeze it (optionally the default)    owner
``POST   /cancellation-policies/{id}/default`` AA-1067: the default for new proposals         owner
``POST   /cancellation-policies/{id}/retire``  AA-1067: no longer for new proposals           owner
``GET    /trips/{id}/cancellation-charge``     AA-1067: what the trip's cancel charged        every role
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
from app.cancel_policy import service as cancel_policy
from app.db import request_pool
from app.ledger import fx, lines, requests, suppliers
from app.lifecycle import trash
from app.payments import schedule_settings
from app.permissions import Action, require
from app.proposals import pricing, retail
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
    commission_due_days: int | None = Field(default=None, ge=0, le=365)  # AA-957
    confirmation_unique: bool = False


class SupplierChange(_Body):
    name: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=20)
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=200)
    default_commission_pct: Decimal | None = Field(default=None, ge=0, le=100)
    commission_due_days: int | None = Field(default=None, ge=0, le=365)  # AA-957
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


MarkupPct = Annotated[Decimal, Field(ge=0, le=300, max_digits=5, decimal_places=2, allow_inf_nan=False)]


class RetailMarkup(_Body):
    """AA-1030: the agency's default retail markup per AA product type; null removes one (no markup)."""

    retail_markup_pct: dict[
        Literal["trip_plan", "hotel_room", "transfer", "guide", "activity", "entrance", "meal"], MarkupPct | None
    ] = Field(max_length=7)


Pct = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2, allow_inf_nan=False)]
AdjustPct = Annotated[Decimal, Field(ge=-90, le=300, max_digits=5, decimal_places=2, allow_inf_nan=False)]
RuleKind = Literal["season", "group_size", "early_booking", "last_minute"]
RuleScope = Literal["all", "trip_plan", "hotel_room", "transfer", "guide", "activity", "entrance", "meal"]


class PricingSettingsIn(_Body):
    """AA-1066: the agency's retail age bands and the discount limit above which the owner approves."""

    child_age_max: int | None = Field(default=None, ge=1, le=17)
    infant_age_max: int | None = Field(default=None, ge=0, le=5)
    discount_owner_limit_pct: Pct | None = None


class PriceRuleIn(_Body):
    """AA-1066: one retail price rule (the kind's own condition only; the service checks the combination)."""

    name: str = Field(min_length=1, max_length=80)
    kind: RuleKind
    product_scope: RuleScope = "all"
    aa_tour_source_id: str | None = Field(default=None, min_length=1, max_length=128)
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    min_pax: int | None = Field(default=None, ge=2, le=99)
    days_before: int | None = Field(default=None, ge=0, le=730)
    adjust_pct: AdjustPct
    priority: int = Field(default=100, ge=1, le=999)
    stacking: Literal["add", "best_only"] = "add"
    active: bool = True


class PriceRuleChange(_Body):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    kind: RuleKind | None = None
    product_scope: RuleScope | None = None
    aa_tour_source_id: str | None = Field(default=None, min_length=1, max_length=128)
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    min_pax: int | None = Field(default=None, ge=2, le=99)
    days_before: int | None = Field(default=None, ge=0, le=730)
    adjust_pct: AdjustPct | None = None
    priority: int | None = Field(default=None, ge=1, le=999)
    stacking: Literal["add", "best_only"] | None = None
    active: bool | None = None


class OverrideIn(_Body):
    base: str = Field(pattern=r"^[A-Z]{3}$")
    quote: str = Field(pattern=r"^[A-Z]{3}$")
    rate: Decimal = Field(gt=0)
    rate_date: dt.date | None = None


class TemplateDueIn(_Body):
    anchor: Literal["accept", "before_start"]
    days: int | None = Field(default=None, ge=0, le=730)


class TemplateLineIn(_Body):
    """AA-1067: one line of the payment schedule template (the service checks the order and the percents)."""

    kind: Literal["deposit", "instalment", "balance"]
    percent: Decimal | None = Field(default=None, gt=0, le=100, decimal_places=2)
    due: TemplateDueIn


class ScheduleTemplateIn(_Body):
    template: list[TemplateLineIn] | None = Field(default=None, min_length=1, max_length=12)


class TierIn(_Body):
    days_before_min: int = Field(ge=0, le=730)
    charge_pct: Decimal = Field(ge=0, le=100, decimal_places=2)
    min_charge_amount: Decimal | None = Field(default=None, gt=0, le=Decimal("99999999.99"), decimal_places=2)


class PolicyIn(_Body):
    """AA-1067: a new draft: a new version of a published policy, the AA starting table, or name + tiers."""

    from_policy_id: uuid.UUID | None = None
    aa_default: bool = False
    name: str | None = Field(default=None, max_length=80)
    currency: str | None = Field(default=None, max_length=3)
    tiers: list[TierIn] | None = Field(default=None, min_length=1, max_length=12)


class PolicyChange(_Body):
    name: str | None = Field(default=None, max_length=80)
    currency: str | None = Field(default=None, max_length=3)
    tiers: list[TierIn] | None = Field(default=None, min_length=1, max_length=12)


class PolicyPublishIn(_Body):
    make_default: bool = False


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
        if body.default_commission_pct is not None or body.commission_due_days is not None:
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
        if "default_commission_pct" in changes or "commission_due_days" in changes:
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


class ConfirmIn(_Body):
    """AA-1069: the number the supplier answered (saved, then the line is confirmed in the same call)."""

    confirmation_no: str | None = Field(default=None, min_length=1, max_length=80)


class SupplierRequestsIn(_Body):
    line_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=requests.MAX_LINES)
    include_names: bool | None = None  # null = by supplier category (flight, insurance, visa: yes)


class SupplierRequestSendIn(_Body):
    message_id: uuid.UUID
    subject: str | None = Field(default=None, max_length=300)
    body: str = Field(min_length=1, max_length=100_000)
    line_ids: list[uuid.UUID] = Field(min_length=1, max_length=requests.MAX_LINES)


@router.post("/lines/{line_id}/confirm")
async def post_confirm(
    request: Request, caller: CallerDep, tenant_id: TenantDep, line_id: uuid.UUID, body: ConfirmIn | None = None
) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await lines.confirm(ctx, line_id, confirmation_no=body.confirmation_no if body else None)


@router.post("/lines/{line_id}/back-to-planning")
async def post_back_to_planning(request: Request, caller: CallerDep, tenant_id: TenantDep, line_id: uuid.UUID) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        await requests.back_to_planning(ctx, line_id)
        return await lines.line(ctx, line_id)


@router.post("/trips/{trip_id}/supplier-requests")
async def post_supplier_requests(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: SupplierRequestsIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_EDIT) as ctx:
        return await requests.build(
            ctx, Vault(keys, ctx), trip_id, line_ids=body.line_ids, include_names=body.include_names
        )


@router.post("/trips/{trip_id}/supplier-requests/send", status_code=202)
async def post_supplier_request_send(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: SupplierRequestSendIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MESSAGE_SEND) as ctx:
        return await requests.send(
            ctx, Vault(keys, ctx), trip_id, message_id=body.message_id, subject=body.subject, body=body.body,
            line_ids=body.line_ids,
        )  # fmt: skip


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


# AA-1030 (crm-046, root ADR 0019 b15): the default retail markup the builder suggests a new AA option's sell from.
@router.get("/settings/retail-markup")
async def get_retail_markup(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        return await retail.read(ctx)


@router.put("/settings/retail-markup")
async def put_retail_markup(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: RetailMarkup
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        _payments_owner(ctx)
        return await retail.update(ctx, body.retail_markup_pct)


# AA-1066 (crm-048): Settings > Pricing, owner only like Settings > Payments.
@router.get("/settings/pricing")
async def get_pricing(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        return await pricing.read(ctx)


@router.put("/settings/pricing")
async def put_pricing(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: PricingSettingsIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        _payments_owner(ctx)
        return await pricing.update_settings(ctx, body.model_dump(exclude_none=True))


@router.post("/settings/price-rules", status_code=201)
async def post_price_rule(request: Request, caller: CallerDep, tenant_id: TenantDep, body: PriceRuleIn) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        _payments_owner(ctx)
        return await pricing.create_rule(ctx, body.model_dump())


@router.patch("/settings/price-rules/{rule_id}")
async def patch_price_rule(
    request: Request, caller: CallerDep, tenant_id: TenantDep, rule_id: uuid.UUID, body: PriceRuleChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        _payments_owner(ctx)
        return await pricing.update_rule(ctx, rule_id, body.model_dump(exclude_unset=True))


@router.delete("/settings/price-rules/{rule_id}")
async def delete_price_rule(
    request: Request, caller: CallerDep, tenant_id: TenantDep, rule_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        _payments_owner(ctx)
        return await pricing.delete_rule(ctx, rule_id)


# AA-1067 (crm-050): the payment schedule template a new builder version starts with (owner only).
@router.get("/settings/payment-schedule")
async def get_payment_schedule(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        return await schedule_settings.read(ctx)


@router.put("/settings/payment-schedule")
async def put_payment_schedule(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: ScheduleTemplateIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        _payments_owner(ctx)
        lines = None if body.template is None else [line.model_dump(mode="json", exclude_none=True)
                                                    for line in body.template]  # fmt: skip
        return await schedule_settings.update(ctx, lines)


# AA-1067 (crm-049): the agency's traveller cancellation policy (every role reads; the owner writes).
@router.get("/cancellation-policies")
async def get_cancellation_policies(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PIPELINE_VIEW) as ctx:
        return await cancel_policy.listing(ctx)


@router.post("/cancellation-policies", status_code=201)
async def post_cancellation_policy(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: PolicyIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        _payments_owner(ctx)
        return await cancel_policy.create(ctx, body.model_dump(mode="json", exclude_none=True))


@router.get("/cancellation-policies/{policy_id}")
async def get_cancellation_policy(
    request: Request, caller: CallerDep, tenant_id: TenantDep, policy_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PIPELINE_VIEW) as ctx:
        return await cancel_policy.get(ctx, policy_id)


@router.patch("/cancellation-policies/{policy_id}")
async def patch_cancellation_policy(
    request: Request, caller: CallerDep, tenant_id: TenantDep, policy_id: uuid.UUID, body: PolicyChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        _payments_owner(ctx)
        return await cancel_policy.update_draft(ctx, policy_id, body.model_dump(mode="json", exclude_unset=True))


@router.delete("/cancellation-policies/{policy_id}")
async def delete_cancellation_policy(
    request: Request, caller: CallerDep, tenant_id: TenantDep, policy_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        _payments_owner(ctx)
        return await cancel_policy.delete_draft(ctx, policy_id)


@router.post("/cancellation-policies/{policy_id}/{action}")
async def post_cancellation_policy_action(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    policy_id: uuid.UUID,
    action: Literal["publish", "default", "retire"],
    body: PolicyPublishIn | None = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PAYMENT_SETTINGS) as ctx:
        _payments_owner(ctx)
        if action == "publish":
            return await cancel_policy.publish(ctx, policy_id, make_default=bool(body and body.make_default))
        if action == "default":
            return await cancel_policy.set_default(ctx, policy_id)
        return await cancel_policy.retire(ctx, policy_id)


@router.get("/trips/{trip_id}/cancellation-charge")
async def get_cancellation_charge(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return {"trip_id": str(trip_id), "charge": await cancel_policy.charge_of(ctx, trip_id)}


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
