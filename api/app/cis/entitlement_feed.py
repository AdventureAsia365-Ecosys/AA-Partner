"""``crm_entitlement_pull`` (AA-1028 R1 slice 2, root ADR 0019 d1 / d2): the CRM consumer of the CIS contract
``tenant.entitlements.v1``. ``crm.tenant_entitlement`` and the CIS part of ``crm.tenant.status`` become a read-only
projection of the CIS tenant master; once this pull has projected an agency (``projected_at`` set) the L1 console no
longer writes its allowed features (crm.ops.v1 1.11: ``PUT /entitlements-allowed`` answers 409
``entitlements_managed_by_cis``; before that the agency is ``managed_by`` ops and the console still writes them).

One run (a due-now maintenance hook every :data:`SLOT_SECONDS`, :class:`EntitlementPullSchedule`; not deploy-gate or
DB Migrate exempt: a row exists only while it is due or running, a few seconds):

1. Not configured (``CIS_API_URL`` or ``M2M_CLIENT`` empty) -> ``not_configured``. After failures the run waits
   :func:`app.cis.membership_feed.backoff_seconds` from the last error (``backoff``).
2. A token for scope ``cis.tenant/read`` (the CRM client, the same as the membership feed), then
   ``GET <CIS_API_URL>/tenant/v1/account-events?after=<cursor>&limit=200``, at most :data:`MAX_PAGES` pages. The feed
   only says *which* tenants changed: for each tenant a page names, once per run, the agency is projected from
   ``GET /tenant/v1/tenants/{id}/account`` (the snapshot; the contract: "a consumer that needs the full state re-reads
   the snapshot"). Then the cursor moves (compare and set, :mod:`app.cis.feed_state`, table
   ``crm.entitlement_feed_state``).
3. Once an hour (``last_reconcile_at``), every agency the CRM holds is projected from its snapshot too, so an agency
   created in the CRM after its tenant's last CIS event (and any drift) is projected without waiting for one. A
   failing agency is counted (``reconcile_failed_count``) and the pass goes on; the reconcile keeps its own failure
   count (``reconcile_failures``) and retries sooner (5, 10, 20, 40 min) until a clean pass.
4. A 401 / 403 / 404 / 5xx on the feed, a transport error or a malformed page records the error class and ends the run
   **successfully** (the next slot retries after the backoff); a tenant whose snapshot fails stops the run before the
   cursor moves (it is retried, never skipped). A tenant CIS answers 404 for is ignored (``unknown_tenant``).

The projection (:func:`projection`, one :func:`app.tenancy.entitlement_feed_tx` per agency, actor
``platform:feed-tenant-account``; an unknown or offboarding agency is left alone):

- ``plan``: the CIS ``crm`` subscription's plan (``crm.core`` -> core, ``crm.pro`` -> pro; another live plan ->
  core) while it is live (trialing, active, past_due), else ``free`` (display only: the CRM checks features, never a
  plan name, ADR 0019 invariant 6);
- ``allowed_features``: the CRM codes whose ``crm.<code>`` key is entitled (a disabled or missing key is not allowed;
  the owner still switches an allowed feature on in Settings);
- ``member_limit``: the ``crm.seats`` limit (1-500), kept as it is when CIS gives none;
- ``tp_crm_leads`` (crm-047, AA-1038): both ``tp.hosted`` and ``crm.core`` are entitled, the two keys CIS 390 resolves
  a TripPlanner tenant's ``lead_channel`` crm from; ``crm_tp_lead_pull`` calls TripPlanner only for these agencies;
- ``projected_at`` = now; an unchanged projection writes nothing;
- adoption guard: an agency never projected before (``projected_at`` NULL: what the L1 console wrote) whose CIS account
  has no ``crm`` subscription in any status keeps its plan, seats and features (outcome ``awaiting_cis``, logged): CIS
  created no crm subscription for the agencies that existed before slice 2 (CIS 355), so the first run must not wipe
  them. Once CIS holds a crm row (any status) the projection follows it, a cancel or an offboard included;
- status: ``in_service`` false (suspended, offboarding, closed, lead, applied) locks an active agency with
  ``lock_source = cis_tenant``; ``in_service`` true lifts a lock whose source is ``cis_tenant`` (an AA lock or a
  last-owner lock stays), as the membership feed does for tenant.locked / tenant.unlocked;
- the pilot marker (AA-969, root ADR 0019 d16, crm-044): ``crm.tenant.is_pilot`` follows the snapshot's ``is_pilot``
  (absent = no write, the agency keeps its current marker: a CIS before contract 1.0's field, or a partial snapshot,
  must never clear the pilot's protections), also under the adoption guard (it is the tenant's, not the
  plan's). Only this actor may write it (``crm.guard_pilot_marker``, CRM04). The clean-up paths skip the marked agency
  and ``crm_pilot_backup`` exports it every night (``app.lifecycle.pilot``);
- the real marker (AA-1086 G9): an agency that is or was the pilot (the snapshot or the stored marker says so) gets
  ``crm.tenant.is_synthetic`` false, one way only: Dev creates every agency synthetic, the pilot included, and the
  E2E identities and the console's test-agency delete read that flag, so clearing the pilot marker at the end of the
  pilot must not make the former pilot's real data a test agency again (audit ``tenant_real_marker``).

Each applied change writes the agency's audit (``entitlements_projected`` / ``tenant_lock`` / ``tenant_unlock`` /
``tenant_pilot_marker``, reason ``cis_account``; ``tenant_real_marker``, reason ``cis_pilot``). A database refusal of
one agency (CRMxx, an integrity error) is counted as rejected and the run goes on; any other database error stops the
run before the cursor moves. Payloads and results are counts only.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app.audit_export import registry
from app.cis import contract, feed_state
from app.cis.http import HttpError
from app.cis.m2m import TokenError
from app.cis.membership_feed import REFUSAL_CLASSES, FeedConfig, backoff_seconds, http_error_class
from app.entitlements import FEATURES
from app.jobs import queue
from app.jobs.kinds import ENTITLEMENT_PULL
from app.jobs.queue import Job
from app.tenancy import SystemContext, entitlement_feed_tx

log = logging.getLogger(__name__)

SCOPE = "cis.tenant/read"
EVENTS_PATH = "/tenant/v1/account-events"
ACCOUNT_PATH = "/tenant/v1/tenants/{tenant_id}/account"
PAGE_LIMIT = 200
MAX_PAGES = 5
RECONCILE_EVERY = dt.timedelta(hours=1)
RECONCILE_RETRY = dt.timedelta(minutes=5)  # after a failing pass: 5, 10, 20, 40 min, then hourly again
RECONCILE_STUCK = 5  # consecutive failing passes (about 1 h 15 min) before crm_entitlement_feed_stuck
SLOT_SECONDS = 120
MAX_ATTEMPTS = 3
HISTORY_DAYS = 1
SCHEDULER = "system:entitlement_pull"
STUCK_FAILURES = 10
LIVE = frozenset({"trialing", "active", "past_due"})
PLANS = {"crm.core": "core", "crm.pro": "pro"}
SEATS_KEY = "crm.seats"
# AA-1038 (crm-047): TripPlanner sends a tenant's leads to its CRM when both keys are entitled (CIS 390
# lead_channel crm).
TP_CRM_LEAD_KEYS = ("tp.hosted", "crm.core")
MEMBER_LIMIT_MAX = 500
HELD = frozenset({"active", "locked"})

ENTITLEMENT_SQL = (
    "SELECT plan, member_limit, allowed_features, projected_at, tp_crm_leads FROM crm.tenant_entitlement"
    " WHERE deleted_at IS NULL"
)
UPSERT_SQL = """
INSERT INTO crm.tenant_entitlement (plan, member_limit, allowed_features, projected_at, tp_crm_leads)
VALUES ($1, $2, $3::text[], now(), $4)
ON CONFLICT (tenant_id) DO UPDATE
   SET plan = EXCLUDED.plan, member_limit = EXCLUDED.member_limit, allowed_features = EXCLUDED.allowed_features,
       projected_at = EXCLUDED.projected_at, tp_crm_leads = EXCLUDED.tp_crm_leads
