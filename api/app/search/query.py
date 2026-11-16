"""What a global search term is (design v3 §2.3 A-00, §4.2; card CRM-32 R: no partial F1 search).

:func:`classify` reads the term once and says how each kind may use it. It never guesses a fragment of an identity
field:

- a term with ``@`` and no white space is an e-mail: a whole, valid address is searched through the e-mail blind index;
  anything else (``@example.test``, ``anna@``) is refused (422 ``partial_contact_search``), and so is one with a
  wildcard (``*@example.test``: 422 ``partial_search_refused``);
- a term that starts with ``+`` and is otherwise digits and separators is a phone: a valid international number is
  searched through the phone blind index (E.164); anything else is refused (422 ``partial_contact_search``);
- any other term is text: whole name tokens through the name token blind index (clients), and the plaintext kinds
  (:mod:`app.search.kinds`). A run of digits without ``+`` is never tried as a phone (it is more often a supplier's
  confirmation number), so it searches references and text only.

Two shapes of a text term never reach the client kind (``Term.clients`` False, ``Term.notice`` says why, the plaintext
kinds still run):

- a wildcard (``*`` or ``%``) asks for a partial match: the plaintext kinds compare with ``strpos`` / whole words /
  exact references, never ``LIKE``, so ``100% Vietnam`` is plain text there; clients are not searched
  (``partial_search_refused``);
- an ``@`` inside a term with white space (``Café @ Hội An``) is not an e-mail: clients are not searched
  (``partial_contact_search``).

Length: the term is first collapsed (white space runs -> one space). Shorter than the platform minimum
(``crm.search.min_text_chars``) -> mode ``too_short`` (nothing is read). Long enough raw but shorter than the minimum
once accent-folded (:func:`app.search.fold.fold`: lone combining marks fold to nothing, an NFD ``à`` to ``a``) -> 422
``query_too_short``: a folded empty or one-letter needle would match nearly every plaintext record.

The term is never logged, stored, echoed or put in a URL (the route takes it in the body, like
``POST /travelers/search``; refusals carry codes only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.errors import Invalid
from app.search.fold import fold
from app.vault.blind_index import NormalisationError, name_tokens, normalise_email, normalise_phone

Mode = Literal["email", "phone", "text", "too_short"]
Notice = Literal["partial_search_refused", "partial_contact_search"]

_PHONE_LIKE = re.compile(r"^\+[\d\s().-]*$")
_WILDCARDS = ("*", "%")


@dataclass(frozen=True)
class Term:
    """The cleaned term and how to use it. ``name_tokens``: the term's name tokens (text mode, may be empty);
    ``clients``: whether the client kind may use the term; ``notice``: why it may not (a refusal code, no value)."""

    text: str
    mode: Mode
    name_tokens: tuple[str, ...] = ()
    clients: bool = True
    notice: Notice | None = None


def classify(raw: str, *, max_chars: int, min_text_chars: int) -> Term:
    """Classify ``raw`` (``max_chars`` / ``min_text_chars``: the platform settings, read by the caller)."""
    text = " ".join(raw.split())
    if not text:
        raise Invalid("invalid_query", "type something to search", field="q")
    if len(text) > max_chars:
        raise Invalid("query_too_long", f"at most {max_chars} characters", field="q")
    has_space = " " in text
    if "@" in text and not has_space:
        if any(w in text for w in _WILDCARDS):
            raise Invalid("partial_search_refused", "search a whole e-mail address (no wildcards)", field="q")
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
    floor = max(1, min_text_chars)
    if len(text) < floor:
        return Term(text, "too_short")
    if len(fold(text).strip()) < floor:
        raise Invalid("query_too_short", f"type at least {floor} letters or digits", field="q")
    notice: Notice | None = None
    if any(w in text for w in _WILDCARDS):
        notice = "partial_search_refused"
    elif "@" in text:
        notice = "partial_contact_search"
    if notice is not None:
        return Term(text, "text", (), clients=False, notice=notice)
    try:
        tokens = tuple(name_tokens(text))
    except NormalisationError:
        tokens = ()
    return Term(text, "text", tokens)
