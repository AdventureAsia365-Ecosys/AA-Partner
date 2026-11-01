"""``/crm/staff/v1``: AA staff routes (route class STAFF; root ADR 0006b decision 6). AA-971 (card CRM-13b):
owner-granted AA support (L2) and break-glass (L3), design v3 §4.8.

WORKFORCE ``crm-staff`` client only, groups ``crm-ops`` or ``crm-support`` (the class guard). Then:

==========================================  =============================================================  ===========
Route                                       What                                                           Group
==========================================  =============================================================  ===========
``GET  /support/agencies``                  agencies whose owner allows AA support now                     crm-support
``POST /support/sessions``                  start a support session (ticket; inside the owner's grant)     crm-support
``POST /support/sessions/end``              end one's own support session                                  any
``GET  /breakglass/agencies``               AA-1019: the agencies break-glass may open (picker: id, name,  crm-ops
                                            status, the caller's live session)
``POST /breakglass/sessions``               break-glass: reason code, ticket, 15-60 minutes; owners mailed  crm-ops
                                            and AA ops alerted
``POST /breakglass/sessions/end``           end one's own break-glass session                              any
``GET  /agencies/{id}/session``             one's own sessions in that agency                              any
``GET  /agencies/{id}/overview``            the agency, its settings codes, trips per stage                live session
``GET  /agencies/{id}/trips``               trips (no title), newest activity first, keyset paging         live session
``GET  /agencies/{id}/trips/{trip_id}``     one trip, its stage history; the party's names only when the   live session
                                            support session shows identity (never break-glass)
==========================================  =============================================================  ===========

Read only by construction: the only writes here are the caller's own session rows (start / end); every agency read
runs as ``crm_support_reader`` (no write privilege, crm-033). There is no export, send, role, integration or settings
route on this class (card R test: support export / send -> 403, ``tests/unit/test_support_routes.py``). Responses are
ids, codes, dates, the agency's name and, with identity shown, travellers' display names; every response model forbids
extra keys.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import GROUP_CRM_OPS, GROUP_CRM_SUPPORT, STAFF
from app.auth.route_guard import Caller, class_router, current_caller, require_group
from app.db import request_pool
from app.support import reads, sessions
from app.vault.provider import require_keys

router = class_router(STAFF, tags=["staff"])

Staff = Annotated[Caller, Depends(current_caller)]
Support = Annotated[Caller, Depends(require_group(GROUP_CRM_SUPPORT))]
Ops = Annotated[Caller, Depends(require_group(GROUP_CRM_OPS))]
# The crm.ops.v1 ticket pattern (a support ticket or incident id: "SUP-1042", "INC-7"); crm-033 CHECKs it too.
TICKET_PATTERN = r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,10}$"
# The same list as app.members.mail.BREAKGLASS_REASONS and the crm-033 CHECK (tests/unit/test_support_routes.py).
BreakglassReason = Literal[
    "security_incident", "data_corruption", "legal_request", "owner_unreachable", "service_outage"
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SupportStartIn(_In):
    tenant_id: uuid.UUID
    ticket_ref: str = Field(pattern=TICKET_PATTERN)


class BreakglassStartIn(_In):
    tenant_id: uuid.UUID
    ticket_ref: str = Field(pattern=TICKET_PATTERN)
    reason_code: BreakglassReason
    minutes: int = Field(default=60, ge=sessions.BREAKGLASS_MINUTES_MIN, le=sessions.BREAKGLASS_MINUTES_MAX)


class EndIn(_In):
    tenant_id: uuid.UUID


class SessionOut(_Out):
    tenant_id: str
    session_id: str
    kind: Literal["support", "breakglass"]
    status: Literal["active", "ended", "revoked", "expired"]
    ticket_ref: str
    reason_code: str | None
    show_identity: bool
    started_at: str
    expires_at: str
    ended_at: str | None = None
    # AA-1019 D1: why a session that is no longer live is over (sessions.END_REASONS); None while it is live.
    end_reason: Literal["staff_ended", "owner_ended", "session_expired", "grant_expired"] | None = None
    owners_notified: int | None = None


class GrantedAgency(_Out):
    tenant_id: str
    agency_name: str
    expires_at: str
    show_identity: bool
    in_use: bool
    mine: bool


class GrantedAgenciesOut(_Out):
    items: list[GrantedAgency]


class BreakglassAgency(_Out):
    tenant_id: str
    agency_name: str | None
    agency_status: Literal["active", "locked"]
    my_session_expires_at: str | None


class BreakglassAgenciesOut(_Out):
    items: list[BreakglassAgency]


class EndOut(_Out):
    tenant_id: str
    ended: list[str]


class MySessionsOut(_Out):
    tenant_id: str
    agency_name: str | None
    agency_status: str | None
    sessions: list[SessionOut]


class SessionRef(_Out):
    kind: Literal["support", "breakglass"]
    session_id: str
    show_identity: bool
    expires_at: str


class SettingsOut(_Out):
    ai_enabled: bool
    ai_budget_usd: str
    timezone: str
    base_currency: str
    booking_validation_level: str
    fx_mode: str
    trash_days: int
    auto_archive_months: int
    stage2_auto_send: bool
    invoice_due_days: int
    service_fee_enabled: bool
    onboarding_later: list[str]
    onboarding_done: list[str]


class OverviewOut(_Out):
    tenant_id: str
    agency_name: str
    status: str
    default_locale: str
    settings: SettingsOut | None
    pipeline: dict[str, int]
    session: SessionRef


class TripOut(_Out):
    trip_id: str
    stage: str
    stage_entered_at: str | None
    waiting_on: str
    start_date: str | None
    end_date: str | None
    month: str | None
    nights: int | None
    destinations: list[str]
    party_adults: int
    party_children: int
    party_type: str | None
    owner_member_id: str | None
    lead_source: str | None
    lost_reason_code: str | None
    cancel_reason_code: str | None
    last_inquiry_at: str | None
    last_activity_at: str | None
    created_at: str | None


class TripsOut(_Out):
    items: list[TripOut]
    next_cursor: str | None
    session: SessionRef


class StageMove(_Out):
    history_id: str
    from_stage: str | None
    to_stage: str
    reason_code: str
    override: bool
    override_reason_code: str | None
    at: str | None
    by: str


class PartyMember(_Out):
    traveler_id: str
    role: str
    display_name: str


class TripDetailOut(TripOut):
    stage_history: list[StageMove]
    party: list[PartyMember] | None
    identity_hidden: bool
    session: SessionRef


# ── Sessions ─────────────────────────────────────────────────────────────────────────────────────
@router.get("/support/agencies", response_model=GrantedAgenciesOut)
async def granted_agencies(request: Request, caller: Support) -> dict[str, Any]:
    return await sessions.granted_agencies(await request_pool(request), caller)


@router.post("/support/sessions", response_model=SessionOut, status_code=201)
async def start_support(request: Request, caller: Support, body: SupportStartIn) -> dict[str, Any]:
    pool = await request_pool(request)
    return await sessions.start_support(pool, caller, body.tenant_id, ticket_ref=body.ticket_ref)


@router.post("/support/sessions/end", response_model=EndOut)
async def end_support(request: Request, caller: Staff, body: EndIn) -> dict[str, Any]:
    return await sessions.end_session(await request_pool(request), caller, body.tenant_id, kind="support")


@router.get("/breakglass/agencies", response_model=BreakglassAgenciesOut)
async def breakglass_agencies(request: Request, caller: Ops) -> dict[str, Any]:
    return await sessions.breakglass_agencies(await request_pool(request), caller)


@router.post("/breakglass/sessions", response_model=SessionOut, status_code=201)
async def start_breakglass(request: Request, caller: Ops, body: BreakglassStartIn) -> dict[str, Any]:
    pool = await request_pool(request)
    return await sessions.start_breakglass(
        pool,
        caller,
        body.tenant_id,
        ticket_ref=body.ticket_ref,
        reason_code=body.reason_code,
        minutes=body.minutes,
        directory=request.app.state.directory,
        mailer=request.app.state.mailer,
        web_origin=request.app.state.settings.invitation_origin,
    )


@router.post("/breakglass/sessions/end", response_model=EndOut)
async def end_breakglass(request: Request, caller: Staff, body: EndIn) -> dict[str, Any]:
    return await sessions.end_session(await request_pool(request), caller, body.tenant_id, kind="breakglass")


@router.get("/agencies/{tenant_id}/session", response_model=MySessionsOut)
async def my_sessions(request: Request, caller: Staff, tenant_id: uuid.UUID) -> dict[str, Any]:
    return await sessions.my_sessions(await request_pool(request), caller, tenant_id)


# ── Reads inside a live session ──────────────────────────────────────────────────────────────────
@router.get("/agencies/{tenant_id}/overview", response_model=OverviewOut)
async def agency_overview(request: Request, caller: Staff, tenant_id: uuid.UUID) -> dict[str, Any]:
    return await reads.overview(await request_pool(request), caller, tenant_id)


@router.get("/agencies/{tenant_id}/trips", response_model=TripsOut)
async def agency_trips(
    request: Request,
    caller: Staff,
    tenant_id: uuid.UUID,
    stage: Annotated[str | None, Query(max_length=20)] = None,
    cursor: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=reads.MAX_PAGE)] = 50,
) -> dict[str, Any]:
    pool = await request_pool(request)
    return await reads.trips(pool, caller, tenant_id, stage=stage, cursor=cursor, limit=limit)


@router.get("/agencies/{tenant_id}/trips/{trip_id}", response_model=TripDetailOut)
async def agency_trip(request: Request, caller: Staff, tenant_id: uuid.UUID, trip_id: uuid.UUID) -> dict[str, Any]:
    keys = request.app.state.keys
    if keys is not None:
        keys = require_keys(keys)
    return await reads.trip(await request_pool(request), caller, tenant_id, trip_id, keys=keys)
