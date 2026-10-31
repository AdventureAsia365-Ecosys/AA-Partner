// web/lib/reports.ts
// AA-966 (card CRM-34; design v3 A-14 Reports, A-01 Home tiles, R23, R28): the shapes of GET /reports/summary,
// /reports/tiles and /reports/export, and the pure helpers the pages use. Numbers come computed by the API (amounts as
// decimal strings in the agency's base currency, rates in percent); nothing here re-computes a total. The assistant
// gets no money section (the API omits it), so every money helper accepts null. Edge-safe.

import { formatMoney } from "./ledger";
import { MESSAGES, fmt, t, type Lang, type MessageKey } from "./i18n";

export const PERIODS = ["30d", "90d", "12m", "ytd"] as const;
export type Period = (typeof PERIODS)[number];
export const DEFAULT_PERIOD: Period = "12m";
export const EXPORT_SECTIONS = [
  "funnel",
  "lead_sources",
  "outcomes",
  "open_trips",
  "money_by_month",
  "top_products",
  "outstanding",
  "team",
  "success",
] as const;
export type ExportSection = (typeof EXPORT_SECTIONS)[number];
export const MONEY_SECTIONS: readonly ExportSection[] = ["money_by_month"];

export type TileKey = "inquiries" | "win_rate" | "revenue" | "margin" | "aa_share";
export type Compare = "previous_year" | "trend_3m" | "none";

export interface Tile {
  key: TileKey;
  unit: "count" | "pct" | "money";
  value: number | string | null;
  href: string;
  basis: "sell" | "lines" | null;
  previous: number | string | null;
  delta: number | null;
  trend: { from: string; to: string; value: number | string | null }[];
}

export interface PeriodInfo {
  from: string;
  to: string;
  today: string;
  timezone: string;
  base_currency: string;
  compare: Compare;
  previous: { from: string; to: string } | null;
  owner_member_id: string | null;
}

export interface TilesView {
  period: PeriodInfo;
  tiles: Tile[];
}

export interface MoneyTotals {
  sell_base: string;
  cost_base: string;
  margin_base: string;
  margin_pct: number | null;
  commission_expected_base: string;
  lines: number;
  lines_missing_amounts: number;
}

export interface Product {
  supplier_kind: "aa" | "agency";
  title: string;
  lines: number;
  trips: number;
  sell_base?: string;
}

export interface SuccessMetric {
  key: string;
  module: string;
  unit: "pct" | "count" | "minutes";
  value: number | null;
  target: number | null;
  comparator: "gte" | "lte" | null;
  status: "met" | "below" | "no_data" | "info" | "not_built";
}

export interface ReportView extends TilesView {
  money_visible: boolean;
  funnel: {
    created: number;
    won: number;
    win_rate_pct: number | null;
    stages: { stage: string; count: number }[];
    lost: number;
    cancelled: number;
  };
  outcomes: { lost: { reason: string | null; count: number }[]; cancelled: { reason: string | null; count: number }[] };
  lead_sources: { source: string | null; trips: number; won: number; win_rate_pct: number | null }[];
  cycle: { trips: number; median_days: number | null };
  open_trips: { stage: string; count: number; stalled: number; stalled_not_waiting: number; median_age_days: number | null }[];
  picks: {
    picks: number;
    kept_default: number;
    upgraded: number;
    kept_default_pct: number | null;
    upgrade_rate_pct: number | null;
    most_changed_type: string | null;
  };
  money:
    | (MoneyTotals & {
        currency: string;
        aa_share_pct: number | null;
        by_month: (MoneyTotals & { month: string })[];
        by_supplier: (MoneyTotals & { supplier_kind: string })[];
        by_party_type: (MoneyTotals & { party_type: string | null })[];
        top_products: (Product & MoneyTotals)[];
      })
    | null;
  top_products: Product[];
  payments: {
    open_invoices: { currency: string; count: number; total: string; paid: string; outstanding: string; overdue_count: number; overdue: string }[];
    schedule_due: { currency: string; kind: string; count: number; total: string; overdue_count: number; overdue: string }[];
    collected: { currency: string; count: number; received: string; refunded: string; net: string }[];
  };
  team: {
    members: {
      member_id: string;
      role: "owner" | "agent" | "assistant";
      status: string;
      is_me: boolean;
      open_trips: number;
      new_trips: number;
      won: number;
      overdue_tasks: number;
      tasks_done: number;
      messages_sent: number;
      first_reply_median_min: number | null;
    }[];
    unassigned_open_trips: number;
    unanswered_24h: number;
  };
  success: SuccessMetric[];
  insights_included: boolean;
}

