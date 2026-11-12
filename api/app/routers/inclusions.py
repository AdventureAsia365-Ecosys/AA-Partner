"""``/crm/v1`` the "Included" / "Not included" lists of a proposal (route class AGENCY; AA-1070 item crm-35, migration
crm-059).

=============================================  ==============================================  ====================
Route                                          What                                            Who
=============================================  ==============================================  ====================
``GET    /inclusion-items?archived=``          the agency library (archived lines too with     every role
                                               ``archived=true``)
``POST   /inclusion-items``                    add a line (kind, English, optional Vietnamese) owner, agent
``PATCH  /inclusion-items/{id}``               change it, its order, archive / unarchive it    owner, agent
``DELETE /inclusion-items/{id}``               delete it (versions that picked it keep their   owner, agent
                                               copy)
``GET    /proposals/{id}/inclusions``          a builder version's lines, the AA tours' fixed  every role
                                               lists, the library lines offered
``PUT    /proposals/{id}/inclusions``          pick / unpick / reorder / type the lines of a   every role
                                               builder draft (``seq``): the new list, whole
=============================================  ==============================================  ====================

The library is the agency's commercial wording of what its prices cover, so the owner and the agents keep it (the
matrix's ``money_fields_edit``, like the service catalogue); every role picks lines on a draft (``proposal_draft``).
The PUT answers the whole builder page, like every builder write.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.db import request_pool
from app.permissions import Action
from app.proposals import builder, inclusions
from app.routers.agency import tenant_header
from app.tenancy import tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["inclusions"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]
Kind = Literal["included", "not_included"]
Text = Annotated[str, Field(min_length=1, max_length=inclusions.MAX_TEXT)]
Seq = Annotated[int, Field(ge=1, le=2_000_000_000)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemIn(_Body):
    kind: Kind
    text_en: Text
    text_vi: str | None = Field(default=None, max_length=inclusions.MAX_TEXT)
    sort_order: int | None = Field(default=None, ge=0, le=inclusions.MAX_SORT)


class ItemChange(_Body):
    kind: Kind | None = None
    text_en: Text | None = None
    text_vi: str | None = Field(default=None, max_length=inclusions.MAX_TEXT)
    sort_order: int | None = Field(default=None, ge=0, le=inclusions.MAX_SORT)
    archived: bool | None = None


class LineIn(_Body):
    """A library line (``inclusion_item_id``) or a line typed for this version (``kind`` + ``text_en``)."""

    inclusion_item_id: uuid.UUID | None = None
    kind: Kind | None = None
    text_en: Text | None = None
    text_vi: str | None = Field(default=None, max_length=inclusions.MAX_TEXT)


class LinesIn(_Body):
    seq: Seq
    lines: list[LineIn] = Field(default_factory=list, max_length=inclusions.MAX_LINES)


def _change(model: ItemChange) -> dict[str, Any]:
    data = model.model_dump(exclude_unset=True)
    for required in ("kind", "text_en", "sort_order", "archived"):
        if required in data and data[required] is None:
            data.pop(required)  # a PATCH null on a NOT NULL field keeps the value
    return data


@router.get("/inclusion-items")
async def get_items(
    request: Request, caller: CallerDep, tenant_id: TenantDep, archived: Annotated[bool, Query()] = False
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await inclusions.library(ctx, archived=archived)


@router.post("/inclusion-items", status_code=201)
async def post_item(request: Request, caller: CallerDep, tenant_id: TenantDep, body: ItemIn) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MONEY_FIELDS_EDIT) as ctx:
        return await inclusions.create_item(ctx, body.model_dump())


@router.patch("/inclusion-items/{item_id}")
async def patch_item(
    request: Request, caller: CallerDep, tenant_id: TenantDep, item_id: uuid.UUID, body: ItemChange
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MONEY_FIELDS_EDIT) as ctx:
        return await inclusions.update_item(ctx, item_id, _change(body))


@router.delete("/inclusion-items/{item_id}")
async def delete_item(request: Request, caller: CallerDep, tenant_id: TenantDep, item_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.MONEY_FIELDS_EDIT) as ctx:
        return await inclusions.delete_item(ctx, item_id)


@router.get("/proposals/{proposal_id}/inclusions")
async def get_lines(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.TRIP_VIEW) as ctx:
        return await builder.inclusions_view(ctx, proposal_id)


@router.put("/proposals/{proposal_id}/inclusions")
async def put_lines(
    request: Request, caller: CallerDep, tenant_id: TenantDep, proposal_id: uuid.UUID, body: LinesIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    entries = [line.model_dump(exclude_none=True) for line in body.lines]
    async with tenant_tx(pool, caller, tenant_id, action=Action.PROPOSAL_DRAFT) as ctx:
        return await builder.set_inclusions(ctx, Vault(keys, ctx), proposal_id, body.seq, entries)
