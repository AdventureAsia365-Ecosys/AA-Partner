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
  // AA-941 (crm-011): an agency deletion ("offboard") or an export only, raised by the owner or by AA; an AA-raised
  // request waits for the owner's Confirm / Decline. Optional: an API without crm-011 code omits them.
  purpose?: "offboard" | "export_only";
  raised_by?: "aa" | "owner";
  awaiting_owner?: boolean;
  dry_run_counts: Record<string, number>;
  export_waived: boolean;
  export_ready: boolean;
  export_expires_at: string | null;
  export_downloaded_at: string | null;
  executed_at: string | null;
  backup_purge_by: string | null;
  failure_class: string | null;
  created_at: string;
  // AA-967 (crm-026): a confirmed deletion waits for its cool-off (erase_not_before; the owner may still cancel);
  // verified_at = the post-erase verification passed. Optional: an API before AA-967 omits them.
  confirmed?: boolean;
  erase_not_before?: string | null;
  verified_at?: string | null;
}

export interface LifecycleView {
  rules: Rule[];
  settings: LifecycleSettings;
  lost_reasons: string[];
  archive_reasons: string[];
  hold_reasons: string[];
  deletion_dry_run: boolean;
  backup_purge_days: number;
  offboarding_cool_off_hours?: number; // AA-967
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
): "request" | "aa_request" | "wait_export" | "confirm" | "scheduled" | "export_ready" | "running" | "failed" | "done" {
  if (!o || o.status === "cancelled") return "request";
  if (o.awaiting_owner) return "aa_request";
  if (o.purpose === "export_only") {
    // An export AA raised and the owner confirmed: download it; it never erases (close it with Cancel).
    if (o.status === "requested") return "wait_export";
    if (o.status === "failed") return "failed";
    return "export_ready";
  }
  if (o.status === "erased") return "done";
  // AA-967: confirmed, waiting for the cool-off to end (the agency works as usual; Cancel stays open).
  if ((o.status === "requested" || o.status === "export_ready") && o.erase_not_before) return "scheduled";
  if (o.status === "erasing") return "running";
  if (o.status === "failed") return "failed";
  if (o.status === "requested" && !o.export_waived) return "wait_export";
  return "confirm";
}

/** AA-967: the dry-run report of an agency deletion: the tables with rows, largest first (document objects last). */
export function deletionReport(counts: Record<string, number>, limit = 12): { table: string; n: number }[] {
  return Object.entries(counts)
    .filter(([k, n]) => n > 0 && !k.endsWith("_unlinked") && !k.endsWith("_kept"))
    .map(([table, n]) => ({ table, n }))
    .sort((a, b) => b.n - a.n || a.table.localeCompare(b.table))
    .slice(0, limit);
}

/** AA-941: the heading of an AA-raised request the owner must answer. */
export function aaRequestKey(o: Offboarding): MessageKey {
  return o.purpose === "export_only" ? "data.aa.export" : "data.aa.deletion";
}

/** The 409 detail of a refused trip delete -> the message key the trip page shows (a blocker: "Cancel trip" instead;
 * a legal hold: wait for its release). */
export function deleteRefusal(detail: string): MessageKey {
  if (detail === "cancellation_pending") return "move.error.cancellation_pending"; // S262 round 3: AA has not confirmed
  return ["has_money", "has_signature", "has_commission", "has_aa_line", "legal_hold"].includes(detail)
    ? (`trip.blocked.${detail}` as MessageKey)
    : "trip.error";
}
