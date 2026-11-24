"""The agency status read of :func:`app.tenancy.system_tx`, cached per process (S291, AA-838, C4.3 W48 finding 4).

A job's context in one agency (``system_tx``) reads ``crm.tenant.status`` when it opens. The scheduled Booking sync
alone opened about six of them per agency and minute run, about 6.4 status reads a second on Dev. Here a job context
keeps an ``active`` answer for ``crm.jobs.tenant_status_cache_s`` seconds (default 10, 0 = off) from the read that saw
it, and reads the database again after that.

Fail-closed: only ``active`` is kept. Any other answer (locked, offboarding, offboarded, a trashed or unknown agency)
is never cached and drops the agency's entry, so the next context reads again. The bound: an agency locked, offboarded
or trashed in any process is refused by every job context of this process at most ``crm.jobs.tenant_status_cache_s``
seconds after the change commits (plus the length of a job transaction already open, as before).

Only job contexts use the cache (``job=True``, :func:`app.tenancy._agency_tx` with no ``statuses``). Member requests
(``tenant_tx``), the AA staff context (``platform_tx``), the feeds and the public contexts read the status every time.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import asyncpg

from app import platform_settings as ps

_SEEN: dict[uuid.UUID, float] = {}  # agency -> clock time of the read that saw it active
CACHE_MAX = 5000  # technical-constant: per-process memory bound of the cache (the oldest entry is dropped)
_clock: Callable[[], float] = time.monotonic


async def read(conn: asyncpg.Connection, sql: str, tenant_id: uuid.UUID, *, job: bool) -> str | None:
    """The agency's status through ``sql`` (:data:`app.tenancy.TENANT_SQL`); a job context answers ``active`` from
    the cache while it is fresh."""
    if not job:
        return await conn.fetchval(sql, tenant_id)
    ttl = float(ps.get(ps.JOBS_TENANT_STATUS_CACHE_S))
    now = _clock()
    seen = _SEEN.get(tenant_id)
    if seen is not None and now - seen < ttl:
        return "active"
    status = await conn.fetchval(sql, tenant_id)
    _SEEN.pop(tenant_id, None)
    if status == "active" and ttl > 0:
        if len(_SEEN) >= CACHE_MAX:
            _SEEN.pop(next(iter(_SEEN)))
        _SEEN[tenant_id] = now
    return status


def forget(tenant_id: uuid.UUID | None = None) -> None:
    """Drop the cached status of one agency, or of every agency (tests; a status change in this process may call it)."""
    if tenant_id is None:
        _SEEN.clear()
    else:
        _SEEN.pop(tenant_id, None)
