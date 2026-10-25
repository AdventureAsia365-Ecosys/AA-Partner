// portal/lib/account.ts — AA-761: the pure half of the Account pages (Billing, Activity, Integrations). No React, no
// imports, so `npm test` (node --test) runs it. Shapes follow the CIS API: GET /v1/quota and /v1/billing
// (api/routers/v1_tours.py), GET /v1/account/activity and /v1/account/api-key (api/routers/v1_account.py).

export const ACTIVITY_PAGE_SIZES = [10, 25, 50, 100];

/** GET /v1/quota. */
export interface Quota {
  plan_tier: string | null;
  tours_per_month: number;
  rewrites_used: number;
  rewrites_remaining: number;
  /** First day of the next period, YYYY-MM-DD (UTC). */
  resets_at: string;
}

/** The /v1/billing fields the usage meter reads (the shell holds the whole body). */
export interface BillingFigures {
  plan_attached?: boolean;
  overage_usd?: number | string | null;
  tours_overage?: number | string | null;
  overage_rate_usd_per_tour?: number | string | null;
}

export type MeterState = "no_plan" | "unlimited" | "ok" | "near" | "over";

export interface UsageMeter {
  state: MeterState;
  used: number;
  quota: number | null;
  /** 0–100, for the bar (100 once the quota is reached). */
  pct: number;
  remaining: number | null;
  overTours: number;
  overageUsd: number;
  ratePerTour: number | null;
  resetsAt: string | null;
}

// A plan quota at or above this reads as "Unlimited" (the API's enterprise rows, PlaceholderTabs fmtQuota).
export const UNLIMITED_QUOTA = 999_999;
// The bar turns amber from this share of the quota.
export const NEAR_QUOTA_PCT = 80;

function num(v: unknown): number {
  const n = typeof v === "string" ? Number(v) : typeof v === "number" ? v : NaN;
  return Number.isFinite(n) ? n : 0;
}

/** The rewrite meter: used vs quota and the reset date from /v1/quota, the overage so far from /v1/billing. */
export function usageMeter(quota: Quota | null, billing: BillingFigures | null): UsageMeter {
  const used = quota ? Math.max(0, num(quota.rewrites_used)) : 0;
  const limit = quota ? Math.max(0, num(quota.tours_per_month)) : 0;
  const overageUsd = Math.max(0, num(billing?.overage_usd));
  const rate = billing?.overage_rate_usd_per_tour == null ? null : num(billing.overage_rate_usd_per_tour);
  const base = { used, overageUsd, ratePerTour: rate, resetsAt: quota?.resets_at ?? null };
  if (billing?.plan_attached === false) {
    return { ...base, state: "no_plan", quota: null, pct: 0, remaining: null, overTours: 0 };
  }
  if (limit >= UNLIMITED_QUOTA) {
    return { ...base, state: "unlimited", quota: null, pct: 0, remaining: null, overTours: 0 };
  }
  const overTours = Math.max(0, used - limit, num(billing?.tours_overage));
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : used > 0 ? 100 : 0;
  const state: MeterState = used > limit || overTours > 0 ? "over" : pct >= NEAR_QUOTA_PCT ? "near" : "ok";
  return { ...base, state, quota: limit, pct, remaining: Math.max(0, limit - used), overTours };
}

/** "1 Nov 2026" from YYYY-MM-DD (UTC, English: the product UI opens in English). */
export function fmtDay(ymd: string | null | undefined): string {
  if (!ymd) return "—";
  const d = new Date(`${ymd.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

export function fmtUsd(n: number): string {
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`;
}

// ── Activity ────────────────────────────────────────────────────────────────────────────────────

export type ActivityActor = "you" | "adventure_asia" | "system";

/** One row of GET /v1/account/activity. */
export interface ActivityItem {
  id: string;
  at: string | null;
  action: string;
  category: "content" | "tours" | "account";
  label: string;
  actor: ActivityActor;
  resource_type: string | null;
  resource_id: string | null;
  detail: string;
}

export interface ActivityPage {
  data: ActivityItem[];
  pagination: { page: number; page_size: number; total: number; pages: number };
  categories: { id: string; label: string }[];
  actions: { id: string; label: string; category: string }[];
}

export interface ActivityFilters {
  category: string; // "" = every type
  since: string; // YYYY-MM-DD or ""
  until: string; // YYYY-MM-DD or ""
}

export const NO_FILTERS: ActivityFilters = { category: "", since: "", until: "" };

const YMD = /^\d{4}-\d{2}-\d{2}$/;

/** A filter value the API accepts, else "" (a half-typed date never reaches the server). */
export function cleanFilters(f: ActivityFilters): ActivityFilters {
  const since = YMD.test(f.since) ? f.since : "";
  const until = YMD.test(f.until) ? f.until : "";
  // The API refuses since > until (400): swap them, that is what the person meant.
  if (since && until && since > until) return { category: f.category, since: until, until: since };
  return { category: f.category, since, until };
}

/** The query string for GET /v1/account/activity (and, without paging, /activity/export). */
export function activityQuery(f: ActivityFilters, page?: number, pageSize?: number): string {
  const c = cleanFilters(f);
  const p = new URLSearchParams();
  if (c.category) p.set("category", c.category);
  if (c.since) p.set("since", c.since);
  if (c.until) p.set("until", c.until);
  if (page !== undefined) p.set("page", String(Math.max(1, Math.floor(page))));
  if (pageSize !== undefined) p.set("page_size", String(pageSize));
  const s = p.toString();
  return s ? `?${s}` : "";
}

export function hasFilters(f: ActivityFilters): boolean {
  const c = cleanFilters(f);
  return !!(c.category || c.since || c.until);
}

/** activity-2026-10-25.csv, or activity-content-2026-10-01-to-2026-10-25.csv with filters. */
export function exportFilename(f: ActivityFilters, today: string): string {
  const c = cleanFilters(f);
  const parts = ["activity"];
  if (c.category) parts.push(c.category);
  if (c.since || c.until) parts.push(`${c.since || "start"}-to-${c.until || today}`);
  else parts.push(today);
  return `${parts.join("-")}.csv`;
}

export const ACTOR_LABEL: Record<ActivityActor, string> = {
  you: "You",
  adventure_asia: "Adventure Asia",
  system: "Automatic",
};

export function actorLabel(a: string): string {
  return ACTOR_LABEL[a as ActivityActor] ?? "Automatic";
}

// ── Integrations ────────────────────────────────────────────────────────────────────────────────

export interface ConnectionFacts {
  connected?: boolean;
  last_verified_at?: string | null;
  last_verify_error?: string | null;
}

export type ConnectionHealth = "not_connected" | "healthy" | "failing" | "untested";

/** A channel's health from its status: the last test's result (the test itself runs on Integrations). */
export function connectionHealth(s: ConnectionFacts | null | undefined): ConnectionHealth {
  if (!s?.connected) return "not_connected";
  if (s.last_verify_error) return "failing";
  return s.last_verified_at ? "healthy" : "untested";
}

export const HEALTH_LABEL: Record<ConnectionHealth, string> = {
  not_connected: "Not connected",
  healthy: "Working",
  failing: "Needs attention",
  untested: "Not tested yet",
};

/** GET /v1/account/api-key. */
export interface ApiKeyFacts {
  has_key: boolean;
  masked: string | null;
  issued_at: string | null;
  last_sign_in_at: string | null;
  last_api_call_at: string | null;
  last_used_at: string | null;
}
