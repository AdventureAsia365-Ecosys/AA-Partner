// web/lib/lifecycle.ts
// AA-942 (card CRM-14) — Settings > Data and privacy: the shapes of GET /crm/v1/lifecycle and GET /crm/v1/trash, and
// the pure helpers the page uses to word them (the API returns codes and numbers only; EN default, VI optional).

import { fmt, t, type Lang, type MessageKey } from "./i18n";

export type RuleWindow = "until_deleted" | "days" | "months" | "with_parent" | "setting";

export interface Rule {
  code: string;
  window: RuleWindow;
  amount: number | null;
  mechanism: string;
  enforced_by: string;
  setting: "trash_days" | "auto_archive_months" | null;
}

export interface LifecycleSettings {
  trash_days: number;
  auto_archive_months: number;
}

export interface Hold {
  hold_id: string;
  scope: "tenant" | "trip" | "traveler";
  subject_id: string | null;
  reason_code: string;
  status: "active" | "released";
  placed_by_aa: boolean;
  created_at: string;
  released_at: string | null;
}

export interface LifecycleRun {
  kind: string;
  dry_run: boolean;
  counts: Record<string, number>;
  at: string;
}

export interface Offboarding {
  request_id: string;
  status: "requested" | "export_ready" | "erasing" | "erased" | "cancelled" | "failed";
  dry_run_counts: Record<string, number>;
  export_waived: boolean;
  export_ready: boolean;
  export_expires_at: string | null;
  export_downloaded_at: string | null;
  executed_at: string | null;
  backup_purge_by: string | null;
  failure_class: string | null;
  created_at: string;
}

export interface LifecycleView {
  rules: Rule[];
  settings: LifecycleSettings;
  lost_reasons: string[];
  archive_reasons: string[];
  hold_reasons: string[];
  deletion_dry_run: boolean;
  backup_purge_days: number;
  role: "owner" | "agent" | "assistant";
  holds?: Hold[];
  runs?: LifecycleRun[];
  archived_inactive?: { traveler_id: string; display_name: string; archived_at: string }[];
  offboarding?: Offboarding | null;
}

export type TrashKind = "trip" | "traveler" | "task" | "note" | "document";

export interface TrashItem {
  kind: TrashKind;
  id: string;
  label: string | null;
  deleted_at: string;
  deleted_by: string | null;
  purge_after: string;
}

export const OPEN_OFFBOARDING = ["requested", "export_ready", "erasing", "failed"] as const;

/** "How long" for one rule, in the page's language; the agency's own value for a setting rule. */
export function ruleWindow(rule: Rule, settings: LifecycleSettings, lang: Lang): string {
  if (rule.window === "setting" && rule.setting) {
    return fmt(t(lang, `data.window.${rule.setting}` as MessageKey), { n: settings[rule.setting] });
  }
  if (rule.window === "days" || rule.window === "months") {
    return fmt(t(lang, `data.window.${rule.window}` as MessageKey), { n: rule.amount ?? 0 });
  }
  return t(lang, `data.window.${rule.window}` as MessageKey);
}

/** Total rows a dry run would remove (the counts of every table, the document objects aside). */
export function totalCount(counts: Record<string, number>): number {
  return Object.entries(counts)
    .filter(([k]) => k !== "document_object" && !k.endsWith("_unlinked"))
    .reduce((sum, [, n]) => sum + (Number.isFinite(n) ? n : 0), 0);
}

/** The typed confirmation of an agency erase: the agency id, exactly (spaces trimmed, case ignored). */
export function confirmsErase(typed: string, tenantId: string): boolean {
  return typed.trim().toLowerCase() === tenantId.toLowerCase() && tenantId.length > 0;
}

/**
 * Which offboarding step the owner can take next. A failed request means the database refused the erase and deleted
 * nothing; the agency is active again (the owner can see this page), so the owner reads the code and cancels.
 */
export function offboardingStep(
  o: Offboarding | null | undefined,
): "request" | "wait_export" | "confirm" | "running" | "failed" | "done" {
  if (!o || o.status === "cancelled") return "request";
  if (o.status === "erased") return "done";
  if (o.status === "erasing") return "running";
  if (o.status === "failed") return "failed";
  if (o.status === "requested" && !o.export_waived) return "wait_export";
  return "confirm";
}

/** The 409 detail of a refused trip delete -> the message key the trip page shows (with "Cancel trip" instead). */
export function deleteRefusal(detail: string): MessageKey {
  return ["has_money", "has_signature", "has_commission", "has_aa_line"].includes(detail)
    ? (`trip.blocked.${detail}` as MessageKey)
    : "trip.error";
}
