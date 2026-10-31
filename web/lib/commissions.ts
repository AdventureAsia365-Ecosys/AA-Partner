// web/lib/commissions.ts
// AA-957 (card CRM-27, Module 13) — the types of the commission routes (GET /crm/v1/commissions, the follow-up of one
// commission), the A-12 filters, status tones, who may act and the refusal wording keys. Commission is a margin field
// (design §4.6): owner and agent only, the API refuses the assistant.
import { formatDate, formatDayMonth, type DateLocale } from "@/lib/dates";

export type CommissionStatus = "expected" | "claimed" | "received" | "written_off" | "cancelled";
export type CommissionFilter = "open" | "overdue" | "received" | "written_off" | "cancelled" | "all";
export type WriteOffReason = "supplier_refused" | "too_small" | "other";

export const FILTERS: readonly CommissionFilter[] = ["open", "overdue", "received", "written_off", "cancelled", "all"];
export const WRITE_OFF_REASONS: readonly WriteOffReason[] = ["supplier_refused", "too_small", "other"];

export interface Money {
  amount: string;
  currency: string;
  amount_base?: string | null;
  base_currency?: string | null;
}

export interface Commission {
  commission_id: string;
  trip_id: string;
  trip_title: string;
  trip_stage: string;
  /** S262: the trip's dates (two trips of one title are told apart by them). */
  trip_start_date?: string | null;
  trip_end_date?: string | null;
  trip_month?: string | null;
  booking_line_id: string;
  line_title: string;
  line_status: string;
  service_from: string | null;
  service_to: string | null;
  agency_supplier_id: string;
  supplier_name: string;
  status: CommissionStatus;
  due_on: string;
  overdue: boolean;
  days_overdue: number;
  claimed_at: string | null;
  claim_ref: string | null;
  close_reason: string | null;
  note: string | null;
  keep_on_cancel: boolean;
  commission_expected: Money | null;
  commission_received:
    | (Money & {
        received_on: string;
        reference: string | null;
        fx: { rate: string; rate_date: string; source: string; basis: "receipt_date"; stale_days: number; attribution?: { text: string; url: string } };
        short_amount: string | null;
        fx_difference_base: string | null;
      })
    | null;
}

export interface CommissionList {
  status: CommissionFilter;
  today: string;
  base_currency: string;
  commissions: Commission[];
  commission_totals: {
    open_count: number;
    overdue_count: number;
    open_base: string;
    overdue_base: string;
    received_base: string;
    received_expected_base: string;
    open_other_base: number;
  };
}

/** The follow-up of one line on the trip's Bookings tab (owner / agent). */
export interface LineCommission {
  commission_id: string;
  status: CommissionStatus;
  due_on: string;
  overdue: boolean;
}

export const STATUS_TONE: Record<CommissionStatus, "neutral" | "info" | "success" | "warning" | "danger"> = {
  expected: "info",
  claimed: "warning",
  received: "success",
  written_off: "neutral",
  cancelled: "neutral",
};

export function isFilter(value: unknown): value is CommissionFilter {
  return typeof value === "string" && (FILTERS as readonly string[]).includes(value);
}

/** Owner and agent follow commissions up; the assistant never sees them (the nav hides A-12, the API answers 403). */
export function canFollowUp(role: string): boolean {
  return role === "owner" || role === "agent";
}

/** The actions a member may take on a commission in its status. */
export function commissionActions(c: Pick<Commission, "status">): ("claim" | "unclaim" | "receive" | "write_off" | "due")[] {
  if (c.status === "expected") return ["claim", "receive", "write_off", "due"];
  if (c.status === "claimed") return ["receive", "unclaim", "write_off", "due"];
  return [];
}

const ERRORS = new Set([
  "commission_closed",
  "commission_not_claimed",
  "commission_not_found",
  "fx_rate_missing",
  "invalid_amount",
  "invalid_currency",
  "invalid_received_on",
  "invalid_reference",
  "invalid_reason",
  "invalid_note",
  "invalid_due_on",
  "role_forbidden",
]);

export function commissionErrorKey(code: string): string {
  return ERRORS.has(code) ? `commission.error.${code}` : "commission.error.other";
}

/** S262: an FX rate as its pair, rounded for reading, in the page locale (the same as `formatAmount` beside it):
 * "1 EUR = 1.0870 USD" (vi-VN "1 EUR = 1,0870 USD"; the stored rate keeps its 10 decimals). */
export function rateText(rate: string, from: string, to: string, locale?: string): string {
  const n = Number(rate);
  const text = Number.isFinite(n)
    ? new Intl.NumberFormat(locale, { minimumFractionDigits: 4, maximumFractionDigits: 4 }).format(n)
    : rate;
  return `1 ${from} = ${text} ${to}`;
}

/** S262: when the trip runs ("12 Nov – 15 Nov 2026"; its month when it has no dates yet; "" when neither). */
export function tripWhen(c: Pick<Commission, "trip_start_date" | "trip_end_date" | "trip_month">, locale: DateLocale): string {
  if (c.trip_start_date && c.trip_end_date && c.trip_end_date !== c.trip_start_date) {
    return `${formatDayMonth(c.trip_start_date, locale)} – ${formatDate(c.trip_end_date, locale)}`;
  }
  if (c.trip_start_date) return formatDate(c.trip_start_date, locale);
  if (c.trip_month) {
    return new Intl.DateTimeFormat(locale, { month: "short", year: "numeric", timeZone: "UTC" }).format(new Date(`${c.trip_month}T00:00:00Z`));
  }
  return "";
}
