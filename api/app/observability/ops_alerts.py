"""In-app ops checks that replaced the CRM CloudWatch alarms (S263 alert diet, AA-1025).

Until S263 AA-CIS-Infra alarmed on custom metrics the job sampler published (``AA/Jobs/Crm``) and on log metric
filters (``crm_breakglass_opened``, ``crm_membership_feed_stuck``, ...). The API process now checks the same things
itself, at $0, and e-mails AA ops through the platform alerts SNS topic (env ``ALERTS_TOPIC_ARN``, the topic the removed
alarms used, so the recipients are the same):

- **state checks** from the job sampler's 5-minute read (:mod:`app.jobs.metrics`): ``jobs_stuck`` (due queued row
  > 900 s while no healthy job runs), ``jobs_overdue`` (a running row past 2 x its expected time), ``audit_export_lag``
  (>= 180 min), ``audit_partitions`` (< 1 month ahead), ``audit_verify_lag`` (>= 50 h). One e-mail per check per UTC
  day while it lasts.
- **events** raised where the code already writes the alarm line, with :func:`emit` (sync, never raises): the
  break-glass opening and the owner not mailed (one e-mail per session), an ops release incomplete at T-21 or withdrawn
  (per release), a refused CIS membership event (per event), the membership feed stuck (per UTC day) and a bulk reveal
  (per member and hour). The log lines stay as they were.

The sampler drains the events and evaluates the state checks every tick, then :class:`AlertPublisher` sends each
finding once: the dedupe is a ledger row in ``crm.job`` (kind :data:`OPS_ALERT_KIND`, status ``succeeded``, never
claimed) whose idempotency key is ``crm_ops_alert:<check>:<scope>`` (the scope is the id, or the UTC day), written by
:func:`app.jobs.queue.record_alert`. Only the task whose insert wins publishes, so two API tasks or a restart never
send twice; a failed publish deletes the row (:func:`app.jobs.queue.forget_alert`) and puts the event back for the
next tick. Without ``ALERTS_TOPIC_ARN`` (local, tests) a finding is logged only.

E-mails hold ids, codes and counts only (no names, no ticket text), like the log lines they replace.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncpg

from app.jobs import queue

log = logging.getLogger(__name__)

OPS_ALERT_KIND = "crm_ops_alert"
SNS_REGION = "us-west-1"  # acc2, where the alerts topic lives
MAX_PENDING = 500

STUCK_AFTER_S = 900
AUDIT_EXPORT_LAG_MIN = 180
AUDIT_VERIFY_LAG_H = 50


@dataclass(frozen=True)
class Check:
    title: str
    action: str


CHECKS: dict[str, Check] = {
    "jobs_stuck": Check(
        "Job queue stuck",
        "A due crm.job row has waited more than 15 minutes and no healthy job runs. Check the worker (WORKER_ENABLED, "
        "the crm_heartbeat row) on the platform console Jobs view and in the CRM log.",
    ),
    "jobs_overdue": Check(
        "Job running too long",
        "A running crm.job row is past twice its expected time and blocks the serial worker. Read it and the CRM log.",
    ),
    "audit_export_lag": Check(
        "Agency audit export behind",
        "The hourly audit export is 3 hours or more behind (design v3 4.9). Check the crm_audit_export job rows and "
        "the audit_export_failed log lines; no partition can be dropped while an agency is behind.",
    ),
    "audit_partitions": Check(
        "Audit partition missing",
        "Next month has no crm.audit_event partition: from the 1st every audit write, and so the request that writes "
        "it, fails. Run SELECT crm.audit_ensure_partitions() as crm_app or let crm_audit_export run.",
    ),
    "audit_verify_lag": Check(
        "Agency audit not verified",
        "An agency's exported audit has not been verified for 50 hours or more: no partition can be dropped for any "
        "agency. Read the crm_audit_retention rows and the audit_verify_failed lines.",
    ),
    "breakglass_opened": Check(
        "Break-glass access opened",
        "AA staff opened emergency read access to one agency without the owner's grant. Check that the session has an "
        "incident ticket; the agency sees every read in its access log.",
    ),
    "breakglass_owner_not_mailed": Check(
        "Break-glass owner not told",
        "Break-glass was opened but no agency owner could be e-mailed. Tell the owner within 24 hours (design v3 L3) "
        "and check SES and the partner pool.",
    ),
    "ops_release_incomplete_t21": Check(
        "Ops release incomplete at T-21",
        "A confirmed AA departure is 21 days away and the agency's traveller details are incomplete. Open S-02 (ops "
        "releases) and follow up with the agency.",
    ),
    "ops_release_withdrawn": Check(
        "Ops release withdrawn",
        "A trip with an AA line was cancelled or the agency withdrew its release: AA ops no longer hold the traveller "
        "details. Check the departure in the Booking staff admin.",
    ),
    "membership_feed_stuck": Check(
        "CIS membership feed stuck",
        "The CRM has failed to pull the CIS membership feed 10 times in a row: CIS removals and agency locks no longer "
        "reach the CRM. Read error on the log line and /crm/health/ready membership_feed.",
    ),
    "membership_apply_refused": Check(
        "CIS membership event refused",
        "The CRM refused one CIS membership event (a guard or integrity rule) and skipped it; the CRM may still list a "
        "member CIS removed. Find the event in crm.membership_inbox.",
    ),
    "bulk_reveal": Check(
        "Bulk reveal",
        "One agency member revealed 60 or more traveller fields or documents within an hour (design v3 4.4). Confirm "
        "with the agency that it was expected; the reveal events are in its audit.",
    ),
}


EVENT_CHECKS = frozenset(
    {
        "breakglass_opened",
        "breakglass_owner_not_mailed",
        "ops_release_incomplete_t21",
        "ops_release_withdrawn",
        "membership_feed_stuck",
        "membership_apply_refused",
        "bulk_reveal",
    }
)


@dataclass(frozen=True)
class Finding:
    check: str
    summary: str
    scope: str | None = None  # an id token for a per-event e-mail; None = once per UTC day
    details: dict[str, Any] = field(default_factory=dict)


# ── events ─────────────────────────────────────────────────────────────────────────────────────


class _Pending:
    """Findings raised since the last sampler tick, per process. Thread-safe and bounded (oldest dropped)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: deque[Finding] = deque(maxlen=MAX_PENDING)

    def add(self, finding: Finding) -> None:
        with self._lock:
            self._items.append(finding)

    def drain(self) -> list[Finding]:
        with self._lock:
            items = list(self._items)
            self._items.clear()
        return items

    def restore(self, items: Iterable[Finding]) -> None:
        with self._lock:
            self._items.extendleft(reversed(list(items)))


