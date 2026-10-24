"""``tenant_tx()``: the only way request code reaches tenant data (design v3 §4.6 "Request context"; ADR 0010 d3).

One transaction on one pooled connection:

1. ``SET LOCAL`` ``app.tenant_id`` (``set_config(..., true)``): from here on every ``crm`` tenant table shows this
   agency's rows only (FORCE RLS policy ``tenant_isolation``, crm-002); before it, none at all;
2. the agency must exist and be ``active`` (a locked agency gets 403 ``tenant_locked``);
3. the membership check: an ``active``, not trashed ``crm.member`` row for the caller's token identity
   ``(iss, sub)`` — never an e-mail lookup (ADR 0006b decision 5). None -> 403 ``not_a_member``;
4. ``SET LOCAL`` ``app.member_role`` (owner / agent / assistant, read by the margin RLS of later cards) and
   ``app.actor`` (``member:<id>``, the ``created_by`` default);
5. optionally ``SET LOCAL ROLE`` to one of the four NOLOGIN readers (``crm_app`` is NOINHERIT and reaches them only
   this way, Infra rl-004). The name comes from :data:`READER_ROLES`, never from input.

The route's work then runs in a savepoint. Audit events it queues with :meth:`TenantContext.audit` are written when
the block ends (AA-934, ``crm.audit_event``):

- normal exit: every queued event is inserted and the transaction commits;
- an exception: the work is rolled back to the savepoint; if ``denied`` events were queued (a refused decrypt, a
  refused access), only those are inserted and the transaction **commits**, then the exception goes on — a denial is
  on record even when it ends the request; with no denial the whole transaction rolls back as before.

Events are inserted as ``crm_app`` (``SET LOCAL ROLE NONE`` first when a reader role was set: the readers have no
privilege on the audit table). Rolling back or committing ends every ``SET LOCAL``. The DB access lint in
``tests/unit/test_code_rules.py`` fails when request code reaches the database any other way than ``ctx.conn`` inside
``async with tenant_tx(...) as ctx``.

Two more entry points, each one transaction with its own narrow context (AA-935):

- :func:`caller_memberships` lists the agencies the caller belongs to (the web needs a tenant id before it can open
  ``tenant_tx()``). It sets ``app.caller_iss`` / ``app.caller_sub`` from the verified token, which the crm-004
  SELECT-only policies ``member_self_lookup`` / ``tenant_member_lookup`` read; it never sets ``app.tenant_id``.
- :func:`provisioning_tx` is the platform context that creates an agency and its first members (the Dev seeder
  ``app.seed`` now, the L1 console ``/crm/ops/v1`` with CRM-13a): ``app.tenant_id`` = the new agency, ``app.actor`` =
  the platform actor, no membership check (there is no member yet). Request code of ``/crm/v1`` never uses it (a unit
  test checks the importers).

Both commit when the block exits normally and roll back on any exception (no audit queue).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import asyncpg

from app.audit import INSERT_SQL, AuditEvent, current_request_id
from app.auth.route_guard import Caller
from app.errors import Forbidden

READER_ROLES = frozenset({"crm_ai_reader", "crm_insights", "crm_support_reader", "crm_eraser"})
MEMBER_ROLES = frozenset({"owner", "agent", "assistant"})

TENANT_SQL = "SELECT status FROM crm.tenant WHERE tenant_id = $1 AND deleted_at IS NULL"
MEMBER_SQL = """
SELECT member_id, role FROM crm.member
 WHERE tenant_id = $1 AND cognito_iss = $2 AND cognito_sub = $3 AND status = 'active' AND deleted_at IS NULL
"""


MEMBERSHIPS_SQL = """
SELECT t.tenant_id, t.display_name, t.default_locale, t.status AS tenant_status, m.member_id, m.role
  FROM crm.member m
  JOIN crm.tenant t ON t.tenant_id = m.tenant_id
 WHERE m.cognito_iss = $1 AND m.cognito_sub = $2 AND m.status = 'active' AND m.deleted_at IS NULL
   AND t.deleted_at IS NULL
 ORDER BY t.display_name, t.tenant_id
 LIMIT $3
