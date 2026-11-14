"""The CRM settings registry behind ``/crm/ops/v1/settings`` (AA-1096 lane L4, crm-064; crm.ops.v1 1.13, 1.14), the
CRM tab of the CIS admin page Platform > Settings (relayed by the CIS admin API with the staff member's own WORKFORCE
token, as the L1 console and the app profile are).

- :func:`list_settings`: every CRM definition (the compiled defaults, equal to the migration seed) with the value in
  force and where it comes from (env / platform / default), bounds, the last decided platform change, the open platform
  change, the "overridden by env" and "not enforced yet" flags.
- :func:`history`: every value row of a key at one scope (the platform, or one agency), newest first, each with the
  value it replaced (the page's diff).
- :func:`propose`: a new value, checked against the definition first (422 with the reason), optimistic lock on the
  version the person saw (409 ``stale_version``), one open change per key and scope (409 ``open_change``).
  ``apply=True`` also approves it in the same transaction.
- Range groups (:data:`app.platform_settings.RANGE_GROUPS`: the new-agency default and the range an agency owner may
  pick, e.g. ``trash_days`` / ``_min`` / ``_max``): a change, and again its approval, is refused when the group would
  break ``min <= default <= max`` with the values in force at the same scope (422 ``range_inconsistent``); the keys of
  a group share one advisory lock, so two changes of one group never pass the check side by side.
- An agency's own value of a key whose env override is in force is refused (409 ``env_override_active``): the env var
  wins over every agency's value (the emergency override, tech lead S279 Q5), so the change would be recorded as
  approved and have no effect.
- :func:`decide`: approve or reject an open change. ``maker_checker``: the approver is not the proposer, unless
  ``crm.settings.two_person_approval`` is off (the dev phase, tech lead S279 Q2): then the administrator approves their
  own change (``self_approved``), and a high-impact one raises an AA ops e-mail (``setting_self_approved``). An approval
  whose ``effective_from`` has come supersedes the older approved rows of the same scope.

Scopes and contexts. crm.setting_value is FORCE RLS (crm-064): a platform row is read and written outside any agency
context (a pool connection, ``app.actor`` = ``platform:<staff sub>`` for the guard trigger), an agency's row inside that
agency's platform context (:func:`app.tenancy.platform_tx`, which also writes the agency's own audit event: the owner
sees in the access log that AA changed one of the agency's limits, ``setting_change`` with the row id; the key and the
value are in the row itself, never in the audit). The setting_value rows are append-only and keep who, when and why;
the CIS relay writes its own audit row (acp_shared.audit_log) for every relayed write.

The reader of this process is invalidated after an approval; other API tasks see it within one cache window (30 s).
Before crm-064 is applied every route answers 503 ``settings_unavailable``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from typing import Any

import asyncpg

from app import platform_settings as ps
from app.agencies import NEW_AGENCY_SETTINGS_SQL, new_agency_settings
from app.audit_export import registry
from app.errors import Conflict, Invalid, NotFound, ServiceError, Unavailable
from app.observability import ops_alerts
from app.platform_settings.reader import Definition
from app.tenancy import TENANT_STATUSES, platform_tx

log = logging.getLogger(__name__)

REASON_MIN = 20  # technical-constant: mirrors the crm-064 CHECK (one break-glass minimum for the platform)
EVIDENCE_MIN = 3  # technical-constant: mirrors the crm-064 CHECK setting_value_evidence_chk (after trimming)
EVIDENCE_MAX = 500  # technical-constant: mirrors the crm-064 CHECK setting_value_evidence_chk
# The clock skew between the CIS page, the relay and this API: an effective_from older than now minus this is refused.
EFFECTIVE_FROM_SKEW_S = 300  # technical-constant: clock skew allowance, not a policy
UNAVAILABLE = "settings_unavailable"

# Platform rows only (the RLS of a context without app.tenant_id shows nothing else).
LATEST_SQL = """
SELECT DISTINCT ON (v.key)
       v.id, v.key, v.tenant_id::text AS tenant_id, v.value::text AS value_text, v.version, v.status,
       v.effective_from, v.proposed_by, v.proposed_at, v.reason, v.evidence_ref, v.self_approved,
       v.decided_by, v.decided_at, v.decision_note
  FROM crm.setting_value v
 WHERE v.tenant_id IS NULL AND v.status IN ('approved', 'rejected', 'superseded')
 ORDER BY v.key, v.decided_at DESC
