"""The agency's reports (AA-966, card CRM-34; spec module 16, design v3 A-14 Reports and A-01 Home tiles, R23, R28).

Every function takes an open ``tenant_tx()`` context (RLS: the caller's agency only) and only reads. What counts:

- **Inquiries / win rate** (report catalog CRM-01): the cohort is the trips *created* in the window; a trip is won
  when it reached Booked (its current stage, or a stage-history row a later Undo did not reverse), so a trip booked
  and then cancelled stays won. The funnel counts, per forward stage, the cohort trips that reached it.
- **Lost / cancelled reasons** (R23): trips that ended lost or cancelled in the window, by their reason code.
- **Money** (owner and agent only; ``view_margin``): confirmed booking lines by confirmation date (R18: only confirmed
  lines count), the head money row per kind in the agency's base currency (R17: frozen at confirmation, so a later
  FX rate never moves a past month). Margin = sell - cost, over the lines that have both (as the ledger's
  ``margin_base``). The assistant gets no money section at all (crm-014 would return no rows anyway); its AA share
  is counted in lines instead of sell.
- **Outstanding payments**: open invoices (paid so far = payments - refunds on them) and payment-schedule rows still
  due, each in its own currency; collected = payments received in the window minus refunds.
- **Comparison** (R28): the tiles carry the same window a year earlier, or the last three months for an agency with
  less than a year of history (:mod:`app.reports.period`). Day boundaries are the agency's local days.
- **Success metrics**: one source query per measurable spec §6 success line (``SUCCESS``); a module that is not built
  yet says so instead of showing a number.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any

from app.errors import Invalid
from app.margin import sees_margin
from app.reports import period as periods
from app.reports import queries as q
from app.tenancy import TenantContext

DEFAULT_TZ = "Asia/Ho_Chi_Minh"  # as app.duty.service: an agency without settings is a Dev seed in Viet Nam
TOP_PRODUCTS = 10
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Scope:
    today: dt.date
    tz: str
    base_currency: str
    first_day: dt.date | None
    window: periods.Window
    compare: periods.Compare
    owner: uuid.UUID | None
    money: bool  # the caller may read money (owner / agent)


async def scope(ctx: TenantContext, period: str, owner: str | None) -> Scope:
    """The report scope: the agency's today and time zone, the window and the owner filter (``me`` or a member id)."""
    row = await ctx.conn.fetchrow(q.SETTINGS_SQL, DEFAULT_TZ)
    win = periods.window(period, row["today"])
    owner_id: uuid.UUID | None = None
    if owner == "me":
        owner_id = ctx.member_id
    elif owner:
        try:
            owner_id = uuid.UUID(owner)
        except ValueError as exc:
            raise Invalid("invalid_owner", "owner must be me or a member id") from exc
    return Scope(
        today=row["today"],
        tz=row["tz"],
        base_currency=row["base_currency"],
        first_day=row["first_day"],
        window=win,
        compare=periods.compare_mode(row["first_day"], win),
        owner=owner_id,
        money=sees_margin(ctx.member_role),
    )


def _args(sc: Scope, win: periods.Window) -> tuple[Any, ...]:
    return (win.start, win.end_excl, sc.tz, sc.owner)


def pct(part: float | int | Decimal, whole: float | int | Decimal) -> float | None:
    if not whole:
        return None
    return round(float(part) * 100.0 / float(whole), 1)


def _money(value: Decimal | int | None) -> str:
    return str(Decimal(value or 0).quantize(CENT))


