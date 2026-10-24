"""The owner's Team page and the plan (A-18 Team & roles, Plan & usage; AA-939, design v3 §4.6, R8, R11).

Every function takes the open :class:`~app.tenancy.TenantContext` (the route opened ``tenant_tx()`` with the matching
:class:`~app.permissions.Action`) and checks the matrix again with :func:`app.permissions.require`, so a helper called
from a new route cannot skip it. Membership writes are guarded by the database too (crm-008: only an owner writes
``crm.member``; the last active owner stays, SQLSTATE CRM01 -> 409 ``last_owner``).

- Members carry no name: ``crm.member`` holds the token's ``(iss, sub)`` only. The Team page shows the address the
  member was invited with (``member_invitation.email_enc``, opened by the vault for the owner, one ``list_view``
  event naming the ids shown); a member created by the seeder or the L1 console shows its id.
- **Invite** (root ADR 0006b decision 5): a single-use token (32 random bytes, URL-safe), only its SHA-256 stored,
  expiring after ``INVITATION_TTL_HOURS`` (default 7 days, at most 30), the address encrypted with its blind index
  (one live invitation per address), the seat limit of the plan, then the AA system mail with the join link. The
  account is created later, by the claim step (:mod:`app.members.invitations`), never here.
- **Remove** = the membership is disabled (never deleted: the audit and the trips keep pointing at it), the person's
  refresh tokens are revoked in the partner pool (best effort: the next request is refused by ``tenant_tx()`` anyway),
  and their open trips and clients land in the **reassign queue** (R8: the agency owns its clients) until the owner
  reassigns them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import secrets
import uuid
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound
from app.members.directory import PartnerDirectory
from app.members.mail import Mailer, invitation_message
from app.permissions import Action, require
from app.tenancy import TenantContext
from app.vault import Vault

log = logging.getLogger(__name__)

MEMBER_ROLES = ("owner", "agent", "assistant")
DEFAULT_PLAN = {"plan": "free", "member_limit": 10, "allowed_features": []}
MAX_INVITATIONS_LISTED = 100
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s.]{2,}$")

MEMBERS_SQL = """
SELECT m.member_id, m.role, m.status, m.created_at, m.cognito_iss,
       (SELECT i.invitation_id FROM crm.member_invitation i
         WHERE i.member_id = m.member_id AND i.status = 'accepted'
         ORDER BY i.accepted_at DESC LIMIT 1) AS invitation_id,
       (SELECT i.email_enc FROM crm.member_invitation i
         WHERE i.member_id = m.member_id AND i.status = 'accepted'
         ORDER BY i.accepted_at DESC LIMIT 1) AS email_enc,
       (SELECT count(*) FROM crm.trip t
         WHERE t.owner_member_id = m.member_id AND t.deleted_at IS NULL
           AND t.stage NOT IN ('completed', 'lost', 'cancelled')) AS open_trips
  FROM crm.member m
 WHERE m.deleted_at IS NULL
 ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'agent' THEN 1 ELSE 2 END, m.created_at, m.member_id
"""
EXPIRE_SQL = """
UPDATE crm.member_invitation SET status = 'expired'
 WHERE status IN ('pending', 'claimed') AND expires_at <= now() AND deleted_at IS NULL
"""
INVITATIONS_SQL = """
SELECT invitation_id, role, status, expires_at, created_at, email_enc
  FROM crm.member_invitation
 WHERE status IN ('pending', 'claimed') AND deleted_at IS NULL
 ORDER BY created_at DESC
 LIMIT $1
"""
SEATS_SQL = """
SELECT (SELECT count(*) FROM crm.member WHERE status IN ('active', 'invited') AND deleted_at IS NULL)
     + (SELECT count(*) FROM crm.member_invitation
         WHERE status IN ('pending', 'claimed') AND expires_at > now() AND deleted_at IS NULL)
"""
SEATS_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('crm.member.seats:' || $1, 0))"
PLAN_SQL = "SELECT plan, member_limit, allowed_features FROM crm.tenant_entitlement WHERE deleted_at IS NULL"
TENANT_LABEL_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
ALREADY_MEMBER_SQL = """
SELECT 1 FROM crm.member_invitation i JOIN crm.member m ON m.member_id = i.member_id
 WHERE i.email_bidx = $1 AND i.status = 'accepted' AND m.status = 'active' AND m.deleted_at IS NULL
 LIMIT 1
