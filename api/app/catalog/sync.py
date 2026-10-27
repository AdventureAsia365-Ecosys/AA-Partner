"""``crm_catalog_sync`` (AA-947): the hourly refresh of ``crm.aa_catalog_tour`` from CIS ``catalog.http.v1``.

Queued **due now** once per UTC hour by :class:`HourlyCatalogSync` (never ahead: the deploy gate and the DB Migrate
idle check count a queued row as busy). One run, bounded (:data:`MAX_PAGES` list pages, :data:`MAX_DETAILS` tour
fetches, :data:`MAX_RECHECKS` re-checks); an unfinished pass keeps its cursor and continues in the next run:

1. list the published tours (pages of 50); a tour whose ``content_hash`` matches the cache is only marked checked;
   a new or changed one is fetched (``If-None-Match`` the stored ETag) and stored with its served body;
2. at the end of a pass, every cached tour the pass did not see is asked again: 410 -> ``retired`` (AA lines of it
   read "no longer offered"), 404 with a reason -> ``paused``, 404 without one -> ``paused`` / ``not_in_catalog``.

The contract's ``updated_since`` is not a full change cursor (AA-976 notes), so the sync always runs full passes
comparing ``content_hash``. AA-951: after the tour pass (finished or partial) the AA-authored content (prep packs,
starter templates) is refreshed too (:mod:`app.catalog.content`); its failure is reported, never fatal. A CIS failure
ends the run successfully with the error class in ``crm.aa_catalog_state``
and backs off (the hour after, at most :data:`BACKOFF_MAX_S` later). Payload and result are counts only.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from collections.abc import Callable
from typing import Any

import asyncpg

from app.catalog import content, store
from app.catalog.client import CatalogClient, CatalogConfig, CatalogUnavailable, ContentClient, TourAnswer, summary
from app.jobs import queue
from app.jobs.kinds import CATALOG_SYNC
from app.jobs.queue import Job

log = logging.getLogger(__name__)

SCHEDULER = "scheduler:crm-catalog-sync"
MAX_PAGES = 10
MAX_DETAILS = 100
MAX_RECHECKS = 50
MAX_ATTEMPTS = 3
HISTORY_DAYS = 2
BACKOFF_BASE_S = 300
BACKOFF_MAX_S = 6 * 3600
STUCK_FAILURES = 6


def backoff_seconds(failures: int) -> int:
    if failures <= 0:
        return 0
    return min(BACKOFF_BASE_S * 2 ** min(failures - 1, 10), BACKOFF_MAX_S)


async def _store_answer(pool: asyncpg.Pool, source_id: str, ans: TourAnswer, counts: Counter[str]) -> None:
    if ans.state == "served" and ans.body is not None:
        await store.upsert(pool, summary(ans.body), ans.etag, ans.body)
        counts["fetched"] += 1
    elif ans.state == "not_modified":
        await store.touch(pool, [source_id])
        counts["unchanged"] += 1
    elif ans.state == "retired":
        await store.set_state(pool, source_id, "retired", ans.reason)
        counts["retired"] += 1
    else:  # paused, or no longer known at all
        await store.set_state(pool, source_id, "paused", ans.reason or "not_in_catalog")
        counts["paused"] += 1


class CatalogSync:
    def __init__(
        self, config: CatalogConfig | None, *, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)
    ) -> None:
        self._client = ContentClient(config) if config is not None else None
        self._now = now
        self._warned = False

    async def __call__(self, pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
        if self._client is None:
            if not self._warned:
                log.warning("crm_catalog_sync not_configured (CIS_API_URL / M2M_CLIENT)")
                self._warned = True
            return {"status": "not_configured"}
        state = await store.load_state(pool)
        if state.failures and state.last_error_at is not None:
            if self._now() < state.last_error_at + dt.timedelta(seconds=backoff_seconds(state.failures)):
                return {"status": "backoff"}
        counts: Counter[str] = Counter()
        try:
            done = await self._run(pool, state, counts)
        except CatalogUnavailable as exc:
            failures = await store.fail(pool, exc.error_class)
            log.warning("crm_catalog_sync_failed error=%s failures=%d", exc.error_class, failures)
            if failures >= STUCK_FAILURES:
                log.error("crm_catalog_sync_stuck error=%s failures=%d", exc.error_class, failures)
            return {"status": "error", "reason_code": exc.error_class, **_counts(counts)}
        await content.refresh_safely(pool, self._client, counts)
        return {"status": "pass_done" if done else "partial", **_counts(counts)}

    async def _run(self, pool: asyncpg.Pool, state: store.SyncState, counts: Counter[str]) -> bool:
        client = self._client
        assert client is not None  # noqa: S101 — checked by the caller
        cursor = state.cursor
        if cursor is None:
            await store.save_cursor(pool, None)  # a new pass: its start time
        details = 0
        for _ in range(MAX_PAGES):
            summaries, next_cursor = await client.list_tours(cursor)
            counts["pages"] += 1
            cached = await store.known(pool, [s.source_id for s in summaries])
            unchanged: list[str] = []
            for s in summaries:
                row = cached.get(s.source_id)
                if row is not None and row["content_hash"] == s.content_hash and row["served_state"] == "served":
                    unchanged.append(s.source_id)
                    continue
                if details >= MAX_DETAILS:
                    await store.touch(pool, unchanged)
                    await store.succeed(pool)
                    return False  # this page again next run (its cursor is kept)
                etag = row["etag"] if row is not None and row["served_state"] == "served" else None
                await _store_answer(pool, s.source_id, await client.get_tour(s.source_id, etag), counts)
                details += 1
            await store.touch(pool, unchanged)
            counts["unchanged"] += len(unchanged)
            if next_cursor is None:
                await self._recheck(pool, client, counts)
                await store.pass_done(pool)
                return True
            cursor = next_cursor
            await store.save_cursor(pool, cursor)
        await store.succeed(pool)
        return False

    async def _recheck(self, pool: asyncpg.Pool, client: CatalogClient, counts: Counter[str]) -> None:
        started = (await store.load_state(pool)).pass_started_at or self._now()
        for source_id, etag in await store.stale(pool, started, MAX_RECHECKS):
            await _store_answer(pool, source_id, await client.get_tour(source_id, etag), counts)
            counts["rechecked"] += 1


def _counts(counts: Counter[str]) -> dict[str, int]:
    return {f"{k}_count": int(v) for k, v in sorted(counts.items())}


def schedule_key(now: dt.datetime) -> str:
    return f"{CATALOG_SYNC}:schedule:{now.strftime('%Y-%m-%dT%H')}"


class HourlyCatalogSync:
    """Worker maintenance hook: one ``crm_catalog_sync`` run per UTC hour, due now (the hourly hook pattern)."""

    def __init__(self, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        self._now = now
        self._done_slot: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        key = schedule_key(self._now())
        if self._done_slot == key:
            return None
        job_id = await queue.enqueue(
            pool,
            CATALOG_SYNC,
            {"purpose": "hourly_schedule"},
            idempotency_key=key,
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, CATALOG_SYNC, HISTORY_DAYS)
        self._done_slot = key
        return job_id
