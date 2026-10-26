// web/lib/builder.ts
// AA-949 (card CRM-21a) — the shapes of the day-list builder API (api/app/proposals/builder.py) and the pure helpers
// the builder page (components/proposals/BuilderPage.tsx) uses. Net, margin and their deltas come only for owner /
// agent (the API never builds them for the assistant); amounts are decimal strings and are never re-computed here
// except the margin share shown next to the totals.

import type { MessageKey } from "@/lib/i18n";

export type BuilderStatus = "draft" | "published" | "sent" | "accepted" | "superseded" | "withdrawn";
export type BlockType = "lodging" | "activity" | "transport" | "meal" | "guide" | "other";
export type RoomType = "single" | "double" | "twin" | "triple" | "family";
export type Availability = "instant" | "on_request" | "sold_out" | "agency";
export type Tier = "budget" | "comfort" | "premium" | "luxury";
export const BLOCK_TYPES: readonly BlockType[] = ["lodging", "activity", "transport", "meal", "guide", "other"];
export const ROOM_TYPES: readonly RoomType[] = ["single", "double", "twin", "triple", "family"];
export const TIERS: readonly Tier[] = ["budget", "comfort", "premium", "luxury"];

export interface BuilderOption {
  option_id: string;
  source: "aa" | "agency";
  title: string;
  tier: Tier | null;
  agency_supplier_id: string | null;
  sell_amount: string;
  sell_delta: string | null;
  net_amount?: string | null;
  net_delta?: string | null;
  pay_on_arrival: boolean;
  choose_later_allowed: boolean;
  availability: Availability;
  availability_checked_at: string | null;
  is_default: boolean;
  picked: boolean;
  aa?: {
    tour_source_id: string;
    component_id: string;
    component_kind: string;
    departure_id: string | null;
    quote_valid_until: string | null;
    quote_expired: boolean;
    from_price: string | null;
    below_from_price: boolean | null;
    no_longer_offered: boolean;
  };
}

export interface BuilderBlock {
  option_block_id: string;
  title: string;
  type: BlockType;
  sort_order: number;
  client_may_swap: boolean;
  default_option_id: string | null;
  options: BuilderOption[];
}

export interface BuilderDay {
  proposal_day_id: string;
  day_no: number;
  before_you_go: boolean;
  date: string | null;
  title: string | null;
  place: string | null;
  tz: string;
  utc_offset: string;
  notes: string | null;
  blocks: BuilderBlock[];
}

export interface Room {
  type: RoomType;
  adults: number;
  children: number;
}

export interface BuilderView {
  proposal_id: string;
  trip_id: string;
  trip_stage: string;
  version: number;
  status: BuilderStatus;
  kind: "builder";
  edit_seq: number;
  selection_seq: number;
  currency: string;
  price_visibility: "total_with_deltas" | "total_only";
  room_config: Room[];
  deposit_pct: string | null;
  starts_on: string | null;
  balance_due_on: string | null;
  terms_doc_id: string | null;
  intro: string;
  price_hash: string | null;
  published_at: string | null;
  based_on: { proposal_id: string; version: number; status: string; client_picks_since_copy: number; selection_seq: number } | null;
  days: BuilderDay[];
  rules: { option_rule_id: string; kind: "requires" | "excludes"; option_id: string; target_option_id: string }[];
  picks: { option_block_id: string; option_id: string; chosen_by: string; choose_later: boolean; at: string | null }[];
  totals: {
    currency: string;
    sell_total: string;
    pay_on_arrival_total: string;
    deposit: string;
    balance: string;
    net_total?: string | null;
    margin_total?: string | null;
  };
  checks: { publishable: boolean; blocking: string[]; warnings: string[] };
  stage2: { accommodation_tier: string | null; group_pref: string | null; pace: string | null; party_adults: number; party_children: number };
  publish?: { published_at: string; price_hash: string; superseded: string[]; picks_carried: number };
  refresh?: { quoted: number; failed: { option_id: string; code: string }[] };
}

/** The 409 the API answers when the page is stale (another editor) or the client picked on the base version. */
export interface VersionConflict {
  reason: "edited" | "client_selected";
  current_seq?: number;
  base_version?: number;
  selection_seq?: number;
  picks?: { block: string; option: string; chosen_by: string; at: string | null }[];
}

export function versionConflict(status: number, detail: string, body: unknown): VersionConflict | null {
  if (status !== 409 || detail !== "version_conflict" || typeof body !== "object" || body === null) return null;
  const b = body as Record<string, unknown>;
  if (b.reason !== "edited" && b.reason !== "client_selected") return null;
  return b as unknown as VersionConflict;
}

/** "UTC+05:45" for the day header (R33; the API computes the offset of the day's zone on that date). */
export function offsetLabel(offset: string): string {
  return /^[+-]\d{2}:\d{2}$/.test(offset) ? `UTC${offset}` : "UTC";
}