def _num(value: Any, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


# ── Cohort: inquiries, funnel, lead sources, win rate ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CohortRow:
    stage: str
    owner_member_id: uuid.UUID | None
    lead_source: str
    has_budget: bool
    max_rank: int
    n: int

    @property
    def won(self) -> bool:
        return self.max_rank >= q.BOOKED_RANK


async def cohort(ctx: TenantContext, sc: Scope, win: periods.Window) -> list[CohortRow]:
    rows = await ctx.conn.fetch(q.COHORT_SQL, *_args(sc, win), list(q.FORWARD_STAGES))
    return [
        CohortRow(
            stage=r["stage"],
            owner_member_id=r["owner_member_id"],
            lead_source=r["lead_source"] or "",
            has_budget=bool(r["has_budget"]),
            max_rank=int(r["max_rank"] or 0),
            n=int(r["n"]),
        )
        for r in rows
    ]


def funnel(rows: list[CohortRow]) -> dict[str, Any]:
    created = sum(r.n for r in rows)
    won = sum(r.n for r in rows if r.won)
    stages = [
        {"stage": stage, "count": sum(r.n for r in rows if r.max_rank >= rank)}
        for rank, stage in enumerate(q.FORWARD_STAGES, start=1)
    ]
    return {
        "created": created,
        "won": won,
        "win_rate_pct": pct(won, created),
        "stages": stages,
        "lost": sum(r.n for r in rows if r.stage == "lost"),
        "cancelled": sum(r.n for r in rows if r.stage == "cancelled"),
    }


def lead_sources(rows: list[CohortRow]) -> list[dict[str, Any]]:
    trips: dict[str, int] = defaultdict(int)
    won: dict[str, int] = defaultdict(int)
    for r in rows:
        trips[r.lead_source] += r.n
        if r.won:
            won[r.lead_source] += r.n
    return [
        {"source": source or None, "trips": n, "won": won[source], "win_rate_pct": pct(won[source], n)}
        for source, n in sorted(trips.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


# ── Money (owner / agent) ───────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LineMoney:
    trip_id: uuid.UUID
    supplier_kind: str
    title: str
    month: dt.date
    party_type: str
    sell: Decimal | None
    cost: Decimal | None
    commission: Decimal | None


async def money_lines(ctx: TenantContext, sc: Scope, win: periods.Window) -> list[LineMoney]:
    if not sc.money:
        return []
    rows = await ctx.conn.fetch(q.MONEY_SQL, *_args(sc, win), sc.base_currency)
    return [
        LineMoney(
            trip_id=r["trip_id"],
            supplier_kind=r["supplier_kind"],
            title=r["title"],
            month=r["month"],
            party_type=r["party_type"] or "",
            sell=r["sell"],
            cost=r["cost"],
            commission=r["commission"],
        )
        for r in rows
    ]


@dataclass
class _Totals:
    sell: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)  # of the lines with both amounts
    sell_with_cost: Decimal = Decimal(0)
    commission: Decimal = Decimal(0)
    lines: int = 0
    missing: int = 0

    def add(self, line: LineMoney) -> None:
        self.lines += 1
        self.sell += line.sell or 0
        self.commission += line.commission or 0
        if line.sell is None or line.cost is None:
            self.missing += 1
        else:
            self.cost += line.cost
            self.sell_with_cost += line.sell

    @property
    def margin(self) -> Decimal:
        return self.sell_with_cost - self.cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "sell_base": _money(self.sell),
            "cost_base": _money(self.cost),
            "margin_base": _money(self.margin),
            "margin_pct": pct(self.margin, self.sell_with_cost),
            "commission_expected_base": _money(self.commission),
            "lines": self.lines,
            "lines_missing_amounts": self.missing,
        }


def money_report(lines: list[LineMoney], sc: Scope) -> dict[str, Any]:
    total = _Totals()
    by_month: dict[dt.date, _Totals] = {m: _Totals() for m in periods.months(sc.window)}
    by_supplier: dict[str, _Totals] = defaultdict(_Totals)
    by_party: dict[str, _Totals] = defaultdict(_Totals)
    products: dict[tuple[str, str], tuple[_Totals, set[uuid.UUID]]] = {}
    for line in lines:
        total.add(line)
        by_month.setdefault(line.month, _Totals()).add(line)
        by_supplier[line.supplier_kind].add(line)
        by_party[line.party_type].add(line)
        tot, trips = products.setdefault((line.supplier_kind, line.title), (_Totals(), set()))
        tot.add(line)
        trips.add(line.trip_id)
    top = sorted(products.items(), key=lambda kv: (-kv[1][0].sell, -kv[1][0].lines, kv[0][1]))[:TOP_PRODUCTS]
    return {
        "currency": sc.base_currency,
        **total.as_dict(),
        "aa_share_pct": pct(by_supplier["aa"].sell if "aa" in by_supplier else 0, total.sell),
        "by_month": [{"month": m.isoformat(), **t.as_dict()} for m, t in sorted(by_month.items())],
        "by_supplier": [{"supplier_kind": k, **t.as_dict()} for k, t in sorted(by_supplier.items())],
        "by_party_type": [{"party_type": k or None, **t.as_dict()} for k, t in sorted(by_party.items())],
        "top_products": [
            {"supplier_kind": kind, "title": title, "trips": len(trips), **tot.as_dict()}
            for (kind, title), (tot, trips) in top
        ],
    }


async def top_products_by_lines(ctx: TenantContext, sc: Scope) -> list[dict[str, Any]]:
    """Top products for a role without money: confirmed lines and trips only."""
    rows = await ctx.conn.fetch(q.TOP_PRODUCTS_SQL, *_args(sc, sc.window), TOP_PRODUCTS)
    return [
        {"supplier_kind": r["supplier_kind"], "title": r["title"], "lines": r["lines"], "trips": r["trips"]}
        for r in rows
    ]


async def aa_share_by_lines(ctx: TenantContext, sc: Scope, win: periods.Window) -> float | None:
    counts = {r["supplier_kind"]: int(r["n"]) for r in await ctx.conn.fetch(q.LINE_COUNT_SQL, *_args(sc, win))}
    return pct(counts.get("aa", 0), sum(counts.values()))


# ── Tiles (A-01 / A-14, R28) ────────────────────────────────────────────────────────────────────────────────
async def _tile_values(ctx: TenantContext, sc: Scope, win: periods.Window) -> dict[str, Any]:
    rows = await cohort(ctx, sc, win)
    f = funnel(rows)
    values: dict[str, Any] = {"inquiries": f["created"], "win_rate": f["win_rate_pct"]}
    if sc.money:
        total = _Totals()
        aa = Decimal(0)
        for line in await money_lines(ctx, sc, win):
            total.add(line)
            if line.supplier_kind == "aa":
                aa += line.sell or 0
        values["revenue"] = _money(total.sell)
        values["margin"] = _money(total.margin)
        values["aa_share"] = pct(aa, total.sell)
    else:
        values["aa_share"] = await aa_share_by_lines(ctx, sc, win)
    return values


TILE_UNITS = {"inquiries": "count", "win_rate": "pct", "revenue": "money", "margin": "money", "aa_share": "pct"}
TILE_LINKS = {"inquiries": "/pipeline", "win_rate": "/pipeline", "revenue": "/bookings", "margin": "/bookings",
              "aa_share": "/bookings"}  # fmt: skip


def _delta(key: str, value: Any, previous: Any) -> float | None:
    if value is None or previous is None:
        return None
    if TILE_UNITS[key] == "pct":
        return round(float(value) - float(previous), 1)  # points
    return pct(Decimal(str(value)) - Decimal(str(previous)), Decimal(str(previous)))


async def tiles(ctx: TenantContext, sc: Scope) -> dict[str, Any]:
    current = await _tile_values(ctx, sc, sc.window)
    previous: dict[str, Any] | None = None
    trend: list[tuple[periods.Window, dict[str, Any]]] = []
    prev_window: periods.Window | None = None
    if sc.compare == "previous_year":
        prev_window = sc.window.shift_years(-1)
        previous = await _tile_values(ctx, sc, prev_window)
    elif sc.compare == "trend_3m":
        for month in periods.trend_months(sc.window):
            trend.append((month, await _tile_values(ctx, sc, month)))
    items = []
    for key, value in current.items():
        item: dict[str, Any] = {
            "key": key,
            "unit": TILE_UNITS[key],
            "value": value,
            "href": TILE_LINKS[key],
            "basis": "sell" if key == "aa_share" and sc.money else ("lines" if key == "aa_share" else None),
            "previous": None,
            "delta": None,
            "trend": [],
        }
        if previous is not None:
            item["previous"] = previous[key]
            item["delta"] = _delta(key, value, previous[key])
        if trend:
            item["trend"] = [{**w.as_dict(), "value": vals[key]} for w, vals in trend]
        items.append(item)
    return {
        "period": period_info(sc, prev_window),
        "tiles": items,
    }


def period_info(sc: Scope, prev_window: periods.Window | None) -> dict[str, Any]:
    return {
        **sc.window.as_dict(),
        "today": sc.today.isoformat(),
        "timezone": sc.tz,
        "base_currency": sc.base_currency,
        "compare": sc.compare,
        "previous": prev_window.as_dict() if prev_window else None,
        "owner_member_id": str(sc.owner) if sc.owner else None,
    }


# ── The other sections ──────────────────────────────────────────────────────────────────────────────────────
async def outcomes(ctx: TenantContext, sc: Scope) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"lost": [], "cancelled": []}
    for r in await ctx.conn.fetch(q.OUTCOME_SQL, *_args(sc, sc.window)):
        out[r["stage"]].append({"reason": r["reason"], "count": int(r["n"])})
    for items in out.values():
        items.sort(key=lambda i: (-i["count"], i["reason"] or ""))
    return out


