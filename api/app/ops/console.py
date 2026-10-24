"""The L1 platform console's agency operations (AA-941, card CRM-13a; design v3 S-01 "Agencies (CRM)", §4.9).

Every function opens :func:`app.tenancy.platform_tx` for one agency with actor ``platform:<staff sub>``, so each write
and each read lands in **that agency's own audit** with actor kind ``aa_ops`` (the owner sees it in the access log):

- reads write one ``ops_read`` event (reason ``list`` / ``summary`` / ``usage`` / ``entitlements`` /
  ``data_requests`` / ``jobs``; design §4.9 "AA ops reads");
- writes write their own event with the L1 ticket reference as an object id ``ticket:<REF>`` (the audit takes codes
  and ids only, never free text).

Only ``app.routers.ops`` calls these functions (writes need the WORKFORCE group ``crm-platform-admin``), and only this
module and ``app/members/recovery.py`` open ``platform_tx`` (``tests/unit/test_code_rules.py``).

What L1 sees: ids, the agency's business name, states, flags and usage meters. Never a trip, a pipeline stage, a
price, revenue or a person's data; the response allow-list (:mod:`app.ops.serializer`) enforces it on every response.

Statuses: reads accept every agency status; lock / unlock and the allowed features / insights opt-out accept active
and locked; a retry accepts active and offboarding (an offboarding agency's erase job); the first-owner invite and AA
data requests need an active agency (the owner must be able to answer in the CRM).
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
from app.errors import Conflict, Invalid, NotFound
from app.jobs import queue
from app.lifecycle import offboarding
from app.llm.gateway import DEFAULT_BUDGET_USD, MONTH_SPEND_SQL
from app.members import team
from app.members.directory import PartnerDirectory
from app.members.mail import Mailer, data_request_message
from app.members.recovery import OWNERS_SQL, mail_owners, platform_actor, ticket_ids
from app.tenancy import TENANT_STATUSES, SystemContext, platform_tx
from app.vault import KeyProvider, Vault

log = logging.getLogger(__name__)

LOCKABLE = frozenset({"active", "locked"})
RETRY_STATUSES = frozenset({"active", "offboarding"})
LOCK_REASONS = ("owner_request", "payment", "abuse", "security", "test")
FEATURES = ("ai_assist", "whatsapp_sms", "stripe_connect", "campaigns", "custom_domain", "insights_export")
DATA_REQUEST_PURPOSE = {"agency_export": "export_only", "agency_deletion": "offboard"}
MAX_PAGE = 200

SUMMARY_SQL = """
SELECT t.tenant_id, t.display_name, t.status, t.is_synthetic, t.created_at,
       EXISTS (SELECT 1 FROM crm.member m
                WHERE m.role = 'owner' AND m.status = 'active' AND m.deleted_at IS NULL) AS has_owner,
       (EXISTS (SELECT 1 FROM crm.traveler) OR EXISTS (SELECT 1 FROM crm.form_submission)) AS holds_travellers
  FROM crm.tenant t
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


def _summary(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    return {
        "tenant_id": str(row["tenant_id"]),
        "agency_name": row["display_name"],
        "state": row["status"],
        "is_synthetic": bool(row["is_synthetic"]),
        "has_owner": bool(row["has_owner"]),
        "holds_travellers": bool(row["holds_travellers"]),
        "created_at": _iso(row["created_at"]),
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
