"""Build the RFC 5322 message the agency's mailbox sends (AA-945).

- From: the mailbox address (send-as: it lands in the agency's Sent folder), with the agency's name as display name;
- To: the client;
- Subject: the agent's subject with the trip tag ``[ref:<mail_token>]`` added once (card E: the client's reply comes
  back to the trip even when a mail program drops ``References``);
- Message-ID: generated here (``make_msgid`` on the mailbox's domain) and stored (its hash) on the sent message. SMTP
  keeps it, so the reply's In-Reply-To threads to it. **Gmail's API (``users.messages.send``) replaces it** with its
  own ``<...@mail.gmail.com>`` id (seen live S252: the client received another Message-ID than the CRM stored) and
  Microsoft Graph may do the same; neither hands the new id back under a send-only scope;
- In-Reply-To: the Message-ID of the client's latest mail in the conversation, when there is one;
- References: that id too, then **our own Message-ID last**. Providers keep References as written, and a reply's
  References is the parent's References plus the parent's Message-ID (RFC 5322 §3.6.4), so the client's reply carries
  the id the CRM stored even when the provider replaced the Message-ID (:mod:`app.mail.threads` rule 1). Our own id
  last (not first) keeps the chain in order for JWZ-style threading: a self-reference is a loop it skips, and the
  client's mail stays this message's parent;
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
    references = [message_id]
    if in_reply_to:
        ref = f"<{in_reply_to.strip('<>')}>"
        msg["In-Reply-To"] = ref
        references.insert(0, ref)
    msg["References"] = " ".join(references)
    msg.set_content(text[:MAX_BODY])
    return Built(raw=msg.as_bytes(), message_id=message_id.strip("<>"))
