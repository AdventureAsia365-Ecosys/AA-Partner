"""Tags (AA-960, card CRM-29b; design v3 R3, R29, R36): the pure rules, no database.

- **Agency tags** (``crm.tag`` kind ``agency``): the agency's own words for trips or clients (VIP, Honeymoon, Repeat
  client). A tag has a fixed ``code`` made once from its first name (:func:`code_for`: ASCII, ``a-z0-9_``, never the
  ``sys_`` prefix of a system tag); renaming keeps the code, so a workflow condition that names it keeps working.
- **System tags** (kind ``system``, codes ``sys_*``): computed by the app, never set by hand (crm-061 guard CRM55).
  :data:`TRIP_SYSTEM_TAGS` come from the AA catalogue facts of the AA tours a trip holds (R29: duration, activities,
  intensity of live CIS tours only, root ADR 0015); :data:`CLIENT_SYSTEM_TAGS` is "ready to travel again" (R36,
  :func:`ready_to_travel`). The web words a system tag by its code (``tag.sys.<code>``); the English name stored with
  the row is a fallback.
- **Ready to travel again** (R36): a client is ready when they have a completed trip, no open trip, and their last trip
  ended at least :data:`READY_AFTER_MONTHS` months ago. :func:`ready_to_travel` returns the reasons the list explains
  (codes and numbers, never free text): how long since they came home, a repeat traveller, the anniversary of their
  last trip coming up, new AA tours in a country they travelled to. The list is the agency's own marketing (it is the
  controller); AA never reads or uses it, and sending needs the client's marketing consent.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.errors import Invalid

SUBJECTS = ("trip", "client")
KINDS = ("agency", "system")
TONES = ("neutral", "accent", "success", "warning", "danger", "info", "purple")
MAX_NAME = 60
MAX_CODE = 40
MAX_TAGS = 300  # agency tags per subject (a vocabulary, not a free-text field)
SYSTEM_PREFIX = "sys_"
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_FOLD = str.maketrans({"đ": "d", "Đ": "D", "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "ß": "ss"})

# R29: the AA catalogue facets (catalog.http.v1 Facets: activities from the CIS vocabulary, intensity) and the tour's
# duration, as trip tags. Code -> English fallback name. The activity codes are CIS's (api/routers/v1_tours.py
# POOL_ACTIVITIES); an activity outside this list adds no tag (no tag per free-text activity).
ACTIVITY_TAGS: dict[str, str] = {
    "adventure_sport": "Adventure sport",
    "cultural_heritage": "Culture and heritage",
    "culinary": "Food and cooking",
    "local_immersion": "Local life",
    "trekking": "Trekking",
    "water_activities": "Water activities",
    "wellness_relaxation": "Wellness",
    "wildlife_nature": "Wildlife and nature",
}
TRIP_SYSTEM_TAGS: dict[str, str] = {
    "sys_aa_day_tour": "AA day tour",
    "sys_aa_multi_day": "AA multi-day tour",
    **{f"sys_aa_{code}": name for code, name in ACTIVITY_TAGS.items()},
    "sys_aa_easy": "Easy pace",
    "sys_aa_challenging": "Challenging",
}
READY_TAG = "sys_ready_to_travel"
CLIENT_SYSTEM_TAGS: dict[str, str] = {READY_TAG: "Ready to travel again"}
SYSTEM_TAGS: dict[str, tuple[str, str]] = {
    **{code: ("trip", name) for code, name in TRIP_SYSTEM_TAGS.items()},
    **{code: ("client", name) for code, name in CLIENT_SYSTEM_TAGS.items()},
}
SYSTEM_TONE = "info"

READY_AFTER_MONTHS = 9
ANNIVERSARY_WINDOW_DAYS = 60
OPEN_STAGES = ("inquiry", "qualifying", "proposal", "booked", "pre_departure", "travelling")


def subject_of(applies_to: str) -> str:
    if applies_to not in SUBJECTS:
        raise Invalid("invalid_subject", "trip or client", field="applies_to")
    return applies_to


def clean_name(value: Any) -> str:
    """A tag's name: spaces collapsed, 1..60 characters, no control characters (422 with the field)."""
    if not isinstance(value, str):
        raise Invalid("invalid_text", "name", field="name")
    clean = " ".join(value.split())
    if not clean:
        raise Invalid("text_required", "name", field="name")
    if len(clean) > MAX_NAME:
        raise Invalid("text_too_long", f"at most {MAX_NAME} characters", field="name")
    if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in clean):
        raise Invalid("invalid_text", "name", field="name")
    return clean


def clean_tone(value: Any) -> str:
    if value not in TONES:
        raise Invalid("invalid_tone", ", ".join(TONES), field="tone")
    return str(value)


def code_for(name: str, taken: Iterable[str] = ()) -> str:
    """The fixed code of a new agency tag: the name folded to ASCII, ``a-z0-9`` joined by ``_``, starting with a
    letter, never ``sys_``; ``_2``, ``_3`` ... when the code is taken on that subject."""
    folded = unicodedata.normalize("NFKD", name.translate(_FOLD))
    ascii_ = "".join(c for c in folded if not unicodedata.combining(c)).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "_", ascii_).strip("_")
    if not base or not base[0].isalpha():
        base = f"tag_{base}".rstrip("_")
    if base.startswith(SYSTEM_PREFIX) or base == "sys":
        base = f"my_{base}"
    base = base[:MAX_CODE].rstrip("_")
    used = set(taken)
    code, n = base, 1
    while code in used:
        n += 1
        suffix = f"_{n}"
        code = base[: MAX_CODE - len(suffix)].rstrip("_") + suffix
    return code


