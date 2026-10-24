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
   and never logged. An ``ADDRESS`` finding that is exactly one known destination ("Hà Giang", "Hội An",
   :func:`app.privacy.places.is_place`) is replaced too, but typed ``<PLACE_n>`` (option C, S249): the model knows a
   place stood there, the value goes into the map like any other and is restored after the reply. A ``NAME`` finding
   stays ``<PERSON_n>`` even when it equals a place. A person named like a destination whom the guardrail labels
   ``ADDRESS`` is typed ``<PLACE_n>``: still masked, but the model is told a place stood there (accepted cost of
   option C, docs/implementation-notes/AA-938.md). **Option B** (S250, ``f3-3``): a ``NAME`` or ``ADDRESS`` finding
   that is exactly one of the trip's own destinations (:attr:`KnownValues.destinations`, the trip's countries, and
   within them only the explicit list :data:`app.privacy.places.KEEPABLE_PLACES`) is left in clear, unless the place
   could be read as a person's name ("Mai Châu", "Hà Giang", "Long An": :func:`app.privacy.names.name_like`) or it
   follows an honorific or a word for a person ("chị Sa Pa", "guest Hội An"). Accepted residual risk: a person nobody
   stored whose whole name is exactly such a place is left visible when the guardrail tags them.

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
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.privacy import names, patterns
from app.privacy.places import is_keepable_place, is_place, place_spans
from app.privacy.text import digits, extend_over_marks, fold, fold_with_map, original_span

# Bumped whenever the same input can scrub to a different output (a new kind, a new rule): the receipt and the stored
# map say which rules produced them. f3-2: detector ADDRESS findings that are exactly one destination -> <PLACE_n>.
# f3-3: detector NAME / ADDRESS findings that are exactly one of the trip's own non-name-like destinations: clear.
VERSION = "f3-3"
MAX_TEXT_CHARS = 24_000
DEFAULT_TIMEOUT_S = 3.0
PASSES = ("known", "regex", "detector")

# Vietnamese family names (folded): when a display name starts with one, the name reads family-first. Honorifics after
# which a family name alone is a person ("anh Nguyễn", "Mrs Smith"). Both live in app.privacy.names (option B reads
# them too).
VN_FAMILY_NAMES = names.VN_FAMILY_NAMES
HONORIFICS = names.HONORIFIC_WORDS
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
    """One person tied to the trip. ``role``: traveller | companion | related | emergency_contact | member |
    supplier_contact."""

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
    """``persons``: pass 1 input. ``destinations``: the trip's destination codes (``crm.trip.destinations``, countries
    such as ``vietnam``; not PII), the scope of option B in pass 3."""

    persons: tuple[KnownPerson, ...] = ()
    destinations: tuple[str, ...] = ()

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
        self.kept_places = 0  # option B: detector findings left in clear (no placeholder, not an entity)

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
    protected = patterns.Ranges(patterns.protected_spans(text))
    places = place_spans(folded)
    place_starts = [ps for ps, _ in places]
    found: list[tuple[int, int, str, str]] = []  # (start, end, kind, key) in original positions
    for i, person in enumerate(known.persons):
        key = f"person:{i}"
        for v in _name_variants(person):
            rx = re.compile(r"(?<!\w)" + r"[\s.\-]+".join(re.escape(t) for t in v.tokens) + r"(?!\w)")
            for m in rx.finditer(folded):
                start, end = original_span(index, m.start(), m.end())
                end = extend_over_marks(text, end)
                if len(v.tokens) == 1 and _inside_place(places, place_starts, m.start(), m.end()):
                    continue  # "Bình" inside "Ninh Bình": the place, not the person
                after_honorific = _HONORIFIC_BEFORE.search(folded[max(0, m.start() - 12) : m.start()]) is not None
                if v.rule == "capital" and not (text[start].isupper() or after_honorific):
                    continue  # "chị an" / "anh hoa": lower case after an honorific is still the person
                if v.rule == "honorific" and not after_honorific:
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
    taken = patterns.Ranges()
    chosen: list[tuple[int, int, str, str]] = []
    for f in found:
        if protected.overlaps(f[0], f[1]) or taken.overlaps(f[0], f[1]):
            continue
        taken.add(f[0], f[1])
        chosen.append(f)
    spans = []
    for start, end, kind, key in sorted(chosen):
        original = _canonical(known, key) if kind == "PERSON" else text[start:end]
        spans.append((start, end, reg.placeholder(kind, key, original, "known")))
    return _apply(text, spans)


