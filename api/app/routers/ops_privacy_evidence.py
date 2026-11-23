"""``/crm/ops/v1/privacy-evidence`` (AA-1174, S288 privacy obligations; crm.ops.v1 1.18): consent capture and campaign
sends to opted-in contacts, as counts across every agency, read by the CIS admin page Platform › Compliance
(Obligations) on Refresh evidence with the staff member's own WORKFORCE token (any full OPS reader: cis-admin or
crm-platform-admin). CIS never reads the ``crm`` schema (root ADR 0008a); this answer is the CRM's own count.

Counts and one timestamp only (no agency id or name, no contact, no address), so it writes no agency audit event (like
``/data-coverage``). ``days`` (1-365, default 30) is the CIS setting cis.compliance.campaign_consent_window_days; the
cap here bounds the work of one call. Logic: :func:`app.ops.privacy_evidence.platform_privacy_evidence`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Query, Request

from app.auth.identity_config import OPS
from app.auth.route_guard import class_router
from app.db import request_pool
from app.ops import privacy_evidence
from app.routers.ops import FullReader, _Out

router = class_router(OPS, tags=["ops"])

MAX_DAYS = 365  # technical-constant: bounds one call's scan; the window itself is a CIS setting passed in


class PrivacyEvidence(_Out):
    as_of: str
    window_days: int
    tenants: int
    marketing_sent: int
    violations: int
    excluded_erased: int
    consents_recorded: int
    last_consent_at: str | None


@router.get("/privacy-evidence", response_model=PrivacyEvidence)
async def get_privacy_evidence(
    request: Request, caller: FullReader, days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 30
) -> dict[str, Any]:
    pool = await request_pool(request)
    return await privacy_evidence.platform_privacy_evidence(pool, staff_sub=caller.sub, days=days)
