// web/lib/staffSettings.ts
// AA-1019 (S262 live finding O2) — the agency settings card of the staff console's agency overview (GET
// /crm/staff/v1/agencies/{id}/overview `settings`): a human label and a formatted value per key, EN and VI, instead of
// the raw "ai enabled false". The keys are api/app/support/reads.py SETTINGS_KEYS, in that order
// (tests/staffSettings.test.ts reads that tuple and fails when a key has no label). Pure, edge-safe.

import { fmt, MESSAGES, plural, t, type Lang, type MessageKey } from "./i18n";
import { formatMoney } from "./ledger";

/** Must equal api/app/support/reads.py SETTINGS_KEYS (same order: the card lists them so). */
export const SETTING_KEYS = [
  "ai_enabled",
  "ai_budget_usd",
  "timezone",
  "base_currency",
  "booking_validation_level",
  "fx_mode",
  "trash_days",
  "auto_archive_months",
  "stage2_auto_send",
  "invoice_due_days",
  "service_fee_enabled",
  "onboarding_later",
  "onboarding_done",
] as const;
export type SettingKey = (typeof SETTING_KEYS)[number];

const BOOLEANS = new Set<string>(["ai_enabled", "stage2_auto_send", "service_fee_enabled"]);
const DAYS = new Set<string>(["trash_days", "invoice_due_days"]);
const STEP_LISTS = new Set<string>(["onboarding_later", "onboarding_done"]);

function isKnown(key: string): key is SettingKey {
  return (SETTING_KEYS as readonly string[]).includes(key);
}

/** A key the API added later than this web: its words, first letter capitalised ("new flag" -> "New flag"). */
function plainWords(key: string): string {
  const words = key.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function settingLabel(lang: Lang, key: string): string {
  return isKnown(key) ? t(lang, `staff.setting.${key}` as MessageKey) : plainWords(key);
}

/** A code with its own label under `prefix` (`staff.setting.level.basic`), else the code in plain words. */
function codeText(lang: Lang, prefix: string, code: string): string {
  const key = `${prefix}.${code}`;
  return key in MESSAGES.en ? t(lang, key as MessageKey) : plainWords(code);
}

function count(value: unknown): number | null {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) && String(value).trim() !== "" ? n : null;
}

/** The value as staff read it: On / Off, "2.00 USD a month", "Basic", "30 days", the setup steps by name, "—" when
 * the agency has none. Never re-computes money (the API sends the decimal string). */
export function settingValue(lang: Lang, key: string, value: unknown, locale: string): string {
  if (value === null || value === undefined || value === "") return "—";
  if (BOOLEANS.has(key) || typeof value === "boolean") {
    if (typeof value !== "boolean") return String(value);
    return t(lang, value ? "staff.setting.on" : "staff.setting.off");
  }
  if (key === "ai_budget_usd") {
    return fmt(t(lang, "staff.setting.perMonth"), { amount: formatMoney(String(value), "USD", locale) });
  }
  if (DAYS.has(key) || key === "auto_archive_months") {
    const n = count(value);
    if (n === null) return String(value);
    return plural(lang, DAYS.has(key) ? "staff.setting.days" : "staff.setting.months", n);
  }
  if (key === "booking_validation_level") return codeText(lang, "staff.setting.level", String(value));
  if (key === "fx_mode") return codeText(lang, "staff.setting.fx", String(value));
  if (Array.isArray(value)) {
    if (value.length === 0) return t(lang, "staff.setting.none");
    const prefix = STEP_LISTS.has(key) ? "onb.step" : "";
    return value.map((v) => (prefix ? codeText(lang, prefix, String(v)) : String(v))).join(", ");
  }
  return String(value);
}

/** The rows of the card: the known keys in the API's order, then any key this web does not know yet (never hidden). */
export function settingRows(lang: Lang, settings: Record<string, unknown>, locale: string): { key: string; label: string; value: string }[] {
  const known = SETTING_KEYS.filter((k) => k in settings);
  const extra = Object.keys(settings).filter((k) => !isKnown(k));
  return [...known, ...extra].map((key) => ({ key, label: settingLabel(lang, key), value: settingValue(lang, key, settings[key], locale) }));
}