def _inside_place(places: list[tuple[int, int]], starts: list[int], start: int, end: int) -> bool:
    """``[start, end)`` lies inside one place phrase (``places`` sorted by start, non-overlapping: finditer order)."""
    i = bisect_right(starts, start) - 1
    return i >= 0 and places[i][0] <= start and end <= places[i][1]


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
    longest = max(len(f) for f in forms)  # a longer window can never equal a stored form: never built (linear cost)
    shortest = min(len(f) for f in forms)
    out = []
    for groups in patterns.digit_groups(text):
        run_digits = digits(text[groups[0][0] : groups[-1][1]])
        offsets = [0]
        for g in groups:
            offsets.append(offsets[-1] + g[2])
        for i in range(len(groups)):
            for j in range(patterns.window_limits(groups, i, longest), i, -1):
                if offsets[j] - offsets[i] < shortest:
                    break
                key = forms.get(run_digits[offsets[i] : offsets[j]])
                if key:
                    out.append((groups[i][0], groups[j - 1][1], "PHONE", key))
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
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        # Passes 1 and 2 are CPU work: a worker thread, under the same deadline as pass 3, so a large prompt never
        # blocks the event loop and a slow scrub fails closed like a slow detector.
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(self._local_passes, neutralise(text), known, reg), timeout=self.timeout_s
            )
        except TimeoutError:
            raise ScrubTimeout("scrub timeout") from None
        except ScrubError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bug in a pass must not let text through
            raise ScrubError(type(exc).__name__) from None
        text = await self._detector_pass(text, reg, deadline - loop.time(), known.destinations)
        counts = dict(reg.counts)
        if reg.kept_places:
            counts[PLACE_KEPT] = reg.kept_places  # a count of what was not replaced: not in "entities"
        receipt = {
            "scrubber": self.version,
            "detector": self.detector.name,
            "entities": sum(reg.counts.values()),
            "counts": dict(sorted(counts.items())),
            "passes": reg.per_pass,
            "known_persons": known.count,
        }
        return ScrubResult(text=text, receipt=receipt, placeholders=dict(reg.values))

    def _local_passes(self, text: str, known: KnownValues, reg: _Registry) -> str:
        return self._regex_pass(_known_pass(text, known, reg), reg)

    def _regex_pass(self, text: str, reg: _Registry) -> str:
        spans = []
        for s in patterns.find(text, self._today):
            original = text[s.start : s.end]
            key = digits(original) if s.kind in ("PHONE", "CARD", "NATIONAL_ID", "OTP") else fold(original)
            spans.append((s.start, s.end, reg.placeholder(s.kind, key or original, original, "regex")))
        return _apply(text, spans)

    async def _detector_pass(
        self, text: str, reg: _Registry, timeout_s: float, destinations: tuple[str, ...] = ()
    ) -> str:
        if timeout_s <= 0:
            raise ScrubTimeout("scrub timeout")
        try:
            detections = await asyncio.wait_for(self.detector.detect(text), timeout=timeout_s)
        except TimeoutError:
            raise ScrubTimeout("detector timeout") from None
        except ScrubError:
            raise
        except Exception as exc:  # noqa: BLE001 — recorded by class only, never the message (it may echo text)
            raise ScrubError(type(exc).__name__) from None
        try:
            return _apply_detections(text, detections, reg, destinations)
        except ScrubError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bug here must not let text through
            raise ScrubError(type(exc).__name__) from None


# ── Pass 3: placing the detector's findings ──────────────────────────────────────────────────────────

