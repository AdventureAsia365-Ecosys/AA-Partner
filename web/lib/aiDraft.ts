// web/lib/aiDraft.ts
// AA-1027 slice F3 (gap CRM-4; design v3 M14, R39 "AI drafts, humans send", A-21) — the shapes of the AI draft API
// (api/app/llm/agency_ai.py POST /crm/v1/trips/{id}/ai/draft; api/app/llm/ai_drafts.py POST
// /crm/v1/ai/drafts/{id}/accept|discard) and the small pure rules of the trip workspace's Draft action: whether it is
// available (the plan's ai_assist, then the AI switch), the wording key of an API refusal and of a draft the gateway
// could not write.

import type { AiSettings } from "./ai";

export type DraftKind = "reply" | "follow_up";
export const DRAFT_KINDS: readonly DraftKind[] = ["reply", "follow_up"];

/** The API's limits (agency_ai.MAX_TEXT / MAX_INSTRUCTION). */
export const MAX_TEXT = 8000;
export const MAX_INSTRUCTION = 500;

/** R39: the member's optional reason with a decision (ai_drafts.FEEDBACK_CODES, crm-040 CHECK). */
export const FEEDBACK_CODES = ["good", "wrong_facts", "wrong_tone", "not_relevant", "too_long", "other"] as const;
export type FeedbackCode = (typeof FEEDBACK_CODES)[number];

export interface DraftResult {
  ai_draft_id: string | null;
  outcome: string;
  text: string | null;
  fallback: string | null;
  refusal_code: string | null;
  error_class: string | null;
  llm_call_id: string | null;
  budget_used_pct: number | null;
  budget_alert: 75 | 90 | 100 | null;
  truncated: boolean;
  approval_required: true;
  drafted_at: string;
  /** AA-1040: the machine-readable mark of the model's text (null when there is no text). */
  ai?: { ai_generated: true; generator: string; generated_at: string } | null;
}

export interface DraftDecision {
  ai_draft_id: string;
  trip_id: string;
  decision: "accepted" | "discarded";
  feedback_code: FeedbackCode | null;
  decided_at: string;
}

/**
 * Whether the Draft action works, and why not: the plan comes first (403 feature_not_allowed from the API), then the
 * AI switch (409 ai_disabled), then a settings read that failed. An older API without `ai_assist_allowed` counts as
 * allowed: the draft call itself still answers 403 and the panel shows that refusal.
 */
export type DraftAvailability = "ready" | "no_plan" | "off" | "unknown";

export function draftAvailability(settings: AiSettings | null): DraftAvailability {
  if (!settings) return "unknown";
  if (settings.ai_assist_allowed === false) return "no_plan";
  if (!settings.ai_enabled) return "off";
  return "ready";
}

/** The message key under a disabled Draft action (the owner gets the Settings > AI hint, the others ask the owner). */
export function unavailableKey(state: Exclude<DraftAvailability, "ready">, isOwner: boolean): string {
  if (state === "off") return isOwner ? "aiDraft.off.owner" : "aiDraft.off.member";
  return `aiDraft.${state === "no_plan" ? "noPlan" : "unknown"}`;
}

const REFUSALS = new Set([
  "feature_not_allowed",
  "ai_disabled",
  "rate_limited",
  "draft_decided",
  "assigned_only",
  "role_forbidden",
  "trip_not_found",
  "text_required",
]);

/** The message key of an API refusal in the panel. */
export function draftErrorKey(detail: string): string {
  return REFUSALS.has(detail) ? `aiDraft.error.${detail}` : "aiDraft.error.generic";
}

/** A draft the gateway did not write (refused, error, cut at its token limit): the reason key, "write it by hand". */
export function fallbackKey(result: Pick<DraftResult, "outcome" | "refusal_code" | "truncated">): string {
  if (result.truncated) return "aiDraft.fallback.truncated";
  // AA-1025 H1c: the CRM app profile's refusals (AI off for the whole CRM, its daily budget used up) read the same.
  if (["budget_exhausted", "spend_unreadable", "app_budget_exhausted"].includes(result.refusal_code ?? "")) {
    return "aiDraft.fallback.budget";
  }
  if (result.refusal_code === "stage_disabled" || result.refusal_code === "app_disabled") return "aiDraft.fallback.paused";
  if (result.refusal_code === "rate_limited") return "aiDraft.fallback.rate_limited";
  return "aiDraft.fallback.generic";
}

/** The form check the API repeats: the client's message is required, both texts within the API's limits. */
export function draftInputError(text: string, instruction: string): string | null {
  if (!text.trim()) return "aiDraft.error.text_required";
  if (text.length > MAX_TEXT) return "aiDraft.error.text_long";
  if (instruction.length > MAX_INSTRUCTION) return "aiDraft.error.instruction_long";
  return null;
}