"""
OPEN_SQL = """
SELECT v.id, v.key, v.tenant_id::text AS tenant_id, v.value::text AS value_text, v.version, v.status,
       v.effective_from, v.proposed_by, v.proposed_at, v.reason, v.evidence_ref, v.self_approved,
       v.decided_by, v.decided_at, v.decision_note
  FROM crm.setting_value v
 WHERE v.tenant_id IS NULL AND v.status = 'proposed'
"""
# One agency's rows of the platform+tenant keys (read inside that agency's platform context: RLS shows only its rows):
# the approved value in force per key, and the open changes.
AGENCY_ROWS_SQL = """
SELECT * FROM (
  SELECT DISTINCT ON (v.key)
         v.id, v.key, v.tenant_id::text AS tenant_id, v.value::text AS value_text, v.version, v.status,
         v.effective_from, v.proposed_by, v.proposed_at, v.reason, v.evidence_ref, v.self_approved,
         v.decided_by, v.decided_at, v.decision_note
    FROM crm.setting_value v
   WHERE v.tenant_id = $1 AND v.key = ANY($2::text[]) AND v.status = 'approved' AND v.effective_from <= now()
   ORDER BY v.key, v.effective_from DESC, v.version DESC
) in_force
UNION ALL
SELECT v.id, v.key, v.tenant_id::text AS tenant_id, v.value::text AS value_text, v.version, v.status,
       v.effective_from, v.proposed_by, v.proposed_at, v.reason, v.evidence_ref, v.self_approved,
       v.decided_by, v.decided_at, v.decision_note
  FROM crm.setting_value v
 WHERE v.tenant_id = $1 AND v.key = ANY($2::text[]) AND v.status = 'proposed'
"""
AGENCY_NAME_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
HISTORY_SQL = """
SELECT v.id, v.key, v.tenant_id::text AS tenant_id, v.value::text AS value_text, v.version, v.status,
       v.effective_from, v.proposed_by, v.proposed_at, v.reason, v.evidence_ref, v.self_approved,
       v.decided_by, v.decided_at, v.decision_note FROM crm.setting_value v
 WHERE v.key = $1 AND v.tenant_id IS NOT DISTINCT FROM $2::uuid
 ORDER BY v.version DESC
 LIMIT 200
"""
CURRENT_SQL = """
SELECT version, value::text AS value_text FROM crm.setting_value
 WHERE key = $1 AND tenant_id IS NOT DISTINCT FROM $2::uuid AND status = 'approved' AND effective_from <= now()
 ORDER BY effective_from DESC, version DESC LIMIT 1
"""
MAX_VERSION_SQL = (
    "SELECT coalesce(max(version), 0) FROM crm.setting_value WHERE key = $1 AND tenant_id IS NOT DISTINCT FROM $2::uuid"
)
LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended($1, 64))"
ACTOR_SQL = "SELECT set_config('app.actor', $1, true)"
INSERT_SQL = """
INSERT INTO crm.setting_value AS v (key, tenant_id, value, version, effective_from, proposed_by, reason, evidence_ref)
VALUES ($1, $2::uuid, $3::jsonb, $4, coalesce($5, now()), $6, $7, $8)
RETURNING v.id, v.key, v.tenant_id::text AS tenant_id, v.value::text AS value_text, v.version, v.status,
       v.effective_from, v.proposed_by, v.proposed_at, v.reason, v.evidence_ref, v.self_approved,
       v.decided_by, v.decided_at, v.decision_note
