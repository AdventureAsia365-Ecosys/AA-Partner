"""Fair share of the database pool on the request path (AA-969, CRM-37a security review F2, card R "a noisy tenant
cannot push another tenant past the SLO").

The API task has **3** pooled connections (``crm_app`` CONNECTION LIMIT 6 = 3 x 2 tasks during a rolling deploy,
:mod:`app.db`), shared by every agency, the public routes and the in-process ``crm.job`` worker. Without a share, one
agency's burst (a bulk script, a stuck browser tab retrying, a heavy report), or two at once, or a flood on a public
route, holds all three and every other request waits in the pool's FIFO queue behind it.

Every request-path transaction therefore takes a **slot** of this gate before it asks the pool for a connection
(:func:`app.tenancy.tenant_tx`, and the request-path contexts through ``app.tenancy._gated``):

- **Total**: at most :attr:`TenantGate.total_slots` (default :data:`REQUEST_SLOTS` = pool size - 1 = 2) gated blocks
  hold a connection at once in this process, so one connection always stays for the ``crm.job`` worker and the AA
  staff paths, whatever the request traffic does.
- **Per key**: at most :attr:`TenantGate.max_concurrency` (default 2, ``CRM_TENANT_MAX_CONCURRENCY``) per key. The key
  is the agency (``tenant:<id>``) once this caller's membership of it was verified, else the caller
  (``caller:<iss>|<sub>``); every anonymous route (inquiry form, client links, calendar feed, invitation claim, Twilio
  webhooks) shares the one key :data:`PUBLIC_KEY`.
- **Fair hand-out**: a slot that frees up goes to the waiting key that holds the **fewest** slots right now, then to the
  key served least recently, then first come first served inside a key. So a calm agency's request gets the next slot
  that frees up, ahead of every queued request of the busy agencies (two busy agencies, or one plus a public flood,
  cannot keep it waiting longer than one of their transactions). With two keys waiting, a key that already holds a
  slot never gets a second one before the other gets its first: the per-key share shrinks to 1 under contention.
- **Bounded wait**: a request that gets no slot within :attr:`TenantGate.wait_s` (default 5 s,
  ``CRM_TENANT_SLOT_WAIT_MS``) is answered **429** ``tenant_busy`` with ``Retry-After: 1`` (an agency or caller key) or
  **503** ``db_busy`` (the public key): the busy key is slowed down, the others are not.

The slot is keyed by the agency only once **this caller's membership of it was verified** in this process
(:meth:`TenantGate.mark_member`, called by ``tenant_tx()`` right after the membership check, kept
:data:`MEMBER_TTL_S`). So a member of agency A who sends agency B's id in ``X-AA-Tenant-Id`` only ever uses up their
**own** slots, never B's (the request is then refused 403 ``not_a_member`` as before). The cache decides nothing about
access: ``tenant_tx()`` checks the membership on every request.

A block nested inside another gated block of the same task (one request = one slot) does not take a second slot:
otherwise two requests that each hold one slot and nest would wait for each other until the timeout.

Process memory, like :mod:`app.ratelimit`: each task counts on its own (two tasks during a deploy double the share,
which stays inside the 6-connection role limit).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.errors import RETRY_AFTER_DB_BUSY_S, DatabaseBusy, RateLimited

DEFAULT_MAX_CONCURRENCY = 2
# app.db pool max_size (3) minus the connection kept for the crm.job worker and the AA staff paths.
POOL_SIZE = 3
REQUEST_SLOTS = POOL_SIZE - 1
DEFAULT_WAIT_S = 5.0
MEMBER_TTL_S = 300.0
MAX_TRACKED = 10_000
BUSY = "tenant_busy"
DB_BUSY = "db_busy"
RETRY_AFTER_S = 1
PUBLIC_KEY = "class:public"

# The gate whose slot the running task holds, and that task (re-entrancy: a nested block of the same task does not
# take a second slot; a task started inside the block copies the context but is another task, so it takes its own).
_HOLDING: ContextVar[tuple[TenantGate, asyncio.Task[object] | None] | None] = ContextVar(
    "tenant_gate_holding", default=None
)


def tenant_busy() -> Exception:
    return RateLimited(BUSY, RETRY_AFTER_S, "too many requests for this agency at once")


def public_busy() -> Exception:
    return DatabaseBusy(DB_BUSY, RETRY_AFTER_DB_BUSY_S, "every database connection is busy")


@dataclass(eq=False)
class _Waiter:
    arrival: int
    future: asyncio.Future[None] = field(repr=False)


class TenantGate:
    def __init__(
        self,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        wait_s: float = DEFAULT_WAIT_S,
        *,
        total_slots: int = REQUEST_SLOTS,
        clock: Callable[[], float] = time.monotonic,
        member_ttl_s: float = MEMBER_TTL_S,
        max_tracked: int = MAX_TRACKED,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if total_slots < 1:
            raise ValueError("total_slots must be >= 1")
        if wait_s <= 0:
            raise ValueError("wait_s must be > 0")
        self.max_concurrency = max_concurrency
        self.total_slots = total_slots
        self.wait_s = wait_s
        self._clock = clock
        self._member_ttl_s = member_ttl_s
        self._max_tracked = max_tracked
        self._held = 0
        self._holders: dict[str, int] = {}
        self._waiters: dict[str, deque[_Waiter]] = {}
        self._last_grant: dict[str, int] = {}
        self._seq = 0
        self._members: OrderedDict[tuple[str, str, uuid.UUID], float] = OrderedDict()

    # ── keys ────────────────────────────────────────────────────────────────────────────────────────────────────
    def key(self, iss: str, sub: str, tenant_id: uuid.UUID) -> str:
        """The slot key: the agency once this caller is a verified member of it, else the caller."""
        seen = self._members.get((iss, sub, tenant_id))
        if seen is not None and self._clock() - seen < self._member_ttl_s:
            return f"tenant:{tenant_id}"
        return caller_key(iss, sub)

    def mark_member(self, iss: str, sub: str, tenant_id: uuid.UUID) -> None:
        """Called by ``tenant_tx()`` after the membership check passed: later requests share the agency's slots."""
        k = (iss, sub, tenant_id)
        self._members.pop(k, None)
        self._members[k] = self._clock()
        while len(self._members) > self._max_tracked:
            self._members.popitem(last=False)

    # ── state ───────────────────────────────────────────────────────────────────────────────────────────────────
    def in_use(self, key: str | None = None) -> int:
        """Slots of ``key`` held right now (every key's when ``key`` is None)."""
        return self._held if key is None else self._holders.get(key, 0)

    def waiting(self, key: str) -> int:
        return len(self._waiters.get(key, ()))

    def tracked(self) -> int:
        """Keys with a holder, a waiter or a fairness record (the table empties when nobody holds or waits)."""
        return len(set(self._holders) | set(self._waiters) | set(self._last_grant))

    # ── slots ───────────────────────────────────────────────────────────────────────────────────────────────────
    @asynccontextmanager
    async def slot(self, iss: str, sub: str, tenant_id: uuid.UUID) -> AsyncIterator[str]:
        """``tenant_tx()``'s slot: the agency's (before a verified membership the caller's); 429 ``tenant_busy``."""
        async with self.hold(self.key(iss, sub, tenant_id)) as key:
            yield key

    @asynccontextmanager
    async def hold(self, key: str, *, busy: Callable[[], Exception] = tenant_busy) -> AsyncIterator[str]:
        """Hold one slot of ``key`` while the block runs; ``busy()`` is raised after :attr:`wait_s` without one."""
        task = asyncio.current_task()
        holding = _HOLDING.get()
        if holding is not None and holding[0] is self and holding[1] is task:
            yield key  # nested in a block of this task that holds a slot of this gate already: one request, one slot
            return
        waiter = self._enqueue(key)
        self._dispatch()
        if not waiter.future.done():
            try:
                await asyncio.wait_for(asyncio.shield(waiter.future), timeout=self.wait_s)
            except TimeoutError:
                if self._withdraw(key, waiter):
                    raise busy() from None
                # Granted at the very moment the wait ran out: keep the slot.
            except BaseException:
                if not self._withdraw(key, waiter):
                    self._release(key)  # granted, but the request was cancelled before it could use it
                raise
        token = _HOLDING.set((self, task))
        try:
            yield key
        finally:
            _HOLDING.reset(token)
            self._release(key)

    def _enqueue(self, key: str) -> _Waiter:
        self._seq += 1
        waiter = _Waiter(self._seq, asyncio.get_running_loop().create_future())
        self._waiters.setdefault(key, deque()).append(waiter)
        return waiter

    def _withdraw(self, key: str, waiter: _Waiter) -> bool:
        """Take a waiter out of the queue. False when it was granted already (it then holds a slot)."""
        if waiter.future.done() and not waiter.future.cancelled():
            return False
        queue = self._waiters.get(key)
        if queue is not None:
            try:
                queue.remove(waiter)
            except ValueError:
                pass
            if not queue:
                del self._waiters[key]
        waiter.future.cancel()
        self._forget_if_idle(key)
        return True

    def _next_key(self) -> str | None:
        """The waiting key that gets the next free slot: fewest held, then served least recently, then first come."""
        best: tuple[int, int, int] | None = None
        chosen: str | None = None
        for key, queue in self._waiters.items():
            held = self._holders.get(key, 0)
            if held >= self.max_concurrency or not queue:
                continue
            rank = (held, self._last_grant.get(key, 0), queue[0].arrival)
            if best is None or rank < best:
                best, chosen = rank, key
        return chosen

    def _dispatch(self) -> None:
        while self._held < self.total_slots:
            key = self._next_key()
            if key is None:
                return
            queue = self._waiters[key]
            waiter = queue.popleft()
            if not queue:
                del self._waiters[key]
            self._held += 1
            self._holders[key] = self._holders.get(key, 0) + 1
            self._seq += 1
            self._last_grant[key] = self._seq
            waiter.future.set_result(None)

    def _release(self, key: str) -> None:
        self._held -= 1
        held = self._holders[key] - 1
        if held:
            self._holders[key] = held
        else:
            del self._holders[key]
        self._forget_if_idle(key)
        self._dispatch()

    def _forget_if_idle(self, key: str) -> None:
        # Nobody holds or waits: drop the key, so the tables stay as small as the set of busy keys.
        if key not in self._holders and key not in self._waiters:
            self._last_grant.pop(key, None)


def caller_key(iss: str, sub: str) -> str:
    return f"caller:{iss}|{sub}"


GATE = TenantGate()


def configure(max_concurrency: int, wait_s: float, total_slots: int = REQUEST_SLOTS) -> TenantGate:
    """Replace the process gate (``app.main.create_app`` from the settings). Returns it."""
    global GATE  # noqa: PLW0603 — one gate per process, set once at app creation
    GATE = TenantGate(max_concurrency, wait_s, total_slots=total_slots)
    return GATE
