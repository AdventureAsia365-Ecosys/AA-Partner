"""Pass 3's local half (S286, scrubber f3-4): person names the guardrail may miss, found without it.

The guardrail is not stable on Vietnamese names: the nightly corpus run of 20/11/2026, with the same guardrail version
and corpus as the green run before it, lost every place-like name in lists and after person words and one full name
with diacritics. These rules are local, free and deterministic; :mod:`app.privacy.scrubber` turns their values into
``NAME`` findings that go through the same placing, merging and option B rules as the guardrail's, and drops a value
the guardrail already covers.
"""

from __future__ import annotations

import re
import unicodedata

from app.privacy import names, patterns
from app.privacy.places import TYPED_PLACE_PHRASES, is_place

_LETTERS = re.compile(r"[^\W\d_]+")
_FULL_NAME_WORDS = (2, 4)  # a Vietnamese full name: the family name and one to three more words
# Titles that are everyday English words in lower case ("don't miss Hội An"): a cue only when capitalised.
_TITLES = ("Mr", "Mrs", "Ms", "Miss", "Mx", "Dr", "Sir", "Madam", "Mme", "Herr", "Frau")
# Words for a person that open a list of people (rule b). PERSON_CUES already holds "khách", "traveller(s)", the
# honorifics with their marks, kin and naming words; a room line ("Room 1:", "Phòng 2:") lists who sleeps there.
# Unmarked "ten" (tên, name) is left out here: it is the English number ("top ten Hội An cafés").
_LIST_CUES = (*(c for c in names.PERSON_CUES if c != "ten"), "danh sách khách", "danh sach khach")
_CUE = re.compile(
    r"(?:(?i:(?<!\w)(?:"
    + "|".join(re.escape(c).replace(r"\ ", r"\s+") for c in sorted(set(_LIST_CUES), key=len, reverse=True))
    + r")(?!\w))|(?<!\w)(?:"
    + "|".join(sorted(_TITLES, key=len, reverse=True))
    + r")(?!\w)\.?|(?i:(?<!\w)(?:room|phòng|phong)\s*\d+(?!\w)))"
    # "chồng là Thái Bình", "husband is ...", then a little punctuation: "Name: ", "(Mrs) ", "Mr." (no sentence end)
    r"(?:\s+(?:là|la|is))?[^\w<>\n.!?]{0,4}"
)
_CUE_BEFORE = re.compile(r"(?:" + _CUE.pattern + r")$")
_CUE_BEFORE_WINDOW = 32
# Items of one list of people: "A, B", "A + B", "A & B", "A and B", "A và B".
_BLANKS = re.compile(r"[ \t]+")
_LIST_SEP = re.compile(r"\s*(?:,|\+|&|(?<!\w)and(?!\w)|(?<!\w)và(?!\w))\s*")
# A list item longer than any known place is not one, and is never compared with the list (that keeps the walk linear).
# Twice the longest place, so a spelling split into more words (a stray combining mark) is still compared.
_MAX_PLACE_WORDS = 2 * max(len(_LETTERS.findall(p)) for p in TYPED_PLACE_PHRASES)


def person_names(text: str) -> list[str]:
    """Person names found without the detector (each value once, in order of appearance):

    - (a) a capitalised run of 2-4 words, whitespace only between, whose first word is a common Vietnamese family name
      written with its marks ("Phạm Gia Bảo", "TRẦN MINH KHOA"), unless the run, a prefix of two or more of its words
      ("Hà Nội Old Quarter") or the words after the family name ("Từ Hà Nội", "Hồ Hoàn Kiếm") is a known place
      (:func:`app.privacy.places.is_place`). After a word for a person it is a name even then ("Khách Lê Hội An").
      An unmarked first word ("Pham", "Le", "To") is left to the detector: it is also everyday English.
    - (b) a known place right after a word for a person (:data:`app.privacy.names.PERSON_CUES`, a capitalised
      title, "room 1", "danh sách khách"), and every later name-like place (:func:`app.privacy.names.name_like`) of
      the same list joined by ",", "+", "&", "and", "và": "Traveller Hà Giang", "chồng là Thái Bình", "Room 1: Ninh
      Bình + Hải Dương", "Mrs Hà Giang, Mr Long An". A title opening a list item is skipped and counts as a cue ("Mrs
      Hà Giang"); a placeholder of the earlier passes is an item too ("Khách <PERSON_1> và Long An").

    The text is read in NFC; pass 3 locates every value in the original spelling."""
    text = unicodedata.normalize("NFC", text)
    found: dict[str, None] = {}
    for value in (*_full_names(text), *_cued_places(text)):
        found.setdefault(value, None)
    return sorted(found, key=text.find)


def _after_cue(text: str, start: int) -> bool:
    return _CUE_BEFORE.search(text[max(0, start - _CUE_BEFORE_WINDOW) : start]) is not None


