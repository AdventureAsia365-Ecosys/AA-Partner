"""The agency's access log (A-18 Settings > Data & privacy, design v3 §4.9 "agency page: 12 months in the app, plus
download"; card CRM-12). Helpers that run inside an open :func:`app.tenancy.tenant_tx`.

- **Owner only** (permission matrix, crm-design-final §3.6: "Access log: owner yes; agent trip Activity only;
  assistant no"). Anyone else gets 403 ``owner_only`` and a ``denied`` event that survives the refusal.
- **Filters** of WF-8: the actor groups ``user`` (members and travellers), ``ai``, ``aa_ops`` (AA ops and staff),
  ``aa_support``, ``aa_emergency`` (break-glass) and ``system``; plus action, outcome, member, object, and a date range
  inside the last 12 months (the database keeps 12 months; older months live in the archive only).
- **Paging**: newest first, keyset on ``(at, event_id)``; the cursor is opaque base64 of the last row's keys.
- **Download**: CSV (spreadsheet-safe) or JSON lines of the same filtered rows, at most :data:`EXPORT_MAX_ROWS`.
- Viewing the log and downloading it are audited too (``audit_view`` / ``audit_export``, object ``audit_log``).

Everything here is codes and random ids (the table's CHECKs): the page never decrypts a traveller's name; it links
an object id to its page instead, where the usual reveal rules apply.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from app.errors import Forbidden, Invalid
from app.tenancy import TenantContext

RETENTION_MONTHS = 12
PAGE_MAX = 100
EXPORT_MAX_ROWS = 50_000
ACTOR_GROUPS: dict[str, tuple[str, ...]] = {
    "user": ("user", "traveler"),
    "ai": ("ai_agent",),
    "aa_ops": ("aa_ops", "aa_staff"),
    "aa_support": ("support",),
    "aa_emergency": ("breakglass",),
    "system": ("system",),
}
GROUP_OF = {kind: group for group, kinds in ACTOR_GROUPS.items() for kind in kinds}
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_ID = re.compile(r"^[A-Za-z0-9:_-]{1,80}$")
EXPORT_FORMATS = ("csv", "jsonl")
CSV_COLUMNS = (
    "at",
    "actor_kind",
    "actor_ref",
    "action",
    "outcome",
    "object_kind",
    "object_id",
    "fields",
    "reason",
    "object_ids",
    "request_id",
    "event_id",
)
SELECT_COLUMNS = (
    "event_id, at, actor_kind, actor_ref, action, outcome, object_kind, object_id, fields, reason, request_id, "
    "object_ids"
)
MEMBER_ROLES_SQL = "SELECT member_id, role FROM crm.member"


@dataclass(frozen=True)
class AuditFilter:
    actor: str | None = None
    action: str | None = None
    outcome: str | None = None
    member_id: uuid.UUID | None = None
    object_kind: str | None = None
    object_id: str | None = None
    date_from: date | None = None
    date_to: date | None = None

    def __post_init__(self) -> None:
        if self.actor is not None and self.actor not in ACTOR_GROUPS:
            raise Invalid("invalid_actor")
        for value in (self.action, self.object_kind):
            if value is not None and not _CODE.match(value):
                raise Invalid("invalid_code")
        if self.outcome is not None and self.outcome not in ("allowed", "denied"):
            raise Invalid("invalid_outcome")
        if self.object_id is not None and not _ID.match(self.object_id):
            raise Invalid("invalid_object_id")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise Invalid("invalid_range")


def require_owner(ctx: TenantContext, action: str) -> None:
    """Owner only; anyone else leaves a ``denied`` event (committed by tenant_tx) and gets 403 ``owner_only``."""
    if ctx.member_role != "owner":
        ctx.audit(action, "denied", "audit_log", reason="owner_only")
        raise Forbidden("owner_only", "only the agency owner sees the access log")


def window_start(now: datetime) -> datetime:
    """The oldest instant the page shows: 365 days back (the partitions keep at least 12 whole months)."""
    return now.astimezone(UTC) - timedelta(days=365)


def _where(f: AuditFilter, now: datetime, params: list[Any]) -> str:
    clauses = []

    def bind(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    oldest = window_start(now)
    if f.date_from is not None:
        oldest = max(oldest, datetime.combine(f.date_from, time.min, tzinfo=UTC))
    clauses.append(f"at >= {bind(oldest)}")
    if f.date_to is not None:
        clauses.append(f"at < {bind(datetime.combine(f.date_to + timedelta(days=1), time.min, tzinfo=UTC))}")
    if f.actor is not None:
        clauses.append(f"actor_kind = ANY({bind(list(ACTOR_GROUPS[f.actor]))}::text[])")
    if f.action is not None:
        clauses.append(f"action = {bind(f.action)}")
    if f.outcome is not None:
        clauses.append(f"outcome = {bind(f.outcome)}")
    if f.member_id is not None:
        clauses.append(f"actor_ref = {bind(f'member:{f.member_id}')}")
    if f.object_kind is not None:
        clauses.append(f"object_kind = {bind(f.object_kind)}")
    if f.object_id is not None:
        clauses.append(f"(object_id = {bind(f.object_id)} OR {bind(f.object_id)} = ANY(object_ids))")
    return " AND ".join(clauses)


def _select(where: str, order: str, limit_param: int) -> str:
    """The page / export query. ``where`` comes from :func:`_where` (fixed fragments, every value a bind parameter)
    and ``order`` from the two literals below; nothing from the request is ever spliced in."""
    if order not in ("at DESC, event_id DESC", "at, event_id"):
        raise ValueError("fixed orders only")
    return f"SELECT {SELECT_COLUMNS} FROM crm.audit_event WHERE {where} ORDER BY {order} LIMIT ${limit_param}"  # noqa: S608


def encode_cursor(at: datetime, event_id: uuid.UUID) -> str:
    raw = f"{at.astimezone(UTC).isoformat()}|{event_id}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
        at, event_id = raw.split("|", 1)
        parsed = datetime.fromisoformat(at)
        if parsed.tzinfo is None:
            raise ValueError("naive")
        return parsed, uuid.UUID(event_id)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise Invalid("invalid_cursor") from None


def _member_id(actor_ref: str) -> str | None:
    if actor_ref.startswith("member:"):
        candidate = actor_ref.removeprefix("member:")
        try:
            return str(uuid.UUID(candidate))
        except ValueError:
            return None
    return None


def _view(row: Any, roles: dict[str, str]) -> dict[str, Any]:
    member = _member_id(row["actor_ref"])
    return {
        "event_id": str(row["event_id"]),
        "at": row["at"].astimezone(UTC).isoformat(),
        "actor_kind": row["actor_kind"],
        "actor_group": GROUP_OF.get(row["actor_kind"], "system"),
        "actor_ref": row["actor_ref"],
        "member_id": member,
        "member_role": roles.get(member) if member else None,
        "action": row["action"],
        "outcome": row["outcome"],
        "object_kind": row["object_kind"],
        "object_id": row["object_id"],
        "fields": list(row["fields"]),
        "reason": row["reason"],
        "object_ids": list(row["object_ids"]),
    }


async def _roles(ctx: TenantContext) -> dict[str, str]:
    return {str(r["member_id"]): r["role"] for r in await ctx.conn.fetch(MEMBER_ROLES_SQL)}


async def list_events(
    ctx: TenantContext, f: AuditFilter, *, cursor: str | None, limit: int, now: datetime | None = None
) -> dict[str, Any]:
    require_owner(ctx, "audit_view")
    if not 1 <= limit <= PAGE_MAX:
        raise Invalid("invalid_limit")
    now = now or datetime.now(UTC)
    params: list[Any] = []
    where = _where(f, now, params)
    if cursor:
        at, event_id = decode_cursor(cursor)
        params.extend([at, event_id])
        where += f" AND (at, event_id) < (${len(params) - 1}, ${len(params)})"
    params.append(limit + 1)
    rows = await ctx.conn.fetch(_select(where, "at DESC, event_id DESC", len(params)), *params)
    roles = await _roles(ctx)
    page = rows[:limit]
    ctx.audit("audit_view", "allowed", "audit_log")
    return {
        "events": [_view(r, roles) for r in page],
        "next_cursor": encode_cursor(page[-1]["at"], page[-1]["event_id"]) if len(rows) > limit else None,
        "retention_months": RETENTION_MONTHS,
        "oldest": window_start(now).isoformat(),
        "actor_groups": list(ACTOR_GROUPS),
    }


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        value = ";".join(str(v) for v in value)
    text = str(value)
    # Spreadsheet formula guard (codes and ids cannot start with these today; the guard keeps it that way).
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


async def export_events(
    ctx: TenantContext, f: AuditFilter, fmt: str, *, now: datetime | None = None
) -> tuple[bytes, int]:
    """The filtered rows as CSV or JSON lines (oldest first), and the row count. 422 ``export_too_large`` beyond
    :data:`EXPORT_MAX_ROWS` (narrow the dates)."""
    require_owner(ctx, "audit_export")
    if fmt not in EXPORT_FORMATS:
        raise Invalid("invalid_format")
    now = now or datetime.now(UTC)
    params: list[Any] = []
    where = _where(f, now, params)
    params.append(EXPORT_MAX_ROWS + 1)
    rows = await ctx.conn.fetch(_select(where, "at, event_id", len(params)), *params)
    if len(rows) > EXPORT_MAX_ROWS:
        raise Invalid("export_too_large", f"more than {EXPORT_MAX_ROWS} events: choose a shorter date range")
    views = [_view(r, {}) | {"request_id": r["request_id"]} for r in rows]
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(CSV_COLUMNS)
        for v in views:
            writer.writerow([_cell(v[c]) for c in CSV_COLUMNS])
        body = buf.getvalue().encode("utf-8")
    else:
        body = b"".join(
            json.dumps({c: v[c] for c in CSV_COLUMNS}, sort_keys=True, ensure_ascii=True).encode("ascii") + b"\n"
            for v in views
        )
    ctx.audit("audit_export", "allowed", "audit_log", reason=f"format_{fmt}")
    return body, len(views)


def record_sign_in(ctx: TenantContext) -> dict[str, Any]:
    """Design §4.9 "sign-ins": the web calls this once when a session opens an agency (picked or switched)."""
    ctx.audit("sign_in", "allowed", "member", str(ctx.member_id))
    return {"recorded": True}
