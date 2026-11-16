// web/lib/campaigns.ts
// AA-968 (card CRM-36; design v3 R14, A-15 Marketing) — the campaign shapes of /crm/v1/campaigns and the pure words
// around them: the state badge, why a campaign is held, the content gate's codes (root ADR 0019 d21), the left-out
// reasons, the API's refusals, and the one-line summary of a preview. The API decides everything; these only word it.

import { fmt, t, type Lang, type MessageKey } from "./i18n";

export type CampaignState = "draft" | "scheduled" | "sending" | "held" | "sent" | "cancelled";

export type CampaignCounts = { planned: number; queued: number; skipped: number; sent: number; unsubscribed: number };

export type CampaignItem = {
  campaign_id: string;
  name: string;
  state: CampaignState;
  hold_code: string | null;
  gate_codes: string[];
  scheduled_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  recipient_count: number;
  excluded: Record<string, number>;
  counts: CampaignCounts;
  version: number;
  created_at: string | null;
};

export type CampaignList = { feature_allowed: boolean; can_send: boolean; items: CampaignItem[] };

export type CampaignDetail = {
  campaign_id: string;
  name: string;
  locale: "en" | "vi";
  subject: string;
  body: string;
  channel_account_id: string | null;
  aa_tours: { source_id: string; title: string | null; served_state: string | null }[];
  aa_content_id: string | null;
  brand_only: boolean;
  audience_tags: string[];
  state: CampaignState;
  hold_code: string | null;
  gate_codes: string[];
  version: number;
};

export type Mailbox = { channel_account_id: string; scope: "agency" | "mine"; kind: string; status: string; address: string };

export type CampaignOptions = {
  feature_allowed: boolean;
  mailboxes: Mailbox[];
  tags: { code: string; name: string; kind: string }[];
  blocks: { content_id: string; locale: string; title: string; version: number }[];
  daily_limit: number;
  max_recipients: number;
  client_origin: string | null;
  tripplanner_origin: string | null;
};

export type TourHit = { source_id: string; title: string; country_code: string | null; duration_days: number | null };

export type Preview = {
  gate_codes: string[];
  eligible: number;
  excluded: Record<string, number>;
  already_done: number;
  daily_limit: number;
  max_recipients: number;
  over_cap: boolean;
  days: number;
  has_mailbox: boolean;
  feature_allowed: boolean;
};

export type Draft = {
  name: string;
  locale: "en" | "vi";
  subject: string;
  body: string;
  channel_account_id: string | null;
  aa_tour_ids: string[];
  aa_content_id: string | null;
  brand_only: boolean;
  audience_tags: string[];
};

export const STATE_TONE: Record<CampaignState, "neutral" | "accent" | "success" | "warning" | "info"> = {
  draft: "neutral",
  scheduled: "info",
  sending: "accent",
  held: "warning",
  sent: "success",
  cancelled: "neutral",
};

export const GATE_CODES = [
  "no_aa_subject",
  "aa_tour_not_served",
  "aa_content_unlisted",
  "link_not_allowed",
  "tour_link_not_listed",
  "brand_has_sales_link",
  "empty_subject",
  "empty_body",
] as const;
export const HOLD_CODES = ["content_gate", "aa_tour_retired", "feature_off", "no_mailbox", "not_configured"] as const;
export const EXCLUDED_CODES = ["no_consent", "suppressed", "no_email"] as const;
const ERRORS = [
  "feature_not_allowed",
  "role_forbidden",
  "version_conflict",
  "campaign_not_editable",
  "campaign_not_draft",
  "campaign_not_cancellable",
  "campaigns_not_configured",
  "mailbox_required",
  "invalid_mailbox",
  "content_gate",
  "audience_empty",
  "audience_over_cap",
  "count_changed",
  "tag_not_found",
  "aa_content_not_found",
  "invalid_name",
] as const;

export function emptyDraft(): Draft {
  return { name: "", locale: "en", subject: "", body: "", channel_account_id: null, aa_tour_ids: [], aa_content_id: null, brand_only: false, audience_tags: [] };
}

export function draftOf(d: CampaignDetail): Draft {
  return {
    name: d.name,
    locale: d.locale,
    subject: d.subject,
    body: d.body,
    channel_account_id: d.channel_account_id,
    aa_tour_ids: d.aa_tours.map((x) => x.source_id),
    aa_content_id: d.aa_content_id,
    brand_only: d.brand_only,
    audience_tags: d.audience_tags,
  };
}

export function editable(state: CampaignState): boolean {
  return state === "draft" || state === "held";
}

export function cancellable(state: CampaignState): boolean {
  return state === "draft" || state === "scheduled" || state === "sending" || state === "held";
}

export function gateText(lang: Lang, code: string): string {
  return (GATE_CODES as readonly string[]).includes(code) ? t(lang, `camp.gate.${code}` as MessageKey) : code;
}

export function holdText(lang: Lang, code: string | null): string {
  if (!code) return "";
  return (HOLD_CODES as readonly string[]).includes(code) ? t(lang, `camp.hold.${code}` as MessageKey) : t(lang, "camp.hold.other");
}

export function campaignErrorKey(detail: string): MessageKey {
  return ((ERRORS as readonly string[]).includes(detail) ? `camp.err.${detail}` : "camp.err.generic") as MessageKey;
}

/** The left-out reasons of a plan, one phrase each ("2 without marketing consent"), in a fixed order. */
export function excludedText(lang: Lang, excluded: Record<string, number>): string[] {
  return EXCLUDED_CODES.filter((c) => (excluded[c] ?? 0) > 0).map((c) => fmt(t(lang, `camp.excluded.${c}` as MessageKey), { n: excluded[c] }));
}

/** The preview in one sentence: how many clients, over how many days at the mailbox's daily limit. */
export function previewSummary(lang: Lang, p: Preview): string {
  if (p.eligible === 0) return t(lang, "camp.preview.none");
  const key: MessageKey = p.days > 1 ? "camp.preview.spread" : "camp.preview.oneDay";
  return fmt(t(lang, key), { n: p.eligible, days: p.days, limit: p.daily_limit });
}

/** The fields the API takes (the BFF passes the body through). */
export function payloadOf(d: Draft): Record<string, unknown> {
  return {
    name: d.name.trim(),
    locale: d.locale,
    subject: d.subject,
    body: d.body,
    channel_account_id: d.channel_account_id || null,
    aa_tour_ids: d.aa_tour_ids,
    aa_content_id: d.aa_content_id || null,
    brand_only: d.brand_only,
    audience_tags: d.audience_tags,
  };
}

/** A datetime-local value (the member's own clock) as an ISO instant; empty = now. */
export function scheduleAt(local: string): string | null {
  if (!local) return null;
  const at = new Date(local);
  return Number.isNaN(at.getTime()) ? null : at.toISOString();
}

/** The public unsubscribe page of one mail: the 43-character token of /c/u/<token>. */
export const UNSUBSCRIBE_TOKEN = /^[A-Za-z0-9_-]{43}$/;
