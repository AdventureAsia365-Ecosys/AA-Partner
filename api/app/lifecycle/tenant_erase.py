"""``crm_tenant_erase`` (AA-1093 stage 2, root plan docs/audits/2026-11-13-S279-session/aa1093-plan.md, decision D2):
the CRM step of the CIS tenant offboarding erasure saga, contract ``tenant.erasure.v1`` (byte copy in
``docs/contracts/``).

D2: after its exit window (T0 + 90 days, computed by CIS) AA erases an offboarded agency's CRM data without the
owner's click. CIS runs the saga (stage 1: CIS, Booking, TripPlanner; stage 2: the CRM alone), the tech lead approves a
combined fingerprint once per round, and each app applies its own approved plan. The CRM holds no grant on another
schema (root ADR 0008a), so it reads the requests and reports over HTTP with the M2M scope ``cis.tenant/erasure``.

One run (a due-now maintenance hook every :data:`SLOT_SECONDS`, :class:`TenantEraseSchedule`; not deploy-gate or DB
Migrate exempt: a row exists only while it is due or running):

1. **Pull.** ``GET <CIS_API_URL>/tenant/v1/erasure-requests?after=<cursor>`` (at most :data:`MAX_PAGES` pages): each
   item is checked against the contract (:func:`app.lifecycle.saga_rules.parse_request`; a malformed item is counted,
   never stored) and stored in ``crm.tenant_erasure_saga`` (the newest state wins); then the cursor moves (compare and
   set, ``crm.tenant_erasure_feed_state``). A 401 / 403 / 5xx, a transport error or a malformed page records the error
   class and the run goes on with the requests it already holds (backoff from the last error, as the CIS feeds).
2. **Work**, per stored request with an active crm step (CIS decides the round) or a report not posted yet: first the
   reports not posted yet are posted again (same report id and body), then the report the step owes now
   (:func:`app.lifecycle.saga_rules.next_mode`), each written to ``crm.tenant_erasure_report`` in the transaction that
   did the work, then posted to ``POST /tenant/v1/erasure-requests/{id}/reports``:

   - **dry_run**: the plan (:func:`plan`): every count of ``crm.erase_subject('tenant')`` in DRY RUN, the keys the
     crypto-shred would destroy, the agency row whose name is replaced, the tenant's Content studio invitations
     (crm-067); its plan hash. ``blocked`` (no hash) under an active legal hold (``legal_hold``) or while the owner's
     own offboarding erase runs (``owner_erasure_running``). Nothing is written but the report and an audit row.
   - **apply**: the guards first (:func:`app.lifecycle.saga_rules.guard`: the pilot, outside prod a synthetic tenant
     only, the kill switch ``TENANT_ERASE_APPLY_ENABLED`` (Terraform, default off), an approved plan hash for this
     round that has not expired); then the plan again, which must give the approved hash (else ``refused``
     ``plan_changed``, nothing erased). Then, in one transaction: the owner's open offboarding / export requests are
     cancelled, the agency moves to ``offboarding``, its portability exports are withdrawn, ``crm.erase_subject`` erases
     every tenant table outside the crm-010 keep list (as ``crm_eraser``; CRM16 if a table is left), the agency's
     keys are destroyed (crypto-shred) and a canary sealed before must no longer open, the agency row keeps no name
     (:func:`app.lifecycle.saga_rules.erased_name`) and becomes ``offboarded``, the tenant's studio invitations are
     deleted, the report and the audit row are written; the bucket objects (documents, exports) are deleted last, so a
     refusal deletes no file. A refusal of the database (a hold placed meanwhile, CRM16, a failed canary) rolls it all
     back and is reported ``refused`` / ``failed`` with its code.
   - **verify**, right after an apply CIS accepted: the census (:func:`app.lifecycle.saga_store.census`) reads, from a
     new transaction, every ``pii:`` column (DM-6.1) of every crm table with a tenant column, and the tenant's studio
     invitations, and counts the tenant's rows that still hold a value: ``ok`` at 0, else ``failed``
     ``census_not_zero`` (a table it cannot read: ``failed`` ``census_incomplete``).

   A tenant the CRM holds no agency for gets the same three reports over its studio invitations only.

CIS's answer is recorded on the report (``accepted``, or ``refused:<code>``: settled, not posted again); a 401 / 429 /
5xx or a network error leaves it for the next run. Payloads and results are counts only; reports carry ids, codes,
hashes and counts only.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.parse
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import asyncpg

from app.cis.http import HttpError
from app.cis.m2m import TokenError
from app.cis.membership_feed import FeedConfig, backoff_seconds, http_error_class
from app.documents.store import DocumentStore
from app.errors import Conflict, ServiceError, Unavailable
from app.jobs import queue
from app.jobs.kinds import TENANT_ERASE
from app.jobs.queue import Job
from app.lifecycle import erase, saga_store
from app.lifecycle import saga_rules as rules
from app.lifecycle.offboarding import CANARY, HOLD_SQL, _canary_unreadable
from app.platform_settings import flag
from app.portability import exports as portability_exports
from app.tenancy import LifecycleContext, lifecycle_tx
from app.vault import Vault

if TYPE_CHECKING:  # pragma: no cover
    from app.vault import KeyProvider

log = logging.getLogger(__name__)

ACTOR = saga_store.ACTOR
SCOPE = "cis.tenant/erasure"
REQUESTS_PATH = "/tenant/v1/erasure-requests"
PAGE_SIZE = 100
MAX_PAGES = 5  # technical-constant: pages per run of a feed of a handful of requests, as the CIS feeds
WORK_BATCH = 20  # requests worked per run (a handful exist at a time)
SLOT_SECONDS = 120
MAX_ATTEMPTS = 3  # technical-constant: job runner mechanics (every step is idempotent and re-read each run)
HISTORY_DAYS = 1  # technical-constant: finished slot rows kept for debugging, as crm_entitlement_pull
SCHEDULER = "system:tenant_erase"
STATUSES = frozenset({"active", "locked", "offboarding", "offboarded"})
SETTLED = frozenset({200, 201})
RETRY_LATER = frozenset({401, 408, 425, 429})
AGENCY_SQL = "SELECT status, is_pilot, is_synthetic, display_name FROM crm.tenant WHERE tenant_id = $1"
LIVE_KEYS_SQL = "SELECT count(*) FROM crm.tenant_key WHERE destroyed_at IS NULL"
OPEN_REQUESTS_SQL = """
SELECT request_id, export_object_key FROM crm.erasure_request
 WHERE scope = 'tenant' AND status IN ('requested', 'export_ready', 'failed')
 FOR UPDATE