"""
ROW_SQL = """
SELECT v.id, v.key, v.tenant_id::text AS tenant_id, v.value::text AS value_text, v.version, v.status,
       v.effective_from, v.proposed_by, v.proposed_at, v.reason, v.evidence_ref, v.self_approved,
       v.decided_by, v.decided_at, v.decision_note
  FROM crm.setting_value v
 WHERE v.id = $1
   FOR UPDATE
"""
DECIDE_SQL = """
UPDATE crm.setting_value AS v SET status = $2, decided_by = $3, decided_at = now(), decision_note = $4,
       self_approved = $5
 WHERE v.id = $1 AND v.status = 'proposed'
RETURNING v.id, v.key, v.tenant_id::text AS tenant_id, v.value::text AS value_text, v.version, v.status,
       v.effective_from, v.proposed_by, v.proposed_at, v.reason, v.evidence_ref, v.self_approved,
       v.decided_by, v.decided_at, v.decision_note
"""
PRIOR_SQL = """
SELECT value::text AS value_text FROM crm.setting_value
 WHERE key = $1 AND tenant_id IS NOT DISTINCT FROM $2::uuid AND status = 'approved' AND id <> $3
   AND effective_from <= now()
 ORDER BY effective_from DESC, version DESC LIMIT 1
"""
SUPERSEDE_SQL = """
UPDATE crm.setting_value SET status = 'superseded'
 WHERE key = $1 AND tenant_id IS NOT DISTINCT FROM $2::uuid AND status = 'approved' AND id <> $3
   AND $4::timestamptz <= now() AND effective_from <= $4::timestamptz
"""
# An approved row of the same scope already in force with a later effective_from: approving an older one would be
# recorded as approved and never take effect (the value in force is the newest effective_from).
LATER_IN_FORCE_SQL = """
SELECT 1 FROM crm.setting_value
 WHERE key = $1 AND tenant_id IS NOT DISTINCT FROM $2::uuid AND status = 'approved' AND id <> $3
   AND effective_from > $4::timestamptz AND effective_from <= now()
 LIMIT 1
"""
# The two-person flag in force, read in the decision transaction (never the process cache). A platform row: inside an
# agency's context it is read with app.tenant_id cleared for that one statement (the crm-064 trigger does the same).
TWO_PERSON_SQL = """
SELECT value::text AS value_text FROM crm.setting_value
 WHERE key = $1 AND tenant_id IS NULL AND status = 'approved' AND effective_from <= now()
 ORDER BY effective_from DESC, version DESC LIMIT 1
"""
# The values in force of a range group's keys at one scope (the change's sibling keys).
GROUP_SQL = """
SELECT DISTINCT ON (key) key, value::text AS value_text FROM crm.setting_value
 WHERE key = ANY($1::text[]) AND status = 'approved' AND tenant_id IS NOT DISTINCT FROM $2::uuid
   AND effective_from <= now()
 ORDER BY key, effective_from DESC, version DESC