async def cycle_time(ctx: TenantContext, sc: Scope) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(q.CYCLE_SQL, *_args(sc, sc.window))
    return {"trips": int(row["n"]) if row else 0, "median_days": _num(row["median_days"]) if row else None}


async def open_trips(ctx: TenantContext, sc: Scope) -> list[dict[str, Any]]:
    found = {r["stage"]: r for r in await ctx.conn.fetch(q.OPEN_SQL, list(q.OPEN_STAGES), sc.owner)}
    out = []
    for stage in q.OPEN_STAGES:
        r = found.get(stage)
        out.append(
            {
                "stage": stage,
                "count": int(r["n"]) if r else 0,
                "stalled": int(r["stalled"]) if r else 0,
                "stalled_not_waiting": int(r["stalled_not_waiting"]) if r else 0,
                "median_age_days": _num(r["median_age_days"]) if r else None,
            }
        )
    return out


async def picks(ctx: TenantContext, sc: Scope) -> dict[str, Any]:
    rows = await ctx.conn.fetch(q.PICKS_SQL, *_args(sc, sc.window))
    total = sum(int(r["n"]) for r in rows)
    kept = sum(int(r["n"]) for r in rows if r["kept_default"])
    upgraded = sum(int(r["n"]) for r in rows if r["upgraded"])
    changed: dict[str, int] = defaultdict(int)
    for r in rows:
        if not r["kept_default"]:
            changed[r["block_type"]] += int(r["n"])
    most = sorted(changed.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "picks": total,
        "kept_default": kept,
        "upgraded": upgraded,
        "kept_default_pct": pct(kept, total),
        "upgrade_rate_pct": pct(upgraded, total),
        "most_changed_type": most[0][0] if most else None,
    }