"""
INVITE_SQL = """
INSERT INTO crm.member_invitation (invitation_id, role, email_enc, email_bidx, token_hash, expires_at)
VALUES ($1, $2, $3, $4, $5, now() + make_interval(hours => $6))
RETURNING expires_at
"""
REVOKE_SQL = """
UPDATE crm.member_invitation SET status = 'revoked'
 WHERE invitation_id = $1 AND status IN ('pending', 'claimed') AND deleted_at IS NULL
RETURNING invitation_id
"""
MEMBER_LOCK_SQL = """
SELECT member_id, role, status, cognito_iss, cognito_sub FROM crm.member
 WHERE member_id = $1 AND deleted_at IS NULL FOR UPDATE
"""
OPEN_TRIPS_OF_SQL = (
    "SELECT count(*) FROM crm.trip WHERE owner_member_id = $1 AND deleted_at IS NULL "
    "AND stage NOT IN ('completed', 'lost', 'cancelled')"
)
ROLE_SQL = "UPDATE crm.member SET role = $2 WHERE member_id = $1"
DISABLE_SQL = "UPDATE crm.member SET status = 'disabled' WHERE member_id = $1"
ACTIVE_MEMBER_SQL = "SELECT 1 FROM crm.member WHERE member_id = $1 AND status = 'active' AND deleted_at IS NULL"
QUEUE_TRIPS_SQL = """
SELECT t.trip_id, t.title, t.stage, t.owner_member_id
  FROM crm.trip t JOIN crm.member m ON m.member_id = t.owner_member_id
 WHERE t.deleted_at IS NULL AND t.stage NOT IN ('completed', 'lost', 'cancelled')
   AND (m.status <> 'active' OR m.deleted_at IS NOT NULL)
 ORDER BY t.stage_entered_at, t.trip_id
 LIMIT 200
"""
QUEUE_CLIENTS_SQL = """
SELECT tr.owner_member_id, count(*) AS clients
  FROM crm.traveler tr JOIN crm.member m ON m.member_id = tr.owner_member_id
 WHERE tr.deleted_at IS NULL AND (m.status <> 'active' OR m.deleted_at IS NOT NULL)
 GROUP BY tr.owner_member_id
"""
REASSIGN_TRIPS_SQL = """
UPDATE crm.trip SET owner_member_id = $2
 WHERE owner_member_id = $1 AND deleted_at IS NULL AND stage NOT IN ('completed', 'lost', 'cancelled')
   AND ($3::uuid[] IS NULL OR trip_id = ANY ($3::uuid[]))
RETURNING trip_id
"""
REASSIGN_TASKS_SQL = """
UPDATE crm.task SET assignee_member_id = $2
 WHERE assignee_member_id = $1 AND status = 'open' AND deleted_at IS NULL AND trip_id = ANY ($3::uuid[])
"""
REASSIGN_CLIENTS_SQL = """
UPDATE crm.traveler SET owner_member_id = $2 WHERE owner_member_id = $1 AND deleted_at IS NULL
"""


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def normalise_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise Invalid("invalid_email", "a valid e-mail address")
    return email


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def new_token() -> tuple[str, bytes]:
    """A single-use invitation token (URL-safe, 43 characters) and its SHA-256 (what the database keeps)."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("ascii")).digest()


async def plan(ctx: TenantContext) -> dict[str, Any]:
    require(ctx, Action.PLAN_VIEW)
    row = await ctx.conn.fetchrow(PLAN_SQL)
    current = (
        dict(DEFAULT_PLAN)
        if row is None
        else {
            "plan": row["plan"],
            "member_limit": row["member_limit"],
            "allowed_features": list(row["allowed_features"] or []),
        }
    )
    current["seats_used"] = int(await ctx.conn.fetchval(SEATS_SQL) or 0)
    return current


