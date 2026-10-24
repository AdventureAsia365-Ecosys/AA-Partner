// web/lib/pipeline.ts
// AA-936 — the board (A-02) and trip workspace (A-03) types the CRM API returns (/crm/v1/pipeline, /trips/{id}),
// and the pure helpers the pages use: stage order, the moves Slice 1 opens, short names for cards, "reply in 2 h",
// party and month labels. No personal data is formatted here beyond the display name the API already decrypted
// for this page view (one list_view audit event per board load, design v3 §4.4). Edge-safe, unit-tested.

import { formatDate, type DateLocale } from "./dates";
import { fmt, type Lang, type MessageKey, t } from "./i18n";

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

/** The moves Slice 1 opens (mirrors crm-005; the database stays the judge, CRM-15 opens the rest). */
export const MOVES: Partial<Record<Stage, readonly Stage[]>> = {
  inquiry: ["qualifying", "lost"],
  qualifying: ["inquiry", "lost"],
  lost: ["inquiry"],
};

export function canMove(from: Stage, to: Stage): boolean {
  return (MOVES[from] ?? []).includes(to);
}

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
}

export interface Board {
  stages: { stage: BoardStage; count: number; cards: Card[]; truncated: boolean }[];
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
}

export interface TripView {
  trip: Trip;
  party: { traveler_id: string; role: "lead" | "companion"; display_name: string; preferred_channel: string | null; has_email: boolean; has_phone: boolean }[];
  tasks: { task_id: string; kind: string; assignee_member_id: string | null; status: "open" | "done" | "skipped"; due_at: string | null; done_at: string | null }[];
  notes: { note_id: string; kind: string; body: string; created_at: string; created_by: string }[];
  history: { from_stage: Stage | null; to_stage: Stage; reason_code: string; at: string; actor: string }[];
  submissions: { submission_id: string; outcome: "created" | "attached" | null; status: string; created_at: string; answers: Record<string, unknown> | null }[];
  allowed_moves: Stage[];
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
  const head = fmt(t(lang, "trip.adults"), { n: adults });
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
