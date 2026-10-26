"""``/crm/v1`` day-list builder (route class AGENCY; card CRM-21a, AA-949: A-04 builder, WF-4).

=================================================  =============================================  =====================
Route                                              What                                           Who
=================================================  =============================================  =====================
``POST   /trips/{id}/builder``                     a builder draft: Stage 2 skeleton, or a        every role
                                                   working copy of a version (``from``)
``GET    /proposals/{id}/builder``                 the builder page (net / margin: owner, agent)  every role
``PATCH  /proposals/{id}/builder``                 settings: visibility, rooms, deposit share,    every role (deposit
                                                   day 1 date, balance date, terms, intro         share: owner, agent)
``POST   /proposals/{id}/days``                    add a day                                      every role
``PATCH  /proposals/{id}/days/{day}``              edit a day (title, place, tz, notes)           every role
``DELETE /proposals/{id}/days/{day}``              delete a day and its blocks                    every role
``PUT    /proposals/{id}/days/order``              reorder the trip days                          every role
``POST   /proposals/{id}/days/{day}/blocks``       add an option block                            every role
``PATCH  /proposals/{id}/blocks/{block}``          edit a block (default, may swap, order)        every role
``DELETE /proposals/{id}/blocks/{block}``          delete a block and its options                 every role
``POST   /proposals/{id}/blocks/{block}/options``  add an agency item                             every role (sell /
                                                                                                  net: owner, agent)
``POST   /proposals/{id}/blocks/{block}/aa-options`` add an AA item (live Booking quote)          every role (sell:
                                                                                                  owner, agent)
``PATCH  /proposals/{id}/options/{option}``        edit an item                                   every role (sell /
                                                                                                  net: owner, agent)
``DELETE /proposals/{id}/options/{option}``        delete an item (its rules go too)              every role
``POST   /proposals/{id}/rules``                   add a requires / excludes rule (Advanced)      every role
``DELETE /proposals/{id}/rules/{rule}``            delete a rule                                  every role
``POST   /proposals/{id}/aa-refresh``              quote the AA items again (live, oldest first,  every role
                                                   40 a run; the rest reported ``refresh_limit``)
``POST   /proposals/{id}/publish``                 publish vN                                     owner, agent
=================================================  =============================================  =====================

Every write sends ``seq`` (the ``edit_seq`` the page read); another value is 409 ``version_conflict`` (§11.2). Writes
answer the whole builder page. Booking is called after the membership check and outside the database transaction
(AA-947 rule): the AA routes run one transaction to plan, the quote, then one transaction to store.
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
from app.errors import Unavailable
from app.permissions import Action, require
from app.proposals import aa_options, builder
from app.routers.agency import tenant_header
from app.tenancy import TenantContext, tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["builder"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Seq = Annotated[int, Field(ge=1, le=2_000_000_000)]
SeqQuery = Annotated[int, Query(ge=1, le=2_000_000_000)]
Money = Annotated[Decimal, Field(ge=0, le=Decimal("999999999999.99"), decimal_places=2)]
Tier = Literal["budget", "comfort", "premium", "luxury"]
BlockType = Literal["lodging", "activity", "transport", "meal", "guide", "other"]
ComponentKind = Literal["trip_plan", "hotel_room", "transfer", "guide", "activity", "entrance", "meal"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateIn(_Body):
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    from_proposal_id: uuid.UUID | None = None


class RoomIn(_Body):
    type: Literal["single", "double", "twin", "triple", "family"]
    adults: int = Field(ge=0, le=6)
    children: int = Field(default=0, ge=0, le=6)


class SettingsIn(_Body):
    seq: Seq
    price_visibility: Literal["total_with_deltas", "total_only"] | None = None
    room_config: list[RoomIn] | None = Field(default=None, min_length=1, max_length=20)
    deposit_pct: Decimal | None = Field(default=None, ge=1, le=100, decimal_places=2)
    starts_on: dt.date | None = None
    balance_due_on: dt.date | None = None
    terms_doc_id: uuid.UUID | None = None
    intro: str | None = Field(default=None, max_length=20_000)


class DayIn(_Body):
    seq: Seq
    day_no: int | None = Field(default=None, ge=1, le=90)
    title: str | None = Field(default=None, max_length=120)
    place: str | None = Field(default=None, max_length=120)
    tz: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=builder.MAX_NOTES)


class DayChange(_Body):
    seq: Seq
    title: str | None = Field(default=None, max_length=120)
    place: str | None = Field(default=None, max_length=120)
    tz: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=builder.MAX_NOTES)


class DayOrderIn(_Body):
    seq: Seq
    day_ids: list[uuid.UUID] = Field(min_length=1, max_length=builder.MAX_DAYS)


class BlockIn(_Body):
    seq: Seq
    title: str = Field(min_length=1, max_length=120)
    type: BlockType = "other"
    client_may_swap: bool = True


class BlockChange(_Body):
    seq: Seq
    title: str | None = Field(default=None, min_length=1, max_length=120)
    type: BlockType | None = None
    client_may_swap: bool | None = None
    default_option_id: uuid.UUID | None = None
    sort_order: int | None = Field(default=None, ge=0, le=99)


class OptionIn(_Body):
    seq: Seq
    title: str = Field(min_length=1, max_length=200)
    tier: Tier | None = None
    agency_supplier_id: uuid.UUID | None = None
    sell_amount: Money | None = None
    net_amount: Money | None = None
    pay_on_arrival: bool = False
    choose_later_allowed: bool = False


class AaOptionIn(_Body):
    seq: Seq
    tour_source_id: str = Field(min_length=1, max_length=128)
    component_id: str = Field(pattern=r"^[A-Za-z0-9:_.-]{1,64}$")
    component_kind: ComponentKind
    departure_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9:_.-]{1,64}$")
    nights: int | None = Field(default=None, ge=1, le=60)  # required for a hotel_room (422 invalid_nights)
    tier: Tier | None = None
    sell_amount: Money | None = None


class OptionChange(_Body):
    seq: Seq
    title: str | None = Field(default=None, min_length=1, max_length=200)
    tier: Tier | None = None
    agency_supplier_id: uuid.UUID | None = None
    sell_amount: Money | None = None
    net_amount: Money | None = None
    pay_on_arrival: bool | None = None
    choose_later_allowed: bool | None = None


class RuleIn(_Body):
    seq: Seq
    kind: Literal["requires", "excludes"]
    option_id: uuid.UUID
    target_option_id: uuid.UUID


class SeqIn(_Body):
    seq: Seq


class PublishIn(_Body):
    seq: Seq
    acknowledge_selection_seq: int | None = Field(default=None, ge=0)


def _money_check(ctx: TenantContext, body: BaseModel, names: tuple[str, ...], object_id: uuid.UUID) -> None:
    """Sell, net and the deposit share are money fields: the assistant prepares drafts without them (§4.6)."""
    given = body.model_dump(exclude_unset=True)
    if any(n in given for n in names):  # an explicit null clears the field: a money edit too
        require(ctx, Action.MONEY_FIELDS_EDIT, object_kind="proposal", object_id=str(object_id))


def _client(request: Request) -> Any:
    client = getattr(request.app.state, "booking_client", None)
    if client is None:
        raise Unavailable("booking_not_configured", "AA products are not connected on this environment")
    return client


def _changes(body: BaseModel) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True, mode="python")
    data.pop("seq", None)
    return data


@router.post("/trips/{trip_id}/builder", status_code=201)
async def post_builder(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: CreateIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.create(
            ctx, Vault(keys, ctx), trip_id, currency=body.currency, from_proposal_id=body.from_proposal_id
        )


@router.get("/proposals/{proposal_id}/builder")
async def get_builder(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await builder.view(ctx, Vault(keys, ctx), proposal_id)


@router.patch("/proposals/{proposal_id}/builder")
async def patch_builder(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, body: SettingsIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        _money_check(ctx, body, ("deposit_pct",), proposal_id)
        changes = _changes(body)
        if body.room_config is not None:
            changes["room_config"] = [r.model_dump() for r in body.room_config]
        return await builder.update_settings(ctx, Vault(keys, ctx), proposal_id, body.seq, changes)


@router.post("/proposals/{proposal_id}/days", status_code=201)
async def post_day(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, body: DayIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.add_day(ctx, Vault(keys, ctx), proposal_id, body.seq, _changes(body))


@router.put("/proposals/{proposal_id}/days/order")
async def put_day_order(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, body: DayOrderIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.reorder_days(ctx, Vault(keys, ctx), proposal_id, body.seq, body.day_ids)


@router.patch("/proposals/{proposal_id}/days/{day_id}")
async def patch_day(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, day_id: uuid.UUID,
    body: DayChange,
) -> dict[str, Any]:  # fmt: skip
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.update_day(ctx, Vault(keys, ctx), proposal_id, body.seq, day_id, _changes(body))


@router.delete("/proposals/{proposal_id}/days/{day_id}")
async def delete_day(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, day_id: uuid.UUID,
    seq: SeqQuery,
) -> dict[str, Any]:  # fmt: skip
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.delete_day(ctx, Vault(keys, ctx), proposal_id, seq, day_id)


@router.post("/proposals/{proposal_id}/days/{day_id}/blocks", status_code=201)
async def post_block(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, day_id: uuid.UUID,
    body: BlockIn,
) -> dict[str, Any]:  # fmt: skip
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.add_block(ctx, Vault(keys, ctx), proposal_id, body.seq, day_id, _changes(body))


@router.patch("/proposals/{proposal_id}/blocks/{block_id}")
async def patch_block(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, block_id: uuid.UUID,
    body: BlockChange,
) -> dict[str, Any]:  # fmt: skip
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.update_block(ctx, Vault(keys, ctx), proposal_id, body.seq, block_id, _changes(body))


@router.delete("/proposals/{proposal_id}/blocks/{block_id}")
async def delete_block(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, block_id: uuid.UUID,
    seq: SeqQuery,
) -> dict[str, Any]:  # fmt: skip
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.delete_block(ctx, Vault(keys, ctx), proposal_id, seq, block_id)


@router.post("/proposals/{proposal_id}/blocks/{block_id}/options", status_code=201)
async def post_option(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, block_id: uuid.UUID,
    body: OptionIn,
) -> dict[str, Any]:  # fmt: skip
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        _money_check(ctx, body, ("sell_amount", "net_amount"), proposal_id)
        return await builder.add_agency_option(ctx, Vault(keys, ctx), proposal_id, body.seq, block_id, _changes(body))


@router.post("/proposals/{proposal_id}/blocks/{block_id}/aa-options", status_code=201)
async def post_aa_option(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, block_id: uuid.UUID,
    body: AaOptionIn,
) -> dict[str, Any]:  # fmt: skip
    client = _client(request)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        _money_check(ctx, body, ("sell_amount",), proposal_id)
        plan = await builder.aa_plan(ctx, proposal_id, body.seq, block_id, _changes(body))
    quote = await aa_options.quote(client, tenant_id, plan)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.add_aa_option(ctx, Vault(keys, ctx), plan, quote, _changes(body))


@router.patch("/proposals/{proposal_id}/options/{option_id}")
async def patch_option(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, option_id: uuid.UUID,
    body: OptionChange,
) -> dict[str, Any]:  # fmt: skip
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        _money_check(ctx, body, ("sell_amount", "net_amount"), proposal_id)
        return await builder.update_option(ctx, Vault(keys, ctx), proposal_id, body.seq, option_id, _changes(body))


@router.delete("/proposals/{proposal_id}/options/{option_id}")
async def delete_option(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, option_id: uuid.UUID,
    seq: SeqQuery,
) -> dict[str, Any]:  # fmt: skip
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.delete_option(ctx, Vault(keys, ctx), proposal_id, seq, option_id)


@router.post("/proposals/{proposal_id}/rules", status_code=201)
async def post_rule(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, body: RuleIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.add_rule(ctx, Vault(keys, ctx), proposal_id, body.seq, _changes(body))


@router.delete("/proposals/{proposal_id}/rules/{rule_id}")
async def delete_rule(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, rule_id: uuid.UUID,
    seq: SeqQuery,
) -> dict[str, Any]:  # fmt: skip
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.delete_rule(ctx, Vault(keys, ctx), proposal_id, seq, rule_id)


@router.post("/proposals/{proposal_id}/aa-refresh")
async def post_aa_refresh(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, body: SeqIn
) -> dict[str, Any]:
    client = _client(request)
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        plans = await builder.aa_refresh_plan(ctx, proposal_id, body.seq)
    answers, failed = await aa_options.refresh(client, tenant_id, plans)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        out = await builder.apply_refresh(ctx, Vault(keys, ctx), proposal_id, body.seq, answers)
    skipped = sum(1 for f in failed if f["code"] == "refresh_limit")
    return {**out, "refresh": {"quoted": len(answers), "failed": failed, "skipped": skipped}}


@router.post("/proposals/{proposal_id}/publish")
async def post_publish(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, body: PublishIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_PUBLISH) as ctx:
        return await builder.publish(
            ctx, Vault(keys, ctx), proposal_id, body.seq, acknowledge_selection_seq=body.acknowledge_selection_seq
        )