_PIECE_TRIM = " \t\r\n,;:"
PLACE = "PLACE"  # option C: a detector ADDRESS finding that is exactly one known destination
PLACE_KEPT = "PLACE_KEPT"  # option B: receipt count of findings left in clear as the trip's own destinations
KEEPABLE_TYPES = frozenset({"NAME", "ADDRESS"})  # option B: only these detector types may be a destination
# Option B: a destination right after an honorific or a word for a person ("chị Sa Pa", "Mrs Hoi An", "guest Hội An",
# "Travellers: Đà Lạt") is a person named like it: masked as before. Matched on the text before the finding with its
# marks (names.mark), so "đi Sa Pa" (go to) is not "dì Sa Pa" (aunt); the unmarked honorifics match unmarked writing.
PERSON_CUES = (
    *names.HONORIFIC_WORDS_MARKED,
    "khach",
    "guest",
    "guests",
    "traveller",
    "travellers",
    "traveler",
    "travelers",
    "passenger",
    "passengers",
)
_PERSON_BEFORE = re.compile(r"\b(?:" + "|".join(HONORIFICS + PERSON_CUES) + r")\.?[:,]?\s+$")
_PERSON_BEFORE_WINDOW = 16


def _apply_detections(
    text: str, detections: Sequence[Detection], reg: _Registry, destinations: Sequence[str] = ()
) -> str:
    """Replace every finding of a mapped kind; **fail closed** when one cannot be placed.

    - A finding is located with any whitespace between its words (a name broken over two lines is found), then
      case-insensitively, then without word boundaries, then folded (no diacritics). One that is still not found
      raises :class:`ScrubError`: the detector saw something the scrubber cannot remove, so the text must not reach
      the model. Nothing is skipped silently.
    - A finding may contain placeholders of the earlier passes ("12 <PERSON_1> street, Ward 5"): the placeholders
      stay, every other fragment of it ("12", "street, Ward 5") is replaced. Protected ranges (placeholders, UUIDs)
      are never touched.
    - Overlapping findings ("John Smith" as a name and "Smith Street 5" as an address) are merged into one
      placeholder instead of dropping one of them, so no part of either reaches the model.
    - Kinds the scrubber does not map (``URL``, ``AGE``) are left as they are; a finding with no letter or digit
      outside its placeholders has nothing left to remove.
    - **PLACE** (option C): an ``ADDRESS`` finding whose whole value is exactly one known destination is still
      replaced, typed ``<PLACE_n>``. A merged span is ``PLACE`` only when every piece in it came from such a finding
      and the merged text is itself exactly one destination; otherwise it keeps the kind it has always had
      (``PLACE`` pieces count as ``ADDRESS``), so a ``NAME`` and an ``ADDRESS`` finding on the same words give
      ``<ADDRESS_n>``, as before option C (never a place hint). The
      typing changes a label only: what is replaced is exactly what was replaced before.
    - **Kept** (option B, f3-3): a ``NAME`` or ``ADDRESS`` finding whose whole value, and the text it was located on,
      is exactly one of the trip's own destinations that cannot be read as a person's name
      (:func:`app.privacy.places.is_keepable_place`, scoped by ``destinations``), standing as whole words and not right
      after an honorific or a word for a person, is a keepable piece. A merged span is left in clear only when every
      piece in it is keepable and the merged text is itself exactly one keepable destination; any other span is
      replaced exactly as in f3-2. With no ``destinations`` nothing is keepable and the output is f3-2's, byte for
      byte. A kept span takes no placeholder; the receipt counts it as ``PLACE_KEPT``.
    """
    protected = patterns.Ranges(patterns.protected_spans(text))
    pieces: list[tuple[int, int, str, bool]] = []
    for d in detections:
        kind = DETECTOR_KINDS.get(d.type)
        if not kind:
            continue
        value = d.match or ""
        if not value.strip():
            raise ScrubError("detector finding without a value")  # nothing to locate: the text cannot be cleared
        if not any(c.isalnum() for c in patterns.PLACEHOLDER.sub("", value)):
            continue  # only placeholders (already scrubbed) or punctuation: nothing left to remove
        spans = _locate(text, value)
        if not spans:
            raise ScrubError("detector finding not located")
        if kind == "ADDRESS" and is_place(value):
            kind = PLACE
        keep_value = bool(destinations) and d.type in KEEPABLE_TYPES and is_keepable_place(value, destinations)
        for start, end in spans:
            for ps, pe in _outside(protected, start, end):
                while ps < pe and text[ps] in _PIECE_TRIM:
                    ps += 1
                while pe > ps and text[pe - 1] in _PIECE_TRIM:
                    pe -= 1
                if any(c.isalnum() for c in text[ps:pe]):
                    pieces.append((ps, pe, kind, keep_value and _keepable_at(text, ps, pe, destinations)))
    merged: list[list[Any]] = []  # [start, end, kind, longest piece length, every piece a place, every piece kept]
    # Sorted and merged on the kind the piece had before option C, so ties between kinds resolve as they always did.
    for ps, pe, kind, keep in sorted(pieces, key=lambda p: (p[0], p[1], "ADDRESS" if p[2] == PLACE else p[2])):
        base = "ADDRESS" if kind == PLACE else kind
        if merged and ps < merged[-1][1]:
            last = merged[-1]
            last[1] = max(last[1], pe)
            if pe - ps > last[3]:
                last[2], last[3] = base, pe - ps
            last[4] = last[4] and kind == PLACE
            last[5] = last[5] and keep
        else:
            merged.append([ps, pe, base, pe - ps, kind == PLACE, keep])
    spans_out = []
    for s, e, k, _, all_place, all_keep in merged:
        if all_keep and _keepable_at(text, s, e, destinations):
            reg.kept_places += 1  # option B: the trip's own destination stays in clear
            continue
        if all_place and is_place(text[s:e]):
            k = PLACE
        spans_out.append((s, e, reg.placeholder(k, fold(text[s:e]), text[s:e], "detector")))
    return _apply(text, spans_out)


