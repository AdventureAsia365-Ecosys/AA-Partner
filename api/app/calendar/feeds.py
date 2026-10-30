"""The member's ICS calendar feed (AA-961, card CRM-30; crm-027 ``crm.calendar_feed``).

- :func:`status`: the member's feed (active, or the last revoked one, so the page can say "your link was revoked:
  subscribed calendars stopped updating", card E) without its URL (the token is not stored).
- :func:`create`: a new feed for the member: a random 32-byte token (URL-safe, 43 characters), only its SHA-256
  stored; an active feed of the member is revoked in the same transaction (one active feed per member, crm-027). The
  URL is returned once. One ``calendar_feed`` audit event.
- :func:`revoke`: the member's own feed, or (an owner) any member's: status ``revoked``; the URL answers 404 from then
  on, so subscribed calendars stop updating (they keep the events they already have). One audit event.

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import Any

from app.errors import Forbidden, NotFound
from app.tenancy import TenantContext

SCOPES = ("mine", "agency")
FEED_PATH = "/crm/public/v1/calendar"

CURRENT_SQL = """
SELECT calendar_feed_id, member_id, scope, status, expires_at, revoked_at, revoked_by, last_fetched_at, fetch_count,
       created_at
  FROM crm.calendar_feed
 WHERE member_id = $1 AND deleted_at IS NULL
 ORDER BY (status = 'active') DESC, created_at DESC
 LIMIT 1
"""
ACTIVE_SQL = """
SELECT calendar_feed_id, member_id FROM crm.calendar_feed
 WHERE member_id = $1 AND status = 'active' AND deleted_at IS NULL
   FOR UPDATE
"""
BY_ID_SQL = """
SELECT calendar_feed_id, member_id, status FROM crm.calendar_feed
 WHERE calendar_feed_id = $1 AND deleted_at IS NULL
   FOR UPDATE
"""
REVOKE_SQL = """
UPDATE crm.calendar_feed SET status = 'revoked', revoked_at = now(), revoked_by = $2
 WHERE calendar_feed_id = $1 AND status = 'active'
"""
INSERT_SQL = """
INSERT INTO crm.calendar_feed (member_id, token_hash, scope) VALUES ($1, $2, $3)
RETURNING calendar_feed_id, member_id, scope, status, expires_at, revoked_at, revoked_by, last_fetched_at,
          fetch_count, created_at
"""


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def feed_url(origin: str, token: str) -> str:
    return f"{origin}{FEED_PATH}/{token}.ics"


def _view(row: Any) -> dict[str, Any]:
    def iso(v: Any) -> str | None:
        return v.isoformat() if v is not None else None

    return {
        "calendar_feed_id": str(row["calendar_feed_id"]),
        "member_id": str(row["member_id"]),
        "scope": row["scope"],
        "status": row["status"],
        "created_at": iso(row["created_at"]),
        "expires_at": iso(row["expires_at"]),
        "revoked_at": iso(row["revoked_at"]),
        "revoked_by_owner": bool(row["revoked_by"]) and row["revoked_by"] != f"member:{row['member_id']}",
        "last_fetched_at": iso(row["last_fetched_at"]),
        "fetch_count": row["fetch_count"],
    }


async def status(ctx: TenantContext) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(CURRENT_SQL, ctx.member_id)
    return {"feed": _view(row) if row is not None else None}


async def create(ctx: TenantContext, *, scope: str, origin: str) -> dict[str, Any]:
    if scope not in SCOPES:
        raise ValueError(scope)  # the router's Literal keeps this unreachable
    actor = f"member:{ctx.member_id}"
    old = await ctx.conn.fetchrow(ACTIVE_SQL, ctx.member_id)
    if old is not None:
        await ctx.conn.execute(REVOKE_SQL, old["calendar_feed_id"], actor)
        ctx.audit("calendar_feed", "allowed", "calendar_feed", str(old["calendar_feed_id"]), reason="replaced")
    token = secrets.token_urlsafe(32)
    row = await ctx.conn.fetchrow(INSERT_SQL, ctx.member_id, token_hash(token), scope)
    ctx.audit("calendar_feed", "allowed", "calendar_feed", str(row["calendar_feed_id"]), reason=f"created_{scope}")
    return {"feed": _view(row), "url": feed_url(origin, token)}


async def revoke(ctx: TenantContext, feed_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(BY_ID_SQL, feed_id)
    if row is None:
        raise NotFound("feed_not_found")
    if row["member_id"] != ctx.member_id and ctx.member_role != "owner":
        ctx.audit("calendar_feed", "denied", "calendar_feed", str(feed_id), reason="not_own_feed")
        raise Forbidden("not_own_feed", "only the member or an owner revokes a calendar feed")
    if row["status"] == "active":
        await ctx.conn.execute(REVOKE_SQL, feed_id, f"member:{ctx.member_id}")
        ctx.audit("calendar_feed", "allowed", "calendar_feed", str(feed_id), reason="revoked")
    current = await ctx.conn.fetchrow(
        "SELECT calendar_feed_id, member_id, scope, status, expires_at, revoked_at, revoked_by, last_fetched_at, "
        "fetch_count, created_at FROM crm.calendar_feed WHERE calendar_feed_id = $1",
        feed_id,
    )
    return {"feed": _view(current)}


TEAM_SQL = """
SELECT calendar_feed_id, member_id, scope, status, expires_at, revoked_at, revoked_by, last_fetched_at, fetch_count,
       created_at
  FROM crm.calendar_feed
 WHERE status = 'active' AND deleted_at IS NULL
 ORDER BY created_at DESC
 LIMIT 200
"""


async def team(ctx: TenantContext) -> dict[str, Any]:
    """The agency's active feeds (an owner's view, to revoke one of a member who left or lost a device)."""
    if ctx.member_role != "owner":
        ctx.audit("calendar_feed", "denied", "calendar_feed", reason="owner_only")
        raise Forbidden("role_forbidden", "only an owner sees the team's calendar feeds")
    return {"feeds": [_view(r) for r in await ctx.conn.fetch(TEAM_SQL)]}