"""
CANCEL_REQUEST_SQL = "UPDATE crm.erasure_request SET status = 'cancelled' WHERE request_id = $1"
OFFBOARDING_SQL = "UPDATE crm.tenant SET status = 'offboarding' WHERE tenant_id = $1 AND status IN ('active', 'locked')"
ERASED_SQL = "UPDATE crm.tenant SET display_name = $2, status = 'offboarded' WHERE tenant_id = $1"


class _FeedError(Exception):
    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


class _ObjectsNotDeleted(RuntimeError):
    """A bucket refused an object delete at the end of an apply: retried by the worker, not a refusal."""


# ── the plan and the census ──────────────────────────────────────────────────────────────────────────────────────
async def _agency(ctx: LifecycleContext) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(AGENCY_SQL, ctx.tenant_id)
    return dict(row)


async def plan(
    conn: asyncpg.Connection, tenant_id: uuid.UUID, ctx: LifecycleContext | None, agency: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """The plan's table entries (read only). ``ctx`` / ``agency`` None: the CRM holds no agency for the tenant."""
    counts = None
    live_keys = name = 0
    if ctx is not None and agency is not None:
        counts = await erase.erase(ctx, "tenant", tenant_id, dry_run=True)
        live_keys = int(await ctx.conn.fetchval(LIVE_KEYS_SQL) or 0)
        name = 0 if agency["display_name"] == rules.erased_name(tenant_id) else 1
    invitations = await saga_store.studio_invitations(conn, tenant_id, dry_run=True)
    return rules.plan_entries(counts, live_keys=live_keys, name_to_erase=name, invitations=invitations)