"""
TENANT_GUC_SQL = "SELECT current_setting('app.tenant_id', true) AS t"
SET_TENANT_SQL = "SELECT set_config('app.tenant_id', $1, true)"


class SettingsUnavailable(Unavailable):
    """crm-064 is not applied (or the tables cannot be read)."""

    def __init__(self) -> None:
        super().__init__(UNAVAILABLE, "the CRM settings registry is not available yet (crm-064)")


_BY_STATUS: dict[int, type[ServiceError]] = {404: NotFound, 409: Conflict, 422: Invalid}


def _refused(status: int, code: str, message: str) -> ServiceError:
    return _BY_STATUS[status](code, message)


def _iso(v: Any) -> str | None:
    return v.astimezone(dt.UTC).isoformat() if isinstance(v, dt.datetime) else v


def _json(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def definition_out(d: Definition) -> dict[str, Any]:
    return {
        "key": d.key,
        "app": d.app,
        "area": d.area,
        "label": d.label,
        "description": d.description,
        "value_type": d.value_type,
        "unit": d.unit,
        "min_value": d.min_value,
        "max_value": d.max_value,
        "allowed_values": list(d.allowed_values) if d.allowed_values is not None else None,
        "default_value": d.default_value,
        "scope_kind": d.scope_kind,
        "impact": d.impact,
        "approval": d.approval,
        "evidence_required": d.evidence_required,
        "env_override": d.env_override,
        "cache_s": d.cache_s,
        "enforced": d.enforced,
        # what a change must carry (the crm-064 CHECKs): the CIS page checks these before it sends
        "reason_min": REASON_MIN,
        "evidence_min": EVIDENCE_MIN,
        "evidence_max": EVIDENCE_MAX,
        "guide": ps.guide(d.key),  # the operator's guide, EN + VI (CIS 416 shape; None = none yet)
    }


def row_out(r: Any) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "key": r["key"],
        "tenant_id": r["tenant_id"],
        "value": _json(r["value_text"]),
        "version": r["version"],
        "status": r["status"],
        "effective_from": _iso(r["effective_from"]),
        "proposed_by": r["proposed_by"],
        "proposed_at": _iso(r["proposed_at"]),
        "reason": r["reason"],
        "evidence_ref": r["evidence_ref"],
        "self_approved": r["self_approved"],
        "decided_by": r["decided_by"],
        "decided_at": _iso(r["decided_at"]),
        "decision_note": r["decision_note"],
    }


def _definition(key: str) -> Definition:
    try:
        return ps.definition(key)
    except KeyError:
        raise _refused(404, "unknown_setting", f"no CRM setting {key}") from None


async def list_settings(pool: asyncpg.Pool, *, area: str | None = None, staff_sub: str = "ops") -> dict[str, Any]:
    defs = [ps.definition(k) for k in ps.keys()]
    defs = [d for d in defs if area is None or d.area == area]
    try:
        await ps.READER.refresh(force=True, pool=pool)
        async with pool.acquire() as conn:
            latest = {r["key"]: row_out(r) for r in await conn.fetch(LATEST_SQL)}
            pending: dict[str, list[dict[str, Any]]] = {}
            for r in await conn.fetch(OPEN_SQL):
                pending.setdefault(r["key"], []).append(row_out(r))
        agency_rows = await _agency_rows(pool, [d.key for d in defs if d.tenant_scoped], staff_sub=staff_sub)
    except (asyncpg.UndefinedTableError, asyncpg.InsufficientPrivilegeError):
        raise SettingsUnavailable() from None
    rows = []
    for d in defs:
        overrides, agency_pending = agency_rows.get(d.key, ([], []))
        res = ps.resolve(d.key)
        _value, env_ignored, env_set = ps.READER.env_value(d)
        rows.append(
            {
                **definition_out(d),
                "value": res.value,
                "source": res.source,
                "version": res.version,
                "overridden_by_env": res.source == "env",
                "env_ignored": env_ignored if env_set else None,
                "refused": res.refused,
                "last_change": latest.get(d.key),
                "pending": pending.get(d.key, []),
                # a platform+tenant key: the agencies with their own value in force, and their open changes (decide
                # one with its tenant_id); empty for a platform-only key
                "agency_overrides": overrides,
                "agency_pending": agency_pending,
            }
        )
    return {
        "app": "crm",
        "areas": sorted({d.area for d in defs}),
        "settings": rows,
        "two_person_approval": bool(ps.get(ps.TWO_PERSON_APPROVAL)),
        "reader_error": ps.READER.last_error,
    }


async def _agency_rows(
    pool: asyncpg.Pool, keys: list[str], *, staff_sub: str
) -> dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    """``{key: (overrides, open changes)}`` of every agency that has its own rows of the platform+tenant ``keys``.

    crm.setting_value is FORCE RLS: an agency's rows are seen only in its own context, so this opens one platform
    context per agency of the registry (the L1 agency list does the same) and reads its rows of these keys only. An
    agency with rows gets one ``ops_read`` event in its audit (reason ``settings``); one without rows none (nothing of
    it was shown). A trashed or unreadable agency is skipped, never guessed."""
    out: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    if not keys:
        return out
    for agency in sorted(await registry.agencies(pool), key=lambda a: str(a.tenant_id)):
        try:
            async with platform_tx(pool, agency.tenant_id, actor=_actor(staff_sub), statuses=TENANT_STATUSES) as ctx:
                found = await ctx.conn.fetch(AGENCY_ROWS_SQL, agency.tenant_id, keys)
                if not found:
                    continue
                name = await ctx.conn.fetchval(AGENCY_NAME_SQL, agency.tenant_id)
                ctx.audit("ops_read", "allowed", "tenant", str(agency.tenant_id), reason="settings")
        except (NotFound, Conflict):
            continue  # trashed or not readable as an agency now
        for r in found:
            row = {**row_out(r), "agency_name": name}
            overrides, open_changes = out.setdefault(r["key"], ([], []))
            (open_changes if r["status"] == "proposed" else overrides).append(row)
    return out


def _with_previous(
    rows: list[dict[str, Any]], d: Definition, platform_rows: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Each row with the value it replaced (the page's diff). For an agency's rows, the value before its first own
    value is the platform value in force at that row's effective_from (``platform_rows``: the platform history), not
    the compiled default."""
    previous: Any = d.default_value
    first = True
    for row in reversed(rows):  # oldest first: each row against the approved value in force before it
        if first and platform_rows is not None:
            previous = _platform_value_at(platform_rows, row["effective_from"], d.default_value)
        row["previous_value"] = previous
        if row["status"] in ("approved", "superseded"):
            previous = row["value"]
            first = False
    return rows


