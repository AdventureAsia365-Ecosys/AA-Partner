"""Generate the F3 scrubber corpus (card CRM-10: ">= 200 EN/VI samples, 0 leaks"; design v3 §4.5 "CI corpus").

    python -m tests.scrub_corpus.generate          # rewrite corpus.jsonl (deterministic, seed 938)
    python -m tests.scrub_corpus.generate --check  # exit 1 when corpus.jsonl differs from what this script writes

Every value is synthetic: names from common Vietnamese / English name lists, e-mails on ``example.invalid``, phone
numbers in real formats with invented digits, the public card test numbers, invented passport / CCCD digits.

One JSON object per line:

- ``id``, ``lang`` (``vi`` / ``en``), ``text``;
- ``known``: the persons the agency stored for the trip (pass 1 input, :class:`app.privacy.scrubber.KnownPerson`);
- ``pii``: strings that must not survive in the scrubbed text (a leak fails the corpus);
- ``keep``: strings that must survive (places, prices, trip dates, catalog ids: over-scrubbing them breaks drafts);
- ``needs_detector``: true when only pass 3 (Bedrock Guardrails) can find a value (a name nobody stored). The offline
  run (every CI) skips these; the Guardrails run (nightly and on scrubber change) includes them;
- ``destinations``: the trip's destination codes (``crm.trip.destinations``, option B: pass 3 leaves the trip's own
  non-name-like destinations in clear). ``vietnam``, plus ``cambodia`` when the text names Siem Reap;
- ``outside`` (a few samples only): destinations outside the trip's countries; never a ``keep`` string, because pass 3
  masks or types them as before option B (checked offline, ``tests/unit/test_scrub_corpus.py``).
"""

from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

from app.privacy.text import fold

OUT = Path(__file__).with_name("corpus.jsonl")
SEED = 938

