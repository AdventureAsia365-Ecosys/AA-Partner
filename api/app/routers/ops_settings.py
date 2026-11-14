"""``/crm/ops/v1/settings`` (AA-1096 lane L4, crm.ops.v1 1.13, crm-064): the admin-editable CRM settings, relayed by
the CIS admin Platform > Settings page (CRM tab) with the staff member's own WORKFORCE token.

- ``GET /settings`` and ``GET /settings/{key}/history[?tenant_id]``: any full OPS reader (cis-admin or
  crm-platform-admin; never a desk caller, 403 ``missing_group``).
- ``POST /settings/{key}/changes`` and ``POST /settings/changes/{value_id}/approve|reject``: ``crm-platform-admin``
  (the Dev-only E2E CIS admin is read-only, 403 ``e2e_read_only``).

The thresholds, limits, caps, platform defaults and ranges of the agency-owner settings and the retention dry-run
switches; service :mod:`app.platform_settings.service`; 503 ``settings_unavailable`` before crm-064. The same route
class as :mod:`app.routers.ops` (OPS, the L1 response allow-list): the open changes are ``pending`` because crm.ops.v1
forbids the word "proposal" in a key.

1.14 (AA-1096 S280, additive): ``agency_overrides`` / ``agency_pending`` per platform+tenant setting, ``reason_min`` /
``evidence_min`` / ``evidence_max`` per definition; a backdated ``effective_from`` is 422 ``effective_from_in_past``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Path, Query, Request
from pydantic import Field

from app.auth.identity_config import OPS
from app.auth.route_guard import class_router
from app.db import request_pool
from app.platform_settings import service as settings_service
from app.routers.ops import FullReader, PlatformAdmin, _In, _Out

router = class_router(OPS, tags=["ops"])

SettingKey = Annotated[str, Path(pattern=r"^crm(\.[a-z0-9_]+){2,}$", max_length=120)]
SettingValue = bool | int | float | str | list[Any] | dict[str, Any]


class SettingRowOut(_Out):
    id: str
    key: str
    tenant_id: str | None
    value: SettingValue
    version: int
    status: Literal["proposed", "approved", "rejected", "superseded"]
    effective_from: str | None
    proposed_by: str
    proposed_at: str | None
    reason: str
    evidence_ref: str | None
    self_approved: bool
    decided_by: str | None
    decided_at: str | None
    decision_note: str | None
    previous_value: SettingValue | None = None


class AgencySettingRowOut(SettingRowOut):
    """An agency's own row of a platform+tenant key, with the agency's name (the list names who keeps its own value)."""

    agency_name: str | None


class SettingGuideTextOut(_Out):
    """One language of a setting's operator guide (the CIS 416 shape the Platform > Settings page reads)."""

    what: str
    raise_: str = Field(alias="raise")
    lower: str
    when: str
    example: str


class SettingGuideOut(_Out):
    en: SettingGuideTextOut
    vi: SettingGuideTextOut


class SettingDefinitionOut(_Out):
    key: str
    app: Literal["crm"]
    area: str
    label: str
    description: str
    value_type: Literal["int", "numeric", "bool", "enum", "duration_s", "usd", "pct", "list", "json"]
    unit: str | None
    min_value: float | None
    max_value: float | None
    allowed_values: list[Any] | None
    default_value: SettingValue
    scope_kind: Literal["platform", "platform+tenant"]
    impact: Literal["high", "medium", "low"]
    approval: Literal["single", "maker_checker"]
    evidence_required: bool
    env_override: str | None
    cache_s: int
    enforced: bool
    # what a change must carry (crm-064 CHECKs), so the page can say it before sending
    reason_min: int
    evidence_min: int
    evidence_max: int
    guide: SettingGuideOut | None = None  # None = no guide yet (tests/unit/test_aa1096_settings_guides.py fails on it)


class SettingOut(SettingDefinitionOut):
    value: SettingValue
    source: Literal["env", "tenant", "platform", "default"]
    version: int | None
    overridden_by_env: bool
    env_ignored: str | None
    refused: str | None
    last_change: SettingRowOut | None
    pending: list[SettingRowOut]
    # a platform+tenant key: each agency's own value in force and each agency's open change (approve or reject it
    # with its tenant_id); empty lists for a platform-only key
    agency_overrides: list[AgencySettingRowOut]
    agency_pending: list[AgencySettingRowOut]


class SettingsOut(_Out):
    app: Literal["crm"]
    areas: list[str]
    settings: list[SettingOut]
    two_person_approval: bool
    reader_error: str | None


class SettingHistoryOut(_Out):
    key: str
    tenant_id: str | None
    definition: SettingDefinitionOut
    history: list[SettingRowOut]


class SettingChangeIn(_In):
    """A new value of one setting. ``tenant_id``: one agency's own value (a platform+tenant key); ``base_version``: the
    approved version the person saw (409 when it moved); ``apply``: approve in the same step (a maker-checker key then
    records a self-approval while two-person approval is off)."""

    value: SettingValue
    # the crm-064 CHECKs (reason >= 20 characters, evidence 3 to 500): refused here before the database sees them
    reason: Annotated[str, Field(min_length=settings_service.REASON_MIN, max_length=2000)]
    evidence_ref: Annotated[
        str | None, Field(min_length=settings_service.EVIDENCE_MIN, max_length=settings_service.EVIDENCE_MAX)
    ] = None
    effective_from: datetime | None = None  # now or later (422 effective_from_in_past)
    tenant_id: uuid.UUID | None = None
    base_version: Annotated[int | None, Field(ge=0)] = None
    apply: bool = False


class SettingDecisionIn(_In):
    note: Annotated[str | None, Field(max_length=2000)] = None
    tenant_id: uuid.UUID | None = None  # the agency of an agency's own change (its rows are seen only in its context)


@router.get("/settings", response_model=SettingsOut)
async def get_settings(
    request: Request, caller: FullReader, area: Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9_]{0,39}$")] = None
) -> dict:
    pool = await request_pool(request)
    return await settings_service.list_settings(pool, area=area, staff_sub=caller.sub)


@router.get("/settings/{key}/history", response_model=SettingHistoryOut)
async def get_setting_history(
    request: Request, key: SettingKey, caller: FullReader, tenant_id: Annotated[uuid.UUID | None, Query()] = None
) -> dict:
    pool = await request_pool(request)
    return await settings_service.history(pool, key, tenant_id, staff_sub=caller.sub)


@router.post("/settings/{key}/changes", response_model=SettingRowOut, status_code=201)
async def post_setting_change(request: Request, key: SettingKey, body: SettingChangeIn, caller: PlatformAdmin) -> dict:
    pool = await request_pool(request)
    return await settings_service.propose(
        pool,
        key,
        value=body.value,
        reason=body.reason,
        staff_sub=caller.sub,
        base_version=body.base_version,
        evidence_ref=body.evidence_ref,
        effective_from=body.effective_from,
        tenant_id=body.tenant_id,
        apply=body.apply,
    )


@router.post("/settings/changes/{value_id}/{decision}", response_model=SettingRowOut)
async def post_setting_decision(
    request: Request,
    value_id: uuid.UUID,
    decision: Literal["approve", "reject"],
    caller: PlatformAdmin,
    body: SettingDecisionIn | None = None,
) -> dict:
    pool = await request_pool(request)
    body = body or SettingDecisionIn()
    return await settings_service.decide(
        pool, value_id, staff_sub=caller.sub, approve=decision == "approve", note=body.note, tenant_id=body.tenant_id
    )
