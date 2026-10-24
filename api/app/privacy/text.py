"""Folding for matching: Vietnamese diacritics stripped (``đ`` -> ``d``), case folded, with a map back to the original.

"Nguyễn Văn An", "Nguyen Van An", "NGUYEN VAN AN" and the NFD spelling of the first all fold to ``nguyen van an``.
:func:`fold_with_map` keeps, for every folded character, the index of the original character it came from, so a match
found in the folded text is replaced in the original text (the agency's own spelling is what the map restores).
"""

from __future__ import annotations

import unicodedata

_SPECIAL = {"đ": "d", "Đ": "d", "ð": "d", "Ð": "d", "ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "æ": "ae", "Æ": "ae"}


def fold_char(ch: str) -> str:
    if ch in _SPECIAL:
        return _SPECIAL[ch]
    decomposed = unicodedata.normalize("NFKD", ch)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def fold(text: str) -> str:
    return "".join(fold_char(ch) for ch in text)


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """``(folded, index)``: ``index[i]`` is the position in ``text`` of the character folded position ``i`` came from;
    ``index[len(folded)] == len(text)`` (so a match end maps too)."""
    out: list[str] = []
    index: list[int] = []
    for pos, ch in enumerate(text):
        folded = fold_char(ch)
        out.append(folded)
        index.extend([pos] * len(folded))
    index.append(len(text))
    return "".join(out), index


def original_span(index: list[int], start: int, end: int) -> tuple[int, int]:
    """The original ``[start, end)`` of the folded ``[start, end)``. Combining marks after the last base character
    (an NFD spelling) belong to the match."""
    o_start = index[start]
    o_end = index[end - 1] + 1 if end > start else o_start
    return o_start, o_end


def extend_over_marks(text: str, end: int) -> int:
    while end < len(text) and unicodedata.combining(text[end]):
        end += 1
    return end


def digits(text: str) -> str:
    return "".join(c for c in text if c.isdigit())
