"""The L1 platform console's agency operations (AA-941, card CRM-13a; design v3 S-01 "Agencies (CRM)", §4.9).

Every function opens :func:`app.tenancy.platform_tx` for one agency with actor ``platform:<staff sub>``, so each write
and each read lands in **that agency's own audit** with actor kind ``aa_ops`` (the owner sees it in the access log):

- reads write one ``ops_read`` event (reason ``list`` / ``summary`` / ``usage`` / ``entitlements`` /
  ``data_requests`` / ``jobs``; design §4.9 "AA ops reads");
- writes write their own event with the L1 ticket reference as an object id ``ticket:<REF>`` (the audit takes codes
  and ids only, never free text);
- one exception, :func:`platform_coverage` (crm.ops.v1 1.4, AA-992): platform-wide status counts, no agency's data,
  no per-agency event (see there).

Only ``app.routers.ops`` calls these functions (writes need the WORKFORCE group ``crm-platform-admin``), and only this
module and ``app/members/recovery.py`` open ``platform_tx`` (``tests/unit/test_code_rules.py``).

What L1 sees: ids, the agency's business name, states, flags and usage meters. Never a trip, a pipeline stage, a
price, revenue or a person's data; the response allow-list (:mod:`app.ops.serializer`) enforces it on every response.

Statuses: reads accept every agency status; lock / unlock and the allowed features / insights opt-out accept active
and locked; a retry accepts active and offboarding (an offboarding agency's erase job); the first-owner invite and AA
data requests need an active agency (the owner must be able to answer in the CRM); withdrawing the first-owner
invitation accepts active and locked; deleting a test agency accepts active, locked and offboarded.

S263 (AA-939 join fix, found live on Dev): :func:`withdraw_owner_invitation` closes the first-owner invitation AA sent
while it is pending or claimed but not accepted (a claimed owner invitation used to block a new one for its 7 days), and
:func:`delete_test_agency` moves a **synthetic** agency with no member and no traveller to the trash and crypto-shreds
its keys (Dev test clean-up; a real agency is only ever deleted by its owner's confirmed offboarding, AA-942).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Any

import asyncpg

from app import agencies
from app.audit_export import registry
from app.catalog import store as catalog_store
from app.cis import feed_state
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.jobs import queue
from app.lifecycle import offboarding
from app.lifecycle.rules import BACKUP_PURGE_DAYS
from app.llm.gateway import DEFAULT_BUDGET_USD, MONTH_SPEND_SQL
from app.members import accounts as partner_accounts
from app.members import team
from app.members.directory import PartnerDirectory
from app.members.mail import Mailer, data_request_message
from app.members.recovery import OWNERS_SQL, mail_owners, platform_actor, ticket_ids
from app.reports import insights
from app.tenancy import TENANT_STATUSES, SystemContext, as_insights_reader, platform_tx
from app.vault import KeyProvider, Vault

log = logging.getLogger(__name__)

LOCKABLE = frozenset({"active", "locked"})
RETRY_STATUSES = frozenset({"active", "offboarding"})
LOCK_REASONS = ("owner_request", "payment", "abuse", "security", "test")
FEATURES = ("ai_assist", "whatsapp_sms", "stripe_connect", "campaigns", "custom_domain", "insights_export")
DATA_REQUEST_PURPOSE = {"agency_export": "export_only", "agency_deletion": "offboard"}
MAX_PAGE = 200

# AA-978 follow-up (crm.ops.v1 1.2): who locked the agency (crm-013 lock_source) and the CIS membership feed's state
# (the crm.membership_feed_state platform singleton, crm_app SELECT): the S-01 page tells a CIS lock from an AA one and
# sees when CIS changes stopped reaching the CRM.
SUMMARY_SQL = """
SELECT t.tenant_id, t.display_name, t.status, t.lock_source, t.is_synthetic, t.created_at,
       EXISTS (SELECT 1 FROM crm.member m
                WHERE m.role = 'owner' AND m.status = 'active' AND m.deleted_at IS NULL) AS has_owner,
       (EXISTS (SELECT 1 FROM crm.traveler) OR EXISTS (SELECT 1 FROM crm.form_submission)) AS holds_travellers,
       f.failures AS feed_failures, f.last_ok_at AS feed_last_ok_at, f.last_error_at AS feed_last_error_at,
       now() AS read_at
  FROM crm.tenant t
  LEFT JOIN crm.membership_feed_state f ON true
 WHERE t.tenant_id = $1
"""
TENANT_LOCK_SQL = "SELECT status, lock_source FROM crm.tenant WHERE tenant_id = $1 FOR UPDATE"
# AA-978: an L1 lock on an agency the CIS feed locked takes the lock over (the feed lifts only its own sources).
TAKE_OVER_LOCK_SQL = "UPDATE crm.tenant SET lock_source = 'aa_ops' WHERE tenant_id = $1"
CIS_LOCK_SOURCES = frozenset({"cis_tenant", "cis_membership"})
SET_STATUS_SQL = "UPDATE crm.tenant SET status = $2 WHERE tenant_id = $1"
# First-owner rule (design S-01: "invite the first owner only while the agency has no owner and no travellers").
OWNER_EXISTS_SQL = """
SELECT 1 FROM crm.member WHERE role = 'owner' AND status IN ('active', 'invited') AND deleted_at IS NULL LIMIT 1
"""
OWNER_INVITE_PENDING_SQL = """
SELECT 1 FROM crm.member_invitation
 WHERE role = 'owner' AND status IN ('pending', 'claimed') AND expires_at > now() AND deleted_at IS NULL
 LIMIT 1
