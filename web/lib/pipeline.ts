// web/lib/pipeline.ts
// AA-936 — the board (A-02) and trip workspace (A-03) types the CRM API returns (/crm/v1/pipeline, /trips/{id}),
// and the pure helpers the pages use: stage order, the moves Slice 1 opens, short names for cards, "reply in 2 h",
// party and month labels. No personal data is formatted here beyond the display name the API already decrypted
// for this page view (one list_view audit event per board load, design v3 §4.4). Edge-safe, unit-tested.
// AA-943 (CRM-15): every crm-012 edge, the gates and their override reasons, the cancel reasons, the Closed group,
// the quiet flag, Stage 2 completeness and the answers of the stage-engine routes (moves, Undo, bulk, cancel).

import { formatDate, type DateLocale } from "./dates";
import { fmt, plural, type Lang, type MessageKey, t } from "./i18n";
import type { PlanPage } from "./planningForm";

export const BOARD_STAGES = ["inquiry", "qualifying", "proposal", "booked", "pre_departure", "travelling"] as const;
export type BoardStage = (typeof BOARD_STAGES)[number];
export type Stage = BoardStage | "completed" | "lost" | "cancelled";
export const WAITING_ON = ["agency", "client", "aa", "supplier", "none"] as const;
export type WaitingOn = (typeof WAITING_ON)[number];
export const LOST_REASONS = [
  "price",
  "price_aa_tour",
  "aa_sold_out",
  "aa_on_request_declined",
  "dates_changed",
  "went_elsewhere",
  "no_response",
  "postponed",
  "other",
] as const;
export type LostReason = (typeof LOST_REASONS)[number];

export const CLOSED_STAGES = ["completed", "lost", "cancelled"] as const;
export type ClosedStage = (typeof CLOSED_STAGES)[number];

/** Every edge of crm-012 a move may take (mirrors API app/stages.py ALLOWED_MOVES; the database stays the judge).
 * `cancelled` is reached only through the cancel flow (dry run first), never by a move or a drop. */
export const MOVES: Record<Stage, readonly Stage[]> = {
  inquiry: ["qualifying", "lost"],
  qualifying: ["inquiry", "proposal", "lost"],
  proposal: ["qualifying", "booked", "lost"],
  booked: ["pre_departure", "travelling"],
  pre_departure: ["travelling"],
  travelling: ["completed"],
  completed: [],
  lost: ["inquiry"],
  cancelled: [],
};

export function canMove(from: Stage, to: Stage): boolean {
  return (MOVES[from] ?? []).includes(to);
}

/** The gated edges (crm-012): Stage 2 >= 80 % or an override; booking / Stage 3 by override only until CRM-18 / 25. */
export type Gate = "stage2" | "booking" | "stage3";
export const GATES: Partial<Record<`${Stage}>${Stage}`, Gate>> = {
  "qualifying>proposal": "stage2",
  "proposal>booked": "booking",
  "booked>pre_departure": "stage3",
};
export const OVERRIDE_REASONS: Record<Gate, readonly string[]> = {
  stage2: ["client_in_hurry", "repeat_client", "info_by_phone", "other"],
  booking: ["manual_booking"],
  stage3: ["manual_stage3"],
};
export function gateOf(from: Stage, to: Stage): Gate | null {
  return GATES[`${from}>${to}`] ?? null;
}

export const CANCEL_REASONS = [
  "client_request",
  "client_illness",
  "travel_advisory",
  "force_majeure",
  "supplier_failure",
  "aa_cancelled",
  "payment_not_received",
  "duplicate",
  "other",
] as const;
export type CancelReason = (typeof CANCEL_REASONS)[number];
export const CANCELLABLE: readonly Stage[] = ["booked", "pre_departure", "travelling"];
/** The Stage 2 percentage the Proposal gate needs (API app/intake/completeness.py STAGE2_GATE). */
export const STAGE2_GATE = 80;
/** How long a move, a bulk move or a send can be taken back (API UNDO_SECONDS; the DB allows 15 s). */
export const UNDO_MS = 10_000;

