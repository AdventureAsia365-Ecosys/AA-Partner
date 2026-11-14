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
  /** True only when nothing billable runs on this environment: the test model and no billed privacy check. */
  test_mode: boolean;
  /** The test model writes the drafts (it costs nothing). Older APIs send only `test_mode`. */
  model_test_mode?: boolean;
  /** The privacy check before each draft (Bedrock Guardrails) is billed; its cost counts in this month's spend. */
  scrub_billed?: boolean;
  rate_limit_per_minute: number;
  can_edit: boolean;
  /** AA-1027 F3: the agency's plan has the AI assistant (crm.tenant_entitlement ai_assist); without it a draft is 403. */
  ai_assist_allowed?: boolean;
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

/**
 * The test-mode note (AA-1027 F3 follow-up): null when a real model writes the drafts; "free" only when the API says
 * nothing billable runs; "scrub_billed" when the test model writes but the privacy check is billed. An API that does
 * not say whether the check is billed gets the billed wording: the page never claims "nothing is charged" unless sure.
 */
export type TestModeNote = "free" | "scrub_billed" | null;

export function testModeNote(settings: AiSettings | null): TestModeNote {
  if (!settings) return null;
  if (!(settings.model_test_mode ?? settings.test_mode)) return null;
  return settings.scrub_billed === false && settings.test_mode ? "free" : "scrub_billed";
}

/** The plan lacks the AI assistant (AI drafts are its only use, so the switch alone does not start them). */
export function notOnPlan(settings: AiSettings): boolean {
  return settings.ai_assist_allowed === false;
}

/** AA-1096 S280: the per-minute line of Settings > AI; 0 turns the limit off (crm.rate.ai_per_minute), never "up to 0". */
export function rateLimitLine(n: number): { key: "ai.limit" | "ai.limitOff"; n: number } {
  return { key: n > 0 ? "ai.limit" : "ai.limitOff", n };
}
