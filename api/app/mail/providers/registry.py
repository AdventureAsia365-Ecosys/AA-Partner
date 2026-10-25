"""The send-as providers of this process, by ``channel_account.kind`` (AA-945)."""

from __future__ import annotations

from typing import Any

from app.mail.providers.http import HttpClient, Transport
from app.mail.providers.oauth_mail import GOOGLE, MICROSOFT, OAuthMailProvider
from app.mail.providers.smtp import Factory, SmtpProvider, connect
from app.settings import Settings


def build_providers(
    settings: Settings, transport: Transport | None = None, smtp_factory: Factory = connect
) -> dict[str, Any]:
    http = HttpClient(transport)
    return {
        "gmail": OAuthMailProvider(GOOGLE, settings.mail_oauth_client("google"), http),
        "microsoft": OAuthMailProvider(MICROSOFT, settings.mail_oauth_client("microsoft"), http),
        "smtp": SmtpProvider(smtp_factory),
    }