export interface Card {
  trip_id: string;
  title: string;
  stage: BoardStage;
  waiting_on: WaitingOn;
  owner_member_id: string | null;
  is_mine: boolean;
  lead_name: string | null;
  companions: number;
  party_adults: number;
  children: number;
  month: string | null;
  start_date: string | null;
  nights: number | null;
  budget_band: string | null;
  lead_source: string | null;
  next_due_at: string | null;
  submissions: number;
  last_inquiry_at: string;
  stage_entered_at: string;
  // AA-943: optional so a board from before the crm-012 API still renders.
  last_activity_at?: string | null;
  quiet_days?: number;
  quiet?: boolean;
  /** Stage 2 % on Inquiry / Qualifying cards, else null. */
  stage2_percent?: number | null;
  /** AA-957 (C12): cancelled in the CRM, AA has not confirmed the cancellation of its bookings yet. */
  cancellation_pending?: boolean;
}

export interface ClosedCard extends Omit<Card, "stage"> {
  stage: ClosedStage;
}

export interface Board {
  stages: { stage: BoardStage; count: number; cards: Card[]; truncated: boolean }[];
  /** AA-943: completed / lost / cancelled, newest activity first. */
  closed?: { count: number; counts: Record<ClosedStage, number>; cards: ClosedCard[] };
  waiting_on_counts: Record<WaitingOn, number>;
  total: number;
  filter: { waiting_on: WaitingOn | null; owner: "anyone" | "me" };
}

export interface Trip {
  trip_id: string;
  title: string;
  stage: Stage;
  stage_entered_at: string;
  waiting_on: WaitingOn;
  start_date: string | null;
  end_date: string | null;
  month: string | null;
  date_flex_days: number;
  nights: number | null;
  destinations: string[];
  party_adults: number;
  party_child_ages: number[];
  party_type: string | null;
  budget_band: string | null;
  trip_style: string | null;
  owner_member_id: string | null;
  is_mine: boolean;
  lead_source: string | null;
  lost_reason_code: LostReason | null;
  last_inquiry_at: string;
  created_at: string;
  // AA-943 (crm-012)
  intake_completeness?: Partial<Record<"s1" | "s2" | "s3", number>>;
  cancel_reason_code?: CancelReason | null;
  last_activity_at?: string | null;
  quiet_days?: number;
  quiet?: boolean;
  cancellation_pending?: boolean; // AA-957
}

export interface Stage2Link {
  link_id: string;
  status: "sent" | "opened" | "submitted" | "expired" | "revoked";
  sent_at: string;
  expires_at: string;
  opened_at: string | null;
  submitted_at: string | null;
}

export interface HistoryRow {
  from_stage: Stage | null;
  to_stage: Stage;
  reason_code: string;
  at: string;
  actor: string;
  history_id?: string;
  override_reason_code?: string | null;
  undo_of?: string | null;
  batch_id?: string | null;
}

export interface SupplierChecklistItem {
  agency_supplier_id: string;
  supplier_name: string;
  lines: { booking_line_id: string; title: string; confirmation_no: string | null }[];
}

/** One checklist line per supplier: "Riverside Lodge: Lodge 2 nights (RL-7), Boat day". */
export function supplierChecklistLine(item: SupplierChecklistItem): string {
  const lines = item.lines.map((l) => (l.confirmation_no ? `${l.title} (${l.confirmation_no})` : l.title));
  return `${item.supplier_name}: ${lines.join(", ")}`;
}

