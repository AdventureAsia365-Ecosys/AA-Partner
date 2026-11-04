// web/lib/studio/tours.ts
// AA-671 — the pure half of the Marketplace (catalog grid, filters, detail drawer) and the tour formatting the other
// tour pages share. No runtime imports (a type-only import is erased), so `npm test` (node --test) runs
// lib/tours.test.ts directly (the AA-761 pattern). Shapes follow CIS GET /v1/tours/pool and GET /v1/marketplace
// (AA-CIS-App api/routers/v1_tours.py, v1_marketplace.py).

import type { UsageMeter } from "./account";
import { translator, type Lang } from "@/app/_kit/core/i18n";
import { TOURS_LIB_MESSAGES } from "./lib.messages";

const tr = (lang: Lang) => translator(TOURS_LIB_MESSAGES, lang);

// ── Formatting ──────────────────────────────────────────────────────────────────────────────────

/** AA-760 — a tour duration for people, one casing everywhere ("14 DAYS" / "7 days" → "14 days" / "7 days";
 *  "15 DAYS 14 NIGHTS" → "15 days"). A string with no leading number is only lower-cased. (Moved here from
 *  _components/ui.tsx by AA-671; lib/studio/format.ts re-exports it.) */
export function fmtDuration(raw: string | null | undefined, lang: Lang = "en"): string {
  if (!raw) return "";
  const m = /(\d+)\s*(day|night)/i.exec(raw);
  if (!m) return raw.trim().toLowerCase();
  const n = Number(m[1]);
  return m[2].toLowerCase() === "night" ? tr(lang)("nights", n) : tr(lang)("days", n);
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

// ── From price (AA-1029, contract price.from.v1) ───────────────────────────────────────────────────

/** The public "from" price of an AA tour: AA-Booking's suggested retail price per person (contract price.from.v1, read
 *  by CIS GET /v1/tours/pool as `from_price`). The studio never computes a price and never shows the source text
 *  (`price_raw`) as one: no price from Booking = nothing is shown. Same shape as the TripPlanner tour page reads. */
export interface FromPrice {
  /** Decimal as text ("235.00"). */
  amount: string;
  /** ISO 4217 ("USD"). */
  currency: string;
  basis: string;
  /** The party size the price is for (2 in v1). */
  pax: number;
  season: string | null;
  /** Last valid day (ISO date); "9999-12-31" = open-ended. */
  valid_to: string | null;
}

/** "$235" / "€1,200": Intl en-US currency in whole units, exactly as the TripPlanner tour page formats it
 *  (frontend/lib/fromPrice.ts there), so a tour reads the same price on both. Null for a missing or unusable price
 *  (no amount, not positive, not an ISO code). */
export function fmtFromPrice(p: FromPrice | null | undefined): string | null {
  if (!p) return null;
  const n = Number(p.amount);
  const code = typeof p.currency === "string" ? p.currency.trim() : "";
  if (!Number.isFinite(n) || n <= 0 || !/^[A-Z]{3}$/.test(code)) return null;
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency: code, maximumFractionDigits: 0 }).format(n);
  } catch {
    return null;
  }
}

