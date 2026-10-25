"""``crm_membership_pull`` (AA-978, CRM-P1): the CRM consumer of the CIS feed ``tenant.membership.v1``.

The CRM treats CIS as a **revocation and lifecycle source, never as a positive gate**: members that exist only in the
CRM have no CIS row, so a missing CIS membership never refuses anything, and no CIS event ever creates a CRM member or
changes a CRM role (root contract text; delivery plan CRM-P1 "CIS can create agencies and revoke members only").

One run (every 20 s, a Recurring kind, deploy-gate and DB Migrate idle exempt):

1. Not configured (``CIS_API_URL`` or ``M2M_CLIENT`` empty) -> ``not_configured``, nothing else. After failures the
   run waits :func:`backoff_seconds` from the last error (``backoff``).
2. A token for scope ``cis.tenant/read`` only (:mod:`app.cis.m2m`), then ``GET <CIS_API_URL>/tenant/membership/v1/
   events?after=<cursor>&limit=200``, at most :data:`MAX_PAGES` pages. A 401 / 403 / 404 / 5xx, a transport error or
   a malformed page records the error class in ``crm.membership_feed_state`` and ends the run **successfully** (no
   failed ``crm.job`` row piles up while CIS is down; the next slot retries after the backoff).
3. Each event, in its own transaction (:func:`app.tenancy.membership_feed_tx`): already in ``crm.membership_inbox`` ->
   skipped (a replayed page is a no-op); else checked against the contract (:mod:`app.cis.contract`) and applied by
   the rules below, then recorded in the inbox with its outcome in the same transaction. Only an agency the CRM holds
   (active or locked) gets an inbox row: an event of an unknown or offboarding agency changes nothing and is counted
   as ignored without a row, so no inbox row outlives its agency's erase.
   If the database refuses one event for a reason of that event (a CRM guard ``CRMxx``, an integrity error),
   the event is recorded as rejected / ``apply_refused`` (ERROR log ``crm_membership_event_apply_refused``) and the
   run goes on, so one event never blocks the feed for every agency. Any other error (a lost connection, a deadlock,
   a privilege or schema problem, a bug) ends the run as ``apply_error`` with the cursor before that event.
4. The cursor moves to ``next_cursor`` only from the value this run read (compare and set). Members disabled by the
   run are signed out of the partner pool after the commit (best effort, as ``app.members.team.remove_member``).

Rules (outcome / reason code in the inbox; every applied change also writes the agency's audit, actor
``platform:feed-tenant-membership``, kind ``aa_ops``, with ``cis_event:<id>`` as an object id):

- an event that breaks the contract (an unknown field, a ``crm_role`` anywhere, a role outside owner / editor /
  viewer, ...) -> **rejected**, nothing changes (AA-978 refusal case);
- a tenant the CRM has no agency for -> ignored ``unknown_agency``; an agency offboarding / offboarded -> ignored
  ``agency_offboarding`` (neither gets an inbox row);
- ``tenant.created``, ``membership.invited`` / ``activated`` / ``role_changed`` -> ignored ``informational``;
- ``membership.removed`` with an identity -> the CRM member with the same ``(iss, sub)`` in that agency is disabled
  (``no_crm_member`` when there is none; ``no_identity`` for a withdrawn invitation, which never had one). The feed
  takes the crm-008 per-agency owner lock first, so it and a member request that demotes an owner never both pass
  the last-owner count; if they were its last active owner, the agency is locked with
  ``lock_source = cis_membership`` (card E): only the AA-941 owner recovery (or an AA unlock) reopens it;
- ``tenant.locked`` / ``tenant.deleted`` -> an active agency is locked with ``lock_source = cis_tenant``;
- ``tenant.unlocked`` -> lifted only when ``lock_source = cis_tenant`` (an AA lock or a last-owner lock stays; an L1
  lock placed on a CIS-locked agency turns its source into ``aa_ops``, :func:`app.ops.console.lock_agency`).

Monitoring: ``/crm/health/ready`` reports ``membership_feed`` (:func:`app.cis.feed_state.health`; never fails
readiness), and from :data:`STUCK_FAILURES` consecutive failures on every failed run logs the ERROR line
``crm_membership_feed_stuck`` (the CloudWatch metric filter / alarm keys on it).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.cis import contract, feed_state
from app.cis.http import DEFAULT_TIMEOUT_S, HttpError, Transport, urllib_transport
from app.cis.m2m import TokenError, TokenProvider
from app.jobs.queue import Job
from app.members.directory import PartnerDirectory
from app.tenancy import SystemContext, membership_feed_tx

log = logging.getLogger(__name__)

SCOPE = "cis.tenant/read"
EVENTS_PATH = "/tenant/membership/v1/events"
PAGE_LIMIT = 200
MAX_PAGES = 5
BACKOFF_BASE_S = 20
BACKOFF_MAX_S = 300
INFORMATIONAL = frozenset({"tenant.created", "membership.invited", "membership.activated", "membership.role_changed"})
LOCK_EVENTS = {"tenant.locked": "cis_tenant_locked", "tenant.deleted": "cis_tenant_deleted"}
# About half an hour of failures with the 5-minute cap (20 + 40 + 80 + 160 + 6 x 300 s).
STUCK_FAILURES = 10
# SQLSTATE classes that belong to the event, not to the system: a CRM guard (CRMxx) or an integrity error. Anything
# else (connection, deadlock / serialization, lock timeout, privileges, a missing object, and data errors: asyncpg
# reports a wrong argument type as 22000 before the server sees it, which is a bug in this code) is systemic.
REFUSAL_CLASSES = frozenset({"CR", "23"})
APPLYING = frozenset({"active", "locked"})

# Inbox reason codes (crm.membership_inbox.reason_code) besides the contract's (app.cis.contract.check_event).
REASONS = frozenset(
    {
        "member_disabled",
        "agency_locked_no_owner",
        "agency_locked",
        "agency_unlocked",
        "already_disabled",
        "already_locked",
        "not_locked",
        "lock_not_from_cis_tenant",
        "no_crm_member",
        "no_identity",
        "unknown_agency",
        "agency_offboarding",
        "informational",
        "apply_refused",
    }
)

# The crm-008 key (crm.guard_member_write): a member request that demotes or disables an owner takes it before its
# last-owner count; the feed is a platform actor, so the trigger skips it for the feed and the feed takes it here.
OWNER_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('crm.member.owner:' || $1::text, 0))"
MEMBER_SQL = """
SELECT member_id, role, status FROM crm.member
 WHERE cognito_iss = $1 AND cognito_sub = $2 AND deleted_at IS NULL
 FOR UPDATE