export interface TripView {
  trip: Trip;
  party: { traveler_id: string; role: "lead" | "companion"; display_name: string; preferred_channel: string | null; has_email: boolean; has_phone: boolean }[];
  tasks: {
    task_id: string;
    kind: string;
    /** AA-959: a custom task's own title (a member's own task, a workflow step's); null for a coded kind. */
    title?: string | null;
    priority?: "normal" | "urgent";
    assignee_member_id: string | null;
    status: "open" | "done" | "skipped";
    due_at: string | null;
    done_at: string | null;
    /** AA-957: on the open cancel_with_suppliers task, one line per supplier with its confirmed lines to cancel. */
    checklist?: SupplierChecklistItem[];
  }[];
  notes: { note_id: string; kind: string; body: string; created_at: string; created_by: string }[];
  history: HistoryRow[];
  submissions: { submission_id: string; outcome: "created" | "attached" | "applied" | null; status: string; created_at: string; answers: Record<string, unknown> | null }[];
  allowed_moves: Stage[];
  /** AA-943: owner / agent may cancel (the API checks the role too). */
  can_cancel?: boolean;
  stage2_link?: Stage2Link | null;
  /** AA-957 (C12): the trip's cancellation, pending while AA cancels its bookings. */
  cancellation?: Cancellation | null;
}

/** S262 (C12 §2.7): a trip that is cancelled, or whose cancellation waits for AA, bills the client no more: its
 * Proposal and Invoices cards turn read-only (the API answers 409 trip_cancelled anyway). */
export function tripClosedForBilling(stage: string, cancellation: { status: string } | null | undefined): boolean {
  return stage === "cancelled" || Boolean(cancellation);
}

/** The Booking details row of Traveller info (AA-957 S262 round 3: a booked trip read "Opens once the trip is booked."):
 * not needed on a cancelled trip; sent by the travellers from their own page once the trip is booked; later before;
 * kept as sent once the trip is over (a completed trip asks nothing more). */
export function bookingDetailsKey(stage: string, closed: boolean): MessageKey {
  if (closed || stage === "cancelled") return "info.bookingClosed";
  if (stage === "completed") return "info.bookingDone";
  if (stage === "booked" || stage === "pre_departure" || stage === "travelling") return "info.bookingOpen";
  return "info.bookingLocked";
}

/** Booking's fee percentage without trailing zeros, in the page locale (the same as `formatAmount` beside it):
 * "75.00" -> "75", "12.50" -> "12.5" (vi-VN "12,5"). */
export function percentText(pct: string | number | null | undefined, locale?: string): string {
  if (pct === null || pct === undefined || pct === "") return "";
  const n = Number(pct);
  if (!Number.isFinite(n)) return String(pct);
  return new Intl.NumberFormat(locale, { minimumFractionDigits: 0, maximumFractionDigits: 2 }).format(n);
}

export interface Cancellation {
  status: "pending" | "done";
  reason_code: CancelReason;
  from_stage: Stage;
  aa_bookings: number;
  requested_at: string;
  finished_at: string | null;
}

/** GET /trips/{id}/stage2: the planning form's pages, completeness with the missing codes, and the F2 answers. */
export interface Completeness {
  s1: number;
  s2: number;
  s3: number;
  missing_s1: string[];
  missing_s2: string[];
  stage2_gate: number;
}

export interface Stage2View {
  schema_key: string;
  version: number;
  pages: PlanPage[];
  completeness: Completeness;
  answers: Record<string, unknown>;
}

/** Every move / undo answers with this (POST /trips/{id}/stage, /stage/undo). */
export interface UndoToken {
  history_id: string;
  undo_until: string;
}

export interface MoveResult {
  trip_id: string;
  from_stage?: Stage;
  stage: Stage;
  waiting_on: WaitingOn;
  allowed_moves: Stage[];
  override_reason_code?: string | null;
  undo?: UndoToken;
  stage2_sent?: SendResult | null;
  stage2_skipped?: string;
}

export interface SendResult {
  link_id: string;
  link_path: string;
  expires_at: string;
  dispatch_id: string;
  send_after: string;
  reissued_from: string | null;
}

export interface BulkPreview {
  to: Stage;
  eligible: string[];
  blocked: { trip_id: string; code: string }[];
  preview_hash: string;
  moved?: number;
  batch_id?: string;
  undo?: UndoToken;
}

