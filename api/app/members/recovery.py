"""Owner recovery (Q1 6.3: "AA promotes an existing member after 14 days, old owner emailed, logged"; AA-939) and the
platform's member deactivation, which is how an agency can end up with no owner (card E test: "last owner
deactivated -> recovery only").

Member side (``/crm/v1``, inside ``tenant_tx()``):

- :func:`banner`: the pending request, if any, for the banner every member sees (Q1 6.3 "every member sees a banner
  with [Cancel]");
- :func:`cancel`: any active member cancels a pending request (one ``recovery_cancel`` event).

Platform side (``/crm/ops/v1`` writes, group ``crm-platform-admin``; the L1 console of CRM-13a calls them), each in
:func:`app.tenancy.platform_tx` with actor ``platform:<staff sub>`` (audit actor kind ``aa_ops``, in the agency's own
audit):

- :func:`open_request`: for an existing active member (the candidate), ``eligible_at`` = now + 14 days (the database
  refuses less), and the agency's active owners get the AA system mail (best effort);
- :func:`complete_request`: not before ``eligible_at`` (409 ``recovery_not_due``), the candidate must still be an
  active member; the candidate becomes owner;
- :func:`deactivate_member`: the member is disabled even when they are the last owner (only a platform context may do
  that, crm-008), and signed out of the partner pool. With no owner left, only an owner recovery makes a new one:
  inside the agency nobody can grant the owner role (matrix ``team_manage`` is owner-only, and the database refuses a
  membership write by anyone but an owner).

AA-941: the L1 console's ticket reference goes on each platform write as the audit object id ``ticket:<REF>``
(:func:`ticket_ids`); :func:`mail_owners` is the best-effort AA system mail to the agency's owners that the console's
data requests use too. :func:`members` reads an agency in any status; the writes need an active one (409
``tenant_locked`` / ``tenant_status`` from :func:`app.tenancy.platform_tx`).

Ids and codes only; no name or e-mail is stored or returned.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound
from app.members.directory import PartnerDirectory
from app.members.mail import Mailer, Message, recovery_message
from app.permissions import Action, require
from app.tenancy import TENANT_STATUSES, SystemContext, TenantContext, platform_tx

log = logging.getLogger(__name__)

RECOVERY_DAYS = 14
REASONS = ("owner_left", "owner_unreachable", "no_owner")

PENDING_SQL = """
SELECT request_id, candidate_member_id, reason, eligible_at, created_at
  FROM crm.owner_recovery_request
 WHERE status = 'pending' AND deleted_at IS NULL
"""
CANCEL_SQL = """
UPDATE crm.owner_recovery_request SET status = 'cancelled', decided_at = now(), decided_by = $2
 WHERE request_id = $1 AND status = 'pending' AND deleted_at IS NULL
RETURNING request_id
"""
OPEN_SQL = """
INSERT INTO crm.owner_recovery_request (candidate_member_id, reason, eligible_at)
VALUES ($1, $2, now() + make_interval(days => $3))
RETURNING request_id, eligible_at
"""
LOCK_SQL = """
SELECT request_id, candidate_member_id, status, eligible_at FROM crm.owner_recovery_request
 WHERE request_id = $1 AND deleted_at IS NULL FOR UPDATE
