"""The 24 h lifetime of the F3 placeholder map (design v3 §3.6 "OTP, placeholder map: 24 h"; lane §6 "Purge job").

``crm_placeholder_purge`` runs every hour (queued due-now by :class:`HourlyPlaceholderPurge`, one idempotency key per
UTC hour, never queued ahead: the deploy gate and DB Migrate count a queued row as busy). It deletes every expired map
of every agency in one statement, through the crm-009 policy ``placeholder_map_expired_purge``: a DELETE policy TO
``crm_app`` that matches only rows whose ``expires_at`` has passed and only while the transaction set
``app.purge_scope = 'placeholder_map'``. No tenant is set (``tenant_isolation`` matches nothing), there is no SELECT
policy for this scope (the job never reads a map), and the DELETE has no WHERE clause, so only the DELETE policy
decides. A map is therefore gone at most about an hour after it expired; reads refuse it from the minute it expires
(``placeholder_store.LOAD_SQL``). The result is a count only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app.jobs import queue
from app.jobs.kinds import PLACEHOLDER_PURGE
from app.jobs.queue import Job

log = logging.getLogger(__name__)

SCOPE_SQL = "SELECT set_config('app.purge_scope', 'placeholder_map', true)"
PURGE_SQL = "DELETE FROM crm.placeholder_map"
SCHEDULER = "scheduler:crm-placeholder-purge"
HISTORY_DAYS = 2
MAX_ATTEMPTS = 3


async def purge_expired_maps(pool: asyncpg.Pool, _job: Job) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(SCOPE_SQL)
        status = await conn.execute(PURGE_SQL)
    deleted = int(status.split()[-1])
    if deleted:
        log.info("placeholder purge: deleted %d expired maps", deleted)
    return {"deleted_count": deleted}


def schedule_key(now: datetime) -> str:
    return f"{PLACEHOLDER_PURGE}:schedule:{now.strftime('%Y-%m-%dT%H')}"


class HourlyPlaceholderPurge:
    """Worker maintenance hook: one ``crm_placeholder_purge`` run per UTC hour, due now (the daily retention pattern of
    ``app.jobs.handlers.DailyRetentionSchedule`` with an hourly slot)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_slot: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        key = schedule_key(self._now())
        if self._done_slot == key:
            return None
        job_id = await queue.enqueue(
            pool,
            PLACEHOLDER_PURGE,
            {"purpose": "hourly_schedule"},
            idempotency_key=key,
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        await queue.prune(pool, PLACEHOLDER_PURGE, HISTORY_DAYS)
        self._done_slot = key
        return job_id
