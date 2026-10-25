"""Send-as connectors (AA-945; errata S247 §1: Gmail, Microsoft 365 and SMTP, send only, no IMAP).

Each provider turns the agency's stored credential (the decrypted JSON of ``crm.integration_credential.secret_enc``)
into one sent message, or a typed failure the sender maps:

- :class:`AuthRevoked`: the token is dead or revoked, the app password was changed -> ``held_reconnect`` (R24);
- :class:`Rejected`: the provider refused this message for good (bad recipient, too large) -> ``failed``;
- anything else (network, 5xx, throttling): the dispatcher job is retried.

The credential JSON per kind: Gmail / Microsoft ``{"refresh_token", "address"}``; SMTP ``{"host", "port", "security"
("starttls" | "tls"), "username", "password", "address"}``. Nothing here logs a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(Exception):
    """Base of the typed failures (a code, never a provider message: it may echo an address)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AuthRevoked(ProviderError):
    pass


class Rejected(ProviderError):
    pass


class Transient(ProviderError):
    pass


@dataclass(frozen=True)
class SendResult:
    external_id: str | None  # the provider's message id when it returns one (Gmail), else None
    # The credential JSON to store instead of the one used, when the provider rotated it (Microsoft returns a new
    # refresh token on every refresh; Google sometimes does). None: keep the stored credential.
    rotated_secret: dict[str, Any] | None = None


class MailProvider(Protocol):
    async def send(self, raw: bytes, secret: dict[str, Any], recipients: list[str]) -> SendResult:
        """Send the message; ``recipients`` = the envelope (SMTP; the HTTP APIs read the headers)."""
        ...

    async def check(self, secret: dict[str, Any]) -> dict[str, Any] | None:
        """A cheap call that proves the credential still works (token refresh, SMTP login + NOOP). Returns the rotated
        credential JSON to store, or None."""
        ...