"""
# S263: the first-owner invitations AA sent that are still live (pending, or claimed but not accepted), newest first,
# locked: what "Withdraw owner invitation" closes. An owner's own team invitations are never touched here.
LIVE_OWNER_INVITES_SQL = """
SELECT invitation_id FROM crm.member_invitation
 WHERE role = 'owner' AND invited_by LIKE 'platform:%' AND status IN ('pending', 'claimed') AND deleted_at IS NULL
 ORDER BY created_at DESC, invitation_id
   FOR UPDATE
"""
# S263: what keeps a synthetic agency from being deleted by AA: a member who is (or is about to be) inside it.
LIVE_MEMBERS_SQL = "SELECT 1 FROM crm.member WHERE status IN ('active', 'invited') AND deleted_at IS NULL LIMIT 1"
DELETE_TENANT_LOCK_SQL = "SELECT status, is_synthetic, display_name FROM crm.tenant WHERE tenant_id = $1 FOR UPDATE"
DELETE_STATUSES = frozenset({"active", "locked", "offboarded"})
LIVE_INVITES_SQL = """
SELECT invitation_id FROM crm.member_invitation WHERE status IN ('pending', 'claimed') AND deleted_at IS NULL
 ORDER BY created_at, invitation_id FOR UPDATE
"""
TENANT_HOLD_SQL = "SELECT crm.legal_hold_blocks('tenant', NULL)"
# The agency row goes to the trash (base template trash columns; every context refuses a trashed agency, and the L1
# list skips it). purge_after = the backups window (lifecycle rules BACKUP_PURGE_DAYS).
TRASH_TENANT_SQL = """
UPDATE crm.tenant SET deleted_at = now(), deleted_by = $2, purge_after = now() + make_interval(days => $3)
 WHERE tenant_id = $1 AND deleted_at IS NULL
RETURNING purge_after
"""
# Any traveller row (trashed and archived ones too) or any stored inquiry (it becomes a traveller soon): a flag.
HOLDS_TRAVELLERS_SQL = "SELECT EXISTS (SELECT 1 FROM crm.traveler) OR EXISTS (SELECT 1 FROM crm.form_submission)"
ENTITLEMENT_SQL = """
SELECT plan, member_limit, allowed_features, insights_opt_out FROM crm.tenant_entitlement WHERE deleted_at IS NULL
"""
ALLOW_FEATURES_SQL = """
INSERT INTO crm.tenant_entitlement (allowed_features) VALUES ($1::text[])
ON CONFLICT (tenant_id) DO UPDATE SET allowed_features = EXCLUDED.allowed_features
RETURNING plan, member_limit, allowed_features, insights_opt_out
"""
OPT_OUT_SQL = """
INSERT INTO crm.tenant_entitlement (insights_opt_out) VALUES ($1)
ON CONFLICT (tenant_id) DO UPDATE SET insights_opt_out = EXCLUDED.insights_opt_out
RETURNING insights_opt_out
"""
ACTIVE_MEMBERS_SQL = """
SELECT count(DISTINCT actor_ref) FROM crm.audit_event
 WHERE actor_kind = 'user' AND actor_ref LIKE 'member:%' AND at > now() - interval '30 days'
