"""The pure rules of the CRM step of the CIS tenant offboarding erasure saga (AA-1093 stage 2, decision D2; contract
``tenant.erasure.v1``, byte copy in ``docs/contracts/``). No I/O: :mod:`app.lifecycle.tenant_erase` does the work and
:mod:`app.lifecycle.saga_store` keeps the record.

- :func:`parse_request`: one feed item (``ErasureRequest``) in the shape the CRM stores, or a reason code when it breaks
  the contract (a closed object, the crm step only).
- :func:`plan_entries` / :func:`plan_hash`: the plan of a whole-agency erase as contract ``TableEntry`` rows, and its
  hash in the contract form (sha256 of ``'tenant.erasure.v1|<tenant>|<round>'`` and the sorted
  ``'<table>|<action>|<rows>|<digest>'`` lines). The digest is null on every entry, as in the CIS plan: the CRM erases
  every row of the agency (no row selection to bind), the agency is out of service since T0 (CIS lock), and a digest
  over feed-written tables (the membership and Booking inboxes, the hourly entitlement projection) would change
  between the DRY RUN and the apply without any change to what is erased. The counts and the round bind the plan.
- :func:`next_mode`: what the step owes CIS now (dry_run, apply, verify or nothing) from the stored request and the
  reports already made; :func:`guard`: the refusals every app enforces on its own (D8).
- :func:`report_body`: the ``ErasureReport`` the CRM posts (ids, codes, hashes, counts only).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

CONTRACT = "tenant.erasure.v1"
APP = "crm"
PHASES = frozenset({"dry_run", "awaiting_approval", "apply", "verify", "done", "failed", "cancelled"})
STEP_STATUSES = frozenset({"waiting", "active", "done", "failed"})
MODES = ("dry_run", "apply", "verify")
OUTCOMES = frozenset({"ok", "blocked", "refused", "failed"})
REQUEST_FIELDS = frozenset(
    {
        "request_id",
        "tenant_id",
        "phase",
        "round",
        "opened_at",
        "approval_expires_at",
        "is_pilot",
        "is_synthetic",
        "steps",
        "updated_at",
        "cursor",
    }
)
STEP_FIELDS = frozenset({"app", "status", "not_before", "approved_plan_hash"})
REPORT_FIELDS = (
    "report_id",
    "request_id",
    "tenant_id",
    "app",
    "round",
    "mode",
    "status",
    "error_code",
    "plan_hash",
    "tables",
    "blockers",
    "census",
    "job_id",
    "reported_at",
)
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
MAX_ROUND = 1000  # technical-constant: the crm-066 CHECK of the round columns
MAX_CURSOR_LEN = 200  # technical-constant: the contract's cursor maxLength
# The erase-count keys of crm.erase_subject('tenant') that are not a table deleted under that name.
DETACH_KEYS = {"traveler_referrer_unlinked": "crm.traveler"}
NOT_TABLES = frozenset({"document_object"})  # bucket objects of the deleted crm.document rows
# The CRM agency row stays (status offboarded, crm-010 keep list) with its name replaced.
ERASED_NAME_PREFIX = "Erased agency "


def erased_name(tenant_id: uuid.UUID | str) -> str:
    """The agency name after the erase (as Booking's partner row, decision D3): no name of the agency is kept."""
    return ERASED_NAME_PREFIX + str(tenant_id)[:8]


# ── the feed ─────────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SagaRequest:
    """One request as the CRM stores it (crm.tenant_erasure_saga)."""

    request_id: uuid.UUID
    tenant_id: uuid.UUID
    phase: str
    round: int
    step_status: str
    not_before: dt.datetime
    approved_plan_hash: str | None
    approval_expires_at: dt.datetime | None
    is_pilot: bool
    is_synthetic: bool
    updated_at: dt.datetime


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _crm_step(steps: Any) -> dict[str, Any] | None:
    if not isinstance(steps, list) or not 1 <= len(steps) <= 4:
        return None
    mine = [s for s in steps if isinstance(s, dict) and s.get("app") == APP]
    if len(mine) != 1 or set(mine[0]) != STEP_FIELDS:
        return None
    return mine[0]


def parse_request(item: Any) -> SagaRequest | str:
    """The stored shape of one ``ErasureRequest`` (the crm step only), or a reason code (a value never leaves)."""
    if not isinstance(item, dict) or set(item) != REQUEST_FIELDS:
        return "request_shape"
    step = _crm_step(item["steps"])
    if step is None:
        return "step_shape"
    rid, tid = _uuid(item["request_id"]), _uuid(item["tenant_id"])
    rnd = item["round"]
    not_before, updated = _time(step["not_before"]), _time(item["updated_at"])
    expires = item["approval_expires_at"]
    expires_at = None if expires is None else _time(expires)
    approved = step["approved_plan_hash"]
    checks = (
        rid is not None and tid is not None,
        item["phase"] in PHASES and step["status"] in STEP_STATUSES,
        isinstance(rnd, int) and not isinstance(rnd, bool) and 1 <= rnd <= MAX_ROUND,
        not_before is not None and updated is not None and (expires is None or expires_at is not None),
        approved is None or (isinstance(approved, str) and HASH_RE.match(approved) is not None),
        isinstance(item["is_pilot"], bool) and isinstance(item["is_synthetic"], bool),
    )
    if not all(checks):
        return "request_value"
    return SagaRequest(
        request_id=rid,  # type: ignore[arg-type]
        tenant_id=tid,  # type: ignore[arg-type]
        phase=item["phase"],
        round=rnd,
        step_status=step["status"],
        not_before=not_before,  # type: ignore[arg-type]
        approved_plan_hash=approved,
        approval_expires_at=expires_at,
        is_pilot=item["is_pilot"],
        is_synthetic=item["is_synthetic"],
        updated_at=updated,  # type: ignore[arg-type]
    )


def parse_page(body: Any) -> tuple[list[Any], str]:
    """``(items, next_cursor)`` of an ``ErasureRequestPage``; ValueError when the page itself is malformed."""
    if not isinstance(body, dict) or set(body) != {"items", "next_cursor"}:
        raise ValueError("page_shape")
    items, cursor = body["items"], body["next_cursor"]
    if not isinstance(items, list) or not isinstance(cursor, str) or len(cursor) > MAX_CURSOR_LEN:
        raise ValueError("page_shape")
    if cursor and not re.match(r"^[!-~]+$", cursor):
        raise ValueError("page_cursor")
    return items, cursor


# ── the plan ─────────────────────────────────────────────────────────────────────────────────────────────────────
def entry(table: str, action: str, rows: int) -> dict[str, Any]:
    if not TABLE_RE.match(table):
        raise ValueError(f"not a table name: {table!r}")
    return {"table": table, "action": action, "rows": int(rows), "digest": None}


def plan_entries(
    erase_counts: Mapping[str, int] | None, *, live_keys: int, name_to_erase: int, invitations: int
) -> list[dict[str, Any]]:
    """The plan as contract table entries: every count of the agency erase (``crm.erase_subject('tenant')`` DRY RUN;
    None = the CRM holds no agency for the tenant), the keys the crypto-shred destroys, the agency row whose name is
    replaced, and the tenant's Content studio invitations (crm-067). Sorted by table and action."""
    out: list[dict[str, Any]] = []
    for key, rows in (erase_counts or {}).items():
        if key in NOT_TABLES:
            continue
        if key in DETACH_KEYS:
            out.append(entry(DETACH_KEYS[key], "detach", rows))
        else:
            out.append(entry(f"crm.{key}", "delete", rows))
    if erase_counts is not None:
        out.append(entry("crm.tenant_key", "shred", live_keys))
        out.append(entry("crm.tenant", "anonymise", name_to_erase))
    out.append(entry("crm.studio_invitation", "delete", invitations))
    return sorted(out, key=lambda e: (e["table"], e["action"]))


def plan_hash(tenant_id: uuid.UUID | str, round_: int, tables: Iterable[Mapping[str, Any]]) -> str:
    lines = sorted(f"{t['table']}|{t['action']}|{int(t['rows'])}|{t.get('digest') or ''}" for t in tables)
    head = f"{CONTRACT}|{tenant_id}|{int(round_)}"
    return hashlib.sha256("\n".join([head, *lines]).encode("utf-8")).hexdigest()


def totals(tables: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tables:
        out[str(t["action"])] = out.get(str(t["action"]), 0) + int(t["rows"])
    return out


# ── what is owed, and the guards ─────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Made:
    """A report already made for the request's current round (crm.tenant_erasure_report)."""

    mode: str
    outcome: str
    post_outcome: str | None


def next_mode(request: SagaRequest, made: Iterable[Made], now: dt.datetime) -> str | None:
    """The report the step owes CIS now, or None. Only an active step past its not_before acts (CIS decides the round);
    each mode once per round; a verify only after an apply that was ok and that CIS accepted (CIS refuses a verify
    before its own record of the ok apply)."""
    if request.step_status != "active" or now < request.not_before:
        return None
    by_mode = {m.mode: m for m in made}
    if request.phase == "dry_run":
        return None if "dry_run" in by_mode else "dry_run"
    if request.phase not in ("apply", "verify"):
        return None
    applied = by_mode.get("apply")
    if applied is None:
        return "apply"
    if applied.outcome == "ok" and applied.post_outcome == "accepted" and "verify" not in by_mode:
        return "verify"
    return None


def guard(
    request: SagaRequest,
    mode: str,
    *,
    environment: str,
    agency: Mapping[str, Any] | None,
    apply_enabled: bool,
    now: dt.datetime,
) -> str | None:
    """The first refusal code for ``mode`` (None = go on). Every mode: the pilot (the request's copy or the agency's own
    marker, D8). Apply only: outside prod a synthetic tenant only (the request's copy, and the agency's own marker when
    the CRM holds one), the Terraform kill switch, an approved hash for this round that has not expired."""
    if request.is_pilot or (agency is not None and agency.get("is_pilot")):
        return "pilot_refused"
    if mode != "apply":
        return None
    synthetic = request.is_synthetic and (agency is None or bool(agency.get("is_synthetic")))
    if environment != "prod" and not synthetic:
        return "not_synthetic"
    if not apply_enabled:
        return "apply_disabled"
    if request.approved_plan_hash is None:
        return "approval_missing"
    if request.approval_expires_at is None or now >= request.approval_expires_at:
        return "approval_expired"
    return None


# ── the report ───────────────────────────────────────────────────────────────────────────────────────────────────
def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def report_body(
    request: SagaRequest,
    *,
    report_id: uuid.UUID,
    mode: str,
    outcome: str,
    error_code: str | None = None,
    plan: str | None = None,
    tables: list[dict[str, Any]] | None = None,
    blockers: list[dict[str, Any]] | None = None,
    census: dict[str, int] | None = None,
    job_id: str | None = None,
    reported_at: dt.datetime,
) -> dict[str, Any]:
    """The contract ``ErasureReport`` (every field present, nullable where the contract says so)."""
    if mode not in MODES or outcome not in OUTCOMES:
        raise ValueError("mode or outcome outside the contract")
    if error_code is not None and not CODE_RE.match(error_code):
        raise ValueError("error_code outside the contract")
    if plan is not None and not HASH_RE.match(plan):
        raise ValueError("plan hash outside the contract")
    job = str(_uuid(job_id)) if _uuid(job_id) is not None else None
    return {
        "report_id": str(report_id),
        "request_id": str(request.request_id),
        "tenant_id": str(request.tenant_id),
        "app": APP,
        "round": request.round,
        "mode": mode,
        "status": outcome,
        "error_code": error_code,
        "plan_hash": None if mode == "verify" else plan,
        "tables": list(tables or [])[:500],
        "blockers": list(blockers or [])[:50],
        "census": census,
        "job_id": job,
        "reported_at": iso(reported_at),
    }