VN_PEOPLE = [
    ("Nguyễn", "Văn", "An"),
    ("Trần", "Thị", "Bình"),
    ("Lê", "Minh", "Châu"),
    ("Phạm", "Quốc", "Dũng"),
    ("Hoàng", "Thu", "Hà"),
    ("Huỳnh", "Ngọc", "Lan"),
    ("Võ", "Thanh", "Tùng"),
    ("Đặng", "Thị", "Mai"),
    ("Bùi", "Đức", "Huy"),
    ("Đỗ", "Phương", "Linh"),
    ("Ngô", "Bảo", "Khánh"),
    ("Dương", "Thị", "Hương"),
    ("Lý", "Hoàng", "Nam"),
    ("Phan", "Minh", "Tuấn"),
    ("Vũ", "Thị", "Thảo"),
]
EN_PEOPLE = [
    ("Emily", "Carter"),
    ("James", "O'Brien"),
    ("Sophie", "Müller"),
    ("Liam", "Walker"),
    ("Olivia", "Nguyen"),
    ("Noah", "Thompson"),
    ("Charlotte", "Dubois"),
    ("Oliver", "Bennett"),
    ("Amelia", "Hughes"),
    ("Jack", "Robertson"),
    ("Grace", "Kowalski"),
    ("Henry", "Fitzgerald"),
]
# Persons whose names are also places (the card's E test: a place-like name is caught).
PLACE_LIKE = [
    ("vi", ("Hà", "", "Giang")),
    ("vi", ("Lâm", "", "Đồng")),
    ("vi", ("Đà", "", "Lạt")),
    ("en", ("Florence", "Lyon")),
    ("en", ("Sydney", "Paris")),
    ("en", ("Austin", "Brooklyn")),
]
# Third parties nobody stored: only Guardrails (pass 3) can find them.
STRANGERS_VI = ["Trần Minh Khoa", "Nguyễn Thị Hồng Nhung", "Phạm Gia Bảo", "Lê Hoài Phương"]
STRANGERS_EN = ["Peter Lawson", "Margaret Ellis", "David Okafor", "Hannah Schmidt"]
PLACES_VI = ["Hạ Long", "Sa Pa", "Hội An", "Ninh Bình", "Phú Quốc", "Huế", "Mai Châu", "Hà Giang", "Đà Lạt", "Côn Đảo"]
PLACES_EN = ["Ha Long Bay", "Sapa", "Hoi An", "Ninh Binh", "Phu Quoc", "Hue", "Mekong Delta", "Da Lat", "Siem Reap"]
# Option B adversarial samples (S250): people nobody stored, named like places that read as names, in the texts of
# the S249 live probe (the guardrail tagged every one NAME or ADDRESS). On a Vietnam trip they must stay masked: an
# allow-list widened to a name-like place turns the corpus red with a leak. (text, pii, keep)
NAME_LIKE_STRANGERS = [
    ("vi", "Khách Kim Liên (vợ anh Tuấn) muốn đổi phòng.", ["Kim Liên", "Tuấn"], ["muốn đổi phòng"]),
    ("vi", "Liên hệ chị Hải Dương để nhận vé.", ["Hải Dương"], ["để nhận vé"]),
    ("en", "Our driver Long An will meet you.", ["Long An"], ["will meet you"]),
    ("en", "Travellers: Mai Châu and Thái Bình.", ["Mai Châu", "Thái Bình"], []),
    ("vi", "Danh sách khách: Hà Giang, Kim Liên, Hòa Bình.", ["Hà Giang", "Kim Liên", "Hòa Bình"], []),
    ("en", "Passengers: Mrs Hà Giang, Mr Long An.", ["Hà Giang", "Long An"], []),
    ("en", "Room 1: Ninh Bình + Hải Dương. Room 2: Thanh Hóa.", ["Ninh Bình", "Hải Dương", "Thanh Hóa"], ["Room 2"]),
    ("vi", "Khách Mai Châu và chồng là Thái Bình đi tour Sa Pa.", ["Mai Châu", "Thái Bình"], ["Sa Pa"]),
    ("en", "Traveller Hà Giang wants to visit Hạ Long.", ["Hà Giang"], ["Hạ Long"]),
]
# Option B review (S250, live on acc2 ApplyGuardrail): people named exactly like a keepable destination, after a person
# cue or next to a family name. The guardrail tagged each one, and before the review fix each was left in clear. They
# must stay masked: a cue dropped from scrubber.PERSON_CUES turns the corpus red with a leak. (text, pii, keep)
PERSON_CUE_STRANGERS = [
    ("en", "Mr.Hội An confirmed the booking.", ["Hội An"], ["confirmed the booking"]),
    ("en", "(Mrs) Hội An will join the trip.", ["Hội An"], ["will join the trip"]),
    ("en", "My wife Hội An and I arrive Monday.", ["Hội An"], ["arrive Monday"]),
    ("vi", "Vợ tôi tên Hội An, đi cùng tôi.", ["Hội An"], ["đi cùng tôi"]),
    ("en", "Name: Nha Trang, phone later.", ["Nha Trang"], ["phone later"]),
    ("vi", "Khách Lê Hội An đã cọc 30%.", ["Hội An"], ["30%"]),
]
# Option B: a destination outside the trip's countries is masked or typed as in f3-2. (lang, countries, text, keep,
# outside)
OUTSIDE_TRIP = [
    ("en", ["laos"], "Luang Prabang for 3 nights, then Hoi An by air.", ["Luang Prabang"], ["Hoi An"]),
    ("vi", ["cambodia"], "Đi Siem Reap 3 đêm rồi bay ra Hạ Long.", ["Siem Reap"], ["Hạ Long"]),
    ("en", ["thailand"], "Chiang Mai first, then Sapa in the north of Vietnam.", ["Chiang Mai"], ["Sapa"]),
]
TOURS = ["Hidden Valleys Trek", "Mekong Homestay", "Northern Loop", "Lantern Town Food Walk", "Bay Kayak Day"]
COMPONENT_IDS = [
    "3f2b8c1e-7d4a-4f6b-9a2e-0c5d8e1f2a3b",
    "a716e2c4-5b8d-4e1f-8c3a-012345678901",
    "0b1c2d3e-4f50-4a61-8b72-9c8d7e6f5a40",
]
CARDS = ["4111 1111 1111 1111", "5555-5555-5555-4444", "378282246310005", "4012888888881881"]