"""
LAST_ACTIVITY_SQL = "SELECT max(at) FROM crm.audit_event WHERE actor_kind = 'user' AND actor_ref LIKE 'member:%'"
STORAGE_SQL = "SELECT coalesce(sum(size_bytes), 0) FROM crm.document"
BUDGET_SQL = "SELECT ai_budget_usd FROM crm.tenant_settings WHERE deleted_at IS NULL"


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _usd(value: Any) -> str:
    """A platform usage amount in USD as the contract string (at most 4 decimals)."""
    return f"{Decimal(value or 0).quantize(Decimal('0.0001')):f}"


def _feed(row: asyncpg.Record | dict[str, Any]) -> tuple[str, int | None]:
    """The CIS membership feed for the summary: :func:`app.cis.feed_state.health_word` (``failing`` / ``pending`` /
    ``stale`` / ``ok``, or ``unknown`` without the crm-013 row) and the seconds since its last successful pull (None
    before the first one). The same words as ``/crm/health/ready`` minus ``not_configured`` (a setting, not data)."""
    if row["feed_failures"] is None:
        return "unknown", None
    last_ok, now = row["feed_last_ok_at"], row["read_at"]
    state = feed_state.FeedState(None, int(row["feed_failures"]), row["feed_last_error_at"], last_ok)
    lag = max(0, int((now - last_ok).total_seconds())) if last_ok is not None else None
    return feed_state.health_word(state, now), lag


def _summary(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    feed, lag = _feed(row)
    return {
        "tenant_id": str(row["tenant_id"]),
        "agency_name": row["display_name"],
        "state": row["status"],
        # NULL on an agency locked before crm-013 (an AA lock, the trigger's reading) -> aa_ops; None when not locked.
        "lock_source": (row["lock_source"] or "aa_ops") if row["status"] == "locked" else None,
        "is_synthetic": bool(row["is_synthetic"]),
        "has_owner": bool(row["has_owner"]),
        "holds_travellers": bool(row["holds_travellers"]),
        "created_at": _iso(row["created_at"]),
        "membership_feed_state": feed,
        "membership_feed_lag_s": lag,
    }


async def _read_summary(ctx: SystemContext) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(SUMMARY_SQL, ctx.tenant_id)
    if row is None:  # trashed between the context check and here: never guess
        raise NotFound("tenant_not_found", "no agency with this id")
    return _summary(row)


def _read(ctx: SystemContext, reason: str, object_kind: str = "tenant") -> None:
    ctx.audit("ops_read", "allowed", object_kind, str(ctx.tenant_id), reason=reason)


# ── Reads ────────────────────────────────────────────────────────────────────────────────────────
async def list_agencies(pool: asyncpg.Pool, *, staff_sub: str, cursor: uuid.UUID | None, limit: int) -> dict[str, Any]:
    """Agencies in tenant id order (keyset on the id). The ids come from the crm-007 registry (the only cross-agency
    read: ``crm.tenant`` is FORCE RLS), then one platform context per agency (one ``ops_read`` event each)."""
    limit = max(1, min(limit, MAX_PAGE))
    ids = sorted(a.tenant_id for a in await registry.agencies(pool) if cursor is None or a.tenant_id > cursor)
    page, more = ids[:limit], len(ids) > limit
    items: list[dict[str, Any]] = []
    for tenant_id in page:
        try:
            async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=TENANT_STATUSES) as ctx:
                items.append(await _read_summary(ctx))
                _read(ctx, "list")
        except NotFound:
            continue  # registered but trashed: not an agency L1 manages
    return {"items": items, "next_cursor": str(page[-1]) if more and page else None}


async def agency_summary(pool: asyncpg.Pool, tenant_id: uuid.UUID, *, staff_sub: str) -> dict[str, Any]:
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=TENANT_STATUSES) as ctx:
        summary = await _read_summary(ctx)
        _read(ctx, "summary")
        return summary


async def agency_usage(pool: asyncpg.Pool, tenant_id: uuid.UUID, *, staff_sub: str) -> dict[str, Any]:
    """Usage meters only (S-01): seats, active members, storage, AI spend of the month against the budget, failed jobs,
    last activity. ``inbound_mail_30d`` is 0 until the inbox card (CRM-17) gives it a source."""
    today = dt.datetime.now(dt.UTC).date()
    start = today.replace(day=1)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=TENANT_STATUSES) as ctx:
        plan = await team.plan_of(ctx)
        budget = await ctx.conn.fetchval(BUDGET_SQL)
        out = {
            "tenant_id": str(tenant_id),
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "seats_used": int(plan["seats_used"]),
            "seats_allowed": int(plan["member_limit"]),
            "active_members_30d": int(await ctx.conn.fetchval(ACTIVE_MEMBERS_SQL) or 0),
            "storage_bytes": int(await ctx.conn.fetchval(STORAGE_SQL) or 0),
            "inbound_mail_30d": 0,
            "ai_cost_usd_month": _usd(await ctx.conn.fetchval(MONTH_SPEND_SQL)),
            "ai_budget_usd_month": _usd(DEFAULT_BUDGET_USD if budget is None else budget),
            "failed_jobs_24h": await queue.failed_count_24h(ctx.conn, str(tenant_id)),
            "last_activity_at": _iso(await ctx.conn.fetchval(LAST_ACTIVITY_SQL)),
        }
        _read(ctx, "usage")
        return out


def _entitlements(row: asyncpg.Record | dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {
            "plan": team.DEFAULT_PLAN["plan"],
            "seats_allowed": team.DEFAULT_PLAN["member_limit"],
            "features_allowed": [],
            "insights_opt_out": False,
        }
    return {
        "plan": row["plan"],
        "seats_allowed": int(row["member_limit"]),
        "features_allowed": sorted(row["allowed_features"] or []),
        "insights_opt_out": bool(row["insights_opt_out"]),
    }


async def agency_entitlements(pool: asyncpg.Pool, tenant_id: uuid.UUID, *, staff_sub: str) -> dict[str, Any]:
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=TENANT_STATUSES) as ctx:
        out = _entitlements(await ctx.conn.fetchrow(ENTITLEMENT_SQL))
        _read(ctx, "entitlements")
        return out


async def agency_data_requests(pool: asyncpg.Pool, tenant_id: uuid.UUID, *, staff_sub: str) -> dict[str, Any]:
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=TENANT_STATUSES) as ctx:
        items = await offboarding.agency_requests(ctx)
        _read(ctx, "data_requests")
        return {"items": items}


async def agency_failed_jobs(pool: asyncpg.Pool, tenant_id: uuid.UUID, *, staff_sub: str) -> dict[str, Any]:
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=TENANT_STATUSES) as ctx:
        rows = await queue.failed_jobs(ctx.conn, str(tenant_id))
        _read(ctx, "jobs")
        return {
            "items": [
                {
                    "job_id": r["job_id"],
                    "kind": r["kind"],
                    "attempt": int(r["attempt"]),
                    "max_attempts": int(r["max_attempts"]),
                    # crm-001: the job row keeps the error class only (a short code, never a message with data).
                    "error_class": (str(r["error"])[:80] if r["error"] else None),
                    "finished_at": _iso(r["finished_at"]),
                }
                for r in rows
            ]
        }


# ── Writes ───────────────────────────────────────────────────────────────────────────────────────
async def create_agency(
    pool: asyncpg.Pool,
    *,
    tenant_id: uuid.UUID,
    agency_name: str,
    is_synthetic: bool,
    default_locale: str,
    environment: str,
    staff_sub: str,
    ticket_ref: str,
) -> dict[str, Any]:
    """Create an agency without any member (S-01 "create agency"); the first owner is invited after. The rules are
    :func:`app.agencies.provision_agency`'s (Dev: synthetic only; prod: real only), create-only: an id that already
    exists is 409 ``tenant_exists`` and nothing is written (never a silent rename, whatever the agency's status). The
    ``agency_create`` event carries ``ticket:<REF>``. Returns the new agency's summary."""
    await agencies.provision_agency(
        pool,
        agencies.AgencySpec(
            tenant_id=tenant_id, display_name=agency_name, default_locale=default_locale, is_synthetic=is_synthetic
        ),
        (),
        environment=environment,
        actor=platform_actor(staff_sub),
        audit_object_ids=ticket_ids(ticket_ref),
        create_only=True,
    )
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=TENANT_STATUSES) as ctx:
        return await _read_summary(ctx)


