// web/lib/clientProposal.ts
// AA-950 (card CRM-21b) — the client side of a day-by-day proposal: the shapes of the API's page
// (api/app/proposals/client_page.py), the link cookie of Fix B11, and the pure helpers the client page (C-03 Keep /
// Swap, C-04 accept, C-12 hold expired) and the trip's Proposal tab share. Amounts are the client's sell amounts and
// the per-option difference to the included one (Decision D1): never a net, a margin or a per-line price.

import type { PublicProposal } from "./proposals";

/** Fix B11: the link's token goes into this host-only cookie (scoped to the API's proposal path) and leaves the URL. */
export const PROPOSAL_COOKIE = "aa_proposal_link";
export const PROPOSAL_API = "/crm/public/v1/proposal";
/** The longest life of a link (crm.client_link: 30 days); the API refuses an expired or revoked token anyway. */
export const PROPOSAL_COOKIE_MAX_AGE = 30 * 24 * 3600;

/** The Set-Cookie value of the exchange: httpOnly (page script never sees the token), Secure, SameSite=Strict, no
 * Domain (host-only: the client host only), Path = the API's proposal routes only. */
export function proposalCookie(token: string): string {
  return `${PROPOSAL_COOKIE}=${token}; Path=${PROPOSAL_API}; Max-Age=${PROPOSAL_COOKIE_MAX_AGE}; HttpOnly; Secure; SameSite=Strict`;
}

export type ConfirmClass = "on_payment" | "partner" | "agent" | "arrival";
export type HoldStatus = "none" | "missing" | "active" | "expired" | "stale";

export interface ClientOption {
  option_id: string;
  title: string;
  is_default: boolean;
  chosen: boolean;
  availability: "instant" | "on_request" | "sold_out" | "agency";
  confirm_class: ConfirmClass;
  pay_on_arrival: boolean;
  /** The sell difference to the block's included option ("0.00" for it); null when the agency shows the total only. */
  delta: string | null;
  blocked: { reason: "sold_out" | "excludes" | "requires"; with?: string | null } | null;
}

export interface ClientBlock {
  option_block_id: string;
  title: string;
  type: string;
  client_may_swap: boolean;
  choose_later: boolean;
  choose_later_allowed: boolean;
  fell_back: boolean;
  options: ClientOption[];
}

export interface ClientDay {
  day_no: number;
  before_you_go: boolean;
  date: string | null;
  tz: string;
  utc_offset: string;
  title: string | null;
  place: string | null;
  notes: string | null;
  blocks: ClientBlock[];
}

export interface HoldBox {
  status: HoldStatus;
  expires_at: string | null;
  first_hold: boolean;
  rechecks_left: number;
}

export interface VersionNotice {
  from_version: number;
  changes: { kind: "added" | "changed" | "removed" | "default" | "days"; title?: string; block?: string; from?: number; to?: number }[];
  more: number;
  total_from: string;
  total_to: string;
  picks_carried: number;
}

/** One section of an AA prep pack (api prep_packs.sections): a visa section carries its official links only. */
export interface PackSection {
  heading: string | null;
  text: string;
  visa: boolean;
  links: { label: string; url: string }[];
}

/** AA-951 (R38, F4): the "Before you go" the e-mail / WhatsApp texts carry too: the picked packs (every locale), the
 * insurance reminder, the fitness warning. */
export interface ClientBeforeYouGo {
  packs: { group_key: string; versions: { locale: string; title: string; sections: PackSection[] }[] }[];
  insurance_reminder: boolean;
  fitness_warning: boolean;
}

/** The page of a builder version: the text page's fields plus the choice. */
export interface ClientBuilderPage extends PublicProposal {
  kind: "builder";
  price_visibility: "total_with_deltas" | "total_only";
  rooms: { type: string; adults: number; children: number }[];
  days: ClientDay[];
  groups: { confirm_class: ConfirmClass; items: { title: string; choose_later: boolean }[] }[];
  totals: { currency: string; total: string; pay_on_arrival_total: string; deposit: string; balance: string; balance_due_on: string | null };
  selection_hash: string;
  hold: HoldBox;
  notice: VersionNotice | null;
  before_you_go?: ClientBeforeYouGo | null;
}

