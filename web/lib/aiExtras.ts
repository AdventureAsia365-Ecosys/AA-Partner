// web/lib/aiExtras.ts
// AA-965 (card CRM-33b) — the shapes and small pure rules of the three AI features beside the reply draft:
// R10 "AI reads a supplier confirmation" (api/app/llm/supplier_confirm.py: POST trips/{id}/ai/supplier-confirmation,
// POST ai/drafts/{id}/apply), R40 "AI ideas from notes" (api/app/llm/notes_options.py: POST proposal-days/{id}/ai/options)
// and R41 voice profiles (api/app/llm/voices.py: GET / POST ai/voices, PATCH / DELETE ai/voices/{id}).

import type { Line, Supplier } from "./ledger";

/** Root ADR 0019 b6: the non-AA ancillary suppliers AI may read (flights, insurance, visas, transfers, fees). */
export const ANCILLARY_CATEGORIES = ["flight", "insurance", "visa", "transport", "other"] as const;

export interface GatewayOutcome {
  outcome: string;
  fallback: string | null;
  refusal_code: string | null;
  error_class: string | null;
  llm_call_id: string | null;
  budget_used_pct: number | null;
  budget_alert: 75 | 90 | 100 | null;
  truncated: boolean;
  approval_required: true;
}

export type ConfirmStatus = "confirmed" | "declined" | "unclear";

export interface ProposedLine {
  code: string;
  booking_line_id: string;
  title: string | null;
  status: ConfirmStatus;
  confirmation_no: string | null;
  service_from: string | null;
  service_to: string | null;
  times: string | null;
}

export interface SupplierConfirmResult extends GatewayOutcome {
  ai_draft_id: string | null;
  agency_supplier_id: string;
  lines: ProposedLine[];
  rejected: number;
  reasons: Record<string, number>;
  source: "message" | "text";
}

export type ApplyAction = "confirm" | "back_to_planning";

export interface ApplyLine {
  booking_line_id: string;
  action: ApplyAction;
  confirmation_no?: string;
}

export interface OptionIdea {
  code: string;
  tour_source_id: string;
  title: string;
  component_id: string;
  component_kind: string;
  tour_day: number | null;
  why: string | null;
}

export interface NotesOptionsResult extends GatewayOutcome {
  ai_draft_id: string | null;
  proposal_day_id: string;
  proposal_id: string;
  options: OptionIdea[];
  closed_list_size: number;
  rejected: number;
  reasons: Record<string, number>;
}

export type Descriptors = Record<string, string>;

export interface Voice {
  voice_profile_id: string;
  member_id: string;
  mine: boolean;
  can_edit: boolean;
  name: string;
  locale: "en" | "vi";
  channel: "email" | "whatsapp";
  descriptors: Descriptors;
  created_at: string;
  updated_at: string;
}

export interface VoicesView {
  voices: Voice[];
  per_member_max: number;
  mine: number;
  descriptors: Record<string, string[]>;
}

/** The suppliers whose confirmation AI may read on this trip: own ancillary suppliers with an open agency line. */
export function readableSuppliers(lines: Line[], suppliers: Supplier[]): Supplier[] {
  const open = new Set(
    lines
      .filter((l) => l.supplier_kind === "agency" && (l.status === "planning" || l.status === "requested"))
      .map((l) => l.agency_supplier_id),
  );
  return suppliers.filter(
    (s) => s.kind === "own" && open.has(s.agency_supplier_id) && (ANCILLARY_CATEGORIES as readonly string[]).includes(s.category),
  );
}

/** The default action of a proposed line: confirm what the supplier confirmed, back to planning what it declined. */
export function defaultAction(line: Pick<ProposedLine, "status">): ApplyAction | null {
  if (line.status === "confirmed") return "confirm";
  if (line.status === "declined") return "back_to_planning";
  return null;
}

/** The apply body from the agent's picks (only picked lines; a reference only on a confirm, trimmed). */
export function applyBody(
  picks: { line: ProposedLine; action: ApplyAction | null; reference: string }[],
): ApplyLine[] {
  return picks
    .filter((p) => p.action !== null)
    .map((p) => {
      const out: ApplyLine = { booking_line_id: p.line.booking_line_id, action: p.action as ApplyAction };
      const ref = p.reference.trim();
      if (p.action === "confirm" && ref) out.confirmation_no = ref.slice(0, 80);
      return out;
    });
}

const REFUSALS = new Set([
  "feature_not_allowed",
  "ai_disabled",
  "supplier_not_ancillary",
  "aa_line_read_only",
  "no_open_lines",
  "too_many_lines",
  "text_too_long",
  "source_required",
  "message_not_found",
  "notes_required",
  "no_aa_products",
  "booking_unavailable",
  "booking_not_configured",
  "aa_partner_not_active",
  "draft_decided",
  "line_not_in_proposal",
  "line_changed",
  "line_not_editable",
  "assigned_only",
  "role_forbidden",
  "voice_limit_reached",
  "not_your_voice",
  "invalid_descriptors",
  "invalid_name",
]);

/** The message key of an API refusal in the three panels. */
export function aiExtraErrorKey(detail: string): string {
  return REFUSALS.has(detail) ? `aiX.error.${detail}` : "aiX.error.generic";
}

/** A run the gateway did not answer (refused, error, cut): the reason key, "do it by hand". */
export function aiExtraFallbackKey(result: Pick<GatewayOutcome, "outcome" | "refusal_code" | "truncated">): string {
  if (result.truncated) return "aiX.fallback.truncated";
  if (["budget_exhausted", "spend_unreadable", "app_budget_exhausted"].includes(result.refusal_code ?? "")) {
    return "aiX.fallback.budget";
  }
  if (result.refusal_code === "stage_disabled" || result.refusal_code === "app_disabled") return "aiX.fallback.paused";
  if (result.refusal_code === "rate_limited") return "aiX.fallback.rate_limited";
  return "aiX.fallback.generic";
}

/** The voices offered in the draft's picker: the agency's voices, the member's own first (the API's order). */
export function voiceOptions(view: VoicesView | null): { value: string; label: string }[] {
  return (view?.voices ?? []).map((v) => ({ value: v.voice_profile_id, label: `${v.name} (${v.locale.toUpperCase()}, ${v.channel})` }));
}

/** Whether the member may add one more voice (the per-member setting the API reports). */
export function canAddVoice(view: VoicesView | null): boolean {
  return !!view && view.mine < view.per_member_max;
}