async def team(ctx: TenantContext, vault: Vault) -> dict[str, Any]:
    """Members (with the address they were invited with), pending invitations (masked address) and the plan."""
    require(ctx, Action.TEAM_VIEW)
    await ctx.conn.execute(EXPIRE_SQL)
    members = []
    for r in await ctx.conn.fetch(MEMBERS_SQL):
        email = None
        if r["email_enc"] is not None:
            email = await vault.decrypt(
                "member_invitation", "email_enc", r["invitation_id"], bytes(r["email_enc"]), tenant_id=ctx.tenant_id
            )
        members.append(
            {
                "member_id": str(r["member_id"]),
                "role": r["role"],
                "status": r["status"],
                "email": email,
                "joined_at": _iso(r["created_at"]),
                "open_trips": int(r["open_trips"] or 0),
                "is_me": r["member_id"] == ctx.member_id,
            }
        )
    invitations = []
    for r in await ctx.conn.fetch(INVITATIONS_SQL, MAX_INVITATIONS_LISTED):
        email = await vault.decrypt(
            "member_invitation", "email_enc", r["invitation_id"], bytes(r["email_enc"]), tenant_id=ctx.tenant_id
        )
        invitations.append(
            {
                "invitation_id": str(r["invitation_id"]),
                "role": r["role"],
                "status": r["status"],
                "email_masked": mask_email(email),
                "expires_at": _iso(r["expires_at"]),
                "created_at": _iso(r["created_at"]),
            }
        )
    ctx.audit(
        "list_view",
        "allowed",
        "member",
        object_ids=tuple([m["member_id"] for m in members] + [i["invitation_id"] for i in invitations])[:200],
        fields=("email_enc",),
    )
    return {"members": members, "invitations": invitations, "plan": await plan(ctx)}


async def invite(
    ctx: TenantContext,
    vault: Vault,
    mailer: Mailer,
    *,
    email: str,
    role: str,
    web_origin: str,
    ttl_hours: int,
) -> dict[str, Any]:
    require(ctx, Action.TEAM_MANAGE, object_kind="member_invitation")
    if role not in MEMBER_ROLES:
        raise Invalid("invalid_role", "owner, agent or assistant")
    address = normalise_email(email)
    # Serialise invites per agency (until this transaction ends), so two at once cannot both take the last seat.
    await ctx.conn.execute(SEATS_LOCK_SQL, str(ctx.tenant_id))
    current = await plan(ctx)
    if current["seats_used"] >= current["member_limit"]:
        raise Conflict(
            "member_limit_reached", "the plan's member limit is reached", member_limit=current["member_limit"]
        )
    bidx = await vault.email_index("member_invitation", "email_bidx", address)
    if await ctx.conn.fetchval(ALREADY_MEMBER_SQL, bidx):
        raise Conflict("already_member", "this address already belongs to an active member")
    invitation_id = uuid.uuid4()
    token, token_hash = new_token()
    email_enc = await vault.encrypt("member_invitation", "email_enc", invitation_id, address)
    try:
        expires_at: dt.datetime = await ctx.conn.fetchval(
            INVITE_SQL, invitation_id, role, email_enc, bidx, token_hash, ttl_hours
        )
    except asyncpg.UniqueViolationError:
        raise Conflict("invitation_exists", "this address already has a pending invitation") from None
    ctx.audit("invite", "allowed", "member_invitation", str(invitation_id), fields=("role",), reason=f"role_{role}")
    agency = await ctx.conn.fetchval(TENANT_LABEL_SQL, ctx.tenant_id) or "AA Partner"
    # Sent inside the transaction: a mail failure rolls the invitation back (no row without a mail).
    await mailer.send(
        invitation_message(
            address, agency=agency, role=role, link=f"{web_origin}/join?t={token}", expires_at=expires_at
        )
    )
    return {
        "invitation_id": str(invitation_id),
        "role": role,
        "status": "pending",
        "email_masked": mask_email(address),
        "expires_at": _iso(expires_at),
    }


async def revoke_invitation(ctx: TenantContext, invitation_id: uuid.UUID) -> dict[str, Any]:
    require(ctx, Action.TEAM_MANAGE, object_kind="member_invitation", object_id=str(invitation_id))
    if await ctx.conn.fetchval(REVOKE_SQL, invitation_id) is None:
        raise NotFound("invitation_not_found", "no pending invitation with this id")
    ctx.audit("invite_revoke", "allowed", "member_invitation", str(invitation_id))
    return {"invitation_id": str(invitation_id), "status": "revoked"}


def _membership_conflict(exc: asyncpg.PostgresError) -> Exception:
    """crm-008 member guard SQLSTATEs -> service errors (anything else goes on unchanged)."""
    if exc.sqlstate == "CRM01":
        return Conflict("last_owner", "an agency keeps at least one active owner")
    if exc.sqlstate == "CRM02":
        return Conflict("role_write_forbidden", "only the owner changes memberships")
    return exc