async def _set_status(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, *, staff_sub: str, target: str, action: str, reason: str | None,
    ticket_ref: str,
) -> dict[str, Any]:  # fmt: skip
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=LOCKABLE) as ctx:
        row = await ctx.conn.fetchrow(TENANT_LOCK_SQL, tenant_id)
        current = row["status"]
        if current not in LOCKABLE:  # moved on between the context check and the row lock
            raise Conflict("tenant_status", f"the agency is {current}", state=current)
        if target == "locked" and current == "locked" and row["lock_source"] in CIS_LOCK_SOURCES:
            # AA-978: locked by the CIS feed. AA's lock must outlive a CIS tenant.unlocked, so AA takes it over
            # (crm-013 lets a platform actor name aa_ops); audited, unlike a lock that is AA's already.
            await ctx.conn.execute(TAKE_OVER_LOCK_SQL, tenant_id)
            ctx.audit(
                action, "allowed", "tenant", str(tenant_id), fields=("lock_source",), reason=reason,
                object_ids=ticket_ids(ticket_ref),
            )  # fmt: skip
        elif current != target:
            await ctx.conn.execute(SET_STATUS_SQL, tenant_id, target)
            ctx.audit(
                action, "allowed", "tenant", str(tenant_id), fields=("status",), reason=reason,
                object_ids=ticket_ids(ticket_ref),
            )  # fmt: skip
        return await _read_summary(ctx)


async def lock_agency(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, *, staff_sub: str, reason_code: str, ticket_ref: str
) -> dict[str, Any]:
    """active -> locked: no member, intake form, job or invitation context opens any more (403 ``tenant_locked`` /
    404 ``form_not_found``); queued jobs of the agency fail and can be retried after the unlock. Already locked by AA:
    a no-op without an audit event. Locked by the CIS membership feed (AA-978, ``lock_source`` cis_tenant /
    cis_membership): AA takes the lock over (``aa_ops``, audited), so a later CIS ``tenant.unlocked`` cannot lift it."""
    if reason_code not in LOCK_REASONS:
        raise Invalid("invalid_reason", ", ".join(LOCK_REASONS))
    return await _set_status(
        pool, tenant_id, staff_sub=staff_sub, target="locked", action="tenant_lock", reason=reason_code,
        ticket_ref=ticket_ref,
    )  # fmt: skip


async def unlock_agency(pool: asyncpg.Pool, tenant_id: uuid.UUID, *, staff_sub: str, ticket_ref: str) -> dict[str, Any]:
    return await _set_status(
        pool, tenant_id, staff_sub=staff_sub, target="active", action="tenant_unlock", reason=None,
        ticket_ref=ticket_ref,
    )  # fmt: skip


async def invite_first_owner(
    pool: asyncpg.Pool,
    tenant_id: uuid.UUID,
    *,
    staff_sub: str,
    owner_email: str,
    ticket_ref: str,
    keys: KeyProvider,
    mailer: Mailer,
    web_origin: str,
    ttl_hours: int,
) -> dict[str, Any]:
    """The first owner's invitation (S-01: only while the agency has no owner and no travellers; card R test). The
    address is encrypted (vault) with its blind index, mailed inside the transaction (a mail failure rolls back) and
    never stored in clear, logged or returned, not even masked. Claim / accept are AA-939's (``invitation_tx``)."""
    address = team.normalise_email(owner_email)
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub)) as ctx:
        await team.lock_seats(ctx)
        if await ctx.conn.fetchval(OWNER_EXISTS_SQL):
            raise Conflict("owner_exists", "the agency already has an owner; the owner invites the team")
        if await ctx.conn.fetchval(OWNER_INVITE_PENDING_SQL):
            raise Conflict("owner_invite_pending", "an owner invitation is already pending")
        if await ctx.conn.fetchval(HOLDS_TRAVELLERS_SQL):
            raise Conflict(
                "has_travellers", "the agency holds client data; a new owner comes only through an owner recovery"
            )
        made = await team.create_invitation(
            ctx,
            Vault(keys, ctx),
            mailer,
            address=address,
            role="owner",
            web_origin=web_origin,
            ttl_hours=ttl_hours,
            object_ids=ticket_ids(ticket_ref),
        )
    return {
        "tenant_id": str(tenant_id),
        "invitation_id": made["invitation_id"],
        "status": "pending",
        "expires_at": made["expires_at"],
    }


