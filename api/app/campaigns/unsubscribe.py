"""A client's unsubscribe from a campaign mail (AA-968, card CRM-36; design v3 C-11 "Preferences / unsubscribe";
RFC 8058 one-click). No sign-in: the 43-character token of the mail is the credential (only its SHA-256 is stored,
``crm.campaign_recipient.unsubscribe_hash``); the work runs in :func:`app.campaigns.link_tx.campaign_unsubscribe_tx`.

- :func:`state`: the page's facts: the agency's name and whether the client is subscribed now.
- :func:`unsubscribe` (the page's button, and the mail program's one-click POST): one ``crm.consent`` row (purpose
  ``marketing``, value false, source ``portal``: the client's own act on a CRM page), the F2 flag
  ``traveler_profile.marketing_ok`` false, the address on the agency's ``crm.campaign_suppression`` list (reason
  ``unsubscribed``: card R "an unsubscribed contact re-imported stays suppressed"), the recipient row's
  ``unsubscribed_at``. Again = nothing new (idempotent).
- :func:`resubscribe` (the page's "Subscribe again"): the client's own yes: the suppression row goes and a consent
  row with value true is written. This is the only path that lifts a suppression.

The answers never carry an id, a name or an address of the client.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import asyncpg

from app.campaigns.link_tx import campaign_unsubscribe_tx
from app.tenancy import SystemContext
from app.vault import KeyProvider, Vault

TRAVELER_SQL = "SELECT email_bidx, profile_token_enc FROM crm.traveler WHERE traveler_id = $1"
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"
CONSENT_SQL = """
SELECT c.value FROM crm.consent c WHERE c.traveler_id = $1 AND c.purpose = 'marketing'
 ORDER BY c.at DESC, c.created_at DESC, c.consent_id DESC
 LIMIT 1
"""
SUPPRESSED_SQL = "SELECT 1 FROM crm.campaign_suppression WHERE email_bidx = $1 AND deleted_at IS NULL"
INSERT_CONSENT_SQL = """
INSERT INTO crm.consent (traveler_id, purpose, value, source) VALUES ($1, 'marketing', $2, 'portal')
"""
PROFILE_SQL = "UPDATE crm.traveler_profile SET marketing_ok = $2 WHERE traveler_token = $1"
SUPPRESS_SQL = """
INSERT INTO crm.campaign_suppression (email_bidx, reason, campaign_id) VALUES ($1, 'unsubscribed', $2)
ON CONFLICT (tenant_id, email_bidx) DO NOTHING
"""
LIFT_SQL = "DELETE FROM crm.campaign_suppression WHERE email_bidx = $1"
MARK_SQL = """
UPDATE crm.campaign_recipient SET unsubscribed_at = coalesce(unsubscribed_at, now()) WHERE campaign_recipient_id = $1
"""


def token_hash_hex(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _subscribed(ctx: SystemContext, traveler_id: uuid.UUID, email_bidx: bytes | None) -> bool:
    if email_bidx is not None and await ctx.conn.fetchval(SUPPRESSED_SQL, email_bidx):
        return False
    return await ctx.conn.fetchval(CONSENT_SQL, traveler_id) is True


async def _flag(ctx: SystemContext, keys: KeyProvider, traveler_id: uuid.UUID, blob: bytes | None, value: bool) -> None:
    """The F2 flag follows the latest marketing row (the profile is reached only through the sealed pointer)."""
    if blob is None:
        return
    token = await Vault(keys, ctx).decrypt("traveler", "profile_token_enc", traveler_id, blob)
    await ctx.conn.execute(PROFILE_SQL, uuid.UUID(token), value)


async def state(pool: asyncpg.Pool, token: str) -> dict[str, Any]:
    async with campaign_unsubscribe_tx(pool, token_hash_hex(token)) as (ctx, found):
        row = await ctx.conn.fetchrow(TRAVELER_SQL, found.traveler_id)
        agency = await ctx.conn.fetchval(AGENCY_SQL, ctx.tenant_id)
        subscribed = await _subscribed(ctx, found.traveler_id, row["email_bidx"])
    return {"agency": agency or "", "subscribed": subscribed}


async def unsubscribe(pool: asyncpg.Pool, keys: KeyProvider, token: str) -> dict[str, Any]:
    async with campaign_unsubscribe_tx(pool, token_hash_hex(token)) as (ctx, found):
        row = await ctx.conn.fetchrow(TRAVELER_SQL, found.traveler_id)
        agency = await ctx.conn.fetchval(AGENCY_SQL, ctx.tenant_id)
        changed = await _subscribed(ctx, found.traveler_id, row["email_bidx"]) or found.unsubscribed_at is None
        if await ctx.conn.fetchval(CONSENT_SQL, found.traveler_id) is not False:
            await ctx.conn.execute(INSERT_CONSENT_SQL, found.traveler_id, False)
            await _flag(ctx, keys, found.traveler_id, row["profile_token_enc"], False)
        if row["email_bidx"] is not None:
            await ctx.conn.execute(SUPPRESS_SQL, row["email_bidx"], found.campaign_id)
        await ctx.conn.execute(MARK_SQL, found.campaign_recipient_id)
        if changed:
            ctx.audit("campaign_unsubscribe", "allowed", "campaign", str(found.campaign_id))
    return {"agency": agency or "", "subscribed": False}


async def resubscribe(pool: asyncpg.Pool, keys: KeyProvider, token: str) -> dict[str, Any]:
    async with campaign_unsubscribe_tx(pool, token_hash_hex(token)) as (ctx, found):
        row = await ctx.conn.fetchrow(TRAVELER_SQL, found.traveler_id)
        agency = await ctx.conn.fetchval(AGENCY_SQL, ctx.tenant_id)
        if not await _subscribed(ctx, found.traveler_id, row["email_bidx"]):
            if row["email_bidx"] is not None:
                await ctx.conn.execute(LIFT_SQL, row["email_bidx"])
            if await ctx.conn.fetchval(CONSENT_SQL, found.traveler_id) is not True:
                await ctx.conn.execute(INSERT_CONSENT_SQL, found.traveler_id, True)
                await _flag(ctx, keys, found.traveler_id, row["profile_token_enc"], True)
            ctx.audit("campaign_resubscribe", "allowed", "campaign", str(found.campaign_id))
    return {"agency": agency or "", "subscribed": True}