def is_code(value: Any) -> bool:
    return isinstance(value, str) and bool(_CODE.match(value))


# ── R29: trip system tags from the AA catalogue ──────────────────────────────────────────────────────────────────


def trip_system_tags(tours: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The system tags of one trip from the live AA tours it holds (``source_id``, ``duration_days`` and ``facets``
    (or the whole ``body``) of ``crm.aa_catalog_tour``): code -> reasons (``{"code": "aa_tour", "tour": <source
    id>}`` per tour that gives it, at most 5). A trip without a live AA tour has none."""
    out: dict[str, list[dict[str, Any]]] = {}

    def add(code: str, tour: str) -> None:
        reasons = out.setdefault(code, [])
        if len(reasons) < 5 and all(r["tour"] != tour for r in reasons):
            reasons.append({"code": "aa_tour", "tour": tour})

    for t in tours:
        tour = str(t.get("source_id") or "")[:128]
        if not tour:
            continue
        days = t.get("duration_days")
        if isinstance(days, int) and days >= 1:
            add("sys_aa_day_tour" if days == 1 else "sys_aa_multi_day", tour)
        facets = t.get("facets")
        if facets is None and isinstance(t.get("body"), Mapping):
            facets = t["body"].get("facets")
        if isinstance(facets, str):
            facets = json.loads(facets)
        if not isinstance(facets, Mapping):
            continue
        for activity in facets.get("activities") or ():
            code = re.sub(r"[^a-z0-9]+", "_", str(activity).strip().lower()).strip("_")
            if code in ACTIVITY_TAGS:
                add(f"sys_aa_{code}", tour)
        intensity = facets.get("intensity")
        if intensity in ("challenging", "strenuous"):
            add("sys_aa_challenging", tour)
        elif intensity == "easy":
            add("sys_aa_easy", tour)
    return out


# ── R36: ready to travel again ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PastTrip:
    stage: str
    start_date: dt.date | None
    end_date: dt.date | None
    countries: tuple[str, ...] = ()
    aa_tours: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClientHistory:
    archived: bool
    trips: tuple[PastTrip, ...]
    # Live AA tours per country (crm.aa_catalog_tour served): the tours the client could take next.
    served_by_country: Mapping[str, frozenset[str]] = field(default_factory=dict)


def months_between(earlier: dt.date, later: dt.date) -> int:
    """Whole months from ``earlier`` to ``later`` (0 when later is earlier)."""
    months = (later.year - earlier.year) * 12 + later.month - earlier.month
    if later.day < earlier.day:
        months -= 1
    return max(months, 0)


def _anniversary_in(start: dt.date, today: dt.date) -> int | None:
    for year in (today.year, today.year + 1):
        try:
            day = start.replace(year=year)
        except ValueError:  # 29 February
            day = dt.date(year, 2, 28)
        delta = (day - today).days
        if 0 <= delta <= ANNIVERSARY_WINDOW_DAYS:
            return delta
    return None


def ready_to_travel(history: ClientHistory, today: dt.date) -> list[dict[str, Any]] | None:
    """The reasons a client is ready to travel again (R36), most telling first, or None when they are not: archived,
    no completed trip with an end date, an open trip, or home for less than :data:`READY_AFTER_MONTHS` months."""
    if history.archived:
        return None
    if any(t.stage in OPEN_STAGES for t in history.trips):
        return None
    done = sorted(
        (t for t in history.trips if t.stage == "completed" and t.end_date is not None and t.end_date <= today),
        key=lambda t: t.end_date or dt.date.min,
    )
    if not done:
        return None
    last = done[-1]
    last_end = last.end_date or today
    months = months_between(last_end, today)
    if months < READY_AFTER_MONTHS:
        return None
    reasons: list[dict[str, Any]] = [{"code": "returned_months_ago", "months": months}]
    years = {t.end_date.year for t in done if t.end_date is not None}
    if len(done) >= 2:
        reasons.append({"code": "repeat_traveller", "trips": len(done), "years": len(years)})
    if last.start_date is not None:
        days = _anniversary_in(last.start_date, today)
        if days is not None:
            reasons.append({"code": "anniversary_soon", "days": days})
    booked = {tour for t in done for tour in t.aa_tours}
    best: tuple[int, str] | None = None
    for country in sorted({c for t in done for c in t.countries}):
        fresh = len(history.served_by_country.get(country, frozenset()) - booked)
        if fresh and (best is None or fresh > best[0]):
            best = (fresh, country)
    if best is not None:
        reasons.append({"code": "new_aa_tours", "country": best[1], "count": best[0]})
    return reasons


def countries_of(destinations: Sequence[str] | None, tour_countries: Iterable[str]) -> tuple[str, ...]:
    """The ISO countries of a past trip: its AA tours' countries plus any destination that is an ISO code."""
    out = {c for c in tour_countries if isinstance(c, str) and re.fullmatch(r"[A-Z]{2}", c)}
    for d in destinations or ():
        if isinstance(d, str) and re.fullmatch(r"[A-Za-z]{2}", d.strip()):
            out.add(d.strip().upper())
    return tuple(sorted(out))