async def payments(ctx: TenantContext, sc: Scope) -> dict[str, Any]:
    invoices = []
    for r in await ctx.conn.fetch(q.OPEN_INVOICES_SQL, sc.today, sc.owner):
        # outstanding = the sum of each invoice's balance (clamped at 0 per invoice in SQL), not total - paid.
        invoices.append(
            {
                "currency": r["currency"],
                "count": int(r["n"]),
                "total": _money(r["total"]),
                "paid": _money(r["paid"]),
                "outstanding": _money(r["outstanding"]),
                "overdue_count": int(r["overdue_n"]),
                "overdue": _money(r["overdue"]),
            }
        )
    schedule = [
        {
            "currency": r["currency"],
            "kind": r["kind"],
            "count": int(r["n"]),
            "total": _money(r["total"]),
            "overdue_count": int(r["overdue_n"]),
            "overdue": _money(r["overdue"]),
        }
        for r in await ctx.conn.fetch(q.SCHEDULE_DUE_SQL, sc.today, sc.owner)
    ]
    collected = [
        {
            "currency": r["currency"],
            "count": int(r["n"]),
            "received": _money(r["received"]),
            "refunded": _money(r["refunded"]),
            "kept": _money(Decimal(r["received"] or 0) - Decimal(r["refunded"] or 0)),  # not "net": a margin key name
        }
        for r in await ctx.conn.fetch(q.COLLECTED_SQL, sc.window.start, sc.window.end_excl, sc.owner)
    ]
    for items in (invoices, schedule, collected):
        items.sort(key=lambda i: (i["currency"], i.get("kind") or ""))
    return {"open_invoices": invoices, "schedule_due": schedule, "collected": collected}


async def first_replies(ctx: TenantContext, sc: Scope) -> list[tuple[uuid.UUID | None, float]]:
    """(owner, minutes) of every trip created in the window that has a first reply."""
    return [
        (r["owner_member_id"], float(r["minutes"]))
        for r in await ctx.conn.fetch(q.FIRST_REPLY_SQL, *_args(sc, sc.window))
        if r["minutes"] is not None
    ]


