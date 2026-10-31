"""AA-1014 follow-up: no mail is ever handed to a provider for a ``.invalid`` address (RFC 2606 / RFC 6761).

The CRM review runs give their synthetic clients ``@example.invalid`` addresses; proposal, portal invite, portal code
and companion mails used to go out through the agency's real mailbox and bounce. The client message still ends in a
state (``failed`` / ``invalid_tld``) and the action that queued it is untouched; only the provider call is skipped.
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from app.mail import health
from app.mail.recipients import INVALID_TLD, deliverable, is_invalid_tld
from app.mail.sender import MailSender
from app.members.mail import SesMailer, invitation_message
from tests.unit.fakes import ScriptedConn
from tests.unit.mail_fakes import FakeProvider
from tests.unit.slice_fakes import enc, system_ctx, tenant_ctx
from tests.unit.test_mail_send import CREDENTIAL, DISPATCH, MAILBOX, MESSAGE, _conn, _health_row, _runtime

APP = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture(autouse=True)
def fake_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.mail import health as health_module
    from app.mail import sender as sender_module
    from tests.unit.mail_fakes import MailVault

    monkeypatch.setattr(sender_module, "Vault", lambda keys, ctx: MailVault())
    monkeypatch.setattr(health_module, "Vault", lambda keys, ctx: MailVault())


# ── the rule ─────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "address",
    [
        "mai@example.invalid",
        "Mai@Example.INVALID",
        "  mai@tour.example.invalid  ",
        "mai@example.invalid.",  # the root dot
        "mai@invalid",
        "Mai Tran <mai@example.invalid>",
        "mai@bến-thành.invalid",  # an IDN label before the reserved TLD
        "mai@example。invalid",  # an ideographic full stop is a label separator (IDNA)
        "mai@example.ｉｎｖａｌｉｄ",  # full-width letters fold to "invalid"
    ],
)
def test_reserved_invalid_addresses_are_refused(address: str) -> None:
    assert is_invalid_tld(address)
    assert deliverable([address]) == []


@pytest.mark.parametrize(
    "address",
    [
        "mai@example.test",  # other reserved names are not this rule's (tests and fixtures use them)
        "mai@example.com",
        "mai@invalid.example.com",
        "mai@notinvalid",
        "mai@example.invalidx",
        "invalid@example.com",
        "mai@bến-thành.vn",
        "",
    ],
)
def test_other_addresses_pass(address: str) -> None:
    assert not is_invalid_tld(address)
    assert deliverable([address]) == [address]


def test_a_mixed_list_keeps_only_the_deliverable_addresses_in_order() -> None:
    mixed = ["lan@example.com", "x@a.invalid", "minh@example.test", " Y@B.INVALID "]
    assert deliverable(mixed) == ["lan@example.com", "minh@example.test"]
    assert deliverable(["x@a.invalid", "y@invalid"]) == []
    assert deliverable([]) == []
    assert INVALID_TLD == "invalid_tld"


# ── the client message sender (app.mail.sender) ──────────────────────────────────────────────────
def _invalid_client(address: str = "Hoa@Tour.Example.INVALID ") -> ScriptedConn:
    return _conn().on("SELECT email_enc FROM crm.traveler", enc("traveler", "email_enc", address))


@pytest.mark.asyncio
async def test_a_client_message_to_an_invalid_address_never_reaches_the_provider() -> None:
    provider = FakeProvider()
    runtime, mailer, _ = _runtime(provider)
    conn = _invalid_client()
    assert await MailSender(runtime).send(system_ctx(conn), DISPATCH, "email", MESSAGE) == "send_failed"
    assert provider.sent == [] and mailer.sent == []  # no provider, and never AA's SES instead
    assert conn.sql("SET status = 'failed'") == [(MESSAGE, "invalid_tld")]
    assert conn.sql("SET status = 'sent'") == [] and conn.sql("UPDATE crm.integration_credential") == []


@pytest.mark.asyncio
async def test_a_portal_code_mail_to_an_invalid_address_is_not_sent_and_keeps_its_code_row() -> None:
    """The code was issued (and echoed on Dev) before the mail was queued; the sender only skips the provider."""
    provider = FakeProvider()
    runtime, _, _ = _runtime(provider)
    conn = _invalid_client("lan@example.invalid")
    conn.on("FROM crm.otp_challenge", {"otp_id": MESSAGE, "code_enc": enc("otp_challenge", "code_enc", "135790"),
                                        "expired": False})  # fmt: skip
    assert await MailSender(runtime).send(system_ctx(conn), DISPATCH, "email", MESSAGE) == "send_failed"
    assert provider.sent == [] and conn.sql("SET code_enc = NULL") == []
    assert conn.sql("SET status = 'failed'") == [(MESSAGE, "invalid_tld")]


@pytest.mark.asyncio
async def test_a_valid_client_address_is_still_sent() -> None:
    provider = FakeProvider()
    runtime, _, _ = _runtime(provider)
    conn = _invalid_client("lan@example.com")
    assert await MailSender(runtime).send(system_ctx(conn), DISPATCH, "email", MESSAGE) == "sent"
    ((_, _, rcpt),) = provider.sent
    assert rcpt == ["lan@example.com"] and conn.sql("SET status = 'failed'") == []


# ── the Test button and AA's system mail ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_mailbox_test_sends_no_mail_to_an_invalid_member_address() -> None:
    provider = FakeProvider()
    runtime, _, _ = _runtime(provider)
    conn = ScriptedConn().on("AND a.channel_account_id = $1", _health_row()).on("RETURNING channel_account_id", MAILBOX)
    out = await health.test_mailbox(tenant_ctx(conn), runtime, MAILBOX, "minh@agency.example.invalid")
    assert out == {"result": "ok", "check": out["check"], "test_mail_sent": False}
    assert provider.sent == [] and conn.sql("UPDATE crm.integration_credential SET last_test")[0][0] == CREDENTIAL


@pytest.mark.asyncio
async def test_the_ses_mailer_skips_an_invalid_address() -> None:
    sent: list[dict[str, Any]] = []

    class FakeSes:
        def send_email(self, **kw: Any) -> dict[str, Any]:
            sent.append(kw)
            return {"MessageId": "m-1"}

    mailer = SesMailer("no-reply@example.test", "transactional", client=FakeSes())
    expires = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
    skipped = invitation_message("ops@agency.INVALID", agency="A", role="agent", link="https://p", expires_at=expires)
    assert await mailer.send(skipped) == "" and sent == []
    kept = invitation_message("ops@agency.example", agency="A", role="agent", link="https://p", expires_at=expires)
    assert await mailer.send(kept) == "m-1" and len(sent) == 1


def test_every_module_that_calls_a_provider_filters_its_recipients() -> None:
    """A new send path in app.mail must go through :func:`deliverable` too."""
    found = []
    for path in sorted((APP / "mail").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if ".send(built.raw" in source:
            found.append(path.name)
            names = {
                alias.name
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ImportFrom) and node.module == "app.mail.recipients"
                for alias in node.names
            }
            assert "deliverable" in names, path.name
    assert found == ["health.py", "sender.py"]
