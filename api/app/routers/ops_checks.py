"""``/crm/ops/v1/ops-checks`` (AA-1171, S287 alerts catalogue P0 decision D1; crm.ops.v1 1.17): the CRM's in-app check
registry, read by the CIS admin page Platform › Alerts › Catalogue with the staff member's own WORKFORCE token (any full
OPS reader: cis-admin or crm-platform-admin). CIS never reads the ``crm`` schema (root ADR 0008a); this answer is the
CRM's own description of its checks.

One entry per check of :data:`app.observability.ops_alerts.CHECKS`: its title and action (the e-mail's), whether it is
a state or an event check, the component it watches, the thresholds in force with their setting keys (crm-085,
edited on CIS Platform › Settings, CRM tab) or the fixed rule still in code, and the last alert it sent (the
``crm_ops_alert`` ledger rows of ``crm.job``, the last 7 days). The shape is the "ops check registry v1" AA-Booking
serves too (``GET /booking/ops/checks``). No agency, member or trip data: check names, numbers and times only, so it
writes no agency audit event (like ``/jobs/summary``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import Request

from app import platform_settings as ps
from app.auth.identity_config import OPS
from app.auth.route_guard import class_router
from app.db import request_pool
from app.jobs import queue
from app.observability import ops_alerts
from app.routers.ops import FullReader, _Out

router = class_router(OPS, tags=["ops"])

WHERE = "CIS Platform › Settings (CRM tab)"


class RegistryThreshold(_Out):
    setting: str | None
    value: Any
    unit: str | None
    editable_via: str
    where: str | None
    label: str


class RegistryCheck(_Out):
    key: str
    title: str
    component: str
    severity: str
    kind: str
    action: str
    thresholds: list[RegistryThreshold]
    channel: list[str]
    last_alert_at: str | None


class CheckRegistry(_Out):
    app: str
    version: int
    as_of: str
    checks: list[RegistryCheck]


def _thresholds(check: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in ops_alerts.THRESHOLD_KEYS.get(check, ()):
        d = ps.definition(key)
        out.append(
            {
                "setting": key,
                "value": ps.get(key),
                "unit": d.unit,
                "editable_via": "settings",
                "where": WHERE,
                "label": d.label,
            }
        )
    fixed = ops_alerts.FIXED_THRESHOLDS.get(check)
    if fixed:
        out.append(
            {"setting": None, "value": None, "unit": None, "editable_via": "none", "where": None, "label": fixed}
        )
    return out


def registry_body(last_alerts: dict[str, datetime], now: datetime) -> dict[str, Any]:
    """The registry answer from the settings in force and the last alert per check."""
    checks = []
    for key, chk in ops_alerts.CHECKS.items():
        last = last_alerts.get(key)
        event = key in ops_alerts.EVENT_CHECKS
        checks.append(
            {
                "key": key,
                "title": chk.title,
                "component": ops_alerts.COMPONENTS.get(key, "api"),
                "severity": "ticket",
                "kind": "event" if event else "state",
                "action": chk.action,
                "thresholds": _thresholds(key),
                "channel": [
                    "alerts topic e-mail (once per event)"
                    if event
                    else "alerts topic e-mail (once per check and UTC day)"
                ],
                "last_alert_at": last.isoformat() if last else None,
            }
        )
    return {"app": "crm", "version": 1, "as_of": now.isoformat(), "checks": checks}


@router.get("/ops-checks", response_model=CheckRegistry)
async def ops_check_registry(request: Request, caller: FullReader) -> dict[str, Any]:
    pool = await request_pool(request)
    return registry_body(await queue.last_alerts(pool, ops_alerts.OPS_ALERT_KIND), datetime.now(UTC))