def _keepable_at(text: str, start: int, end: int, destinations: Sequence[str]) -> bool:
    """Option B: ``text[start:end]`` is exactly one keepable destination of the trip, stands as whole words, and does
    not follow an honorific or a word for a person ("chị Sa Pa", "guest Hội An" stay masked)."""
    if not destinations or not is_keepable_place(text[start:end], destinations):
        return False
    if (start > 0 and text[start - 1].isalnum()) or (end < len(text) and text[end].isalnum()):
        return False
    before = names.mark(text[max(0, start - _PERSON_BEFORE_WINDOW) : start])
    return _PERSON_BEFORE.search(before) is None


def _locate(text: str, value: str) -> list[tuple[int, int]]:
    tokens = value.split()
    if not tokens:
        return []
    body = r"\s+".join(re.escape(t) for t in tokens)
    lead = r"(?<!\w)" if re.match(r"\w", tokens[0]) else ""
    trail = r"(?!\w)" if re.search(r"\w$", tokens[-1]) else ""
    for rx, flags in ((lead + body + trail, 0), (lead + body + trail, re.IGNORECASE), (body, re.IGNORECASE)):
        found = [(m.start(), m.end()) for m in re.finditer(rx, text, flags)]
        if found:
            return found
    folded, index = fold_with_map(text)
    folded_tokens = fold(" ".join(tokens)).split()
    if not folded_tokens:
        return []
    rx = r"\s+".join(re.escape(t) for t in folded_tokens)
    out = []
    for m in re.finditer(rx, folded):
        start, end = original_span(index, m.start(), m.end())
        out.append((start, extend_over_marks(text, end)))
    return out


def _outside(protected: patterns.Ranges, start: int, end: int) -> list[tuple[int, int]]:
    """``[start, end)`` minus the protected ranges."""
    out, pos = [], start
    i = bisect_right(protected.ends, start)
    while i < len(protected.starts) and protected.starts[i] < end:
        if protected.starts[i] > pos:
            out.append((pos, protected.starts[i]))
        pos = max(pos, protected.ends[i])
        i += 1
    if pos < end:
        out.append((pos, end))
    return out
