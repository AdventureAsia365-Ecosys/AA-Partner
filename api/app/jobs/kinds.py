"""Per-kind facts for ``crm.job``: expected run time and the deploy-gate exemptions (ported from AA-Booking, AA-751).

A running job counts as **overdue** in the backlog metrics (:mod:`app.jobs.metrics`) once its current attempt has run
longer than ``OVERDUE_FACTOR`` x its expected seconds. Every kind the worker handles must be listed here (a unit test
compares this map with the handlers the lifespan registers).

``DEPLOY_GATE_EXEMPT``: kinds the deploy gate (``GET /crm/ci/deploy-gate``) does not count. ``crm_heartbeat`` is the
always-queued liveness kind (one row per minute slot): counting it would block every deploy, and interrupting it is
harmless. It is also idle-exempt in AA-CIS-Infra DB Migrate (``db/owners.yaml`` ``idle_exempt_kinds``), so a CIS,
Booking or TripPlanner migration is never blocked by the CRM (card CRM-04 E check). ``crm_job_retention`` is
deliberately NOT exempt: it is queued only when due (``handlers.DailyRetentionSchedule``), so it blocks only while it
really runs. The AA-940 kinds ``crm_audit_export`` (hourly) and ``crm_audit_retention`` (daily) follow the same rule,
and so do the AA-942 kinds: ``crm_lifecycle`` (daily, due-now hook), ``crm_offboard`` and ``crm_dispatch`` (one row per
event; a message's row waits its 10-second undo delay), AA-943's hourly ``crm_stage_date_rule`` and AA-944's daily
``crm_fx_fetch``, and AA-945's ``crm_mail_ingest`` (queued only while forwarded mail waits), ``crm_mail_health``
(daily) and ``crm_mail_history_import`` (one per upload).

AA-978: ``crm_membership_pull`` (the CIS membership feed) is Recurring every 20 s, so like the heartbeat it always
holds its next slot's row queued: it is exempt here and in DB Migrate (Infra ``owners.yaml`` ``idle_exempt_kinds``).
Interrupting it is harmless (each event is its own transaction, recorded once in ``crm.membership_inbox``).

AA-947: ``crm_booking_sync`` (the AA outage queue and the Booking event feed) is Recurring every 60 s, plus one-agency
nudge rows queued by the AA requests: exempt here and in DB Migrate for the same reason (Infra ``owners.yaml``
``idle_exempt_kinds``); every request and every event is its own short transaction, and a request a stopped run held
is sent again only after its lease (with the same Idempotency-Key). ``crm_catalog_sync`` (hourly, due-now hook) is not
exempt.
"""

from __future__ import annotations

HEARTBEAT = "crm_heartbeat"
JOB_RETENTION = "crm_job_retention"
# AA-936: one stored inquiry -> traveller, profile, trip at Inquiry, reply task (app.intake.process).
INTAKE_PROCESS = "crm_intake_process"
# AA-940: the hourly audit export (both Object Lock buckets, hash chain) and the daily audit retention (verify, drop).
AUDIT_EXPORT = "crm_audit_export"
AUDIT_RETENTION = "crm_audit_retention"
# AA-938: hourly delete of expired F3 placeholder maps (app.privacy.purge), queued due-now, never ahead.
PLACEHOLDER_PURGE = "crm_placeholder_purge"
# AA-942: the daily data lifecycle run (redaction, LLM-call purge, trash purge, auto-archive), the agency offboarding
# steps (export, erase) and one outbound message through the dispatcher gate.
LIFECYCLE = "crm_lifecycle"
OFFBOARD = "crm_offboard"
DISPATCH = "crm_dispatch"
# AA-943: the hourly trip date rule (booked / pre-departure -> travelling on the departure date, travelling ->
# completed on the return date; app.stages_job), queued due-now, never ahead.
STAGE_DATE_RULE = "crm_stage_date_rule"
# AA-944: the daily platform FX rates (app.fx.fetch), queued due-now once per UTC day, never ahead.
FX_FETCH = "crm_fx_fetch"
# AA-978: pull the CIS tenant.membership.v1 feed (app.cis.membership_feed), Recurring every 20 s.
MEMBERSHIP_PULL = "crm_membership_pull"
# AA-945: forwarded mail from the ingest bucket (queued per minute only while raw/ holds objects, app.mail.ingest),
# the daily mailbox health check (due-now hook, app.mail.health) and one mbox / CSV history import per upload.
MAIL_INGEST = "crm_mail_ingest"
MAIL_HEALTH = "crm_mail_health"
MAIL_IMPORT = "crm_mail_history_import"
# AA-947: the AA outage queue + the Booking /events feed (app.booking.sync), Recurring every 60 s, nudged per request;
# the hourly cache of published AA tours from CIS catalog.http.v1 (app.catalog.sync), queued due-now.
BOOKING_SYNC = "crm_booking_sync"
CATALOG_SYNC = "crm_catalog_sync"

DEFAULT_EXPECTED_SECONDS = 300
OVERDUE_FACTOR = 2

DEPLOY_GATE_EXEMPT: frozenset[str] = frozenset({HEARTBEAT, MEMBERSHIP_PULL, BOOKING_SYNC})

EXPECTED_SECONDS: dict[str, int] = {
    HEARTBEAT: 30,
    JOB_RETENTION: 900,
    INTAKE_PROCESS: 30,
    AUDIT_EXPORT: 300,
    AUDIT_RETENTION: 900,
    PLACEHOLDER_PURGE: 60,
    LIFECYCLE: 900,
    OFFBOARD: 600,
    DISPATCH: 30,
    STAGE_DATE_RULE: 120,
    FX_FETCH: 120,
    MEMBERSHIP_PULL: 30,
    MAIL_INGEST: 120,
    MAIL_HEALTH: 300,
    MAIL_IMPORT: 120,  # one slice (history_import.SLICE messages)
    BOOKING_SYNC: 60,
    CATALOG_SYNC: 300,
}


def expected_seconds(kind: str) -> int:
    return EXPECTED_SECONDS.get(kind, DEFAULT_EXPECTED_SECONDS)


def overdue_after_seconds(kind: str) -> int:
    """Seconds after which the current attempt of a running job of ``kind`` counts as overdue."""
    return OVERDUE_FACTOR * expected_seconds(kind)