export interface CancelPreview {
  trip_id: string;
  from_stage: Stage;
  cancel_reason_code: CancelReason;
  dry_run: boolean;
  effects: { kind: string; count: number }[];
  stage?: Stage;
  // AA-957 (C12): the detail blocks of the dry run.
  aa_bookings?: CancelBooking[];
  supplier_lines?: { booking_line_id: string; title: string; supplier_name: string; confirmation_no: string | null }[];
  payments?: CancelPayment[];
  commissions?: { commission_id: string; status: "expected" | "claimed"; title: string; supplier_name: string; expected: { amount: string; currency: string } | null }[];
  terms?: { version_label: string; accepted_at: string } | null;
  booking_unavailable?: boolean;
  cancellation?: Cancellation | null;
  requeued?: number;
}

export interface CancelBooking {
  booking_id: string;
  titles: string[];
  asked: boolean;
  failed_code: string | null;
  fee?: { amount: string; currency: string; fee_pct: string; days_before_departure: number } | null;
  fee_code?: string;
}

export interface CancelPayment {
  payment_id: string;
  invoice_id: string | null;
  invoice_number: string | null;
  amount: string;
  currency: string;
  received_on: string;
  refundable: string;
  refund_amount: string;
  needs_invoice: boolean;
}

/** A 409 of the stage engine that a dialog can answer: the gate and the override reasons the API offers. */
export interface GateRefusal {
  detail: "stage2_incomplete" | "gate_unmet";
  gate: Gate;
  s2?: number;
  missing: string[];
  override_reasons: string[];
}

/** Reads a refused move's body: a gate the member may override, or null for any other refusal. */
export function gateRefusal(body: unknown, from: Stage, to: Stage): GateRefusal | null {
  if (!body || typeof body !== "object") return null;
  const b = body as Record<string, unknown>;
  if (b.detail !== "stage2_incomplete" && b.detail !== "gate_unmet") return null;
  const gate = (typeof b.gate === "string" ? b.gate : gateOf(from, to)) as Gate | null;
  if (!gate || !(gate in OVERRIDE_REASONS)) return null;
  const offered = Array.isArray(b.override_reasons) ? b.override_reasons.filter((r): r is string => typeof r === "string") : [];
  return {
    detail: b.detail,
    gate,
    s2: typeof b.s2 === "number" ? b.s2 : undefined,
    missing: Array.isArray(b.missing) ? b.missing.filter((m): m is string => typeof m === "string") : [],
    // Only reasons this web knows how to word; the API's list wins when it narrows ours.
    override_reasons: (offered.length ? offered : [...OVERRIDE_REASONS[gate]]).filter((r) => OVERRIDE_REASONS[gate].includes(r)),
  };
}

export interface Member {
  member_id: string;
  role: "owner" | "agent" | "assistant";
  status: string;
  is_me: boolean;
}

/** "Anna Mai" -> "Anna M."; "Nguyễn Thị Hoa" -> "Nguyễn Thị H." (cards never show more than the first name in full). */
export function shortName(name: string | null): string {
  if (!name) return "—";
  const parts = name.trim().split(/\s+/);
  if (parts.length < 2) return parts[0] ?? "—";
  const last = parts[parts.length - 1];
  return `${parts.slice(0, -1).join(" ")} ${Array.from(last)[0]}.`;
}

/** "Anna M. +2" */
export function partyLabel(card: Pick<Card, "lead_name" | "companions">): string {
  const lead = shortName(card.lead_name);
  return card.companions > 0 ? `${lead} +${card.companions}` : lead;
}

/** Month (1st of month) or start date as "Mar 2027" / "tháng 3, 2027" (month spelled, R32). */
export function whenLabel(card: Pick<Card, "month" | "start_date">, locale: DateLocale): string | null {
  const value = card.start_date ?? card.month;
  if (!value) return null;
  const date = new Date(`${value}T00:00:00Z`);
  if (card.start_date) return formatDate(card.start_date, locale, { year: true });
  return new Intl.DateTimeFormat(locale, { month: "short", year: "numeric", timeZone: "UTC" }).format(date);
}

