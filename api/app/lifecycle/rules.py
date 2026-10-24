"""Retention rules per data class (design v3 §3.6, lane §4; data standards §7 rows of owner ``crm``), shown to the owner
on Settings > Data and privacy (``GET /crm/v1/lifecycle``) and enforced by the jobs named here.

The API returns codes and numbers only; the web words them (EN default, VI). A rule names its mechanism and the job or
card that enforces it, so the page never promises something no code does.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.pipeline import LOST_REASONS

MIN_TRASH_DAYS, MAX_TRASH_DAYS = 1, 30
DEFAULT_AUTO_ARCHIVE_MONTHS = 36
MIN_AUTO_ARCHIVE_MONTHS, MAX_AUTO_ARCHIVE_MONTHS = 12, 120
SUBMISSION_REDACT_DAYS = 90  # crm-005 form_submission.redact_after default
LLM_CALL_DAYS = 180  # crm-010 crm.lifecycle_purge_expired
BACKUP_PURGE_DAYS = 30  # V1: erased data leaves backups and DEK copies within 30 days
EXPORT_FILE_DAYS = 7  # AA-CIS-Infra crm_storage.tf rule exports-7d

ARCHIVE_REASONS = ("agent", "client_request", "auto_inactive", "duplicate", "other")
HOLD_SCOPES = ("tenant", "trip", "traveler")
HOLD_REASONS = ("litigation", "regulator_request", "dispute", "insurance_claim", "other")


@dataclass(frozen=True)
class Rule:
    """One data class: how long it is kept and what removes it.

    ``window``: ``until_deleted`` (the agency decides), ``days`` / ``months`` (``amount``), ``with_parent`` (goes with
    the record it belongs to), ``setting`` (the agency's own value, ``setting`` names it). ``mechanism``: what removes
    it. ``enforced_by``: the job kind or card."""

    code: str
    window: str
    amount: int | None
    mechanism: str
    enforced_by: str
    setting: str | None = None

    def view(self) -> dict[str, object]:
        return asdict(self)


RULES: tuple[Rule, ...] = (
    Rule("traveler_pii", "until_deleted", None, "owner_delete_or_offboarding", "crm_lifecycle"),
    Rule("traveler_inactive", "setting", None, "auto_archive", "crm_lifecycle", "auto_archive_months"),
    Rule("trips", "until_deleted", None, "owner_delete_or_offboarding", "crm_lifecycle"),
    Rule("trip_stage_history", "with_parent", None, "erased_with_trip", "crm_lifecycle"),
    Rule("documents", "with_parent", None, "erased_with_trip_or_client", "crm_lifecycle"),
    Rule("consent", "with_parent", None, "erased_with_client", "crm_lifecycle"),
    Rule("trash", "setting", None, "trash_purge", "crm_lifecycle", "trash_days"),
    Rule("form_submissions", "days", SUBMISSION_REDACT_DAYS, "payload_redacted", "crm_lifecycle"),
    Rule("llm_calls", "days", LLM_CALL_DAYS, "purge_keep_daily_rollup", "crm_lifecycle"),
    Rule("audit_in_app", "months", 12, "partition_drop_after_verified_export", "crm_audit_retention"),
    Rule("audit_archive", "months", 84, "object_lock_archive", "crm_audit_export"),
    Rule("placeholder_map", "days", 1, "purge", "AA-938"),
    Rule("job_rows", "days", 30, "purge", "crm_job_retention"),
    Rule("export_files", "days", EXPORT_FILE_DAYS, "bucket_lifecycle", "AA-CIS-Infra"),
    Rule("erased_in_backups", "days", BACKUP_PURGE_DAYS, "backup_expiry_with_alarm", "AA-CIS-Infra"),
)


def rules_view() -> list[dict[str, object]]:
    return [r.view() for r in RULES]


# ── Lost reasons (R23) ────────────────────────────────────────────────────────────────────────────
# The fixed codes live in app.pipeline (LOST_REASONS, the crm-005 CHECK). The Booking feed pre-selects one when the
# last AA event explains the loss (lane §3.3): CRM-19 calls this with the trip's AA event codes, oldest first.
AA_EVENT_LOST_REASON: dict[str, str] = {
    "line.declined": "aa_on_request_declined",
    "availability.sold_out": "aa_sold_out",
    "departure.cancelled": "aa_sold_out",
    "quote.price_changed": "price_aa_tour",
}


def suggest_lost_reason(aa_events: list[str]) -> str | None:
    """The Lost reason the AA events explain, or None (the agent picks). A hold that expired after a sold-out notice
    is ``aa_sold_out``; an expired hold alone explains nothing."""
    for event in reversed(aa_events):
        if event == "hold.expired":
            continue
        reason = AA_EVENT_LOST_REASON.get(event)
        if reason is not None:
            assert reason in LOST_REASONS  # noqa: S101 — a code outside the crm-005 CHECK is a programming error
            return reason
        return None
    return None