async def withdraw_owner_invitation(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, *, staff_sub: str, ticket_ref: str
) -> dict[str, Any]:
    """S263: close the first-owner invitation(s) AA sent that are still live (pending, or claimed and not accepted),
    so "Invite first owner" works again (``owner_invite_pending`` blocked it for the invitation's 7 days). Each one is
    ``revoked`` (its link answers 410 ``invitation_used``) with an ``owner_invite_withdraw`` event carrying
    ``ticket:<REF>``. The partner-pool account a claim created is left alone: if it never signed in, the claim of the
    next invitation to that address gives it a new temporary password (:mod:`app.members.directory`). 404
    ``owner_invite_not_found`` when there is none. Active and locked agencies (a locked agency's owner invitation can be
    withdrawn before it is unlocked)."""
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=LOCKABLE) as ctx:
        live = [r["invitation_id"] for r in await ctx.conn.fetch(LIVE_OWNER_INVITES_SQL)]
        if not live:
            raise NotFound("owner_invite_not_found", "no first-owner invitation is pending")
        for invitation_id in live:
            await ctx.conn.fetchval(team.REVOKE_SQL, invitation_id)
            ctx.audit(
                "owner_invite_withdraw", "allowed", "member_invitation", str(invitation_id), fields=("status",),
                object_ids=ticket_ids(ticket_ref),
            )  # fmt: skip
    log.info("crm_ops_owner_invite_withdrawn tenant_id=%s count=%s", tenant_id, len(live))
    return {"tenant_id": str(tenant_id), "invitation_id": str(live[0]), "status": "revoked"}


async def delete_test_agency(
    pool: asyncpg.Pool,
    tenant_id: uuid.UUID,
    *,
    staff_sub: str,
    agency_name: str,
    ticket_ref: str,
    keys: KeyProvider,
) -> dict[str, Any]:
    """S263: AA deletes a **synthetic** (test) agency it created. Refused unless the agency is synthetic (403
    ``not_synthetic``: a real agency is deleted only through its owner's confirmed offboarding), the typed name is the
    agency's name exactly (422 ``agency_name_mismatch``), no member is active or invited (409 ``has_members``), it
    holds no traveller or inquiry (409 ``has_travellers``), no legal hold is active (409 ``legal_hold``) and it is not
    being erased (409 ``tenant_status``). Then, in one transaction: every live invitation is revoked, the agency's
    keys are crypto-shredded (:meth:`KeyProvider.destroy`: anything encrypted under them, such as invited addresses,
    is unreadable for good) and the agency row goes to the trash (``deleted_at``; no context opens it any more and the
    L1 list leaves it out). Audited in the agency's own log: ``agency_delete`` with ``ticket:<REF>`` (plus the
    ``key_destroy`` event). The rows stay as the trash keeps them (DM-7.1); nothing can read their personal data.
    In the same transaction a ``crm_partner_accounts`` job is queued for the agency (:mod:`app.members.accounts`): the
    partner-pool accounts its invitation claims created and that never signed in, and that no other agency or Content
    studio invitation needs, are deleted (``partner_account_delete`` events in this agency's log)."""
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=DELETE_STATUSES) as ctx:
        row = await ctx.conn.fetchrow(DELETE_TENANT_LOCK_SQL, tenant_id)
        if row is None:  # trashed between the context check and the row lock
            raise NotFound("tenant_not_found", "no agency with this id")
        if not row["is_synthetic"]:
            ctx.audit("agency_delete", "denied", "tenant", str(tenant_id), reason="not_synthetic")
            raise Forbidden(
                "not_synthetic", "only a test agency can be deleted here; a real agency is offboarded by its owner"
            )
        if agency_name != row["display_name"]:
            raise Invalid("agency_name_mismatch", "type the agency name exactly as it is shown")
        if row["status"] not in DELETE_STATUSES:  # moved on between the context check and the row lock
            raise Conflict("tenant_status", f"the agency is {row['status']}", state=row["status"])
        if await ctx.conn.fetchval(LIVE_MEMBERS_SQL):
            raise Conflict("has_members", "the agency has active or invited members")
        if await ctx.conn.fetchval(HOLDS_TRAVELLERS_SQL):
            raise Conflict("has_travellers", "the agency holds travellers or inquiries")
        if await ctx.conn.fetchval(TENANT_HOLD_SQL):
            raise Conflict("legal_hold", "an active legal hold covers the agency")
        for r in await ctx.conn.fetch(LIVE_INVITES_SQL):
            await ctx.conn.fetchval(team.REVOKE_SQL, r["invitation_id"])
            ctx.audit("invite_revoke", "allowed", "member_invitation", str(r["invitation_id"]), reason="agency_delete")
        await keys.destroy(ctx)  # crypto-shred, after the checks, before the trash and the commit
        purge_after = await ctx.conn.fetchval(TRASH_TENANT_SQL, tenant_id, ctx.actor, BACKUP_PURGE_DAYS)
        if purge_after is None:
            raise NotFound("tenant_not_found", "no agency with this id")
        ctx.audit(
            "agency_delete", "allowed", "tenant", str(tenant_id), fields=("deleted_at",),
            reason="synthetic_test_agency", object_ids=ticket_ids(ticket_ref),
        )  # fmt: skip
        # S263 (AA-939 follow-up): the partner-pool accounts its invitation claims created and nobody signed in to
        # are deleted by the clean-up job, queued here so it runs only if this delete commits.
        await partner_accounts.queue_after_agency_delete(ctx)
    log.info("crm_ops_test_agency_deleted tenant_id=%s", tenant_id)
    return {"tenant_id": str(tenant_id), "status": "deleted"}