/** "2 h", "40 min", or null when overdue. */
export function dueIn(dueAt: string | null, now: number = Date.now()): { label: string; overdue: boolean } | null {
  if (!dueAt) return null;
  const ms = new Date(dueAt).getTime() - now;
  if (Number.isNaN(ms)) return null;
  if (ms <= 0) return { label: "", overdue: true };
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return { label: `${Math.max(minutes, 1)} min`, overdue: false };
  const hours = Math.round(minutes / 60);
  if (hours < 48) return { label: `${hours} h`, overdue: false };
  return { label: `${Math.round(hours / 24)} d`, overdue: false };
}

/** The board's move refusal message: a known code worded, anything else the generic one. */
export function moveErrorKey(detail: string): MessageKey {
  const known = ["transition_not_allowed", "use_cancel_flow", "undo_expired", "already_in_stage", "preview_stale", "too_many_trips", "nothing_to_move", "lost_reason_required", "cancellation_pending", "invalid_refunds", "invalid_keep_commission"];
  return (known.includes(detail) ? `move.error.${detail}` : "trip.error") as MessageKey;
}

/** "Quiet 4 d" on cards not touched for more than 3 days (crm-012 last_activity_at; the API sets `quiet`). */
export function quietDays(card: Pick<Card, "quiet" | "quiet_days">): number | null {
  return card.quiet && typeof card.quiet_days === "number" ? card.quiet_days : null;
}

/** The trip ids a column's bulk move covers: the whole column when no filter is set (the API reads the stage, at most
 * 200 trips), otherwise only the cards on screen (a filtered board must not move trips the member cannot see). */
export function bulkScope(
  column: { stage: BoardStage; cards: Pick<Card, "trip_id">[] },
  filter: Board["filter"],
): { from_stage: BoardStage } | { trip_ids: string[] } {
  if (!filter.waiting_on && filter.owner === "anyone") return { from_stage: column.stage };
  return { trip_ids: column.cards.map((c) => c.trip_id) };
}

export function waitingLabel(lang: Lang, waiting: WaitingOn): string {
  return t(lang, `waiting.${waiting}` as MessageKey);
}

export function stageLabel(lang: Lang, stage: Stage): string {
  return t(lang, `stage.${stage}` as MessageKey);
}

export function codeLabel(lang: Lang, prefix: string, code: string | null | undefined): string {
  if (!code) return "—";
  const key = `${prefix}.${code}` as MessageKey;
  const text = t(lang, key);
  return text ?? code;
}

/** "2 adults, children aged 7, 12" */
export function partySummary(lang: Lang, adults: number, childAges: number[]): string {
  const head = plural(lang, "trip.adults", adults);
  return childAges.length ? `${head}, ${fmt(t(lang, "trip.children"), { ages: childAges.join(", ") })}` : head;
}

/** The board URL for a filter (chips and owner toggle are links: no client state needed). */
export function boardHref(filter: { waiting_on?: WaitingOn | null; owner?: "anyone" | "me" }): string {
  const q = new URLSearchParams();
  if (filter.waiting_on) q.set("waiting_on", filter.waiting_on);
  if (filter.owner === "me") q.set("owner", "me");
  const s = q.toString();
  return s ? `/pipeline?${s}` : "/pipeline";
}

export function parseFilter(params: Record<string, string | string[] | undefined>): {
  waiting_on: WaitingOn | null;
  owner: "anyone" | "me";
} {
  const w = typeof params.waiting_on === "string" ? params.waiting_on : "";
  return {
    waiting_on: (WAITING_ON as readonly string[]).includes(w) ? (w as WaitingOn) : null,
    owner: params.owner === "me" ? "me" : "anyone",
  };
}

/** A member in the assign menu: "Me", or the role and a short id (names arrive with the team page, CRM-11). */
export function memberLabel(lang: Lang, m: Member): string {
  if (m.is_me) return t(lang, "trip.me");
  return fmt(t(lang, "trip.member"), { role: t(lang, `role.${m.role}` as MessageKey), id: m.member_id.slice(0, 4) });
}
