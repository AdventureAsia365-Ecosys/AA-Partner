"""Pass 2 of the F3 filter: regex detectors (design v3 §4.5 step 2; lane §5.4).

What is found (kind -> placeholder ``<KIND_n>``):

- ``LINK``: signed or token-bearing URLs (``X-Amz-Signature``, ``token=``, ``sig=``, ``code=``, a long random path
  segment, a client-portal ``/c/`` link). Plain links (an agency's website, a catalog page) stay;
- ``SECRET``: JWTs;
- ``EMAIL``;
- ``DOB``: a date (``12/03/1985``, ``1985-03-12``, ``12 March 1985``, ``March 12, 1985``, ``ngày 12 tháng 3 năm 1985``)
  next to a birth cue (``DOB``, ``born``, ``ngày sinh``, ``sinh năm`` ...), or any full date at least two years in the
  past (a trip's own dates are in the future and stay); a bare year after a birth cue;
- ``CARD``: 13–19 digits passing the Luhn check;
- ``NATIONAL_ID``: a 12-digit Vietnamese CCCD (leading 0), or 9 / 12 digits after an id cue (``CCCD``, ``CMND``,
  ``ID card`` ...);
- ``PASSPORT``: one or two capitals and seven digits (``C1234567``), or 6–9 letters / digits after a passport cue;
- ``PHONE``: ``+`` and 8–15 digits; Vietnamese mobiles ``0[35789]`` + 8 digits and ``84`` forms; landlines ``02x``;
  North American ``(415) 555-0132``; 8–15 digits after a phone cue (``Tel``, ``SĐT``, ``Zalo``, ``WhatsApp`` ...);
- ``OTP``: 4–8 digits after a code cue (``OTP``, ``verification code``, ``mã xác nhận`` ...).

Digit groups separated by one space, dot or dash are read together ("0912 345 678"), and a run that does not classify
as a whole is tried window by window ("room 12 0912 345 678" finds the phone). UUIDs and placeholders are never
touched, so ids in a prompt survive. Cues are matched in folded text (no diacritics, lower case).

:func:`find` returns non-overlapping spans; it never returns or logs the values themselves beyond the spans.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from app.privacy.text import fold_with_map, original_span

# Highest priority first: an overlap keeps the earlier kind.
KINDS = ("LINK", "SECRET", "EMAIL", "DOB", "CARD", "NATIONAL_ID", "PASSPORT", "PHONE", "OTP")
_PRIORITY = {k: i for i, k in enumerate(KINDS)}


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str


PLACEHOLDER = re.compile(r"<([A-Z][A-Z_]{1,20})_(\d{1,4})>")
UUID = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")

URL = re.compile(r"\bhttps?://[^\s<>\"'()\[\]{}]+", re.IGNORECASE)
_URL_TRAIL = ".,;:!?"
SIGNED_PARAMS = frozenset(
    {
        "signature",
        "sig",
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "otp",
        "code",
        "key",
        "api_key",
        "apikey",
        "auth",
        "jwt",
        "session",
        "sid",
        "hash",
        "secret",
        "password",
    }
)
_TOKENISH = re.compile(r"^[A-Za-z0-9_\-.~%=+]{24,}$")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH_RE = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?"
    r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
# All date patterns run on folded text (lower case, no diacritics).
NUM_DATE = re.compile(r"(?<![\d/.\-])(\d{1,2})([/.\-])(\d{1,2})\2(\d{4}|\d{2})(?![\d/.\-]*\d)")
ISO_DATE = re.compile(r"(?<![\d/.\-])(\d{4})([/.\-])(\d{1,2})\2(\d{1,2})(?![\d/.\-]*\d)")
DMY_TEXT = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_RE + r"\.?,?\s+(\d{4})\b")
MDY_TEXT = re.compile(r"\b" + _MONTH_RE + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
VN_DATE = re.compile(r"\b(?:ngay\s+)?(\d{1,2})\s+thang\s+(\d{1,2})(?:\s*(?:,|nam)\s*|\s+)(\d{4})\b")
YEAR_AFTER_CUE = re.compile(
    r"\b(?:dob|d\.o\.b\.?|date of birth|year of birth|born(?: in)?|sinh nam|nam sinh|ns)\s*[:\-]?\s*((?:19|20)\d{2})\b"
)
DOB_CUE = re.compile(
    r"\b(?:dob|d\.o\.b|date of birth|birth\s?date|birthdate|born|birthday|ngay sinh|sinh ngay|sinh nam|nam sinh|ns)\b"
)
PHONE_CUE = re.compile(
    r"\b(?:tel|telephone|phone|mobile|cell|sdt|so dien thoai|dien thoai|dt|zalo|whatsapp|viber|hotline|call|goi"
    r"|lien he|lh|sms)\b"
)
ID_CUE = re.compile(
    r"\b(?:cccd|cmnd|can cuoc|chung minh|id card|national id|identity card|id no|id number|ssn|social security)\b"
)
PASSPORT_CUE = re.compile(r"\b(?:passport|ho chieu|pp)\b")
OTP_CUE = re.compile(
    r"\b(?:otp|one[- ]time|verification code|security code|confirmation code|confirm code|ma xac nhan|ma xac thuc"
    r"|ma otp|ma pin|pin|passcode|code|ma)\b"
)
PASSPORT_ALNUM = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,2}\d{7}(?![A-Za-z0-9])")
PASSPORT_AFTER_CUE = re.compile(r"\b(?:passport|ho chieu|pp)\s*(?:no\.?|number|num|so)?\s*[:#.\-]?\s*([a-z0-9]{6,9})\b")
# A run of digit groups joined by single separators; the leading "+", "(0xx)" belong to the first group.
DIGIT_RUN = re.compile(r"(?<![\w+])\+?(?:\(\d{1,4}\)[ .\-]?)?\d(?:[ .\-]?(?:\(\d+\)|\d))*(?![\w])")
DIGIT_GROUP = re.compile(r"\+?(?:\(\d+\)|\d+)")
NANP = re.compile(r"^\(?\d{3}\)?[ .\-]?\d{3}[ .\-]\d{4}$")
CUE_WINDOW = 28


def luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def protected_spans(text: str) -> list[tuple[int, int]]:
    """Placeholders and UUIDs: never matched by any detector (ids in a prompt survive, numbering stays intact)."""
    return [(m.start(), m.end()) for rx in (PLACEHOLDER, UUID) for m in rx.finditer(text)]


def find(text: str, today: dt.date | None = None) -> list[Span]:
    """Every pass-2 match in ``text``, non-overlapping, sorted by position."""
    today = today or dt.date.today()
    folded, index = fold_with_map(text)
    spans: list[Span] = []
    spans += _links(text)
    spans += [Span(m.start(), m.end(), "SECRET") for m in JWT.finditer(text)]
    spans += [Span(m.start(), m.end(), "EMAIL") for m in EMAIL.finditer(text)]
    spans += _dates(folded, index, today)
    spans += _digit_runs(text, folded, index)
    spans += [Span(m.start(), m.end(), "PASSPORT") for m in PASSPORT_ALNUM.finditer(text)]
    for m in PASSPORT_AFTER_CUE.finditer(folded):
        if any(c.isdigit() for c in m.group(1)):
            spans.append(Span(*original_span(index, m.start(1), m.end(1)), "PASSPORT"))
    return resolve(spans, protected_spans(text))


def resolve(spans: list[Span], protected: list[tuple[int, int]]) -> list[Span]:
    """Drop spans touching a protected range, then keep the highest-priority (then longest) of overlapping spans."""
    live = [s for s in spans if s.end > s.start and not any(s.start < pe and ps < s.end for ps, pe in protected)]
    live.sort(key=lambda s: (_PRIORITY.get(s.kind, len(KINDS)), -(s.end - s.start), s.start))
    chosen: list[Span] = []
    for s in live:
        if not any(s.start < c.end and c.start < s.end for c in chosen):
            chosen.append(s)
    return sorted(chosen, key=lambda s: s.start)


def _links(text: str) -> list[Span]:
    out = []
    for m in URL.finditer(text):
        url = m.group(0).rstrip(_URL_TRAIL)
        if _is_signed(url):
            out.append(Span(m.start(), m.start() + len(url), "LINK"))
    return out


def _is_signed(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return True  # unparsable: treat as a token-bearing link (fail closed)
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in SIGNED_PARAMS or key.lower().startswith("x-amz-") or _TOKENISH.match(value):
            return True
    if parts.fragment and _TOKENISH.match(parts.fragment.split("=")[-1]):
        return True
    segments = [s for s in parts.path.split("/") if s]
    if segments[:1] == ["c"]:
        return True  # the client portal's links (proposal, OTP sign-in, forms) carry access keys
    return any(_TOKENISH.match(s) and any(c.isdigit() for c in s) and any(c.isalpha() for c in s) for s in segments)


_CLAUSE_END = re.compile(r"[,;|\n]|\.\s|\band\b|\bva\b")


def _cue_before(folded: str, start: int, cue: re.Pattern[str]) -> bool:
    """A cue in the same clause, at most CUE_WINDOW characters before ``start`` ("DOB 1/2/1985, trip 3/4/2027": the
    trip date is not a birth date)."""
    window = folded[max(0, start - CUE_WINDOW) : start]
    ends = list(_CLAUSE_END.finditer(window))
    if ends:
        window = window[ends[-1].end() :]
    return cue.search(window) is not None


def _valid(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _year4(raw: str, today: dt.date) -> int:
    if len(raw) == 4:
        return int(raw)
    y = int(raw)
    return 2000 + y if 2000 + y <= today.year else 1900 + y


def _dates(folded: str, index: list[int], today: dt.date) -> list[Span]:
    out: list[Span] = []

    def consider(start: int, end: int, candidates: list[dt.date | None], two_digit: bool = False) -> None:
        dates = [d for d in candidates if d is not None]
        if not dates:
            return
        cue = _cue_before(folded, start, DOB_CUE)
        past = all(d.year <= today.year - 2 for d in dates)
        if cue or (past and not two_digit):
            out.append(Span(*original_span(index, start, end), "DOB"))

    for m in NUM_DATE.finditer(folded):
        a, b, y = int(m.group(1)), int(m.group(3)), _year4(m.group(4), today)
        consider(m.start(), m.end(), [_valid(y, b, a), _valid(y, a, b)], two_digit=len(m.group(4)) == 2)
    for m in ISO_DATE.finditer(folded):
        consider(m.start(), m.end(), [_valid(int(m.group(1)), int(m.group(3)), int(m.group(4)))])
    for m in DMY_TEXT.finditer(folded):
        consider(m.start(), m.end(), [_valid(int(m.group(3)), _MONTHS[m.group(2)[:3]], int(m.group(1)))])
    for m in MDY_TEXT.finditer(folded):
        consider(m.start(), m.end(), [_valid(int(m.group(3)), _MONTHS[m.group(1)[:3]], int(m.group(2)))])
    for m in VN_DATE.finditer(folded):
        consider(m.start(), m.end(), [_valid(int(m.group(3)), int(m.group(2)), int(m.group(1)))])
    for m in YEAR_AFTER_CUE.finditer(folded):
        out.append(Span(*original_span(index, m.start(1), m.end(1)), "DOB"))
    return out


def _classify(raw: str, number: str, plus: bool, folded: str, start: int) -> str | None:
    n = len(number)
    if plus and 8 <= n <= 15:
        return "PHONE"
    if 13 <= n <= 19 and luhn_ok(number):
        return "CARD"
    if 4 <= n <= 8 and _cue_before(folded, start, OTP_CUE) and not _cue_before(folded, start, PHONE_CUE):
        return "OTP"
    if 6 <= n <= 9 and _cue_before(folded, start, PASSPORT_CUE):
        return "PASSPORT"
    if n in (9, 12) and _cue_before(folded, start, ID_CUE):
        return "NATIONAL_ID"
    if n == 12 and number.startswith("0"):
        return "NATIONAL_ID"
    if n == 10 and number[0] == "0" and number[1] in "35789":
        return "PHONE"
    if n == 11 and number.startswith("02"):
        return "PHONE"
    if n in (11, 12) and number.startswith("84") and number[2] in "235789":
        return "PHONE"
    if n == 10 and NANP.match(raw.strip()):
        return "PHONE"
    if 8 <= n <= 15 and _cue_before(folded, start, PHONE_CUE):
        return "PHONE"
    return None


def _digit_runs(text: str, folded: str, index: list[int]) -> list[Span]:
    # Digits fold to themselves, so positions in `folded` equal positions in `text` only when nothing before them
    # expanded; cue lookups therefore go through the folded position of the run's start.
    rev = {orig: i for i, orig in reversed(list(enumerate(index)))}
    out: list[Span] = []
    for run in DIGIT_RUN.finditer(text):
        groups = [
            (g.start() + run.start(), g.end() + run.start(), g.group(0)) for g in DIGIT_GROUP.finditer(run.group(0))
        ]
        i = 0
        while i < len(groups):
            matched = False
            for j in range(len(groups), i, -1):
                start, end = groups[i][0], groups[j - 1][1]
                raw = text[start:end]
                number = "".join(c for c in raw if c.isdigit())
                kind = _classify(raw, number, raw.startswith("+"), folded, rev.get(start, start))
                if kind:
                    out.append(Span(start, end, kind))
                    i, matched = j, True
                    break
            if not matched:
                i += 1
    return out
