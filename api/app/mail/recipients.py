"""Recipients no mail may be sent to (AA-1014 follow-up).

RFC 2606 §2 / RFC 6761 §6.4 reserve the top-level domain ``invalid``: it never resolves, in any environment, so a
mail to ``someone@anything.invalid`` can only bounce back to the sending mailbox. Synthetic agencies (the CRM review
runs on Dev) give their clients such addresses on purpose; the CRM must still queue and record every mail, but never
hand one of them to a provider.

:func:`deliverable` keeps the addresses that may be sent to; the e-mail sender (:mod:`app.mail.sender`) and AA's
system mail (:class:`app.members.mail.SesMailer`) call it right before the provider. Only ``.invalid`` is refused:
``.test`` and ``.example`` addresses are left to the caller (tests and fixtures use them with fake providers).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from email.utils import parseaddr

INVALID_TLD = "invalid_tld"  # the failure code a message gets when no recipient is left
_DOTS = re.compile("[.。．｡]")  # the IDNA label separators (RFC 3490 §3.1)


def _domain(address: str) -> str:
    """The address's domain, lower-case ASCII (IDNA) when it encodes, without a trailing root dot."""
    value = address.strip()
    value = (parseaddr(value)[1] or value).strip()
    domain = value.rpartition("@")[2].strip()
    domain = _DOTS.sub(".", domain).rstrip(".")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        pass  # not IDNA-encodable (an empty or too long label): the plain lower-case text decides
    return domain.lower()


def is_invalid_tld(address: str) -> bool:
    """True when the address's domain is ``invalid`` or ends in ``.invalid`` (case and whitespace ignored)."""
    domain = _domain(address)
    return domain == "invalid" or domain.endswith(".invalid")


def deliverable(addresses: Iterable[str]) -> list[str]:
    """The addresses a mail may go to, in their order: every ``.invalid`` address dropped."""
    return [a for a in addresses if not is_invalid_tld(a)]
