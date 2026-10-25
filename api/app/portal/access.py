"""Portal access per traveller, the agency side (card CRM-18, AA-946; design v3 M11 base, R22, §2.9 rule 1).

- :func:`invite` gives one traveller of the trip their own portal link (``client_link`` purpose ``portal``, 30 days,
  only its SHA-256 stored) and a ``traveler_access`` row (lead / companion from ``trip_traveler``); a second invite
  re-issues the link (the old one is revoked, ``reissued_from``). The message goes through the dispatcher like the
  Stage 2 link (held ``no_message`` until a template send path exists), so the answer carries the link path once for
  "Copy link". With a published waiver, a pending ``signature_request`` is opened too.
- :func:`invite_all` invites every traveller of the trip (the deposit-paid effect, design F6.6), each in a savepoint:
  a traveller who cannot be messaged (archived) is skipped with a code, never failing the payment.
- :func:`revoke` ends one traveller's access (and its link).

Every function takes the open context and never opens one.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import Any

from app.dispatch import queue as dispatch
from app.errors import Conflict, NotFound
from app.portal import progress
from app.tenancy import AnyContext

PURPOSE = "portal"
LINK_DAYS = 30
CLOSED_STAGES = ("lost", "cancelled", "completed")

TRIP_SQL = "SELECT stage FROM crm.trip WHERE trip_id = $1 AND deleted_at IS NULL"
PARTY_SQL = """
SELECT tt.traveler_id, tt.role FROM crm.trip_traveler tt
  JOIN crm.traveler t ON t.traveler_id = tt.traveler_id AND t.deleted_at IS NULL
 WHERE tt.trip_id = $1 AND tt.deleted_at IS NULL
 ORDER BY tt.role = 'lead' DESC, tt.created_at, tt.traveler_id
"""
ROLE_SQL = """
SELECT role FROM crm.trip_traveler WHERE trip_id = $1 AND traveler_id = $2 AND deleted_at IS NULL
"""
ACCESS_SQL = """
SELECT access_id, link_id, status FROM crm.traveler_access
 WHERE trip_id = $1 AND traveler_id = $2 AND status <> 'revoked' AND deleted_at IS NULL
   FOR UPDATE
"""
ACCESS_BY_ID_SQL = """
SELECT access_id, trip_id, traveler_id, link_id, status FROM crm.traveler_access
 WHERE access_id = $1 AND deleted_at IS NULL FOR UPDATE
"""
REVOKE_LINK_SQL = """
UPDATE crm.client_link SET status = 'revoked', revoked_at = now()
 WHERE link_id = $1 AND status IN ('sent', 'opened', 'submitted')
"""
INSERT_LINK_SQL = """
INSERT INTO crm.client_link (link_id, trip_id, traveler_id, purpose, token_hash, expires_at, reissued_from)
VALUES ($1, $2, $3, $4, $5, now() + make_interval(days => $6), $7)
RETURNING expires_at
"""
LINK_DISPATCH_SQL = "UPDATE crm.client_link SET dispatch_id = $2 WHERE link_id = $1"
INSERT_ACCESS_SQL = """
INSERT INTO crm.traveler_access (access_id, trip_id, traveler_id, role, link_id) VALUES ($1, $2, $3, $4, $5)
"""
RELINK_ACCESS_SQL = "UPDATE crm.traveler_access SET link_id = $2 WHERE access_id = $1"
REVOKE_ACCESS_SQL = "UPDATE crm.traveler_access SET status = 'revoked' WHERE access_id = $1"
PARTY_PORTAL_SQL = """
UPDATE crm.trip_traveler SET portal_access = $3 WHERE trip_id = $1 AND traveler_id = $2 AND portal_access <> $3
"""
OPEN_SIGNATURE_SQL = """
INSERT INTO crm.signature_request (trip_id, traveler_id, access_id, doc_id)
SELECT $1, $2, $3, d.doc_id FROM crm.agreement_doc d
 WHERE d.kind = 'waiver' AND d.status = 'published' AND d.deleted_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM crm.signature_request s
                    WHERE s.trip_id = $1 AND s.traveler_id = $2 AND s.doc_id = d.doc_id AND s.status <> 'void'
                      AND s.deleted_at IS NULL)