"""
TENANT_LOCK_SQL = "SELECT status, lock_source, is_pilot, is_synthetic FROM crm.tenant WHERE tenant_id = $1 FOR UPDATE"
SET_PILOT_SQL = "UPDATE crm.tenant SET is_pilot = $2 WHERE tenant_id = $1"
# AA-1086 G9: one way only, never back to true (the seeder and the console refuse to flip it, app.agencies).
SET_REAL_SQL = "UPDATE crm.tenant SET is_synthetic = false WHERE tenant_id = $1 AND is_synthetic"
SET_LOCK_SQL = "UPDATE crm.tenant SET status = 'locked', lock_source = 'cis_tenant' WHERE tenant_id = $1"
UNLOCK_SQL = "UPDATE crm.tenant SET status = 'active' WHERE tenant_id = $1"
DEFAULT_MEMBER_LIMIT = 10


class AccountError(ValueError):
    """A snapshot that does not match ``TenantAccount``: the agency is not projected (the run stops)."""


@dataclass(frozen=True)
class Projection:
    plan: str
    member_limit: int | None
    features: tuple[str, ...]
    in_service: bool
    crm_subscribed: bool = True  # the CIS account has a crm subscription in any status (the adoption guard)
    is_pilot: bool | None = None  # AA-969 d16: the CIS pilot marker (crm-044); None = absent, keep the current one
    tp_crm_leads: bool = False  # AA-1038 (crm-047): tp.hosted and crm.core entitled, TripPlanner leads come here


@dataclass
class RunCounts:
    applied: int = 0
    unchanged: int = 0
    awaiting_cis: int = 0
    ignored: int = 0
    rejected: int = 0
    tenants: int = 0
    pages: int = 0
    locked: int = 0
    unlocked: int = 0
    pilot_changed: int = 0
    reconciled: bool = False
    reconcile_failed: int = 0

    def result(self, status: str, reason_code: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": status,
            "applied_count": self.applied,
            "unchanged_count": self.unchanged,
            "awaiting_cis_count": self.awaiting_cis,
            "ignored_count": self.ignored,
            "rejected_count": self.rejected,
            "tenant_count": self.tenants,
            "page_count": self.pages,
            "locked_count": self.locked,
            "unlocked_count": self.unlocked,
            "pilot_changed_count": self.pilot_changed,
            "reconciled_count": int(self.reconciled),
            "reconcile_failed_count": self.reconcile_failed,
        }
        if reason_code is not None:
            out["reason_code"] = reason_code
        return out


class _FeedError(Exception):
    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


# ── the contract (the fields this consumer reads; unknown fields are ignored: a minor adds them) ────────────────────


def _limit(value: Any) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AccountError("limit_not_decimal")
    try:
        out = Decimal(value)
    except InvalidOperation:
        raise AccountError("limit_not_decimal") from None
    if not out.is_finite() or out < 0:
        raise AccountError("limit_not_decimal")
    return out


def parse_account(body: Any, tenant_id: uuid.UUID) -> dict[str, Any]:
    """The snapshot's ``in_service``, ``is_pilot``, ``subscriptions`` and ``entitlements`` (feature key -> limit),
    checked."""
    if not isinstance(body, dict):
        raise AccountError("account_shape")
    if str(body.get("tenant_id", "")).lower() != str(tenant_id):
        raise AccountError("account_tenant")
    if not isinstance(body.get("in_service"), bool):
        raise AccountError("account_in_service")
    is_pilot = body.get("is_pilot")  # absent: None (the feed keeps the agency's current marker)
    if is_pilot is not None and not isinstance(is_pilot, bool):
        raise AccountError("account_is_pilot")
    subs, ents = body.get("subscriptions"), body.get("entitlements")
    if not isinstance(subs, list) or not isinstance(ents, list):
        raise AccountError("account_lists")
    subscriptions: dict[str, dict[str, Any]] = {}
    for s in subs:
        if not isinstance(s, dict) or not isinstance(s.get("product"), str) or not isinstance(s.get("status"), str):
            raise AccountError("subscription_shape")
        if not isinstance(s.get("plan_key"), str):
            raise AccountError("subscription_shape")
        subscriptions[s["product"]] = s
    entitlements: dict[str, Decimal | None] = {}
    for e in ents:
        if not isinstance(e, dict) or not isinstance(e.get("feature_key"), str):
            raise AccountError("entitlement_shape")
        entitlements[e["feature_key"]] = _limit(e.get("limit"))
    return {
        "in_service": body["in_service"],
        "is_pilot": is_pilot,
        "subscriptions": subscriptions,
        "entitlements": entitlements,
    }


def projection(account: dict[str, Any]) -> Projection:
    """What the CRM holds for an agency with this CIS account (pure; the module docstring has the rules)."""
    crm = account["subscriptions"].get("crm")
    if crm is not None and crm["status"] in LIVE:
        plan = PLANS.get(crm["plan_key"], "core")
    else:
        plan = "free"
    ents = account["entitlements"]
    features = tuple(code for code in FEATURES if f"crm.{code}" in ents)
    seats = ents.get(SEATS_KEY)
    member_limit = None if seats is None else max(1, min(int(seats), MEMBER_LIMIT_MAX))
    return Projection(
        plan,
        member_limit,
        features,
        bool(account["in_service"]),
        crm is not None,
        account.get("is_pilot"),
        all(key in ents for key in TP_CRM_LEAD_KEYS),
    )


def status_change(in_service: bool, status: str, lock_source: str | None) -> str | None:
    """``lock`` / ``unlock`` / None for an agency in ``status`` (active or locked)."""
    if not in_service and status == "active":
        return "lock"
    if in_service and status == "locked" and lock_source == "cis_tenant":
        return "unlock"
    return None


def events_url(base_url: str, cursor: str | None) -> str:
    query: dict[str, str] = {"limit": str(PAGE_LIMIT)}
    if cursor:
        query["after"] = cursor
    return f"{base_url}{EVENTS_PATH}?{urllib.parse.urlencode(query)}"


def account_url(base_url: str, tenant_id: uuid.UUID) -> str:
    return f"{base_url}{ACCOUNT_PATH.format(tenant_id=tenant_id)}"


def page_tenants(items: list[Any]) -> list[uuid.UUID]:
    """The tenants a page names, in first-seen order, each once (an item without a tenant id is skipped)."""
    seen: dict[uuid.UUID, None] = {}
    for item in items:
        raw = item.get("tenant_id") if isinstance(item, dict) else None
        try:
            seen.setdefault(uuid.UUID(str(raw)), None)
        except ValueError:
            log.warning("crm_entitlement_event_unrecorded reason=no_tenant_id")
    return list(seen)


# ── apply ───────────────────────────────────────────────────────────────────────────────────────────────────────────


async def held(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> bool:
    """True when the CRM holds an active or locked agency for this tenant (a read-only look, no audit event)."""
    async with entitlement_feed_tx(pool, tenant_id) as ctx:
        return ctx.status in HELD


async def apply(pool: asyncpg.Pool, tenant_id: uuid.UUID, wanted: Projection, counts: RunCounts) -> str:
    """One agency's projection in one transaction; returns the outcome (applied / unchanged / awaiting_cis / ignored /
    rejected)."""
    try:
        async with entitlement_feed_tx(pool, tenant_id) as ctx:
            if ctx.status not in HELD:
                outcome = "ignored"  # no agency, or offboarding / offboarded: nothing to project
            else:
                outcome = await _apply_in(ctx, wanted, counts)
    except asyncpg.PostgresError as exc:
        if (exc.sqlstate or "")[:2] not in REFUSAL_CLASSES:
            raise
        log.error("crm_entitlement_projection_refused tenant_id=%s sqlstate=%s", tenant_id, exc.sqlstate)
        outcome = "rejected"
    setattr(counts, outcome, getattr(counts, outcome) + 1)
    return outcome


async def _apply_in(ctx: SystemContext, wanted: Projection, counts: RunCounts) -> str:
    row = await ctx.conn.fetchrow(ENTITLEMENT_SQL)
    member_limit = wanted.member_limit
    if member_limit is None:
        member_limit = int(row["member_limit"]) if row is not None else DEFAULT_MEMBER_LIMIT
    features = sorted(wanted.features)
    awaiting = row is not None and row["projected_at"] is None and not wanted.crm_subscribed
    if awaiting:
        log.info("crm_entitlement_awaiting_cis tenant_id=%s", ctx.tenant_id)  # kept as the L1 console left it
    changed = not awaiting and (
        row is None
        or row["projected_at"] is None
        or row["plan"] != wanted.plan
        or int(row["member_limit"]) != member_limit
        or sorted(row["allowed_features"] or []) != features
        or bool(row["tp_crm_leads"]) != wanted.tp_crm_leads
    )
    if changed:
        await ctx.conn.execute(UPSERT_SQL, wanted.plan, member_limit, features, wanted.tp_crm_leads)
        ctx.audit(
            "entitlements_projected", "allowed", "tenant_entitlement", str(ctx.tenant_id),
            fields=("plan", "member_limit", "allowed_features", "tp_crm_leads"), reason="cis_account",
        )  # fmt: skip
    tenant = await ctx.conn.fetchrow(TENANT_LOCK_SQL, ctx.tenant_id)
    change = status_change(wanted.in_service, tenant["status"], tenant["lock_source"])
    if change == "lock":
        await ctx.conn.execute(SET_LOCK_SQL, ctx.tenant_id)
        ctx.audit("tenant_lock", "allowed", "tenant", str(ctx.tenant_id), fields=("status",), reason="cis_account")
        counts.locked += 1
    elif change == "unlock":
        await ctx.conn.execute(UNLOCK_SQL, ctx.tenant_id)
        ctx.audit("tenant_unlock", "allowed", "tenant", str(ctx.tenant_id), fields=("status",), reason="cis_account")
        counts.unlocked += 1
    pilot = wanted.is_pilot is not None and bool(tenant["is_pilot"]) != wanted.is_pilot
    if pilot:
        await ctx.conn.execute(SET_PILOT_SQL, ctx.tenant_id, wanted.is_pilot)
        ctx.audit(
            "tenant_pilot_marker", "allowed", "tenant", str(ctx.tenant_id), fields=("is_pilot",), reason="cis_account"
        )
        counts.pilot_changed += 1
    # AA-1086 G9: an agency CIS ever marked the pilot holds a real agency's data, so it stops being synthetic for good
    # (CIS agrees: the pilot's is_canary is false and stays false once the pilot ends). Ending the pilot clears is_pilot
    # only; the E2E identities (A10b-1, A10c) and the console's test-agency delete read is_synthetic and stay out.
    real = bool(tenant["is_synthetic"]) and (bool(tenant["is_pilot"]) or wanted.is_pilot is True)
    if real:
        await ctx.conn.execute(SET_REAL_SQL, ctx.tenant_id)
        ctx.audit(
            "tenant_real_marker", "allowed", "tenant", str(ctx.tenant_id), fields=("is_synthetic",), reason="cis_pilot"
        )
    if changed or change or pilot or real:
        return "applied"
    return "awaiting_cis" if awaiting else "unchanged"


# ── the job ─────────────────────────────────────────────────────────────────────────────────────────────────────────


class EntitlementFeed:
    def __init__(
        self, config: FeedConfig | None, *, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)
    ) -> None:
        self._config = config
        self._now = now
        self._warned = False

    async def __call__(self, pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        counts = RunCounts()
        if self._config is None:
            if not self._warned:
                log.warning("crm_entitlement_pull not_configured (CIS_API_URL / M2M_CLIENT)")
                self._warned = True
            return counts.result("not_configured")
        table = feed_state.ENTITLEMENT
        state = await feed_state.load(pool, table)
        if state.failures and state.last_error_at is not None:
            if self._now() < state.last_error_at + dt.timedelta(seconds=backoff_seconds(state.failures)):
                return counts.result("backoff")
        try:
            await self._pull(pool, state.cursor, counts)
            await self._reconcile(pool, counts)
        except _FeedError as exc:
            failures = await feed_state.fail(pool, exc.error_class, table)
            log.warning("crm_entitlement_pull_failed error=%s failures=%d", exc.error_class, failures)
            if failures >= STUCK_FAILURES:
                log.error("crm_entitlement_feed_stuck error=%s failures=%d", exc.error_class, failures)
            return counts.result("error", exc.error_class)
        return counts.result("ok")

    async def _pull(self, pool: asyncpg.Pool, cursor: str | None, counts: RunCounts) -> None:
        done: set[uuid.UUID] = set()
        for _ in range(MAX_PAGES):
            items, next_cursor = await self._page(cursor)
            counts.pages += 1
            for tenant_id in page_tenants(items):
                if tenant_id not in done:
                    await self._project(pool, tenant_id, counts)
                    done.add(tenant_id)
            if next_cursor and next_cursor != cursor:
                if not await feed_state.advance(pool, cursor, next_cursor, feed_state.ENTITLEMENT):
                    log.info("crm_entitlement_pull cursor moved by another run; stopping")
                    return
                cursor = next_cursor
            else:
                await feed_state.succeed(pool, feed_state.ENTITLEMENT)
            if len(items) < PAGE_LIMIT:
                return

    async def _reconcile(self, pool: asyncpg.Pool, counts: RunCounts) -> None:
        """Every agency from its snapshot, once per :data:`RECONCILE_EVERY`. Review fix: a failing agency (a CIS 5xx or
        429, a malformed snapshot, a database error) no longer stops the pass, so the agencies after it are still
        projected; it is counted and retried by the next pass, which comes sooner (:func:`reconcile_wait`). The
        reconcile keeps its own consecutive failures (``reconcile_failures``, crm-043), which the event pull's success
        never resets; :data:`RECONCILE_STUCK` failing passes in a row log ``crm_entitlement_feed_stuck``."""
        state = await feed_state.reconcile_state(pool)
        if state.last_at is not None and self._now() < state.last_at + reconcile_wait(state.failures):
            return
        error_class: str | None = None
        for agency in await registry.agencies(pool):
            try:
                await self._project(pool, agency.tenant_id, counts)
            except _FeedError as exc:
                counts.reconcile_failed += 1
                error_class = error_class or exc.error_class
        counts.reconciled = True
        if error_class is None:
            await feed_state.reconciled(pool)
            return
        failures = await feed_state.reconcile_failed(pool)
        log.warning(
            "crm_entitlement_reconcile_failed error=%s agencies=%d failures=%d",
            error_class, counts.reconcile_failed, failures,
        )  # fmt: skip
        if failures >= RECONCILE_STUCK:
            log.error("crm_entitlement_feed_stuck part=reconcile error=%s failures=%d", error_class, failures)

    async def _project(self, pool: asyncpg.Pool, tenant_id: uuid.UUID, counts: RunCounts) -> None:
        counts.tenants += 1
        if not await held(pool, tenant_id):
            counts.ignored += 1  # most CIS tenants have no CRM agency: no snapshot call for them
            return
        account = await self._account(tenant_id)
        if account is None:
            counts.ignored += 1  # CIS has no such tenant (404)
            return
        try:
            await apply(pool, tenant_id, projection(account), counts)
        except asyncpg.PostgresError as exc:
            log.error("crm_entitlement_projection_failed sqlstate=%s", exc.sqlstate)
            raise _FeedError("apply_error") from None
        except Exception as exc:  # noqa: BLE001 — a lost connection or a bug: the backoff, never a crash
            log.error("crm_entitlement_projection_failed error=%s", type(exc).__name__)
            raise _FeedError("apply_error") from None

    async def _get(self, url: str) -> Any:
        config = self._config
        assert config is not None  # noqa: S101 — checked by the caller
        try:
            token = await config.tokens.token(SCOPE)
        except TokenError as exc:
            raise _FeedError(exc.error_class) from None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            resp = await config.transport("GET", url, headers, None, config.timeout_s)
        except HttpError as exc:
            raise _FeedError(f"cis_{exc.error_class}") from None
        if resp.status == 401:
            config.tokens.invalidate(SCOPE)
        return resp

    async def _page(self, cursor: str | None) -> tuple[list[Any], str]:
        config = self._config
        assert config is not None  # noqa: S101
        resp = await self._get(events_url(config.base_url, cursor))
        if resp.status != 200:
            raise _FeedError(http_error_class(resp.status))
        try:
            return contract.parse_page(json.loads(resp.body))
        except (ValueError, contract.PageError):
            raise _FeedError("cis_contract") from None

    async def _account(self, tenant_id: uuid.UUID) -> dict[str, Any] | None:
        config = self._config
        assert config is not None  # noqa: S101
        resp = await self._get(account_url(config.base_url, tenant_id))
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise _FeedError(http_error_class(resp.status))
        try:
            return parse_account(json.loads(resp.body), tenant_id)
        except (ValueError, AccountError):
            raise _FeedError("cis_contract") from None


def reconcile_wait(failures: int) -> dt.timedelta:
    """Time from the last reconcile pass to the next: :data:`RECONCILE_EVERY` after a clean pass; after ``failures``
    failing passes in a row :data:`RECONCILE_RETRY` doubled each time, capped at :data:`RECONCILE_EVERY`."""
    if failures <= 0:
        return RECONCILE_EVERY
    return min(RECONCILE_RETRY * 2 ** min(failures - 1, 10), RECONCILE_EVERY)


# ── the schedule ────────────────────────────────────────────────────────────────────────────────────────────────────


def schedule_key(now: dt.datetime) -> str:
    slot = int(now.timestamp()) // SLOT_SECONDS
    return f"{ENTITLEMENT_PULL}:slot:{slot}"


class EntitlementPullSchedule:
    """Worker maintenance hook: one ``crm_entitlement_pull`` run per :data:`SLOT_SECONDS` slot, due now, never ahead
    (the due-now hook pattern of ``HourlyCatalogSync``). Idempotent across workers (key per slot)."""

    def __init__(self, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        self._now = now
        self._done_slot: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        key = schedule_key(self._now())
        if self._done_slot == key:
            return None
        job_id = await queue.enqueue(
            pool,
            ENTITLEMENT_PULL,
            {"purpose": "slot_schedule"},
            idempotency_key=key,
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, ENTITLEMENT_PULL, HISTORY_DAYS)
        self._done_slot = key
        return job_id
