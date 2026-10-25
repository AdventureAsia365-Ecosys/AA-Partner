"""OAuth connect of a Gmail or Microsoft 365 mailbox (AA-945; lane §6.5; errata S247 §2 "OAuth consent").

Authorization code + PKCE (S256) through AA's own OAuth clients (``MAIL_OAUTH``), scopes ``gmail.send`` /
``Mail.Send`` + ``offline_access`` (plus ``openid email`` to learn the mailbox address from the ID token). The
**state** is the only thing the browser carries: a JSON of (provider, scope, member, nonce, expiry, PKCE verifier)
sealed with the agency's ``mail`` DEK, its AAD bound to (agency, ``oauth_state``, ``state``, member). So a state
cannot be completed in another agency or by another member (it does not open: 403 ``decrypt_denied``, on record), is
refused after :data:`STATE_TTL_S` (409 ``oauth_state_expired``), and the verifier never leaves the server unsealed.
Replaying a state is harmless: the provider accepts each code once. Nothing is stored server side between the two
steps.

The redirect URI is ``<MAIL_OAUTH_REDIRECT_BASE>/connect/mail/callback`` (the CRM web's route, which posts the code and
state back through the BFF to ``POST /crm/v1/mailbox/oauth/complete``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from app.errors import Conflict, Invalid, Unavailable
from app.mail.providers import ProviderError
from app.mail.providers.oauth_mail import ENDPOINTS, OAuthMailProvider
from app.settings import Settings
from app.tenancy import TenantContext
from app.vault import Vault

STATE_TTL_S = 600
CALLBACK_PATH = "/connect/mail/callback"
SCOPES = ("own", "agency")
PURPOSE = "mail"


@dataclass(frozen=True)
class Completed:
    provider: str
    scope: str
    secret: dict[str, Any]  # {refresh_token, address}


def redirect_uri(settings: Settings) -> str:
    if not settings.mail_oauth_redirect_base:
        raise Unavailable("mail_oauth_not_configured", "MAIL_OAUTH_REDIRECT_BASE is not set")
    return settings.mail_oauth_redirect_base + CALLBACK_PATH


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise Invalid("oauth_state_invalid") from None


async def start(ctx: TenantContext, vault: Vault, settings: Settings, provider: str, scope: str) -> dict[str, Any]:
    if provider not in ENDPOINTS or scope not in SCOPES:
        raise Invalid("invalid_provider", "google or microsoft; scope own or agency")
    client = settings.mail_oauth_client(provider)
    if client is None:
        raise Unavailable("mail_oauth_not_configured", f"the {provider} OAuth client is not configured")
    endpoints = ENDPOINTS[provider]
    verifier = secrets.token_urlsafe(48)
    payload = {
        "p": provider,
        "s": scope,
        "m": str(ctx.member_id),
        "n": secrets.token_urlsafe(12),
        "e": int(time.time()) + STATE_TTL_S,
        "v": verifier,
    }
    sealed = await vault.encrypt("oauth_state", "state", ctx.member_id, json.dumps(payload), purpose=PURPOSE)
    params = {
        "client_id": client[0],
        "redirect_uri": redirect_uri(settings),
        "response_type": "code",
        "scope": " ".join(endpoints.scopes),
        "state": _b64(sealed),
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    if provider == "google":
        params |= {"access_type": "offline", "prompt": "consent"}
    else:
        params |= {"response_mode": "query", "prompt": "select_account"}
    ctx.audit("mailbox_connect_start", "allowed", "channel_account", reason=provider)
    return {"authorize_url": f"{endpoints.authorize_url}?{urllib.parse.urlencode(params)}", "expires_in": STATE_TTL_S}


async def complete(
    ctx: TenantContext, vault: Vault, settings: Settings, providers: dict[str, Any], code: str, state: str
) -> Completed:
    if not code or len(code) > 4096 or not state or len(state) > 4096:
        raise Invalid("oauth_state_invalid")
    opened = await vault.decrypt("oauth_state", "state", ctx.member_id, _unb64(state), purpose=PURPOSE)
    try:
        payload = json.loads(opened)
    except ValueError:
        raise Invalid("oauth_state_invalid") from None
    if payload.get("m") != str(ctx.member_id) or payload.get("p") not in ENDPOINTS or payload.get("s") not in SCOPES:
        raise Invalid("oauth_state_invalid")
    if int(payload.get("e", 0)) < int(time.time()):
        raise Conflict("oauth_state_expired", "start the connection again")
    provider: OAuthMailProvider = providers[ENDPOINTS[payload["p"]].kind]
    try:
        secret = await provider.exchange_code(code, str(payload["v"]), redirect_uri(settings))
    except ProviderError as exc:
        raise Conflict(exc.code, "the provider did not accept the authorization") from None
    return Completed(provider=payload["p"], scope=payload["s"], secret=secret)
