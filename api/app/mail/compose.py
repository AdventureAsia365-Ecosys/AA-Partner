"""Build the RFC 5322 message the agency's mailbox sends (AA-945).

- From: the mailbox address (send-as: it lands in the agency's Sent folder), with the agency's name as display name;
- To: the client;
- Subject: the agent's subject with the trip tag ``[ref:<mail_token>]`` added once (card E: the client's reply comes
  back to the trip even when a mail program drops ``References``);
- Message-ID: generated here (``make_msgid`` on the mailbox's domain); Gmail keeps it, so the reply's In-Reply-To
  threads to it; Microsoft Graph may replace it, which the trip tag covers;
- In-Reply-To / References: the Message-ID of the client's latest mail in the conversation, when there is one;
- one text/plain part (UTF-8). No HTML from the composer in v1: the agent writes text.
"""

from __future__ import annotations

import email.policy
import email.utils
from dataclasses import dataclass
from email.message import EmailMessage

from app.mail.parse import TOKEN_RE

MAX_SUBJECT = 300
MAX_BODY = 100_000


@dataclass(frozen=True)
class Built:
    raw: bytes
    message_id: str  # without angle brackets


def tagged_subject(subject: str, mail_token: str | None) -> str:
    subject = " ".join(subject.split())[:MAX_SUBJECT]
    if mail_token and not TOKEN_RE.search(subject):
        subject = f"{subject} [ref:{mail_token}]".strip()
    return subject


def build(
    *,
    from_address: str,
    from_name: str | None,
    to_address: str,
    subject: str,
    text: str,
    mail_token: str | None,
    in_reply_to: str | None = None,
) -> Built:
    domain = from_address.rpartition("@")[2] or "localhost"
    message_id = email.utils.make_msgid(domain=domain)
    msg = EmailMessage(policy=email.policy.SMTP)
    msg["From"] = email.utils.formataddr((from_name or "", from_address))
    msg["To"] = to_address
    msg["Subject"] = tagged_subject(subject, mail_token)
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg["Message-ID"] = message_id
    if in_reply_to:
        ref = f"<{in_reply_to.strip('<>')}>"
        msg["In-Reply-To"] = ref
        msg["References"] = ref
    msg.set_content(text[:MAX_BODY])
    return Built(raw=msg.as_bytes(), message_id=message_id.strip("<>"))
