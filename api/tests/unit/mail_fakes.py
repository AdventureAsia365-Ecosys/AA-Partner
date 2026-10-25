"""Stand-ins for the AA-945 mail unit tests: raw mail builders, a vault with the integration-secret pair, fake
providers and a fake SMTP server. The real SQL, RLS, encryption and threading run on PostgreSQL in
tests/integration/test_mail_db.py."""

from __future__ import annotations

import json
from email.message import EmailMessage
from typing import Any

from app.mail.providers import AuthRevoked, ProviderError, SendResult
from app.vault import DecryptionDenied
from tests.unit.slice_fakes import FakeVault

INGEST_DOMAIN = "crm-in.example.test"
# What SES adds to a clean mail whose From domain (sunshine.example) signed it: the agency's own BCC copy.
SES_AUTHENTICATED = {
    "X-SES-Spam-Verdict": "PASS",
    "X-SES-Virus-Verdict": "PASS",
    "Authentication-Results": (
        "amazonses.com; spf=pass (spfCheck: domain of sunshine.example designates 192.0.2.1 as permitted sender) "
        "client-ip=192.0.2.1; envelope-from=hello@sunshine.example; dkim=pass header.i=@sunshine.example; "
        "dmarc=pass header.from=sunshine.example;"
    ),
}


def raw_mail(
    *,
    to: str = f"sunshine-ab12cd@{INGEST_DOMAIN}",
    sender: str = "Lan Nguyen <lan@example.test>",
    subject: str = "Our trip to Hoi An",
    text: str | None = "Hello, we would like to come in March.",
    html: str | None = None,
    message_id: str | None = "<abc-1@mail.example.test>",
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: list[tuple[str, str, bytes]] | None = None,
    extra: dict[str, str] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = "Sat, 24 Oct 2026 09:00:00 +0700"
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    for k, v in (extra or {}).items():
        if k.lower() == "date":
            del msg[k]
        msg[k] = v
    if text is not None:
        msg.set_content(text)
    if html is not None:
        if text is None:
            msg.set_content(html, subtype="html")
        else:
            msg.add_alternative(html, subtype="html")
    for name, ctype, data in attachments or []:
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


class MailVault(FakeVault):
    """FakeVault plus the integration-secret pair (``secret:`` + JSON) and bytes values."""

    def __init__(self, refuse: bool = False) -> None:
        super().__init__(refuse)
        self.secrets_opened = 0

    async def encrypt(self, table: str, column: str, row_id: Any, value: str | bytes, **_: Any) -> bytes:
        text = value if isinstance(value, str) else value.decode("latin-1")
        return f"enc:{table}.{column}:{text}".encode("latin-1")

    async def encrypt_secret(self, credential_id: Any, secret: str) -> bytes:
        return b"secret:" + secret.encode()

    async def decrypt_secret(self, credential_id: Any, blob: bytes) -> str:
        if not bytes(blob).startswith(b"secret:"):
            raise DecryptionDenied("integrity_check_failed")
        self.secrets_opened += 1
        return bytes(blob)[7:].decode()


def secret_blob(**secret: Any) -> bytes:
    return b"secret:" + json.dumps(secret).encode()


class FakeProvider:
    def __init__(
        self,
        error: ProviderError | None = None,
        external_id: str | None = "gm-1",
        rotated: dict[str, Any] | None = None,
    ) -> None:
        self.error = error
        self.external_id = external_id
        self.rotated = rotated  # what a provider that rotated the refresh token hands back
        self.sent: list[tuple[bytes, dict[str, Any], list[str]]] = []
        self.checked = 0

    async def send(self, raw: bytes, secret: dict[str, Any], recipients: list[str]) -> SendResult:
        if self.error:
            raise self.error
        self.sent.append((raw, secret, recipients))
        return SendResult(external_id=self.external_id, rotated_secret=self.rotated)

    async def check(self, secret: dict[str, Any]) -> dict[str, Any] | None:
        self.checked += 1
        if self.error:
            raise self.error
        return self.rotated


REVOKED = AuthRevoked("auth_revoked")
