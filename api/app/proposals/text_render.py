"""The text proposal renderers (card CRM-22, AA-951; design v3 M4, F4 "email text or WhatsApp text, all three share one
``price_hash``", WF-5). Pure functions: a :class:`Snapshot` of one proposal version in, plain text out. The same
snapshot renders as an e-mail (subject + body) or a WhatsApp message, in English or Vietnamese; every rendering ends
with the version's price reference (the first 12 hex digits of its ``price_hash``), so a reader can tell which version
a text came from and the send path can refuse a text whose hash is not the version's (card R).

What a client text never carries: a net price, a margin, a per-line price (D1: only the difference of an alternative
against the included option, and only for ``total_with_deltas``), a traveller's name other than the greeting the
agent's template asks for, visa advice (only the official links, :func:`app.proposals.prep_packs.sections`).

Merge fields of a template (R9; the names of the CIS starter templates, AA-977): ``{{traveller_first_name}}``,
``{{trip_name}}``, ``{{agency_name}}``, ``{{agent_name}}``, ``{{destination}}``, ``{{departure_date}}`` and
``{{proposal}}`` (where the proposal goes; without it the proposal follows the template). A field without a value is
left out (a line that held only that field goes) and reported in ``unfilled``.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.proposals.prep_packs import Section

FORMATS = ("email", "whatsapp")
LOCALES = ("en", "vi")
ZERO_DECIMAL = frozenset({"VND", "JPY", "KRW", "CLP", "ISK", "UGX", "XAF", "XOF", "PYG", "RWF"})
MERGE_FIELDS = (
    "traveller_first_name",
    "trip_name",
    "agency_name",
    "agent_name",
    "destination",
    "departure_date",
    "proposal",
)
_FIELD = re.compile(r"\{\{\s*([a-z_]{1,40})\s*\}\}")

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "subject": "Your trip proposal: {trip} (version {version})",
        "heading": "Your trip proposal: {trip}",
        "version": "Version {version}, prepared by {agency}",
        "day": "Day {n}",
        "included": "Included",
        "alternatives": "Other choices",
        "on_request": "on request",
        "pay_on_arrival": "paid on arrival",
        "rooms": "Rooms",
        "room": {"single": "single", "double": "double", "twin": "twin", "triple": "triple", "family": "family"},
        "adults": "{n} adult(s)",
        "children": "{n} child(ren)",
        "before_you_go": "Before you go",
        "visa_links": "Official information",
        "visa_none": "Check the official government website for your passport.",
        "insurance": "Travel insurance: take out a policy that covers medical treatment, emergency evacuation and "
        "trip cancellation before you travel, and keep the policy number with you.",
        "fitness": "Fitness: this trip includes trekking or high-altitude days. Check with your doctor that you "
        "are fit for it, and tell us about any health condition before you book.",
        "price": "Price",
        "total": "Total",
        "deposit": "Deposit to confirm",
        "balance": "Balance",
        "due": "due {date}",
        "on_arrival_total": "Paid on arrival (not included in the total)",
        "link": "See the details, choose your options and accept here: {link}",
        "reference": "Proposal reference: {ref}",
        "fallback_name": "there",
    },
    "vi": {
        "subject": "Đề xuất chuyến đi của bạn: {trip} (phiên bản {version})",
        "heading": "Đề xuất chuyến đi: {trip}",
        "version": "Phiên bản {version}, do {agency} chuẩn bị",
        "day": "Ngày {n}",
        "included": "Đã bao gồm",
        "alternatives": "Lựa chọn khác",
        "on_request": "theo yêu cầu",
        "pay_on_arrival": "trả khi đến nơi",
        "rooms": "Phòng",
        "room": {"single": "đơn", "double": "đôi", "twin": "hai giường", "triple": "ba người", "family": "gia đình"},
        "adults": "{n} người lớn",
        "children": "{n} trẻ em",
        "before_you_go": "Trước khi đi",
        "visa_links": "Thông tin chính thức",
        "visa_none": "Hãy kiểm tra trang web chính thức của chính phủ cho hộ chiếu của bạn.",
        "insurance": "Bảo hiểm du lịch: hãy mua bảo hiểm chi trả điều trị y tế, vận chuyển cấp cứu và huỷ chuyến trước "
        "khi đi, và mang theo số hợp đồng bảo hiểm.",
        "fitness": "Thể lực: chuyến đi có những ngày đi bộ đường dài hoặc ở độ cao lớn. Hãy hỏi ý kiến bác sĩ "
        "về sức khoẻ của bạn và cho chúng tôi biết mọi vấn đề sức khoẻ trước khi đặt.",
        "price": "Giá",
        "total": "Tổng cộng",
        "deposit": "Đặt cọc để xác nhận",
        "balance": "Số còn lại",
        "due": "hạn {date}",
        "on_arrival_total": "Trả khi đến nơi (không tính trong tổng)",
        "link": "Xem chi tiết, chọn dịch vụ và chấp nhận tại đây: {link}",
        "reference": "Mã tham chiếu đề xuất: {ref}",
        "fallback_name": "bạn",
    },
}
_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DAYS_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DAYS_VI = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật")


@dataclass(frozen=True)
class Alternative:
    title: str
    delta: Decimal | None  # sell difference against the included option (None for total_only)
    on_request: bool = False


@dataclass(frozen=True)
class Item:
    block: str
    title: str
    pay_on_arrival: bool = False
    on_request: bool = False
    alternatives: tuple[Alternative, ...] = ()


@dataclass(frozen=True)
class Day:
    day_no: int
    date: dt.date | None
    title: str | None
    place: str | None
    utc_offset: str | None
    notes: str | None
    items: tuple[Item, ...] = ()


@dataclass(frozen=True)
class Pack:
    title: str
    sections: tuple[Section, ...]


@dataclass(frozen=True)
class BeforeYouGo:
    notes: str | None
    packs: tuple[Pack, ...] = ()
    fitness_warning: bool = False


@dataclass(frozen=True)
class Snapshot:
    """One proposal version as the client sees it (no net, no margin)."""

    kind: str  # text | builder
    version: int
    price_hash: str  # hex
    agency_name: str
    trip_title: str
    currency: str
    total: Decimal
    deposit: Decimal
    balance_due_on: dt.date | None
    pay_on_arrival_total: Decimal = Decimal(0)
    body: str = ""  # the text version's text, or the builder version's intro
    starts_on: dt.date | None = None
    rooms: tuple[dict[str, Any], ...] = ()
    days: tuple[Day, ...] = ()
    before_you_go: BeforeYouGo | None = None


@dataclass(frozen=True)
class Rendered:
    format: str
    locale: str
    subject: str | None
    body: str
    price_hash: str
    unfilled: tuple[str, ...] = field(default_factory=tuple)


# ── Formatting ───────────────────────────────────────────────────────────────────────────────────


def money(amount: Decimal, currency: str, locale: str) -> str:
    places = 0 if currency in ZERO_DECIMAL else 2
    q = Decimal(amount).quantize(Decimal(1) if places == 0 else Decimal("0.01"))
    sign = "-" if q < 0 else ""
    whole, _, frac = f"{abs(q):.{places}f}".partition(".")
    groups = f"{int(whole):,}"
    if locale == "vi":
        text = groups.replace(",", ".") + (f",{frac}" if frac else "")
    else:
        text = groups + (f".{frac}" if frac else "")
    return f"{sign}{text} {currency}"


def delta(amount: Decimal, currency: str, locale: str) -> str:
    return ("+" if amount >= 0 else "-") + money(abs(amount), currency, locale)


def date(day: dt.date, locale: str, *, weekday: bool = True) -> str:
    if locale == "vi":
        text = f"{day.day:02d}/{day.month:02d}/{day.year}"
        return f"{_DAYS_VI[day.weekday()]}, {text}" if weekday else text
    text = f"{day.day} {_MONTHS_EN[day.month - 1]} {day.year}"
    return f"{_DAYS_EN[day.weekday()]} {text}" if weekday else text


def reference(price_hash: str) -> str:
    return price_hash[:12]


def merge(template: str, values: dict[str, str | None]) -> tuple[str, tuple[str, ...]]:
    """Fill ``{{field}}``; a field without a value is dropped (and a line that held only fields goes too). Unknown
    names are left as written (the agent sees them in the preview). Returns the text and the unfilled known fields."""
    unfilled: list[str] = []
    lines = []
    for line in template.replace("\r\n", "\n").split("\n"):
        names = _FIELD.findall(line)

        def fill(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in MERGE_FIELDS:
                return m.group(0)
            value = values.get(name)
            if not value:
                if name not in unfilled:
                    unfilled.append(name)
                return ""
            return value

        out = _FIELD.sub(fill, line)
        if names and not out.strip() and line.strip():
            continue  # the line was only fields without a value
        lines.append(out.rstrip())
    return "\n".join(lines).strip("\n"), tuple(unfilled)


# ── The proposal block ───────────────────────────────────────────────────────────────────────────


def _bold(text: str, fmt: str) -> str:
    return f"*{text}*" if fmt == "whatsapp" else text


def _rooms(rooms: tuple[dict[str, Any], ...], s: dict[str, Any]) -> str | None:
    parts = []
    for r in rooms:
        label = s["room"].get(str(r.get("type")), str(r.get("type")))
        who = [s["adults"].format(n=r.get("adults", 0))]
        if r.get("children"):
            who.append(s["children"].format(n=r["children"]))
        parts.append(f"{label} ({', '.join(who)})")
    return f"{s['rooms']}: " + "; ".join(parts) if parts else None


def _day_heading(d: Day, fmt: str, locale: str, s: dict[str, Any]) -> str:
    bits = [s["day"].format(n=d.day_no)]
    if d.date is not None:
        bits.append(date(d.date, locale))
    if d.place:
        bits.append(d.place + (f" (UTC{d.utc_offset})" if d.utc_offset else ""))
    return _bold(" · ".join(bits), fmt)


def _items(d: Day, snap: Snapshot, fmt: str, locale: str, s: dict[str, Any]) -> list[str]:
    out = []
    for it in d.items:
        flags = [s["pay_on_arrival"]] if it.pay_on_arrival else []
        if it.on_request:
            flags.append(s["on_request"])
        out.append(f"- {it.block}: {it.title}" + (f" ({', '.join(flags)})" if flags else ""))
        if it.alternatives:
            alts = []
            for a in it.alternatives:
                extra = [delta(a.delta, snap.currency, locale)] if a.delta is not None else []
                if a.on_request:
                    extra.append(s["on_request"])
                alts.append(a.title + (f" ({', '.join(extra)})" if extra else ""))
            out.append(f"  {s['alternatives']}: " + "; ".join(alts))
    return out


def _before_you_go(b: BeforeYouGo, fmt: str, s: dict[str, Any]) -> list[str]:
    out = [_bold(s["before_you_go"], fmt)]
    if b.notes:
        out.append(b.notes.strip())
    for pack in b.packs:
        out.append("")
        out.append(_bold(pack.title, fmt))
        for sec in pack.sections:
            if sec.visa:
                head = f"{sec.heading}: " if sec.heading else ""
                if sec.official_links:
                    urls = ", ".join(url for _label, url in sec.official_links)
                    out.append(f"{head}{s['visa_links']}: {urls}")
                else:
                    out.append(f"{head}{s['visa_none']}")
            elif fmt == "whatsapp":
                out.append((f"{sec.heading}: " if sec.heading else "") + " ".join(sec.text.split()))
            else:
                if sec.heading:
                    out.append(sec.heading)
                out.append(sec.text)
    out.append("")
    out.append(s["insurance"])
    if b.fitness_warning:
        out.append(s["fitness"])
    return out


def _price(snap: Snapshot, fmt: str, locale: str, s: dict[str, Any]) -> list[str]:
    out = [_bold(s["price"], fmt), f"{s['total']}: {money(snap.total, snap.currency, locale)}"]
    out.append(f"{s['deposit']}: {money(snap.deposit, snap.currency, locale)}")
    balance = snap.total - snap.deposit
    if balance > 0:
        due = (
            f", {s['due'].format(date=date(snap.balance_due_on, locale, weekday=False))}" if snap.balance_due_on else ""
        )
        out.append(f"{s['balance']}: {money(balance, snap.currency, locale)}{due}")
    if snap.pay_on_arrival_total > 0:
        out.append(f"{s['on_arrival_total']}: {money(snap.pay_on_arrival_total, snap.currency, locale)}")
    return out


def proposal_block(snap: Snapshot, fmt: str, locale: str, link_url: str | None = None) -> str:
    s = STRINGS[locale]
    out = [_bold(s["heading"].format(trip=snap.trip_title), fmt),
           s["version"].format(version=snap.version, agency=snap.agency_name)]  # fmt: skip
    if snap.kind == "builder":
        rooms = _rooms(snap.rooms, s)
        if rooms:
            out.append(rooms)
        if snap.body.strip():
            out += ["", snap.body.strip()]
        if snap.before_you_go is not None:
            out += ["", *_before_you_go(snap.before_you_go, fmt, s)]
        for d in snap.days:
            out += ["", _day_heading(d, fmt, locale, s)]
            if d.title:
                out.append(d.title)
            out += _items(d, snap, fmt, locale, s)
            if d.notes:
                out.append(d.notes.strip())
    elif snap.body.strip():
        out += ["", snap.body.strip()]
    out += ["", *_price(snap, fmt, locale, s)]
    if link_url:
        out += ["", s["link"].format(link=link_url)]
    out += ["", s["reference"].format(ref=reference(snap.price_hash))]
    return "\n".join(out)


def render(
    snap: Snapshot,
    fmt: str,
    locale: str,
    *,
    template: str | None = None,
    values: dict[str, str | None] | None = None,
    link_url: str | None = None,
) -> Rendered:
    """One text of the version: the template (merged) around the proposal block, the subject for an e-mail."""
    if fmt not in FORMATS or locale not in LOCALES:
        raise ValueError("format or locale")
    block = proposal_block(snap, fmt, locale, link_url)
    unfilled: tuple[str, ...] = ()
    if template and template.strip():
        merged, unfilled = merge(template, {**(values or {}), "proposal": block})
        body = merged if "proposal" in _FIELD.findall(template) else f"{merged}\n\n{block}"
        unfilled = tuple(u for u in unfilled if u != "proposal")
    else:
        body = block
    body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
    subject = STRINGS[locale]["subject"].format(trip=snap.trip_title, version=snap.version) if fmt == "email" else None
    return Rendered(fmt, locale, subject, body, snap.price_hash, unfilled)
