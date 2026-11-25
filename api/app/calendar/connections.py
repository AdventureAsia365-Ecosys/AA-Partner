"""Account > Integrations > Calendar: a member's own Google / Microsoft 365 calendar connections (AA-1011 part 3;
crm-089 ``crm.calendar_connection``).

- :func:`view`: the member's connections (provider, scope, status, the account address, the last push: when, how many
  CRM events, the failure class), never a token; which providers AA has a client for.
- :func:`connect`: after the OAuth completion (:func:`app.calendar.oauth_sync.complete`): the refresh token goes into
  the member's own ``crm.integration_credential`` row (provider ``google_calendar`` / ``microsoft_calendar``, the
  integration DEK, :func:`app.calendar.oauth_sync.seal_secret`), the connection row is created or (a reconnect of an
  active one: a new consent, maybe another account) updated, and one push of this connection is queued at once. A
  connection whose disconnect is still being cleaned up answers 409 ``calendar_disconnect_pending`` (seconds).
- :func:`disconnect`: the member's own connection (an owner: anyone's): status ``revoked`` and one clean-up run queued
  (:mod:`app.calendar.push`: it deletes the pushed events, revokes the grant and deletes the credential, which takes
  the connection row with it).

One audit event per change (``calendar_connect`` / ``calendar_disconnect``, the provider as reason; no address, no
token). Every function takes the open context and never opens one.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.calendar import oauth_sync, push
from app.errors import Conflict, Forbidden, NotFound
from app.mail.oauth import PURPOSE as MAIL_PURPOSE
from app.tenancy import TenantContext
from app.vault import Vault

COLUMNS = """c.calendar_connection_id, c.member_id, c.provider, c.scope, c.status, c.address_enc, c.failure_class,
       c.last_sync_at, c.last_ok_at, c.event_count, c.revoked_at, c.created_at"""
MINE_SQL = f"SELECT {COLUMNS} FROM crm.calendar_connection c WHERE c.member_id = $1 ORDER BY c.provider"  # noqa: S608
BY_ID_SQL = f"SELECT {COLUMNS} FROM crm.calendar_connection c WHERE c.calendar_connection_id = $1"  # noqa: S608
EXISTING_SQL = """
SELECT calendar_connection_id, status, credential_id FROM crm.calendar_connection
 WHERE member_id = $1 AND provider = $2
   FOR UPDATE
"""
CREDENTIAL_SQL = """
SELECT integration_credential_id FROM crm.integration_credential
 WHERE provider = $1 AND member_id = $2
   FOR UPDATE
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
INSERT_SQL = """
INSERT INTO crm.calendar_connection (calendar_connection_id, member_id, provider, scope, credential_id, address_enc)
VALUES ($1, $2, $3, $4, $5, $6)
"""
RECONNECT_SQL = """
UPDATE crm.calendar_connection SET scope = $2, address_enc = $3, failure_class = NULL
 WHERE calendar_connection_id = $1
"""
REVOKE_SQL = """
UPDATE crm.calendar_connection SET status = 'revoked', revoked_at = now(), revoked_by = $2
 WHERE calendar_connection_id = $1 AND status = 'active'
"""
LOCK_SQL = "SELECT member_id, status FROM crm.calendar_connection WHERE calendar_connection_id = $1 FOR UPDATE"


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def _view(vault: Vault, row: Any) -> dict[str, Any]:
    address = await vault.decrypt(
        "calendar_connection", "address_enc", row["calendar_connection_id"], row["address_enc"],
        purpose=MAIL_PURPOSE,
    )  # fmt: skip
    return {
        "calendar_connection_id": str(row["calendar_connection_id"]),
        "member_id": str(row["member_id"]),
        "provider": row["provider"],
        "scope": row["scope"],
        "status": row["status"],
        "address": address,
        "failure_class": row["failure_class"],
        "last_sync_at": _iso(row["last_sync_at"]),
        "last_ok_at": _iso(row["last_ok_at"]),
        "event_count": row["event_count"],
        "revoked_at": _iso(row["revoked_at"]),
        "created_at": _iso(row["created_at"]),
    }


