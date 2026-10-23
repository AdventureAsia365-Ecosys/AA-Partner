"""The Stage 1 inquiry form (C-01): its AA-prebuilt definition and the server validator (spec §5 "Stage 1", design v3
§2.4 C-01, Fix E5).

One definition, ``stage1.v1``, serves every agency in Slice 1 (``crm.form_definition.schema`` stores a snapshot of
:func:`definition`; conditional branches, R31, arrive with CRM-15). The web renders the form from the definition the
public GET returns and words every field and option code itself (EN/VI); the server never trusts the browser:
:func:`validate` checks every answer again and returns either the normalised :class:`Stage1Answers` or the list of
field codes that failed.

Rules from the spec and the card:

- budget is a band per person, excluding flights (USD bands, spec §5 rule 2); trip style is a band too;
- the prospect's name and e-mail are required, the phone optional (E.164, starting with ``+``);
- companions are optional, names only (Fix E5): no e-mail is asked before the portal invite (C-07);
- ``consent_contact`` must be true, or nothing is stored (card R test); ``consent_marketing`` is optional;
- unknown keys are dropped and counted (``hidden_answers_dropped``), never stored.

Names are NFC-normalised so a Vietnamese name round-trips byte for byte.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from app.vault.blind_index import NormalisationError, normalise_email, normalise_phone

SCHEMA_KEY = "stage1.v1"
VERSION = 1

DESTINATIONS = ("vietnam", "laos", "cambodia", "thailand", "myanmar", "bhutan", "nepal", "indonesia", "not_sure")
PARTY_TYPES = ("solo", "couple", "family", "friends", "group")
BUDGET_BANDS = ("under_1000", "1000_2000", "2000_3500", "3500_5000", "over_5000", "not_sure")
TRIP_STYLES = ("relaxed", "balanced", "adventure")
CHANNELS = ("email", "phone", "sms", "whatsapp")
HEARD_FROM = ("search", "social", "friend", "aa_content", "returning", "other")
LOCALES = ("en", "vi")

MAX_NAME = 120
MAX_MESSAGE = 2000
MAX_COMPANIONS = 8
MAX_CHILDREN = 20
MAX_ADULTS = 50
MAX_NIGHTS = 90
MAX_FLEX = 60

_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_CONTROL = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]")

# Keys the page sends that are not answers (the honeypot field is the form's own name and handled first).
META_KEYS = frozenset({"client_submission_id", "locale"})


def definition() -> dict[str, Any]:
    """The field list the web renders (codes only; labels are the web's i18n)."""
    return {
        "schema_key": SCHEMA_KEY,
        "version": VERSION,
        "fields": [
            {"name": "destinations", "type": "multi_select", "required": True, "options": list(DESTINATIONS)},
            {"name": "month", "type": "month", "required": False},
            {"name": "start_date", "type": "date", "required": False},
            {"name": "end_date", "type": "date", "required": False},
            {"name": "date_flex_days", "type": "integer", "required": False, "min": 0, "max": MAX_FLEX},
            {"name": "nights", "type": "integer", "required": False, "min": 1, "max": MAX_NIGHTS},
            {"name": "adults", "type": "integer", "required": True, "min": 1, "max": MAX_ADULTS},
            {
                "name": "child_ages",
                "type": "integer_list",
                "required": False,
                "min": 0,
                "max": 17,
                "max_items": MAX_CHILDREN,
            },
            {"name": "party_type", "type": "select", "required": False, "options": list(PARTY_TYPES)},
            {
                "name": "budget_band",
                "type": "select",
                "required": True,
                "options": list(BUDGET_BANDS),
                "note": "per_person_excluding_flights_usd",
            },
            {"name": "trip_style", "type": "select", "required": False, "options": list(TRIP_STYLES)},
            {"name": "name", "type": "text", "required": True, "max_length": MAX_NAME},
            {"name": "email", "type": "email", "required": True},
            {"name": "phone", "type": "phone", "required": False},
            {"name": "preferred_channel", "type": "select", "required": False, "options": list(CHANNELS)},
            {"name": "heard_from", "type": "select", "required": False, "options": list(HEARD_FROM)},
            {"name": "message", "type": "long_text", "required": False, "max_length": MAX_MESSAGE},
            {
                "name": "companions",
                "type": "name_list",
                "required": False,
                "max_items": MAX_COMPANIONS,
                "collapsed": True,
            },
            {"name": "consent_contact", "type": "consent", "required": True},
            {"name": "consent_marketing", "type": "consent", "required": False},
        ],
    }


@dataclass(frozen=True)
class Stage1Answers:
    destinations: tuple[str, ...]
    adults: int
    budget_band: str
    name: str
    email: str
    consent_contact: bool
    month: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    date_flex_days: int = 0
    nights: int | None = None
    child_ages: tuple[int, ...] = ()
    party_type: str | None = None
    trip_style: str | None = None
    phone: str | None = None
    preferred_channel: str | None = None
    heard_from: str | None = None
    message: str | None = None
    companions: tuple[str, ...] = ()
    consent_marketing: bool = False
    locale: str = "en"
    client_submission_id: str = ""
    hidden_answers_dropped: int = field(default=0, compare=False)

    def as_json(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("hidden_answers_dropped")
        out["destinations"] = list(self.destinations)
        out["child_ages"] = list(self.child_ages)
        out["companions"] = list(self.companions)
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Stage1Answers:
        """Rebuild stored answers (already validated when stored)."""
        return cls(
            **{
                **data,
                "destinations": tuple(data.get("destinations") or ()),
                "child_ages": tuple(data.get("child_ages") or ()),
                "companions": tuple(data.get("companions") or ()),
            }
        )

    @property
    def month_date(self) -> date | None:
        return date.fromisoformat(f"{self.month}-01") if self.month else None


class Stage1Invalid(ValueError):
    """Answers that fail validation; ``fields`` holds the field codes (never the values)."""

    def __init__(self, fields: list[str]) -> None:
        super().__init__("invalid_submission")
        self.fields = sorted(set(fields))


class ConsentMissing(ValueError):
    """The contact consent box was not ticked: nothing may be stored (card R test)."""


def clean_text(value: Any, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError
    text = unicodedata.normalize("NFC", _CONTROL.sub("", value)).strip()
    if len(text) > max_length:
        raise ValueError
    return text or None


def _int(value: Any, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError
    number = int(value)
    if not lo <= number <= hi:
        raise ValueError
    return number


def _choice(value: Any, options: tuple[str, ...]) -> str | None:
    if value in (None, ""):
        return None
    if value not in options:
        raise ValueError
    return str(value)


def _date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError
    return date.fromisoformat(value).isoformat()


def validate(raw: dict[str, Any], *, honeypot_field: str) -> Stage1Answers:
    """Normalised answers, or :class:`ConsentMissing` / :class:`Stage1Invalid`. The honeypot is checked by the
    caller first (:func:`honeypot_filled`)."""
    if not isinstance(raw, dict):
        raise Stage1Invalid(["body"])
    if raw.get("consent_contact") is not True:
        raise ConsentMissing
    known = {f["name"] for f in definition()["fields"]} | META_KEYS | {honeypot_field}
    dropped = sum(1 for k in raw if k not in known)
    bad: list[str] = []
    out: dict[str, Any] = {"consent_contact": True, "hidden_answers_dropped": dropped}

    def take(name: str, fn: Any) -> None:
        try:
            out[name] = fn(raw.get(name))
        except (ValueError, TypeError, NormalisationError):
            bad.append(name)

    def destinations(v: Any) -> tuple[str, ...]:
        if not isinstance(v, list) or not v or len(v) > len(DESTINATIONS):
            raise ValueError
        codes = tuple(dict.fromkeys(str(x) for x in v))
        if any(c not in DESTINATIONS for c in codes):
            raise ValueError
        return codes

    def child_ages(v: Any) -> tuple[int, ...]:
        if v in (None, ""):
            return ()
        if not isinstance(v, list) or len(v) > MAX_CHILDREN:
            raise ValueError
        return tuple(_int(x, 0, 17) for x in v)

    def companions(v: Any) -> tuple[str, ...]:
        if v in (None, ""):
            return ()
        if not isinstance(v, list) or len(v) > MAX_COMPANIONS:
            raise ValueError
        names = [clean_text(x, MAX_NAME) for x in v]
        return tuple(n for n in names if n)

    def required_name(v: Any) -> str:
        text = clean_text(v, MAX_NAME)
        if not text:
            raise ValueError
        return text

    def phone(v: Any) -> str | None:
        text = clean_text(v, 40)
        return normalise_phone(text) if text else None

    def flag(v: Any) -> bool:
        if v in (None, False):
            return False
        if v is True:
            return True
        raise ValueError

    def submission_id(v: Any) -> str:
        return str(uuid.UUID(str(v)))

    take("destinations", destinations)
    take("month", lambda v: _month(v))
    take("start_date", _date)
    take("end_date", _date)
    take("date_flex_days", lambda v: 0 if v in (None, "") else _int(v, 0, MAX_FLEX))
    take("nights", lambda v: None if v in (None, "") else _int(v, 1, MAX_NIGHTS))
    take("adults", lambda v: _int(v, 1, MAX_ADULTS))
    take("child_ages", child_ages)
    take("party_type", lambda v: _choice(v, PARTY_TYPES))
    take("budget_band", lambda v: _required(_choice(v, BUDGET_BANDS)))
    take("trip_style", lambda v: _choice(v, TRIP_STYLES))
    take("name", required_name)
    take("email", lambda v: normalise_email(_required(clean_text(v, 254))))
    take("phone", phone)
    take("preferred_channel", lambda v: _choice(v, CHANNELS))
    take("heard_from", lambda v: _choice(v, HEARD_FROM))
    take("message", lambda v: clean_text(v, MAX_MESSAGE))
    take("companions", companions)
    take("consent_marketing", flag)
    take("locale", lambda v: _choice(v, LOCALES) or "en")
    take("client_submission_id", submission_id)
    if not bad:
        if out.get("start_date") and out.get("end_date") and out["end_date"] < out["start_date"]:
            bad.append("end_date")
        if out.get("preferred_channel") in ("phone", "sms", "whatsapp") and not out.get("phone"):
            bad.append("phone")
    if bad:
        raise Stage1Invalid(bad)
    return Stage1Answers(**out)


def honeypot_filled(raw: Any, honeypot_field: str) -> bool:
    """True when the hidden field bots fill holds anything (card R test: such a submission leaves no row)."""
    if not isinstance(raw, dict):
        return False
    value = raw.get(honeypot_field)
    return value not in (None, "", False)


def _required(value: Any) -> Any:
    if value in (None, ""):
        raise ValueError
    return value


def _month(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _MONTH.match(value):
        raise ValueError
    return value


# ── Trip title (generated from codes, never from a name) ─────────────────────────────────────────
DESTINATION_LABELS = {
    "en": {
        "vietnam": "Vietnam",
        "laos": "Laos",
        "cambodia": "Cambodia",
        "thailand": "Thailand",
        "myanmar": "Myanmar",
        "bhutan": "Bhutan",
        "nepal": "Nepal",
        "indonesia": "Indonesia",
        "not_sure": "Asia",
    },
    "vi": {
        "vietnam": "Việt Nam",
        "laos": "Lào",
        "cambodia": "Campuchia",
        "thailand": "Thái Lan",
        "myanmar": "Myanmar",
        "bhutan": "Bhutan",
        "nepal": "Nepal",
        "indonesia": "Indonesia",
        "not_sure": "Châu Á",
    },
}


def trip_title(answers: Stage1Answers, locale: str) -> str:
    """``Vietnam + Laos, 10 nights`` / ``Việt Nam + Lào, 10 đêm``: the agency's language (its default locale)."""
    lang = "vi" if locale.startswith("vi") else "en"
    labels = DESTINATION_LABELS[lang]
    places = [labels[c] for c in answers.destinations if c != "not_sure"] or [labels["not_sure"]]
    head = " + ".join(places[:3]) + (" …" if len(places) > 3 else "")
    if answers.nights:
        unit = "đêm" if lang == "vi" else ("night" if answers.nights == 1 else "nights")
        return f"{head}, {answers.nights} {unit}"[:120]
    return (f"Chuyến đi {head}" if lang == "vi" else f"{head} trip")[:120]