def _full_names(text: str) -> list[str]:
    out = []
    tokens = list(_LETTERS.finditer(text))
    i = 0
    while i < len(tokens):
        first = tokens[i].group(0)
        if not (first[0].isupper() and not first.isascii() and names.common_family_name(first)):
            i += 1
            continue
        j = i
        while (
            j + 1 < len(tokens)
            and j + 1 - i < _FULL_NAME_WORDS[1]
            and tokens[j + 1].group(0)[0].isupper()
            and text[tokens[j].end() : tokens[j + 1].start()].isspace()
        ):
            j += 1
        if j + 1 - i >= _FULL_NAME_WORDS[0]:
            words = [t.group(0) for t in tokens[i : j + 1]]
            if _after_cue(text, tokens[i].start()) or not _place_run(words):
                out.append(text[tokens[i].start() : tokens[j].end()])
        i = j + 1
    return out


def _place_run(words: list[str]) -> bool:
    """The run is a place, starts with one ("Hà Nội Old Quarter") or is a family-name word before one ("Từ Hà Nội")."""
    return any(is_place(" ".join(words[:k])) for k in range(2, len(words) + 1)) or is_place(" ".join(words[1:]))


def _cued_places(text: str) -> list[str]:
    """Linear in the text (review S286: a walk per cue was quadratic on "Con, Con, ..." and stalled the event loop).

    Every capitalised run is measured once (:class:`_Runs`); a list position reached after a separator is walked only
    once, because what follows it does not depend on where the walk started (only a list's first item is cued by the
    word before it); and a run longer than any known place is never compared with the place list."""
    out = []
    runs = _Runs(text)
    walked: set[int] = set()  # positions after a list separator: the rest of that list is already read
    after: dict[int, int | None] = {}  # item end -> position after its separator (None: the list ends there)
    for m in _CUE.finditer(text):
        pos, cued = m.end(), True
        while True:
            held = patterns.PLACEHOLDER.match(text, pos)  # a person of the earlier passes: the list goes on
            if held:
                end = held.end()
            else:
                item = runs.item(pos)
                if item is None:
                    break
                start, end, titled = item
                # Right after a cue any place is a person; later in the list only a name-like one ("Khách Lê, Hội An
                # là điểm đầu" keeps the trip's own Hội An, "Travellers: Mai Châu and Thái Bình" masks both). A title
                # opening the item ("Mrs Hà Giang") is a cue of its own.
                value = text[start:end]
                if start < end and is_place(value) and (cued or titled or names.name_like(value)):
                    out.append(value)
            if end not in after:
                sep = _LIST_SEP.match(text, end)
                after[end] = sep.end() if sep else None
            nxt = after[end]
            if nxt is None or nxt in walked:
                break
            walked.add(nxt)
            pos, cued = nxt, False
    return out


class _Runs:
    """The capitalised runs of ``text`` (letter words, spaces or tabs only between them), measured once."""

    def __init__(self, text: str):
        self.text = text
        self.tokens = list(_LETTERS.finditer(text))
        self.index = {t.start(): i for i, t in enumerate(self.tokens)}
        n = len(self.tokens)
        self.last = list(range(n))  # i -> the last word of the capitalised run that word i opens
        self.untitled = list(range(n))  # i -> the first word from i on that is not a title (last + 1: none)
        for i in range(n - 1, -1, -1):
            if i + 1 < n and self._joined(i):
                self.last[i] = self.last[i + 1]
            if self.tokens[i].group(0) in _TITLES:
                self.untitled[i] = self.untitled[i + 1] if self.last[i] > i else i + 1

    def _joined(self, i: int) -> bool:
        a, b = self.tokens[i], self.tokens[i + 1]
        return b.group(0)[0].isupper() and _BLANKS.fullmatch(self.text, a.end(), b.start()) is not None

    def item(self, pos: int) -> tuple[int, int, bool] | None:
        """The capitalised run at ``pos`` without the titles opening it: ``(start, end, titled)``, an empty span when
        it is titles only or longer than any known place, None when no capitalised word starts at ``pos``."""
        i = self.index.get(pos)
        if i is None:
            return self._item_inside(pos)
        if not self.tokens[i].group(0)[0].isupper():
            return None
        last, k = self.last[i], self.untitled[i]
        end = self.tokens[last].end()
        if k > last or last - k + 1 > _MAX_PLACE_WORDS:
            return end, end, k > i
        return self.tokens[k].start(), end, k > i

    def _item_inside(self, pos: int) -> tuple[int, int, bool] | None:
        """``pos`` inside a word ("Mr isÀ": the cue took "is"): that word's rest, joined to the run after it."""
        m = _LETTERS.match(self.text, pos)
        if not m or not m.group(0)[0].isupper():
            return None
        gap = _BLANKS.match(self.text, m.end())
        j = self.index.get(gap.end()) if gap else None
        if j is None or not self.tokens[j].group(0)[0].isupper():
            if m.group(0) in _TITLES:
                return m.end(), m.end(), True
            return pos, m.end(), False
        last, end = self.last[j], self.tokens[self.last[j]].end()
        if m.group(0) not in _TITLES:
            return (end, end, False) if last - j + 2 > _MAX_PLACE_WORDS else (pos, end, False)
        k = self.untitled[j]
        if k > last or last - k + 1 > _MAX_PLACE_WORDS:
            return end, end, True
        return self.tokens[k].start(), end, True
