"""The report window (AA-966, design v3 A-14 "Period [Last 12 months]", R28).

Dates are the agency's **local** dates (``crm.tenant_settings.timezone``; card E "day boundaries use the agency time
zone"): a window is ``[start, end]`` inclusive, and every query turns the two bounds into instants with
``(<date>::timestamp AT TIME ZONE <tz>)`` so a trip created at 23:30 local on the last day is inside.

Comparison (R28): the same window one year earlier when the agency has data from before that earlier window began
(``previous_year``); otherwise the last three calendar months up to the window end (``trend_3m``: "new agency (< 12
months): trend shows the last 3 months instead of YoY"); ``none`` when the agency has no trip yet.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from app.errors import Invalid

PERIODS = ("30d", "90d", "12m", "ytd")
DEFAULT_PERIOD = "12m"
Compare = Literal["previous_year", "trend_3m", "none"]


@dataclass(frozen=True)
class Window:
    start: dt.date
    end: dt.date  # inclusive

    @property
    def end_excl(self) -> dt.date:
        return self.end + dt.timedelta(days=1)

    def shift_years(self, years: int) -> Window:
        return Window(_shift_year(self.start, years), _shift_year(self.end, years))

    def as_dict(self) -> dict[str, str]:
        return {"from": self.start.isoformat(), "to": self.end.isoformat()}


def _shift_year(day: dt.date, years: int) -> dt.date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February in a year without one
        return day.replace(year=day.year + years, day=28)


def _month_start(day: dt.date, back: int = 0) -> dt.date:
    index = day.year * 12 + (day.month - 1) - back
    return dt.date(index // 12, index % 12 + 1, 1)


def window(period: str, today: dt.date) -> Window:
    """The window of a period code ending today (local)."""
    if period == "30d":
        return Window(today - dt.timedelta(days=29), today)
    if period == "90d":
        return Window(today - dt.timedelta(days=89), today)
    if period == "12m":
        return Window(_month_start(today, 11), today)
    if period == "ytd":
        return Window(dt.date(today.year, 1, 1), today)
    raise Invalid("invalid_period", f"period must be one of {', '.join(PERIODS)}")


def compare_mode(first_day: dt.date | None, win: Window) -> Compare:
    """``previous_year`` when the agency's first trip is on or before the start of the same window a year earlier."""
    if first_day is None:
        return "none"
    return "previous_year" if first_day <= win.shift_years(-1).start else "trend_3m"


def trend_months(win: Window) -> list[Window]:
    """The three calendar months up to the window end, oldest first; the last one ends on the window end."""
    out = []
    for back in (2, 1, 0):
        start = _month_start(win.end, back)
        end = min(_month_start(win.end, back - 1) - dt.timedelta(days=1), win.end)
        out.append(Window(start, end))
    return out


def months(win: Window) -> list[dt.date]:
    """The first day of every calendar month the window touches, oldest first (the monthly series)."""
    out, cursor = [], _month_start(win.start)
    while cursor <= win.end:
        out.append(cursor)
        cursor = _month_start(cursor, -1)
    return out