async def change_role(ctx: TenantContext, member_id: uuid.UUID, role: str) -> dict[str, Any]:
    require(ctx, Action.TEAM_MANAGE, object_kind="member", object_id=str(member_id))
    if role not in MEMBER_ROLES:
        raise Invalid("invalid_role", "owner, agent or assistant")
    row = await ctx.conn.fetchrow(MEMBER_LOCK_SQL, member_id)
    if row is None:
        raise NotFound("member_not_found")
    if row["status"] == "disabled":
        raise Conflict("member_disabled", "invite the person again to bring them back")
    if row["role"] != role:
        try:
            await ctx.conn.execute(ROLE_SQL, member_id, role)
        except asyncpg.PostgresError as exc:
            raise _membership_conflict(exc) from None
        ctx.audit(
            "role_change", "allowed", "member", str(member_id), fields=("role",), reason=f"{row['role']}_to_{role}"
        )
    return {"member_id": str(member_id), "role": role, "status": row["status"]}


async def remove_member(ctx: TenantContext, directory: PartnerDirectory | None, member_id: uuid.UUID) -> dict[str, Any]:
    require(ctx, Action.TEAM_MANAGE, object_kind="member", object_id=str(member_id))
    row = await ctx.conn.fetchrow(MEMBER_LOCK_SQL, member_id)
    if row is None:
        raise NotFound("member_not_found")
    if row["status"] != "disabled":
        try:
            await ctx.conn.execute(DISABLE_SQL, member_id)
        except asyncpg.PostgresError as exc:
            raise _membership_conflict(exc) from None
        ctx.audit("member_remove", "allowed", "member", str(member_id), fields=("status",))
    signed_out = False
    if directory is not None and row["cognito_iss"] == directory.issuer:
        try:
            await directory.global_sign_out(row["cognito_sub"])
            signed_out = True
        except Exception as exc:  # noqa: BLE001 — tenant_tx() refuses the person's next request anyway
            log.warning("crm_member_sign_out_failed member=%s error=%s", member_id, type(exc).__name__)
    open_trips = await ctx.conn.fetchval(OPEN_TRIPS_OF_SQL, member_id)
    return {
        "member_id": str(member_id),
        "status": "disabled",
        "signed_out": signed_out,
        "to_reassign": int(open_trips or 0),
    }


async def reassign_queue(ctx: TenantContext) -> dict[str, Any]:
    """Open trips and clients whose owner is no longer an active member (R8 reassign queue)."""
    require(ctx, Action.REASSIGN)
    trips = [
        {
            "trip_id": str(r["trip_id"]),
            "title": r["title"],
            "stage": r["stage"],
            "from_member_id": str(r["owner_member_id"]),
        }
        for r in await ctx.conn.fetch(QUEUE_TRIPS_SQL)
    ]
    clients = {str(r["owner_member_id"]): int(r["clients"]) for r in await ctx.conn.fetch(QUEUE_CLIENTS_SQL)}
    ctx.audit("list_view", "allowed", "trip", object_ids=tuple(t["trip_id"] for t in trips))
    return {"trips": trips, "clients_by_member": clients}


async def reassign(
    ctx: TenantContext, from_member_id: uuid.UUID, to_member_id: uuid.UUID, trip_ids: list[uuid.UUID] | None
) -> dict[str, Any]:
    """Move the open trips (all, or the listed ones) of one member to an active member, with their open tasks; when
    no trip list is given, the member's clients move too."""
    require(ctx, Action.REASSIGN, object_kind="member", object_id=str(from_member_id))
    if from_member_id == to_member_id:
        raise Invalid("same_member", "pick another member")
    if not await ctx.conn.fetchval(ACTIVE_MEMBER_SQL, to_member_id):
        raise Invalid("invalid_member", "an active member of this agency")
    moved = [r["trip_id"] for r in await ctx.conn.fetch(REASSIGN_TRIPS_SQL, from_member_id, to_member_id, trip_ids)]
    if moved:
        await ctx.conn.execute(REASSIGN_TASKS_SQL, from_member_id, to_member_id, moved)
    clients = 0
    if trip_ids is None:
        status = await ctx.conn.execute(REASSIGN_CLIENTS_SQL, from_member_id, to_member_id)
        clients = int(status.split()[-1]) if status and status.split()[-1].isdigit() else 0
    ctx.audit(
        "reassign",
        "allowed",
        "member",
        str(from_member_id),
        fields=("owner_member_id",),
        object_ids=tuple(str(t) for t in moved)[:200],
    )
    return {
        "from_member_id": str(from_member_id),
        "to_member_id": str(to_member_id),
        "trips": len(moved),
        "clients": clients,
    }
