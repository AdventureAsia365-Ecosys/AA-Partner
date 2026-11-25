"""A member's own Google / Microsoft 365 calendar: OAuth connect, the token, and the provider calendar APIs (AA-1011
part 3; errata S247 §1 bring-your-own; crm-089 ``crm.calendar_connection``).

**OAuth** reuses the mailbox's machinery (:mod:`app.mail.oauth`, :mod:`app.mail.providers.oauth_mail`): AA's own
OAuth clients (``MAIL_OAUTH``: one Google client, one Entra multi-tenant app), authorization code + PKCE (S256), the
redirect URI ``<MAIL_OAUTH_REDIRECT_BASE>/connect/mail/callback`` (so Google needs no new redirect URI). Only the scopes
differ: Google ``calendar.events``, Graph ``Calendars.ReadWrite`` + ``offline_access`` (plus ``openid email`` for the
account's address). The sealed state is bound to a column of its own (``oauth_state.calendar_state``), so a calendar
state never completes a mailbox connection nor the other way round, and it carries the visible prefix
:data:`STATE_PREFIX` so the web callback sends the browser back to Account > Integrations instead of Settings > Mailbox
(the prefix only routes; the sealed part decides). Without a provider's client pair the start answers 503
``mail_oauth_not_configured`` (Microsoft until the Entra values exist).

**The token** is the member's own ``crm.integration_credential`` row (provider ``google_calendar`` /
``microsoft_calendar``), sealed under the agency integration DEK (``Vault.encrypt_secret``); this module is the only
calendar code that opens it (:func:`open_secret`, the ``decrypt_secret`` lint of ``tests/unit/test_vault_rules.py``).
A refresh that returns a new refresh token (Microsoft always, Google sometimes) is stored again by the push job, so a
calendar in use never ages into ``invalid_grant``. Nothing here logs a token or an address.

**The calendar APIs** (:class:`GoogleCalendar`, :class:`GraphCalendar`): list the CRM's own events of one connection
(a private extended property ``aaCrm`` = the connection id, with ``aaCrmKey`` = the CRM event key and ``aaCrmHash``
= the digest of what was written), create, update, delete, and revoke the grant on disconnect (Google's revoke
endpoint; Microsoft has no per-token revoke: the token is deleted and the member may remove the app's consent in their
account). Google events get a deterministic id derived from (connection, key), Graph events a deterministic
``transactionId``: a create retried after a lost answer never makes a second event (idempotent by external event id).

Failures map to :mod:`app.mail.providers`: 401 / ``invalid_grant`` -> :class:`AuthRevoked` (``auth_revoked``), 403 ->
``permission_missing`` (a Google rate-limit 403 is :class:`Transient` ``rate_limited``), 400 / 422 -> :class:`Rejected`
``vendor_rejected``, 429 / 5xx / network -> :class:`Transient`.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import secrets
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

from app.errors import Conflict, Invalid, Unavailable
from app.mail import oauth as mail_oauth
from app.mail.providers import AuthRevoked, ProviderError, Rejected, Transient
from app.mail.providers.http import HttpClient, HttpResponse, Transport
from app.mail.providers.oauth_mail import GOOGLE, MICROSOFT, OAuthEndpoints, OAuthMailProvider
from app.settings import Settings
from app.tenancy import TenantContext
from app.vault import Vault

PROVIDERS = ("google", "microsoft")
SCOPES = ("mine", "agency")
CREDENTIAL_PROVIDER = {"google": "google_calendar", "microsoft": "microsoft_calendar"}
STATE_PREFIX = mail_oauth.CALENDAR_STATE_PREFIX  # "cal.": the web callback routes on it
STATE_TABLE = "oauth_state"
STATE_COLUMN = "calendar_state"

CAL_GOOGLE = OAuthEndpoints(
    provider="google",
    kind="google_calendar",
    authorize_url=GOOGLE.authorize_url,
    token_url=GOOGLE.token_url,
    scopes=("openid", "email", "https://www.googleapis.com/auth/calendar.events"),
)
CAL_MICROSOFT = OAuthEndpoints(
    provider="microsoft",
    kind="microsoft_calendar",
    authorize_url=MICROSOFT.authorize_url,
    token_url=MICROSOFT.token_url,
    scopes=("openid", "email", "offline_access", "https://graph.microsoft.com/Calendars.ReadWrite"),
)
CAL_ENDPOINTS = {"google": CAL_GOOGLE, "microsoft": CAL_MICROSOFT}

GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GRAPH_EVENTS_URL = "https://graph.microsoft.com/v1.0/me/events"
# Graph single-value extended properties: a GUID of our own, one name per value (MAPI named properties).
GRAPH_PROP_SET = "{6b1f4c2e-8d3a-4e5b-9c7f-2a0d1e3b5c4f}"
GRAPH_TAG = f"String {GRAPH_PROP_SET} Name aaCrm"
GRAPH_KEY = f"String {GRAPH_PROP_SET} Name aaCrmKey"
GRAPH_HASH = f"String {GRAPH_PROP_SET} Name aaCrmHash"
PAGE_SIZE = 250  # technical-constant: Google's maximum page of events.list (Graph pages at 100)
LIST_PAGE_CAP = 40  # technical-constant: a runaway nextPageToken / nextLink loop stops after this many pages
BASE32HEX = "0123456789abcdefghijklmnopqrstuv"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise Invalid("oauth_state_invalid") from None


# ── OAuth ────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Completed:
    provider: str
    scope: str
    secret: dict[str, Any]  # {refresh_token, address}


async def start(ctx: TenantContext, vault: Vault, settings: Settings, provider: str, scope: str) -> dict[str, Any]:
    """The consent URL for the member's own calendar (state sealed for this member, ``STATE_TTL_S``)."""
    if provider not in CAL_ENDPOINTS or scope not in SCOPES:
        raise Invalid("invalid_provider", "google or microsoft; scope mine or agency")
    client = settings.mail_oauth_client(provider)
    if client is None:
        raise Unavailable("mail_oauth_not_configured", f"the {provider} OAuth client is not configured")
    endpoints = CAL_ENDPOINTS[provider]
    verifier = secrets.token_urlsafe(48)
    payload = {
        "p": provider,
        "s": scope,
        "m": str(ctx.member_id),
        "n": secrets.token_urlsafe(12),
        "e": int(time.time()) + mail_oauth.STATE_TTL_S,
        "v": verifier,
    }
    sealed = await vault.encrypt(
        STATE_TABLE, STATE_COLUMN, ctx.member_id, json.dumps(payload), purpose=mail_oauth.PURPOSE
    )
    params = {
        "client_id": client[0],
        "redirect_uri": mail_oauth.redirect_uri(settings),
        "response_type": "code",
        "scope": " ".join(endpoints.scopes),
        "state": STATE_PREFIX + _b64(sealed),
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    if provider == "google":
        params |= {"access_type": "offline", "prompt": "consent"}
    else:
        params |= {"response_mode": "query", "prompt": "select_account"}
    ctx.audit("calendar_connect_start", "allowed", "calendar_connection", reason=provider)
    return {"authorize_url": f"{endpoints.authorize_url}?{urllib.parse.urlencode(params)}",
            "expires_in": mail_oauth.STATE_TTL_S}  # fmt: skip


async def complete(
    ctx: TenantContext, vault: Vault, settings: Settings, clients: dict[str, Any], code: str, state: str
) -> Completed:
    """The code + the sealed state -> the refresh token and the account's address (never stored here)."""
    if not code or len(code) > 4096 or not state.startswith(STATE_PREFIX) or len(state) > 4096:
        raise Invalid("oauth_state_invalid")
    sealed = _unb64(state[len(STATE_PREFIX) :])
    opened = await vault.decrypt(STATE_TABLE, STATE_COLUMN, ctx.member_id, sealed, purpose=mail_oauth.PURPOSE)
    try:
        payload = json.loads(opened)
    except ValueError:
        raise Invalid("oauth_state_invalid") from None
    if (
        payload.get("m") != str(ctx.member_id)
        or payload.get("p") not in CAL_ENDPOINTS
        or payload.get("s") not in SCOPES
    ):
        raise Invalid("oauth_state_invalid")
    if int(payload.get("e", 0)) < int(time.time()):
        raise Conflict("oauth_state_expired", "start the connection again")
    client: CalendarClient | None = clients.get(payload["p"])
    if client is None:
        raise Unavailable("mail_oauth_not_configured", "the calendar client is not configured")
    try:
        secret = await client.oauth.exchange_code(code, str(payload["v"]), mail_oauth.redirect_uri(settings))
    except ProviderError as exc:
        raise Conflict(exc.code, "the provider did not accept the authorization") from None
    return Completed(provider=payload["p"], scope=payload["s"], secret=secret)


# ── The token (the only calendar code that opens it) ─────────────────────────────────────────────


async def open_secret(vault: Vault, credential_id: uuid.UUID, blob: bytes) -> dict[str, Any]:
    """The credential JSON ``{refresh_token, address}``. Never logged, never returned by a route."""
    value = json.loads(await vault.decrypt_secret(credential_id, blob))
    return value if isinstance(value, dict) else {}


async def seal_secret(vault: Vault, credential_id: uuid.UUID, secret: dict[str, Any]) -> bytes:
    return await vault.encrypt_secret(credential_id, json.dumps(secret))


# ── Events ───────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PushEvent:
    """One CRM event as written into the calendar: no personal data (the ICS feed's rule, :mod:`app.calendar.ics`)."""

    key: str  # the CRM event id (events.py ``id``): departure-<trip>, task-<task>
    summary: str
    description: str
    all_day: bool
    day: dt.date
    starts_at: dt.datetime | None
    ends_at: dt.datetime | None
    timezone: str

    def digest(self) -> str:
        start = self.day.isoformat() if self.all_day else self.starts_at.isoformat()  # type: ignore[union-attr]
        end = "" if self.all_day else self.ends_at.isoformat()  # type: ignore[union-attr]
        raw = json.dumps([self.summary, self.description, self.all_day, start, end, self.timezone])
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class RemoteEvent:
    event_id: str
    key: str
    digest: str


def event_id(connection_id: uuid.UUID, key: str) -> str:
    """A deterministic Google event id (base32hex, lowercase a-v and 0-9, 5-1024 characters): the same (connection,
    key) always names the same event, so a create retried after a lost answer finds the first one (409)."""
    digest = hashlib.sha256(f"{connection_id}:{key}".encode()).digest()
    bits = int.from_bytes(digest[:25], "big")
    out = "".join(BASE32HEX[(bits >> (5 * i)) & 31] for i in range(40))
    return "aacrm" + out


def _failure(resp: HttpResponse) -> ProviderError:
    if resp.status == 401:
        return AuthRevoked("auth_revoked")
    if resp.status == 403:
        reason = json.dumps(resp.json()).lower()
        if "ratelimit" in reason or "quota" in reason:
            return Transient("rate_limited")
        return AuthRevoked("permission_missing")
    if resp.status == 429:
        return Transient("rate_limited")
    if resp.status in (400, 409, 412, 413, 422):
        return Rejected("vendor_rejected")
    return Transient("unreachable")


class CalendarClient:
    """One provider's calendar API through AA's OAuth client (``client`` None = not configured: 503 on connect)."""

    provider = ""

    def __init__(self, endpoints: OAuthEndpoints, client: tuple[str, str] | None, http: HttpClient) -> None:
        self.oauth = OAuthMailProvider(endpoints, client, http)
        self.configured = client is not None
        self._http = http

    async def access_token(self, secret: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        return await self.oauth.access_token(secret)

    async def _json(self, method: str, url: str, token: str, body: dict[str, Any] | None = None) -> HttpResponse:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return await self._http.call(method, url, headers=headers, body=json.dumps(body).encode() if body else None)

    async def delete(self, token: str, external_id: str) -> None:
        resp = await self._json("DELETE", f"{self.events_url}/{urllib.parse.quote(external_id, safe='')}", token)
        if resp.status not in (200, 202, 204, 404, 410):
            raise _failure(resp)

    events_url = ""

    async def list_ours(
        self, token: str, connection_id: uuid.UUID, since: dt.datetime | None, until: dt.datetime | None
    ) -> list[RemoteEvent]:
        raise NotImplementedError

    async def create(self, token: str, connection_id: uuid.UUID, event: PushEvent) -> str:
        raise NotImplementedError

    async def update(self, token: str, connection_id: uuid.UUID, external_id: str, event: PushEvent) -> None:
        raise NotImplementedError

    async def revoke(self, secret: dict[str, Any]) -> bool:
        """Revoke the grant at the provider; True when the provider confirmed (or it was already gone)."""
        return False


def _iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class GoogleCalendar(CalendarClient):
    provider = "google"
    events_url = GOOGLE_EVENTS_URL

    def _body(self, connection_id: uuid.UUID, event: PushEvent) -> dict[str, Any]:
        if event.all_day:
            start: dict[str, str] = {"date": event.day.isoformat()}
            end: dict[str, str] = {"date": (event.day + dt.timedelta(days=1)).isoformat()}
        else:
            start = {"dateTime": _iso_utc(event.starts_at), "timeZone": "UTC"}  # type: ignore[arg-type]
            end = {"dateTime": _iso_utc(event.ends_at), "timeZone": "UTC"}  # type: ignore[arg-type]
        return {
            "summary": event.summary,
            "description": event.description,
            "start": start,
            "end": end,
            "status": "confirmed",
            "transparency": "transparent" if event.all_day else "opaque",
            "extendedProperties": {
                "private": {"aaCrm": str(connection_id), "aaCrmKey": event.key, "aaCrmHash": event.digest()}
            },
        }

    async def list_ours(
        self, token: str, connection_id: uuid.UUID, since: dt.datetime | None, until: dt.datetime | None
    ) -> list[RemoteEvent]:
        params: dict[str, str] = {
            "privateExtendedProperty": f"aaCrm={connection_id}",
            "maxResults": str(PAGE_SIZE),
            "showDeleted": "false",
            "singleEvents": "true",
        }
        if since is not None:
            params["timeMin"] = _iso_utc(since)
        if until is not None:
            params["timeMax"] = _iso_utc(until)
        out: list[RemoteEvent] = []
        for _ in range(LIST_PAGE_CAP):
            resp = await self._json("GET", f"{GOOGLE_EVENTS_URL}?{urllib.parse.urlencode(params)}", token)
            if resp.status != 200:
                raise _failure(resp)
            body = resp.json()
            for item in body.get("items") or []:
                private = ((item or {}).get("extendedProperties") or {}).get("private") or {}
                if item.get("id") and private.get("aaCrmKey"):
                    out.append(RemoteEvent(str(item["id"]), str(private["aaCrmKey"]), str(private.get("aaCrmHash"))))
            token_next = body.get("nextPageToken")
            if not token_next:
                break
            params["pageToken"] = str(token_next)
        return out

    async def create(self, token: str, connection_id: uuid.UUID, event: PushEvent) -> str:
        external = event_id(connection_id, event.key)
        resp = await self._json("POST", GOOGLE_EVENTS_URL, token, {"id": external, **self._body(connection_id, event)})
        if resp.status == 409:  # made by an earlier attempt (or cancelled since): write it again
            await self.update(token, connection_id, external, event)
            return external
        if resp.status not in (200, 201):
            raise _failure(resp)
        return external

    async def update(self, token: str, connection_id: uuid.UUID, external_id: str, event: PushEvent) -> None:
        url = f"{GOOGLE_EVENTS_URL}/{urllib.parse.quote(external_id, safe='')}"
        resp = await self._json("PUT", url, token, self._body(connection_id, event))
        if resp.status != 200:
            raise _failure(resp)

    async def revoke(self, secret: dict[str, Any]) -> bool:
        refresh = secret.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            return True
        resp = await self._http.form(GOOGLE_REVOKE_URL, {"token": refresh})
        # 400 invalid_token: the grant is already gone (the member removed it in their Google account).
        return resp.status in (200, 400)


class GraphCalendar(CalendarClient):
    provider = "microsoft"
    events_url = GRAPH_EVENTS_URL

    def _body(self, connection_id: uuid.UUID, event: PushEvent) -> dict[str, Any]:
        if event.all_day:
            day = event.day
            start = {"dateTime": f"{day.isoformat()}T00:00:00", "timeZone": event.timezone}
            end = {"dateTime": f"{(day + dt.timedelta(days=1)).isoformat()}T00:00:00", "timeZone": event.timezone}
        else:
            start = {"dateTime": _iso_utc(event.starts_at)[:-1], "timeZone": "UTC"}  # type: ignore[arg-type]
            end = {"dateTime": _iso_utc(event.ends_at)[:-1], "timeZone": "UTC"}  # type: ignore[arg-type]
        return {
            "subject": event.summary,
            "body": {"contentType": "text", "content": event.description},
            "start": start,
            "end": end,
            "isAllDay": event.all_day,
            "showAs": "free" if event.all_day else "busy",
            "singleValueExtendedProperties": [
                {"id": GRAPH_TAG, "value": str(connection_id)},
                {"id": GRAPH_KEY, "value": event.key},
                {"id": GRAPH_HASH, "value": event.digest()},
            ],
        }

    def _list_url(self, connection_id: uuid.UUID) -> str:
        params = {
            "$filter": (
                f"singleValueExtendedProperties/Any(ep: ep/id eq '{GRAPH_TAG}' and ep/value eq '{connection_id}')"
            ),
            "$select": "id,start",
            "$expand": f"singleValueExtendedProperties($filter=id eq '{GRAPH_KEY}' or id eq '{GRAPH_HASH}')",
            "$top": "100",
        }
        return f"{GRAPH_EVENTS_URL}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"

    async def list_ours(
        self, token: str, connection_id: uuid.UUID, since: dt.datetime | None, until: dt.datetime | None
    ) -> list[RemoteEvent]:
        url: str | None = self._list_url(connection_id)
        out: list[RemoteEvent] = []
        for _ in range(LIST_PAGE_CAP):
            if not url:
                break
            resp = await self._json("GET", url, token)
            if resp.status != 200:
                raise _failure(resp)
            body = resp.json()
            for item in body.get("value") or []:
                remote = _graph_item(item, since, until)
                if remote is not None:
                    out.append(remote)
            link = body.get("@odata.nextLink")
            url = str(link) if isinstance(link, str) and link.startswith(GRAPH_EVENTS_URL) else None
        return out

    async def create(self, token: str, connection_id: uuid.UUID, event: PushEvent) -> str:
        body = {**self._body(connection_id, event), "transactionId": event_id(connection_id, event.key)}
        resp = await self._json("POST", GRAPH_EVENTS_URL, token, body)
        if resp.status not in (200, 201):
            raise _failure(resp)
        external = resp.json().get("id")
        if not isinstance(external, str) or not external:
            raise Transient("unreachable")
        return external

    async def update(self, token: str, connection_id: uuid.UUID, external_id: str, event: PushEvent) -> None:
        url = f"{GRAPH_EVENTS_URL}/{urllib.parse.quote(external_id, safe='')}"
        resp = await self._json("PATCH", url, token, self._body(connection_id, event))
        if resp.status != 200:
            raise _failure(resp)


def _graph_item(item: dict[str, Any], since: dt.datetime | None, until: dt.datetime | None) -> RemoteEvent | None:
    values = item.get("singleValueExtendedProperties") or []
    props = {p.get("id"): p.get("value") for p in values if isinstance(p, dict)}
    key = next((v for k, v in props.items() if isinstance(k, str) and k.lower() == GRAPH_KEY.lower()), None)
    digest = next((v for k, v in props.items() if isinstance(k, str) and k.lower() == GRAPH_HASH.lower()), "")
    if not item.get("id") or not key:
        return None
    start = _graph_start(item.get("start"))
    if start is not None and ((since and start < since) or (until and start >= until)):
        return None  # outside the window: left as it is (the Google list asks the API for the window)
    return RemoteEvent(str(item["id"]), str(key), str(digest))


def _graph_start(value: Any) -> dt.datetime | None:
    if not isinstance(value, dict) or not isinstance(value.get("dateTime"), str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value["dateTime"][:19])
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.UTC)  # Graph answers in UTC unless a Prefer header asks otherwise


def build_clients(settings: Settings, transport: Transport | None = None) -> dict[str, CalendarClient]:
    """The calendar clients of this process, by provider."""
    http = HttpClient(transport)
    return {
        "google": GoogleCalendar(CAL_GOOGLE, settings.mail_oauth_client("google"), http),
        "microsoft": GraphCalendar(CAL_MICROSOFT, settings.mail_oauth_client("microsoft"), http),
    }