/** Margin share of the sell total, one decimal ("18.4"); null when the net is not known or nothing is sold. */
export function marginPct(sell: string | null | undefined, margin: string | null | undefined): string | null {
  const s = Number(sell);
  const m = Number(margin);
  if (margin === null || margin === undefined || !Number.isFinite(s) || !Number.isFinite(m) || s <= 0) return null;
  return ((m / s) * 100).toFixed(1);
}

/** A decimal amount typed by the agent: "" -> null, "1,500.5" -> "1500.50"; invalid -> undefined. */
export function moneyInput(raw: string): string | null | undefined {
  const clean = raw.replace(/[\s,]/g, "");
  if (clean === "") return null;
  if (!/^\d{1,12}(\.\d{1,2})?$/.test(clean)) return undefined;
  return Number(clean).toFixed(2);
}

/** Every option of the version, with its block and day (the Advanced rule pickers name them). */
export function allOptions(view: BuilderView): { option: BuilderOption; block: BuilderBlock; day: BuilderDay }[] {
  return view.days.flatMap((day) => day.blocks.flatMap((block) => block.options.map((option) => ({ option, block, day }))));
}

/** The title of an option for a rule line: "Day 2 · Lodging · Homestay". */
export function optionLabel(view: BuilderView, optionId: string, dayWord: string): string {
  const hit = allOptions(view).find((x) => x.option.option_id === optionId);
  if (!hit) return "?";
  return `${dayWord} ${hit.day.day_no} · ${hit.block.title} · ${hit.option.title}`;
}

/** The API's refusal codes, worded (EN / VI in lib/i18n.ts). */
export function builderErrorKey(detail: string): MessageKey {
  const known: Record<string, MessageKey> = {
    version_conflict: "builder.err.version_conflict",
    option_conflict: "builder.err.option_conflict",
    proposal_not_draft: "builder.err.proposal_not_draft",
    builder_draft_exists: "builder.err.builder_draft_exists",
    not_publishable: "builder.err.not_publishable",
    aa_sold_out: "builder.err.aa_sold_out",
    booking_unavailable: "builder.err.booking_unavailable",
    booking_not_configured: "builder.err.booking_unavailable",
    aa_partner_not_active: "aa.error.aa_partner_not_active",
    aa_tour_not_served: "builder.err.aa_tour_not_served",
    day_has_no_date: "builder.err.day_has_no_date",
    rule_same_block: "builder.err.rule_same_block",
    rule_exists: "builder.err.rule_exists",
    role_forbidden: "builder.err.role_forbidden",
    before_you_go_kept: "builder.err.before_you_go_kept",
    last_day_kept: "builder.err.last_day_kept",
    invalid_tz: "builder.err.invalid_tz",
    terms_not_published: "proposal.error.terms_not_published",
    trip_not_open_for_proposal: "builder.err.trip_not_open",
    option_sold_out: "builder.err.aa_sold_out",
    aa_net_from_quote: "builder.err.aa_net_from_quote",
  };
  return known[detail] ?? "builder.err.generic";
}

/** The blocking checks and warnings the API lists, worded. */
export function checkKey(code: string): MessageKey {
  const known: Record<string, MessageKey> = {
    no_options: "builder.check.no_options",
    block_without_default: "builder.check.block_without_default",
    default_sold_out: "builder.check.default_sold_out",
    option_conflict: "builder.check.option_conflict",
    aa_prices_stale: "builder.check.aa_prices_stale",
    nothing_to_charge: "builder.check.nothing_to_charge",
    balance_due_on_required: "builder.check.balance_due_on_required",
    below_from_price: "builder.check.below_from_price",
    aa_no_longer_offered: "builder.check.aa_no_longer_offered",
    net_missing: "builder.check.net_missing",
  };
  return known[code] ?? "builder.err.generic";
}

/** A room list as the API takes it, or the message key of the first problem. */
export function roomsBody(rooms: Room[]): { ok: true; rooms: Room[] } | { ok: false; key: MessageKey } {
  if (rooms.length < 1 || rooms.length > 20) return { ok: false, key: "builder.rooms.invalid" };
  for (const r of rooms) {
    if (!ROOM_TYPES.includes(r.type)) return { ok: false, key: "builder.rooms.invalid" };
    if (!Number.isInteger(r.adults) || !Number.isInteger(r.children)) return { ok: false, key: "builder.rooms.invalid" };
    if (r.adults < 0 || r.adults > 6 || r.children < 0 || r.children > 6 || r.adults + r.children < 1) {
      return { ok: false, key: "builder.rooms.invalid" };
    }
  }
  return { ok: true, rooms };
}

/** "1 family (2A 1C), 1 single (1A)" — the rooms line of WF-4. */
export function roomsSummary(rooms: Room[], words: Record<RoomType, string>): string {
  return rooms.map((r) => `${words[r.type]} (${r.adults}A${r.children ? ` ${r.children}C` : ""})`).join(", ");
}