async def allow_features(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, *, staff_sub: str, features: list[str], ticket_ref: str
) -> dict[str, Any]:
    """AA allows features; the owner switches them on (this never writes ``crm.tenant_settings``)."""
    if len(set(features)) != len(features) or not set(features) <= set(FEATURES):
        raise Invalid("invalid_feature", f"unique codes from {', '.join(FEATURES)}")
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=LOCKABLE) as ctx:
        row = await ctx.conn.fetchrow(ALLOW_FEATURES_SQL, sorted(features))
        ctx.audit(
            "entitlements_allow", "allowed", "tenant_entitlement", str(tenant_id), fields=("allowed_features",),
            object_ids=ticket_ids(ticket_ref),
        )  # fmt: skip
        return _entitlements(row)


async def set_insights_opt_out(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, *, staff_sub: str, opt_out: bool, owner_request_ref: str
) -> dict[str, Any]:
    """Q2 3.12: the agency is in AA insights by default; AA sets the opt-out on the owner's written request."""
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=LOCKABLE) as ctx:
        value = await ctx.conn.fetchval(OPT_OUT_SQL, bool(opt_out))
        ctx.audit(
            "insights_opt_out", "allowed", "tenant_entitlement", str(tenant_id), fields=("insights_opt_out",),
            reason="opt_out" if opt_out else "opt_in", object_ids=ticket_ids(owner_request_ref),
        )  # fmt: skip
        return {"opt_out": bool(value)}


async def raise_data_request(
    pool: asyncpg.Pool,
    tenant_id: uuid.UUID,
    *,
    staff_sub: str,
    kind: str,
    ticket_ref: str,
    directory: PartnerDirectory | None,
    mailer: Mailer | None,
    web_origin: str,
) -> dict[str, Any]:
    """AA raises an agency export or deletion request; nothing runs until the owner confirms it in Settings > Data and
    privacy (crm-011: AA can never accept, confirm or run it). The owners get an AA system mail (best effort)."""
    purpose = DATA_REQUEST_PURPOSE.get(kind)
    if purpose is None:
        raise Invalid("invalid_kind", "agency_export or agency_deletion")
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub)) as ctx:
        made = await offboarding.raise_request(ctx, purpose)
        ctx.audit(
            "data_request_raise", "allowed", "erasure_request", made["request_id"], reason=kind,
            object_ids=ticket_ids(ticket_ref),
        )  # fmt: skip
        owners = await ctx.conn.fetch(OWNERS_SQL)
        agency = await ctx.conn.fetchval("SELECT display_name FROM crm.tenant WHERE tenant_id = $1", tenant_id)
    made["owners_notified"] = await mail_owners(
        owners,
        directory,
        mailer,
        lambda email: data_request_message(email, agency=agency or "AA Partner", kind=kind, link=web_origin),
        tenant_id=tenant_id,
        tag="data_request_notice",
    )
    return made


async def retry_agency_job(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, job_id: uuid.UUID, *, staff_sub: str, ticket_ref: str
) -> dict[str, Any]:
    """Re-queue one failed job of the agency (intake, dispatch, offboarding). A locked agency's job would fail again:
    409 ``tenant_locked``. A ``crm_offboard`` erase retry continues an erase the owner confirmed (AA-942)."""
    async with platform_tx(pool, tenant_id, actor=platform_actor(staff_sub), statuses=RETRY_STATUSES) as ctx:
        found = await queue.retry_failed(ctx.conn, str(job_id), str(tenant_id))
        if found is None:
            raise NotFound("job_not_found", "no failed job of this agency with this id")
        ctx.audit("job_retry", "allowed", "job", str(job_id), object_ids=ticket_ids(ticket_ref))
    log.info("crm_ops_job_retry tenant_id=%s job_id=%s", tenant_id, job_id)
    return {"job_id": str(job_id), "status": "queued"}


# ── crm.ops.v1 1.4 (AA-992): platform-wide counts for the CIS admin page /admin/data-coverage ───────────────────────
# Counts only: agencies per lifecycle status (plus how many are synthetic), members per status, and the AA catalog
# cache. No agency id, name, trip, proposal, traveller or money figure (ADR 0008a: the agency is the controller of its
# business data). ``crm.tenant`` / ``crm.member`` are FORCE RLS, so the agencies come from the crm-007 registry and each
# is counted inside its own platform context, like list_agencies. Unlike every other read here it writes NO
# ``ops_read`` event into the agencies' audits: it reads no agency's data, only its status and member statuses, and the
# CIS page refreshes it every 10 minutes (one event per agency per refresh would bury the owner's access log). The
# read is logged once per call (``crm_ops_data_coverage``, the agency count only); /jobs/summary writes no event either.
AGENCY_STATUSES = ("active", "locked", "offboarding", "offboarded")
MEMBER_STATUSES = ("active", "invited", "disabled")
COVERAGE_SYNTHETIC_SQL = "SELECT is_synthetic FROM crm.tenant WHERE tenant_id = $1"
COVERAGE_MEMBERS_SQL = "SELECT status, count(*)::int AS n FROM crm.member WHERE deleted_at IS NULL GROUP BY status"