"""
# A person belongs to a handful of agencies; the cap keeps one response bounded whatever the data says.
MAX_MEMBERSHIPS = 50
PROVISION_ACTOR_PREFIXES = ("platform:", "seed:")


@dataclass(frozen=True)
class Membership:
    """One agency the caller belongs to (ids, the agency label and role; no personal data)."""

    tenant_id: uuid.UUID
    display_name: str
    default_locale: str
    tenant_status: str
    member_id: uuid.UUID
    member_role: str


@dataclass(frozen=True)
class ProvisioningContext:
    """What platform provisioning may use inside :func:`provisioning_tx`."""

    conn: asyncpg.Connection
    tenant_id: uuid.UUID
    actor: str


@dataclass(frozen=True)
class TenantContext:
    """What a route may use inside :func:`tenant_tx`: the connection (inside the transaction) and who is acting."""

    conn: asyncpg.Connection
    tenant_id: uuid.UUID
    member_id: uuid.UUID
    member_role: str
    reader_role: str | None = None
    audit_events: list[AuditEvent] = field(default_factory=list, repr=False)
    # Field encryption (app.vault): DEKs generated in this transaction, never in the process cache until committed.
    dek_memo: dict[tuple[str, int], bytes] = field(default_factory=dict, repr=False)
    dek_current: dict[str, int] = field(default_factory=dict, repr=False)

    def audit(
        self,
        action: str,
        outcome: str,
        object_kind: str,
        object_id: str | None = None,
        *,
        fields: tuple[str, ...] = (),
        reason: str | None = None,
    ) -> AuditEvent:
        """Queue an ``audit_event`` for this member (written when the ``tenant_tx()`` block ends)."""
        event = AuditEvent(
            actor_kind="user",
            actor_ref=f"member:{self.member_id}",
            action=action,
            outcome=outcome,
            object_kind=object_kind,
            object_id=object_id,
            fields=tuple(fields),
            reason=reason,
            request_id=current_request_id(),
        )
        self.audit_events.append(event)
        return event


@asynccontextmanager
async def tenant_tx(
    pool: asyncpg.Pool, caller: Caller, tenant_id: uuid.UUID, *, reader_role: str | None = None
) -> AsyncIterator[TenantContext]:
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_id must be a UUID")
    if reader_role is not None and reader_role not in READER_ROLES:
        raise ValueError(f"not a CRM reader role: {reader_role!r}")
    pending: Exception | None = None
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        status = await conn.fetchval(TENANT_SQL, tenant_id)
        if status is None:
            # Unknown, trashed or another agency's id: the same answer, so ids cannot be probed.
            raise Forbidden("not_a_member")
        if status != "active":
            raise Forbidden("tenant_locked")
        member = await conn.fetchrow(MEMBER_SQL, tenant_id, caller.iss, caller.sub)
        if member is None:
            raise Forbidden("not_a_member")
        role = member["role"]
        if role not in MEMBER_ROLES:  # the CHECK makes this impossible; refuse rather than trust it
            raise Forbidden("not_a_member")
        await conn.execute("SELECT set_config('app.member_role', $1, true)", role)
        await conn.execute("SELECT set_config('app.actor', $1, true)", f"member:{member['member_id']}")
        if reader_role is not None:
            # A role name cannot be a bind parameter; READER_ROLES is a fixed allow-list checked above.
            await conn.execute(f"SET LOCAL ROLE {reader_role}")
        ctx = TenantContext(
            conn=conn, tenant_id=tenant_id, member_id=member["member_id"], member_role=role, reader_role=reader_role
        )
        try:
            async with conn.transaction():  # savepoint: the route's work
                yield ctx
        except Exception as exc:
            denied = [e for e in ctx.audit_events if e.outcome == "denied"]
            if not denied:
                raise
            await _write_audit(ctx, denied)
            pending = exc  # commit the denials, then let the exception go on
        else:
            await _write_audit(ctx, ctx.audit_events)
    if pending is not None:
        raise pending


async def _write_audit(ctx: TenantContext, events: list[AuditEvent]) -> None:
    if not events:
        return
    if ctx.reader_role is not None:
        await ctx.conn.execute("SET LOCAL ROLE NONE")
    await ctx.conn.executemany(INSERT_SQL, [e.row() for e in events])


async def caller_memberships(pool: asyncpg.Pool, caller: Caller) -> list[Membership]:
    """The caller's active memberships in agencies that are not trashed (locked ones included, with their status)."""
    if not caller.iss or not caller.sub:
        raise Forbidden("not_a_member")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.caller_iss', $1, true)", caller.iss)
        await conn.execute("SELECT set_config('app.caller_sub', $1, true)", caller.sub)
        rows = await conn.fetch(MEMBERSHIPS_SQL, caller.iss, caller.sub, MAX_MEMBERSHIPS)
    return [
        Membership(
            tenant_id=r["tenant_id"],
            display_name=r["display_name"],
            default_locale=r["default_locale"],
            tenant_status=r["tenant_status"],
            member_id=r["member_id"],
            member_role=r["role"],
        )
        for r in rows
        if r["role"] in MEMBER_ROLES
    ]


@asynccontextmanager
async def provisioning_tx(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, *, actor: str
) -> AsyncIterator[ProvisioningContext]:
    """Platform context for creating one agency and its first members (no membership check: none exists yet)."""
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_id must be a UUID")
    if not actor.startswith(PROVISION_ACTOR_PREFIXES) or len(actor) > 120:
        raise ValueError(f"provisioning actor must start with one of {PROVISION_ACTOR_PREFIXES}")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        yield ProvisioningContext(conn=conn, tenant_id=tenant_id, actor=actor)
