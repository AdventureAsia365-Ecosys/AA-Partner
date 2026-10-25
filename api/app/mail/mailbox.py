"""Settings > Mailbox (AA-945; errata S247 §2 Connections, "Mailbox" row; design §4.6 own mailbox).

A mailbox is the member's own (``scope = own``, every role: ``OWN_MAILBOX_CONNECT``) or the agency's (``scope =
agency``, ``member_id`` NULL, owner: ``INTEGRATIONS_MANAGE``). One per scope (crm-015 ``channel_account_member_uq``);
a reconnect replaces its credential and may change its kind.

- :func:`connect` stores the credential (``Vault.encrypt_secret``, the ``integration`` DEK; the JSON of
  :mod:`app.mail.providers`) and the mailbox (address under the ``mail`` DEK + the traveller e-mail blind index, so
  the ingest recognises the agency's own copied mail), sets it ``connected`` and puts its held messages back in the
  queue. Another mail credential of the same scope (the kind changed) is deleted.
- :func:`disconnect` sets the mailbox ``disconnected`` and deletes the credential (errata §2: "Disconnect deletes the
  credential row ... keeps the history, audits it"). Messages sent to clients from then on wait in ``held_reconnect``.
- :func:`view` answers what the settings page shows: kind, address, health (status, failure class, last ok, last test),
  never a secret.
- :func:`ingest_address` is the agency's forward-in address (created on first read: ``<slug>-<16 hex>``, 64 random
  bits: the address is the only secret of the forward-in path) and the Gmail forwarding confirmation code the ingest
  captured.
"""

from __future__ import annotations

import json
import re
import secrets
import unicodedata
import uuid
from typing import Any

import asyncpg

from app.errors import Conflict, NotFound
from app.mail import state
from app.settings import Settings
from app.tenancy import TenantContext
from app.vault import Vault

MAIL_PROVIDERS = ("google", "microsoft", "smtp")
KIND_OF = {"google": "gmail", "microsoft": "microsoft", "smtp": "smtp"}
PURPOSE = "mail"

CREDENTIAL_SQL = """
SELECT integration_credential_id FROM crm.integration_credential
 WHERE provider = $1 AND member_id IS NOT DISTINCT FROM $2 AND deleted_at IS NULL
"""
INSERT_CREDENTIAL_SQL = """
INSERT INTO crm.integration_credential (integration_credential_id, provider, member_id, secret_enc, status,
                                        last_checked_at, last_ok_at)
VALUES ($1, $2, $3, $4, 'connected', now(), now())
"""
UPDATE_CREDENTIAL_SQL = """
UPDATE crm.integration_credential
   SET secret_enc = $2, status = 'connected', failure_class = NULL, last_checked_at = now(), last_ok_at = now()
 WHERE integration_credential_id = $1
"""
MAILBOX_SQL = (
    "SELECT channel_account_id, credential_id FROM crm.channel_account WHERE member_id IS NOT DISTINCT FROM $1"
)
INSERT_MAILBOX_SQL = """
INSERT INTO crm.channel_account (channel_account_id, member_id, kind, address_enc, address_bidx, credential_id)
VALUES ($1, $2, $3, $4, $5, $6)
"""
UPDATE_MAILBOX_SQL = """
UPDATE crm.channel_account
   SET kind = $2, address_enc = $3, address_bidx = $4, credential_id = $5, status = 'connected',
       last_token_check_at = now(), deleted_at = NULL, deleted_by = NULL, purge_after = NULL
 WHERE channel_account_id = $1
"""
OTHER_CREDENTIALS_SQL = """
DELETE FROM crm.integration_credential
 WHERE member_id IS NOT DISTINCT FROM $1 AND provider = ANY($2::text[]) AND integration_credential_id <> $3
"""
DISCONNECT_SQL = """
UPDATE crm.channel_account SET status = 'disconnected', last_token_check_at = now()
 WHERE channel_account_id = $1 AND status <> 'disconnected'
"""
DELETE_CREDENTIAL_SQL = "DELETE FROM crm.integration_credential WHERE integration_credential_id = $1"
VIEW_SQL = """
SELECT a.channel_account_id, a.member_id, a.kind, a.address_enc, a.status, a.last_token_check_at,
       ic.status AS credential_status, ic.failure_class, ic.last_ok_at, ic.last_test_at, ic.last_test_result
  FROM crm.channel_account a
  LEFT JOIN crm.integration_credential ic ON ic.integration_credential_id = a.credential_id
 WHERE a.deleted_at IS NULL AND (a.member_id IS NULL OR a.member_id = $1)
 ORDER BY a.member_id NULLS FIRST
"""
HELD_COUNT_SQL = """
SELECT channel_account_id, count(*)::int AS n FROM crm.message
 WHERE status = 'held_reconnect' AND channel_account_id = ANY($1::uuid[])
 GROUP BY channel_account_id
"""
INGEST_SQL = """
SELECT ingest_address_id, local_part, forwarding_confirmation_enc, forwarding_code_at FROM crm.ingest_address
 WHERE active AND deleted_at IS NULL
"""
INSERT_INGEST_SQL = "INSERT INTO crm.ingest_address (local_part) VALUES ($1) RETURNING ingest_address_id"
AGENCY_SQL = "SELECT display_name FROM crm.tenant WHERE tenant_id = $1"


def member_of(ctx: TenantContext, scope: str) -> uuid.UUID | None:
    return ctx.member_id if scope == "own" else None


