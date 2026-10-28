// portal/lib/tours.ts
// AA-671 — the pure half of the Marketplace (catalog grid, filters, detail drawer) and the tour formatting the other
// tour pages share. No runtime imports (a type-only import is erased), so `npm test` (node --test) runs
// lib/tours.test.ts directly (the AA-761 pattern). Shapes follow CIS GET /v1/tours/pool and GET /v1/marketplace
// (AA-CIS-App api/routers/v1_tours.py, v1_marketplace.py).

import type { UsageMeter } from "./account";

// ── Formatting ──────────────────────────────────────────────────────────────────────────────────

/** AA-760 — a tour duration for people, one casing everywhere ("14 DAYS" / "7 days" → "14 days" / "7 days";
 *  "15 DAYS 14 NIGHTS" → "15 days"). A string with no leading number is only lower-cased. (Moved here from
 *  _components/ui.tsx by AA-671, which re-exports it.) */
export function fmtDuration(raw: string | null | undefined): string {
  if (!raw) return "";
  const m = /(\d+)\s*(day|night)/i.exec(raw);
  if (!m) return raw.trim().toLowerCase();
  const n = Number(m[1]);
  const unit = m[2].toLowerCase();
  return `${n} ${unit}${n === 1 ? "" : "s"}`;
}

/** A JSON value the API may store as a JSON string, or as a JSON string of a JSON string; null when unreadable. */
export function parseJsonish(raw: unknown): unknown {
  if (raw == null) return null;
  try {
    let v = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (typeof v === "string") v = JSON.parse(v);
    return v;
  } catch {
    return null;
  }
}

/** A list of strings from a JSON(-in-JSON) array; objects give their keyword / text / title. Empty when unreadable. */
export function parseStringList(raw: unknown): string[] {
  const v = parseJsonish(raw);
  if (!Array.isArray(v)) return [];
  return v
    .map((x) => {
      if (typeof x === "string") return x.trim();
      if (x && typeof x === "object") {
        const o = x as Record<string, unknown>;
        const s = o.keyword ?? o.text ?? o.title;
        return typeof s === "string" ? s.trim() : "";
      }
      return "";
    })
    .filter(Boolean);
}

/** The card's theme tags: the tour's SEO keywords, distinct (case-insensitive), at most `max`, without the words the
 *  card already shows as a chip (`exclude`, e.g. the country). */
export function themeTags(seoKeywords: unknown, max = 3, exclude: (string | null | undefined)[] = []): string[] {
  const seen = new Set<string>(exclude.filter((e): e is string => !!e).map((e) => e.trim().toLowerCase()));
  const out: string[] = [];
  for (const k of parseStringList(seoKeywords)) {
    const key = k.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(k);
    if (out.length >= max) break;
  }
  return out;
}

/** Whole US dollars with a thousands separator ("$1,225"). */
export function fmtUsdWhole(n: number): string {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

// ── Recently added ──────────────────────────────────────────────────────────────────────────────

/** A tour published this many days ago or less carries the "New" chip. */
export const NEW_TOUR_DAYS = 14;

export function isRecentlyAdded(publishedAt: string | null | undefined, now: Date, days = NEW_TOUR_DAYS): boolean {
  if (!publishedAt) return false;
  const t = new Date(publishedAt).getTime();
  if (Number.isNaN(t)) return false;
  const age = now.getTime() - t;
  return age >= 0 && age <= days * 86_400_000;
}

// ── Filters → GET /v1/tours/pool query ─────────────────────────────────────────────────────────────

export type DurationFilter = "all" | "1-3" | "4-7" | "8+";

export const DURATION_OPTIONS: { value: DurationFilter; label: string }[] = [
  { value: "all", label: "Any length" },
  { value: "1-3", label: "1–3 days" },
  { value: "4-7", label: "4–7 days" },
  { value: "8+", label: "8+ days" },
];

export interface CatalogFilters {
  search: string;
  country: string;
  duration: DurationFilter;
}

export const NO_CATALOG_FILTERS: CatalogFilters = { search: "", country: "", duration: "all" };

/** 24 = a full last row on 2, 3 and 4 columns. */
export const CATALOG_PAGE_SIZE = 24;

export function hasCatalogFilters(f: CatalogFilters): boolean {
  return f.search.trim() !== "" || f.country !== "" || f.duration !== "all";
}

/** The query string (no "?") for GET /v1/tours/pool: paging, name search, country, duration range. */
export function poolQuery(f: CatalogFilters, page: number, pageSize = CATALOG_PAGE_SIZE): string {
  const p = new URLSearchParams({ page: String(Math.max(1, Math.floor(page))), page_size: String(pageSize) });
  const search = f.search.trim();
  if (search) p.set("search", search);
  if (f.country) p.set("country", f.country);
  if (f.duration === "8+") {
    p.set("duration_min", "8");
  } else if (f.duration !== "all") {
    const [min, max] = f.duration.split("-");
    p.set("duration_min", min);
    p.set("duration_max", max);
  }
  return p.toString();
}

export function pageCount(total: number, pageSize = CATALOG_PAGE_SIZE): number {
  return Math.max(1, Math.ceil(Math.max(0, total) / pageSize));
}

// ── View (All tours / In my catalog) ─────────────────────────────────────────────────────────────

export type MarketplaceView = "all" | "mine";

/** The `?view=` value → the view; anything else is the catalog. */
export function marketplaceView(param: string | null | undefined): MarketplaceView {
  return param === "mine" ? "mine" : "all";
}

// ── Quota note (drawer footer, bulk bar) ─────────────────────────────────────────────────────────

export interface QuotaNote {
  tone: "neutral" | "warning" | "danger";
  text: string;
}

/** What starting `count` rewrites means for the plan, in words, from the Billing meter (lib/account usageMeter).
 *  Only what CIS does (AA-640): a rewrite past the quota still runs and is billed as overage; no plan = not billed. */
export function rewriteQuotaNote(m: UsageMeter | null, count = 1): QuotaNote | null {
  if (!m) return null;
  const n = Math.max(1, Math.floor(count));
  const rate = m.ratePerTour ? ` at ${fmtUsdWhole(m.ratePerTour)} each` : "";
  if (m.state === "no_plan") return { tone: "neutral", text: "No plan attached yet: rewrites are not billed." };
  if (m.state === "unlimited") return { tone: "neutral", text: "Your plan has no rewrite limit." };
  const left = m.remaining ?? 0;
  if (m.state === "over" || left === 0) {
    return { tone: "danger", text: `Your plan's rewrites are used up: ${n === 1 ? "this rewrite is" : `these ${n} rewrites are`} billed as overage${rate}.` };
  }
  if (n > left) {
    const extra = n - left;
    return { tone: "warning", text: `${left} rewrite${left === 1 ? "" : "s"} left this period: ${extra} of these ${n} ${extra === 1 ? "is" : "are"} billed as overage${rate}.` };
  }
  const tone = m.state === "near" ? "warning" : "neutral";
  return { tone, text: `${left} rewrite${left === 1 ? "" : "s"} left this period.` };
}
