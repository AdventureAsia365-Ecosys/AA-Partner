"""Place names that contain a common given name ("Hội An", "Ninh Bình", "Mai Châu", "Hà Giang").

Pass 1 replaces a traveller's given name written alone with a capital ("chị Bình"). Inside one of these place names the
single word is the place, not the person, so a one-word match there is skipped. A known person's **full** name (two
or more words) is always replaced, even when it equals a place ("Hà Giang" the traveller): pass 1 trusts what the
agency stored. Matched folded, so "Hoi An" and "Hội An" are both protected.
"""

from __future__ import annotations

import re

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
