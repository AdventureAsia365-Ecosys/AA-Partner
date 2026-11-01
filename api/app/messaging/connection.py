"""Settings > Channels > SMS and WhatsApp (AA-958; BYO errata S247 §2 "Connections": owner edits, agents and
assistants see status only).

- :func:`validate` checks what the owner typed (Account SID, Auth Token, optional API key, the senders) before any
  network call; the route then proves the credential with the provider (``check``) outside any transaction, like the
  SMTP connect of AA-945, and only then :func:`connect` stores it.
- :func:`connect` stores the credential (``crm.integration_credential`` provider ``twilio``, agency level: the Account
  SID as ``account_ref``, the JSON sealed with ``Vault.encrypt_secret`` under the ``integration`` DEK) and the
  connection (a new 128-bit hook id on first connect; a reconnect keeps it, so the URLs pasted into Twilio stay
  valid), sets it ``connected`` and queues a template sync (``crm_messaging_templates``).
- :func:`disconnect` deletes the credential (the only copy of the token); the connection and its templates leave with
  it (crm-036 cascade). Messages and conversations stay; new SMS / WhatsApp sends answer 409 until a reconnect.
- :func:`view`: health (errata §2 states ``connected | needs_attention | disconnected``, failure class, last check,
  last test), the senders, the webhook URLs to paste into Twilio and the templates; never a secret. An agent or an
  assistant gets the status and which channels can send, nothing else.
"""

from __future__ import annotations

import json
import re
import secrets
import time
import uuid
from typing import Any

from app.errors import Invalid, NotFound, Unavailable
from app.jobs import queue as jobs
from app.jobs.kinds import MESSAGING_TEMPLATES
from app.messaging import state
from app.messaging.providers import placeholders
from app.permissions import Action, may
from app.tenancy import TenantContext
from app.vault import Vault
from app.vault.blind_index import NormalisationError, normalise_phone

PROVIDERS = ("twilio", "sandbox")
_ACCOUNT_SID = re.compile(r"^AC[0-9a-f]{32}$")
_AUTH_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_SANDBOX_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_API_KEY_SID = re.compile(r"^SK[0-9a-f]{32}$")
_API_KEY_SECRET = re.compile(r"^[A-Za-z0-9]{32}$")
_SERVICE_SID = re.compile(r"^MG[0-9a-f]{32}$")
_ALPHA_SENDER = re.compile(r"^[A-Za-z0-9 ]{1,11}$")
TEMPLATE_SYNC_ATTEMPTS = 3

CREDENTIAL_SQL = """
SELECT integration_credential_id FROM crm.integration_credential
 WHERE provider = 'twilio' AND member_id IS NULL AND deleted_at IS NULL
"""
INSERT_CREDENTIAL_SQL = """
INSERT INTO crm.integration_credential (integration_credential_id, provider, member_id, account_ref, secret_enc,
                                        status, last_checked_at, last_ok_at)
VALUES ($1, 'twilio', NULL, $2, $3, 'connected', now(), now())
"""
UPDATE_CREDENTIAL_SQL = """
UPDATE crm.integration_credential
   SET account_ref = $2, secret_enc = $3, status = 'connected', failure_class = NULL, last_checked_at = now(),
       last_ok_at = now()
 WHERE integration_credential_id = $1
"""
CONNECTION_ROW_SQL = """
SELECT messaging_connection_id, hook_id FROM crm.messaging_connection WHERE deleted_at IS NULL
"""
INSERT_CONNECTION_SQL = """
INSERT INTO crm.messaging_connection (messaging_connection_id, provider, credential_id, hook_id, sms_sender,
                                      whatsapp_sender)
VALUES ($1, $2, $3, $4, $5, $6)
"""
UPDATE_CONNECTION_SQL = """
UPDATE crm.messaging_connection SET provider = $2, credential_id = $3, sms_sender = $4, whatsapp_sender = $5
 WHERE messaging_connection_id = $1
"""
DELETE_CREDENTIAL_SQL = "DELETE FROM crm.integration_credential WHERE integration_credential_id = $1"
TEMPLATES_SQL = """
SELECT channel_template_id, content_sid, name, language, category, approval, body, variables, synced_at
  FROM crm.channel_template WHERE deleted_at IS NULL
 ORDER BY approval <> 'approved', name, language
 LIMIT 500
"""


