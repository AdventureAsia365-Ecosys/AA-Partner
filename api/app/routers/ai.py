"""``/crm/v1`` AI settings and drafts (route class AGENCY; AA-964, card CRM-33a: A-18 "AI", A-21 AI draft).

=============================================  =============================================  ======================
Route                                          What                                           Who
=============================================  =============================================  ======================
``GET    /settings/ai``                        AI on / off, monthly budget, this month's      every role
                                               spend and alert level, test mode and
                                               whether the privacy check is billed
``PUT    /settings/ai``                        switch AI on / off, set the budget (<= plan)   owner
``POST   /trips/{id}/ai/draft``                one AI draft for the trip (behind the plan's   every role
                                               ai_assist and ai_enabled; a suggestion, never
                                               sent from here; stored as a crm.ai_draft row)
``POST   /ai/drafts/{id}/accept``              accept a pending draft (+ feedback code)       owner, agent;
                                                                                              assistant on
                                                                                              assigned trips
``POST   /ai/drafts/{id}/discard``             discard a pending draft (+ feedback code)      every role
``GET    /ai/voices``                          AA-965 R41: the agency's voices, the limit     every role
``POST   /ai/voices``                          make a voice (fixed codes; at most the         every role
                                               per-member setting)
``PATCH  /ai/voices/{id}``                     change a voice                                 its member, owner
``DELETE /ai/voices/{id}``                     delete a voice (hard delete)                   its member, owner
``POST   /trips/{id}/ai/supplier-confirmation``  AA-965 R10: read one ancillary supplier's      every role
                                               confirmation into a proposed line update
                                               (nothing written but the draft row)
``POST   /ai/drafts/{id}/apply``               write the agent's confirmed line updates of    owner, agent;
                                               a supplier confirmation draft, accept it       assistant on
                                                                                              assigned trips
``POST   /proposal-days/{id}/ai/options``      AA-965 R40: AA options for one day from the    every role
                                               trip's notes, from the day's closed list only
=============================================  =============================================  ======================

The services are :mod:`app.llm.agency_ai` and :mod:`app.llm.ai_drafts` (AA-1027 F3); every model call goes through
the process's :class:`app.llm.gateway.LlmGateway` (``app.state.llm_gateway``).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app import platform_settings as ps
from app.auth.identity_config import AGENCY
from app.auth.route_guard import Caller, class_router, current_caller
from app.booking.client import BookingRefusal, BookingUnavailable
from app.booking.contract import ContractError
from app.booking.products import _booking_error
from app.db import request_pool
from app.errors import Unavailable
from app.llm import agency_ai, ai_drafts, notes_options, structured, supplier_confirm, voices
from app.llm.budget_limits import budget_ceiling
from app.permissions import Action
from app.privacy.guardrails import guardrail_billed
from app.routers.agency import tenant_header
from app.tenancy import TenantContext, tenant_tx
from app.vault import Vault
from app.vault.provider import require_keys

router = class_router(AGENCY, tags=["ai"])

CallerDep = Annotated[Caller, Depends(current_caller)]
TenantDep = Annotated[uuid.UUID, Depends(tenant_header)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


FeedbackCode = Literal["good", "wrong_facts", "wrong_tone", "not_relevant", "too_long", "other"]
# Request body bounds (AA-965): technical caps above the highest value the admin settings allow; the settings
# (crm.ai.*_max_chars) are the policy and are checked in the services.
SOURCE_TEXT_BOUND = 60_000  # technical-constant: = crm.ai.supplier_confirm_max_chars max_value (crm-077)
NOTES_TEXT_BOUND = 30_000  # technical-constant: = crm.ai.notes_options_max_chars max_value (crm-077)
APPLY_LINES_BOUND = 50  # technical-constant: = crm.ai.supplier_confirm_max_lines max_value (crm-077)


class AiSettingsIn(_Body):
    ai_enabled: bool | None = None
    ai_budget_usd: Decimal | None = Field(default=None, ge=0, le=10_000, decimal_places=2)


class DraftIn(_Body):
    kind: Literal["reply", "follow_up"] = "reply"
    text: str = Field(min_length=1, max_length=agency_ai.MAX_TEXT)
    instruction: str | None = Field(default=None, max_length=agency_ai.MAX_INSTRUCTION)
    voice_profile_id: uuid.UUID | None = None  # AA-965 R41


class VoiceIn(_Body):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    locale: Literal["en", "vi"] | None = None
    channel: Literal["email", "whatsapp"] | None = None
    descriptors: dict[str, str] | None = None


class SupplierConfirmIn(_Body):
    agency_supplier_id: uuid.UUID
    message_id: uuid.UUID | None = None
    text: str | None = Field(default=None, max_length=SOURCE_TEXT_BOUND)


class ApplyLineIn(_Body):
    booking_line_id: uuid.UUID
    action: Literal["confirm", "back_to_planning"]
    confirmation_no: str | None = Field(default=None, min_length=1, max_length=80)


class ApplyIn(_Body):
    lines: list[ApplyLineIn] = Field(min_length=1, max_length=APPLY_LINES_BOUND)
    feedback_code: FeedbackCode | None = None


class NotesOptionsIn(_Body):
    notes: str | None = Field(default=None, max_length=NOTES_TEXT_BOUND)


class DecisionIn(_Body):
    feedback_code: Literal["good", "wrong_facts", "wrong_tone", "not_relevant", "too_long", "other"] | None = None


async def _limits(request: Request, ctx: TenantContext) -> dict[str, Any]:
    """AA-1096 L4: the ceiling (this agency's own or the platform's) and the AI rate are admin-editable settings."""
    settings = request.app.state.settings
    return {
        "budget_max": await budget_ceiling(ctx),
        "provider": settings.crm_llm_provider,
        "rate_per_minute": int(ps.get(ps.RATE_AI)),
        "scrub_billed": guardrail_billed(settings),
    }


@router.get("/settings/ai")
async def get_ai_settings(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_USE) as ctx:
        return await agency_ai.ai_settings(ctx, **await _limits(request, ctx))


@router.put("/settings/ai")
async def put_ai_settings(
    request: Request, caller: CallerDep, tenant_id: TenantDep, body: AiSettingsIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_SETTINGS) as ctx:
        return await agency_ai.update_ai_settings(
            ctx, body.model_dump(exclude_unset=True), **await _limits(request, ctx)
        )


@router.post("/trips/{trip_id}/ai/draft")
async def post_ai_draft(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: DraftIn
) -> dict[str, Any]:
    keys = require_keys(request.app.state.keys)
    pool = await request_pool(request)
    # A long slot (app.tenant_gate): the scrub and the model call run while the slot is held (AA-969 round 3).
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_USE, long=True) as ctx:
        return await agency_ai.draft(
            ctx,
            request.app.state.llm_gateway,
            Vault(keys, ctx),
            trip_id,
            kind=body.kind,
            text=body.text,
            instruction=body.instruction,
            voice_profile_id=body.voice_profile_id,
        )


@router.post("/ai/drafts/{draft_id}/{decision}")
async def post_ai_draft_decision(
    request: Request,
    caller: CallerDep,
    tenant_id: TenantDep,
    draft_id: uuid.UUID,
    decision: Literal["accept", "discard"],
    body: DecisionIn | None = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_USE) as ctx:
        return await ai_drafts.decide(
            ctx, draft_id, action=decision, feedback_code=body.feedback_code if body else None
        )


# ── AA-965 R41: voice profiles ───────────────────────────────────────────────────────────────────
@router.get("/ai/voices")
async def get_voices(request: Request, caller: CallerDep, tenant_id: TenantDep) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_USE) as ctx:
        return await voices.list_voices(ctx)


@router.post("/ai/voices", status_code=201)
async def post_voice(request: Request, caller: CallerDep, tenant_id: TenantDep, body: VoiceIn) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_USE) as ctx:
        return await voices.create(ctx, body.model_dump(exclude_unset=True))


@router.patch("/ai/voices/{voice_id}")
async def patch_voice(
    request: Request, caller: CallerDep, tenant_id: TenantDep, voice_id: uuid.UUID, body: VoiceIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_USE) as ctx:
        return await voices.update(ctx, voice_id, body.model_dump(exclude_unset=True))


@router.delete("/ai/voices/{voice_id}")
async def delete_voice(request: Request, caller: CallerDep, tenant_id: TenantDep, voice_id: uuid.UUID) -> dict:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_USE) as ctx:
        return await voices.delete(ctx, voice_id)


# ── AA-965 R10: supplier confirmations ───────────────────────────────────────────────────────────
@router.post("/trips/{trip_id}/ai/supplier-confirmation")
async def post_supplier_confirmation(
    request: Request, caller: CallerDep, tenant_id: TenantDep, trip_id: uuid.UUID, body: SupplierConfirmIn
) -> dict[str, Any]:
    keys = require_keys(request.app.state.keys)
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_USE, long=True) as ctx:
        return await supplier_confirm.run(
            ctx,
            request.app.state.llm_gateway,
            Vault(keys, ctx),
            trip_id,
            agency_supplier_id=body.agency_supplier_id,
            message_id=body.message_id,
            text=body.text,
        )


@router.post("/ai/drafts/{draft_id}/apply")
async def post_ai_draft_apply(
    request: Request, caller: CallerDep, tenant_id: TenantDep, draft_id: uuid.UUID, body: ApplyIn
) -> dict[str, Any]:
    pool = await request_pool(request)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_USE) as ctx:
        return await supplier_confirm.apply(
            ctx, draft_id, [line.model_dump() for line in body.lines], feedback_code=body.feedback_code
        )


# ── AA-965 R40: notes -> AA options ──────────────────────────────────────────────────────────────
async def _live_products(request: Request, tenant_id: uuid.UUID) -> dict[str, Any]:
    """The products Booking sells this agency (sellable only, kept per agency by the process's cache). Read outside
    any transaction (no connection held while Booking answers)."""
    cache = getattr(request.app.state, "aa_products", None)
    if cache is None:
        raise Unavailable("booking_not_configured", "AA products are not connected on this environment")
    try:
        return dict(await cache.products(tenant_id))
    except (BookingUnavailable, BookingRefusal, ContractError) as exc:
        raise _booking_error(exc) from None


@router.post("/proposal-days/{day_id}/ai/options")
async def post_notes_options(
    request: Request, caller: CallerDep, tenant_id: TenantDep, day_id: uuid.UUID, body: NotesOptionsIn | None = None
) -> dict[str, Any]:
    keys = require_keys(request.app.state.keys)
    pool = await request_pool(request)
    # 1. who, which day, the plan and the switch: before Booking is asked or anything is scrubbed or paid
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_USE) as ctx:
        day = await notes_options.day_of(ctx, day_id)
        await structured.preflight(ctx, day["trip_id"])
    # 2. the agency's live AA products (Booking), outside the transaction
    products = await _live_products(request, tenant_id)
    # 3. the run (a long slot: the scrub and the model call run while it is held)
    async with tenant_tx(pool, caller, tenant_id, action=Action.AI_USE, long=True) as ctx:
        day = await notes_options.day_of(ctx, day_id)
        destinations = await structured.preflight(ctx, day["trip_id"])
        return await notes_options.run(
            ctx,
            request.app.state.llm_gateway,
            Vault(keys, ctx),
            day,
            products,
            destinations=destinations,
            notes=body.notes if body else None,
        )