async def platform_coverage(pool: asyncpg.Pool, *, staff_sub: str) -> dict[str, Any]:
    agencies_n = dict.fromkeys(AGENCY_STATUSES, 0) | {"synthetic": 0}
    members_n = dict.fromkeys(MEMBER_STATUSES, 0)
    for agency in await registry.agencies(pool):
        try:
            async with platform_tx(
                pool, agency.tenant_id, actor=platform_actor(staff_sub), statuses=TENANT_STATUSES
            ) as ctx:
                if ctx.status in agencies_n:
                    agencies_n[ctx.status] += 1
                if await ctx.conn.fetchval(COVERAGE_SYNTHETIC_SQL, ctx.tenant_id):
                    agencies_n["synthetic"] += 1
                for row in await ctx.conn.fetch(COVERAGE_MEMBERS_SQL):
                    if row["status"] in members_n:
                        members_n[row["status"]] += int(row["n"])
        except NotFound:
            continue  # registered but trashed: not an agency
    cache = await catalog_store.coverage(pool)
    log.info("crm_ops_data_coverage agencies=%d", sum(agencies_n[s] for s in AGENCY_STATUSES))
    return {
        "as_of": dt.datetime.now(dt.UTC).isoformat(),
        "agencies": agencies_n,
        "members": members_n,
        "catalog_cache": {
            "served": int(cache["served"] or 0),
            "paused": int(cache["paused"] or 0),
            "retired": int(cache["retired"] or 0),
            "last_full_pass_at": _iso(cache["last_full_pass_at"]),
            "last_ok_at": _iso(cache["last_ok_at"]),
            "failures": int(cache["failures"] or 0),
            "last_error_class": cache["last_error_class"],
        },
    }


# ── AA-966 (card CRM-34, spec module 17, design S-03): AA insights, crm.ops.v1 1.5 ──────────────────────────────
# Like platform_coverage: platform-wide totals only (no agency's data leaves app.reports.insights), so no per-agency
# event; one log line per call (the dimension and the agency count). Each agency: active only (a locked agency does
# not contribute), synthetic agencies only where the environment holds nothing else (Dev, tests), the opt-out read as
# crm_app BEFORE any fact is read, then the facts as crm_insights (the NOLOGIN role granted only the fact view).
INSIGHT_OPT_OUT_SQL = """
SELECT coalesce((SELECT insights_opt_out FROM crm.tenant_entitlement WHERE deleted_at IS NULL), false)
"""
# The destination dimension counts per trip and destination (crm-006); every other one counts each trip once
# (crm-031 v_insight_trip_fact: trips per band combination). app.reports.insights.aggregate checks the grain.
INSIGHT_FACTS_SQL = """
SELECT month, destination, budget_band, age_band, party_size, stage_reached, lost_reason_code, 1 AS trip_count
  FROM crm.v_insight_fact
 WHERE month >= $1 AND month <= $2
 LIMIT $3
"""
INSIGHT_TRIP_FACTS_SQL = """
SELECT month, NULL::text AS destination, budget_band, age_band, party_size, stage_reached, lost_reason_code, trip_count
  FROM crm.v_insight_trip_fact
 WHERE month >= $1 AND month <= $2
 LIMIT $3
"""
MAX_FACTS_PER_AGENCY = 50_000


async def platform_insights(
    pool: asyncpg.Pool,
    *,
    staff_sub: str,
    dimension: str,
    month_from: dt.date,
    month_to: dt.date,
    include_synthetic: bool,
) -> dict[str, Any]:
    facts: list[insights.Fact] = []
    included = opted_out = 0
    grain = insights.grain_for(dimension)
    facts_sql = INSIGHT_FACTS_SQL if grain == insights.TRIP_DESTINATION else INSIGHT_TRIP_FACTS_SQL
    for agency in await registry.agencies(pool):
        try:
            async with platform_tx(pool, agency.tenant_id, actor=platform_actor(staff_sub)) as ctx:
                if not include_synthetic and await ctx.conn.fetchval(COVERAGE_SYNTHETIC_SQL, ctx.tenant_id):
                    continue
                if await ctx.conn.fetchval(INSIGHT_OPT_OUT_SQL):
                    opted_out += 1
                    continue
                async with as_insights_reader(ctx):
                    rows = await ctx.conn.fetch(facts_sql, month_from, month_to, MAX_FACTS_PER_AGENCY)
        except (NotFound, Conflict):
            continue  # trashed, locked or offboarding: not counted
        included += 1
        facts.extend(
            insights.Fact(
                tenant_id=agency.tenant_id,
                month=r["month"],
                destination=r["destination"],
                budget_band=r["budget_band"],
                age_band=r["age_band"],
                party_size=int(r["party_size"] or 0),
                stage_reached=r["stage_reached"],
                lost_reason_code=r["lost_reason_code"],
                trip_count=int(r["trip_count"] or 0),
                grain=grain,
            )
            for r in rows
        )
    result = insights.aggregate(facts, dimension)
    log.info("crm_ops_insights dimension=%s agencies=%d opted_out=%d", dimension, included, opted_out)
    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "dimension": dimension,
        "from": month_from.isoformat(),
        "to": month_to.isoformat(),
        "agencies_included": included,
        "agencies_opted_out": opted_out,
        **result,
    }