async def team(
    ctx: TenantContext, sc: Scope, rows: list[CohortRow], reply_rows: list[tuple[uuid.UUID | None, float]]
) -> dict[str, Any]:
    replies: dict[uuid.UUID | None, list[float]] = defaultdict(list)
    for owner_id, minutes in reply_rows:
        replies[owner_id].append(minutes)
    created: dict[uuid.UUID | None, int] = defaultdict(int)
    won: dict[uuid.UUID | None, int] = defaultdict(int)
    for c in rows:
        created[c.owner_member_id] += c.n
        if c.won:
            won[c.owner_member_id] += c.n
    members = []
    for r in await ctx.conn.fetch(q.TEAM_SQL, *_args(sc, sc.window), list(q.OPEN_STAGES)):
        mid = r["member_id"]
        members.append(
            {
                "member_id": str(mid),
                "role": r["role"],
                "status": r["status"],
                "is_me": mid == ctx.member_id,
                "open_trips": int(r["open_trips"]),
                "new_trips": created[mid],
                "won": won[mid],
                "overdue_tasks": int(r["overdue_tasks"]),
                "tasks_done": int(r["tasks_done"]),
                "messages_sent": int(r["messages_sent"]),
                "first_reply_median_min": _num(median(replies[mid])) if replies[mid] else None,
            }
        )
    unassigned = await ctx.conn.fetchval(q.UNASSIGNED_SQL, list(q.OPEN_STAGES)) if sc.owner is None else 0
    return {
        "members": members,
        "unassigned_open_trips": int(unassigned or 0),
        "unanswered_24h": int(await ctx.conn.fetchval(q.UNANSWERED_SQL, sc.owner) or 0),
    }


# ── Spec §6 success metrics ─────────────────────────────────────────────────────────────────────────────────
# key -> (module, unit, target, comparator). Modules not built yet are listed with status "not_built".
SUCCESS: dict[str, tuple[str, str, float | None, str | None]] = {
    "budget_band_share": ("1", "pct", 80, "gte"),
    "first_reply_median": ("1", "minutes", 120, "lte"),
    "quiet_leads": ("2", "count", 0, "lte"),
    "upgrade_rate": ("3", "pct", 20, "gte"),
    "messages_per_booked_trip": ("3", "count", 3, "lte"),
    "text_proposal_minutes": ("4", "minutes", 2, "lte"),
    "messages_sent": ("5", "count", None, None),
    "margin_known_share": ("6", "pct", 100, "gte"),
    "deposit_48h_share": ("7", "pct", 70, "gte"),
    "ops_release_errors": ("8", "count", 0, "lte"),
    "chat_messages_sent": ("9", "count", None, None),
    "overdue_before_departure": ("10", "count", 0, "lte"),
    "portal_opened_share": ("11", "pct", None, None),
    "signed_before_departure_share": ("12", "pct", 100, "gte"),
    "commission_unclaimed": ("13", "count", 0, "lte"),
    "ai_drafts_light_edit_share": ("14", "pct", 60, "gte"),
    "mixed_trips_share": ("15", "pct", None, None),
    "report_exports": ("16", "count", None, None),
    "data_exports": ("18", "count", None, None),
}
NOT_BUILT = frozenset({"ops_release_errors", "commission_unclaimed", "ai_drafts_light_edit_share"})


def _status(key: str, value: float | int | None) -> str:
    _, _, target, comparator = SUCCESS[key]
    if key in NOT_BUILT:
        return "not_built"
    if value is None:
        return "no_data"
    if comparator is None or target is None:
        return "info"
    ok = value >= target if comparator == "gte" else value <= target
    return "met" if ok else "below"


