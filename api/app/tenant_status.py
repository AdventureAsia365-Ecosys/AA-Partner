"""The agency status read of :func:`app.tenancy.system_tx`, remembered for one Booking sync run of one agency (S291,
AA-838, C4.3 W48 finding 4).

A job's context in one agency (``system_tx``) reads ``crm.tenant.status`` when it opens. The scheduled Booking sync
alone opened about six of them per agency and minute run, about 6.4 status reads a second on Dev. Inside a
:func:`memo` scope (the Booking sync runs each agency in one, :func:`scoped` in ``BookingSync.__call__``) the first job
context reads the status and the later ones of that run reuse an ``active`` answer; outside a scope (intake, campaigns,
imports, dispatch, mail import, every other job) every context reads the status, as before.

Fail-closed:

- only ``active`` is remembered; any other answer (locked, offboarding, offboarded, a trashed or unknown agency) is
  never kept and drops the agency from the scope, so the next context reads again;
- a scope ends with the agency's run, so a status change made in another process reaches this process's job contexts
  at the next agency run at the latest (the minute run), and a context of a later run always reads again;
- every writer in this process that moves an agency away from ``active`` (lock, offboarding, erase, trash) calls
  :func:`leaving` inside its transaction: the agency is dropped from every open scope and is never remembered again
  by this process, so every later job context reads the status (before the commit: the old ``active``, as before;
  after it: the refusal). ``tests/unit/test_code_rules.py`` refuses a module that writes such a status without it.

Member requests (``tenant_tx``), the AA staff context (``platform_tx``), the feeds and the public contexts never use a
scope: they read the status every time.
"""

from __future__ import annotations

import contextlib
import contextvars
import uuid
import weakref
from collections.abc import Awaitable, Iterator

import asyncpg


class _Memo:
    """The agencies one scope saw ``active``."""

    __slots__ = ("__weakref__", "seen")

    def __init__(self) -> None:
        self.seen: set[uuid.UUID] = set()


_SCOPE: contextvars.ContextVar[_Memo | None] = contextvars.ContextVar("crm_tenant_status_memo", default=None)
_OPEN: weakref.WeakSet[_Memo] = weakref.WeakSet()  # every scope open in this process
# Agencies a writer of this process moved away from "active": never remembered again by this process (a handful of
# rows at most, one per agency locked, offboarded or trashed while the process runs).
_LEFT: set[uuid.UUID] = set()


@contextlib.contextmanager
def memo() -> Iterator[None]:
    """A scope in which job contexts reuse an agency's ``active`` status (one agency's Booking sync run)."""
    m = _Memo()
    _OPEN.add(m)
    token = _SCOPE.set(m)
    try:
        yield
    finally:
        _SCOPE.reset(token)
        _OPEN.discard(m)


async def scoped[T](run: Awaitable[T]) -> T:
    """Await ``run`` (one agency's Booking sync run) inside its own :func:`memo` scope."""
    with memo():
        return await run


async def read(conn: asyncpg.Connection, sql: str, tenant_id: uuid.UUID, *, job: bool) -> str | None:
    """The agency's status through ``sql`` (:data:`app.tenancy.TENANT_SQL`); a job context inside a :func:`memo`
    scope answers ``active`` when the scope already saw it."""
    m = _SCOPE.get() if job else None
    if m is not None and tenant_id in m.seen:
        return "active"
    status = await conn.fetchval(sql, tenant_id)
    if m is not None:
        # Checked after the read: a writer that called leaving() while the read was in flight wins.
        if status == "active" and tenant_id not in _LEFT:
            m.seen.add(tenant_id)
        else:
            m.seen.discard(tenant_id)
    return status


def leaving(tenant_id: uuid.UUID) -> uuid.UUID:
    """A writer of this process moves the agency away from ``active`` (call it in the writer's transaction, before or
    after the UPDATE): every open scope forgets it and no scope of this process remembers it again. Returns the id, so
    a writer may pass it straight to its UPDATE."""
    _LEFT.add(tenant_id)
    for m in list(_OPEN):
        m.seen.discard(tenant_id)
    return tenant_id


def forget() -> None:
    """Back to a fresh process (tests)."""
    _LEFT.clear()
    for m in list(_OPEN):
        m.seen.clear()