"""


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _origin(ctx: AnyContext) -> str:
    return "user_action" if getattr(ctx, "member_id", None) is not None else "system"


async def invite(ctx: AnyContext, trip_id: uuid.UUID, traveler_id: uuid.UUID) -> dict[str, Any]:
    stage = await ctx.conn.fetchval(TRIP_SQL, trip_id)
    if stage is None:
        raise NotFound("trip_not_found")
    if stage in CLOSED_STAGES:
        raise Conflict("trip_closed", "a lost, cancelled or completed trip has no portal")
    role = await ctx.conn.fetchval(ROLE_SQL, trip_id, traveler_id)
    if role is None:
        raise NotFound("traveler_not_on_trip")
    current = await ctx.conn.fetchrow(ACCESS_SQL, trip_id, traveler_id)
    previous_link = current["link_id"] if current is not None else None
    if previous_link is not None:
        await ctx.conn.execute(REVOKE_LINK_SQL, previous_link)
    token = secrets.token_urlsafe(32)
    link_id = uuid.uuid4()
    expires_at = await ctx.conn.fetchval(
        INSERT_LINK_SQL, link_id, trip_id, traveler_id, PURPOSE, token_hash(token), LINK_DAYS, previous_link
    )
    if current is None:
        access_id = uuid.uuid4()
        await ctx.conn.execute(INSERT_ACCESS_SQL, access_id, trip_id, traveler_id, role, link_id)
    else:
        access_id = current["access_id"]
        await ctx.conn.execute(RELINK_ACCESS_SQL, access_id, link_id)
    await ctx.conn.execute(PARTY_PORTAL_SQL, trip_id, traveler_id, role)
    await ctx.conn.execute(OPEN_SIGNATURE_SQL, trip_id, traveler_id, access_id)
    queued = await dispatch.enqueue_message(
        ctx,
        traveler_id=traveler_id,
        channel="email",
        purpose="transactional",
        origin=_origin(ctx),
        trip_id=trip_id,
        message_ref=link_id,
    )
    await ctx.conn.execute(LINK_DISPATCH_SQL, link_id, uuid.UUID(queued["dispatch_id"]))
    ctx.audit("portal_invite", "allowed", "traveler_access", str(access_id), reason="reissue" if current else "new")
    return {
        "access_id": str(access_id),
        "traveler_id": str(traveler_id),
        "role": role,
        "link_path": f"/c/t/{token}",
        "expires_at": expires_at.isoformat(),
        "dispatch_id": queued["dispatch_id"],
        "reissued_from": str(previous_link) if previous_link else None,
    }


async def invite_all(ctx: AnyContext, trip_id: uuid.UUID) -> dict[str, Any]:
    """Invite every traveller of the trip; a traveller who cannot be invited is listed with the refusal code."""
    invited: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for row in await ctx.conn.fetch(PARTY_SQL, trip_id):
        try:
            async with ctx.conn.transaction():
                invited.append(await invite(ctx, trip_id, row["traveler_id"]))
        except (Conflict, NotFound) as exc:
            skipped.append({"traveler_id": str(row["traveler_id"]), "code": exc.code})
    await progress.refresh(ctx, trip_id)
    return {"invited": invited, "skipped": skipped}


async def revoke(ctx: AnyContext, access_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(ACCESS_BY_ID_SQL, access_id)
    if row is None:
        raise NotFound("access_not_found")
    if row["status"] == "revoked":
        raise Conflict("already_revoked")
    if row["link_id"] is not None:
        await ctx.conn.execute(REVOKE_LINK_SQL, row["link_id"])
    await ctx.conn.execute(REVOKE_ACCESS_SQL, access_id)
    await ctx.conn.execute(PARTY_PORTAL_SQL, row["trip_id"], row["traveler_id"], "none")
    ctx.audit("portal_revoke", "allowed", "traveler_access", str(access_id))
    return {"access_id": str(access_id), "status": "revoked"}


async def trip_of(ctx: AnyContext, access_id: uuid.UUID) -> uuid.UUID:
    """The trip of one portal access (the router checks the member's message rule on it first)."""
    trip_id = await ctx.conn.fetchval(
        "SELECT trip_id FROM crm.traveler_access WHERE access_id = $1 AND deleted_at IS NULL", access_id
    )
    if trip_id is None:
        raise NotFound("access_not_found")
    return trip_id
