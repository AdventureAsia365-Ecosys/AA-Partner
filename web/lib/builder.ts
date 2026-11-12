// web/lib/builder.ts
// AA-949 (card CRM-21a) — the shapes of the day-list builder API (api/app/proposals/builder.py) and the pure helpers
// the builder page (components/proposals/BuilderPage.tsx) uses. Net, margin and their deltas come only for owner /
// agent (the API never builds them for the assistant); amounts are decimal strings and are never re-computed here
// except the margin share shown next to the totals.

import { fmt, type MessageKey } from "@/lib/i18n";
import type { PolicySummary, ScheduleAmount, ScheduleLine } from "@/lib/retailPayments";
import type { AppliedRule, PriceRow, PricingBlock } from "@/lib/retailPricing";
import type { InclusionsPanelData } from "@/lib/inclusions";
import type { BeforeYouGo } from "@/lib/textProposal";

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
  /** AA-1066: the price per traveller type (the sell is their sum), the rules the sell was suggested with, and the
   *  grid the editor starts from (the suggestion shared per head, or the current sell). */
  prices?: PriceRow[];
  price_rules?: AppliedRule[] | null;
  suggested_prices?: PriceRow[];
  aa?: {
    tour_source_id: string;
    component_id: string;
    component_kind: string;
    departure_id: string | null;
    nights: number | null;
    unpriced: boolean;
    quote_valid_until: string | null;
    quote_expired: boolean;
    from_price: string | null;
    below_from_price: boolean | null;
    no_longer_offered: boolean;
    /** AA-1030: the suggested sell (the agency markup on the B2B price, else AA's suggested retail price x party). */
    suggested_sell?: string | null;
    suggested_basis?: "agency_markup" | "aa_rrp" | null;
    /** AA-1066: the agency's price rules the suggestion applied. */
    suggested_rules?: AppliedRule[];
  };
}

export interface BuilderBlock {
  option_block_id: string;
  title: string;
  type: BlockType;
  sort_order: number;
  client_may_swap: boolean;
  default_option_id: string | null;
  default_pinned: boolean;
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
  /** AA-1067: the payment lines (null = the deposit / balance pair) and the cancellation policy of the version. */
  schedule?: ScheduleLine[] | null;
  cancellation_policy?: PolicySummary | null;
  terms_doc_id: string | null;
  /** S262 round 3: the named version's state (null without terms or once removed). */
  terms_status?: "published" | "retired" | "draft" | null;
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
    /** AA-1066: only with a discount. */
    subtotal?: string;
    discount_pct?: string;
    discount?: string;
    /** AA-1067: every payment line with its amount (only with lines). */
    schedule?: ScheduleAmount[];
  };
  /** AA-1066: the agency's age bands, the party by traveller type, the discount and its approval. */
  pricing?: PricingBlock;
  checks: { publishable: boolean; blocking: string[]; warnings: string[] };
  stage2: { accommodation_tier: string | null; group_pref: string | null; pace: string | null; party_adults: number; party_children: number };
  publish?: { published_at: string; price_hash: string; superseded: string[]; picks_carried: number };
  refresh?: { quoted: number; failed: { option_id: string; code: string }[]; skipped: number };
  /** AA-951 (CRM-22, R38): the day 0 packs, the suggestions from the AA items and the reminders. */
  before_you_go?: BeforeYouGo;
  /** AA-1070 crm-35: the version's "Included" / "Not included" lines, the AA tours' fixed lists, the library. */
  inclusions?: InclusionsPanelData;
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
    trip_cancelled: "builder.closed", // S262 round 3: a cancelled trip's builder is read-only
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
    invalid_nights: "aa.error.nights",
    aa_net_from_quote: "builder.err.aa_net_from_quote",
    // AA-1069: "From catalogue" (the service is switched off, not offered on the day, gone, or needs a rate).
    service_inactive: "catalogue.error.service_inactive",
    service_not_valid: "catalogue.error.service_not_valid",
    service_not_found: "catalogue.error.service_not_found",
    fx_rate_missing: "catalogue.error.fx_rate_missing",
    // AA-1066: traveller prices and the discount.
    amount_from_prices: "pricing.err.amount_from_prices",
    no_suggestion: "pricing.err.no_suggestion",
    unit_net_partial: "pricing.err.unit_net_partial",
    prices_nothing_charged: "pricing.err.prices_nothing_charged",
    invalid_prices: "pricing.err.invalid_prices",
    discount_needs_approval: "pricing.err.discount_needs_approval",
    discount_approval_not_needed: "pricing.err.discount_approval_not_needed",
    discount_approval_not_pending: "pricing.err.discount_approval_not_pending",
    invalid_discount_pct: "pricing.err.invalid_discount_pct",
    // AA-1067: the payment lines and the cancellation policy.
    invalid_schedule: "payments.err.invalid_schedule",
    policy_not_live: "payments.err.policy_not_live",
  };
  return known[detail] ?? "builder.err.generic";
}

/** What "Refresh AA prices" says: how many were checked, how many are left for another run (40 a run, the oldest
 *  quotes first) and how many Booking refused or could not quote (no date). */
export function refreshMessage(
  r: { quoted: number; failed: { option_id: string; code: string }[]; skipped: number },
  tr: (key: string) => string,
): { tone: "danger" | "success"; text: string } {
  const failed = r.failed.filter((f) => f.code !== "refresh_limit").length;
  const parts = [fmt(tr("builder.aa.refreshed"), { n: r.quoted })];
  if (r.skipped > 0) parts.push(fmt(tr("builder.aa.refreshSkipped"), { n: r.skipped }));
  if (failed > 0) parts.push(fmt(tr("builder.aa.refreshFailed"), { n: failed }));
  return { tone: failed > 0 || r.skipped > 0 ? "danger" : "success", text: parts.join(" ") };
}

/** The blocking checks and warnings the API lists, worded. */
export function checkKey(code: string): MessageKey {
  const known: Record<string, MessageKey> = {
    no_options: "builder.check.no_options",
    block_without_default: "builder.check.block_without_default",
    default_sold_out: "builder.check.default_sold_out",
    option_conflict: "builder.check.option_conflict",
    aa_prices_stale: "builder.check.aa_prices_stale",
    aa_item_unpriced: "builder.check.aa_item_unpriced",
    balance_due_on_not_needed: "builder.check.balance_due_on_not_needed",
    nothing_to_charge: "builder.check.nothing_to_charge",
    balance_due_on_required: "builder.check.balance_due_on_required",
    below_from_price: "builder.check.below_from_price",
    aa_no_longer_offered: "builder.check.aa_no_longer_offered",
    net_missing: "builder.check.net_missing",
    prep_packs_not_chosen: "builder.check.prep_packs_not_chosen",
    terms_retired: "builder.check.terms_retired",
    terms_not_published: "builder.check.terms_not_published",
    discount_needs_approval: "builder.check.discount_needs_approval",
    discount_owner_approves: "builder.check.discount_owner_approves",
    prices_party_changed: "builder.check.prices_party_changed",
    // AA-1067: the payment lines.
    schedule_dates_required: "builder.check.schedule_dates_required",
    schedule_dates_order: "builder.check.schedule_dates_order",
    schedule_after_start: "builder.check.schedule_after_start",
    schedule_amount_zero: "builder.check.schedule_amount_zero",
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
