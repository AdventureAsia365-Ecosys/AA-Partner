"""AA-1174 (S288, privacy obligations of CIS Platform › Compliance): crm.ops.v1 1.18 ``GET /privacy-evidence``.

Like :func:`app.ops.console.platform_coverage`: platform-wide counts only (no agency id or name, no contact, no
address), so no per-agency audit event; one log line per call. Each agency is read inside ``platform_tx`` (RLS FORCE: a
bare ``crm_app`` count would read 0 rows, lesson S269), read-only. A marketing send is a dispatch row (purpose
marketing) the gate let through (status and gate_result ``sent``) in the window; it is a violation when the contact's
latest marketing consent at or before the send was not a yes, or the address was on the agency's suppression list at
send time. A send whose contact was erased since (no traveller row) is left out and counted apart: erasure removes the
consent too, so it would read as a false violation. This module opens ``platform_tx`` (listed in the code rules'
PLATFORM_MODULES) for reads only.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import asyncpg

from app.audit_export import registry
from app.errors import NotFound
from app.members.recovery import platform_actor
from app.tenancy import TENANT_STATUSES, platform_tx

log = logging.getLogger(__name__)

PRIVACY_SENDS_SQL = """
SELECT count(*)                                                                                   AS sent,
       count(*) FILTER (WHERE t.traveler_id IS NULL)                                               AS erased,
       count(*) FILTER (WHERE t.traveler_id IS NOT NULL AND (c.value IS DISTINCT FROM true OR s.hit)) AS violations
  FROM crm.dispatch_queue d
  LEFT JOIN crm.traveler t ON t.traveler_id = d.traveler_id
  LEFT JOIN LATERAL (SELECT c.value FROM crm.consent c
                      WHERE c.traveler_id = d.traveler_id AND c.purpose = 'marketing' AND c.at <= d.decided_at
                      ORDER BY c.at DESC, c.created_at DESC, c.consent_id DESC LIMIT 1) c ON true
  LEFT JOIN LATERAL (SELECT true AS hit FROM crm.campaign_suppression s
                      WHERE s.email_bidx = t.email_bidx AND s.created_at <= d.decided_at
                        AND (s.deleted_at IS NULL OR s.deleted_at > d.decided_at) LIMIT 1) s ON true
 WHERE d.purpose = 'marketing' AND d.status = 'sent' AND d.gate_result = 'sent'
   AND d.decided_at >= now() - make_interval(days => $1)
"""
PRIVACY_CONSENT_SQL = """
SELECT count(*) FILTER (WHERE at >= now() - make_interval(days => $1)) AS recorded, max(at) AS last_at
  FROM crm.consent
"""


async def platform_privacy_evidence(pool: asyncpg.Pool, *, staff_sub: str, days: int) -> dict[str, Any]:
    """Counts across every agency for the last ``days`` days (the CIS setting
    cis.compliance.campaign_consent_window_days, capped by the route)."""
    out = {"tenants": 0, "marketing_sent": 0, "violations": 0, "excluded_erased": 0, "consents_recorded": 0}
    last: dt.datetime | None = None
    for agency in await registry.agencies(pool):
        try:
            async with platform_tx(
                pool, agency.tenant_id, actor=platform_actor(staff_sub), statuses=TENANT_STATUSES
            ) as ctx:
                sends = await ctx.conn.fetchrow(PRIVACY_SENDS_SQL, days)
                consent = await ctx.conn.fetchrow(PRIVACY_CONSENT_SQL, days)
        except NotFound:
            continue  # registered but trashed: not an agency
        out["tenants"] += 1
        out["marketing_sent"] += int(sends["sent"] or 0)
        out["excluded_erased"] += int(sends["erased"] or 0)
        out["violations"] += int(sends["violations"] or 0)
        out["consents_recorded"] += int(consent["recorded"] or 0)
        if consent["last_at"] is not None and (last is None or consent["last_at"] > last):
            last = consent["last_at"]
    log.info(
        "crm_ops_privacy_evidence agencies=%d sent=%d violations=%d",
        out["tenants"],
        out["marketing_sent"],
        out["violations"],
    )
    return {
        "as_of": dt.datetime.now(dt.UTC).isoformat(),
        "window_days": days,
        **out,
        "last_consent_at": last.isoformat() if last else None,
    }
