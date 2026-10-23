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

The transaction commits when the block exits normally and rolls back on any exception, which also ends every
``SET LOCAL``. ``tests/unit/test_db_access_lint.py`` fails when request code calls the pool another way.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg

from app.auth.route_guard import Caller
from app.errors import Forbidden

READER_ROLES = frozenset({"crm_ai_reader", "crm_insights", "crm_support_reader", "crm_eraser"})
MEMBER_ROLES = frozenset({"owner", "agent", "assistant"})

TENANT_SQL = "SELECT status FROM crm.tenant WHERE tenant_id = $1 AND deleted_at IS NULL"
MEMBER_SQL = """
SELECT member_id, role FROM crm.member
 WHERE tenant_id = $1 AND cognito_iss = $2 AND cognito_sub = $3 AND status = 'active' AND deleted_at IS NULL
"""


@dataclass(frozen=True)
class TenantContext:
    """What a route may use inside :func:`tenant_tx`: the connection (inside the transaction) and who is acting."""

    conn: asyncpg.Connection
    tenant_id: uuid.UUID
    member_id: uuid.UUID
    member_role: str


@asynccontextmanager
async def tenant_tx(
    pool: asyncpg.Pool, caller: Caller, tenant_id: uuid.UUID, *, reader_role: str | None = None
) -> AsyncIterator[TenantContext]:
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_id must be a UUID")
    if reader_role is not None and reader_role not in READER_ROLES:
        raise ValueError(f"not a CRM reader role: {reader_role!r}")
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
        yield TenantContext(conn=conn, tenant_id=tenant_id, member_id=member["member_id"], member_role=role)