def _sender_phone(value: str | None, field: str) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        return normalise_phone(value.strip())
    except NormalisationError:
        raise Invalid(f"invalid_{field}", "an international number starting with +") from None


def validate(
    *,
    provider: str,
    account_sid: str,
    auth_token: str,
    api_key_sid: str | None,
    api_key_secret: str | None,
    sms_sender: str | None,
    whatsapp_sender: str | None,
    environment_providers: tuple[str, ...],
) -> dict[str, Any]:
    """The checked values: ``{"secret": {...}, "sms_sender", "whatsapp_sender"}``. 422 with a field code on a bad
    value; 409-free (nothing is read)."""
    if provider not in PROVIDERS or provider not in environment_providers:
        raise Invalid("provider_not_available", "this provider is not available here")
    account_sid = account_sid.strip()
    auth_token = auth_token.strip()
    if not _ACCOUNT_SID.match(account_sid):
        raise Invalid("invalid_account_sid", "AC followed by 32 hexadecimal characters")
    token_rule = _AUTH_TOKEN if provider == "twilio" else _SANDBOX_TOKEN
    if not token_rule.match(auth_token):
        raise Invalid("invalid_auth_token", "the 32-character Auth Token of the account")
    secret: dict[str, Any] = {"account_sid": account_sid, "auth_token": auth_token}
    if api_key_sid or api_key_secret:
        if not (api_key_sid and _API_KEY_SID.match(api_key_sid.strip())):
            raise Invalid("invalid_api_key_sid", "SK followed by 32 hexadecimal characters")
        if not (api_key_secret and _API_KEY_SECRET.match(api_key_secret.strip())):
            raise Invalid("invalid_api_key_secret", "the 32-character secret of the API key")
        secret |= {"api_key_sid": api_key_sid.strip(), "api_key_secret": api_key_secret.strip()}
    sms = (sms_sender or "").strip() or None
    if sms is not None and not _SERVICE_SID.match(sms):
        if sms.startswith("+"):
            sms = _sender_phone(sms, "sms_sender")
        elif not (_ALPHA_SENDER.match(sms) and re.search(r"[A-Za-z]", sms)):
            raise Invalid("invalid_sms_sender", "a number with +, a sender name (11 letters) or MG… service")
    whatsapp = _sender_phone(whatsapp_sender, "whatsapp_sender")
    if sms is None and whatsapp is None:
        raise Invalid("sender_required", "an SMS sender, a WhatsApp sender or both")
    return {"secret": secret, "sms_sender": sms, "whatsapp_sender": whatsapp}


async def connect(
    ctx: TenantContext,
    vault: Vault,
    runtime: state.MessagingRuntime,
    *,
    provider: str,
    secret: dict[str, Any],
    sms_sender: str | None,
    whatsapp_sender: str | None,
) -> dict[str, Any]:
    if not runtime.hooks_origin:
        raise Unavailable("messaging_hook_not_configured")
    credential_id = await ctx.conn.fetchval(CREDENTIAL_SQL)
    sealed: bytes
    if credential_id is None:
        credential_id = uuid.uuid4()
        sealed = await vault.encrypt_secret(credential_id, json.dumps(secret))
        await ctx.conn.execute(INSERT_CREDENTIAL_SQL, credential_id, secret["account_sid"], sealed)
    else:
        sealed = await vault.encrypt_secret(credential_id, json.dumps(secret))
        await ctx.conn.execute(UPDATE_CREDENTIAL_SQL, credential_id, secret["account_sid"], sealed)
    row = await ctx.conn.fetchrow(CONNECTION_ROW_SQL)
    if row is None:
        connection_id, hook_id = uuid.uuid4(), secrets.token_hex(16)
        await ctx.conn.execute(
            INSERT_CONNECTION_SQL, connection_id, provider, credential_id, hook_id, sms_sender, whatsapp_sender
        )
    else:
        connection_id, hook_id = row["messaging_connection_id"], row["hook_id"]
        await ctx.conn.execute(
            UPDATE_CONNECTION_SQL, connection_id, provider, credential_id, sms_sender, whatsapp_sender
        )
    await queue_template_sync(ctx, reason="connect")
    ctx.audit("messaging_connect", "allowed", "messaging_connection", str(connection_id), reason=provider)
    return {
        "messaging_connection_id": str(connection_id),
        "status": "connected",
        "hook_url": runtime.hook_url(hook_id),
        "status_callback_url": runtime.status_url(hook_id),
    }


