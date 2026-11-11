// web/lib/partners.ts
// AA-1071 (crm-056) — the types of the sales partner routes (GET /crm/v1/sales-partners, /partner-commissions, the
// partner's own pages on the client host) and the pure helpers the pages use: who may do what, the terms in words, the
// filters, the refusal wording. Commission terms and amounts are margin fields (owner / agent); the assistant gets the
// partner names only (the trip's picker).
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

export type PartnerKind = "sub_agent" | "referrer" | "freelancer" | "ota";
export type CommissionType = "percent" | "fixed_per_pax" | "net_rate";
export type Basis = "revenue" | "margin";
export type PartnerCommissionStatus = "accrued" | "approved" | "paid" | "cancelled";
export type PartnerFilter = "open" | PartnerCommissionStatus | "all";

export const KINDS: readonly PartnerKind[] = ["sub_agent", "referrer", "freelancer", "ota"];
export const TYPES: readonly CommissionType[] = ["percent", "fixed_per_pax", "net_rate"];
export const BASES: readonly Basis[] = ["revenue", "margin"];
export const FILTERS: readonly PartnerFilter[] = ["open", "accrued", "approved", "paid", "cancelled", "all"];

export interface PartnerLinkInfo {
  link_id: string;
  intake_form_id: string | null;
  expires_at: string;
  last_used_at: string | null;
  use_count: number;
}

export interface Partner {
  sales_partner_id: string;
  kind: PartnerKind;
  name: string;
  status: "active" | "inactive";
  has_email: boolean;
  has_phone: boolean;
  trip_count: number;
  commission_type?: CommissionType;
  rate?: string;
  currency?: string | null;
  basis?: Basis;
  payout_due_days?: number;
  accrued_base?: string;
  approved_base?: string;
  links?: Partial<Record<"intake" | "view", PartnerLinkInfo>>;
}

export interface PartnerList {
  items: Partner[];
  base_currency: string;
  terms_visible: boolean;
}

export interface PartnerDetail {
  sales_partner_id: string;
  kind: PartnerKind;
  name: string;
  contact_email: string | null;
  contact_phone: string | null;
  status: "active" | "inactive";
  commission_type: CommissionType;
  rate: string;
  currency: string | null;
  basis: Basis;
  payout_due_days: number;
}

export interface PartnerCommission {
  partner_commission_id: string;
  trip_id: string;
  trip_ref: string;
  trip_stage: string;
  start_date: string | null;
  sales_partner_id: string;
  commission_type: CommissionType;
  rate: string;
  basis: Basis | "pax";
  basis_amount: string;
  pax: number | null;
  amount: string;
  currency: string;
  amount_base: string | null;
  base_currency: string | null;
  due_on: string | null;
  overdue: boolean;
  approved_at: string | null;
  paid_on: string | null;
  payout_ref: string | null;
  cancel_reason: string | null;
  status: PartnerCommissionStatus;
}

export interface PartnerCommissionList {
  items: PartnerCommission[];
  base_currency: string;
  totals: Record<PartnerCommissionStatus, string>;
  unconverted: number;
}

/** The partner's own view (GET /crm/public/v1/partner). */
export interface PartnerView {
  agency: string | null;
  signed_in: boolean;
  link_expires_at: string;
  partner_name?: string | null;
  trips?: {
    ref: string;
    stage: "enquiry" | "quoted" | "booked" | "travelling" | "completed" | "closed";
    start_month: string | null;
    travellers: number;
    received_on: string;
    commission: { amount: string; currency: string; status: PartnerCommissionStatus; due_on: string | null; paid_on: string | null } | null;
  }[];
  totals?: { currency: string; open: string; paid: string }[];
}

export const PARTNER_API = "/crm/public/v1/partner";
export const PARTNER_LINK_COOKIE = "aa_partner_link";

export function isFilter(value: unknown): value is PartnerFilter {
  return typeof value === "string" && (FILTERS as readonly string[]).includes(value);
}

export function canManage(role: string | null | undefined): boolean {
  return role === "owner";
}

export function seesMoney(role: string | null | undefined): boolean {
  return role === "owner" || role === "agent";
}

/** The commission terms in words ("10% of the trip price", "25 EUR per traveller"). */
export function termsText(lang: Lang, p: Pick<Partner, "commission_type" | "rate" | "currency" | "basis">, money: (a: string, c: string) => string): string {
  if (!p.commission_type || p.rate === undefined) return "";
  if (p.commission_type === "fixed_per_pax") return fmt(t(lang, "partners.terms.fixed_per_pax"), { amount: money(p.rate, p.currency ?? "") });
  const basis = t(lang, `partners.basis.${p.basis ?? "revenue"}` as MessageKey);
  return fmt(t(lang, `partners.terms.${p.commission_type}` as MessageKey), { rate: Number(p.rate).toString(), basis });
}

const KNOWN_ERRORS = new Set([
  "partner_in_use",
  "partner_no_email",
  "partner_inactive",
  "invalid_partner",
  "invalid_link",
  "commission_changed",
  "role_forbidden",
]);

export function partnerErrorKey(detail: string | null | undefined): MessageKey {
  return (detail && KNOWN_ERRORS.has(detail) ? `partners.error.${detail}` : "partners.error.generic") as MessageKey;
}

/** The actions an owner has on one partner commission. */
export function commissionActions(status: PartnerCommissionStatus): ("approve" | "unapprove" | "pay" | "cancel")[] {
  if (status === "accrued") return ["approve", "cancel"];
  if (status === "approved") return ["pay", "unapprove", "cancel"];
  return [];
}

export const STATUS_TONE: Record<PartnerCommissionStatus, "warning" | "info" | "success" | "neutral"> = {
  accrued: "warning",
  approved: "info",
  paid: "success",
  cancelled: "neutral",
};

/** The partner view's refusal wording for a code request or a code check. */
export function partnerPageErrorKey(detail: string | null | undefined): MessageKey {
  switch (detail) {
    case "too_many_codes":
      return "partnerPage.tooManyCodes";
    case "no_email_on_file":
      return "partnerPage.noEmail";
    case "code_delivery_unavailable":
      return "partnerPage.noDelivery";
    case "code_expired":
      return "partnerPage.codeExpired";
    case "link_not_found":
      return "partnerPage.notFound";
    default:
      return "partnerPage.error";
  }
}



/** The partner page's link cookie: host-only, httpOnly, scoped to the API's partner routes (as the traveller portal's,
 * lib/clientPortal.ts); 30 days, the API decides whether the link still works. */
export function partnerCookie(token: string): string {
  return `${PARTNER_LINK_COOKIE}=${token}; Path=${PARTNER_API}; Max-Age=${30 * 24 * 3600}; HttpOnly; Secure; SameSite=Strict`;
}