def _platform_value_at(platform_rows: list[dict[str, Any]], at: str | None, default: Any) -> Any:
    """The platform value in force at ``at`` (ISO): the approved or superseded platform row with the newest
    effective_from not after it (ties: the higher version), else the compiled default. An env override is not seeded:
    no record says when it was in force, and an agency's own value cannot be written while one is
    (``env_override_active``)."""
    if at is None:
        return default
    limit = dt.datetime.fromisoformat(at)
    best: tuple[dt.datetime, int, Any] | None = None
    for row in platform_rows:
        if row["status"] not in ("approved", "superseded") or row["effective_from"] is None:
            continue
        when = dt.datetime.fromisoformat(row["effective_from"])
        if when <= limit and (best is None or (when, row["version"]) > best[:2]):
            best = (when, row["version"], row["value"])
    return default if best is None else best[2]


async def history(
    pool: asyncpg.Pool, key: str, tenant_id: uuid.UUID | None = None, *, staff_sub: str
) -> dict[str, Any]:
    d = _definition(key)
    try:
        if tenant_id is None:
            async with pool.acquire() as conn:
                rows = [row_out(r) for r in await conn.fetch(HISTORY_SQL, key, None)]
        else:
            if not d.tenant_scoped:
                raise _refused(422, "not_tenant_scoped", f"{d.label} has no per-agency value")
            # the platform history first (outside any agency context: RLS), for the value an agency's first own value
            # replaced
            async with pool.acquire() as conn:
                platform_rows = [row_out(r) for r in await conn.fetch(HISTORY_SQL, key, None)]
            async with platform_tx(pool, tenant_id, actor=_actor(staff_sub), statuses=TENANT_STATUSES) as ctx:
                rows = [row_out(r) for r in await ctx.conn.fetch(HISTORY_SQL, key, tenant_id)]
                ctx.audit("ops_read", "allowed", "tenant", str(tenant_id), reason="settings")
    except (asyncpg.UndefinedTableError, asyncpg.InsufficientPrivilegeError):
        raise SettingsUnavailable() from None
    return {
        "key": key,
        "tenant_id": None if tenant_id is None else str(tenant_id),
        "definition": definition_out(d),
        "history": _with_previous(rows, d, None if tenant_id is None else platform_rows),
    }


