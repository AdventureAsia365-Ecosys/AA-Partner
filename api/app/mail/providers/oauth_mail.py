"""Gmail (``gmail.send``) and Microsoft 365 (Graph ``Mail.Send`` + ``offline_access``) send-as through AA's own OAuth
clients (AA-945; lane §6.5; errata S247 §1). One class, two endpoint sets: each send refreshes an access token from the
stored refresh token, then posts the RFC 5322 message we built:

- Gmail ``users/me/messages/send`` with ``{"raw": base64url}``: Google keeps our Message-ID and returns its id;
- Graph ``me/sendMail`` with the MIME message base64 (``Content-Type: text/plain``): 202, no id. The trip token in the
  subject threads the client's reply either way (card E).

Refresh-token rotation: Microsoft returns a new refresh token on every refresh (each one valid for at most 90 days) and
Google sometimes does. :meth:`OAuthMailProvider.access_token` hands the new one back, and ``send`` / ``check`` return
the credential to store (``SendResult.rotated_secret``; :func:`app.mail.sender.store_rotated`), so a mailbox in use
never ages into ``invalid_grant``.

Failures: ``invalid_grant`` on refresh, or 401 / 403 on send -> :class:`AuthRevoked` (``auth_revoked`` /
``permission_missing``); 400 / 413 on send -> :class:`Rejected`; 429, 5xx, network -> :class:`Transient`. AA's own
client being refused (``invalid_client``) is Transient too: it is AA's configuration, not the agency's mailbox, so no
mailbox is marked disconnected for it.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from app.mail.providers import AuthRevoked, Rejected, SendResult, Transient
from app.mail.providers.http import HttpClient


@dataclass(frozen=True)
class OAuthEndpoints:
    provider: str  # integration_credential.provider: google | microsoft
    kind: str  # channel_account.kind: gmail | microsoft
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]


GOOGLE = OAuthEndpoints(
    provider="google",
    kind="gmail",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",  # noqa: S106 — a URL, not a secret
    scopes=("openid", "email", "https://www.googleapis.com/auth/gmail.send"),
)
MICROSOFT = OAuthEndpoints(
    provider="microsoft",
    kind="microsoft",
    authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",  # noqa: S106
    scopes=("openid", "email", "offline_access", "https://graph.microsoft.com/Mail.Send"),
)
ENDPOINTS = {"google": GOOGLE, "microsoft": MICROSOFT}
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"


def _token_failure(status: int, body: dict[str, Any]) -> Exception:
    error = str(body.get("error") or "")
    if error == "invalid_grant":
        return AuthRevoked("auth_revoked")
    if status == 429 or status >= 500 or error in ("invalid_client", "unauthorized_client", "temporarily_unavailable"):
        return Transient("token_endpoint")
    return AuthRevoked("auth_revoked") if status in (400, 401) else Transient("token_endpoint")


class OAuthMailProvider:
    def __init__(self, endpoints: OAuthEndpoints, client: tuple[str, str] | None, http: HttpClient) -> None:
        self.endpoints = endpoints
        self._client = client
        self._http = http

    def _require_client(self) -> tuple[str, str]:
        if self._client is None:
            raise Transient("mail_oauth_not_configured")
        return self._client

    async def access_token(self, secret: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """A fresh access token, and the credential JSON with the rotated refresh token when the provider sent a new
        one (None when it did not)."""
        client_id, client_secret = self._require_client()
        refresh = secret.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise AuthRevoked("auth_revoked")
        resp = await self._http.form(
            self.endpoints.token_url,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        body = resp.json()
        if resp.status != 200 or not body.get("access_token"):
            raise _token_failure(resp.status, body)
        new_refresh = body.get("refresh_token")
        rotated = (
            {**secret, "refresh_token": new_refresh}
            if isinstance(new_refresh, str) and new_refresh and new_refresh != refresh
            else None
        )
        return str(body["access_token"]), rotated

    async def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> dict[str, Any]:
        """The OAuth completion: code + PKCE verifier -> refresh token and the mailbox address (the ``email`` claim of
        the ID token, read without a signature check: it came straight from the token endpoint over TLS, OIDC Core
        §3.1.3.7)."""
        client_id, client_secret = self._require_client()
        resp = await self._http.form(
            self.endpoints.token_url,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        body = resp.json()
        refresh = body.get("refresh_token")
        if resp.status != 200 or not refresh:
            raise Rejected("oauth_code_rejected")
        claims = id_token_claims(str(body.get("id_token") or ""))
        address = claims.get("email") or claims.get("preferred_username")
        if not isinstance(address, str) or "@" not in address:
            raise Rejected("oauth_no_address")
        return {"refresh_token": str(refresh), "address": address.strip().lower()}

    async def send(self, raw: bytes, secret: dict[str, Any], recipients: list[str]) -> SendResult:
        token, rotated = await self.access_token(secret)
        headers = {"Authorization": f"Bearer {token}"}
        if self.endpoints.provider == "google":
            body = json.dumps({"raw": base64.urlsafe_b64encode(raw).decode()}).encode()
            resp = await self._http.call(
                "POST", GMAIL_SEND_URL, headers={**headers, "Content-Type": "application/json"}, body=body
            )
        else:
            resp = await self._http.call(
                "POST", GRAPH_SEND_URL, headers={**headers, "Content-Type": "text/plain"}, body=base64.b64encode(raw)
            )
        if resp.status in (200, 202):
            external = resp.json().get("id") if self.endpoints.provider == "google" else None
            return SendResult(external_id=str(external)[:200] if external else None, rotated_secret=rotated)
        if resp.status == 401:
            raise AuthRevoked("auth_revoked")
        if resp.status == 403:
            raise AuthRevoked("permission_missing")
        if resp.status in (400, 413, 422):
            raise Rejected("provider_rejected")
        raise Transient("provider_unavailable")

    async def check(self, secret: dict[str, Any]) -> dict[str, Any] | None:
        _, rotated = await self.access_token(secret)
        return rotated


def id_token_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        value = json.loads(payload)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
