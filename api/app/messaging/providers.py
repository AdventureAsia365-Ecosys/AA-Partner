"""Text-message providers (AA-958; design v3 §5.4; BYO errata S247 §1: the agency's own Twilio account, no AA account).

One interface, two implementations:

- :class:`TwilioProvider`: the agency's own Twilio account (Messages API for SMS and WhatsApp, Content API for the
  WhatsApp templates the agency got approved, the account read for the health check). Credentials are the decrypted
  JSON of ``crm.integration_credential.secret_enc`` (provider ``twilio``): ``{"account_sid", "auth_token"}`` plus an
  optional ``{"api_key_sid", "api_key_secret"}`` used for the REST calls instead of the token (the token is still
  needed: Twilio signs webhooks with it).
- :class:`SandboxProvider`: a deterministic stub with the same behaviour and no network (dev / test / local only, so
  Dev checks cost $0). It never sends anything; its answers depend only on its inputs: the message SID is derived from
  the CRM's message id; a recipient ending ``0000`` is refused (``invalid_number``), one ending ``0001`` raises a
  transient error; an Auth Token starting ``revoked`` is refused as revoked; three fixed templates (two approved).

Failures reuse the typed errors of :mod:`app.mail.providers`: :class:`AuthRevoked` (token dead or account closed:
the connection is disconnected), :class:`Rejected` (this message, for good: the code becomes the message's
``failure_code``), :class:`Transient` (the dispatcher job is retried). A code, never a provider message (it may echo a
number). Nothing here logs a value.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.mail.providers import AuthRevoked, ProviderError, Rejected, Transient
from app.mail.providers.http import HttpClient, HttpResponse, Transport

__all__ = [
    "AuthRevoked",
    "ProviderError",
    "Rejected",
    "Transient",
    "TextOut",
    "TextSent",
    "TemplateInfo",
    "TextProvider",
    "TwilioProvider",
    "SandboxProvider",
    "build_providers",
    "placeholders",
]

API = "https://api.twilio.com/2010-04-01"
CONTENT_API = "https://content.twilio.com/v1/ContentAndApprovals"
MAX_TEMPLATE_PAGES = 10
APPROVALS = ("approved", "pending", "rejected", "paused", "disabled", "unsubmitted")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]{1,40})\s*\}\}")
_CONTENT_SID = re.compile(r"^HX[0-9a-f]{32}$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}([_-][A-Za-z0-9]{2,8})?$")
_CATEGORY = re.compile(r"^[A-Za-z_]{1,40}$")

# Twilio error codes (https://www.twilio.com/docs/api/errors) -> our failure codes.
AUTH_CODES = frozenset({20003, 20005})
REJECT_CODES: dict[int, str] = {
    21610: "recipient_opted_out",  # the recipient replied STOP
    63016: "window_closed",  # free-form WhatsApp message outside the 24-hour window
    21211: "invalid_number",
    21614: "invalid_number",
    63003: "invalid_number",
    21408: "region_not_enabled",
    21606: "sender_not_configured",
    21212: "sender_not_configured",
    63007: "sender_not_configured",
    21656: "invalid_template_variables",
    63027: "template_not_approved",
}


@dataclass(frozen=True)
class TextOut:
    """One outbound SMS / WhatsApp message."""

    channel: str  # sms | whatsapp
    to: str  # E.164
    sender: str  # E.164, an alphanumeric sender id (SMS) or a Messaging Service SID (MG…)
    status_callback: str
    reference: str  # the CRM's message id (the sandbox derives its SID from it)
    body: str | None = None
    content_sid: str | None = None
    variables: dict[str, str] | None = None


@dataclass(frozen=True)
class TextSent:
    sid: str
    status: str | None


@dataclass(frozen=True)
class TemplateInfo:
    content_sid: str
    name: str
    language: str
    category: str | None
    approval: str
    body: str
    variables: tuple[str, ...] = field(default_factory=tuple)


class TextProvider(Protocol):
    name: str

    async def send(self, secret: dict[str, Any], out: TextOut) -> TextSent: ...

    async def check(self, secret: dict[str, Any]) -> None:
        """A cheap call that proves the credential works; raises the typed errors."""
        ...

    async def templates(self, secret: dict[str, Any]) -> list[TemplateInfo]: ...


def placeholders(body: str) -> tuple[str, ...]:
    """The placeholder names of a template body, in first-seen order."""
    out: list[str] = []
    for name in _PLACEHOLDER.findall(body):
        if name not in out:
            out.append(name)
    return tuple(out)


def render(body: str, variables: dict[str, str]) -> str:
    """The template text with its placeholders filled (what the timeline shows)."""
    return _PLACEHOLDER.sub(lambda m: variables.get(m.group(1), m.group(0)), body)


def address(channel: str, number: str) -> str:
    return f"whatsapp:{number}" if channel == "whatsapp" else number


def _template(
    sid: str, name: str, language: str, category: str | None, approval: str, body: str
) -> TemplateInfo | None:
    """A provider template in the shape crm.channel_template accepts, or None when a field would not fit."""
    name = " ".join(str(name or "").split())[:200]
    approval = approval.lower() if approval and approval.lower() in APPROVALS else "unsubmitted"
    if not (_CONTENT_SID.match(sid) and name and _LANGUAGE.match(language or "")):
        return None
    category = category if category and _CATEGORY.match(category) else None
    body = body.replace("\x00", "")[:4096]
    return TemplateInfo(sid, name, language, category, approval, body, placeholders(body)[:100])


class TwilioProvider:
    """The agency's own Twilio account over its REST API (stdlib HTTPS, :mod:`app.mail.providers.http`)."""

    name = "twilio"

    def __init__(self, transport: Transport | None = None) -> None:
        self.http = HttpClient(transport)

    @staticmethod
    def _auth(secret: dict[str, Any]) -> str:
        user, password = (
            (secret["api_key_sid"], secret["api_key_secret"])
            if secret.get("api_key_sid") and secret.get("api_key_secret")
            else (secret["account_sid"], secret["auth_token"])
        )
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    @staticmethod
    def _fail(resp: HttpResponse) -> ProviderError:
        code = resp.json().get("code")
        if resp.status in (401, 403) or code in AUTH_CODES:
            return AuthRevoked("auth_revoked")
        if resp.status == 429:
            return Transient("rate_limited")
        if resp.status >= 500:
            return Transient("unreachable")
        if isinstance(code, int) and code in REJECT_CODES:
            return Rejected(REJECT_CODES[code])
        return Rejected("vendor_rejected")

    async def send(self, secret: dict[str, Any], out: TextOut) -> TextSent:
        fields = {"To": address(out.channel, out.to), "StatusCallback": out.status_callback}
        if out.channel == "sms" and out.sender.startswith("MG"):
            fields["MessagingServiceSid"] = out.sender
        else:
            fields["From"] = address(out.channel, out.sender)
        if out.content_sid:
            fields["ContentSid"] = out.content_sid
            fields["ContentVariables"] = json.dumps(out.variables or {}, separators=(",", ":"))
        else:
            fields["Body"] = out.body or ""
        resp = await self.http.call(
            "POST",
            f"{API}/Accounts/{urllib.parse.quote(secret['account_sid'])}/Messages.json",
            headers={
                "Authorization": self._auth(secret),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body=urllib.parse.urlencode(fields).encode(),
        )
        if resp.status not in (200, 201):
            raise self._fail(resp)
        data = resp.json()
        sid = str(data.get("sid") or "")
        if not re.match(r"^(SM|MM)[0-9a-f]{32}$", sid):
            raise Transient("bad_response")
        return TextSent(sid=sid, status=str(data.get("status") or "") or None)

    async def check(self, secret: dict[str, Any]) -> None:
        resp = await self.http.call(
            "GET",
            f"{API}/Accounts/{urllib.parse.quote(secret['account_sid'])}.json",
            headers={"Authorization": self._auth(secret), "Accept": "application/json"},
        )
        if resp.status != 200:
            raise self._fail(resp)
        status = resp.json().get("status")
        if status == "closed":
            raise AuthRevoked("auth_revoked")
        if status != "active":
            raise Rejected("vendor_rejected")  # suspended: the agency sorts it out with Twilio

    async def templates(self, secret: dict[str, Any]) -> list[TemplateInfo]:
        out: list[TemplateInfo] = []
        url: str | None = f"{CONTENT_API}?PageSize=50"
        for _ in range(MAX_TEMPLATE_PAGES):
            if not url:
                break
            resp = await self.http.call(
                "GET", url, headers={"Authorization": self._auth(secret), "Accept": "application/json"}
            )
            if resp.status != 200:
                raise self._fail(resp)
            data = resp.json()
            for item in data.get("contents") or []:
                if not isinstance(item, dict):
                    continue
                types = item.get("types") if isinstance(item.get("types"), dict) else {}
                body = next(
                    (str(v["body"]) for v in types.values() if isinstance(v, dict) and isinstance(v.get("body"), str)),
                    "",
                )
                approvals = item.get("approval_requests") if isinstance(item.get("approval_requests"), dict) else {}
                found = _template(
                    str(item.get("sid") or ""),
                    str(item.get("friendly_name") or approvals.get("name") or ""),
                    str(item.get("language") or ""),
                    str(approvals.get("category") or "") or None,
                    str(approvals.get("status") or ""),
                    body,
                )
                if found is not None:
                    out.append(found)
            next_url = (data.get("meta") or {}).get("next_page_url") if isinstance(data.get("meta"), dict) else None
            url = next_url if isinstance(next_url, str) and next_url.startswith(CONTENT_API) else None
        return out


def _sandbox_sid(prefix: str, seed: str) -> str:
    return prefix + hashlib.sha256(seed.encode()).hexdigest()[:32]


SANDBOX_TEMPLATES = (
    ("deposit_reminder", "en", "UTILITY", "approved", "Hi {{1}}, a reminder that the deposit for {{2}} is due soon."),
    ("deposit_reminder", "vi", "UTILITY", "approved", "Chào {{1}}, xin nhắc khoản đặt cọc cho {{2}} sắp đến hạn."),
    ("summer_offer", "en", "MARKETING", "pending", "Hello {{1}}, new departures are open for next summer."),
)


class SandboxProvider:
    """Deterministic stub (dev / test / local): no network, no message ever leaves."""

    name = "sandbox"

    async def send(self, secret: dict[str, Any], out: TextOut) -> TextSent:
        if str(secret.get("auth_token", "")).startswith("revoked"):
            raise AuthRevoked("auth_revoked")
        if out.to.endswith("0000"):
            raise Rejected("invalid_number")
        if out.to.endswith("0001"):
            raise Transient("unreachable")
        return TextSent(sid=_sandbox_sid("SM", out.reference), status="queued")

    async def check(self, secret: dict[str, Any]) -> None:
        if str(secret.get("auth_token", "")).startswith("revoked"):
            raise AuthRevoked("auth_revoked")

    async def templates(self, secret: dict[str, Any]) -> list[TemplateInfo]:
        await self.check(secret)
        out = []
        for name, language, category, approval, body in SANDBOX_TEMPLATES:
            found = _template(_sandbox_sid("HX", f"{name}:{language}"), name, language, category, approval, body)
            if found is not None:
                out.append(found)
        return out


def build_providers(environment: str, transport: Transport | None = None) -> dict[str, TextProvider]:
    """``twilio`` always; ``sandbox`` only on dev / test / local (never on stg / prod)."""
    from app.settings import DEV_ONLY_ENVIRONMENTS

    providers: dict[str, TextProvider] = {"twilio": TwilioProvider(transport)}
    if environment in DEV_ONLY_ENVIRONMENTS:
        providers["sandbox"] = SandboxProvider()
    return providers
