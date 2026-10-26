"""Pure rules of the day-list builder (card CRM-21a, AA-949; design v3 F3 / F5 / WF-4): no database, no I/O.

- **Choices and rules** (the Advanced section, card R): a version's choice is one option per block, the client's pick
  where there is one, else the block's default. ``requires(A, B)``: choosing A needs B chosen; ``excludes(A, B)``: A and
  B are never chosen together. :func:`violations` lists every broken rule; the API answers 422 ``option_conflict``.
- **Totals**: the client pays the chosen options' sell amounts except pay-on-arrival ones (R25: listed, never
  charged); net and margin are the agency's (margin-bearing, design §4.6). The deposit is ``deposit_pct`` of the
  charged total, rounded half up to the cent.
- **Stage 2 defaults** (spec rule 5): the rooms from the party (Fix B10: the agent edits them), the day time zone from
  the destination (R33), the lodging default from the accommodation tier.
- **Time zones** (R33, card E): :func:`utc_offset` renders the UTC offset of a day's IANA zone on that date
  (``Asia/Kathmandu`` -> ``+05:45``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CENT = Decimal("0.01")
ROOM_TYPES = ("single", "double", "twin", "triple", "family")
TIERS = ("budget", "comfort", "premium", "luxury")
BLOCK_TYPES = ("lodging", "activity", "transport", "meal", "guide", "other")
RULE_KINDS = ("requires", "excludes")
FALLBACK_TZ = "Asia/Ho_Chi_Minh"

# The intake destinations (app.intake.stage1.DESTINATIONS) and their main zone. Indonesia spans three zones: Jakarta
# is the default and the agent sets Bali / Lombok days to Asia/Makassar.
DESTINATION_TZ: Mapping[str, str] = {
    "vietnam": "Asia/Ho_Chi_Minh",
    "laos": "Asia/Vientiane",
    "cambodia": "Asia/Phnom_Penh",
    "thailand": "Asia/Bangkok",
    "myanmar": "Asia/Yangon",
    "bhutan": "Asia/Thimphu",
    "nepal": "Asia/Kathmandu",
    "indonesia": "Asia/Jakarta",
}


def valid_tz(name: str) -> bool:
    if not isinstance(name, str) or not 1 <= len(name) <= 64:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return name == "UTC" or ("/" in name and name[0].isupper())


def default_tz(destinations: Sequence[str] | None, agency_tz: str | None) -> str:
    """The zone of a new day: the trip's first known destination, else the agency's zone, else Vietnam's."""
    for code in destinations or ():
        if code in DESTINATION_TZ:
            return DESTINATION_TZ[code]
    if agency_tz and valid_tz(agency_tz):
        return agency_tz
    return FALLBACK_TZ


def utc_offset(tz: str, day: dt.date | None) -> str:
    """``+05:45`` for Asia/Kathmandu: the offset at local noon of ``day`` (today in that zone when no date)."""
    zone = ZoneInfo(tz)
    when = day or dt.datetime.now(zone).date()
    offset = dt.datetime.combine(when, dt.time(12), tzinfo=zone).utcoffset() or dt.timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{mins:02d}"


def default_rooms(adults: int, child_ages: Sequence[int], party_type: str | None) -> list[dict[str, Any]]:
    """Fix B10: the agent's starting room set from the Stage 1/2 party (counts only). Pairs of adults share a room
    (double for a couple, twin for friends or a group), an odd adult gets a single, the children join the first room
    (then a family room)."""
    adults = max(1, int(adults))
    children = len(child_ages)
    rooms: list[dict[str, Any]] = []
    pair_type = "double" if party_type in (None, "couple", "family") else "twin"
    left = adults
    while left >= 2:
        rooms.append({"type": pair_type, "adults": 2, "children": 0})
        left -= 2
    if left:
        rooms.append({"type": "single", "adults": 1, "children": 0})
    if children:
        first = rooms[0]
        first["children"] = children
        first["type"] = "family"
    return rooms[:20]


