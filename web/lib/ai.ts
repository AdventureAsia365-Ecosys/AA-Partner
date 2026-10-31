// web/lib/ai.ts
// AA-964 (card CRM-33a) — the shape of Settings > AI (api/app/llm/agency_ai.py, GET / PUT /crm/v1/settings/ai) and the
// small pure helpers the settings page uses: the meter tone at the 75 / 90 / 100 % alert levels (design R5), budget
// input checks, and the wording key of an API refusal.

export interface AiSettings {
  ai_enabled: boolean;
  ai_budget_usd: string;
  budget_max_usd: string;
  spent_usd_month: string;
  budget_used_pct: number;
  alert_level: 75 | 90 | 100 | null;
  period_start: string;
  provider: "stub" | "bedrock";
  test_mode: boolean;
  rate_limit_per_minute: number;
  can_edit: boolean;
}

export type MeterTone = "success" | "warning" | "danger";

/** The meter colour: green below 75 %, amber from 75 % (and 90 %), red at 100 % (AI stops, nothing is charged). */
export function meterTone(pct: number): MeterTone {
  if (pct >= 100) return "danger";
  if (pct >= 75) return "warning";
  return "success";
}

/** The bar width, clamped to 0..100 (a month can go past 100 % by the last call's own cost). */
export function meterWidth(pct: number): number {
  if (!Number.isFinite(pct) || pct < 0) return 0;
  return Math.min(100, pct);
}

/** A budget the API accepts: 0 up to the plan's cap, at most two decimals. Returns the error key or null. */
export function budgetError(value: string, max: string): "ai.error.budget_format" | "ai.error.budget_above_plan" | null {
  const text = value.trim();
  if (!/^\d{1,5}(\.\d{1,2})?$/.test(text)) return "ai.error.budget_format";
  if (Number(text) > Number(max)) return "ai.error.budget_above_plan";
  return null;
}

/** The message key of an API refusal on this page. */
export function aiErrorKey(detail: string): string {
  if (detail === "budget_above_plan" || detail === "role_forbidden") return `ai.error.${detail}`;
  return "ai.error.generic";
}

/** "$0.50 of $2.00" in the person's locale (USD: the budget is AA's plan money). */
export function usd(value: string, locale: string): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return value;
  return new Intl.NumberFormat(locale, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n);
}