def _phones(rng: random.Random) -> list[str]:
    n = rng.choice(["09", "03", "07", "08", "05"]) + str(rng.randint(1, 9)) + f"{rng.randint(0, 9999999):07d}"
    intl = n[1:]
    return [
        f"{n[:4]} {n[4:7]} {n[7:]}",
        n,
        f"+84 {intl[:3]} {intl[3:6]} {intl[6:]}",
        f"+84{intl}",
        f"{n[:4]}.{n[4:7]}.{n[7:]}",
        f"84-{intl[:3]}-{intl[3:6]}-{intl[6:]}",
        f"(028) {rng.randint(3000, 3999)} {rng.randint(1000, 9999)}",
        f"+1 415 555 {rng.randint(1000, 1999)}",
        f"+61 4{rng.randint(10, 99)} {rng.randint(100, 999)} {rng.randint(100, 999)}",
        f"+44 7700 9{rng.randint(10000, 99999)}",
    ]


def _dob(rng: random.Random, lang: str) -> tuple[str, str]:
    y, m, d = rng.randint(1950, 2005), rng.randint(1, 12), rng.randint(1, 28)
    months = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
    if lang == "vi":
        forms = [
            (f"ngày sinh {d:02d}/{m:02d}/{y}", f"{d:02d}/{m:02d}/{y}"),
            (f"sinh ngày {d} tháng {m} năm {y}", f"{d} tháng {m} năm {y}"),
            (f"sinh năm {y}", str(y)),
            (f"NS: {d:02d}-{m:02d}-{y}", f"{d:02d}-{m:02d}-{y}"),
        ]
    else:
        forms = [
            (f"DOB {d:02d}/{m:02d}/{y}", f"{d:02d}/{m:02d}/{y}"),
            (f"born {d} {months[m - 1]} {y}", f"{d} {months[m - 1]} {y}"),
            (f"date of birth: {y}-{m:02d}-{d:02d}", f"{y}-{m:02d}-{d:02d}"),
            (f"birthday {months[m - 1]} {d}, {y}", f"{months[m - 1]} {d}, {y}"),
        ]
    return rng.choice(forms)


def _passport(rng: random.Random, lang: str) -> tuple[str, str]:
    letter = rng.choice("BCKN")
    num = f"{letter}{rng.randint(1000000, 9999999)}"
    us = str(rng.randint(500000000, 599999999))
    if lang == "vi":
        return rng.choice([(f"hộ chiếu số {num}", num), (f"số hộ chiếu: {num}", num), (f"passport {num}", num)])
    return rng.choice([(f"passport no. {num}", num), (f"passport number: {us}", us), (f"PP {num}", num)])


def _cccd(rng: random.Random) -> tuple[str, str]:
    raw = f"0{rng.randint(1, 96):02d}{rng.randint(0, 1)}{rng.randint(50, 99)}{rng.randint(100000, 999999)}"
    spaced = f"{raw[:3]} {raw[3:6]} {raw[6:]}"
    form = rng.choice([raw, spaced])
    return rng.choice([(f"CCCD {form}", form), (f"số căn cước {form}", form), (f"ID card {form}", form)]), form


def _link(rng: random.Random) -> str:
    # Low-entropy on purpose: a random 32-character value after "token=" would trip the gitleaks generic-key rule
    # (entropy > 3.5) on every PR, although it is synthetic.
    tok = "".join(rng.choice("ab12") for _ in range(32))
    return rng.choice(
        [
            f"https://aa-crm-dev-documents.s3.example.invalid/t/doc.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature={tok}",
            f"https://go-dev.example.invalid/c/p/{tok}",
            f"https://agency.example.invalid/reset?token={tok}",
            f"https://pay.example.invalid/checkout/{tok}9",
        ]
    )


def _otp(rng: random.Random, lang: str) -> tuple[str, str]:
    code = str(rng.randint(100000, 999999)) if rng.random() < 0.7 else str(rng.randint(1000, 9999))
    if lang == "vi":
        return rng.choice([(f"mã OTP {code}", code), (f"mã xác nhận là {code}", code)]), code
    return rng.choice([(f"verification code {code}", code), (f"OTP: {code}", code), (f"code {code}", code)]), code


