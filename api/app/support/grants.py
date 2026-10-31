"""The agency side of AA support access (AA-971, design v3 WF-12 "AA support access", §2.3 "Support banner").

``GET /crm/v1/support-access`` (every role): the owner's grant if one is live, the sessions live now (support and
break-glass: the banner), the sessions of the last 90 days (who: a short staff id; the ticket; the reason code of a
break-glass; when), and whether the caller may change it. ``POST /crm/v1/support-access`` (owner): grant a window of
1 to 7 days (default 1), read only, show identity or not. ``POST /crm/v1/support-access/revoke`` (owner, "End now"):
the grant ends and the live support session is revoked at once (the next staff read is 403). A break-glass session is
not the owner's to end (an emergency); it ends by itself within the hour.

Rows whose window passed stay ``active`` in the table until someone meets them (crm-033 lets any actor mark them
expired once passed); this module treats them as expired and marks them before a new grant.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import asyncpg

from app.errors import Conflict
from app.tenancy import TenantContext

DAYS_MIN, DAYS_MAX, DAYS_DEFAULT = 1, 7, 1
HISTORY_DAYS = 90
HISTORY_LIMIT = 20

GRANT_SQL = """
SELECT support_grant_id, show_identity, starts_at, expires_at, granted_by_member_id, expires_at <= now() AS expired
  FROM crm.support_grant WHERE status = 'active'
"""
GRANT_LOCK_SQL = GRANT_SQL + " FOR UPDATE"
EXPIRE_GRANTS_SQL = """
UPDATE crm.support_grant SET status = 'expired', ended_at = now(), ended_by = $1
 WHERE status = 'active' AND expires_at <= now()
"""
INSERT_SQL = """
INSERT INTO crm.support_grant (expires_at, granted_by_member_id, show_identity)
VALUES (now() + make_interval(days => $1), $2, $3)
RETURNING support_grant_id
"""
REVOKE_SQL = """
UPDATE crm.support_grant SET status = 'revoked', ended_at = now(), ended_by = $2 WHERE support_grant_id = $1
"""
REVOKE_SESSIONS_SQL = """
UPDATE crm.support_session SET status = 'revoked', ended_at = now(), ended_by = $2
 WHERE support_grant_id = $1 AND status = 'active'
RETURNING support_session_id
"""
SESSIONS_SQL = """
SELECT s.support_session_id, s.kind, s.status, s.staff_sub, s.ticket_ref, s.reason_code, s.show_identity,
       s.started_at, s.expires_at, s.ended_at, s.owner_notified_at,
       s.status = 'active' AND s.expires_at > now()
         AND (s.kind = 'breakglass' OR (g.status = 'active' AND g.expires_at > now())) AS live
  FROM crm.support_session s
  LEFT JOIN crm.support_grant g ON g.support_grant_id = s.support_grant_id
 WHERE s.started_at > now() - make_interval(days => $1)
 ORDER BY s.started_at DESC, s.support_session_id
 LIMIT $2
"""


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def staff_ref(sub: str) -> str:
    """A short, stable id of the AA staff member for the agency (no name, no e-mail: the CRM never holds them)."""
    return f"aa-{sub.replace('-', '')[:8]}"


def session_view(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    status = row["status"]
    if status == "active" and not row["live"]:
        status = "expired"  # its window, or its grant's, passed: shown as what it is
    return {
        "session_id": str(row["support_session_id"]),
        "kind": row["kind"],
        "status": status,
        "staff_ref": staff_ref(row["staff_sub"]),
        "ticket_ref": row["ticket_ref"],
        "reason_code": row["reason_code"],
        "show_identity": bool(row["show_identity"]),
        "started_at": _iso(row["started_at"]),
        "expires_at": _iso(row["expires_at"]),
        "ended_at": _iso(row["ended_at"]),
        "owner_notified": row["owner_notified_at"] is not None,
    }


async def support_access(ctx: TenantContext) -> dict[str, Any]:
    grant = await ctx.conn.fetchrow(GRANT_SQL)
    rows = await ctx.conn.fetch(SESSIONS_SQL, HISTORY_DAYS, HISTORY_LIMIT)
    sessions = [session_view(r) for r in rows]
    live_grant = grant is not None and not grant["expired"]
    return {
        "grant": (
            {
                "grant_id": str(grant["support_grant_id"]),
                "show_identity": bool(grant["show_identity"]),
                "starts_at": _iso(grant["starts_at"]),
                "expires_at": _iso(grant["expires_at"]),
                "granted_by_member_id": str(grant["granted_by_member_id"]),
            }
            if live_grant
            else None
        ),
        "active_sessions": [s for s in sessions if s["status"] == "active"],
        "recent_sessions": sessions,
        "can_manage": ctx.member_role == "owner",
        "days_min": DAYS_MIN,
        "days_max": DAYS_MAX,
        "days_default": DAYS_DEFAULT,
    }


def _actor(ctx: TenantContext) -> str:
    return f"member:{ctx.member_id}"


async def grant_access(ctx: TenantContext, *, days: int, show_identity: bool) -> dict[str, Any]:
    """The owner allows AA support for ``days`` (1..7) from now. 409 ``support_grant_active`` while a live grant
    exists (end it first: a grant's window and scope never change, crm-033)."""
    if not DAYS_MIN <= days <= DAYS_MAX:
        raise ValueError("days out of range")  # the route model bounds it; never reached from a request
    await ctx.conn.execute(EXPIRE_GRANTS_SQL, _actor(ctx))
    if await ctx.conn.fetchrow(GRANT_LOCK_SQL) is not None:
        raise Conflict("support_grant_active", "AA support access is already allowed; end it first")
    try:
        grant_id: uuid.UUID = await ctx.conn.fetchval(INSERT_SQL, days, ctx.member_id, show_identity)
    except asyncpg.UniqueViolationError as exc:  # a second owner granted at the same moment
        raise Conflict("support_grant_active", "AA support access is already allowed; end it first") from exc
    ctx.audit(
        "support_grant",
        "allowed",
        "support_grant",
        str(grant_id),
        reason=f"days_{days}",
        fields=("show_identity",) if show_identity else (),
    )
    return await support_access(ctx)


async def revoke_access(ctx: TenantContext) -> dict[str, Any]:
    """ "End now": the grant ends and its live support session is revoked. 409 ``no_support_grant`` when none is
    active (an expired one is marked expired, not revoked)."""
    await ctx.conn.execute(EXPIRE_GRANTS_SQL, _actor(ctx))
    grant = await ctx.conn.fetchrow(GRANT_LOCK_SQL)
    if grant is None:
        raise Conflict("no_support_grant", "AA support access is not allowed now")
    await ctx.conn.execute(REVOKE_SQL, grant["support_grant_id"], _actor(ctx))
    ended = await ctx.conn.fetch(REVOKE_SESSIONS_SQL, grant["support_grant_id"], _actor(ctx))
    ctx.audit(
        "support_revoke",
        "allowed",
        "support_grant",
        str(grant["support_grant_id"]),
        object_ids=tuple(str(r["support_session_id"]) for r in ended),
    )
    return await support_access(ctx)
