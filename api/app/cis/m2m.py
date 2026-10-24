"""Client-credentials tokens of the aa-m2m pool for the CRM's calls to other apps (root ADR 0006b decision 7; AA-978).

``M2M_CLIENT`` (the task's secret, AA-CIS-Infra ``crm_app.tf``) is the JSON of ``aa-crm/<env>/m2m-booking-client``:
``{client_id, client_secret, token_url, scopes}``. :func:`parse_credentials` reads it (``scopes`` is informational: each
call asks for exactly the scope it needs). :class:`TokenProvider` asks the pool's token endpoint for one scope at a
time (``cis.tenant/read`` for the membership feed, later ``cis.catalog/read`` / ``booking.b2b/*``), so a token only
ever carries what its call needs, and caches each token until :data:`REFRESH_MARGIN_S` before it expires (Cognito
M2M tokens live hours, so a pull every 20 s asks the pool a few times a day).

Errors are :class:`TokenError` with a code only (``token_unauthorized``: the pool refused the client;
``token_unavailable``: the endpoint failed or was unreachable; ``token_invalid``: an answer without a usable token).
The secret, the token and the endpoint's body are never logged or put in an error.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field

from app.cis.http import DEFAULT_TIMEOUT_S, HttpError, Transport, urllib_transport

REFRESH_MARGIN_S = 300
MIN_LIFETIME_S = 60
MAX_LIFETIME_S = 24 * 3600


class CredentialsError(ValueError):
    """``M2M_CLIENT`` is not the expected JSON. The message names the problem, never the value."""


class TokenError(Exception):
    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


@dataclass(frozen=True)
class M2mCredentials:
    client_id: str
    client_secret: str = field(repr=False)
    token_url: str


def parse_credentials(raw: str, *, allow_http: bool = False) -> M2mCredentials:
    try:
        data = json.loads(raw)
    except ValueError:
        raise CredentialsError("M2M_CLIENT must be a JSON object") from None
    if not isinstance(data, dict):
        raise CredentialsError("M2M_CLIENT must be a JSON object")
    values = {k: data.get(k) for k in ("client_id", "client_secret", "token_url")}
    missing = sorted(k for k, v in values.items() if not isinstance(v, str) or not v.strip())
    if missing:
        raise CredentialsError(f"M2M_CLIENT lacks {', '.join(missing)}")
    if not values["client_id"].isalnum():
        raise CredentialsError("M2M_CLIENT client_id must be a Cognito app client id")
    schemes = ("https://", "http://") if allow_http else ("https://",)
    if not values["token_url"].startswith(schemes):
        raise CredentialsError("M2M_CLIENT token_url must be an https URL")
    return M2mCredentials(values["client_id"], values["client_secret"], values["token_url"])


class TokenProvider:
    def __init__(
        self,
        credentials: M2mCredentials,
        *,
        transport: Transport = urllib_transport,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._credentials = credentials
        self._transport = transport
        self._clock = clock
        self._timeout_s = timeout_s
        self._cache: dict[str, tuple[str, float]] = {}  # scope -> (token, refresh at)

    def invalidate(self, scope: str) -> None:
        """Forget the cached token of ``scope`` (the receiver answered 401: revoked or rotated)."""
        self._cache.pop(scope, None)

    async def token(self, scope: str) -> str:
        cached = self._cache.get(scope)
        now = self._clock()
        if cached is not None and now < cached[1]:
            return cached[0]
        token, lifetime = await self._fetch(scope)
        # Refresh REFRESH_MARGIN_S before it expires (half way for a token shorter than twice the margin).
        self._cache[scope] = (token, now + max(lifetime - REFRESH_MARGIN_S, lifetime / 2))
        return token

    async def _fetch(self, scope: str) -> tuple[str, float]:
        creds = self._credentials
        basic = base64.b64encode(f"{creds.client_id}:{creds.client_secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": scope}).encode()
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        try:
            resp = await self._transport("POST", creds.token_url, headers, body, self._timeout_s)
        except HttpError:
            raise TokenError("token_unavailable") from None
        if resp.status in (400, 401, 403):
            raise TokenError("token_unauthorized")  # invalid_client / unauthorized_client / invalid_scope
        if resp.status != 200:
            raise TokenError("token_unavailable")
        try:
            data = json.loads(resp.body)
            token = data["access_token"]
            lifetime = float(data.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError):
            raise TokenError("token_invalid") from None
        if not isinstance(token, str) or not token or str(data.get("token_type", "Bearer")).lower() != "bearer":
            raise TokenError("token_invalid")
        return token, min(max(lifetime, MIN_LIFETIME_S), MAX_LIFETIME_S)