async def queue_template_sync(ctx: TenantContext, *, reason: str) -> None:
    """One ``crm_messaging_templates`` run for this agency (connect, Refresh); ids only in the payload."""
    await jobs.enqueue(
        ctx.conn,
        MESSAGING_TEMPLATES,
        {"tenant_id": str(ctx.tenant_id), "reason_code": reason},
        idempotency_key=f"{MESSAGING_TEMPLATES}:{ctx.tenant_id}:{int(time.time())}",
        max_attempts=TEMPLATE_SYNC_ATTEMPTS,
        created_by="messaging",
    )


async def disconnect(ctx: TenantContext) -> dict[str, Any]:
    row = await state.connection(ctx)
    if row is None:
        raise NotFound("messaging_not_connected")
    await ctx.conn.execute(DELETE_CREDENTIAL_SQL, row["credential_id"])
    ctx.audit("messaging_disconnect", "allowed", "messaging_connection", str(row["messaging_connection_id"]))
    return {"messaging_connection_id": str(row["messaging_connection_id"]), "status": "disconnected"}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _mask(account_sid: str | None) -> str | None:
    return f"{account_sid[:4]}…{account_sid[-4:]}" if account_sid and len(account_sid) > 8 else None


async def templates(ctx: TenantContext) -> list[dict[str, Any]]:
    return [
        {
            "channel_template_id": str(r["channel_template_id"]),
            "content_sid": r["content_sid"],
            "name": r["name"],
            "language": r["language"],
            "category": r["category"],
            "approval": r["approval"],
            "body": r["body"],
            "variables": list(r["variables"] or []) or list(placeholders(r["body"])),
            "synced_at": _iso(r["synced_at"]),
        }
        for r in await ctx.conn.fetch(TEMPLATES_SQL)
    ]


async def view(ctx: TenantContext, runtime: state.MessagingRuntime) -> dict[str, Any]:
    row = await state.connection(ctx)
    channels = {
        "sms": bool(row and row["sms_sender"] and row["credential_status"] != "disconnected"),
        "whatsapp": bool(row and row["whatsapp_sender"] and row["credential_status"] != "disconnected"),
    }
    out: dict[str, Any] = {
        "status": row["credential_status"] if row else "not_set_up",
        "channels": channels,
        "providers": [p for p in PROVIDERS if p in runtime.providers],
        "can_manage": may(ctx.member_role, Action.INTEGRATIONS_MANAGE),
    }
    if row is None or not out["can_manage"]:
        return out
    items = await templates(ctx)
    return out | {
        "connection": {
            "messaging_connection_id": str(row["messaging_connection_id"]),
            "provider": row["provider"],
            "account": _mask(row["account_ref"]),
            "sms_sender": row["sms_sender"],
            "whatsapp_sender": row["whatsapp_sender"],
            "failure_class": row["failure_class"],
            "last_checked_at": _iso(row["last_checked_at"]),
            "last_ok_at": _iso(row["last_ok_at"]),
            "last_test_at": _iso(row["last_test_at"]),
            "last_test_result": row["last_test_result"],
            "last_inbound_at": _iso(row["last_inbound_at"]),
            "last_status_at": _iso(row["last_status_at"]),
            "connected_at": _iso(row["created_at"]),
            "hook_url": runtime.hook_url(row["hook_id"]),
            "status_callback_url": runtime.status_url(row["hook_id"]),
        },
        "templates": items,
    }