async def view(ctx: TenantContext, vault: Vault, clients: dict[str, Any]) -> dict[str, Any]:
    rows = await ctx.conn.fetch(MINE_SQL, ctx.member_id)
    return {
        "connections": [await _view(vault, r) for r in rows],
        "providers": {p: bool(getattr(clients.get(p), "configured", False)) for p in oauth_sync.PROVIDERS},
        "kinds": list(push.PUSH_KINDS),
    }


async def _credential(ctx: TenantContext, vault: Vault, provider: str, secret: dict[str, Any]) -> uuid.UUID:
    """The member's credential row of this calendar provider, created or given the new token."""
    kind = oauth_sync.CREDENTIAL_PROVIDER[provider]
    credential_id = await ctx.conn.fetchval(CREDENTIAL_SQL, kind, ctx.member_id)
    stored = {"refresh_token": secret["refresh_token"], "address": secret["address"]}
    if credential_id is None:
        credential_id = uuid.uuid4()
        sealed = await oauth_sync.seal_secret(vault, credential_id, stored)
        await ctx.conn.execute(INSERT_CREDENTIAL_SQL, credential_id, kind, ctx.member_id, sealed)
    else:
        await ctx.conn.execute(
            UPDATE_CREDENTIAL_SQL, credential_id, await oauth_sync.seal_secret(vault, credential_id, stored)
        )
    return credential_id


async def connect(
    ctx: TenantContext, vault: Vault, *, provider: str, scope: str, secret: dict[str, Any]
) -> dict[str, Any]:
    existing = await ctx.conn.fetchrow(EXISTING_SQL, ctx.member_id, provider)
    if existing is not None and existing["status"] != "active":
        raise Conflict("calendar_disconnect_pending", "the last disconnect is still being cleaned up; try again")
    address = str(secret["address"]).strip().lower()
    credential_id = await _credential(ctx, vault, provider, secret)
    connection_id = existing["calendar_connection_id"] if existing is not None else uuid.uuid4()
    sealed_address = await vault.encrypt(
        "calendar_connection", "address_enc", connection_id, address, purpose=MAIL_PURPOSE
    )
    if existing is None:
        await ctx.conn.execute(INSERT_SQL, connection_id, ctx.member_id, provider, scope, credential_id, sealed_address)
    else:
        await ctx.conn.execute(RECONNECT_SQL, connection_id, scope, sealed_address)
    await push.enqueue_connection(ctx.conn, ctx.tenant_id, connection_id, "connect")
    ctx.audit("calendar_connect", "allowed", "calendar_connection", str(connection_id), reason=provider)
    row = await ctx.conn.fetchrow(BY_ID_SQL, connection_id)
    return {"connection": await _view(vault, row)}


async def disconnect(ctx: TenantContext, vault: Vault, connection_id: uuid.UUID) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(LOCK_SQL, connection_id)
    if row is None:
        raise NotFound("calendar_connection_not_found")
    if row["member_id"] != ctx.member_id and ctx.member_role != "owner":
        ctx.audit("calendar_disconnect", "denied", "calendar_connection", str(connection_id), reason="not_own")
        raise Forbidden("not_own_connection", "only the member or an owner disconnects a calendar")
    if row["status"] == "active":
        await ctx.conn.execute(REVOKE_SQL, connection_id, f"member:{ctx.member_id}")
        await push.enqueue_connection(ctx.conn, ctx.tenant_id, connection_id, "disconnect")
        ctx.audit("calendar_disconnect", "allowed", "calendar_connection", str(connection_id), reason="revoked")
    return {"connection": await _view(vault, await ctx.conn.fetchrow(BY_ID_SQL, connection_id))}
