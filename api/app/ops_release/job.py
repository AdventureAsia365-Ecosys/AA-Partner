"""``crm_ops_release_sync``: the ops release job (AA-956, card CRM-26; design v3 F8.3, F10.3, Fix B2).

Two kinds of row, one handler:

- **hourly** (``{"purpose": "hourly_schedule"}``, queued due now once per UTC hour by :class:`HourlyOpsReleaseSync`,
  never ahead, not deploy-gate exempt): for every active agency, in one :func:`app.tenancy.system_tx` (actor
  ``job:crm_ops_release_sync``), with "today" in the agency's time zone: every trip with a confirmed AA line (not past
  its purge window) or a live release is synced (:func:`app.ops_release.service.sync_trip`: create, refresh, T-21
  alert, withdraw), then every release due is purged with a receipt (:func:`app.ops_release.service.purge_due`).
- **nudge** (``{"tenant_id", "trip_id"}``, queued in the caller's transaction by
  :func:`app.ops_release.service.nudge`: Booking confirmed / declined / cancelled an AA line, a member withdrew, the
  trip was cancelled): that trip only, then the purge of that agency, so a confirm reaches AA ops within about a
  minute and a withdrawal before departure is purged at once.

Payload and result are ids and counts only. A locked, offboarding or gone agency is skipped (a nudge for it finishes
as ``skipped``); its releases keep their last version until the next pass after an unlock.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app.audit_export import registry
from app.errors import Forbidden, NotFound, ServiceError
from app.jobs import queue
from app.jobs.kinds import OPS_RELEASE_SYNC
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.ops_release import service
from app.tenancy import SystemContext, system_tx
from app.vault import DecryptionDenied, KeyProvider, Vault

log = logging.getLogger(__name__)

SCHEDULER = "scheduler:crm-ops-release-sync"
HISTORY_DAYS = 2
MAX_ATTEMPTS = 3


async def run_agency(ctx: SystemContext, vault: Vault, trip_ids: list[uuid.UUID] | None = None) -> Counter[str]:
    """Sync ``trip_ids`` (default: every due trip of the agency), then purge what is due."""
    counts: Counter[str] = Counter()
    on = await service.today(ctx)
    trips = trip_ids if trip_ids is not None else await service.due_trips(ctx, on)
    built: list[str] = []
    for trip_id in trips:
        outcome = await service.sync_trip(ctx, vault, trip_id, on)
        for part in outcome.split("+"):
            counts[part] += 1
        if outcome.split("+")[0] in ("created", "relisted", "refreshed", "unchanged", "updated"):
            built.append(str(trip_id))
    service.opened_fields_event(ctx, built)
    purged = await service.purge_due(ctx, on)
    if purged:
        counts["purged"] += purged
    counts.pop("none", None)
    return counts


def _ids(payload: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID] | None:
    if "trip_id" not in payload:
        return None
    try:
        return uuid.UUID(str(payload["tenant_id"])), uuid.UUID(str(payload["trip_id"]))
    except (KeyError, ValueError):
        raise PermanentError("bad_payload") from None


def build_handler(keys: KeyProvider | None) -> Handler:
    async def crm_ops_release_sync(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        if keys is None:
            raise RuntimeError("key_provider_not_configured")  # retried: the provider may be configured by then
        one = _ids(job.payload)
        if one is not None:
            tenant_id, trip_id = one
            try:
                async with system_tx(pool, tenant_id, actor=service.ACTOR) as ctx:
                    counts = await run_agency(ctx, Vault(keys, ctx), [trip_id])
            except DecryptionDenied as exc:
                raise PermanentError(type(exc).__name__) from None
            except (Forbidden, NotFound):
                return {"agency_count": 0, "skipped_count": 1}
            return {"agency_count": 1, **{f"{k}_count": int(v) for k, v in sorted(counts.items())}}
        return await hourly(pool, keys)

    return crm_ops_release_sync


async def hourly(pool: asyncpg.Pool, keys: KeyProvider) -> dict[str, Any]:
    totals: Counter[str] = Counter()
    done = skipped = failed = 0
    for agency in await registry.agencies(pool):
        try:
            async with system_tx(pool, agency.tenant_id, actor=service.ACTOR) as ctx:
                counts = await run_agency(ctx, Vault(keys, ctx))
        except DecryptionDenied:
            failed += 1  # audited by system_tx (the denied event survives the rollback)
            log.error("ops_release_sync_failed tenant_id=%s error_class=DecryptionDenied", agency.tenant_id)
            continue
        except (Forbidden, NotFound):
            skipped += 1  # locked, offboarding or gone
            continue
        except ServiceError as exc:
            failed += 1
            log.error("ops_release_sync_failed tenant_id=%s code=%s", agency.tenant_id, exc.code)
            continue
        except Exception as exc:  # noqa: BLE001 — one agency's error must not skip every later agency
            failed += 1
            log.error("ops_release_sync_failed tenant_id=%s error_class=%s", agency.tenant_id, type(exc).__name__)
            continue
        done += 1
        totals.update(counts)
    if sum(totals.values()):
        log.info("ops_release_sync %s", " ".join(f"{k}={v}" for k, v in sorted(totals.items())))
    return {
        "agency_count": done,
        "skipped_count": skipped,
        "failed_count": failed,
        **{f"{k}_count": int(v) for k, v in sorted(totals.items())},
    }


def schedule_key(now: datetime) -> str:
    return f"{OPS_RELEASE_SYNC}:schedule:{now.strftime('%Y-%m-%dT%H')}"


class HourlyOpsReleaseSync:
    """Worker maintenance hook: one hourly ``crm_ops_release_sync`` run per UTC hour, due now (the
    :class:`app.stages_job.HourlyStageDateRule` pattern)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_slot: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        key = schedule_key(self._now())
        if self._done_slot == key:
            return None
        job_id = await queue.enqueue(
            pool,
            OPS_RELEASE_SYNC,
            {"purpose": "hourly_schedule"},
            idempotency_key=key,
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, OPS_RELEASE_SYNC, HISTORY_DAYS)
        self._done_slot = key
        return job_id
