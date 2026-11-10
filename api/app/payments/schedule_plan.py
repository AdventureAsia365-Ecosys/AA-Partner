"""Payment schedule plans with several instalments (AA-1067, item crm-08 of the S275 study; crm-050). Pure: no
database, no clock (callers pass "today").

Two shapes:

- A **template** (``crm.tenant_settings.schedule_template``, Settings > Payments; NULL = :data:`DEFAULT_TEMPLATE`, the
  AA default 30 % at accept, 40 % 60 days before departure, the rest 30 days before): lines
  ``{kind, percent, due: {anchor: accept | before_start, days}}``, dates relative to the trip's start.
- A **plan** (``crm.proposal.schedule_plan`` of a builder version, edited in the builder before publish): the same
  lines with a date each, ``{kind, seq, percent, due_on, days_before}``. ``days_before`` is kept while the date still
  follows the template (a new day 1 moves it); a date the agent typed sets it to NULL.

Rules (both shapes): one ``deposit``, first, due at accept, with a percent; 0..:data:`MAX_INSTALMENTS` ``instalment``
lines with a percent; at most one ``balance``, last, without a percent (it takes the rest). The percents of the
deposit and the instalments add up to less than 100 with a balance, to exactly 100 without one. Due dates go forward
line by line and never after the trip's start.

Money: amounts are rounded half up **per currency** (:func:`quantum`: whole units for the zero-decimal currencies, else
cents); the deposit is at least one unit; the last line (the balance, else the last instalment) takes the rest, so the
lines always add up to exactly the total (:func:`split`). The accept writes one ``crm.payment_schedule`` row per line
(:func:`accept_rows`): a date already past is due on the day of the accept.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.errors import Invalid

KINDS = ("deposit", "instalment", "balance")
MAX_LINES = 12
MAX_INSTALMENTS = 10
MAX_DAYS_BEFORE = 730
# ISO 4217 currencies without minor units that agencies quote in (an amount is a whole number of them).
ZERO_DECIMAL = frozenset({"VND", "JPY", "KRW", "CLP", "ISK", "UGX", "XAF", "XOF", "PYG", "RWF"})
CENT = Decimal("0.01")
HUNDRED = Decimal("100")
DEFAULT_TEMPLATE: tuple[dict[str, Any], ...] = (
    {"kind": "deposit", "percent": "30", "due": {"anchor": "accept"}},
    {"kind": "instalment", "percent": "40", "due": {"anchor": "before_start", "days": 60}},
    {"kind": "balance", "due": {"anchor": "before_start", "days": 30}},
)


@dataclass(frozen=True)
class Line:
    kind: str
    seq: int
    percent: Decimal | None
    due_on: dt.date | None  # None: the deposit (due at accept), or a date not known yet (no day 1)
    days_before: int | None  # the template's days before the start, while the date follows it


# ── Money ────────────────────────────────────────────────────────────────────────────────────────


def quantum(currency: str) -> Decimal:
    return Decimal("1") if currency in ZERO_DECIMAL else CENT


def round_money(value: Decimal, currency: str) -> Decimal:
    """Half up to the currency's unit, stored with two decimals (NUMERIC(14,2))."""
    return Decimal(value).quantize(quantum(currency), rounding=ROUND_HALF_UP).quantize(CENT)


def split(total: Decimal, lines: list[Line], currency: str) -> list[Decimal]:
    """The amount of each line of ``total``: the deposit and the instalments by their percent (the deposit at least
    one unit), the last line the rest. The amounts always add up to ``total``; a line may come out 0 or below when the
    percents leave nothing for it (:func:`problems` reports ``schedule_amount_zero``)."""
    total = Decimal(total).quantize(CENT)
    if not lines:
        return []
    if total <= 0:
        return [Decimal("0.00")] * len(lines)
    unit = quantum(currency)
    out: list[Decimal] = []
    for n, line in enumerate(lines[:-1]):
        amount = round_money(total * (line.percent or Decimal(0)) / HUNDRED, currency)
        if n == 0 and line.kind == "deposit":
            amount = min(total, max(unit.quantize(CENT), amount))
        out.append(amount)
    out.append(total - sum(out, Decimal("0.00")))
    return out


def deposit_pct(lines: list[Line]) -> Decimal | None:
    return next((line.percent for line in lines if line.kind == "deposit"), None)


def balance_due_on(lines: list[Line]) -> dt.date | None:
    """The last line's date when there is a balance or an instalment after the deposit (crm.proposal.balance_due_on
    mirrors it: proposal_amounts_chk wants a date whenever something is left after the deposit)."""
    return lines[-1].due_on if len(lines) > 1 else None


# ── Shapes ───────────────────────────────────────────────────────────────────────────────────────


def _err(message: str, field: str) -> Invalid:
    return Invalid("invalid_schedule", message, field=field)


def _percent(value: Any, field: str) -> Decimal:
    try:
        pct = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _err("a percent from 0.01 to 100", field) from None
    if not pct.is_finite() or pct <= 0 or pct > HUNDRED or pct != pct.quantize(CENT):
        raise _err("a percent from 0.01 to 100, two decimals at most", field)
    return pct


