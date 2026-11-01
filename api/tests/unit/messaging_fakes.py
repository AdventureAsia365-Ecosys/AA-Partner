"""Stand-ins for the AA-958 SMS / WhatsApp unit tests: invented credentials built at run time (no literal that looks
like a key: gitleaks scans every commit), a recording text provider and the vault with the integration-secret pair.
The real SQL, RLS and encryption run on PostgreSQL in tests/integration/test_messaging_db.py."""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.messaging.providers import ProviderError, TemplateInfo, TextOut, TextSent
from app.messaging.state import MessagingRuntime
from tests.unit.mail_fakes import MailVault

# Built at run time: an invented account SID / Auth Token in the provider's shapes.
ACCOUNT_SID = "AC" + "0123456789abcdef" * 2
AUTH_TOKEN = "".join(["fedcba98", "76543210"]) * 2
OTHER_TOKEN = "".join(["01234567", "89abcdef"]) * 2
HOOK_ID = "ab" * 16
HOOKS_ORIGIN = "https://go.crm.example.test"
CONNECTION = uuid.UUID("00000000-0000-4000-8000-00000000e958")
CREDENTIAL = uuid.UUID("00000000-0000-4000-8000-00000000e959")
CONTENT_SID = "HX" + "a1" * 16
# Invented numbers in the Australian fictional range (ACMA: 0491 570 xxx is never assigned).
CLIENT_PHONE = "+61491570110"
AGENCY_WA = "+61491570156"
AGENCY_SMS = "+61491570157"


def secret_blob(**over: Any) -> bytes:
    return b"secret:" + json.dumps({"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN} | over).encode()


def connection_row(**over: Any) -> dict[str, Any]:
    return {
        "messaging_connection_id": CONNECTION,
        "provider": "sandbox",
        "credential_id": CREDENTIAL,
        "hook_id": HOOK_ID,
        "sms_sender": AGENCY_SMS,
        "whatsapp_sender": AGENCY_WA,
        "last_inbound_at": None,
        "last_status_at": None,
        "created_at": None,
        "account_ref": ACCOUNT_SID,
        "secret_enc": secret_blob(),
        "credential_status": "connected",
        "failure_class": None,
        "last_checked_at": None,
        "last_ok_at": None,
        "last_test_at": None,
        "last_test_result": None,
    } | over


class TextVault(MailVault):
    """MailVault plus a phone index that is the E.164 number itself (so a test can match it)."""

    async def phone_index(self, table: str, column: str, value: str, region: str | None = None) -> bytes:
        return f"p:{value}".encode()


class FakeTextProvider:
    name = "sandbox"

    def __init__(
        self,
        *,
        error: ProviderError | None = None,
        check_error: ProviderError | None = None,
        templates: list[TemplateInfo] | None = None,
        status: str | None = "queued",
    ) -> None:
        self.error = error
        self.check_error = check_error
        self.found = templates or []
        self.status = status
        self.sent: list[tuple[dict[str, Any], TextOut]] = []
        self.checked = 0

    async def send(self, secret: dict[str, Any], out: TextOut) -> TextSent:
        if self.error is not None:
            raise self.error
        self.sent.append((secret, out))
        return TextSent(sid="SM" + "5" * 32, status=self.status)

    async def check(self, secret: dict[str, Any]) -> None:
        self.checked += 1
        if self.check_error is not None:
            raise self.check_error

    async def templates(self, secret: dict[str, Any]) -> list[TemplateInfo]:
        if self.check_error is not None:
            raise self.check_error
        return self.found


def runtime(provider: Any = None, **over: Any) -> MessagingRuntime:
    values: dict[str, Any] = {
        "keys": object(),
        "providers": {"sandbox": provider or FakeTextProvider(), "twilio": provider or FakeTextProvider()},
        "hooks_origin": HOOKS_ORIGIN,
    }
    return MessagingRuntime(**(values | over))