# ── AA-964 (card CRM-33a; root ADR 0009 §8, root ADR 0008a §6): the CRM ledger rows, crm.ops.v1 1.6 ─────────────────
# Row-level ``crm.llm_call`` for the CIS admin "Calls" tab, the CRM's second ledger under the ledger v2 contract. Only
# the scrubbed fields: ids, codes, numbers and the hash of the scrubbed prompt; never the scrub receipt's per-kind
# counts, the placeholder map, the member who asked, a prompt or an output. One ``ops_read`` event (reason
# ``llm_calls``) in each agency whose rows were returned, so the owner's access log shows that AA read its AI ledger.
LLM_CALLS_MAX_DAYS = 31
LLM_CALLS_MAX_LIMIT = 500
LLM_CALLS_SQL = """
SELECT llm_call_id, at, app, stage, role, provider, model, family, connection_key, deployment_wire_id, attempt_no,
       request_id, job_id, outcome, refusal_code, error_class, input_tokens, output_tokens, cache_read_tokens,
       cache_write_tokens, units, unit_kind, cost_usd_est, pricing_mode, latency_ms, prompt_hash, prompt_version,
       is_shadow, is_experiment, (scrub_receipt->>'entities')::int AS scrub_entities
  FROM crm.llm_call
 WHERE at >= $1 AND at < $2
   AND ($3::text IS NULL OR stage = $3)
   AND ($4::text IS NULL OR outcome = $4)
   AND ($5::timestamptz IS NULL OR (at, llm_call_id) < ($5, $6::uuid))
 ORDER BY at DESC, llm_call_id DESC
 LIMIT $7
"""


def llm_calls_cursor(value: str | None) -> tuple[dt.datetime | None, uuid.UUID | None]:
    """``<at ISO>~<call id>`` (the last row of the previous page) -> the keyset; anything else is 422."""
    if not value:
        return None, None
    at_text, sep, call_text = value.partition("~")
    try:
        at = dt.datetime.fromisoformat(at_text)
        call_id = uuid.UUID(call_text)
    except ValueError:
        raise Invalid("invalid_cursor", "the cursor is the next_cursor of a previous page") from None
    if not sep or at.tzinfo is None:
        raise Invalid("invalid_cursor", "the cursor is the next_cursor of a previous page")
    return at, call_id


def _llm_call_row(tenant_id: uuid.UUID, r: Any) -> dict[str, Any]:
    def num(value: Any) -> int | None:
        return int(value) if value is not None else None

    return {
        "call_id": str(r["llm_call_id"]),
        "tenant_id": str(tenant_id),
        "at": r["at"].isoformat(),
        "app": r["app"],
        "stage": r["stage"],
        "role": r["role"],
        "provider": r["provider"],
        "model": r["model"],
        "family": r["family"],
        "connection_key": r["connection_key"],
        "deployment_wire_id": r["deployment_wire_id"],
        "attempt_no": num(r["attempt_no"]),
        "provider_request_id": r["request_id"],
        "job_id": str(r["job_id"]) if r["job_id"] else None,
        "status": r["outcome"],
        "refusal_code": r["refusal_code"],
        "error_class": r["error_class"],
        "input_tokens": int(r["input_tokens"]),
        "output_tokens": int(r["output_tokens"]),
        "cache_read_tokens": num(r["cache_read_tokens"]),
        "cache_write_tokens": num(r["cache_write_tokens"]),
        "units": num(r["units"]),
        "unit_kind": r["unit_kind"],
        "cost_usd": f"{Decimal(r['cost_usd_est']):.6f}",
        "pricing_mode": r["pricing_mode"],
        "latency_ms": int(r["latency_ms"]),
        "prompt_hash": r["prompt_hash"],
        "prompt_version": r["prompt_version"],
        "is_shadow": bool(r["is_shadow"]),
        "is_experiment": bool(r["is_experiment"]),
        "scrub_entities": num(r["scrub_entities"]),
    }


async def platform_llm_calls(
    pool: asyncpg.Pool,
    *,
    staff_sub: str,
    start: dt.date,
    end: dt.date,
    tenant_id: uuid.UUID | None = None,
    stage: str | None = None,
    status: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """The newest ledger rows first, across every agency (or one), ``start`` .. ``end`` UTC days inclusive."""
    if end < start or (end - start).days >= LLM_CALLS_MAX_DAYS:
        raise Invalid("invalid_range", f"from <= to, at most {LLM_CALLS_MAX_DAYS} days")
    limit = max(1, min(limit, LLM_CALLS_MAX_LIMIT))
    before_at, before_id = llm_calls_cursor(cursor)
    lower = dt.datetime.combine(start, dt.time(), tzinfo=dt.UTC)
    upper = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), tzinfo=dt.UTC)
    ids = [tenant_id] if tenant_id is not None else [a.tenant_id for a in await registry.agencies(pool)]
    rows: list[tuple[dt.datetime, uuid.UUID, dict[str, Any]]] = []
    for agency_id in ids:
        try:
            async with platform_tx(pool, agency_id, actor=platform_actor(staff_sub), statuses=TENANT_STATUSES) as ctx:
                found = await ctx.conn.fetch(
                    LLM_CALLS_SQL, lower, upper, stage, status, before_at, before_id, limit + 1
                )
                if found:
                    _read(ctx, "llm_calls")
                rows.extend((r["at"], r["llm_call_id"], _llm_call_row(agency_id, r)) for r in found)
        except NotFound:
            if tenant_id is not None:
                raise
            continue  # registered but trashed
    rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    page = rows[:limit]
    more = len(rows) > limit
    next_cursor = f"{page[-1][0].isoformat()}~{page[-1][1]}" if more and page else None
    log.info("crm_ops_llm_calls agencies=%d rows=%d", len(ids), len(page))
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "rows": [r for _, _, r in page],
        "next_cursor": next_cursor,
    }
