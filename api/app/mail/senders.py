"""The sender table (AA-945; design v3 R24 "generated from the dispatcher code", lane §6.5; errata S247 §1 + §3).

Every path that sends mail has one row: what is sent, to whom, the sender kind and the way it leaves. Errata S247:
``agency_mailbox`` (the agency's own Gmail, Microsoft 365 or SMTP) or ``system`` (AA's SES, a closed list: people of
the agency, never a client message; the one-time codes to travellers are Cognito's / later cards'). There is no
``platform`` kind and no fallback from one row to another: a dead mailbox holds the message (``held_reconnect``).

``tests/unit/test_mail_senders.py`` fails when a ``Message(kind=...)`` of :mod:`app.members.mail` or a send path of
:mod:`app.mail` is missing here. ``GET /crm/v1/mail/senders`` returns the table; Settings > Mailbox renders it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class SenderRow:
    path: str  # the code of the send path (a Message kind of app.members.mail, or a mail send path)
    sender_kind: str  # agency_mailbox | system
    recipient: str  # client | agency_member
    via: str  # agency_mailbox | aa_ses | agency_twilio (AA-958)
    module: str


SENDERS: tuple[SenderRow, ...] = (
    SenderRow("client_message", "agency_mailbox", "client", "agency_mailbox", "app.mail.sender"),
    SenderRow("mailbox_test", "agency_mailbox", "agency_member", "agency_mailbox", "app.mail.health"),
    SenderRow("invitation", "system", "agency_member", "aa_ses", "app.members.mail"),
    SenderRow("recovery_notice", "system", "agency_member", "aa_ses", "app.members.mail"),
    SenderRow("data_request_notice", "system", "agency_member", "aa_ses", "app.members.mail"),
    SenderRow("mailbox_disconnected", "system", "agency_member", "aa_ses", "app.members.mail"),
    # AA-979 P2b: a Content studio invitation CIS created (the CRM delivers it; app.members.studio).
    SenderRow("studio_invitation", "system", "agency_member", "aa_ses", "app.members.mail"),
    # AA-971: the agency owners are told when AA opens break-glass (emergency) read access (app.support.sessions).
    SenderRow("breakglass_notice", "system", "agency_member", "aa_ses", "app.members.mail"),
    # AA-958 (CRM-28): SMS / WhatsApp to a client leave only through the agency's own Twilio account (errata S247 §1);
    # a dead connection fails the message with a reason (errata §2), nothing is sent another way. The owners' alert.
    SenderRow("client_text_message", "agency_mailbox", "client", "agency_twilio", "app.messaging.sender"),
    SenderRow("messaging_test", "agency_mailbox", "agency_member", "agency_twilio", "app.messaging.health"),
    SenderRow("messaging_disconnected", "system", "agency_member", "aa_ses", "app.members.mail"),
    # AA-959 (CRM-29a): the morning digest of a member's open tasks, counts only (errata S247 §3 item 3). A workflow's
    # client message is a client_message / client_text_message row (the agency's own mailbox or Twilio).
    SenderRow("task_digest", "system", "agency_member", "aa_ses", "app.members.mail"),
)
SENDER_KINDS = frozenset({"agency_mailbox", "system"})


def table() -> dict[str, Any]:
    return {"senders": [asdict(row) for row in SENDERS]}