/** The caveat under every "from" price, the TripPlanner tour page's sentence (party size from the contract row). */
export function fromPriceNote(p: FromPrice, lang: Lang = "en"): string {
  const pax = Number.isInteger(p.pax) && p.pax > 0 ? p.pax : 2;
  return tr(lang)("priceNote", pax);
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

const DURATION_KEYS = [
  { value: "all", key: "durAll" },
  { value: "1-3", key: "dur13" },
  { value: "4-7", key: "dur47" },
  { value: "8+", key: "dur8" },
] as const;

/** The duration filter's options in the UI language. */
export function durationOptions(lang: Lang = "en"): { value: DurationFilter; label: string }[] {
  return DURATION_KEYS.map((o) => ({ value: o.value, label: tr(lang)(o.key) }));
}

export const DURATION_OPTIONS = durationOptions("en");

// ── Activity / intensity facets (CIS GET /v1/tours/pool `activities`, `intensity`, `facets`) ──────────────────────

/** English names of the CIS graph activity codes (geo.tour_place activity vocabulary). An unknown code (CIS added
 *  one) still reads: underscores become spaces, first letter upper case. */
const ACTIVITY_KEYS = {
  adventure_sport: "actAdventure",
  cultural_heritage: "actCulture",
  culinary: "actFood",
  local_immersion: "actLocal",
  trekking: "actTrekking",
  water_activities: "actWater",
  wellness_relaxation: "actWellness",
  wildlife_nature: "actWildlife",
} as const;

export const ACTIVITY_LABELS: Record<string, string> = Object.fromEntries(
  Object.entries(ACTIVITY_KEYS).map(([code, key]) => [code, TOURS_LIB_MESSAGES.en[key]]),
);

/** The tenant intensity names CIS answers (the hardest part of the tour), easiest first. */
const INTENSITY_KEYS = { easy: "intEasy", moderate: "intModerate", challenging: "intChallenging", strenuous: "intStrenuous" } as const;

export const INTENSITY_LABELS: Record<string, string> = Object.fromEntries(
  Object.entries(INTENSITY_KEYS).map(([code, key]) => [code, TOURS_LIB_MESSAGES.en[key]]),
);

function humanize(code: string): string {
  const s = code.replace(/_/g, " ").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

export function activityLabel(code: string, lang: Lang = "en"): string {
  return Object.hasOwn(ACTIVITY_KEYS, code) ? tr(lang)(ACTIVITY_KEYS[code as keyof typeof ACTIVITY_KEYS]) : humanize(code);
}

export function intensityLabel(code: string, lang: Lang = "en"): string {
  return Object.hasOwn(INTENSITY_KEYS, code) ? tr(lang)(INTENSITY_KEYS[code as keyof typeof INTENSITY_KEYS]) : humanize(code);
}

/** The select options for a facet: the values the API lists (`facets`), else the known ones; labels sorted by the
 *  API order (intensities go easiest first, so their order matters). */
export function facetOptions(fromApi: string[] | undefined, known: Record<string, string>,
  label: (code: string) => string): { value: string; label: string }[] {
  const codes = fromApi && fromApi.length > 0 ? fromApi : Object.keys(known);
  return codes.map((value) => ({ value, label: label(value) }));
}

// ── Cover photo (CIS public photo route, WebP renditions 600 px `size=small` and 1600 px `size=large`) ─────────

/** Widths of the CIS photo renditions (AA-CIS-App services/photos/sync.py SIZES). */
export const COVER_WIDTHS = { small: 600, large: 1600 } as const;

/** The `srcset` for a cover from the two rendition URLs, so the browser picks the size for the slot and the screen
 *  density (a 2x phone gets the 1600 px one, a desktop card the 600 px one). Undefined when there is fewer than two
 *  usable URLs (a plain `src` is enough then). */
export function coverSrcSet(small: string | null | undefined, large: string | null | undefined): string | undefined {
  const ok = (u: string | null | undefined): u is string => typeof u === "string" && u.trim() !== "";
  if (!ok(small) || !ok(large) || small === large) return undefined;
  return `${small} ${COVER_WIDTHS.small}w, ${large} ${COVER_WIDTHS.large}w`;
}

/** "Recently added" filter: published in the last N days (`added_within_days`), or any time. */
export type AddedFilter = "all" | "14" | "30" | "90";

const ADDED_KEYS = [
  { value: "all", key: "addedAll" },
  { value: "14", key: "added14" },
  { value: "30", key: "added30" },
  { value: "90", key: "added90" },
] as const;

/** The "recently added" filter's options in the UI language. */
export function addedOptions(lang: Lang = "en"): { value: AddedFilter; label: string }[] {
  return ADDED_KEYS.map((o) => ({ value: o.value, label: tr(lang)(o.key) }));
}

export const ADDED_OPTIONS = addedOptions("en");

export interface CatalogFilters {
  search: string;
  country: string;
  duration: DurationFilter;
  /** A CIS activity code, "" = any. */
  activity: string;
  /** A tenant intensity name (easy … strenuous), "" = any. */
  intensity: string;
  added: AddedFilter;
}

export const NO_CATALOG_FILTERS: CatalogFilters = {
  search: "", country: "", duration: "all", activity: "", intensity: "", added: "all",
};

/** 24 = a full last row on 2, 3 and 4 columns. */
export const CATALOG_PAGE_SIZE = 24;

export function hasCatalogFilters(f: CatalogFilters): boolean {
  return f.search.trim() !== "" || f.country !== "" || f.duration !== "all"
    || f.activity !== "" || f.intensity !== "" || f.added !== "all";
}

/** The query string (no "?") for GET /v1/tours/pool: paging, name search, country, duration range, activity,
 *  intensity and "added in the last N days". */
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
  if (f.activity) p.set("activity", f.activity);
  if (f.intensity) p.set("intensity", f.intensity);
  if (f.added !== "all") p.set("added_within_days", f.added);
  return p.toString();
}

/** The card tags: the tour's activities (CIS graph) when it has any, else its theme tags (SEO keywords). */
export function cardTags(activities: string[] | null | undefined, themes: string[], max = 3, lang: Lang = "en"): string[] {
  const acts = (activities ?? []).filter((a) => typeof a === "string" && a.trim() !== "").map((a) => activityLabel(a, lang));
  return (acts.length > 0 ? acts : themes).slice(0, max);
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
export function rewriteQuotaNote(m: UsageMeter | null, count = 1, lang: Lang = "en"): QuotaNote | null {
  if (!m) return null;
  const t = tr(lang);
  const n = Math.max(1, Math.floor(count));
  const rate = m.ratePerTour ? t("rate", fmtUsdWhole(m.ratePerTour)) : "";
  if (m.state === "no_plan") return { tone: "neutral", text: t("quotaNoPlan") };
  if (m.state === "unlimited") return { tone: "neutral", text: t("quotaUnlimited") };
  const left = m.remaining ?? 0;
  if (m.state === "over" || left === 0) {
    return { tone: "danger", text: n === 1 ? t("quotaUsedUpOne", rate) : t("quotaUsedUpMany", n, rate) };
  }
  if (n > left) {
    return { tone: "warning", text: t("quotaPartly", left, n - left, n, rate) };
  }
  const tone = m.state === "near" ? "warning" : "neutral";
  return { tone, text: t("quotaLeft", left) };
}