def _actor(staff_sub: str) -> str:
    return f"platform:{staff_sub}"


def check_value(d: Definition, value: Any) -> None:
    problem = ps.validate(d, value)
    if problem:
        raise _refused(422, "out_of_bounds", f"{d.label}: {problem}")


def check_env_override(d: Definition, tenant_id: uuid.UUID | str | None) -> None:
    """An agency's own value has no effect while the key's env override is in force (the env var wins over every
    agency, :func:`app.platform_settings.resolve`): refuse it instead of recording a change that does nothing."""
    if tenant_id is None:
        return
    res = ps.READER.resolve(d.key)
    if res.source == "env":
        raise _refused(
            409,
            "env_override_active",
            f"{d.label}: the env var {res.env_name} overrides every agency's value in this environment; remove it "
            "from the environment first",
        )


def check_effective_from(effective_from: dt.datetime | None, now: dt.datetime | None = None) -> None:
    """A change takes effect now (None) or later, never in the past: the value in force is the approved row with the
    newest effective_from, so a backdated row behind the one in force would be recorded as approved and never apply."""
    if effective_from is None:
        return
    when = effective_from if effective_from.tzinfo else effective_from.replace(tzinfo=dt.UTC)
    now = now or dt.datetime.now(dt.UTC)
    if when < now - dt.timedelta(seconds=EFFECTIVE_FROM_SKEW_S):
        raise _refused(
            422, "effective_from_in_past", "a change takes effect now or later: leave the date empty for now"
        )


def _lock_scope(d: Definition, tenant_id: uuid.UUID | str | None) -> str:
    group = ps.range_group(d.key)
    name = f"range:{group[0]}" if group else d.key
    return name if tenant_id is None else f"{name}@{tenant_id}"


async def check_range(conn: asyncpg.Connection, d: Definition, value: Any, tenant_id: uuid.UUID | str | None) -> None:
    """``value`` for ``d.key`` keeps its range group ``min <= default <= max`` with the values in force at the same
    scope (the caller holds the group lock, :func:`_lock_scope`)."""
    group = ps.range_group(d.key)
    if group is None:
        return
    values = {k: ps.definition(k).default_value for k in group}
    tid = None if tenant_id is None else uuid.UUID(str(tenant_id))
    for r in await conn.fetch(GROUP_SQL, list(group), tid):
        values[r["key"]] = json.loads(r["value_text"])
    values[d.key] = value
    default, low, high = (values[k] for k in group)
    if not low <= default <= high:
        labels = [ps.definition(k).label for k in group]
        raise _refused(
            422,
            "range_inconsistent",
            f"{d.label}: {labels[1]} ({low}) <= {labels[0]} ({default}) <= {labels[2]} ({high}) must hold; change the "
            "other value of the range first",
        )


