"""``/crm/ops/v1``: the L1 platform console contract ``crm.ops.v1`` (route class OPS; root ``docs/contracts``).

WORKFORCE ``cis-admin`` tokens only (the CIS admin API relays the signed-in staff member's own access token); groups
``cis-admin`` or ``crm-platform-admin`` read, ``crm-platform-admin`` writes (``require_group``). Every response passes
the L1 allow-list (:mod:`app.ops.serializer`, card CRM-13a R: no trip, pipeline, revenue or personal-data key) and a
response model with ``extra='forbid'``. Ids, states, flags and meters only; every agency call is written into that
agency's own audit (actor ``platform:<staff sub>``, kind ``aa_ops``; reads as ``ops_read``, writes with the ticket
reference as ``ticket:<REF>``).

AA-936 (card CRM-08, what CIS #975 reads): ``GET /jobs/summary`` (``crm.job`` per kind), ``GET /llm/usage``.
AA-992 (crm.ops.v1 1.4, the CIS admin page /admin/data-coverage): ``GET /data-coverage``, platform-wide counts only
(agencies and members per status, the AA catalog cache); no per-agency audit event (:func:`console.platform_coverage`).
AA-939 (card CRM-11, Q1 6.3): members, member deactivation, owner recovery open / complete.
AA-941 (card CRM-13a, design S-01), :mod:`app.ops.console`:

=====  ==========================================  =============================================================
GET    ``/console``                                ``{can_write}``: the caller is in ``crm-platform-admin``
GET    ``/tenants?cursor=&limit=``                 agency list (TenantSummary page)
POST   ``/tenants``                                create agency (201; an existing id is 409 tenant_exists); no member
GET    ``/tenants/{id}``                           one TenantSummary
GET    ``/tenants/{id}/usage``                     usage meters
POST   ``/tenants/{id}/lock`` / ``unlock``         lock / unlock (ticket, lock reason)
POST   ``/tenants/{id}/owner-invite``              first-owner invitation (no owner, no travellers), 202
GET    ``/tenants/{id}/entitlements``              plan, seats, features allowed, insights opt-out
PUT    ``/tenants/{id}/entitlements-allowed``      allow features (the owner switches them on)
PUT    ``/tenants/{id}/insights-opt-out``          insights opt-out on the owner's written request (Q2 3.12)
GET    ``/tenants/{id}/data-requests``             latest agency export / deletion requests
POST   ``/tenants/{id}/data-requests``             raise one the owner confirms, 202
GET    ``/tenants/{id}/jobs``                      failed jobs of the agency (7 days)
POST   ``/tenants/{id}/jobs/{job_id}/retry``       re-queue one, 202
POST   ``/tenants/{id}/studio-invitations``         AA-979 P2b: deliver a CIS Content studio invitation, 202
=====  ==========================================  =============================================================

AA-979 (crm.ops.v1 1.3): ``POST /tenants/{id}/studio-invitations`` ``{email, cis_invitation_id, locale}`` — the CIS
staff relay (Tenant 360 Members invite / send again) hands over the address of an invitation CIS created; the CRM sends
the AA system mail and owns the claim (:mod:`app.members.studio`). ``{id}`` is the CIS tenant, which may have no CRM
agency. Writers: any OPS caller, ``cis-admin`` included (orchestrator decision Q4: the CIS route already requires
``tenant.write``, and CIS decides the Content studio role; this call grants nothing in the CRM). The address is never
echoed, logged or stored in clear.

AA-979 / root ADR 0006a A10b-1 (S252 security review): the Dev-only E2E CIS admin (a ``cis-admin`` token whose
``email`` claim is the A10b address, ``Caller.e2e_read_only``, :mod:`app.auth.e2e_staff`) is read-only here: every
``crm-platform-admin`` write refuses it with 403 ``e2e_read_only`` (:func:`require_group`), ``/console`` says
``can_write: false``, and the studio-invitation relay takes it only on the terms of
:func:`app.members.studio.check_e2e_invitation` (synthetic CRM agency, E2E mailbox address, an ``invited`` ``viewer`` /
``editor`` membership at CIS in an answer that marks the tenant synthetic, contract 1.2 ``is_synthetic``; 403
``e2e_read_only`` otherwise, 503 ``cis_unavailable`` when CIS cannot answer).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.identity_config import GROUP_CRM_PLATFORM_ADMIN, OPS
from app.auth.route_guard import Caller, class_router, current_caller, require_group
from app.db import request_pool
from app.jobs import queue
from app.llm.usage import MAX_DAYS, llm_usage
from app.members import recovery, studio
from app.members.mail import require_mailer
from app.ops import console
from app.vault.provider import require_keys

router = class_router(OPS, tags=["ops"])

PlatformAdmin = Annotated[Caller, Depends(require_group(GROUP_CRM_PLATFORM_ADMIN))]
Reader = Annotated[Caller, Depends(current_caller)]
# A support ticket or Linear id ("AA-941", "SUP-1234"): it goes into the agency audit as the object id ticket:<REF>.
TICKET_PATTERN = r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,10}$"
TicketRef = Annotated[str, Field(pattern=TICKET_PATTERN)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Response models (crm.ops.v1) ─────────────────────────────────────────────────────────────────
class ConsoleOut(_Out):
    can_write: bool


TenantState = Literal["active", "locked", "offboarding", "offboarded"]


LockSource = Literal["aa_ops", "cis_tenant", "cis_membership"]
FeedState = Literal["ok", "pending", "stale", "failing", "unknown"]


class TenantSummary(_Out):
    tenant_id: str
    agency_name: str
    state: TenantState
    is_synthetic: bool
    has_owner: bool
    holds_travellers: bool
    created_at: str
    # crm.ops.v1 1.2 (AA-978 follow-up): additive and optional for a 1.1 reader.
    lock_source: LockSource | None = None
    membership_feed_state: FeedState = "unknown"
    membership_feed_lag_s: int | None = None


class TenantPage(_Out):
    items: list[TenantSummary]
    next_cursor: str | None


class UsageMeters(_Out):
    tenant_id: str
    period_start: str
    period_end: str
    seats_used: int
    seats_allowed: int
    active_members_30d: int
    storage_bytes: int
    inbound_mail_30d: int
    ai_cost_usd_month: str
    ai_budget_usd_month: str
    failed_jobs_24h: int
    last_activity_at: str | None


class OwnerInviteAccepted(_Out):
    tenant_id: str
    invitation_id: str
    status: Literal["pending"]
    expires_at: str


Feature = Literal["ai_assist", "whatsapp_sms", "stripe_connect", "campaigns", "custom_domain", "insights_export"]


class Entitlements(_Out):
    plan: Literal["free", "core", "pro"]
    seats_allowed: int
    features_allowed: list[Feature]
    insights_opt_out: bool


class InsightsOptOutOut(_Out):
    opt_out: bool


class DataRequest(_Out):
    request_id: str
    tenant_id: str
    kind: Literal["agency_export", "agency_deletion"]
    status: Literal[
        "awaiting_owner_confirmation", "confirmed", "export_downloaded", "rejected", "cancelled", "failed", "done"
    ]
    raised_at: str


class DataRequestRaised(DataRequest):
    owners_notified: int


class DataRequestList(_Out):
    items: list[DataRequest]


class FailedJob(_Out):
    job_id: str
    kind: str
    attempt: int
    max_attempts: int
    error_class: str | None
    finished_at: str | None


class FailedJobList(_Out):
    items: list[FailedJob]


class JobRetried(_Out):
    job_id: str
    status: Literal["queued"]


class MemberRow(_Out):
    member_id: str
    role: Literal["owner", "agent", "assistant"]
    status: str
    joined_at: str | None


class PendingRecovery(_Out):
    request_id: str
    eligible_at: str | None


class MemberList(_Out):
    members: list[MemberRow]
    active_owners: int
    recovery: PendingRecovery | None


class MemberDeactivated(_Out):
    member_id: str
    status: Literal["disabled"]
    signed_out: bool
    active_owners: int


class RecoveryOpened(_Out):
    request_id: str
    status: Literal["pending"]
    eligible_at: str | None
    owners_notified: int


class RecoveryCompleted(_Out):
    request_id: str
    status: Literal["completed"]
    owner_member_id: str


class JobKindRow(_Out):
    kind: str
    queued: int
    running: int
    failed_24h: int
    oldest_queued_age_s: int | None


class JobSummary(_Out):
    generated_at: str
    kinds: list[JobKindRow]


class LlmUsageRow(_Out):
    day: str
    stage: str
    model: str
    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: str


class LlmUsage(_Out):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    rows: list[LlmUsageRow]


# crm.ops.v1 1.4 (AA-992): platform-wide counts for the CIS admin page /admin/data-coverage. Counts only.
class AgencyCounts(_Out):
    active: int
    locked: int
    offboarding: int
    offboarded: int
    synthetic: int


class MemberCounts(_Out):
    active: int
    invited: int
    disabled: int


class CatalogCacheCounts(_Out):
    served: int
    paused: int
    retired: int
    last_full_pass_at: str | None
    last_ok_at: str | None
    failures: int
    last_error_class: str | None


class DataCoverage(_Out):
    as_of: str
    agencies: AgencyCounts
    members: MemberCounts
    catalog_cache: CatalogCacheCounts


# ── Request bodies ───────────────────────────────────────────────────────────────────────────────
class TenantCreate(_In):
    tenant_id: uuid.UUID
    agency_name: str = Field(min_length=1, max_length=200)
    is_synthetic: bool
    default_locale: Literal["en-AU", "en-GB", "en-US", "vi-VN"] = "en-AU"
    ticket_ref: TicketRef


class LockIn(_In):
    ticket_ref: TicketRef
    reason_code: Literal["owner_request", "payment", "abuse", "security", "test"]


class TicketIn(_In):
    ticket_ref: TicketRef


class OptionalTicketIn(_In):
    ticket_ref: TicketRef | None = None


class OwnerInviteIn(_In):
    # Write-only (x-pii in crm.ops.v1): validated by app.members.team.normalise_email, so a refusal never echoes it.
    owner_email: str = Field(min_length=3, max_length=254)
    ticket_ref: TicketRef


class AllowedIn(_In):
    features_allowed: list[str] = Field(max_length=6)
    ticket_ref: TicketRef


class InsightsOptOutIn(_In):
    opt_out: bool
    owner_request_ref: TicketRef


class DataRequestIn(_In):
    kind: Literal["agency_export", "agency_deletion"]
    ticket_ref: TicketRef


class RecoveryIn(_In):
    candidate_member_id: uuid.UUID
    reason: Literal["owner_left", "owner_unreachable", "no_owner"]
    ticket_ref: TicketRef


# ── AA-936: what CIS #975 reads ──────────────────────────────────────────────────────────────────
@router.get("/jobs/summary", response_model=JobSummary)
async def jobs_summary(request: Request) -> dict:
    pool = await request_pool(request)
    return {"generated_at": datetime.now(UTC).isoformat(), "kinds": await queue.kind_summary(pool)}


@router.get("/llm/usage", response_model=LlmUsage)
async def get_llm_usage(
    request: Request,
    from_: Annotated[date, Query(alias="from")],
    to: Annotated[date, Query()],
) -> dict:
    if to < from_ or (to - from_).days > MAX_DAYS:
        raise HTTPException(status_code=422, detail="invalid_range")
    pool = await request_pool(request)
    return {"from": from_.isoformat(), "to": to.isoformat(), "rows": await llm_usage(pool, from_, to)}


# ── AA-992: what the CIS admin page /admin/data-coverage reads (crm.ops.v1 1.4) ───────────────────────────────
@router.get("/data-coverage", response_model=DataCoverage)
async def data_coverage(request: Request, caller: Reader) -> dict:
    pool = await request_pool(request)
    return await console.platform_coverage(pool, staff_sub=caller.sub)


# ── AA-941: the console ──────────────────────────────────────────────────────────────────────────
@router.get("/console", response_model=ConsoleOut)
async def get_console(caller: Reader) -> dict:
    """What the S-01 page may offer: write buttons only for ``crm-platform-admin`` (never the read-only Dev E2E CIS
    admin, root ADR 0006a A10b). No database access."""
    return {"can_write": caller.in_group(GROUP_CRM_PLATFORM_ADMIN) and not caller.e2e_read_only}


@router.get("/tenants", response_model=TenantPage)
async def list_tenants(
    request: Request,
    caller: Reader,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=console.MAX_PAGE)] = 50,
) -> dict:
    pool = await request_pool(request)
    return await console.list_agencies(pool, staff_sub=caller.sub, cursor=cursor, limit=limit)


@router.post("/tenants", response_model=TenantSummary, status_code=201)
async def create_tenant(request: Request, body: TenantCreate, caller: PlatformAdmin) -> dict:
    pool = await request_pool(request)
    return await console.create_agency(
        pool,
        tenant_id=body.tenant_id,
        agency_name=body.agency_name,
        is_synthetic=body.is_synthetic,
        default_locale=body.default_locale,
        environment=request.app.state.settings.environment,
        staff_sub=caller.sub,
        ticket_ref=body.ticket_ref,
    )


@router.get("/tenants/{tenant_id}", response_model=TenantSummary)
async def get_tenant(request: Request, tenant_id: uuid.UUID, caller: Reader) -> dict:
    pool = await request_pool(request)
    return await console.agency_summary(pool, tenant_id, staff_sub=caller.sub)


@router.get("/tenants/{tenant_id}/usage", response_model=UsageMeters)
async def get_usage(request: Request, tenant_id: uuid.UUID, caller: Reader) -> dict:
    pool = await request_pool(request)
    return await console.agency_usage(pool, tenant_id, staff_sub=caller.sub)


@router.post("/tenants/{tenant_id}/lock", response_model=TenantSummary)
async def lock_tenant(request: Request, tenant_id: uuid.UUID, body: LockIn, caller: PlatformAdmin) -> dict:
    pool = await request_pool(request)
    return await console.lock_agency(
        pool, tenant_id, staff_sub=caller.sub, reason_code=body.reason_code, ticket_ref=body.ticket_ref
    )


@router.post("/tenants/{tenant_id}/unlock", response_model=TenantSummary)
async def unlock_tenant(request: Request, tenant_id: uuid.UUID, body: TicketIn, caller: PlatformAdmin) -> dict:
    pool = await request_pool(request)
    return await console.unlock_agency(pool, tenant_id, staff_sub=caller.sub, ticket_ref=body.ticket_ref)


class StudioInvitationIn(_In):
    email: str = Field(min_length=3, max_length=254)
    cis_invitation_id: uuid.UUID
    locale: Literal["en", "vi"] = "en"


class StudioInvitationAccepted(_Out):
    invitation_id: str
    tenant_id: str
    status: Literal["pending"]
    expires_at: str
    reissued: bool


@router.post("/tenants/{tenant_id}/studio-invitations", response_model=StudioInvitationAccepted, status_code=202)
async def studio_invitation(request: Request, tenant_id: uuid.UUID, body: StudioInvitationIn, caller: Reader) -> dict:
    """AA-979: deliver a CIS Content studio invitation (``tenant_id`` = the CIS tenant; no CRM agency needed).

    The Dev-only E2E CIS admin (root ADR 0006a A10b-1) only on the terms of :func:`studio.check_e2e_invitation`
    (synthetic CRM agency, E2E mailbox address, an invited viewer / editor membership at CIS, which also marks the
    tenant synthetic)."""
    pool = await request_pool(request)
    settings = request.app.state.settings
    if caller.e2e_read_only:
        await studio.check_e2e_invitation(
            pool,
            environment=settings.environment,
            cis_tenant_id=tenant_id,
            cis_invitation_id=body.cis_invitation_id,
            email=body.email,
            cis=request.app.state.studio_activation,
        )
    return await studio.issue(
        pool,
        cis_tenant_id=tenant_id,
        cis_invitation_id=body.cis_invitation_id,
        email=body.email,
        locale=body.locale,
        staff_sub=caller.sub,
        mailer=require_mailer(request.app.state.mailer),
        web_origin=settings.invitation_origin,
        ttl_hours=settings.invitation_ttl_hours,
    )


@router.post("/tenants/{tenant_id}/owner-invite", response_model=OwnerInviteAccepted, status_code=202)
async def owner_invite(request: Request, tenant_id: uuid.UUID, body: OwnerInviteIn, caller: PlatformAdmin) -> dict:
    pool = await request_pool(request)
    settings = request.app.state.settings
    return await console.invite_first_owner(
        pool,
        tenant_id,
        staff_sub=caller.sub,
        owner_email=body.owner_email,
        ticket_ref=body.ticket_ref,
        keys=require_keys(request.app.state.keys),
        mailer=require_mailer(request.app.state.mailer),
        web_origin=settings.invitation_origin,
        ttl_hours=settings.invitation_ttl_hours,
    )


@router.get("/tenants/{tenant_id}/entitlements", response_model=Entitlements)
async def get_entitlements(request: Request, tenant_id: uuid.UUID, caller: Reader) -> dict:
    pool = await request_pool(request)
    return await console.agency_entitlements(pool, tenant_id, staff_sub=caller.sub)


@router.put("/tenants/{tenant_id}/entitlements-allowed", response_model=Entitlements)
async def put_entitlements_allowed(
    request: Request, tenant_id: uuid.UUID, body: AllowedIn, caller: PlatformAdmin
) -> dict:
    pool = await request_pool(request)
    return await console.allow_features(
        pool, tenant_id, staff_sub=caller.sub, features=body.features_allowed, ticket_ref=body.ticket_ref
    )


@router.put("/tenants/{tenant_id}/insights-opt-out", response_model=InsightsOptOutOut)
async def put_insights_opt_out(
    request: Request, tenant_id: uuid.UUID, body: InsightsOptOutIn, caller: PlatformAdmin
) -> dict:
    pool = await request_pool(request)
    return await console.set_insights_opt_out(
        pool, tenant_id, staff_sub=caller.sub, opt_out=body.opt_out, owner_request_ref=body.owner_request_ref
    )


@router.get("/tenants/{tenant_id}/data-requests", response_model=DataRequestList)
async def get_data_requests(request: Request, tenant_id: uuid.UUID, caller: Reader) -> dict:
    pool = await request_pool(request)
    return await console.agency_data_requests(pool, tenant_id, staff_sub=caller.sub)


@router.post("/tenants/{tenant_id}/data-requests", response_model=DataRequestRaised, status_code=202)
async def post_data_request(request: Request, tenant_id: uuid.UUID, body: DataRequestIn, caller: PlatformAdmin) -> dict:
    pool = await request_pool(request)
    return await console.raise_data_request(
        pool,
        tenant_id,
        staff_sub=caller.sub,
        kind=body.kind,
        ticket_ref=body.ticket_ref,
        directory=request.app.state.directory,
        mailer=request.app.state.mailer,
        web_origin=request.app.state.settings.invitation_origin,
    )


@router.get("/tenants/{tenant_id}/jobs", response_model=FailedJobList)
async def get_jobs(request: Request, tenant_id: uuid.UUID, caller: Reader) -> dict:
    pool = await request_pool(request)
    return await console.agency_failed_jobs(pool, tenant_id, staff_sub=caller.sub)


@router.post("/tenants/{tenant_id}/jobs/{job_id}/retry", response_model=JobRetried, status_code=202)
async def retry_job(
    request: Request, tenant_id: uuid.UUID, job_id: uuid.UUID, body: TicketIn, caller: PlatformAdmin
) -> dict:
    pool = await request_pool(request)
    return await console.retry_agency_job(pool, tenant_id, job_id, staff_sub=caller.sub, ticket_ref=body.ticket_ref)


# ── AA-939: members and owner recovery (Q1 6.3) ──────────────────────────────────────────────────
@router.get("/tenants/{tenant_id}/members", response_model=MemberList)
async def tenant_members(request: Request, tenant_id: uuid.UUID, caller: Reader) -> dict:
    pool = await request_pool(request)
    return await recovery.members(pool, tenant_id, staff_sub=caller.sub)


@router.post("/tenants/{tenant_id}/members/{member_id}/deactivate", response_model=MemberDeactivated)
async def deactivate_member(
    request: Request,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    caller: PlatformAdmin,
    body: OptionalTicketIn | None = None,
) -> dict:
    pool = await request_pool(request)
    return await recovery.deactivate_member(
        pool,
        tenant_id,
        member_id,
        staff_sub=caller.sub,
        directory=request.app.state.directory,
        ticket_ref=body.ticket_ref if body else None,
    )


@router.post("/tenants/{tenant_id}/owner-recovery", response_model=RecoveryOpened, status_code=201)
async def open_recovery(request: Request, tenant_id: uuid.UUID, body: RecoveryIn, caller: PlatformAdmin) -> dict:
    pool = await request_pool(request)
    settings = request.app.state.settings
    return await recovery.open_request(
        pool,
        tenant_id,
        candidate_member_id=body.candidate_member_id,
        reason=body.reason,
        staff_sub=caller.sub,
        directory=request.app.state.directory,
        mailer=request.app.state.mailer,
        web_origin=settings.invitation_origin,
        ticket_ref=body.ticket_ref,
    )


@router.post("/tenants/{tenant_id}/owner-recovery/{request_id}/complete", response_model=RecoveryCompleted)
async def complete_recovery(
    request: Request,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    caller: PlatformAdmin,
    body: OptionalTicketIn | None = None,
) -> dict:
    pool = await request_pool(request)
    return await recovery.complete_request(
        pool, tenant_id, request_id, staff_sub=caller.sub, ticket_ref=body.ticket_ref if body else None
    )
