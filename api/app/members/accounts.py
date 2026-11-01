"""The partner-account clean-up (S263, AA-939 follow-up; root ADR 0006b amendment A1, ``crm_partner_accounts``).

An invitation claim creates the invited person's partner-pool account (:mod:`app.members.directory`). When the
invitation then ends without being accepted (AA withdraws it, the owner revokes it, it expires, or AA deletes the
synthetic test agency it belonged to), the account stays behind: an address that never signed in stays a
``FORCE_CHANGE_PASSWORD`` user for ever. This module deletes such an account (``AdminDeleteUser``) only when **all**
of these hold:

1. it is an identity of the partner pool (the directory's issuer) that a CRM invitation claim bound
   (``crm.member_invitation.cognito_sub``): the clean-up never lists the pool, so an account nothing here created is
   never a candidate;
2. no CRM member row in **any** agency names it (status ``active`` or ``invited``, not trashed);
3. no CRM invitation in any agency is live with it (``pending`` / ``claimed`` and not expired) or ``accepted``;
4. no Content studio invitation is live with it (``claimed`` and not expired, ``accepted``, ``activated``; crm-039 lets
   this job read ``crm.studio_invitation``, and the read refuses to run before crm-039 is applied);
5. every one of its invitations (CRM and studio) ended more than :data:`GRACE_DAYS` days ago, except a CRM invitation
   of a **trashed** agency (a deleted test agency), which counts as ended for good at once;
6. the account itself, read again from Cognito right before the delete, is still ``FORCE_CHANGE_PASSWORD`` and was
   not changed after the last of those invitations ended (a claim's ``RESEND`` changes it: a claim racing the
   clean-up keeps the account). An account with its own password (``CONFIRMED``) is never deleted by this code: the
   directory refuses it whatever the caller decided.

Cross-agency reads go through the agency's own context, :func:`app.tenancy.partner_account_tx` (``app.tenant_id`` +
``tenant_isolation``, trashed agencies included), one transaction per agency of the audit registry; the studio rows
through :func:`app.members.studio.account_links`. Nothing turns row security off.

Each deleted account is recorded in the audit of every agency whose invitation it was: ``partner_account_delete``
(object = the invitation, ``object_ids`` = all of its invitations there, reason ``never_signed_in``): ids and a code,
never the address or the subject. Logs carry ids and counts only.

When it runs: the job ``crm_partner_accounts``, queued (a) by :func:`app.ops.console.delete_test_agency` in its own
transaction (purpose ``agency_delete``, the agency's id: the accounts of that agency's invitations, at once) and (b)
once per UTC day by :class:`DailyPartnerAccountSchedule` (purpose ``daily_schedule``: every candidate). Both honour
``PARTNER_ACCOUNT_CLEANUP_DRY_RUN`` (default on: counts only, Cognito is only read). At most
:data:`MAX_DELETES_PER_RUN` deletes per run; the rest is counted ``capped`` and waits for the next run. By hand, inside
the API container: ``python -m app.members.accounts [--apply] [--limit N] [--tenant <uuid>] [--grace-days N]`` (dry
run unless ``--apply``; one JSON line of counts; ``--grace-days`` lets an operator clear a known test address before
its 7 days, every other rule still holds).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg

from app.audit_export import registry
from app.errors import NotFound
from app.jobs import queue
from app.jobs.kinds import PARTNER_ACCOUNTS
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.members import studio
from app.members.directory import DELETED, GONE, PartnerDirectory, delete_verdict
from app.tenancy import SystemContext, partner_account_tx

log = logging.getLogger(__name__)

GRACE_DAYS = 7
MAX_DELETES_PER_RUN = 50
MAX_ATTEMPTS = 3
HISTORY_DAYS = 30
SCHEDULER = "scheduler:crm-partner-accounts"
REASON = "never_signed_in"
AUDIT_ACTION = "partner_account_delete"
PURPOSES = frozenset({"daily_schedule", "agency_delete"})
MAX_OBJECT_IDS = 200  # crm.audit_event object_ids cap

TENANT_SQL = "SELECT deleted_at IS NOT NULL AS trashed FROM crm.tenant WHERE tenant_id = $1"
INVITATIONS_SQL = """
SELECT invitation_id, cognito_iss, cognito_sub, status, expires_at, updated_at, deleted_at
  FROM crm.member_invitation
 WHERE cognito_sub IS NOT NULL