def room_pax(rooms: Sequence[Mapping[str, Any]] | None, adults: int, child_ages: Sequence[int]) -> dict[str, Any]:
    """The counts a Booking quote needs: adults / children / infants (under 2) and rooms by occupancy."""
    infants = sum(1 for a in child_ages if a < 2)
    children = len(child_ages) - infants
    if rooms:
        adults = sum(int(r.get("adults", 0)) for r in rooms) or adults
    counts: dict[str, int] = {}
    for room in rooms or ():
        counts[room["type"]] = counts.get(room["type"], 0) + 1
    return {
        "adult": max(1, adults),
        "child": children,
        "infant": infants,
        "rooms": [{"occupancy": k, "count": v} for k, v in counts.items()] or None,
    }


def check_rooms(rooms: Any) -> list[dict[str, Any]]:
    """A clean copy of a room list or ValueError: 1-20 rooms of the known types, 0-6 adults and 0-6 children each,
    at least one person per room. Counts only."""
    if not isinstance(rooms, list) or not 1 <= len(rooms) <= 20:
        raise ValueError("rooms: 1 to 20")
    out = []
    for room in rooms:
        if not isinstance(room, Mapping) or set(room) - {"type", "adults", "children"}:
            raise ValueError("room: type, adults, children")
        kind, adults, children = room.get("type"), room.get("adults", 0), room.get("children", 0)
        if kind not in ROOM_TYPES:
            raise ValueError("room type")
        if not (isinstance(adults, int) and isinstance(children, int)) or isinstance(adults, bool):
            raise ValueError("room counts")
        if not (0 <= adults <= 6 and 0 <= children <= 6 and adults + children >= 1):
            raise ValueError("room counts")
        out.append({"type": kind, "adults": adults, "children": children})
    return out


@dataclass(frozen=True)
class Rule:
    rule_id: str
    kind: str
    option_id: str
    target_option_id: str


def chosen(
    defaults: Mapping[str, str | None], picks: Mapping[str, str] | None = None
) -> dict[str, str]:  # fmt: skip
    """block -> option: the pick where there is one, else the default (blocks without either are left out)."""
    out = {b: o for b, o in defaults.items() if o}
    out.update(picks or {})
    return out


def violations(choice: Mapping[str, str], rules: Iterable[Rule]) -> list[dict[str, str]]:
    """Every rule the choice breaks (card R): an excludes pair both chosen, a requires target not chosen."""
    picked = set(choice.values())
    out = []
    for rule in rules:
        if rule.option_id not in picked:
            continue
        if rule.kind == "excludes" and rule.target_option_id in picked:
            out.append({"rule_id": rule.rule_id, "kind": rule.kind, "option_id": rule.option_id,
                        "target_option_id": rule.target_option_id})  # fmt: skip
        if rule.kind == "requires" and rule.target_option_id not in picked:
            out.append({"rule_id": rule.rule_id, "kind": rule.kind, "option_id": rule.option_id,
                        "target_option_id": rule.target_option_id})  # fmt: skip
    return out


def money(value: Any) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def deposit_of(total: Decimal, pct: Decimal) -> Decimal:
    if total <= 0:
        return Decimal("0.00")
    return min(total, max(CENT, money(total * pct / 100)))


def totals(choice: Mapping[str, str], options: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Sums over the chosen options: ``sell_total`` (charged now: pay-on-arrival excluded), ``pay_on_arrival_total``,
    ``net_total`` / ``margin_total`` (``None`` when a chosen option has no net: the margin is not known)."""
    sell = Decimal("0")
    on_arrival = Decimal("0")
    net: Decimal | None = Decimal("0")
    for option_id in choice.values():
        o = options.get(option_id)
        if o is None:
            continue
        amount = Decimal(o["sell_amount"])
        if o.get("pay_on_arrival"):
            on_arrival += amount
            continue
        sell += amount
        if net is not None:
            net = None if o.get("net_amount") is None else net + Decimal(o["net_amount"])
    return {
        "sell_total": money(sell),
        "pay_on_arrival_total": money(on_arrival),
        "net_total": money(net) if net is not None else None,
        "margin_total": money(sell - net) if net is not None else None,
    }


def tier_default(options: Sequence[Mapping[str, Any]], tier: str | None) -> str | None:
    """Spec rule 5: the first option whose tier is the lead's Stage 2 accommodation tier (and not sold out)."""
    if not tier:
        return None
    for o in options:
        if o.get("tier") == tier and o.get("availability") != "sold_out":
            return str(o["option_id"])
    return None
