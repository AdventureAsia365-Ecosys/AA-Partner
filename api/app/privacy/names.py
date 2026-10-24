"""Name lists the F3 scrubber reasons with: Vietnamese family names, honorifics, common given names.

Pass 1 (:mod:`app.privacy.scrubber`) reads a display name family-first when it starts with a family name and matches a
family name alone only after an honorific. Option B (AA-938 S250, :mod:`app.privacy.places`) uses the same lists the
other way round: :func:`name_like` says whether a place could also be read as a person's name ("Hà Giang", "Mai
Châu", "Long An"), and such a place is never left in clear.

Every set exists twice: **folded** (no diacritics, :func:`app.privacy.text.fold`; "Hà" -> ``ha``) and **marked**
(NFC, case folded, diacritics kept; "Hà" -> ``hà``). A word written with its marks is compared marked, so "Hạ" (in
"Hạ Long") is not the family name "Hà"; a word written without marks could stand for any of its marked spellings, so
it is compared folded ("Ha" may be "Hà").
"""

from __future__ import annotations

import re
import unicodedata

from app.privacy.text import fold

_VN_FAMILY = (
    "Nguyễn Trần Lê Phạm Hoàng Huỳnh Phan Vũ Võ Đặng Bùi Đỗ Hồ Ngô Dương Lý Đào Đoàn Vương Trịnh Trương Lâm Phùng "
    "Mai Tô Hà Tạ Lương Đinh Cao Châu Quách Thái Văn Kiều Lại Lục Mạc Triệu Từ Chu Hứa La Tăng Thân Kim"
).split()
# Family names that are rarer as a display name's first word, so pass 1 does not read a name family-first on them,
# but that still make a place name-like when it starts with one ("Ninh Bình", "Cù ...", "Khúc ...").
_VN_FAMILY_EXTRA = (
    "Ninh Cù Khúc Âu Bạch Bế Giáp Hàn Hầu Khổng Kha Lò Lưu Mã Nông Ông Quan Tôn Trình Viên Ứng Yên".split()
)
# Given names that are also words of Vietnamese place names (provinces, towns, destinations). A place whose every word
# is one of them reads as a person's given name(s) ("Long An", "Thanh Hóa", "Hải Dương", "Phú Quốc", "Huế").
_VN_GIVEN = (
    "An Anh Bảo Bích Bình Châu Chi Cúc Cường Diệp Dũng Duy Dương Đào Đông Đồng Đức Giang Hà Hải Hạnh Hằng Hiền Hiếu "
    "Hòa Hoà Hoa Hóa Hoá Hoàng Hồng Huệ Huế Hùng Hưng Hương Huy Huyền Khang Khánh Khoa Kiên Lâm Lan Liên Linh Loan "
    "Lộc Long Mai Minh My Mỹ Nam Nga Ngân Nghĩa Ngọc Nhàn Nhung Ninh Phát Phong Phú Phúc Phước Phương Quang Quân Quốc "
    "Quỳnh Sang Sơn Tài Tâm Tân Thái Thắng Thanh Thành Thảo Thịnh Thọ Thơ Thu Thuận Thủy Thuỷ Tiến Tín Tĩnh Toàn "
    "Trang Trinh Trung Tú Tuấn Tùng Tuyên Tuyết Vân Việt Vinh Vĩnh Vy Xuân Yên Yến"
).split()
# Common middle names: a capitalised one right before a destination ("Nguyễn Thị Nha Trang", "Lê Văn Hội An") makes
# the destination the end of a full name (option B, :mod:`app.privacy.scrubber`). "Mỹ" is left out: unmarked it reads
# "My", the English word that starts many sentences ("My Hoi An trip").
_VN_MIDDLE = (
    "Thị Văn Đức Hữu Ngọc Minh Công Quang Xuân Thanh Kim Hoàng Gia Bảo Đình Quốc Mạnh Trọng Như Diệu Phương Hồng "
    "Thu Anh"
).split()
HONORIFIC_WORDS = (
    "anh",
    "chi",
    "em",
    "ong",
    "ba",
    "co",
    "chu",
    "bac",
    "di",
    "thay",
    "ban",
    "mr",
    "mrs",
    "ms",
    "miss",
    "mx",
    "dr",
    "sir",
    "madam",
    "mme",
    "herr",
    "frau",
)

# The same honorifics with their marks (and "khách", a guest): option B reads the text before a destination marked, so
# "đi Sa Pa" (go to Sa Pa) is not "dì Sa Pa" (aunt Sa Pa); an unmarked "di" may be either and counts as an honorific.
HONORIFIC_WORDS_MARKED = ("anh", "chị", "em", "ông", "bà", "cô", "chú", "bác", "dì", "thầy", "bạn", "khách")


def mark(word: str) -> str:
    """NFC, case folded, diacritics kept: "Hạ" -> ``hạ``, never ``ha``."""
    return unicodedata.normalize("NFC", word).casefold()


VN_FAMILY_NAMES = frozenset(fold(n) for n in _VN_FAMILY)  # folded; pass 1 reads names family-first on these
FAMILY_FOLDED = VN_FAMILY_NAMES | {fold(n) for n in _VN_FAMILY_EXTRA}
FAMILY_MARKED = frozenset(mark(n) for n in (*_VN_FAMILY, *_VN_FAMILY_EXTRA))
GIVEN_FOLDED = frozenset(fold(n) for n in _VN_GIVEN)
GIVEN_MARKED = frozenset(mark(n) for n in _VN_GIVEN)
MIDDLE_FOLDED = frozenset(fold(n) for n in _VN_MIDDLE)
MIDDLE_MARKED = frozenset(mark(n) for n in _VN_MIDDLE)
_WORD = re.compile(r"[^\W\d_]+")


def _is(word: str, folded: frozenset[str], marked: frozenset[str]) -> bool:
    """``word`` written without marks may be any marked spelling (compared folded); with marks, only itself."""
    return fold(word) in folded if word.isascii() else mark(word) in marked


def name_like(phrase: str) -> bool:
    """``phrase`` could be read as a person's name: its first word is a family name or an honorific ("Hà Giang", "Mai
    Châu", "Ninh Bình", "Ba Bể"), or every word is a common given name ("Long An", "Thanh Hóa", "Huế"). A phrase with no
    word at all is name-like too (fail closed). Read in NFC, so a decomposed spelling gives the same answer."""
    words = _WORD.findall(unicodedata.normalize("NFC", phrase))
    if not words:
        return True
    first = words[0]
    if _is(first, FAMILY_FOLDED, FAMILY_MARKED) or fold(first) in HONORIFIC_WORDS:
        return True
    return all(_is(w, GIVEN_FOLDED, GIVEN_MARKED) for w in words)


def name_part(word: str, *, middle: bool) -> bool:
    """``word`` can stand inside a Vietnamese full name next to a given name: a family name ("Lê" in "Lê Hội An",
    "Nguyễn" in "Hội An Nguyễn") or, with ``middle``, a common middle name ("Thị" in "Nguyễn Thị Nha Trang")."""
    return _is(word, FAMILY_FOLDED, FAMILY_MARKED) or (middle and _is(word, MIDDLE_FOLDED, MIDDLE_MARKED))