"""
MEMBERS_SQL = """
SELECT cognito_iss, cognito_sub FROM crm.member WHERE status IN ('active', 'invited') AND deleted_at IS NULL
"""

Identity = tuple[str, str]  # (iss, sub)


@dataclass(frozen=True)
class Link:
    """One invitation bound to an identity. ``keep``: the reason it still needs the account (None = it ended);
    ``ended_at``: when it ended; ``grace_waived``: a CRM invitation of a trashed agency (ended for good)."""

    tenant_id: uuid.UUID | None  # None = a Content studio invitation (no agency)
    invitation_id: uuid.UUID
    keep: str | None
    ended_at: datetime | None
    grace_waived: bool = False


@dataclass
class Facts:
    links: dict[Identity, list[Link]] = field(default_factory=dict)
    members: Counter[Identity] = field(default_factory=Counter)

    def add(self, identity: Identity, link: Link) -> None:
        self.links.setdefault(identity, []).append(link)


def crm_link(row: Any, tenant_id: uuid.UUID, *, trashed: bool, now: datetime) -> Link:
    status, expires_at = row["status"], row["expires_at"]
    if status == "accepted":
        return Link(tenant_id, row["invitation_id"], "accepted_invitation", None)
    if row["deleted_at"] is not None:
        ended: datetime = row["deleted_at"]
    elif status in ("revoked", "expired"):
        ended = row["updated_at"]
    elif expires_at > now:  # pending / claimed and still open
        return Link(tenant_id, row["invitation_id"], "live_invitation", None)
    else:  # pending / claimed past its expiry (nobody marked it expired yet)
        ended = expires_at
    return Link(tenant_id, row["invitation_id"], None, ended, grace_waived=trashed)


def studio_link(row: studio.StudioLink, *, now: datetime) -> Link:
    if row.status in ("accepted", "activated"):
        return Link(None, row.studio_invitation_id, "studio_member", None)
    if row.status == "claimed" and row.expires_at > now:
        return Link(None, row.studio_invitation_id, "studio_invitation", None)
    ended = row.updated_at if row.status in ("expired", "revoked") else row.expires_at
    return Link(None, row.studio_invitation_id, None, ended)


def decide(links: list[Link], members: int, *, now: datetime, grace: timedelta) -> tuple[str | None, datetime | None]:
    """``(keep reason, None)`` or ``(None, untouched_since)``: the account may go if Cognito agrees that it is still
    ``FORCE_CHANGE_PASSWORD`` and unchanged since the last of its invitations ended."""
    if members:
        return "member", None
    for link in links:
        if link.keep is not None:
            return link.keep, None
    ended = [link.ended_at for link in links if link.ended_at is not None]
    if not ended:  # cannot happen (every link either keeps or ended); keep rather than guess
        return "unknown", None
    if any(not link.grace_waived and link.ended_at > now - grace for link in links if link.ended_at is not None):
        return "grace", None
    return None, max(ended)


async def _read_agency(ctx: SystemContext, facts: Facts, *, now: datetime) -> None:
    trashed = bool(await ctx.conn.fetchval(TENANT_SQL, ctx.tenant_id))
    for row in await ctx.conn.fetch(INVITATIONS_SQL):
        facts.add((row["cognito_iss"], row["cognito_sub"]), crm_link(row, ctx.tenant_id, trashed=trashed, now=now))
    for row in await ctx.conn.fetch(MEMBERS_SQL):
        facts.members[(row["cognito_iss"], row["cognito_sub"])] += 1


async def gather(pool: asyncpg.Pool, *, now: datetime) -> Facts:
    """Every agency's invitations and members (trashed agencies included) and every studio invitation bound to an
    identity. An agency that cannot be read stops the run (its links are unknown): nothing is deleted on half the
    facts."""
    facts = Facts()
    for agency in await registry.agencies(pool):
        try:
            async with partner_account_tx(pool, agency.tenant_id) as ctx:
                await _read_agency(ctx, facts, now=now)
        except NotFound:
            continue  # the registry row outlived its agency row: nothing of it is left to read
    for row in await studio.account_links(pool):
        facts.add((row.cognito_iss, row.cognito_sub), studio_link(row, now=now))
    return facts


def _crm_invitations(links: list[Link]) -> dict[uuid.UUID, list[uuid.UUID]]:
    by_agency: dict[uuid.UUID, list[uuid.UUID]] = {}
    for link in links:
        if link.tenant_id is not None:
            by_agency.setdefault(link.tenant_id, []).append(link.invitation_id)
    return {t: sorted(ids, key=str) for t, ids in sorted(by_agency.items(), key=lambda kv: str(kv[0]))}


def _record(ctx: SystemContext, invitation_ids: list[uuid.UUID]) -> None:
    ctx.audit(
        AUDIT_ACTION,
        "allowed",
        "member_invitation",
        str(invitation_ids[0]),
        reason=REASON,
        object_ids=tuple(str(i) for i in invitation_ids[:MAX_OBJECT_IDS]),
    )


async def _delete(
    pool: asyncpg.Pool, directory: PartnerDirectory, sub: str, untouched_since: datetime, links: list[Link]
) -> str:
    """Delete in the first agency's context (its audit event is written only when the delete succeeded), then record it
    in the other agencies. Returns the directory's outcome."""
    agencies = list(_crm_invitations(links).items())
    first_tenant, first_ids = agencies[0]
    async with partner_account_tx(pool, first_tenant) as ctx:
        outcome = await directory.delete_never_signed_in(sub, untouched_since=untouched_since)
        if outcome == DELETED:
            _record(ctx, first_ids)
    if outcome != DELETED:
        return outcome
    for tenant_id, ids in agencies[1:]:
        try:
            async with partner_account_tx(pool, tenant_id) as ctx:
                _record(ctx, ids)
        except Exception as exc:  # noqa: BLE001 — the account is gone; one agency's audit write must not hide that
            log.error("crm_partner_account_audit_failed tenant_id=%s error_class=%s", tenant_id, type(exc).__name__)
    for tenant_id, ids in agencies:
        log.info("crm_partner_account_deleted tenant_id=%s invitation_id=%s reason=%s", tenant_id, ids[0], REASON)
    return outcome