export function isPeriod(value: unknown): value is Period {
  return typeof value === "string" && (PERIODS as readonly string[]).includes(value);
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The page's filters from its search params: an unknown period falls back to the default, an unknown owner to all. */
export function parseFilters(params: Record<string, string | string[] | undefined>): { period: Period; owner: string | null } {
  const p = typeof params.period === "string" ? params.period : "";
  const o = typeof params.owner === "string" ? params.owner : "";
  return { period: isPeriod(p) ? p : DEFAULT_PERIOD, owner: o === "me" || UUID_RE.test(o) ? o : null };
}

export function reportQuery(f: { period: Period; owner: string | null }, extra: Record<string, string> = {}): URLSearchParams {
  const q = new URLSearchParams({ period: f.period, ...extra });
  if (f.owner) q.set("owner", f.owner);
  return q;
}

export function pageHref(f: { period: Period; owner: string | null }): string {
  return `/reports?${reportQuery(f).toString()}`;
}

export function exportPath(section: ExportSection, f: { period: Period; owner: string | null }): string {
  return `reports/export?${reportQuery(f, { section }).toString()}`;
}

export function exportFilename(section: ExportSection, period: Period, now: Date = new Date()): string {
  const stamp = now.toISOString().slice(0, 10).replace(/-/g, "");
  return `report-${section.replace(/_/g, "-")}-${period}-${stamp}.csv`;
}

/** The sections a role may download (the money ones need view_margin: owner and agent). */
export function exportableSections(moneyVisible: boolean): ExportSection[] {
  return EXPORT_SECTIONS.filter((s) => moneyVisible || !MONEY_SECTIONS.includes(s));
}

export function pctText(value: number | null | undefined, locale: string): string {
  if (value === null || value === undefined) return "—";
  return `${value.toLocaleString(locale, { maximumFractionDigits: 1 })}%`;
}

export function countText(value: number | null | undefined, locale: string): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString(locale, { maximumFractionDigits: 1 });
}

/** A tile value as text: money in the agency's base currency, rates with %, counts plain. */
export function tileValue(tile: Pick<Tile, "unit" | "value">, currency: string, locale: string): string {
  if (tile.value === null || tile.value === undefined) return "—";
  if (tile.unit === "money") return formatMoney(String(tile.value), currency, locale);
  if (tile.unit === "pct") return pctText(Number(tile.value), locale);
  return countText(Number(tile.value), locale);
}

/** The hint under a tile: "+18% vs last year", "+4 pts vs last year", or the three-month trend for a new agency. */
export function tileHint(
  tile: Tile,
  compare: Compare,
  lang: Lang,
  locale: string,
  currency: string,
): { text: string; tone: "success" | "danger" | "neutral" } {
  if (compare === "previous_year") {
    if (tile.delta === null) return { text: t(lang, "reports.noPrevious"), tone: "neutral" };
    const sign = tile.delta > 0 ? "+" : "";
    const key: MessageKey = tile.unit === "pct" ? "reports.deltaPts" : "reports.deltaPct";
    const text = fmt(t(lang, key), { delta: `${sign}${tile.delta.toLocaleString(locale, { maximumFractionDigits: 1 })}` });
    return { text, tone: tile.delta > 0 ? "success" : tile.delta < 0 ? "danger" : "neutral" };
  }
  if (compare === "trend_3m" && tile.trend.length) {
    const values = tile.trend.map((p) => tileValue({ unit: tile.unit, value: p.value }, currency, locale));
    return { text: fmt(t(lang, "reports.trend3m"), { values: values.join(" → ") }), tone: "neutral" };
  }
  return { text: t(lang, "reports.noHistory"), tone: "neutral" };
}

/** A code from the API in plain words: a known label, else the code with spaces ("website_form" -> "website form"). */
export function codeText(lang: Lang, prefix: string, code: string | null, fallback: MessageKey): string {
  if (!code) return t(lang, fallback);
  const key = `${prefix}.${code}`;
  return key in MESSAGES.en ? t(lang, key as MessageKey) : code.replace(/_/g, " ");
}

/** Bar widths for a horizontal bar list, in percent of the largest value (0 when every value is 0). */
export function barWidths(values: number[]): number[] {
  const max = Math.max(0, ...values);
  return values.map((v) => (max > 0 ? Math.round((v / max) * 1000) / 10 : 0));
}

export function successValue(m: SuccessMetric, lang: Lang, locale: string): string {
  if (m.status === "not_built") return t(lang, "reports.success.notBuilt");
  if (m.value === null) return t(lang, "reports.success.noData");
  if (m.unit === "pct") return pctText(m.value, locale);
  if (m.unit === "minutes") return fmt(t(lang, "reports.minutes"), { n: countText(m.value, locale) });
  return countText(m.value, locale);
}

export function successTarget(m: SuccessMetric, lang: Lang, locale: string): string {
  if (m.target === null || m.comparator === null) return "";
  const value = m.unit === "pct" ? pctText(m.target, locale) : m.unit === "minutes"
    ? fmt(t(lang, "reports.minutes"), { n: countText(m.target, locale) })
    : countText(m.target, locale);
  return fmt(t(lang, m.comparator === "gte" ? "reports.target.gte" : "reports.target.lte"), { value });
}
