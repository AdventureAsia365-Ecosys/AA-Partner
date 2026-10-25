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
    via: str  # agency_mailbox | aa_ses
    module: str


SENDERS: tuple[SenderRow, ...] = (
    SenderRow("client_message", "agency_mailbox", "client", "agency_mailbox", "app.mail.sender"),
    SenderRow("mailbox_test", "agency_mailbox", "agency_member", "agency_mailbox", "app.mail.health"),
    SenderRow("invitation", "system", "agency_member", "aa_ses", "app.members.mail"),
    SenderRow("recovery_notice", "system", "agency_member", "aa_ses", "app.members.mail"),
    SenderRow("data_request_notice", "system", "agency_member", "aa_ses", "app.members.mail"),
    SenderRow("mailbox_disconnected", "system", "agency_member", "aa_ses", "app.members.mail"),
)
SENDER_KINDS = frozenset({"agency_mailbox", "system"})


def table() -> dict[str, Any]:
    return {"senders": [asdict(row) for row in SENDERS]}