async def sweep(
    pool: asyncpg.Pool,
    directory: PartnerDirectory,
    *,
    dry_run: bool,
    tenant_id: uuid.UUID | None = None,
    limit: int = MAX_DELETES_PER_RUN,
    grace_days: int = GRACE_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One clean-up run. ``tenant_id``: only identities with a CRM invitation in that agency (the agency-delete run);
    every agency is still read for the links. ``limit`` is capped at :data:`MAX_DELETES_PER_RUN`. Returns counts."""
    now = now or datetime.now(UTC)
    limit = max(0, min(int(limit), MAX_DELETES_PER_RUN))
    grace = timedelta(days=grace_days)
    facts = await gather(pool, now=now)
    counts: Counter[str] = Counter()
    deleted = 0
    candidates = sorted(
        identity
        for identity, links in facts.links.items()
        if identity[0] == directory.issuer
        and any(link.tenant_id is not None and (tenant_id is None or link.tenant_id == tenant_id) for link in links)
    )
    for identity in candidates:
        links = facts.links[identity]
        keep, untouched_since = decide(links, facts.members[identity], now=now, grace=grace)
        if keep is not None or untouched_since is None:
            counts[f"kept_{keep}"] += 1
            continue
        if deleted >= limit:
            counts["capped"] += 1
            continue
        sub = identity[1]
        try:
            if dry_run:
                refused = delete_verdict(await directory.account_state(sub), untouched_since)
                outcome = DELETED if refused is None else refused
            else:
                outcome = await _delete(pool, directory, sub, untouched_since, links)
        except Exception as exc:  # noqa: BLE001 — one account's Cognito error must not stop the others
            counts["failed"] += 1
            log.error("crm_partner_account_cleanup_failed error_class=%s", type(exc).__name__)
            continue
        if outcome == DELETED:
            deleted += 1
            counts["would_delete" if dry_run else "deleted"] += 1
        elif outcome == GONE:
            counts["already_gone"] += 1
        else:
            counts[f"kept_{outcome}"] += 1
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "candidate_count": len(candidates),
        **{f"{k}_count": int(v) for k, v in sorted(counts.items())},
    }
    log.info(
        "crm_partner_account_cleanup dry_run=%s %s",
        dry_run,
        " ".join(f"{k}={v}" for k, v in result.items() if k != "dry_run"),
    )
    return result


def build_handler(directory: PartnerDirectory | None, *, dry_run: bool) -> Handler:
    """The ``crm_partner_accounts`` job: payload ``{purpose: daily_schedule}`` or ``{purpose: agency_delete,
    tenant_id}``. ``dry_run``: ``PARTNER_ACCOUNT_CLEANUP_DRY_RUN``."""

    async def handler(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        purpose = job.payload.get("purpose")
        if purpose not in PURPOSES:
            raise PermanentError("invalid_payload")
        tenant_id = None
        if purpose == "agency_delete":
            try:
                tenant_id = uuid.UUID(str(job.payload.get("tenant_id")))
            except ValueError:
                raise PermanentError("invalid_payload") from None
        if directory is None:
            raise PermanentError("directory_not_configured")
        result = await sweep(pool, directory, dry_run=dry_run, tenant_id=tenant_id)
        if result.get("failed_count"):
            # Each account's Cognito error was counted, not raised, so the others went on; the job still fails so the
            # worker retries it with its backoff (lessons: a per-item catch must not end the job "succeeded").
            raise CleanupIncomplete(f"failed_count={result['failed_count']}")
        return {"purpose": purpose, **result}

    return handler


class CleanupIncomplete(Exception):  # noqa: N818 — reads as an outcome; the worker retries it with backoff
    pass


async def queue_after_agency_delete(ctx: SystemContext) -> str | None:
    """Inside :func:`app.ops.console.delete_test_agency`'s transaction: the clean-up of that agency's accounts runs
    only if the delete commits (and is retried by the worker if Cognito fails)."""
    return await queue.enqueue(
        ctx.conn,
        PARTNER_ACCOUNTS,
        {"purpose": "agency_delete", "tenant_id": str(ctx.tenant_id)},
        idempotency_key=f"{PARTNER_ACCOUNTS}:agency_delete:{ctx.tenant_id}",
        max_attempts=MAX_ATTEMPTS,
        created_by=ctx.actor,
    )


def schedule_key(day: date) -> str:
    return f"{PARTNER_ACCOUNTS}:schedule:{day.isoformat()}"


class DailyPartnerAccountSchedule:
    """Worker maintenance hook: one ``crm_partner_accounts`` run per UTC day, due now, from ``utc_hour``; never
    queued ahead (the ``DailyLifecycleSchedule`` pattern)."""

    def __init__(self, utc_hour: int, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if not 0 <= utc_hour <= 23:
            raise ValueError(f"utc_hour must be 0..23, got {utc_hour}")
        self._utc_hour = utc_hour
        self._now = now
        self._done_day: date | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        now = self._now().astimezone(UTC)
        today = now.date()
        if now.hour < self._utc_hour or self._done_day == today:
            return None
        job_id = await queue.enqueue(
            pool,
            PARTNER_ACCOUNTS,
            {"purpose": "daily_schedule"},
            idempotency_key=schedule_key(today),
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, PARTNER_ACCOUNTS, HISTORY_DAYS)
        self._done_day = today
        return job_id


async def _main(argv: list[str]) -> int:  # pragma: no cover — the container entry point; sweep() is tested
    from app.db import Database
    from app.members.directory import CognitoDirectory
    from app.settings import load_settings

    parser = argparse.ArgumentParser(prog="python -m app.members.accounts", description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="delete for real (default: dry run, counts only)")
    parser.add_argument("--limit", type=int, default=MAX_DELETES_PER_RUN, help=f"at most {MAX_DELETES_PER_RUN}")
    parser.add_argument("--tenant", type=uuid.UUID, default=None, help="only the accounts of this agency's invitations")
    parser.add_argument(
        "--grace-days",
        type=int,
        default=GRACE_DAYS,
        help=f"days an ended invitation keeps its account (default {GRACE_DAYS}; an operator may lower it, never < 0)",
    )
    args = parser.parse_args(argv)
    settings = load_settings()
    if not (settings.cognito_partner_pool_id and settings.cognito_partner_issuer):
        print(json.dumps({"error": "directory_not_configured"}))
        return 2
    directory = CognitoDirectory(settings.cognito_partner_pool_id, settings.cognito_partner_issuer)
    db = Database(settings)
    try:
        pool = await db.pool()
        result = await sweep(
            pool,
            directory,
            dry_run=not args.apply,
            tenant_id=args.tenant,
            limit=args.limit,
            grace_days=max(0, args.grace_days),
        )
    finally:
        await db.close()
    print(json.dumps(result, sort_keys=True))
    return 0


def cli(argv: list[str] | None = None) -> int:  # pragma: no cover
    return asyncio.run(_main(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli())
