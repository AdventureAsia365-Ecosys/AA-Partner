"""Place names that contain a common given name ("Hội An", "Ninh Bình", "Mai Châu", "Hà Giang").

Pass 1 replaces a traveller's given name written alone with a capital ("chị Bình"). Inside one of these place names the
single word is the place, not the person, so a one-word match there is skipped. A known person's **full** name (two
or more words) is always replaced, even when it equals a place ("Hà Giang" the traveller): pass 1 trusts what the
agency stored. Matched folded, so "Hoi An" and "Hội An" are both protected.

:func:`is_place` serves pass 3 (option C, AA-938 S249): a detector ``ADDRESS`` finding that is exactly one destination
is still replaced, but as ``<PLACE_n>`` instead of ``<ADDRESS_n>``, so the model knows a place stood there.

:func:`is_keepable_place` serves pass 3 too (option B, AA-938 S250): a ``NAME`` or ``ADDRESS`` finding that is exactly
one of the trip's own destinations (:data:`KEEPABLE_PLACES`, by the trip's countries) and cannot be read as a person's
name is left in clear.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.privacy.names import mark, name_like
from app.privacy.text import fold

_PLACES = """
An Giang|Bà Rịa|Vũng Tàu|Bạc Liêu|Bắc Giang|Bắc Kạn|Bắc Ninh|Bến Tre|Bình Định|Bình Dương|Bình Phước|Bình Thuận|Cà Mau|
Cần Thơ|Cao Bằng|Đà Nẵng|Đắk Lắk|Đắk Nông|Điện Biên|Đồng Nai|Đồng Tháp|Gia Lai|Hà Giang|Hà Nam|Hà Nội|Hà Tĩnh|
Hải Dương|Hải Phòng|Hậu Giang|Hòa Bình|Hưng Yên|Khánh Hòa|Kiên Giang|Kon Tum|Lai Châu|Lâm Đồng|Lạng Sơn|Lào Cai|
Long An|Nam Định|Nghệ An|Ninh Bình|Ninh Thuận|Phú Thọ|Phú Yên|Quảng Bình|Quảng Nam|Quảng Ngãi|Quảng Ninh|Quảng Trị|
Sóc Trăng|Sơn La|Tây Ninh|Thái Bình|Thái Nguyên|Thanh Hóa|Thừa Thiên Huế|Tiền Giang|Trà Vinh|Tuyên Quang|Vĩnh Long|
Vĩnh Phúc|Yên Bái|Hồ Chí Minh|Sài Gòn|Hội An|Hạ Long|Sa Pa|Mai Châu|Phú Quốc|Côn Đảo|Đà Lạt|Mũi Né|Nha Trang|Tam Cốc|
Tràng An|Bái Đính|Phong Nha|Mộc Châu|Mù Cang Chải|Bắc Hà|Đồng Văn|Mèo Vạc|Cát Bà|Lan Hạ|Bái Tử Long|Ba Bể|Pù Luông|
Cù Lao Chàm|Mỹ Sơn|Mỹ Tho|Châu Đốc|Cái Răng|Quy Nhơn|Kim Liên|Văn Miếu|Hoàn Kiếm|Tây Hồ|Long Biên|Bình Thạnh|Thủ Đức|
Phan Thiết|Tuy Hòa|Kontum|Luang Prabang|Siem Reap|Phnom Penh|Vientiane|Chiang Mai|Hoi An Ancient Town
"""
PLACE_PHRASES = tuple(sorted({fold(p.strip()) for p in _PLACES.replace("\n", "").split("|") if p.strip()}, key=len))
PLACE_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(p).replace(r"\ ", r"[\s.\-]+") for p in reversed(PLACE_PHRASES)) + r")(?!\w)"
)


def place_spans(folded: str) -> list[tuple[int, int]]:
    """Folded ``[start, end)`` of every known place phrase in already folded text."""
    return [(m.start(), m.end()) for m in PLACE_RE.finditer(folded)]


# Option C (AA-938, S249): a detector ADDRESS finding that is exactly one destination becomes ``<PLACE_n>``. Typing
# never unmasks anything, so this list may be wider than PLACE_PHRASES: it adds English spellings and destinations
# whose single word is also a common given name ("Huế"). Those must NOT join PLACE_PHRASES, where they would make
# pass 1 skip a traveller called Huế.
_TYPED_ONLY = """
Huế|Sapa|Halong|Ha Long Bay|Halong Bay|Hạ Long Bay|Lan Ha Bay|Bai Tu Long Bay|Hanoi|Saigon|Ho Chi Minh City|Danang|
Dalat|Phu Quoc Island|Mekong|Mekong Delta|Cu Chi|Ninh Binh Province|Vietnam|Việt Nam|Laos|Cambodia|Thailand|Myanmar|
Bangkok|Angkor|Angkor Wat
"""
TYPED_PLACE_PHRASES = tuple(
    sorted(
        set(PLACE_PHRASES) | {fold(p.strip()) for p in _TYPED_ONLY.replace("\n", "").split("|") if p.strip()}, key=len
    )
)
TYPED_PLACE_RE = re.compile(
    "(?:" + "|".join(re.escape(p).replace(r"\ ", r"[\s.\-]+") for p in reversed(TYPED_PLACE_PHRASES)) + ")"
)
_TRIM = " \t\r\n,;:.()[]\"'"


def is_place(value: str) -> bool:
    """``value`` (outer punctuation and spaces ignored) is exactly one known destination, matched folded ("Hoi An" =
    "Hội An"). A place plus anything else ("45 Hải Phòng, Đà Nẵng", "Ninh Bình + Hải Dương") is not."""
    return TYPED_PLACE_RE.fullmatch(fold(value.strip(_TRIM))) is not None


# Option B (AA-938, S250): the trip's own destinations, left in clear when the detector tags one NAME or ADDRESS.
# ``crm.trip.destinations`` holds country codes (``app.intake.stage1.DESTINATIONS``), so the scope is the trip's
# countries, and within them only this explicit list: a place not listed is never kept (fail closed). Every phrase must
# not read as a person's name (:func:`app.privacy.names.name_like`: first word a family name or an honorific, or every
# word a common given name), enforced by a test and checked again at run time, so "Hà Giang", "Mai Châu", "Ninh Bình",
# "Phú Quốc", "Huế", "Long An", "Thanh Hóa", "Hải Dương", "Kim Liên" are never kept. Spellings are listed as written:
# a phrase is matched case-insensitively with its diacritics ("Hạ Long" is kept, "Hà Long" is not), and an unmarked
# spelling ("Ha Long") is listed only when it passes the rule read folded (it does not: "Ha" may be "Hà").
# The name lists are Vietnamese, so the other countries' entries were screened by hand for use as personal names
# (S250 review): "Mandalay", "Paro" and "Bali" are given names too and are left out; a destination nobody listed is
# masked as in f3-2, which costs only an over-scrub.
KEEPABLE_PLACES: dict[str, tuple[str, ...]] = {
    "vietnam": (
        "Hạ Long",
        "Vịnh Hạ Long",
        "Hạ Long Bay",
        "Halong",
        "Halong Bay",
        "Sa Pa",
        "Sapa",
        "Hội An",
        "Hoi An",
        "Hội An Ancient Town",
        "Hoi An Ancient Town",
        "Đà Lạt",
        "Da Lat",
        "Dalat",
        "Đà Nẵng",
        "Da Nang",
        "Danang",
        "Hanoi",
        "Saigon",
        "Sài Gòn",
        "Tam Cốc",
        "Tràng An",
        "Mù Cang Chải",
        "Cát Bà",
        "Cat Ba",
        "Côn Đảo",
        "Con Dao",
        "Nha Trang",
        "Mũi Né",
        "Mui Ne",
        "Phong Nha",
        "Mộc Châu",
        "Pù Luông",
        "Pu Luong",
        "Mekong",
        "Mekong Delta",
    ),
    "cambodia": ("Siem Reap", "Phnom Penh", "Angkor", "Angkor Wat", "Battambang", "Kampot", "Tonle Sap"),
    "laos": ("Luang Prabang", "Vientiane", "Vang Vieng", "Pakse"),
    "thailand": ("Bangkok", "Chiang Mai", "Chiang Rai", "Phuket", "Krabi", "Ayutthaya", "Sukhothai"),
    "myanmar": ("Yangon", "Bagan", "Inle Lake"),
    "bhutan": ("Thimphu", "Punakha"),
    "nepal": ("Kathmandu", "Pokhara", "Chitwan"),
    "indonesia": ("Ubud", "Yogyakarta", "Borobudur", "Komodo"),
}
_SEPARATORS = re.compile(r"[\s.\-]+")


def keep_key(value: str) -> str:
    """The comparison form of a keepable phrase: outer punctuation off, separators one space, NFC, case folded,
    diacritics kept ("Sa-Pa" -> ``sa pa``, "HỘI AN" -> ``hội an``)."""
    return _SEPARATORS.sub(" ", mark(value.strip(_TRIM))).strip()


_KEEPABLE = {country: frozenset(keep_key(p) for p in phrases) for country, phrases in KEEPABLE_PLACES.items()}


def is_keepable_place(value: str, countries: Sequence[str]) -> bool:
    """``value`` is exactly one listed destination of one of ``countries`` (the trip's), matched with its diacritics,
    and does not read as a person's name. No countries -> never."""
    key = keep_key(value)
    return (
        bool(key)
        and any(key in _KEEPABLE.get(country, ()) for country in countries)
        and not name_like(value.strip(_TRIM))
    )