async def success_metrics(
    ctx: TenantContext,
    sc: Scope,
    rows: list[CohortRow],
    picks_: dict[str, Any],
    lines: list[LineMoney],
    reply_rows: list[tuple[uuid.UUID | None, float]],
) -> list[dict[str, Any]]:
    a = _args(sc, sc.window)
    values: dict[str, Any] = {}
    created = sum(r.n for r in rows)
    values["budget_band_share"] = pct(sum(r.n for r in rows if r.has_budget), created)
    reply = [minutes for _, minutes in reply_rows]
    values["first_reply_median"] = _num(median(reply)) if reply else None
    values["quiet_leads"] = int(await ctx.conn.fetchval(q.QUIET_SQL, list(q.OPEN_STAGES), sc.owner) or 0)
    values["upgrade_rate"] = picks_["upgrade_rate_pct"]
    per_trip = await ctx.conn.fetchrow(q.MESSAGES_PER_BOOKED_SQL, *a)
    values["messages_per_booked_trip"] = _num(per_trip["median"]) if per_trip and per_trip["trips"] else None
    text = await ctx.conn.fetchrow(q.TEXT_PROPOSAL_SQL, *a)
    values["text_proposal_minutes"] = _num(text["median"]) if text and text["n"] else None
    sent = {r["channel"]: int(r["n"]) for r in await ctx.conn.fetch(q.SENT_BY_CHANNEL_SQL, *a)}
    values["messages_sent"] = sent.get("email", 0)
    values["chat_messages_sent"] = sent.get("whatsapp", 0) + sent.get("sms", 0)
    if sc.money:
        by_trip: dict[uuid.UUID, bool] = {}
        for line in lines:
            by_trip[line.trip_id] = by_trip.get(line.trip_id, True) and line.sell is not None and line.cost is not None
        values["margin_known_share"] = pct(sum(by_trip.values()), len(by_trip))
    deposit = await ctx.conn.fetchrow(q.DEPOSIT_48H_SQL, *a)
    values["deposit_48h_share"] = pct(deposit["fast"], deposit["accepted"]) if deposit else None
    values["overdue_before_departure"] = int(await ctx.conn.fetchval(q.OVERDUE_BEFORE_DEPARTURE_SQL, sc.owner) or 0)
    portal = await ctx.conn.fetchrow(q.PORTAL_SQL, *a)
    values["portal_opened_share"] = pct(portal["opened"], portal["trips"]) if portal else None
    signed = await ctx.conn.fetchrow(q.SIGNED_SQL, *a)
    values["signed_before_departure_share"] = pct(signed["signed"], signed["departed"]) if signed else None
    mixed = await ctx.conn.fetchrow(q.MIXED_SQL, *a)
    values["mixed_trips_share"] = pct(mixed["mixed"], mixed["trips"]) if mixed else None
    values["report_exports"] = int(await ctx.conn.fetchval(q.REPORT_EXPORTS_SQL, *a[:3]) or 0)
    values["data_exports"] = int(await ctx.conn.fetchval(q.DATA_EXPORTS_SQL, *a[:3]) or 0)
    out = []
    for key, (module, unit, target, comparator) in SUCCESS.items():
        if key == "margin_known_share" and not sc.money:
            continue
        value = None if key in NOT_BUILT else values.get(key)
        out.append(
            {
                "key": key,
                "module": module,
                "unit": unit,
                "value": value,
                "target": target,
                "comparator": comparator,
                "status": _status(key, value),
            }
        )
    return out


# ── The two reads the routes serve ──────────────────────────────────────────────────────────────────────────
async def summary(ctx: TenantContext, period: str, owner: str | None) -> dict[str, Any]:
    """The whole Reports page (A-14)."""
    sc = await scope(ctx, period, owner)
    head = await tiles(ctx, sc)
    rows = await cohort(ctx, sc, sc.window)
    lines = await money_lines(ctx, sc, sc.window)
    picks_ = await picks(ctx, sc)
    money = money_report(lines, sc) if sc.money else None
    reply_rows = await first_replies(ctx, sc)
    return {
        **head,
        "money_visible": sc.money,
        "funnel": funnel(rows),
        "outcomes": await outcomes(ctx, sc),
        "lead_sources": lead_sources(rows),
        "cycle": await cycle_time(ctx, sc),
        "open_trips": await open_trips(ctx, sc),
        "picks": picks_,
        "money": money,
        "top_products": money["top_products"] if money else await top_products_by_lines(ctx, sc),
        "payments": await payments(ctx, sc),
        "team": await team(ctx, sc, rows, reply_rows),
        "success": await success_metrics(ctx, sc, rows, picks_, lines, reply_rows),
        "insights_included": bool(await ctx.conn.fetchval(q.INSIGHTS_INCLUDED_SQL)),
    }


async def home_tiles(ctx: TenantContext) -> dict[str, Any]:
    """The Home page tiles (A-01: the same tiles as Reports, last 12 months, everyone's trips)."""
    sc = await scope(ctx, periods.DEFAULT_PERIOD, None)
    return await tiles(ctx, sc)
