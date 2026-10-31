"""One report section as CSV (AA-966, design v3 A-14 "[Export CSV]").

- UTF-8 with a BOM (spreadsheets open it as UTF-8), CRLF lines, a header row of plain English column names.
- **Spreadsheet formula guard**: a text cell that starts with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return
  gets a leading apostrophe (as the access log and the portability CSV), so an agency-written title such as
  ``=HYPERLINK(...)`` stays text. Numbers are written as numbers (a negative margin keeps its minus sign).
- **Money**: the margin serializer (:mod:`app.margin`) filters JSON only, so this module checks ``view_margin`` itself:
  a money section for the assistant is 403 ``role_forbidden`` (and a ``denied`` audit event); the team and outstanding
  sections carry no margin column.
- Every download writes one ``report_export`` audit event (reason = the section), which is also the module 16
  success metric "the agency uses its reports".
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any

from app.permissions import Action, require
from app.reports import service
from app.tenancy import TenantContext

SECTIONS = (
    "funnel",
    "lead_sources",
    "outcomes",
    "open_trips",
    "money_by_month",
    "top_products",
    "outstanding",
    "team",
    "success",
)
MONEY_SECTIONS = frozenset({"money_by_month"})
_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


def guard(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | Decimal):
        return str(value)
    text = str(value)
    return "'" + text if text[:1] in _DANGEROUS else text


def to_csv(header: list[str], rows: list[list[Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([guard(v) for v in row])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


async def section_rows(ctx: TenantContext, sc: service.Scope, section: str) -> tuple[list[str], list[list[Any]]]:
    """The header and rows of one section, from the same service functions the page uses (so they reconcile)."""
    if section in {"funnel", "lead_sources", "team"}:
        rows = await service.cohort(ctx, sc, sc.window)
    if section == "funnel":
        f = service.funnel(rows)
        out = [[s["stage"], s["count"]] for s in f["stages"]]
        out += [["lost", f["lost"]], ["cancelled", f["cancelled"]]]
        return ["stage", "trips"], out
    if section == "lead_sources":
        return ["lead_source", "trips", "won", "win_rate_pct"], [
            [s["source"] or "", s["trips"], s["won"], s["win_rate_pct"]] for s in service.lead_sources(rows)
        ]
    if section == "outcomes":
        o = await service.outcomes(ctx, sc)
        return ["outcome", "reason", "trips"], [
            [kind, item["reason"], item["count"]] for kind in ("lost", "cancelled") for item in o[kind]
        ]
    if section == "open_trips":
        return ["stage", "trips", "stalled_14_days", "stalled_not_waiting", "median_age_days"], [
            [s["stage"], s["count"], s["stalled"], s["stalled_not_waiting"], s["median_age_days"]]
            for s in await service.open_trips(ctx, sc)
        ]
    if section == "money_by_month":
        m = service.money_report(await service.money_lines(ctx, sc, sc.window), sc)
        return ["month", "currency", "lines", "sell", "cost", "margin", "margin_pct", "commission_expected"], [
            [
                r["month"],
                m["currency"],
                r["lines"],
                _dec(r["sell_base"]),
                _dec(r["cost_base"]),
                _dec(r["margin_base"]),
                r["margin_pct"],
                _dec(r["commission_expected_base"]),
            ]
            for r in m["by_month"]
        ]
    if section == "top_products":
        if sc.money:
            m = service.money_report(await service.money_lines(ctx, sc, sc.window), sc)
            return ["supplier", "product", "lines", "trips", "currency", "sell"], [
                [p["supplier_kind"], p["title"], p["lines"], p["trips"], m["currency"], _dec(p["sell_base"])]
                for p in m["top_products"]
            ]
        top = await service.top_products_by_lines(ctx, sc)
        return ["supplier", "product", "lines", "trips"], [
            [p["supplier_kind"], p["title"], p["lines"], p["trips"]] for p in top
        ]
    if section == "outstanding":
        p = await service.payments(ctx, sc)
        header = ["what", "currency", "kind", "count", "total", "paid", "outstanding", "overdue_count", "overdue"]
        out = [
            ["open_invoices", i["currency"], "", i["count"], _dec(i["total"]), _dec(i["paid"]),
             _dec(i["outstanding"]), i["overdue_count"], _dec(i["overdue"])]
            for i in p["open_invoices"]
        ]  # fmt: skip
        out += [
            ["schedule_due", s["currency"], s["kind"], s["count"], _dec(s["total"]), None, _dec(s["total"]),
             s["overdue_count"], _dec(s["overdue"])]
            for s in p["schedule_due"]
        ]  # fmt: skip
        return header, out
    if section == "team":
        t = await service.team(ctx, sc, rows, await service.first_replies(ctx, sc))
        return [
            "member_id", "role", "status", "open_trips", "new_trips", "won", "overdue_tasks", "tasks_done",
            "messages_sent", "first_reply_median_min",
        ], [
            [m["member_id"], m["role"], m["status"], m["open_trips"], m["new_trips"], m["won"], m["overdue_tasks"],
             m["tasks_done"], m["messages_sent"], m["first_reply_median_min"]]
            for m in t["members"]
        ]  # fmt: skip
    # success (the route accepts SECTIONS only)
    rows = await service.cohort(ctx, sc, sc.window)
    lines = await service.money_lines(ctx, sc, sc.window)
    metrics = await service.success_metrics(
        ctx, sc, rows, await service.picks(ctx, sc), lines, await service.first_replies(ctx, sc)
    )
    return ["module", "metric", "unit", "value", "target", "status"], [
        [m["module"], m["key"], m["unit"], m["value"], m["target"], m["status"]] for m in metrics
    ]


async def export(ctx: TenantContext, period: str, owner: str | None, section: str) -> tuple[bytes, int]:
    """The section as CSV bytes and its row count; audited as ``report_export``."""
    if section in MONEY_SECTIONS:
        require(ctx, Action.VIEW_MARGIN, object_kind="report", object_id=section)
    sc = await service.scope(ctx, period, owner)
    header, rows = await section_rows(ctx, sc, section)
    ctx.audit("report_export", "allowed", "report", section, reason=f"period_{period}")
    return to_csv(header, rows), len(rows)