# ── the handler ──────────────────────────────────────────────────────────────────────────────────────────────────
class TenantErase:
    def __init__(
        self,
        config: FeedConfig | None,
        *,
        keys: KeyProvider | None,
        store: DocumentStore | None,
        apply_enabled: bool | Callable[[], bool],
        environment: str,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._config = config
        self._keys = keys
        self._store = store
        self._apply_enabled = apply_enabled
        self._environment = environment
        self._now = now
        self._warned = False

    async def __call__(self, pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        counts = {"stored": 0, "rejected": 0, "pages": 0, "reports": 0, "posted": 0, "post_failed": 0}
        if self._config is None:
            if not self._warned:
                log.warning("crm_tenant_erase not_configured (CIS_API_URL / M2M_CLIENT)")
                self._warned = True
            return {"status": "not_configured", **counts}
        status = await self._pull(pool, counts)
        for req in await saga_store.actionable(pool, WORK_BATCH):
            await self._work(pool, req, job, counts)
        return {"status": status, **_counted(counts)}

    # ── pull ─────────────────────────────────────────────────────────────────────────────────────────────────────
    async def _pull(self, pool: asyncpg.Pool, counts: dict[str, int]) -> str:
        state = await saga_store.load_state(pool)
        if state.failures and state.last_error_at is not None:
            if self._now() < state.last_error_at + dt.timedelta(seconds=backoff_seconds(state.failures)):
                return "backoff"
        cursor = state.cursor
        try:
            for _ in range(MAX_PAGES):
                items, next_cursor = await self._page(cursor)
                counts["pages"] += 1
                await self._keep(pool, items, counts)
                if next_cursor and next_cursor != cursor:
                    if not await saga_store.advance(pool, cursor, next_cursor):
                        return "ok"  # another run moved it first
                    cursor = next_cursor
                else:
                    await saga_store.succeed(pool)
                if len(items) < PAGE_SIZE:
                    break
        except _FeedError as exc:
            failures = await saga_store.fail(pool, exc.error_class)
            log.warning("crm_tenant_erase_pull_failed error=%s failures=%d", exc.error_class, failures)
            return "error"
        return "ok"

    async def _keep(self, pool: asyncpg.Pool, items: list[Any], counts: dict[str, int]) -> None:
        """Each item checked against the contract and stored (the newest state wins); a malformed one is counted."""
        for item in items:
            req = rules.parse_request(item)
            if isinstance(req, str):
                counts["rejected"] += 1
                log.warning("crm_tenant_erase_request_rejected reason=%s", req)
            elif await saga_store.upsert_request(pool, req):
                counts["stored"] += 1

    async def _page(self, cursor: str | None) -> tuple[list[Any], str]:
        config = self._config
        assert config is not None  # noqa: S101 — checked by the caller
        query = {"limit": str(PAGE_SIZE), **({"after": cursor} if cursor else {})}
        url = f"{config.base_url}{REQUESTS_PATH}?{urllib.parse.urlencode(query)}"
        resp = await self._call("GET", url, None)
        if resp.status != 200:
            raise _FeedError(http_error_class(resp.status))
        try:
            return rules.parse_page(json.loads(resp.body))
        except ValueError:
            raise _FeedError("cis_contract") from None

    async def _call(self, method: str, url: str, body: bytes | None) -> Any:
        config = self._config
        assert config is not None  # noqa: S101 — checked by the caller
        try:
            token = await config.tokens.token(SCOPE)
        except TokenError as exc:
            raise _FeedError(exc.error_class) from None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = await config.transport(method, url, headers, body, config.timeout_s)
        except HttpError as exc:
            raise _FeedError(f"cis_{exc.error_class}") from None
        if resp.status == 401:
            config.tokens.invalidate(SCOPE)  # revoked or rotated: the next call asks the pool again
        return resp

    # ── work ─────────────────────────────────────────────────────────────────────────────────────────────────────
    async def _work(self, pool: asyncpg.Pool, req: rules.SagaRequest, job: Job, counts: dict[str, int]) -> None:
        for report_id, body in await saga_store.unposted(pool, req.request_id):
            if not await self._post(pool, report_id, body, counts):
                return  # CIS not reachable: the next run goes on from here
        for _ in rules.MODES:
            mode = rules.next_mode(req, await saga_store.made(pool, req.request_id, req.round), self._now())
            if mode is None:
                return
            body = await self._report(pool, req, mode, job)
            counts["reports"] += 1
            log.info(
                "crm_tenant_erase_report request_id=%s round=%d mode=%s status=%s code=%s",
                req.request_id, req.round, mode, body["status"], body["error_code"],
            )  # fmt: skip
            if not await self._post(pool, uuid.UUID(body["report_id"]), body, counts):
                return

    async def _post(
        self, pool: asyncpg.Pool, report_id: uuid.UUID, body: dict[str, Any], counts: dict[str, int]
    ) -> bool:
        """Post one stored report. True when CIS settled it (accepted or refused with a code)."""
        config = self._config
        assert config is not None  # noqa: S101 — checked by the caller
        url = f"{config.base_url}{REQUESTS_PATH}/{body['request_id']}/reports"
        try:
            resp = await self._call("POST", url, json.dumps(body).encode("utf-8"))
        except _FeedError as exc:
            await saga_store.post_failed(pool, report_id)
            counts["post_failed"] += 1
            log.warning("crm_tenant_erase_post_failed error=%s", exc.error_class)
            return False
        if resp.status in SETTLED:
            await saga_store.posted(pool, report_id, "accepted")
            counts["posted"] += 1
            return True
        if resp.status in RETRY_LATER or resp.status >= 500:
            await saga_store.post_failed(pool, report_id)
            counts["post_failed"] += 1
            log.warning("crm_tenant_erase_post_failed error=%s", http_error_class(resp.status))
            return False
        code = _problem_code(resp.body, resp.status)
        await saga_store.posted(pool, report_id, f"refused:{code}")
        log.error("crm_tenant_erase_report_refused request_id=%s code=%s", body["request_id"], code)
        return True

    async def _report(self, pool: asyncpg.Pool, req: rules.SagaRequest, mode: str, job: Job) -> dict[str, Any]:
        """Compute, record (in the work's transaction) and return the report of ``mode``."""
        report_id, now = uuid.uuid4(), self._now()

        def body(outcome: str, **kw: Any) -> dict[str, Any]:
            return rules.report_body(
                req, report_id=report_id, mode=mode, outcome=outcome, job_id=job.id, reported_at=now, **kw
            )

        if mode == "apply" and self._keys is None:
            raise Unavailable("key_provider_not_configured")  # retried: nothing is erased without the crypto-shred
        try:
            if await saga_store.agency_status(pool, req.tenant_id) is None:
                out = await saga_store.without_agency(
                    pool, req.tenant_id, lambda conn: self._step(conn, None, req, mode, body)
                )
            else:
                async with lifecycle_tx(pool, req.tenant_id, actor=ACTOR, statuses=STATUSES) as ctx:
                    out = await self._step(ctx.conn, ctx, req, mode, body)
            objects = out.pop("_objects", [])
        except ServiceError as exc:
            # The work rolled back (a hold placed meanwhile, CRM16, a failed canary, an unreadable census table):
            # nothing was erased; the report says why, in a transaction of its own.
            outcome = "refused" if isinstance(exc, Conflict) else "failed"
            out = body(outcome, error_code=_code(exc.code))
            await saga_store.without_agency(pool, req.tenant_id, lambda conn: saga_store.insert_report(conn, out))
            return out
        try:
            await erase.delete_objects(self._store, objects)  # last: after the commit of rows, report and audit
        except ServiceError as exc:
            raise _ObjectsNotDeleted(exc.code) from exc
        return out

    async def _step(
        self,
        conn: asyncpg.Connection,
        ctx: LifecycleContext | None,
        req: rules.SagaRequest,
        mode: str,
        body: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        agency = await _agency(ctx) if ctx is not None else None
        code = rules.guard(
            req, mode, environment=self._environment, agency=agency, apply_enabled=flag(self._apply_enabled),
            now=self._now(),
        )  # fmt: skip
        if code is not None:
            out = body("refused", error_code=code)
        elif mode == "verify":
            found, left = await saga_store.census(conn, req.tenant_id)
            failed = found["pii_rows_found"] > 0
            out = body("failed" if failed else "ok", census=found, tables=left,
                       error_code="census_not_zero" if failed else None)  # fmt: skip
        else:
            out = await self._dry_or_apply(conn, ctx, agency, req, mode, body)
        objects = out.pop("_objects", [])
        await saga_store.insert_report(conn, out)
        if ctx is not None:
            ctx.audit(
                f"tenant_erasure_{mode}",
                "allowed" if out["status"] == "ok" else "denied",
                "tenant",
                str(ctx.tenant_id),
                reason=out["error_code"] or out["status"],
                object_ids=(f"cis_erasure:{req.request_id}",),
            )
        return {**out, "_objects": objects}

    async def _dry_or_apply(
        self,
        conn: asyncpg.Connection,
        ctx: LifecycleContext | None,
        agency: dict[str, Any] | None,
        req: rules.SagaRequest,
        mode: str,
        body: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        blockers = []
        if agency is not None and agency["status"] == "offboarding":
            blockers.append({"code": "owner_erasure_running", "count": 1})  # the owner's own erase (AA-942) runs
        if ctx is not None and await ctx.conn.fetchval(HOLD_SQL):
            blockers.append({"code": "legal_hold", "count": 1})
        if blockers:
            outcome = "blocked" if mode == "dry_run" else "refused"
            return body(outcome, error_code=blockers[0]["code"], blockers=blockers)
        tables = await plan(conn, req.tenant_id, ctx, agency)
        planned = rules.plan_hash(req.tenant_id, req.round, tables)
        if mode == "dry_run":
            return body("ok", plan=planned, tables=tables)
        if planned != req.approved_plan_hash:
            return body("refused", error_code="plan_changed", plan=planned, tables=tables)
        applied, objects = await self._apply(conn, ctx, agency, req)
        return {**body("ok", plan=planned, tables=applied), "_objects": objects}

    async def _apply(
        self,
        conn: asyncpg.Connection,
        ctx: LifecycleContext | None,
        agency: dict[str, Any] | None,
        req: rules.SagaRequest,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """The erase itself, in the caller's transaction (the bucket objects are returned, deleted after the commit)."""
        objects: list[str] = []
        counts = None
        destroyed = name = 0
        if ctx is not None and agency is not None and agency["status"] != "offboarded":
            for row in await ctx.conn.fetch(OPEN_REQUESTS_SQL):
                await ctx.conn.execute(CANCEL_REQUEST_SQL, row["request_id"])
                if row["export_object_key"]:
                    objects.append(row["export_object_key"])
            await ctx.conn.execute(OFFBOARDING_SQL, ctx.tenant_id)
            await portability_exports.withdraw(ctx, self._store, None, dry_run=False, pending=objects)
            counts = await erase.erase(ctx, "tenant", ctx.tenant_id, dry_run=False, store=self._store, pending=objects)
            assert self._keys is not None  # noqa: S101 — checked before the transaction
            vault = Vault(self._keys, ctx)  # type: ignore[arg-type]
            canary = await vault.encrypt("erasure_request", "verify_canary", req.request_id, CANARY)
            destroyed = await self._keys.destroy(ctx)  # type: ignore[arg-type]  # crypto-shred, before the commit
            if not await _canary_unreadable(vault, req.request_id, canary):
                raise Unavailable("verify_failed", "a value sealed before the key destroy still opens")
            name = 0 if agency["display_name"] == rules.erased_name(ctx.tenant_id) else 1
            await ctx.conn.execute(ERASED_SQL, ctx.tenant_id, rules.erased_name(ctx.tenant_id))
        elif ctx is not None:
            counts = {}  # erased before (the owner's own offboarding): nothing of the agency is left to erase
        invitations = await saga_store.studio_invitations(conn, req.tenant_id, dry_run=False)
        tables = rules.plan_entries(counts, live_keys=destroyed, name_to_erase=name, invitations=invitations)
        return tables, objects


def _problem_code(raw: bytes, status: int) -> str:
    try:
        code = json.loads(raw).get("error")
    except (ValueError, AttributeError):
        code = None
    return code if isinstance(code, str) and rules.CODE_RE.match(code) else f"http_{status}"


def _code(value: str) -> str:
    return value if rules.CODE_RE.match(value or "") else "internal_error"


def _counted(counts: dict[str, int]) -> dict[str, int]:
    return {f"{k}_count": v for k, v in counts.items()}


# ── the schedule ─────────────────────────────────────────────────────────────────────────────────────────────────
def schedule_key(now: dt.datetime) -> str:
    return f"{TENANT_ERASE}:slot:{int(now.timestamp()) // SLOT_SECONDS}"


class TenantEraseSchedule:
    """Worker maintenance hook: one ``crm_tenant_erase`` run per :data:`SLOT_SECONDS` slot, due now, never ahead (the
    due-now hook pattern of ``EntitlementPullSchedule``). Idempotent across workers (key per slot)."""

    def __init__(self, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        self._now = now
        self._done_slot: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        key = schedule_key(self._now())
        if self._done_slot == key:
            return None
        job_id = await queue.enqueue(
            pool,
            TENANT_ERASE,
            {"purpose": "slot_schedule"},
            idempotency_key=key,
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, TENANT_ERASE, HISTORY_DAYS)
        self._done_slot = key
        return job_id