async def propose(
    pool: asyncpg.Pool,
    key: str,
    *,
    value: Any,
    reason: str,
    staff_sub: str,
    base_version: int | None,
    evidence_ref: str | None = None,
    effective_from: dt.datetime | None = None,
    tenant_id: uuid.UUID | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    d = _definition(key)
    check_value(d, value)
    reason = (reason or "").strip()
    if len(reason) < REASON_MIN:
        raise _refused(422, "reason_too_short", f"give a reason of at least {REASON_MIN} characters")
    evidence_ref = (evidence_ref or "").strip() or None
    if d.evidence_required and not evidence_ref:
        raise _refused(422, "evidence_required", f"{d.label} needs an evidence link")
    if evidence_ref is not None and not EVIDENCE_MIN <= len(evidence_ref) <= EVIDENCE_MAX:
        raise _refused(
            422,
            "evidence_invalid",
            f"the evidence is a link or a reference of {EVIDENCE_MIN} to {EVIDENCE_MAX} characters",
        )
    check_effective_from(effective_from)
    if tenant_id is not None and not d.tenant_scoped:
        raise _refused(422, "not_tenant_scoped", f"{d.label} has no per-agency value")
    check_env_override(d, tenant_id)
    actor = _actor(staff_sub)
    try:
        if tenant_id is None:
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(ACTOR_SQL, actor)
                out = await _propose(conn, d, value, reason, actor, base_version, evidence_ref, effective_from, None,
                                     apply)  # fmt: skip
        else:
            async with platform_tx(pool, tenant_id, actor=actor, statuses=TENANT_STATUSES) as ctx:
                # the agency's row needs its settings row (the one foreign key, crm-064; the erase cascades from it)
                await ctx.conn.execute(NEW_AGENCY_SETTINGS_SQL, *new_agency_settings())
                out = await _propose(ctx.conn, d, value, reason, actor, base_version, evidence_ref, effective_from,
                                     tenant_id, apply)  # fmt: skip
                ctx.audit("setting_change", "allowed", "platform_setting", out["id"], reason=out["status"])
    except (asyncpg.UndefinedTableError, asyncpg.InsufficientPrivilegeError):
        raise SettingsUnavailable() from None
    _after_decision(d, out)
    return out


async def _propose(
    conn: asyncpg.Connection,
    d: Definition,
    value: Any,
    reason: str,
    actor: str,
    base_version: int | None,
    evidence_ref: str | None,
    effective_from: dt.datetime | None,
    tenant_id: uuid.UUID | None,
    apply: bool,
) -> dict[str, Any]:
    await conn.execute(LOCK_SQL, _lock_scope(d, tenant_id))
    current = await conn.fetchrow(CURRENT_SQL, d.key, tenant_id)
    current_version = current["version"] if current else 0
    if (base_version or 0) != current_version:
        raise _refused(
            409, "stale_version", f"{d.label} changed since you opened it (version {current_version}); reload"
        )
    await check_range(conn, d, value, tenant_id)
    version = int(await conn.fetchval(MAX_VERSION_SQL, d.key, tenant_id)) + 1
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                INSERT_SQL, d.key, tenant_id, json.dumps(value), version, effective_from, actor, reason, evidence_ref
            )
    except asyncpg.UniqueViolationError:
        raise _refused(
            409, "open_change", f"{d.label} already has an open change: approve or reject it first"
        ) from None
    out = row_out(row)
    if apply:
        out = await _decide(conn, d, out, actor=actor, approve=True, note=None)
    return out