PENDING = _Pending()


def emit(check: str, scope: Any = None, **details: Any) -> None:
    """Raise an event check (sync, cheap, never raises). ``details`` are ids, codes and counts only."""
    try:
        if check not in CHECKS:
            raise KeyError(check)
        summary = " ".join(f"{k}={v}" for k, v in details.items()) or check
        fields = {k: str(v) for k, v in details.items()}
        PENDING.add(Finding(check, summary, None if scope is None else str(scope), fields))
    except Exception:  # noqa: BLE001 — an alert must never break the request or job that raised it
        log.warning("ops_alert_emit_failed check=%s", check)


# ── state checks ───────────────────────────────────────────────────────────────────────────────


def state_findings(sample: Any, audit: Any = None) -> list[Finding]:
    """Findings of one sampler read (:class:`app.jobs.metrics.Sample`, :class:`app.jobs.metrics.AuditSample`)."""
    out: list[Finding] = []
    if sample.oldest_queued_age_s > STUCK_AFTER_S:
        out.append(
            Finding(
                "jobs_stuck",
                f"oldest due row waited {int(sample.oldest_queued_age_s)} s",
                details={"running": sample.running},
            )
        )
    if sample.overdue_running:
        out.append(Finding("jobs_overdue", f"{sample.overdue_running} running row(s) overdue"))
    if audit is not None:
        if audit.export_lag_minutes >= AUDIT_EXPORT_LAG_MIN:
            out.append(Finding("audit_export_lag", f"{int(audit.export_lag_minutes)} minutes behind"))
        if audit.partition_months_ahead < 1:
            out.append(Finding("audit_partitions", f"{audit.partition_months_ahead} month(s) ahead"))
        if audit.verify_lag_hours >= AUDIT_VERIFY_LAG_H:
            out.append(Finding("audit_verify_lag", f"{int(audit.verify_lag_hours)} hours unverified"))
    return out


# ── publishing ─────────────────────────────────────────────────────────────────────────────────


class SnsClient(Protocol):
    def publish(self, **kwargs: Any) -> dict[str, Any]: ...


def sns_client() -> SnsClient:
    import boto3  # lazy: only an alert needs it
    from botocore.config import Config

    return boto3.client(
        "sns", region_name=SNS_REGION, config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
    )