"""
DISABLE_SQL = "UPDATE crm.member SET status = 'disabled' WHERE member_id = $1"
ACTIVE_OWNERS_SQL = """
SELECT count(*) FROM crm.member WHERE role = 'owner' AND status = 'active' AND deleted_at IS NULL
"""
TENANT_LOCK_SQL = "SELECT status, lock_source FROM crm.tenant WHERE tenant_id = $1 FOR UPDATE"
SET_LOCK_SQL = "UPDATE crm.tenant SET status = 'locked', lock_source = $2 WHERE tenant_id = $1"
UNLOCK_SQL = "UPDATE crm.tenant SET status = 'active' WHERE tenant_id = $1"
SEEN_SQL = "SELECT 1 FROM crm.membership_inbox WHERE event_id = $1"
INBOX_SQL = """
INSERT INTO crm.membership_inbox (event_id, event_type, outcome, reason_code) VALUES ($1, $2, $3, $4)
ON CONFLICT (event_id) DO NOTHING
RETURNING event_id
"""


class _Replayed(Exception):
    """Another pull recorded the same event first: roll this one back."""


@dataclass(frozen=True)
class FeedConfig:
    """Where and how to pull. ``base_url`` = ``CIS_API_URL`` (an origin, no trailing slash)."""

    base_url: str
    tokens: TokenProvider
    transport: Transport = urllib_transport
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass
class RunCounts:
    applied: int = 0
    ignored: int = 0
    rejected: int = 0
    replayed: int = 0
    unrecorded: int = 0
    pages: int = 0
    signed_out: int = 0
    sign_outs: list[str] = field(default_factory=list, repr=False)

    def result(self, status: str, reason_code: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": status,
            "applied_count": self.applied,
            "ignored_count": self.ignored,
            "rejected_count": self.rejected,
            "replayed_count": self.replayed,
            "unrecorded_count": self.unrecorded,
            "page_count": self.pages,
            "signed_out_count": self.signed_out,
        }
        if reason_code is not None:
            out["reason_code"] = reason_code
        return out


def backoff_seconds(failures: int) -> int:
    """Wait after ``failures`` consecutive failures: 20 s, 40 s, 80 s, ... capped at 5 minutes."""
    if failures <= 0:
        return 0
    return min(BACKOFF_BASE_S * 2 ** min(failures - 1, 10), BACKOFF_MAX_S)


def http_error_class(status: int) -> str:
    if status == 401:
        return "cis_unauthorized"
    if status == 403:
        return "cis_forbidden"
    if status == 404:
        return "cis_not_found"
    if status == 429:
        return "cis_throttled"
    if status >= 500:
        return "cis_unavailable"
    return "cis_unexpected_status"


def lifecycle_decision(event_type: str, status: str, lock_source: str | None) -> tuple[str, str, str | None]:
    """``(outcome, reason, change)`` of a tenant lifecycle event on an agency in ``status`` (active or locked);
    ``change`` is ``lock`` / ``unlock`` / None."""
    if event_type in LOCK_EVENTS:
        if status == "active":
            return "applied", "agency_locked", "lock"
        return "ignored", "already_locked", None
    # tenant.unlocked: lift only CIS's own lifecycle lock (an AA lock or a last-owner lock stays).
    if status != "locked":
        return "ignored", "not_locked", None
    if lock_source == "cis_tenant":
        return "applied", "agency_unlocked", "unlock"
    return "ignored", "lock_not_from_cis_tenant", None


def events_url(base_url: str, cursor: str | None) -> str:
    query: dict[str, str] = {"limit": str(PAGE_LIMIT)}
    if cursor:
        query["after"] = cursor
    return f"{base_url}{EVENTS_PATH}?{urllib.parse.urlencode(query)}"


async def _apply_removal(ctx: SystemContext, event_id: uuid.UUID, membership: dict[str, Any]) -> tuple[str, str, str]:
    """Returns (outcome, reason, sub to sign out or '')."""
    identity = membership.get("identity")
    if not identity:
        return "ignored", "no_identity", ""  # a withdrawn invitation: never activated, so no CRM member to match
    # Serialise with a member request that demotes or disables an owner (crm-008 key). Each statement below then takes
    # a fresh READ COMMITTED snapshot after the lock, so the last-owner count sees the other side's commit.
    await ctx.conn.execute(OWNER_LOCK_SQL, str(ctx.tenant_id))
    member = await ctx.conn.fetchrow(MEMBER_SQL, identity["iss"], identity["sub"])
    if member is None:
        return "ignored", "no_crm_member", ""
    if member["status"] == "disabled":
        return "ignored", "already_disabled", ""
    ids = (f"cis_event:{event_id}",)
    await ctx.conn.execute(DISABLE_SQL, member["member_id"])
    ctx.audit(
        "member_deactivate",
        "allowed",
        "member",
        str(member["member_id"]),
        fields=("status",),
        reason="cis_membership_removed",
        object_ids=ids,
    )
    reason = "member_disabled"
    was_owner = member["role"] == "owner" and member["status"] == "active"
    if was_owner and int(await ctx.conn.fetchval(ACTIVE_OWNERS_SQL) or 0) == 0:
        tenant = await ctx.conn.fetchrow(TENANT_LOCK_SQL, ctx.tenant_id)
        # The last owner left in CIS: recovery only (card E). A CIS lifecycle lock becomes this stricter one; an AA
        # lock stays AA's (AA decides when to unlock; a recovery is still needed for an owner).
        if tenant["status"] == "active" or (tenant["status"] == "locked" and tenant["lock_source"] == "cis_tenant"):
            await ctx.conn.execute(SET_LOCK_SQL, ctx.tenant_id, "cis_membership")
            ctx.audit(
                "tenant_lock", "allowed", "tenant", str(ctx.tenant_id), fields=("status",),
                reason="cis_last_owner_removed", object_ids=ids,
            )  # fmt: skip
            reason = "agency_locked_no_owner"
    return "applied", reason, identity["sub"]


async def _apply_lifecycle(ctx: SystemContext, event_id: uuid.UUID, event_type: str) -> tuple[str, str]:
    tenant = await ctx.conn.fetchrow(TENANT_LOCK_SQL, ctx.tenant_id)
    outcome, reason, change = lifecycle_decision(event_type, tenant["status"], tenant["lock_source"])
    ids = (f"cis_event:{event_id}",)
    if change == "lock":
        await ctx.conn.execute(SET_LOCK_SQL, ctx.tenant_id, "cis_tenant")
        ctx.audit(
            "tenant_lock", "allowed", "tenant", str(ctx.tenant_id), fields=("status",),
            reason=LOCK_EVENTS[event_type], object_ids=ids,
        )  # fmt: skip
    elif change == "unlock":
        await ctx.conn.execute(UNLOCK_SQL, ctx.tenant_id)
        ctx.audit(
            "tenant_unlock", "allowed", "tenant", str(ctx.tenant_id), fields=("status",),
            reason="cis_tenant_unlocked", object_ids=ids,
        )  # fmt: skip
    return outcome, reason


async def _decide_and_apply(ctx: SystemContext, event_id: uuid.UUID, event: dict[str, Any]) -> tuple[str, str, str]:
    violation = contract.check_event(event)
    if violation is not None:
        return "rejected", violation, ""
    if ctx.status is None:
        return "ignored", "unknown_agency", ""
    if ctx.status not in APPLYING:
        return "ignored", "agency_offboarding", ""
    event_type = event["type"]
    if event_type in INFORMATIONAL:
        return "ignored", "informational", ""  # never a new CRM member, never a CRM role change
    if event_type == "membership.removed":
        return await _apply_removal(ctx, event_id, event["membership"])
    outcome, reason = await _apply_lifecycle(ctx, event_id, event_type)
    return outcome, reason, ""


async def process_event(pool: asyncpg.Pool, event: Any, counts: RunCounts, issuer: str | None) -> None:
    """One event: recorded once in the inbox with its outcome, applied in the same transaction."""
    ids = (event.get("event_id"), event.get("tenant_id")) if isinstance(event, dict) else (None, None)
    try:
        event_id, tenant_id = uuid.UUID(str(ids[0])), uuid.UUID(str(ids[1]))
    except ValueError:
        counts.unrecorded += 1  # no key to record it under: logged (codes only), the cursor still moves past it
        log.warning("crm_membership_event_unrecorded reason=no_event_or_tenant_id")
        return
    raw_type = event.get("type")
    event_type = raw_type if isinstance(raw_type, str) and raw_type in contract.EVENT_TYPES else None
    sign_out = ""
    try:
        async with membership_feed_tx(pool, tenant_id) as ctx:
            if await ctx.conn.fetchval(SEEN_SQL, event_id) is not None:
                raise _Replayed
            outcome, reason, sign_out = await _decide_and_apply(ctx, event_id, event)
            if outcome != "rejected" and reason not in REASONS:  # pragma: no cover — a code path above misnamed it
                raise ValueError(f"unknown membership feed reason {reason}")
            # No row for an unknown or offboarding agency: nothing changed, and no erase would ever reach the row.
            if (
                ctx.status in APPLYING
                and await ctx.conn.fetchval(INBOX_SQL, event_id, event_type, outcome, reason) is None
            ):
                raise _Replayed
    except _Replayed:
        counts.replayed += 1
        return
    except asyncpg.UniqueViolationError:
        counts.replayed += 1  # the same event id under another tenant id: recorded already, not applied twice
        return
    except asyncpg.PostgresError as exc:
        if (exc.sqlstate or "")[:2] not in REFUSAL_CLASSES:
            raise  # systemic: the run stops before this event (apply_error)
        # The database refused this event for a reason of its own: record it as rejected and go on, so one event
        # never holds back every later revocation. ERROR: a refused event is a bug or a rule this code does not know.
        log.error("crm_membership_event_apply_refused event_id=%s sqlstate=%s", event_id, exc.sqlstate)
        await _record_refusal(pool, event_id, tenant_id, event_type)
        counts.rejected += 1
        return
    setattr(counts, outcome, getattr(counts, outcome) + 1)
    if outcome == "rejected":
        log.warning("crm_membership_event_rejected event_id=%s reason=%s", event_id, reason)
    if sign_out and issuer is not None and event["membership"]["identity"]["iss"] == issuer:
        counts.sign_outs.append(sign_out)


async def _record_refusal(
    pool: asyncpg.Pool, event_id: uuid.UUID, tenant_id: uuid.UUID, event_type: str | None
) -> None:
    """The inbox row of an event the database refused (its own transaction; the event's work was rolled back)."""
    async with membership_feed_tx(pool, tenant_id) as ctx:
        if ctx.status in APPLYING:
            await ctx.conn.fetchval(INBOX_SQL, event_id, event_type, "rejected", "apply_refused")


class MembershipFeed:
    def __init__(
        self,
        config: FeedConfig | None,
        directory: PartnerDirectory | None,
        *,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._config = config
        self._directory = directory
        self._now = now
        self._warned = False

    async def __call__(self, pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        counts = RunCounts()
        if self._config is None:
            if not self._warned:
                log.warning("crm_membership_pull not_configured (CIS_API_URL / M2M_CLIENT)")
                self._warned = True
            return counts.result("not_configured")
        state = await feed_state.load(pool)
        if state.failures and state.last_error_at is not None:
            wait = dt.timedelta(seconds=backoff_seconds(state.failures))
            if self._now() < state.last_error_at + wait:
                return counts.result("backoff")
        try:
            await self._pull(pool, state.cursor, counts)
        except _FeedError as exc:
            failures = await feed_state.fail(pool, exc.error_class)
            log.warning("crm_membership_pull_failed error=%s failures=%d", exc.error_class, failures)
            if failures >= STUCK_FAILURES:
                # Revocations stopped reaching the CRM: the alarm line (an error class code and a count only).
                log.error("crm_membership_feed_stuck error=%s failures=%d", exc.error_class, failures)
            return counts.result("error", exc.error_class)
        finally:
            await self._sign_out(counts)
        return counts.result("ok")

    async def _pull(self, pool: asyncpg.Pool, cursor: str | None, counts: RunCounts) -> None:
        config = self._config
        assert config is not None  # noqa: S101 — checked by the caller
        issuer = self._directory.issuer if self._directory is not None else None
        for _ in range(MAX_PAGES):
            items, next_cursor = await self._page(config, cursor)
            counts.pages += 1
            for item in items:
                try:
                    await process_event(pool, item, counts, issuer)
                except asyncpg.PostgresError as exc:
                    # A systemic database error (process_event records an event-specific refusal itself): stop here,
                    # keep the cursor before this event, and back off instead of failing a job row every 20 s.
                    log.error("crm_membership_event_apply_failed sqlstate=%s", exc.sqlstate)
                    raise _FeedError("apply_error") from None
                except Exception as exc:  # noqa: BLE001 — a lost connection or a bug: the same backoff, never a crash
                    log.error("crm_membership_event_apply_failed error=%s", type(exc).__name__)
                    raise _FeedError("apply_error") from None
            if next_cursor and next_cursor != cursor:
                if not await feed_state.advance(pool, cursor, next_cursor):
                    log.info("crm_membership_pull cursor moved by another run; stopping")
                    return
                cursor = next_cursor
            else:
                await feed_state.succeed(pool)
            if len(items) < PAGE_LIMIT:
                return

    async def _page(self, config: FeedConfig, cursor: str | None) -> tuple[list[Any], str]:
        try:
            token = await config.tokens.token(SCOPE)
        except TokenError as exc:
            raise _FeedError(exc.error_class) from None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            resp = await config.transport("GET", events_url(config.base_url, cursor), headers, None, config.timeout_s)
        except HttpError as exc:
            raise _FeedError(f"cis_{exc.error_class}") from None
        if resp.status == 401:
            config.tokens.invalidate(SCOPE)  # revoked or rotated: the next run asks the pool again
        if resp.status != 200:
            raise _FeedError(http_error_class(resp.status))
        try:
            return contract.parse_page(json.loads(resp.body))
        except (ValueError, contract.PageError):
            raise _FeedError("cis_contract") from None

    async def _sign_out(self, counts: RunCounts) -> None:
        if self._directory is None:
            return
        for sub in counts.sign_outs:
            try:
                await self._directory.global_sign_out(sub)
                counts.signed_out += 1
            except Exception as exc:  # noqa: BLE001 — the membership is already disabled; tenant_tx() refuses anyway
                log.warning("crm_member_sign_out_failed source=cis_feed error=%s", type(exc).__name__)


class _FeedError(Exception):
    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class