async def decide(
    pool: asyncpg.Pool,
    value_id: uuid.UUID,
    *,
    staff_sub: str,
    approve: bool,
    note: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Approve or reject the open change ``value_id``. An agency's change is only visible inside its own context, so
    the caller names the agency (``tenant_id``, from the history row)."""
    actor = _actor(staff_sub)
    try:
        if tenant_id is None:
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(ACTOR_SQL, actor)
                out, d = await _decide_row(conn, value_id, actor, approve, note)
        else:
            async with platform_tx(pool, tenant_id, actor=actor, statuses=TENANT_STATUSES) as ctx:
                out, d = await _decide_row(ctx.conn, value_id, actor, approve, note)
                ctx.audit("setting_change", "allowed", "platform_setting", out["id"], reason=out["status"])
    except (asyncpg.UndefinedTableError, asyncpg.InsufficientPrivilegeError):
        raise SettingsUnavailable() from None
    _after_decision(d, out)
    return out


async def _decide_row(
    conn: asyncpg.Connection, value_id: uuid.UUID, actor: str, approve: bool, note: str | None
) -> tuple[dict[str, Any], Definition]:
    row = await conn.fetchrow(ROW_SQL, value_id)
    if row is None:
        raise _refused(404, "unknown_change", "no open change with this id at this scope")
    proposal = row_out(row)
    if proposal["status"] != "proposed":
        raise _refused(409, "already_decided", f"this change is already {proposal['status']}")
    d = _definition(proposal["key"])
    return await _decide(conn, d, proposal, actor=actor, approve=approve, note=note), d


async def two_person_on(conn: asyncpg.Connection) -> bool:
    """Is two-person approval on, read in the caller's transaction. Fails closed: an unreadable row or a value that is
    not a boolean counts as on."""
    try:
        async with conn.transaction():  # a savepoint: a failed read leaves the decision transaction usable
            agency = await conn.fetchval(TENANT_GUC_SQL)
            if agency:
                await conn.execute(SET_TENANT_SQL, "")
            row = await conn.fetchrow(TWO_PERSON_SQL, ps.TWO_PERSON_APPROVAL)
            if agency:
                await conn.execute(SET_TENANT_SQL, agency)
    except Exception as exc:  # noqa: BLE001 — fail closed
        log.warning("settings_two_person_read_failed error=%s", type(exc).__name__)
        return True
    if row is None:
        return ps.definition(ps.TWO_PERSON_APPROVAL).default_value is not False
    try:
        return json.loads(row["value_text"]) is not False
    except (TypeError, ValueError):
        return True


async def _decide(
    conn: asyncpg.Connection, d: Definition, proposal: dict[str, Any], *, actor: str, approve: bool, note: str | None
) -> dict[str, Any]:
    note = (note or "").strip() or None
    if not approve and (note is None or len(note) < 3):
        raise _refused(422, "note_required", "say why the change is rejected")
    if approve:  # the scope may have moved since the proposal: check again under the group lock
        check_env_override(d, proposal["tenant_id"])
        await conn.execute(LOCK_SQL, _lock_scope(d, proposal["tenant_id"]))
        await check_range(conn, d, proposal["value"], proposal["tenant_id"])
        scope = None if proposal["tenant_id"] is None else uuid.UUID(proposal["tenant_id"])
        effective = dt.datetime.fromisoformat(proposal["effective_from"]) if proposal["effective_from"] else None
        args = (d.key, scope, uuid.UUID(proposal["id"]), effective)
        if effective is not None and await conn.fetchval(LATER_IN_FORCE_SQL, *args):
            raise _refused(
                409,
                "superseded_by_later",
                f"{d.label}: a value with a later start date is already in force, so this change would never apply; "
                "reject it and propose again",
            )
    self_approved = False
    if approve and actor == proposal["proposed_by"] and d.approval == "maker_checker":
        # The flag is maker_checker itself (crm-064): while it is on, the one person it checks cannot turn it off.
        if await two_person_on(conn):
            raise _refused(
                409, "second_person_required", f"{d.label} needs a second person to approve (two-person approval)"
            )
        self_approved = True
    row = await conn.fetchrow(
        DECIDE_SQL, uuid.UUID(proposal["id"]), "approved" if approve else "rejected", actor, note, self_approved
    )
    if row is None:
        raise _refused(409, "already_decided", "this change was decided meanwhile")
    out = row_out(row)
    tenant = None if out["tenant_id"] is None else uuid.UUID(out["tenant_id"])
    if approve:
        await conn.execute(SUPERSEDE_SQL, d.key, tenant, row["id"], row["effective_from"])
        if self_approved and d.impact == "high":
            ops_alerts.emit("setting_self_approved", out["id"], key=d.key, version=out["version"])
    log.info(
        "crm_setting_decided key=%s status=%s version=%s self_approved=%s agency=%s",
        d.key,
        out["status"],
        out["version"],
        self_approved,
        out["tenant_id"] is not None,
    )
    return out


def _after_decision(d: Definition, out: dict[str, Any]) -> None:
    if out["status"] != "approved":
        return
    if out["tenant_id"] is None:
        ps.invalidate()
    else:
        ps.forget_tenant(out["tenant_id"])


__all__ = [
    "SettingsUnavailable",
    "check_effective_from",
    "check_env_override",
    "check_range",
    "check_value",
    "decide",
    "definition_out",
    "history",
    "list_settings",
    "propose",
    "row_out",
    "two_person_on",
]
