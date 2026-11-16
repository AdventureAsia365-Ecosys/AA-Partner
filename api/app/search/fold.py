"""Accent- and case-insensitive matching of plaintext fields (design v3 §2.3: "accent-insensitive on non-PII fields";
card CRM-32 E: "Đà Lạt" = "Da Lat").

The fold is one SQL expression applied to BOTH sides of a comparison (the column and the query), so the two always
fold the same way, whatever the database's collation:

    NFKD, combining marks (U+0300-U+036F) removed, đ/Đ ø/Ø ł/Ł -> d o l, ß -> ss, lower-cased.

:func:`fold` is the same rule in Python, for the tests and the documentation of the web's copy
(``web/lib/search.ts`` ``foldText``, the Content pieces filter). No index is built on a folded column: the search
reads one agency's rows (FORCE RLS) and stops at the per-kind cap.

Two ways to compare (:func:`contains_sql`, :func:`words_sql`):

- ``contains``: the folded query is a substring of the folded column. Only for fields that hold no personal data.
- ``words``: every word of the folded query is a whole word of the folded column ("Da Lat" finds "Đà Lạt honeymoon",
  "Lat" alone does not). For plaintext fields tagged ``pii:`` that are not identity fields (``crm.trip.title``,
  ``pii:travel``): a fragment never matches, so a title cannot be probed letter by letter.

Identity fields (F1: names, e-mails, phones) are never compared here: they are encrypted and searched through their
blind indexes only (:mod:`app.vault.blind_index`).
"""

from __future__ import annotations

import re
import unicodedata

# translate() is one character to one character; ß -> ss needs replace().
_FROM = "đĐøØłŁ"
_TO = "ddooll"
_PY_FOLD = str.maketrans({**{a: b for a, b in zip(_FROM, _TO, strict=True)}, "ß": "ss", "ẞ": "ss"})
# Words are split on white space and punctuation (the database's [[:space:][:punct:]] under its UTF-8 ctype; the
# Python mirror splits on anything that is not a letter or a digit).
_WORD_SPLIT_SQL = "[[:space:][:punct:]]+"
_WORD_SPLIT = re.compile(r"[\W_]+")


def fold_sql(expr: str) -> str:
    """The folded form of the SQL expression ``expr`` (a column or a ``$n`` parameter)."""
    return (
        "lower(regexp_replace(normalize(replace(replace(translate("
        f"{expr}, '{_FROM}', '{_TO}'), 'ß', 'ss'), 'ẞ', 'ss'), NFKD), '[\\u0300-\\u036f]', '', 'g'))"
    )


def words_sql(expr: str) -> str:
    """The folded words of ``expr`` as a text[] (empty words dropped)."""
    return f"array_remove(regexp_split_to_array({fold_sql(expr)}, '{_WORD_SPLIT_SQL}'), '')"


def contains_sql(column: str, param: str) -> str:
    """``column`` contains ``param`` once both are folded (fields without personal data only)."""
    return f"strpos({fold_sql(column)}, {fold_sql(param)}) > 0"


def all_words_sql(column: str, param: str) -> str:
    """Every word of ``param`` is a whole word of ``column``, both folded (``pii:`` plaintext that is not identity)."""
    return f"({words_sql(column)} @> {words_sql(param)} AND cardinality({words_sql(param)}) > 0)"


def fold(text: str) -> str:
    """:func:`fold_sql` in Python (tests, documentation)."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_PY_FOLD))
    return "".join(c for c in decomposed if not 0x300 <= ord(c) <= 0x36F).lower()


def words(text: str) -> list[str]:
    """:func:`words_sql` in Python."""
    return [w for w in _WORD_SPLIT.split(fold(text)) if w]
