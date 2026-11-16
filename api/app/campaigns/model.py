"""Pure rules of the campaigns (AA-968, card CRM-36; design v3 R14; root ADR 0019 d21). No database, no I/O.

- :func:`content_gate`: the d21 content gate without an LLM, run when a campaign is saved, when it is scheduled and on
  each send day. A campaign links to at least one AA subject (an AA tour Adventure Asia still serves, an AA campaign
  block CIS still lists, or brand content with no sales call to action); every web link in its subject or text goes
  to an AA tour page of the agency's client host (``<client origin>/trip/<tour key>``, a tour the campaign names) or to
  the TripPlanner site (``TRIPPLANNER_SITE_ORIGIN``); brand content links to neither. ``mailto:`` and ``tel:`` links
  are contact, not sales links: they pass. The answer is a sorted list of codes (empty = passed); the web words them.
- :func:`spread`: card CRM-36 E "a list over the mailbox daily limit is spread over days": each client gets the first
  day from the start day on where the mailbox still has room, the room of a day being the daily limit (the platform
  setting ``crm.campaign.mailbox_daily_limit`` or the agency's own value) minus what the agency's campaigns already
  planned or queued on that mailbox that day.
- :func:`footer`: the unsubscribe lines every campaign mail ends with (the agency's name, the one-click link).
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

STATES = ("draft", "scheduled", "sending", "held", "sent", "cancelled")
EDITABLE = frozenset({"draft", "held"})
LIVE = frozenset({"scheduled", "sending"})
CANCELLABLE = frozenset({"draft", "scheduled", "sending", "held"})
LOCALES = ("en", "vi")

# Hold codes (crm.campaign.hold_code).
HOLD_CONTENT = "content_gate"
HOLD_RETIRED = "aa_tour_retired"
HOLD_FEATURE = "feature_off"
HOLD_MAILBOX = "no_mailbox"
HOLD_NOT_CONFIGURED = "not_configured"

# Gate codes (crm.campaign.gate_codes).
NO_SUBJECT = "no_aa_subject"
TOUR_NOT_SERVED = "aa_tour_not_served"
CONTENT_UNLISTED = "aa_content_unlisted"
LINK_NOT_ALLOWED = "link_not_allowed"
TOUR_LINK_NOT_LISTED = "tour_link_not_listed"
BRAND_SALES_LINK = "brand_has_sales_link"
EMPTY_SUBJECT = "empty_subject"
EMPTY_BODY = "empty_body"

# Field sizes: the crm-073 CHECKs and the composer's own bounds (not policy: a setting would not change the table).
NAME_CHARS = 120  # technical-constant: crm.campaign name CHECK (1..120)
SUBJECT_CHARS = 300  # technical-constant: app.mail.compose.MAX_SUBJECT
BODY_CHARS = 20_000  # technical-constant: a campaign text, far under app.mail.compose.MAX_BODY
LIST_ITEMS = 20  # technical-constant: crm.campaign aa_tour_ids / audience_tags CHECK cardinality
TAG_CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
TOUR_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")

_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'`]+")
_TRAILING = ".,;:!?)]}*_"
_TOUR_PATH = re.compile(r"^/trip/([0-9A-Fa-f-]{36})/?$")


def links(text: str) -> list[str]:
    """The web links of a text (``http(s)://`` or ``www.``), trailing punctuation left out."""
    return [m.group(0).rstrip(_TRAILING) for m in _URL.finditer(text or "")]


def _origin_host(origin: str | None) -> str | None:
    if not origin:
        return None
    try:
        return (urlsplit(origin).hostname or "").lower() or None
    except ValueError:
        return None


def _tour_key(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError):
        return None


def _subject_codes(
    subject: str, body: str, tours: list[str], tour_states: Mapping[str, str | None], content_listed: bool | None,
    brand_only: bool,
) -> set[str]:  # fmt: skip
    """The AA subject and the text being there (d21 "every piece links to at least one AA subject")."""
    codes: set[str] = set()
    if not subject.strip():
        codes.add(EMPTY_SUBJECT)
    if not body.strip():
        codes.add(EMPTY_BODY)
    if not tours and content_listed is None and not brand_only:
        codes.add(NO_SUBJECT)
    if content_listed is False:
        codes.add(CONTENT_UNLISTED)
    if any(tour_states.get(t) != "served" for t in tours):
        codes.add(TOUR_NOT_SERVED)
    return codes


def _link_kind(link: str, client_host: str | None, planner_host: str | None) -> tuple[str, str | None]:
    """``("tour", key)`` for an AA tour page of the client host, ``("planner", None)`` for the TripPlanner site,
    ``("other", None)`` for anything else (another host, http, a port, credentials in the URL)."""
    url = link if "://" in link else f"https://{link}"
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return "other", None
    if parts.scheme != "https" or parts.username is not None or parts.password is not None or port not in (None, 443):
        return "other", None
    tour = _TOUR_PATH.match(parts.path or "")
    if client_host and host == client_host and tour:
        return "tour", _tour_key(tour.group(1))
    if planner_host and host == planner_host:
        return "planner", None
    return "other", None


def _link_codes(
    text: str, named: set[str | None], brand_only: bool, client: str | None, planner: str | None
) -> set[str]:
    """d21 "sales links and CTAs may point only to AA tour pages or the tenant's TripPlanner site"; brand content has
    no sales link at all."""
    codes: set[str] = set()
    client_host, planner_host = _origin_host(client), _origin_host(planner)
    for link in links(text):
        kind, key = _link_kind(link, client_host, planner_host)
        if kind == "other":
            codes.add(LINK_NOT_ALLOWED)
            continue
        if kind == "tour" and key not in named:
            codes.add(TOUR_LINK_NOT_LISTED)
        if brand_only:
            codes.add(BRAND_SALES_LINK)
    return codes


def content_gate(
    *,
    subject: str,
    body: str,
    aa_tour_ids: Iterable[str],
    tour_states: Mapping[str, str | None],
    content_listed: bool | None,
    brand_only: bool,
    client_origin: str | None,
    tripplanner_origin: str | None,
) -> list[str]:
    """The d21 refusal codes of a campaign's content (empty = it passes). ``tour_states``: the CRM catalogue cache's
    ``served_state`` of each named tour (None = not in the cache); ``content_listed``: whether the AA campaign block
    is still listed (None = the campaign names no block)."""
    tours = list(aa_tour_ids)
    named = {_tour_key(t) for t in tours} - {None}
    codes = _subject_codes(subject, body, tours, tour_states, content_listed, brand_only)
    codes |= _link_codes(f"{subject}\n{body}", named, brand_only, client_origin, tripplanner_origin)
    return sorted(codes)


def hold_for(codes: Iterable[str]) -> str | None:
    """The hold code of a live campaign whose gate refused it on a send day (a retired tour is its own reason)."""
    found = set(codes)
    if not found:
        return None
    return HOLD_RETIRED if TOUR_NOT_SERVED in found else HOLD_CONTENT


def spread(count: int, start: dt.date, daily_limit: int, used: Mapping[dt.date, int]) -> list[dt.date]:
    """The send day of each of ``count`` clients in order: the first day from ``start`` on with room left (the
    limit minus ``used`` that day, which the call does not change). ``daily_limit`` >= 1."""
    if daily_limit < 1:
        raise ValueError("daily_limit must be at least 1")
    days: list[dt.date] = []
    day = start
    room = max(daily_limit - used.get(day, 0), 0)
    while len(days) < count:
        while room <= 0:
            day += dt.timedelta(days=1)
            room = max(daily_limit - used.get(day, 0), 0)
        take = min(room, count - len(days))
        days.extend([day] * take)
        room -= take
    return days


FOOTER = {
    "en": "\n\n--\nYou receive this e-mail because you agreed to hear from {agency}.\nUnsubscribe: {link}",
    "vi": "\n\n--\nBạn nhận e-mail này vì đã đồng ý nhận tin từ {agency}.\nHuỷ nhận tin: {link}",
}


def footer(locale: str, agency: str, link: str) -> str:
    return FOOTER.get(locale, FOOTER["en"]).format(agency=agency or "us", link=link)


def page_url(client_origin: str, token: str) -> str:
    """The client's preferences page of one mail (the web, ``/c/u/<token>`` on the client host)."""
    return f"{client_origin.rstrip('/')}/c/u/{token}"


def one_click_url(client_origin: str, token: str) -> str:
    """The RFC 8058 one-click address (the API's public route on the client host's ``/crm/public/*`` rule)."""
    return f"{client_origin.rstrip('/')}/crm/public/v1/unsubscribe/{token}"