def _days(value: Any, field: str, low: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= MAX_DAYS_BEFORE:
        raise _err(f"a whole number of days from {low} to {MAX_DAYS_BEFORE}", field)
    return value


def _kinds(kinds: list[str]) -> None:
    """One deposit first, instalments, at most one balance last."""
    if not kinds or len(kinds) > MAX_LINES:
        raise _err(f"1 to {MAX_LINES} lines", "schedule")
    if kinds[0] != "deposit" or kinds.count("deposit") != 1:
        raise _err("the first line is the deposit, and only the first", "schedule.0.kind")
    if kinds.count("balance") > 1 or ("balance" in kinds and kinds[-1] != "balance"):
        raise _err("at most one balance, the last line", "schedule.kind")
    if kinds.count("instalment") > MAX_INSTALMENTS:
        raise _err(f"at most {MAX_INSTALMENTS} instalments", "schedule")
    for n, kind in enumerate(kinds):
        if kind not in KINDS:
            raise _err(", ".join(KINDS), f"schedule.{n}.kind")


def _percents(lines: list[Line]) -> None:
    shares = sum((line.percent or Decimal(0) for line in lines if line.kind != "balance"), Decimal(0))
    if lines[-1].kind == "balance" and shares >= HUNDRED:
        raise _err("the deposit and instalments leave nothing for the balance (under 100 % in all)", "schedule")
    if lines[-1].kind != "balance" and shares != HUNDRED:
        raise _err("without a balance line the percents add up to 100", "schedule")


def _numbered(kinds_percents: list[tuple[str, Decimal | None, dt.date | None, int | None]]) -> list[Line]:
    out, seq = [], 0
    for kind, pct, due_on, days in kinds_percents:
        if kind == "instalment":
            seq += 1
        out.append(Line(kind, seq if kind == "instalment" else 1, pct, due_on, days))
    return out


def check_template(raw: Any) -> list[dict[str, Any]]:
    """A template from Settings > Payments, normalised (422 ``invalid_schedule`` with the field path)."""
    if not isinstance(raw, list):
        raise _err("a list of lines", "schedule")
    kinds = [item.get("kind") if isinstance(item, dict) else None for item in raw]
    _kinds(kinds)  # type: ignore[arg-type]
    out: list[dict[str, Any]] = []
    parts: list[tuple[str, Decimal | None, dt.date | None, int | None]] = []
    last_days: int | None = None
    for n, item in enumerate(raw):
        where = f"schedule.{n}"
        if set(item) - {"kind", "percent", "due"}:
            raise _err("kind, percent, due", where)
        kind = item["kind"]
        due = item.get("due") or {}
        if not isinstance(due, dict) or set(due) - {"anchor", "days"}:
            raise _err("{anchor, days}", f"{where}.due")
        pct = None if kind == "balance" else _percent(item.get("percent"), f"{where}.percent")
        if kind == "balance" and item.get("percent") is not None:
            raise _err("the balance takes the rest: no percent", f"{where}.percent")
        if kind == "deposit":
            if due.get("anchor", "accept") != "accept" or "days" in due:
                raise _err("the deposit is due at accept", f"{where}.due")
            days = None
            out.append({"kind": kind, "percent": str(pct), "due": {"anchor": "accept"}})
        else:
            if due.get("anchor") != "before_start":
                raise _err("before_start", f"{where}.due.anchor")
            days = _days(due.get("days"), f"{where}.due.days", 0 if kind == "balance" else 1)
            if last_days is not None and days >= last_days:
                raise _err("each line falls due later than the one before (fewer days before departure)",
                           f"{where}.due.days")  # fmt: skip
            last_days = days
            line: dict[str, Any] = {"kind": kind, "due": {"anchor": "before_start", "days": days}}
            if pct is not None:
                line["percent"] = str(pct)
            out.append(line)
        parts.append((kind, pct, None, days))
    _percents(_numbered(parts))
    return out


def from_template(template: Any, starts_on: dt.date | None) -> list[Line]:
    """A new version's plan from the agency template (None = the AA default), dated from day 1 when it is known."""
    lines = check_template(list(template) if template else [dict(t) for t in DEFAULT_TEMPLATE])
    parts: list[tuple[str, Decimal | None, dt.date | None, int | None]] = []
    for line in lines:
        days = line["due"].get("days")
        due_on = starts_on - dt.timedelta(days=days) if (days is not None and starts_on is not None) else None
        parts.append((line["kind"], Decimal(line["percent"]) if "percent" in line else None, due_on, days))
    return _numbered(parts)


def _date(value: Any, field: str) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise _err("a date (YYYY-MM-DD)", field) from None


def check_plan(raw: Any, starts_on: dt.date | None) -> list[Line]:
    """The lines the agent set in the builder (``[{kind, percent, due_on, days_before}]``): the shape (422
    ``invalid_schedule``). A line with ``days_before`` and no date is dated from day 1; dates themselves are checked
    by :func:`problems` (a blocking check of Publish, not a refusal of the edit)."""
    if not isinstance(raw, list):
        raise _err("a list of lines", "schedule")
    kinds = [item.get("kind") if isinstance(item, dict) else None for item in raw]
    _kinds(kinds)  # type: ignore[arg-type]
    parts: list[tuple[str, Decimal | None, dt.date | None, int | None]] = []
    for n, item in enumerate(raw):
        where = f"schedule.{n}"
        if set(item) - {"kind", "seq", "percent", "due_on", "days_before"}:
            raise _err("kind, percent, due_on, days_before", where)
        kind = item["kind"]
        if kind == "balance":
            if item.get("percent") is not None:
                raise _err("the balance takes the rest: no percent", f"{where}.percent")
            pct = None
        else:
            pct = _percent(item.get("percent"), f"{where}.percent")
        if kind == "deposit":
            if item.get("due_on") is not None or item.get("days_before") is not None:
                raise _err("the deposit is due at accept", f"{where}.due_on")
            parts.append((kind, pct, None, None))
            continue
        days = item.get("days_before")
        days = None if days is None else _days(days, f"{where}.days_before", 0)
        due_on = _date(item.get("due_on"), f"{where}.due_on")
        if days is not None and starts_on is not None:
            due_on = starts_on - dt.timedelta(days=days)
        parts.append((kind, pct, due_on, days))
    lines = _numbered(parts)
    _percents(lines)
    return lines


def redate(lines: list[Line], starts_on: dt.date | None) -> list[Line]:
    """Day 1 moved: the lines that still follow the template move with it."""
    return [
        Line(line.kind, line.seq, line.percent,
             starts_on - dt.timedelta(days=line.days_before) if (line.days_before is not None and starts_on) else
             line.due_on, line.days_before)
        for line in lines
    ]  # fmt: skip


def problems(lines: list[Line], total: Decimal, currency: str, starts_on: dt.date | None) -> list[str]:
    """What blocks Publish: a line after the deposit without a date, dates not going forward, a date after day 1, a
    line the percents leave without money."""
    out: set[str] = set()
    dates = [line.due_on for line in lines[1:]]
    if any(d is None for d in dates):
        out.add("schedule_dates_required")
    known = [d for d in dates if d is not None]
    if any(b <= a for a, b in zip(known, known[1:], strict=False)):
        out.add("schedule_dates_order")
    if starts_on is not None and any(d > starts_on for d in known):
        out.add("schedule_after_start")
    if Decimal(total) > 0 and any(a <= 0 for a in split(Decimal(total), lines, currency)):
        out.add("schedule_amount_zero")
    return sorted(out)


def accept_rows(lines: list[Line], total: Decimal, currency: str, today: dt.date) -> list[dict[str, Any]]:
    """The ``crm.payment_schedule`` rows of an accept: one per line with an amount, a date already past (or the
    deposit's) is due today."""
    rows = []
    for line, amount in zip(lines, split(total, lines, currency), strict=True):
        if amount <= 0:
            continue
        due = today if line.due_on is None else max(line.due_on, today)
        rows.append({"kind": line.kind, "seq": line.seq, "percent": line.percent, "amount": amount, "due_on": due})
    return rows


# ── JSON (crm.proposal.schedule_plan) and views ──────────────────────────────────────────────────


def to_json(lines: list[Line]) -> list[dict[str, Any]]:
    return [
        {
            "kind": line.kind,
            "seq": line.seq,
            "percent": None if line.percent is None else str(line.percent),
            "due_on": line.due_on.isoformat() if line.due_on else None,
            "days_before": line.days_before,
        }
        for line in lines
    ]


def from_json(raw: Any) -> list[Line] | None:
    """A stored plan (trusted: written by :func:`to_json`); None = no plan (the crm-019 pair)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = json.loads(raw)
    return [
        Line(
            item["kind"],
            int(item.get("seq") or 1),
            None if item.get("percent") is None else Decimal(str(item["percent"])),
            dt.date.fromisoformat(item["due_on"]) if item.get("due_on") else None,
            item.get("days_before"),
        )
        for item in raw
    ]


def view(lines: list[Line], total: Decimal, currency: str) -> list[dict[str, Any]]:
    """The lines with their amounts for a page (the builder, the client page): strings, no internals."""
    return [
        {
            "kind": line.kind,
            "seq": line.seq,
            "percent": None if line.percent is None else str(line.percent),
            "due_on": line.due_on.isoformat() if line.due_on else None,
            "days_before": line.days_before,
            "amount": str(amount),
        }
        for line, amount in zip(lines, split(total, lines, currency), strict=True)
    ]


def hash_record(lines: list[Line]) -> list[list[Any]]:
    """What the price hash covers of a plan (the client sees each line's share and date)."""
    return [
        [line.kind, line.seq, None if line.percent is None else str(line.percent),
         line.due_on.isoformat() if line.due_on else None]
        for line in lines
    ]  # fmt: skip