"""
COMPLETE_SQL = """
UPDATE crm.owner_recovery_request SET status = 'completed', decided_at = now(), decided_by = $2 WHERE request_id = $1
"""
MEMBER_SQL = """
SELECT member_id, role, status, cognito_iss, cognito_sub FROM crm.member WHERE member_id = $1 AND deleted_at IS NULL
"""
PROMOTE_SQL = "UPDATE crm.member SET role = 'owner' WHERE member_id = $1"
DISABLE_SQL = "UPDATE crm.member SET status = 'disabled' WHERE member_id = $1"
OWNERS_SQL = """
SELECT cognito_iss, cognito_sub FROM crm.member WHERE role = 'owner' AND status = 'active' AND deleted_at IS NULL
"""
MEMBERS_SQL = """
SELECT member_id, role, status, created_at FROM crm.member WHERE deleted_at IS NULL ORDER BY created_at, member_id
"""
TENANT_LABEL_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def platform_actor(staff_sub: str) -> str:
    return f"platform:{staff_sub}"[:80]


def _not_due(exc: asyncpg.PostgresError) -> Exception:
    if exc.sqlstate == "CRM03":
        return Conflict("recovery_not_due", "an owner recovery completes 14 days after it opens")
    return exc


# ── Member side ──────────────────────────────────────────────────────────────────────────────────
async def banner(ctx: TenantContext) -> dict[str, Any]:
    require(ctx, Action.RECOVERY_VIEW)
    row = await ctx.conn.fetchrow(PENDING_SQL)
    if row is None:
        return {"recovery": None}
    return {
        "recovery": {
            "request_id": str(row["request_id"]),
            "reason": row["reason"],
            "eligible_at": _iso(row["eligible_at"]),
            "opened_at": _iso(row["created_at"]),
            "candidate_member_id": str(row["candidate_member_id"]),
            "is_candidate": row["candidate_member_id"] == ctx.member_id,
        }
    }


async def cancel(ctx: TenantContext, request_id: uuid.UUID) -> dict[str, Any]:
    require(ctx, Action.RECOVERY_CANCEL, object_kind="owner_recovery_request", object_id=str(request_id))
    if await ctx.conn.fetchval(CANCEL_SQL, request_id, f"member:{ctx.member_id}") is None:
        raise NotFound("recovery_not_found", "no pending owner recovery with this id")
    ctx.audit("recovery_cancel", "allowed", "owner_recovery_request", str(request_id))
    return {"request_id": str(request_id), "status": "cancelled"}


# ── Platform side ────────────────────────────────────────────────────────────────────────────────
async def _active_member(ctx: SystemContext, member_id: uuid.UUID) -> asyncpg.Record:
    row = await ctx.conn.fetchrow(MEMBER_SQL, member_id)
    if row is None:
        raise NotFound("member_not_found")
    if row["status"] != "active":
        raise Conflict("candidate_inactive", "the candidate must be an active member")
    return row


async def mail_owners(
    owners: Sequence[asyncpg.Record | dict[str, Any]],
    directory: PartnerDirectory | None,
    mailer: Mailer | None,
    build: Callable[[str], Message],
    *,
    tenant_id: uuid.UUID,
    tag: str,
) -> int:
    """AA system mail to the agency's active owners of the partner pool, best effort (after the commit): a failure is
    logged by class only and never undoes the platform action. Returns how many mails went out."""
    notified = 0
    if directory is None or mailer is None:
        return notified
    for owner in owners:
        if owner["cognito_iss"] != directory.issuer:
            continue
        try:
            email = await directory.email_for(owner["cognito_sub"])
            if email:
                await mailer.send(build(email))
                notified += 1
        except Exception as exc:  # noqa: BLE001 — the action stands; the agency sees it in the CRM anyway
            log.warning("crm_%s_failed tenant=%s error=%s", tag, tenant_id, type(exc).__name__)
    return notified


def ticket_ids(ticket_ref: str | None) -> tuple[str, ...]:
    """AA-941: the L1 ticket reference as an audit object id (``ticket:<REF>``; the route validated the pattern)."""
    return (f"ticket:{ticket_ref}",) if ticket_ref else ()


async def members(pool: asyncpg.Pool, tenant_id: uuid.UUID, *, staff_sub: str) -> dict[str, Any]:
    """Ids, roles and statuses (the L1 console picks a recovery candidate; no identity data). Any agency status."""
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=TENANT_STATUSES) as ctx:
        rows = await ctx.conn.fetch(MEMBERS_SQL)
        pending = await ctx.conn.fetchrow(PENDING_SQL)
        ctx.audit("list_view", "allowed", "member", object_ids=tuple(str(r["member_id"]) for r in rows)[:200])
        return {
            "members": [
                {
                    "member_id": str(r["member_id"]),
                    "role": r["role"],
                    "status": r["status"],
                    "joined_at": _iso(r["created_at"]),
                }
                for r in rows
            ],
            "active_owners": sum(1 for r in rows if r["role"] == "owner" and r["status"] == "active"),
            "recovery": None
            if pending is None
            else {"request_id": str(pending["request_id"]), "eligible_at": _iso(pending["eligible_at"])},
        }


async def open_request(
    pool: asyncpg.Pool,
    tenant_id: uuid.UUID,
    *,
    candidate_member_id: uuid.UUID,
    reason: str,
    staff_sub: str,
    directory: PartnerDirectory | None,
    mailer: Mailer | None,
    web_origin: str,
    ticket_ref: str | None = None,
) -> dict[str, Any]:
    if reason not in REASONS:
        raise Invalid("invalid_reason", "owner_left, owner_unreachable or no_owner")
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub)) as ctx:
        candidate = await _active_member(ctx, candidate_member_id)
        if candidate["role"] == "owner":
            raise Conflict("candidate_is_owner", "the candidate is already an owner")
        try:
            row = await ctx.conn.fetchrow(OPEN_SQL, candidate_member_id, reason, RECOVERY_DAYS)
        except asyncpg.UniqueViolationError:
            raise Conflict("recovery_pending", "this agency already has a pending owner recovery") from None
        ctx.audit(
            "recovery_open",
            "allowed",
            "owner_recovery_request",
            str(row["request_id"]),
            reason=reason,
            object_ids=ticket_ids(ticket_ref),
        )
        owners = await ctx.conn.fetch(OWNERS_SQL)
        agency = await ctx.conn.fetchval(TENANT_LABEL_SQL, tenant_id) or "AA Partner"
    notified = await mail_owners(
        owners,
        directory,
        mailer,
        lambda email: recovery_message(email, agency=agency, eligible_at=row["eligible_at"], link=web_origin),
        tenant_id=tenant_id,
        tag="recovery_notice",
    )
    return {
        "request_id": str(row["request_id"]),
        "status": "pending",
        "eligible_at": _iso(row["eligible_at"]),
        "owners_notified": notified,
    }


async def complete_request(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, request_id: uuid.UUID, *, staff_sub: str, ticket_ref: str | None = None
) -> dict[str, Any]:
    actor = platform_actor(staff_sub)
    async with platform_tx(pool, tenant_id, actor=actor) as ctx:
        row = await ctx.conn.fetchrow(LOCK_SQL, request_id)
        if row is None:
            raise NotFound("recovery_not_found")
        if row["status"] != "pending":
            raise Conflict("recovery_closed", f"the request is {row['status']}")
        await _active_member(ctx, row["candidate_member_id"])
        try:
            await ctx.conn.execute(COMPLETE_SQL, request_id, actor)
        except asyncpg.PostgresError as exc:
            raise _not_due(exc) from None
        await ctx.conn.execute(PROMOTE_SQL, row["candidate_member_id"])
        ctx.audit(
            "recovery_complete",
            "allowed",
            "owner_recovery_request",
            str(request_id),
            object_ids=ticket_ids(ticket_ref),
        )
        ctx.audit(
            "role_change",
            "allowed",
            "member",
            str(row["candidate_member_id"]),
            fields=("role",),
            reason="recovery_to_owner",
        )
        return {
            "request_id": str(request_id),
            "status": "completed",
            "owner_member_id": str(row["candidate_member_id"]),
        }


async def deactivate_member(
    pool: asyncpg.Pool,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    staff_sub: str,
    directory: PartnerDirectory | None,
    ticket_ref: str | None = None,
) -> dict[str, Any]:
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub)) as ctx:
        row = await ctx.conn.fetchrow(MEMBER_SQL, member_id)
        if row is None:
            raise NotFound("member_not_found")
        if row["status"] != "disabled":
            await ctx.conn.execute(DISABLE_SQL, member_id)
            ctx.audit(
                "member_deactivate",
                "allowed",
                "member",
                str(member_id),
                fields=("status",),
                object_ids=ticket_ids(ticket_ref),
            )
        owners_left = len(await ctx.conn.fetch(OWNERS_SQL))
    signed_out = False
    if directory is not None and row["cognito_iss"] == directory.issuer:
        try:
            await directory.global_sign_out(row["cognito_sub"])
            signed_out = True
        except Exception as exc:  # noqa: BLE001 — the membership is already disabled
            log.warning("crm_member_sign_out_failed member=%s error=%s", member_id, type(exc).__name__)
    return {"member_id": str(member_id), "status": "disabled", "signed_out": signed_out, "active_owners": owners_left}
