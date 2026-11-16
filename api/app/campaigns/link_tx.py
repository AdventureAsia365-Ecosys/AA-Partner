"""The context of a client's campaign unsubscribe link (AA-968, crm-073): the token of one campaign mail finds its
agency and its recipient row before anything else is read. Kept beside the campaigns (not in :mod:`app.tenancy`, which
is at its size ceiling) and built from the tenancy's own pieces: the public pool gate, the agency status read and the
audit writer, exactly as :func:`app.tenancy.calendar_feed_tx` uses them. Listed in the DB modules of
``tests/unit/test_code_rules.py``; only :mod:`app.campaigns.unsubscribe` may open it.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg

from app import tenant_gate
from app.errors import NotFound
from app.tenancy import TENANT_SQL, SystemContext, _gated, _write_audit

LOOKUP_SQL = """
SELECT campaign_recipient_id, tenant_id FROM crm.campaign_recipient
 WHERE unsubscribe_hash = decode($1, 'hex') AND deleted_at IS NULL
"""
RECIPIENT_SQL = """
SELECT r.campaign_recipient_id, r.campaign_id, r.traveler_id, r.unsubscribed_at
  FROM crm.campaign_recipient r
  JOIN crm.traveler t ON t.traveler_id = r.traveler_id AND t.deleted_at IS NULL
 WHERE r.campaign_recipient_id = $1 AND r.deleted_at IS NULL
"""
# A client's opt-out is honoured while AA has locked the agency too (a billing or support matter); an offboarding or
# offboarded agency's data is being erased.
STATUSES = frozenset({"active", "locked"})
_HASH = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CampaignRecipient:
    campaign_recipient_id: uuid.UUID
    campaign_id: uuid.UUID
    traveler_id: uuid.UUID
    tenant_id: uuid.UUID
    unsubscribed_at: dt.datetime | None


@asynccontextmanager
async def campaign_unsubscribe_tx(
    pool: asyncpg.Pool, token_hash_hex: str
) -> AsyncIterator[tuple[SystemContext, CampaignRecipient]]:
    """The token's SHA-256 (hex) finds one ``crm.campaign_recipient`` row through the SELECT-only policy
    ``campaign_recipient_unsubscribe_lookup`` before any agency is known; then ``app.tenant_id`` = its agency (active
    or locked), the client must still exist, and ``app.actor`` = ``campaign_unsubscribe:<recipient id>`` (audit kind
    ``traveler``). Unknown token, a deleted client, an offboarding agency: 404 ``link_not_found`` (one answer, so tokens
    cannot be probed). Audit semantics as :func:`app.tenancy.tenant_tx` (a denied event survives a refusal)."""
    if not isinstance(token_hash_hex, str) or not _HASH.fullmatch(token_hash_hex):
        raise NotFound("link_not_found")
    pending: Exception | None = None
    async with _gated(pool, tenant_gate.PUBLIC_KEY, busy=tenant_gate.public_busy) as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.campaign_unsubscribe_hash', $1, true)", token_hash_hex)
        row = await conn.fetchrow(LOOKUP_SQL, token_hash_hex)
        await conn.execute("SELECT set_config('app.campaign_unsubscribe_hash', '', true)")
        if row is None:
            raise NotFound("link_not_found")
        tenant_id = row["tenant_id"]
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        status = await conn.fetchval(TENANT_SQL, tenant_id)
        if status not in STATUSES:
            raise NotFound("link_not_found")
        live = await conn.fetchrow(RECIPIENT_SQL, row["campaign_recipient_id"])
        if live is None:
            raise NotFound("link_not_found")
        actor = f"campaign_unsubscribe:{live['campaign_recipient_id']}"
        await conn.execute("SELECT set_config('app.actor', $1, true)", actor)
        found = CampaignRecipient(
            campaign_recipient_id=live["campaign_recipient_id"],
            campaign_id=live["campaign_id"],
            traveler_id=live["traveler_id"],
            tenant_id=tenant_id,
            unsubscribed_at=live["unsubscribed_at"],
        )
        ctx = SystemContext(conn=conn, tenant_id=tenant_id, actor=actor, status=status)
        try:
            async with conn.transaction():  # savepoint: the page's work
                yield ctx, found
        except Exception as exc:
            denied = [e for e in ctx.audit_events if e.outcome == "denied"]
            if not denied:
                raise
            await _write_audit(ctx, denied)
            pending = exc
        else:
            await _write_audit(ctx, ctx.audit_events)
    if pending is not None:
        raise pending
