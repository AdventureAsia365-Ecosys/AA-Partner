"""SMTP send-as (AA-945; errata S247 §1: host, port, TLS required, user, app password; send only).

Standard library ``smtplib`` in a worker thread. SSRF guard (the agency names the host): the port must be a mail
submission port (:data:`PORTS`), the host is resolved **once** and every address must be public
(:func:`require_public`), and the socket is opened to that vetted address (:class:`_PinnedSMTP`), so a DNS answer that
changes between the check and the connection (rebinding to the VPC or 169.254.x) is never followed. The host name is
still used for TLS SNI and the certificate check. TLS is required: ``security = "tls"`` (implicit TLS, port 465
usually) or ``"starttls"`` (port 587 usually; a server without STARTTLS is refused, never a plaintext login).
Certificates are verified with the system store (``ssl.create_default_context``).

Failures: a refused login -> :class:`AuthRevoked` (``auth_revoked``: the app password was changed or revoked); every
recipient refused or the data refused (5xx) -> :class:`Rejected`; connection errors, timeouts and 4xx ->
:class:`Transient`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import smtplib
import socket
import ssl
from collections.abc import Callable
from typing import Any

from app.mail.providers import AuthRevoked, Rejected, SendResult, Transient

TIMEOUT_S = 20.0
SECURITY = ("starttls", "tls")
# SMTP relay, implicit TLS, submission and the common alternative submission port: nothing else is dialled.
PORTS = frozenset({25, 465, 587, 2525})


def validate(secret: dict[str, Any]) -> None:
    host, port, security = secret.get("host"), secret.get("port"), secret.get("security")
    if not isinstance(host, str) or not host or len(host) > 253 or any(c.isspace() for c in host):
        raise Rejected("smtp_invalid_host")
    if not isinstance(port, int) or isinstance(port, bool) or port not in PORTS:
        raise Rejected("smtp_invalid_port")
    if security not in SECURITY:
        raise Rejected("smtp_tls_required")
    for name in ("username", "password", "address"):
        if not isinstance(secret.get(name), str) or not secret[name]:
            raise Rejected(f"smtp_missing_{name}")


Factory = Callable[[dict[str, Any]], smtplib.SMTP]
Resolver = Callable[[str, int], list[str]]


def resolve(host: str, port: int) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]


def require_public(host: str, port: int, resolver: Resolver = resolve) -> str:
    """The agency names the SMTP host, so the task would connect wherever it points: refuse a host that resolves to
    a private, loopback, link-local (instance metadata), reserved or multicast address (SSRF into the VPC). Returns
    the vetted address the caller must connect to (the name is never resolved a second time)."""
    try:
        addresses = resolver(host, port)
    except (OSError, UnicodeError):
        raise Transient("smtp_unreachable") from None
    if not addresses:
        raise Transient("smtp_unreachable")
    for raw in addresses:
        ip = ipaddress.ip_address(raw.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise Rejected("smtp_invalid_host")
    return addresses[0].split("%")[0]


class _PinnedSMTP(smtplib.SMTP):
    """``smtplib.SMTP`` whose socket goes to the address :func:`require_public` vetted, not to a new DNS answer.
    ``self._host`` stays the host name, which ``starttls()`` uses for SNI and the certificate check."""

    def __init__(self, host: str, port: int, address: str, **kwargs: Any) -> None:
        self._pinned_address = address
        super().__init__(host, port, **kwargs)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return socket.create_connection((self._pinned_address, port), timeout, self.source_address)


class _PinnedSMTPSSL(smtplib.SMTP_SSL, _PinnedSMTP):
    """Implicit TLS on the pinned address: ``SMTP_SSL._get_socket`` wraps the socket :class:`_PinnedSMTP` opens, with
    ``server_hostname`` = the host name."""

    def __init__(self, host: str, port: int, address: str, **kwargs: Any) -> None:
        self._pinned_address = address
        smtplib.SMTP_SSL.__init__(self, host, port, **kwargs)


def connect(secret: dict[str, Any], resolver: Resolver = resolve) -> smtplib.SMTP:
    host, port = secret["host"], secret["port"]
    address = require_public(host, port, resolver)
    context = ssl.create_default_context()
    if secret["security"] == "tls":
        server: smtplib.SMTP = _PinnedSMTPSSL(host, port, address, timeout=TIMEOUT_S, context=context)
    else:
        server = _PinnedSMTP(host, port, address, timeout=TIMEOUT_S)
        server.ehlo()
        if not server.has_extn("starttls"):
            server.quit()
            raise Rejected("smtp_tls_required")
        server.starttls(context=context)
        server.ehlo()
    return server


class SmtpProvider:
    def __init__(self, factory: Factory = connect) -> None:
        self._factory = factory

    def _run(self, secret: dict[str, Any], work: Callable[[smtplib.SMTP], Any]) -> Any:
        validate(secret)
        try:
            server = self._factory(secret)
        except (smtplib.SMTPException, OSError) as exc:
            raise Transient("smtp_unreachable") from exc
        try:
            try:
                server.login(secret["username"], secret["password"])
            except smtplib.SMTPAuthenticationError:
                raise AuthRevoked("auth_revoked") from None
            return work(server)
        except smtplib.SMTPRecipientsRefused:
            raise Rejected("provider_rejected") from None
        except smtplib.SMTPResponseException as exc:
            if 500 <= exc.smtp_code < 600:
                raise Rejected("provider_rejected") from None
            raise Transient("smtp_temporary") from None
        except (smtplib.SMTPException, OSError) as exc:
            raise Transient("smtp_unreachable") from exc
        finally:
            try:
                server.quit()
            except (smtplib.SMTPException, OSError):
                pass

    async def send(self, raw: bytes, secret: dict[str, Any], recipients: list[str]) -> SendResult:
        if not recipients:
            raise Rejected("no_recipient")

        def work(server: smtplib.SMTP) -> None:
            raise_if_refused(server.sendmail(secret["address"], list(recipients), raw))

        await asyncio.to_thread(self._run, secret, work)
        return SendResult(external_id=None)

    async def check(self, secret: dict[str, Any]) -> dict[str, Any] | None:
        await asyncio.to_thread(self._run, secret, lambda server: server.noop())
        return None


def raise_if_refused(refused: dict[str, Any]) -> None:
    if refused:  # some recipients refused (all refused raises SMTPRecipientsRefused)
        raise Rejected("provider_rejected")
