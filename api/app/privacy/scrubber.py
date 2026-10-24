"""The F3 PII scrubber: three passes before any text reaches a model (design v3 §4.5; lane §5.4; card CRM-10).

1. **Known values** — every identity value tied to the trip (travellers, companions, emergency contacts; agency
   members and supplier contacts when a caller passes them) becomes a numbered placeholder. Names are matched folded
   (no diacritics, any case), so "Nguyen Van An" is caught for a stored "Nguyễn Văn An"; the full name, every run of
   two or more of its words, the given name alone when written with a capital, and the family name alone only after
   an honorific ("anh", "chị", "ông", "bà", "Mr", "Mrs", ...), because a family name alone ("Nguyễn", "Trần") is also a
   street or a temple. A known person whose name looks like a place ("Sơn La", "Florence") is still caught: this pass
   matches what the agency stored, it does not guess what a word is. Known e-mails, phones (any spacing, ``0`` or
   ``+84``), passport numbers and dates of birth (several spellings) too. A one-word match inside a known place name
   ("Bình" in "Ninh Bình", :mod:`app.privacy.places`) is skipped; a full name never is.
2. **Regex** — :mod:`app.privacy.patterns` (signed links, JWTs, e-mail, DOB, cards, national ids, passports, phones,
   OTPs).
3. **Detector** — Bedrock Guardrails ``ApplyGuardrail`` in detect mode on what is left
   (:mod:`app.privacy.guardrails`): names and addresses nobody stored. Its ``match`` values are replaced in the text
   and never logged.

Rules:

- **Fail closed**: any error raises :class:`ScrubError` (a detector timeout :class:`ScrubTimeout`); the gateway then
  makes no call and the draft says "reply by hand" (``manual_reply_needed``). A text longer than
  :data:`MAX_TEXT_CHARS` is refused, never cut.
- Placeholders are ``<KIND_n>``, numbered per kind in order of first appearance; one value (or one known person, under
  any spelling) keeps one placeholder. Text that already contains a placeholder-shaped token is neutralised to
  ``[KIND_n]`` first, so a note cannot make :func:`restore` put a real value where the author wrote a token.
- The **receipt** holds the scrubber version, the detector, and counts per kind and per pass; never a value.
- :attr:`ScrubResult.placeholders` (placeholder -> original) is the only place the values live; it is excluded from
  ``repr`` and goes to the encrypted placeholder map (:mod:`app.privacy.placeholder_store`) or nowhere.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.privacy import patterns
from app.privacy.places import place_spans
from app.privacy.text import digits, extend_over_marks, fold, fold_with_map, original_span

VERSION = "f3-1"
MAX_TEXT_CHARS = 24_000
DEFAULT_TIMEOUT_S = 3.0
PASSES = ("known", "regex", "detector")

# Vietnamese family names: when a display name starts with one, the name reads family-first.
VN_FAMILY_NAMES = frozenset(
    fold(n)
    for n in (
        "Nguyễn Trần Lê Phạm Hoàng Huỳnh Phan Vũ Võ Đặng Bùi Đỗ Hồ Ngô Dương Lý Đào Đoàn Vương Trịnh Trương Lâm Phùng "
        "Mai Tô Hà Tạ Lương Đinh Cao Châu Quách Thái Văn Kiều Lại Lục Mạc Triệu Từ Chu Hứa La Tăng Thân Kim"
    ).split()
)
# Honorifics after which a family name alone is a person ("anh Nguyễn", "Mrs Smith").
HONORIFICS = (
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
_HONORIFIC_BEFORE = re.compile(r"\b(?:" + "|".join(HONORIFICS) + r")\.?\s+$")
_PLACEHOLDER_SHAPE = re.compile(r"<([A-Za-z][A-Za-z_]{0,20}_\d{1,4})>")
_WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")

# Guardrails entity types -> placeholder kinds. URL is deliberately absent (catalog links must survive; signed links
# are pass 2). AGE is absent too (an age band is planning data).
DETECTOR_KINDS = {
    "NAME": "PERSON",
    "EMAIL": "EMAIL",
    "PHONE": "PHONE",
    "ADDRESS": "ADDRESS",
    "USERNAME": "USERNAME",
    "PASSWORD": "SECRET",
    "PIN": "OTP",
    "DRIVER_ID": "NATIONAL_ID",
    "LICENSE_PLATE": "PLATE",
    "VEHICLE_IDENTIFICATION_NUMBER": "PLATE",
    "CREDIT_DEBIT_CARD_NUMBER": "CARD",
    "CREDIT_DEBIT_CARD_CVV": "CARD",
    "CREDIT_DEBIT_CARD_EXPIRY": "CARD",
    "INTERNATIONAL_BANK_ACCOUNT_NUMBER": "BANK",
    "SWIFT_CODE": "BANK",
    "US_BANK_ACCOUNT_NUMBER": "BANK",
    "US_BANK_ROUTING_NUMBER": "BANK",
    "IP_ADDRESS": "IP",
    "MAC_ADDRESS": "IP",
    "US_PASSPORT_NUMBER": "PASSPORT",
    "US_SOCIAL_SECURITY_NUMBER": "NATIONAL_ID",
    "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER": "NATIONAL_ID",
    "UK_NATIONAL_INSURANCE_NUMBER": "NATIONAL_ID",
    "UK_UNIQUE_TAXPAYER_REFERENCE_NUMBER": "NATIONAL_ID",
    "UK_NATIONAL_HEALTH_SERVICE_NUMBER": "NATIONAL_ID",
    "CA_SOCIAL_INSURANCE_NUMBER": "NATIONAL_ID",
    "CA_HEALTH_NUMBER": "NATIONAL_ID",
    "AWS_ACCESS_KEY": "SECRET",
    "AWS_SECRET_KEY": "SECRET",
}


class ScrubError(Exception):
    """The text could not be scrubbed; no model may receive it. ``code`` is the refusal code the gateway logs."""

    code = "scrub_failed"


class ScrubTimeout(ScrubError):
    code = "scrub_timeout"


@dataclass(frozen=True)
class Detection:
    """One finding of pass 3: an entity type and the matched text (never logged)."""

    type: str
    match: str = field(repr=False)


class Detector(Protocol):
    name: str

    async def detect(self, text: str) -> Sequence[Detection]: ...


@dataclass(frozen=True)
class KnownPerson:
    """One person tied to the trip. ``role``: traveller | companion | emergency_contact | member | supplier_contact."""

    role: str
    display_name: str | None = field(default=None, repr=False)
    given_name: str | None = field(default=None, repr=False)
    family_name: str | None = field(default=None, repr=False)
    emails: tuple[str, ...] = field(default=(), repr=False)
    phones: tuple[str, ...] = field(default=(), repr=False)
    passports: tuple[str, ...] = field(default=(), repr=False)
    dobs: tuple[dt.date, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class KnownValues:
    persons: tuple[KnownPerson, ...] = ()

    @property
    def count(self) -> int:
        return len(self.persons)


@dataclass(frozen=True)
class ScrubResult:
    text: str
    receipt: dict[str, Any]
    placeholders: dict[str, str] = field(default_factory=dict, repr=False)


class _Registry:
    """Numbered placeholders per kind; one key (a known person, or a normalised value) keeps one placeholder."""

    def __init__(self) -> None:
        self.by_key: dict[tuple[str, str], str] = {}
        self.values: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self.per_pass: dict[str, int] = dict.fromkeys(PASSES, 0)

    def placeholder(self, kind: str, key: str, original: str, pass_name: str) -> str:
        k = (kind, key)
        if k not in self.by_key:
            n = sum(1 for (kk, _) in self.by_key if kk == kind) + 1
            ph = f"<{kind}_{n}>"
            self.by_key[k] = ph
            self.values[ph] = original
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.per_pass[pass_name] += 1
        return self.by_key[k]


def _apply(text: str, spans: Iterable[tuple[int, int, str]]) -> str:
    """Replace non-overlapping ``(start, end, placeholder)`` spans (any order)."""
    out, pos = [], 0
    for start, end, ph in sorted(spans):
        out.append(text[pos:start])
        out.append(ph)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def neutralise(text: str) -> str:
    return _PLACEHOLDER_SHAPE.sub(lambda m: f"[{m.group(1)}]", text)


def restore(text: str, placeholders: dict[str, str]) -> str:
    """Put the original values back for the placeholders in ``placeholders``; any other token stays as it is."""
    return patterns.PLACEHOLDER.sub(lambda m: placeholders.get(m.group(0), m.group(0)), text)


# ── Pass 1: known values ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _NameVariant:
    tokens: tuple[str, ...]  # folded words
    rule: str  # "any" (two or more words) | "capital" (one word, written with a capital) | "honorific"


def _words(value: str | None) -> list[str]:
    return [fold(w) for w in _WORD.findall(value or "") if fold(w)]


def _name_variants(p: KnownPerson) -> list[_NameVariant]:
    given, family, display = _words(p.given_name), _words(p.family_name), _words(p.display_name)
    fulls = [t for t in (display, given + family, family + given) if t]
    variants: dict[tuple[str, ...], str] = {}
    for full in fulls:
        for size in range(len(full), 1, -1):
            for i in range(len(full) - size + 1):
                variants.setdefault(tuple(full[i : i + size]), "any")
    singles_capital: set[str] = set()
    singles_honorific: set[str] = set()
    if given or family:
        singles_capital.update(given[-1:] if family else given)
        if given and len(given) > 1 and display[:1] and display[0] not in VN_FAMILY_NAMES:
            singles_capital.add(given[0])  # western "John Paul Smith": "John"
        singles_honorific.update(family)
    if display:
        vn_order = display[0] in VN_FAMILY_NAMES and len(display) > 1
        if vn_order:
            singles_honorific.add(display[0])
            singles_capital.add(display[-1])  # "Nguyễn Văn An": "An"; middle words alone say nothing
        elif len(display) > 1:
            singles_capital.add(display[0])
            singles_honorific.add(display[-1])
        else:
            singles_capital.add(display[0])
    for w in singles_capital:
        if len(w) >= 2:
            variants.setdefault((w,), "capital")
    for w in singles_honorific - singles_capital:
        if len(w) >= 2:
            variants.setdefault((w,), "honorific")
    return [_NameVariant(t, r) for t, r in variants.items()]


def _dob_spellings(d: dt.date) -> list[str]:
    month_en = d.strftime("%B")
    return [
        f"{d.day:02d}/{d.month:02d}/{d.year}",
        f"{d.day}/{d.month}/{d.year}",
        f"{d.month:02d}/{d.day:02d}/{d.year}",
        f"{d.day:02d}-{d.month:02d}-{d.year}",
        f"{d.day:02d}.{d.month:02d}.{d.year}",
        d.isoformat(),
        f"{d.day} {month_en} {d.year}",
        f"{d.day} {month_en[:3]} {d.year}",
        f"{month_en} {d.day}, {d.year}",
        f"ngày {d.day} tháng {d.month} năm {d.year}",
    ]


def _known_pass(text: str, known: KnownValues, reg: _Registry) -> str:
    folded, index = fold_with_map(text)
    protected = patterns.protected_spans(text)
    places = place_spans(folded)
    found: list[tuple[int, int, str, str]] = []  # (start, end, kind, key) in original positions
    for i, person in enumerate(known.persons):
        key = f"person:{i}"
        for v in _name_variants(person):
            rx = re.compile(r"(?<!\w)" + r"[\s.\-]+".join(re.escape(t) for t in v.tokens) + r"(?!\w)")
            for m in rx.finditer(folded):
                start, end = original_span(index, m.start(), m.end())
                end = extend_over_marks(text, end)
                if len(v.tokens) == 1 and any(ps <= m.start() and m.end() <= pe for ps, pe in places):
                    continue  # "Bình" inside "Ninh Bình": the place, not the person
                if v.rule == "capital" and not text[start].isupper():
                    continue
                if v.rule == "honorific" and not _HONORIFIC_BEFORE.search(folded[max(0, m.start() - 12) : m.start()]):
                    continue
                found.append((start, end, "PERSON", key))
        for email in person.emails:
            for m in re.finditer(re.escape(email.strip()), text, re.IGNORECASE):
                found.append((m.start(), m.end(), "EMAIL", "email:" + email.strip().lower()))
        for passport in person.passports:
            p = passport.strip()
            if len(p) >= 5:
                for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(p) + r"(?![A-Za-z0-9])", text, re.IGNORECASE):
                    found.append((m.start(), m.end(), "PASSPORT", "passport:" + p.upper()))
        for d in person.dobs:
            for spelling in _dob_spellings(d):
                fs = fold(spelling)
                for m in re.finditer(r"(?<![\w/.\-])" + re.escape(fs) + r"(?![\w/.\-]*\d)", folded):
                    s, e = original_span(index, m.start(), m.end())
                    found.append((s, e, "DOB", "dob:" + d.isoformat()))
        found += _known_phones(text, person.phones)
    # Longest first, then earliest; protected ranges (ids, placeholders) are never touched.
    found.sort(key=lambda f: (-(f[1] - f[0]), f[0]))
    chosen: list[tuple[int, int, str, str]] = []
    for f in found:
        if any(f[0] < pe and ps < f[1] for ps, pe in protected):
            continue
        if not any(f[0] < c[1] and c[0] < f[1] for c in chosen):
            chosen.append(f)
    spans = []
    for start, end, kind, key in sorted(chosen):
        original = _canonical(known, key) if kind == "PERSON" else text[start:end]
        spans.append((start, end, reg.placeholder(kind, key, original, "known")))
    return _apply(text, spans)


def _canonical(known: KnownValues, key: str) -> str:
    p = known.persons[int(key.split(":")[1])]
    return p.display_name or " ".join(x for x in (p.given_name, p.family_name) if x) or ""


def _phone_forms(phone: str) -> set[str]:
    d = digits(phone)
    forms = {d} if len(d) >= 7 else set()
    if d.startswith("84") and len(d) in (11, 12):
        forms.add("0" + d[2:])
    if d.startswith("0") and len(d) in (10, 11):
        forms.add("84" + d[1:])
    return forms


def _known_phones(text: str, phones: tuple[str, ...]) -> list[tuple[int, int, str, str]]:
    forms: dict[str, str] = {}
    for phone in phones:
        for f in _phone_forms(phone):
            forms[f] = "phone:" + max(_phone_forms(phone), key=len)
    if not forms:
        return []
    out = []
    for run in patterns.DIGIT_RUN.finditer(text):
        groups = [(g.start() + run.start(), g.end() + run.start()) for g in patterns.DIGIT_GROUP.finditer(run.group(0))]
        for i in range(len(groups)):
            for j in range(len(groups), i, -1):
                start, end = groups[i][0], groups[j - 1][1]
                key = forms.get(digits(text[start:end]))
                if key:
                    out.append((start, end, "PHONE", key))
                    break
    return out


# ── The scrubber ─────────────────────────────────────────────────────────────────────────────────────


class PiiScrubber:
    """The three passes. ``detector``: pass 3 (Bedrock Guardrails in production, a fake in tests); ``timeout_s``: how
    long pass 3 may take before the scrub fails closed with :class:`ScrubTimeout`."""

    version = VERSION

    def __init__(self, detector: Detector, *, timeout_s: float = DEFAULT_TIMEOUT_S, today: dt.date | None = None):
        self.detector = detector
        self.timeout_s = timeout_s
        self._today = today

    async def scrub(self, text: str, known: KnownValues | None = None) -> ScrubResult:
        if not isinstance(text, str):
            raise ScrubError("text must be a string")
        if len(text) > MAX_TEXT_CHARS:
            raise ScrubError("text too long")
        known = known or KnownValues()
        reg = _Registry()
        text = neutralise(text)
        try:
            text = _known_pass(text, known, reg)
            text = self._regex_pass(text, reg)
        except ScrubError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bug in a pass must not let text through
            raise ScrubError(type(exc).__name__) from None
        text = await self._detector_pass(text, reg)
        receipt = {
            "scrubber": self.version,
            "detector": self.detector.name,
            "entities": sum(reg.counts.values()),
            "counts": dict(sorted(reg.counts.items())),
            "passes": reg.per_pass,
            "known_persons": known.count,
        }
        return ScrubResult(text=text, receipt=receipt, placeholders=dict(reg.values))

    def _regex_pass(self, text: str, reg: _Registry) -> str:
        spans = []
        for s in patterns.find(text, self._today):
            original = text[s.start : s.end]
            key = digits(original) if s.kind in ("PHONE", "CARD", "NATIONAL_ID", "OTP") else fold(original)
            spans.append((s.start, s.end, reg.placeholder(s.kind, key or original, original, "regex")))
        return _apply(text, spans)

    async def _detector_pass(self, text: str, reg: _Registry) -> str:
        try:
            detections = await asyncio.wait_for(self.detector.detect(text), timeout=self.timeout_s)
        except TimeoutError:
            raise ScrubTimeout("detector timeout") from None
        except ScrubError:
            raise
        except Exception as exc:  # noqa: BLE001 — recorded by class only, never the message (it may echo text)
            raise ScrubError(type(exc).__name__) from None
        protected = patterns.protected_spans(text)
        found: list[tuple[int, int, str, str]] = []
        for d in detections:
            kind = DETECTOR_KINDS.get(d.type)
            value = (d.match or "").strip()
            if not kind or len(value) < 2 or "<" in value or ">" in value:
                continue
            for m in re.finditer(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text):
                found.append((m.start(), m.end(), kind, fold(value)))
        found.sort(key=lambda f: (-(f[1] - f[0]), f[0]))
        chosen: list[tuple[int, int, str, str]] = []
        for f in found:
            if any(f[0] < pe and ps < f[1] for ps, pe in protected):
                continue
            if not any(f[0] < c[1] and c[0] < f[1] for c in chosen):
                chosen.append(f)
        spans = [(s, e, reg.placeholder(k, key, text[s:e], "detector")) for s, e, k, key in sorted(chosen)]
        return _apply(text, spans)
