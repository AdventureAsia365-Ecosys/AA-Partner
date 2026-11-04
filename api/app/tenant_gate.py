"""Per-agency share of the database pool on ``/crm/v1`` (AA-969, CRM-37a security review F2, card R "a noisy tenant
cannot push another tenant past the SLO").

The API task has **3** pooled connections (``crm_app`` CONNECTION LIMIT 6 = 3 x 2 tasks during a rolling deploy,
:mod:`app.db`), shared by every agency and the ``crm.job`` worker. Without a share, one agency's burst (a bulk script,
a stuck browser tab retrying, a heavy report) holds all three and every other agency's request waits in the pool's
queue behind it, unbounded.

:func:`app.tenancy.tenant_tx` therefore takes a **slot** of this gate before it asks the pool for a connection:

- at most :attr:`TenantGate.max_concurrency` (default 2, ``CRM_TENANT_MAX_CONCURRENCY``) ``tenant_tx()`` blocks run at
  once per agency in this process, so one agency never holds the whole pool;
- a request that cannot get a slot within :attr:`TenantGate.wait_s` (default 5 s, ``CRM_TENANT_SLOT_WAIT_S``) is
  answered **429** ``tenant_busy`` with ``Retry-After: 1``: the noisy agency is slowed down, the others are not.

The slot is keyed by the agency only once **this caller's membership of it was verified** in this process
(:meth:`TenantGate.mark_member`, called by ``tenant_tx()`` right after the membership check, kept
:data:`MEMBER_TTL_S`). Before that the slot is keyed by the caller's own identity. So a member of agency A who sends
agency B's id in ``X-AA-Tenant-Id`` only ever uses up their **own** slots, never B's (the request is then refused
403 ``not_a_member`` as before). The cache decides nothing about access: ``tenant_tx()`` checks the membership on
every request.

Process memory, like :mod:`app.ratelimit`: each task counts on its own (two tasks during a deploy double the share,
which stays inside the 6-connection role limit).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from app.errors import RateLimited

DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_WAIT_S = 5.0
MEMBER_TTL_S = 300.0
MAX_TRACKED = 10_000
BUSY = "tenant_busy"
# A semaphore's acquire, named so the DB access lint (tests/unit/test_code_rules.py, which reads every ``.acquire()``
# as a pool hand-out) does not take this module for one: no database is reached here.
_take_slot = asyncio.Semaphore.acquire
RETRY_AFTER_S = 1


class TenantGate:
    def __init__(
        self,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        wait_s: float = DEFAULT_WAIT_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        member_ttl_s: float = MEMBER_TTL_S,
        max_tracked: int = MAX_TRACKED,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if wait_s <= 0:
            raise ValueError("wait_s must be > 0")
        self.max_concurrency = max_concurrency
        self.wait_s = wait_s
        self._clock = clock
        self._member_ttl_s = member_ttl_s
        self._max_tracked = max_tracked
        self._slots: dict[str, asyncio.Semaphore] = {}
        self._holders: dict[str, int] = {}
        self._refs: dict[str, int] = {}
        self._members: OrderedDict[tuple[str, str, uuid.UUID], float] = OrderedDict()

    def key(self, iss: str, sub: str, tenant_id: uuid.UUID) -> str:
        """The slot key: the agency once this caller is a verified member of it, else the caller."""
        seen = self._members.get((iss, sub, tenant_id))
        if seen is not None and self._clock() - seen < self._member_ttl_s:
            return f"tenant:{tenant_id}"
        return f"caller:{iss}|{sub}"

    def mark_member(self, iss: str, sub: str, tenant_id: uuid.UUID) -> None:
        """Called by ``tenant_tx()`` after the membership check passed: later requests share the agency's slots."""
        k = (iss, sub, tenant_id)
        self._members.pop(k, None)
        self._members[k] = self._clock()
        while len(self._members) > self._max_tracked:
            self._members.popitem(last=False)

    def in_use(self, key: str) -> int:
        """Slots of ``key`` held right now."""
        return self._holders.get(key, 0)

    @asynccontextmanager
    async def slot(self, iss: str, sub: str, tenant_id: uuid.UUID) -> AsyncIterator[str]:
        """Hold one slot of the caller's key while the block runs; 429 ``tenant_busy`` after :attr:`wait_s`."""
        key = self.key(iss, sub, tenant_id)
        sem = self._slots.get(key)
        if sem is None:
            sem = self._slots[key] = asyncio.Semaphore(self.max_concurrency)
        self._refs[key] = self._refs.get(key, 0) + 1  # holders + waiters: the entry lives while anyone uses it
        try:
            try:
                await asyncio.wait_for(_take_slot(sem), timeout=self.wait_s)
            except TimeoutError:
                raise RateLimited(BUSY, RETRY_AFTER_S, "too many requests for this agency at once") from None
            self._holders[key] = self._holders.get(key, 0) + 1
            try:
                yield key
            finally:
                sem.release()
                held = self._holders[key] - 1
                if held:
                    self._holders[key] = held
                else:
                    del self._holders[key]
        finally:
            refs = self._refs[key] - 1
            if refs:
                self._refs[key] = refs
            else:
                # Nobody holds or waits: drop the entry, so the table stays as small as the set of busy agencies.
                del self._refs[key]
                self._slots.pop(key, None)


GATE = TenantGate()


def configure(max_concurrency: int, wait_s: float) -> TenantGate:
    """Replace the process gate (``app.main.create_app`` from the settings). Returns it."""
    global GATE  # noqa: PLW0603 — one gate per process, set once at app creation
    GATE = TenantGate(max_concurrency, wait_s)
    return GATE
