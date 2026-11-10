// web/lib/finance.ts
// AA-1068 (crm-051) — the types and small helpers of CRM finance: the trip's Money card (GET /trips/{id}/pnl), supplier
// bills, the "Bookings with AA" page (GET /aa-bookings), group departure costs (GET /departures/{ref}/pnl), the profit
// cube (GET /reports/profit) and the Home "today" tiles (GET /reports/agenda). Money is owner / agent only (the API
// refuses the assistant): canSeeMoney decides what a page offers.

export type Dimension = "lead_source" | "sales_partner" | "product" | "travel_month" | "owner_member";
export const DIMENSIONS: readonly Dimension[] = ["lead_source", "product", "travel_month", "owner_member", "sales_partner"];

export function canSeeMoney(role: string): boolean {
  return role === "owner" || role === "agent";
}

export function isDimension(value: unknown): value is Dimension {
  return typeof value === "string" && (DIMENSIONS as readonly string[]).includes(value);
}

export interface TripPnl {
  trip_id: string;
  base_currency: string;
  pax: number;
  revenue: { sales_base: string; service_fees_base: string; cancellation_fees_base: string; cancellation_charged: boolean; refunds_base: string; total_base: string };
  costs: {
    aa_quoted_base: string;
    aa_base: string;
    agency_planned_base: string;
    agency_base: string;
    /** Only once a supplier billed a line: actual - planned of the billed lines. */
    variance_base: string | null;
    cancellation_base: string;
    departure_base: string;
    partner_commission_base: string;
    total_base: string;
  };
  commission_income_base: string;
  margin_base: string;
  margin_pct: string | null;
  margin_per_pax_base: string;
  counts: {
    lines_confirmed: number;
    lines_planning: number;
    lines_billed: number;
    lines_other_base: number;
    aa_lines_invoiced: number;
    aa_lines_mismatch: number;
    departure_costs: number;
    revenue_unconverted: number;
  };
  notes: PnlNote[];
  group_code: string | null;
  departure_refs: string[];
}

export type PnlNote =
  | "lines_planning"
  | "revenue_unconverted"
  | "lines_other_base"
  | "aa_invoice_mismatch"
  | "cancellation_charge"
  | "no_revenue";

export interface SupplierBill {
  supplier_bill_id: string;
  agency_supplier_id: string;
  supplier_name: string;
  number: string;
  bill_date: string;
  status: "recorded" | "paid" | "void";
  paid_on: string | null;
  note: string | null;
  bill_cost: { amount: string; currency: string; amount_base: string; base_currency: string; allocated: string; unallocated: string };
  lines: { booking_line_id: string; line_title: string; trip_id: string; trip_title: string; line_cost: string }[];
}

export interface AaInvoiceRow {
  number: string;
  invoice_kind: "saas" | "settlement";
  status: "issued" | "paid" | "void";
  currency: string;
  b2b_total: string;
  b2b_balance: string;
  issued_on: string;
  due_on: string;
  overdue_days: number;
  overdue: boolean;
  detail_read: boolean;
  bookings: { booking_id: string; b2b_amount: string | null; complete: boolean; lines: { booking_line_id: string; trip_id: string; trip_title: string }[] }[];
}

export interface AaBookingRow {
  booking_line_id: string;
  trip_id: string;
  trip_title: string;
  trip_start_date: string | null;
  title: string;
  status: string;
  aa_sub_status: string | null;
  booking_id: string | null;
  hold_id: string | null;
  service_from: string | null;
  quoted_b2b: { amount: string; currency: string } | null;
  /** The sum of the quotes of the confirmed lines on the same AA booking (null: none, or quotes in two currencies). */
  booking_quoted_b2b: { amount: string; currency: string } | null;
  /** How many confirmed CRM lines share this AA booking; AA invoices (and the mismatch compares) per booking. */
  booking_lines: number;
  invoiced_b2b: { amount: string; currency: string; complete: boolean; invoices: string[] } | null;
  mismatch: boolean;
}

export interface AaBanner {
  level: "warn" | "past_due" | "overdue";
  holds_paused: boolean | null;
  overdue_invoices: number;
  oldest_overdue_days: number;
  overdue_b2b: { amount: string; currency: string }[];
  source: "live" | "cache";
}

export interface AaBookingsView {
  source: "live" | "cache";
  booking_error: string | null;
  refreshed_at: string | null;
  billing_status: { status: "ok" | "warn" | "past_due"; holds_paused: boolean; paid_through: string | null } | null;
  banner: AaBanner | null;
  invoices: AaInvoiceRow[];
  bookings: AaBookingRow[];
  mismatches: number;
}

export interface DepartureCost {
  departure_cost_id: string;
  departure_ref: string;
  agency_supplier_id: string;
  supplier_name: string;
  title: string;
  basis: "per_departure" | "per_pax";
  cost_date: string;
  group_cost: { amount: string; currency: string; amount_base: string; base_currency: string };
}

export interface DeparturePnl {
  departure_ref: string;
  base_currency: string;
  costs: DepartureCost[];
  trips: {
    trip_id: string;
    title: string;
    stage: string;
    start_date: string | null;
    pax: number;
    departure_cost_base: string;
    revenue_base: string;
    total_cost_base: string;
    margin_base: string;
  }[];
  totals: { pax: number; trips: number; departure_cost_base: string; revenue_base: string; trip_cost_base: string; margin_base: string };
  costs_other_base: number;
}

export interface ProfitRow {
  key: string;
  trips: number;
  pax: number;
  revenue_base: string;
  cost_base: string;
  commission_income_base: string;
  margin_base: string;
  margin_pct: string | null;
}

export interface ProfitView {
  dimension: Dimension;
  period: string;
  window: { from: string; to: string };
  base_currency: string;
  rows: ProfitRow[];
  totals: ProfitRow;
  trips_unconverted: number;
  trips_other_base: number;
  truncated: boolean;
}

export interface AgendaView {
  open_proposals: number;
  deposits_due_week: number;
  overdue_instalments: number;
  passport_missing: number;
  passport_days: number;
  aa_overdue_invoices: number | null;
  next_departures: { departure_ref: string; start_date: string | null; trips: number; pax: number; guaranteed: boolean | null }[];
  departure_days: number;
}

/** The message key of an API refusal on a finance form (unknown codes fall back to the generic one). */
export function financeErrorKey(detail: string): string {
  const known = [
    "bill_lines_over",
    "duplicate_bill_number",
    "bill_line_invalid",
    "bill_currency_mixed",
    "fx_rate_missing",
    "invalid_bill_date",
    "invalid_lines",
    "aa_supplier_bill",
    "bill_void",
    "invalid_paid_on",
    "invalid_departure_ref",
    "too_many_costs",
    "invalid_cost_date",
    "role_forbidden",
  ];
  return known.includes(detail) ? `finance.error.${detail}` : "finance.error.other";
}

/** The departure page's path (the ref is letters, digits and : _ . - only, so it needs no escaping beyond encoding). */
export function departureHref(ref: string): string {
  return `/departures/${encodeURIComponent(ref)}`;
}

export function profitExportPath(period: string, dimension: Dimension, owner: string | null): string {
  const q = new URLSearchParams({ section: "profit", period, dimension });
  if (owner) q.set("owner", owner);
  return `reports/export?${q.toString()}`;
}

/** A sum of decimal strings (two places), kept as a string for formatMoney. */
export function sumAmounts(values: readonly string[]): string {
  const cents = values.reduce((acc, v) => acc + Math.round(Number(v) * 100), 0);
  return (cents / 100).toFixed(2);
}