def scope_of(finding: Finding, now: datetime | None = None) -> str:
    return finding.scope or (now or datetime.now(UTC)).astimezone(UTC).date().isoformat()


def alert_key(finding: Finding, now: datetime | None = None) -> str:
    return f"{OPS_ALERT_KIND}:{finding.check}:{scope_of(finding, now)}"[:200]


def render(finding: Finding, environment: str) -> tuple[str, str]:
    """(subject, text). The subject stays under the SNS limit of 100 ASCII characters."""
    chk = CHECKS[finding.check]
    subject = f"[AA-Partner {environment}] Ops check: {chk.title}"[:100]
    lines = [f"{chk.title}: {finding.summary}.", "", chk.action, ""]
    lines += [f"{k}: {v}" for k, v in finding.details.items()]
    once = "this event" if finding.scope else "check and UTC day"
    lines += [
        "",
        f"At most one e-mail per {once} (check {finding.check}, an in-app check that replaced a CloudWatch alarm).",
    ]
    return subject.encode("ascii", "replace").decode("ascii"), "\n".join(lines)


class AlertPublisher:
    """Publishes findings to the alerts topic once each (ledger row in crm.job)."""

    def __init__(
        self,
        topic_arn: str | None = None,
        *,
        environment: str | None = None,
        client_factory: Callable[[], SnsClient] = sns_client,
    ) -> None:
        self._topic_arn = (os.environ.get("ALERTS_TOPIC_ARN", "") if topic_arn is None else topic_arn).strip()
        self._environment = environment or os.environ.get("ENVIRONMENT", "dev")
        self._client_factory = client_factory
        self._client: SnsClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self._topic_arn)

    def _publish(self, subject: str, text: str) -> None:
        # Runs in a thread: creating the boto3 client reads credentials, so it is created here, once.
        if self._client is None:
            self._client = self._client_factory()
        self._client.publish(TopicArn=self._topic_arn, Subject=subject, Message=text)

    async def alert(
        self, pool: asyncpg.Pool, found: list[Finding], now: datetime | None = None
    ) -> tuple[dict[str, str], list[Finding]]:
        """Send each finding once. Returns ({check: result}, the findings to try again next tick). Never raises."""
        results: dict[str, str] = {}
        retry: list[Finding] = []
        for f in found:
            if not self.configured:
                log.warning("ops_check_finding check=%s summary=%s (ALERTS_TOPIC_ARN unset)", f.check, f.summary)
                results[f.check] = "not_configured"
                continue
            try:
                ledger_id = await queue.record_alert(
                    pool, OPS_ALERT_KIND, alert_key(f, now), {"kind": f.check, "scope": scope_of(f, now)[:64]}
                )
            except Exception as exc:  # noqa: BLE001 — the next tick tries again
                log.warning("ops_check_alert_failed check=%s stage=ledger error=%s", f.check, type(exc).__name__)
                results[f.check] = "failed"
                retry.append(f)
                continue
            if ledger_id is None:
                results.setdefault(f.check, "already_sent")
                continue
            subject, text = render(f, self._environment)
            try:
                await asyncio.to_thread(self._publish, subject, text)
            except Exception as exc:  # noqa: BLE001 — AccessDenied before the Infra apply, throttling, network
                log.warning("ops_check_alert_failed check=%s stage=publish error=%s", f.check, type(exc).__name__)
                try:
                    await queue.forget_alert(pool, ledger_id)
                except Exception as exc2:  # noqa: BLE001
                    log.warning("ops_check_alert_failed check=%s stage=forget error=%s", f.check, type(exc2).__name__)
                results[f.check] = "failed"
                retry.append(f)
                continue
            log.warning("ops_check_alert check=%s summary=%s", f.check, f.summary)
            results[f.check] = "sent"
        return results, retry


async def run_checks(
    pool: asyncpg.Pool, publisher: AlertPublisher, sample: Any, audit: Any = None, now: datetime | None = None
) -> dict[str, str]:
    """One tick: the state checks of this read plus the events raised since the last tick. Events whose alert failed
    are put back for the next tick (state checks are simply evaluated again). Never raises."""
    events = PENDING.drain()
    results, retry = await publisher.alert(pool, state_findings(sample, audit) + events, now)
    pending = [f for f in retry if f.check in EVENT_CHECKS]
    if pending:
        PENDING.restore(pending)
    return results
