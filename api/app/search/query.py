"""What a global search term is (design v3 §2.3 A-00, §4.2; card CRM-32 R: no partial F1 search).

:func:`classify` reads the term once and says how each kind may use it. It never guesses a fragment of an identity
field:

- a term with ``@`` is an e-mail: a whole, valid address is searched through the e-mail blind index; anything else
  (``@example.test``, ``anna@``) is refused (422 ``partial_contact_search``);
- a term that starts with ``+`` and is otherwise digits and separators is a phone: a valid international number is
  searched through the phone blind index (E.164); anything else is refused (422 ``partial_contact_search``);
- a wildcard (``*`` or ``%``) asks for a partial match: refused (422 ``partial_search_refused``);
- any other term is text: whole name tokens through the name token blind index (clients), and the plaintext kinds
  (:mod:`app.search.kinds`). A run of digits without ``+`` is never tried as a phone (it is more often a supplier's
  confirmation number), so it searches references and text only.

The term is never logged, stored, echoed or put in a URL (the route takes it in the body, like
``POST /travelers/search``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.errors import Invalid
from app.vault.blind_index import NormalisationError, name_tokens, normalise_email, normalise_phone

Mode = Literal["email", "phone", "text", "too_short"]

_PHONE_LIKE = re.compile(r"^\+[\d\s().-]*$")
_WILDCARDS = ("*", "%")


@dataclass(frozen=True)
class Term:
    """The cleaned term and how to use it. ``name_tokens``: the term's name tokens (text mode, may be empty)."""

    text: str
    mode: Mode
    name_tokens: tuple[str, ...] = ()


def classify(raw: str, *, max_chars: int, min_text_chars: int) -> Term:
    """Classify ``raw`` (``max_chars`` / ``min_text_chars``: the platform settings, read by the caller)."""
    text = " ".join(raw.split())
    if not text:
        raise Invalid("invalid_query", "type something to search", field="q")
    if len(text) > max_chars:
        raise Invalid("query_too_long", f"at most {max_chars} characters", field="q")
    if any(w in text for w in _WILDCARDS):
        raise Invalid("partial_search_refused", "search a whole word, e-mail or phone number (no wildcards)", field="q")
    if "@" in text:
        try:
            normalise_email(text)
        except NormalisationError:
            raise Invalid("partial_contact_search", "search a whole e-mail address", field="q") from None
        return Term(text, "email")
    if _PHONE_LIKE.match(text):
        try:
            normalise_phone(text)
        except NormalisationError:
            raise Invalid(
                "partial_contact_search", "search a whole international phone number (+ and country code)", field="q"
            ) from None
        return Term(text, "phone")
    if len(text) < min_text_chars:
        return Term(text, "too_short")
    try:
        tokens = tuple(name_tokens(text))
    except NormalisationError:
        tokens = ()
    return Term(text, "text", tokens)