export type AnyClientPage = (PublicProposal & { kind?: "text" }) | ClientBuilderPage;

export function isBuilderPage(page: AnyClientPage): page is ClientBuilderPage {
  return page.kind === "builder";
}

/** The pack copy in the page's language, else English, else the first one (as the texts pick it). */
export function packVersion<V extends { locale: string }>(versions: V[], lang: string): V | null {
  return versions.find((v) => v.locale === lang) ?? versions.find((v) => v.locale === "en") ?? versions[0] ?? null;
}

/** "Included", "+40.00 USD" or "−20.00 USD" (minus sign), or null when only the total is shown. */
export function deltaLabel(delta: string | null, currency: string, locale: string): string | null {
  if (delta == null) return null;
  const n = Number(delta);
  if (!Number.isFinite(n) || n === 0) return "";
  const digits = currency === "VND" ? 0 : 2;
  const text = Math.abs(n).toLocaleString(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${n > 0 ? "+" : "−"}${text} ${currency}`;
}

/** The i18n key of a hold box line (C-04 / C-12). */
export function holdKey(hold: HoldBox): string {
  if (hold.status === "expired") return hold.rechecks_left > 0 ? "client.hold.expired" : "client.hold.askAgent";
  if (hold.status === "stale") return "client.hold.stale";
  if (hold.status === "missing") return "client.hold.missing";
  if (hold.status === "active") return "client.hold.activeUntil";
  return "client.hold.none";
}

/** Accept is offered when nothing needs holding, or the hold is live and covers the choice. */
export function acceptOpen(page: ClientBuilderPage): boolean {
  return page.status === "sent" && (page.hold.status === "none" || page.hold.status === "active");
}

/** The "Check availability" button: needed when AA items are chosen and no live hold covers them; once the day's
 * re-checks are used, only "Ask your agent" is left (card R). */
export function holdAction(hold: HoldBox): "none" | "hold" | "recheck" | "ask_agent" {
  if (hold.status === "none" || hold.status === "active") return "none";
  if (hold.first_hold) return "hold";
  return hold.rechecks_left > 0 ? "recheck" : "ask_agent";
}

export const CHANGE_REASONS = ["rooms", "dates", "activities", "lodging", "budget", "other"] as const;
export type ChangeReason = (typeof CHANGE_REASONS)[number];

const HOLD_CODES = new Set([
  "option_sold_out",
  "recheck_limit",
  "hold_unavailable",
  "hold_in_progress",
  "booking_unavailable",
  "hold_required",
  "hold_expired",
  "choice_changed",
  "choose_later_not_allowed",
  "swap_not_allowed",
  "option_conflict",
  "already_accepted",
  "nothing_to_charge",
]);

/** The i18n key of a refusal of the interactive page (client.proposalError.<code>), else null (the shared ones). */
export function interactiveErrorKey(code: string): string | null {
  return HOLD_CODES.has(code) ? `client.proposalError.${code}` : null;
}

/** "Day 3 · 12 Dec · Pokhara" pieces are rendered by the page; the zone label for a day: "UTC+05:45". */
export function zoneLabel(offset: string): string {
  return `UTC${offset}`;
}

/** The R19 / notice line of one change, as an i18n key and its variables. */
export function noticeLine(change: VersionNotice["changes"][number]): { key: string; vars: Record<string, string | number> } {
  switch (change.kind) {
    case "days":
      return { key: "client.notice.days", vars: { from: change.from ?? 0, to: change.to ?? 0 } };
    case "default":
      return { key: "client.notice.default", vars: { block: change.block ?? "", title: change.title ?? "" } };
    default:
      return { key: `client.notice.${change.kind}`, vars: { title: change.title ?? "" } };
  }
}
