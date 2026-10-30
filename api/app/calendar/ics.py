"""RFC 5545 rendering of a member's calendar feed (AA-961, card CRM-30).

The feed leaves the CRM (the member subscribes to it from their own Google / Outlook / Apple calendar), so it carries
**no personal data and no money**: no trip title (it may name the client), no amount, no client name. Each event is
a fixed English label, the short trip reference (the first 8 characters of its id) and a link back to the trip in the
CRM, where the member signs in to see the rest.

- All-day events (departures, returns, due dates, signing deadlines) are ``DTSTART;VALUE=DATE`` / ``DTEND`` the next
  day; tasks and calls are UTC date-times (a task lasts 15 minutes in the feed).
- ``UID`` is stable per record (``<id>@crm``), so a moved due date updates the same event in the subscriber.
- Text is escaped (backslash, comma, semicolon, newline) and lines are folded at 75 octets with CRLF endings.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

PRODID = "-//Adventure Asia//Partner CRM calendar//EN"
TASK_MINUTES = 15
LABELS = {
    "departure": "Departure",
    "return": "Return",
    "payment_due": "Payment due",
    "invoice_due": "Invoice due",
    "signing_deadline": "Signatures due",
    "task": "Task due",
    "call": "Client call",
}
PAYMENT_LABELS = {"deposit": "Deposit due", "balance": "Balance due"}


def escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Fold one content line at 75 octets (continuation lines start with a space), never inside a UTF-8 sequence."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts: list[str] = []
    current = b""
    limit = 75
    for ch in line:
        b = ch.encode("utf-8")
        if len(current) + len(b) > limit:
            parts.append(current.decode("utf-8"))
            current = b""
            limit = 74  # the leading space takes one octet
        current += b
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def _utc(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _date(value: dt.date) -> str:
    return value.strftime("%Y%m%d")


def summary(event: dict[str, Any]) -> str:
    kind = event["kind"]
    label = LABELS.get(kind, "Event")
    if kind == "payment_due":
        label = PAYMENT_LABELS.get(event.get("payment_kind") or "", label)
    if kind == "invoice_due" and event.get("invoice_number"):
        label = f"Invoice {event['invoice_number']} due"
    if kind == "signing_deadline" and event.get("pending_signatures"):
        label = f"Signatures due ({event['pending_signatures']} pending)"
    if kind == "task" and event.get("task_kind"):
        label = f"Task due: {event['task_kind'].replace('_', ' ')}"
    trip = event.get("trip_id")
    return f"{label} · trip {trip[:8]}" if trip else label


def render(events: Iterable[dict[str, Any]], *, calendar_name: str, web_origin: str, now: dt.datetime) -> str:
    """The whole ``VCALENDAR`` as text (CRLF line endings)."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape(calendar_name)}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    stamp = _utc(now)
    for e in events:
        lines += ["BEGIN:VEVENT", f"UID:{e['id']}@crm", f"DTSTAMP:{stamp}"]
        if e.get("all_day", True):
            day: dt.date = e["date"]
            lines += [f"DTSTART;VALUE=DATE:{_date(day)}", f"DTEND;VALUE=DATE:{_date(day + dt.timedelta(days=1))}"]
            lines.append("TRANSP:TRANSPARENT")
        else:
            start: dt.datetime = e["starts_at"]
            end: dt.datetime = e.get("ends_at") or start + dt.timedelta(minutes=TASK_MINUTES)
            lines += [f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end)}"]
        lines.append(f"SUMMARY:{escape(summary(e))}")
        if e.get("trip_id") and web_origin:
            url = f"{web_origin}/pipeline/{e['trip_id']}"
            lines += [f"URL:{url}", f"DESCRIPTION:{escape('Open the trip in the CRM: ' + url)}"]
        lines.append(f"CATEGORIES:{e['kind'].upper()}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"