def _person_vi(fam: str, mid: str, giv: str) -> dict:
    display = " ".join(x for x in (fam, mid, giv) if x)
    return {"role": "traveller", "display_name": display, "given_name": giv, "family_name": fam}


def _person_en(giv: str, fam: str) -> dict:
    return {"role": "traveller", "display_name": f"{giv} {fam}", "given_name": giv, "family_name": fam}


def _email(given: str, family: str, rng: random.Random) -> str:
    local = f"{fold(given)}.{fold(family)}".replace("'", "").replace(" ", "")
    if rng.random() < 0.3:
        local = local.upper()
    return f"{local}{rng.randint(1, 99)}@example.invalid"


def build() -> list[dict]:
    rng = random.Random(SEED)  # noqa: S311 — deterministic synthetic data, not crypto
    samples: list[dict] = []

    def add(
        lang: str,
        text: str,
        known: list[dict],
        pii: list[str],
        keep: list[str],
        needs: bool = False,
        destinations: list[str] | None = None,
        outside: list[str] | None = None,
    ) -> None:
        sample = {
            "id": f"{lang}-{len(samples) + 1:03d}",
            "lang": lang,
            "text": text,
            "known": known,
            "pii": [p for p in pii if p],
            "keep": [k for k in keep if k],
            "needs_detector": needs,
            "destinations": destinations or ["vietnam"] + (["cambodia"] if "Siem Reap" in text else []),
        }
        if outside:
            sample["outside"] = outside
        samples.append(sample)

    # ── Vietnamese ─────────────────────────────────────────────────────────────────────────────────
    for i in range(110):
        fam, mid, giv = VN_PEOPLE[i % len(VN_PEOPLE)]
        person = _person_vi(fam, mid, giv)
        full = person["display_name"]
        unaccented = fold(full).title()
        place, place2 = rng.sample(PLACES_VI, 2)
        tour = rng.choice(TOURS)
        phone = rng.choice(_phones(rng))
        email = _email(giv, fam, rng)
        person["emails"] = [email]
        person["phones"] = [phone]
        kind = i % 11
        if kind == 0:
            text = f"Chị {full} hỏi tour {place} 4 ngày cho 2 người lớn, SĐT {phone}, email {email}."
            add("vi", text, [person], [full, phone, email], [place, "2 người lớn"])
        elif kind == 1:
            text = f"Anh {unaccented} nhắn Zalo: muốn đi {place} rồi {place2}, ngân sách 15.000.000 VND/người."
            add("vi", text, [person], [unaccented], [place, place2, "15.000.000 VND"])
        elif kind == 2:
            line, value = _dob(rng, "vi")
            text = f"Khách {full}, {line}, đi cùng con gái. Tour {tour} khởi hành 12/03/2027."
            add("vi", text, [person], [full, value], [tour, "12/03/2027"])
        elif kind == 3:
            line, value = _passport(rng, "vi")
            text = f"Đã nhận {line} của {fold(full).lower()}; hạn đến 2031. Liên hệ {phone}."
            add("vi", text, [person], [fold(full).lower(), value, phone], ["2031"])
        elif kind == 4:
            (line, value) = _cccd(rng)[0]
            text = f"Bổ sung {line} cho chị {giv} để làm thủ tục ở {place}."
            add("vi", text, [person], [value, giv], [place])
        elif kind == 5:
            card = rng.choice(CARDS)
            text = f"Khách đọc số thẻ {card} qua điện thoại, nhờ giữ chỗ {tour}."
            add("vi", text, [person], [card], [tour])
        elif kind == 6:
            (line, value) = _otp(rng, "vi")[0]
            text = f"Anh {giv} gửi {line} để xác nhận đặt cọc."
            add("vi", text, [person], [value, giv], ["đặt cọc"])
        elif kind == 7:
            link = _link(rng)
            text = f"Gửi lại link cho {full}: {link} (hết hạn sau 24 giờ). Xem thêm https://adventure.example.invalid/tours"
            add("vi", text, [person], [full, link], ["https://adventure.example.invalid/tours", "24 giờ"])
        elif kind == 8:
            ec_fam, ec_mid, ec_giv = VN_PEOPLE[(i + 3) % len(VN_PEOPLE)]
            ec_name = f"{ec_fam} {ec_mid} {ec_giv}"
            ec_phone = rng.choice(_phones(rng))
            ec = {"role": "emergency_contact", "display_name": ec_name, "phones": [ec_phone]}
            text = f"Người liên hệ khẩn cấp của {full}: {ec_name}, {ec_phone}. Mã tour {COMPONENT_IDS[i % 3]}."
            add("vi", text, [person, ec], [full, ec_name, ec_phone], [COMPONENT_IDS[i % 3]])
        elif kind == 9:
            comp_fam, comp_mid, comp_giv = VN_PEOPLE[(i + 5) % len(VN_PEOPLE)]
            comp = _person_vi(comp_fam, comp_mid, comp_giv)
            comp["role"] = "companion"
            text = (
                f"{full} và {fold(comp['display_name']).title()} muốn phòng đôi ở {place}; "
                f"chồng là anh {comp_giv}, gọi {phone}."
            )
            add("vi", text, [person, comp], [full, fold(comp["display_name"]).title(), comp_giv, phone], [place])
        else:
            stranger = STRANGERS_VI[i % len(STRANGERS_VI)]
            text = f"Khách {full} được {stranger} giới thiệu, muốn đi {place} tháng 5/2027."
            add("vi", text, [person], [full, stranger], [place, "5/2027"], needs=True)

    # ── English ────────────────────────────────────────────────────────────────────────────────────
    for i in range(110):
        giv, fam = EN_PEOPLE[i % len(EN_PEOPLE)]
        person = _person_en(giv, fam)
        full = person["display_name"]
        place, place2 = rng.sample(PLACES_EN, 2)
        tour = rng.choice(TOURS)
        phone = rng.choice(_phones(rng))
        email = _email(giv, fam, rng)
        person["emails"] = [email]
        person["phones"] = [phone]
        kind = i % 11
        if kind == 0:
            text = f"Hi, this is {full}. We'd love {place} for 5 nights in April 2027. Call me on {phone} or {email}."
            add("en", text, [person], [full, phone, email], [place, "April 2027"])
        elif kind == 1:
            text = f"{giv} asked whether the {tour} suits two kids (8 and 11). Mrs {fam} prefers {place2}."
            add("en", text, [person], [giv, fam], [tour, place2, "8 and 11"])
        elif kind == 2:
            line, value = _dob(rng, "en")
            text = f"Traveller {full.upper()}, {line}, vegetarian, departs 02/06/2027 to {place}."
            add("en", text, [person], [full.upper(), value], [place, "02/06/2027"])
        elif kind == 3:
            line, value = _passport(rng, "en")
            text = f"Received {line} for {full}; expiry 2030. Tour {tour}."
            add("en", text, [person], [full, value], [tour, "2030"])
        elif kind == 4:
            card = rng.choice(CARDS)
            text = f"{giv} {fam} pasted a card number by mistake: {card} exp 09/28. Please delete it."
            add("en", text, [person], [f"{giv} {fam}", card], ["Please delete it"])
        elif kind == 5:
            (line, value) = _otp(rng, "en")[0]
            text = f"Your {line} was forwarded by {giv}; deposit link pending for {place}."
            add("en", text, [person], [value, giv], [place])
        elif kind == 6:
            link = _link(rng)
            text = f"Resend this to {full}: {link} . Brochure: https://adventure.example.invalid/tours/{i}"
            add("en", text, [person], [full, link], [f"https://adventure.example.invalid/tours/{i}"])
        elif kind == 7:
            ec_giv, ec_fam = EN_PEOPLE[(i + 4) % len(EN_PEOPLE)]
            ec_phone = rng.choice(_phones(rng))
            ec = {"role": "emergency_contact", "display_name": f"{ec_giv} {ec_fam}", "phones": [ec_phone]}
            text = f"Emergency contact for {full}: {ec_giv} {ec_fam} ({ec_phone}). Component {COMPONENT_IDS[i % 3]}."
            add("en", text, [person, ec], [full, f"{ec_giv} {ec_fam}", ec_phone], [COMPONENT_IDS[i % 3]])
        elif kind == 8:
            comp_giv, comp_fam = EN_PEOPLE[(i + 6) % len(EN_PEOPLE)]
            comp = _person_en(comp_giv, comp_fam)
            comp["role"] = "companion"
            text = f"{full} travels with {comp_giv} {comp_fam}; {comp_giv} has a nut allergy. Rooms: 1 twin in {place}."
            add("en", text, [person, comp], [full, f"{comp_giv} {comp_fam}", comp_giv], [place, "1 twin"])
        elif kind == 9:
            text = f"Note: {giv.lower()} {fam.lower()} wrote in lower case; phone {phone}; budget USD 2,400 pp."
            add("en", text, [person], [f"{giv.lower()} {fam.lower()}", phone], ["USD 2,400"])
        else:
            stranger = STRANGERS_EN[i % len(STRANGERS_EN)]
            text = f"{full} was referred by {stranger}, who did the {tour} last year."
            add("en", text, [person], [full, stranger], [tour], needs=True)

    # ── The card's E cases: unaccented Vietnamese names and place-like names ─────────────────────
    for lang, parts in PLACE_LIKE:
        if lang == "vi":
            fam, _, giv = parts
            person = {"role": "traveller", "display_name": f"{fam} {giv}", "given_name": giv, "family_name": fam}
            name = f"{fam} {giv}"
            add(
                "vi",
                f"Khách {fold(name).title()} xác nhận đi tour Mekong; bạn {name} gọi lại sau.",
                [person],
                [fold(name).title(), name],
                ["Mekong"],
            )
        else:
            giv, fam = parts
            person = _person_en(giv, fam)
            add(
                "en",
                f"{giv} {fam} confirmed; {giv} will call back about Hoi An.",
                [person],
                [f"{giv} {fam}", giv],
                ["Hoi An"],
            )
    for fam, mid, giv in VN_PEOPLE[:8]:
        person = _person_vi(fam, mid, giv)
        flat = fold(person["display_name"])
        add("vi", f"khach {flat} muon doi ngay khoi hanh sang 20/04/2027, so dt cu", [person], [flat], ["20/04/2027"])
        add("vi", f"{flat.upper()} - đã cọc 30%", [person], [flat.upper()], ["30%"])
    # ── Option B (S250): name-like strangers on a Vietnam trip, and places outside the trip's countries ──
    for lang, text, pii, keep in NAME_LIKE_STRANGERS:
        add(lang, text, [], pii, keep, needs=True)
    for lang, countries, text, keep, outside in OUTSIDE_TRIP:
        add(lang, text, [], [], keep, destinations=countries, outside=outside)
    for lang, text, pii, keep in PERSON_CUE_STRANGERS:
        add(lang, text, [], pii, keep, needs=True)
    return samples


_TWELVE_DIGITS = re.compile(r"(?<!\d)\d{12}(?!\d)")
LINT_NOTE = "docs-lint: ignore (synthetic Vietnamese CCCD number, not an AWS account id)"


def render(samples: list[dict]) -> str:
    """One sample per line. A sample holding a 12-digit CCCD carries the resource-id scanner's ignore marker
    (.github/scripts/check_resource_ids.py reads any 12-digit run as an AWS account id)."""
    lines = []
    for s in samples:
        if _TWELVE_DIGITS.search(json.dumps(s, ensure_ascii=False)):
            s = {**s, "lint": LINT_NOTE}
        lines.append(json.dumps(s, ensure_ascii=False, sort_keys=True) + "\n")
    return "".join(lines)


def main(argv: list[str]) -> int:
    text = render(build())
    if "--check" in argv:
        return 0 if OUT.read_text(encoding="utf-8") == text else 1
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {text.count(chr(10))} samples to {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