async def connect(
    ctx: TenantContext, vault: Vault, *, scope: str, provider: str, secret: dict[str, Any]
) -> dict[str, Any]:
    member = member_of(ctx, scope)
    address = str(secret["address"]).strip().lower()
    credential_id = await ctx.conn.fetchval(CREDENTIAL_SQL, provider, member)
    if credential_id is None:
        credential_id = uuid.uuid4()
        sealed = await vault.encrypt_secret(credential_id, json.dumps(secret))
        await ctx.conn.execute(INSERT_CREDENTIAL_SQL, credential_id, provider, member, sealed)
    else:
        await ctx.conn.execute(
            UPDATE_CREDENTIAL_SQL, credential_id, await vault.encrypt_secret(credential_id, json.dumps(secret))
        )
    mailbox = await ctx.conn.fetchrow(MAILBOX_SQL, member)
    channel_account_id = mailbox["channel_account_id"] if mailbox else uuid.uuid4()
    sealed_address = await vault.encrypt("channel_account", "address_enc", channel_account_id, address, purpose=PURPOSE)
    bidx = await vault.email_index("traveler", "email", address)
    try:
        if mailbox is None:
            await ctx.conn.execute(
                INSERT_MAILBOX_SQL, channel_account_id, member, KIND_OF[provider], sealed_address, bidx, credential_id
            )
        else:
            await ctx.conn.execute(
                UPDATE_MAILBOX_SQL, channel_account_id, KIND_OF[provider], sealed_address, bidx, credential_id
            )
    except asyncpg.PostgresError as exc:
        if exc.sqlstate == "CRM30":
            raise Conflict("mailbox_credential_mismatch") from None
        raise
    others = [p for p in MAIL_PROVIDERS if p != provider]
    await ctx.conn.execute(OTHER_CREDENTIALS_SQL, member, others, credential_id)
    requeued = await state.mark_connected(ctx, channel_account_id, credential_id)
    ctx.audit("mailbox_connect", "allowed", "channel_account", str(channel_account_id), reason=provider)
    return {"channel_account_id": str(channel_account_id), "status": "connected", "requeued_count": requeued}


async def disconnect(ctx: TenantContext, scope: str) -> dict[str, Any]:
    mailbox = await ctx.conn.fetchrow(MAILBOX_SQL, member_of(ctx, scope))
    if mailbox is None:
        raise NotFound("mailbox_not_found")
    await ctx.conn.execute(DISCONNECT_SQL, mailbox["channel_account_id"])
    if mailbox["credential_id"] is not None:
        await ctx.conn.execute(DELETE_CREDENTIAL_SQL, mailbox["credential_id"])
    ctx.audit("mailbox_disconnect", "allowed", "channel_account", str(mailbox["channel_account_id"]))
    return {"channel_account_id": str(mailbox["channel_account_id"]), "status": "disconnected"}


async def mailbox_id(ctx: TenantContext, scope: str) -> uuid.UUID:
    mailbox = await ctx.conn.fetchrow(MAILBOX_SQL, member_of(ctx, scope))
    if mailbox is None or mailbox["credential_id"] is None:
        raise NotFound("mailbox_not_found")
    return mailbox["channel_account_id"]


async def view(ctx: TenantContext, vault: Vault, settings: Settings) -> dict[str, Any]:
    rows = await ctx.conn.fetch(VIEW_SQL, ctx.member_id)
    held = {
        r["channel_account_id"]: r["n"]
        for r in await ctx.conn.fetch(HELD_COUNT_SQL, [r["channel_account_id"] for r in rows])
    }
    items = []
    for r in rows:
        items.append(
            {
                "channel_account_id": str(r["channel_account_id"]),
                "scope": "agency" if r["member_id"] is None else "own",
                "kind": r["kind"],
                "address": await vault.decrypt(
                    "channel_account", "address_enc", r["channel_account_id"], r["address_enc"], purpose=PURPOSE
                ),
                "status": r["status"],
                "failure_class": r["failure_class"],
                "last_checked_at": _iso(r["last_token_check_at"]),
                "last_ok_at": _iso(r["last_ok_at"]),
                "last_test_at": _iso(r["last_test_at"]),
                "last_test_result": r["last_test_result"],
                "held_count": held.get(r["channel_account_id"], 0),
            }
        )
    return {
        "mailboxes": items,
        "providers": {p: settings.mail_oauth_client(p) is not None for p in ("google", "microsoft")} | {"smtp": True},
        "billing_note": "billed_by_your_mail_provider",
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def slug(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "d"))
    ascii_name = "".join(c for c in folded if not unicodedata.combining(c)).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:30].strip("-")
    return value or "agency"


async def ingest_address(ctx: TenantContext, vault: Vault, settings: Settings) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(INGEST_SQL)
    if row is None:
        base = slug(await ctx.conn.fetchval(AGENCY_SQL, ctx.tenant_id) or "agency")
        for _ in range(3):
            local_part = f"{base}-{secrets.token_hex(8)}"  # 64 random bits: the address is the only secret
            try:
                async with ctx.conn.transaction():
                    await ctx.conn.fetchval(INSERT_INGEST_SQL, local_part)
                break
            except asyncpg.UniqueViolationError:
                continue
        row = await ctx.conn.fetchrow(INGEST_SQL)
        if row is None:
            raise Conflict("ingest_address_unavailable", "try again")
        ctx.audit("ingest_address_create", "allowed", "ingest_address", str(row["ingest_address_id"]))
    code = None
    if row["forwarding_confirmation_enc"] is not None:
        code = await vault.decrypt(
            "ingest_address",
            "forwarding_confirmation_enc",
            row["ingest_address_id"],
            row["forwarding_confirmation_enc"],
            purpose=PURPOSE,
        )
    domain = settings.crm_ingest_domain
    return {
        "ingest_address_id": str(row["ingest_address_id"]),
        "address": f"{row['local_part']}@{domain}" if domain else None,
        "forwarding_code": code,
        "forwarding_code_at": _iso(row["forwarding_code_at"]),
    }
