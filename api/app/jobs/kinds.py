"""Per-kind facts for ``crm.job``: expected run time and the deploy-gate exemptions (ported from AA-Booking, AA-751).

A running job counts as **overdue** in the backlog metrics (:mod:`app.jobs.metrics`) once its current attempt has run
longer than ``OVERDUE_FACTOR`` x its expected seconds. Every kind the worker handles must be listed here (a unit test
compares this map with the handlers the lifespan registers).

``DEPLOY_GATE_EXEMPT``: kinds the deploy gate (``GET /crm/ci/deploy-gate``) does not count. ``crm_heartbeat`` is the
always-queued liveness kind (one row per minute slot): counting it would block every deploy, and interrupting it is
harmless. It is also idle-exempt in AA-CIS-Infra DB Migrate (``db/owners.yaml`` ``idle_exempt_kinds``), so a CIS,
Booking or TripPlanner migration is never blocked by the CRM (card CRM-04 E check). ``crm_job_retention`` is
deliberately NOT exempt: it is queued only when due (``handlers.DailyRetentionSchedule``), so it blocks only while it
really runs.
"""

from __future__ import annotations

HEARTBEAT = "crm_heartbeat"
JOB_RETENTION = "crm_job_retention"

DEFAULT_EXPECTED_SECONDS = 300
OVERDUE_FACTOR = 2

DEPLOY_GATE_EXEMPT: frozenset[str] = frozenset({HEARTBEAT})

EXPECTED_SECONDS: dict[str, int] = {
    HEARTBEAT: 30,
    JOB_RETENTION: 900,
}


def expected_seconds(kind: str) -> int:
    return EXPECTED_SECONDS.get(kind, DEFAULT_EXPECTED_SECONDS)


def overdue_after_seconds(kind: str) -> int:
    """Seconds after which the current attempt of a running job of ``kind`` counts as overdue."""
    return OVERDUE_FACTOR * expected_seconds(kind)
