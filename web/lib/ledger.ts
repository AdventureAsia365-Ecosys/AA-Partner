// web/lib/ledger.ts
// AA-944 (card CRM-16) — the shapes of the ledger API (api/app/ledger) and the small pure helpers the Bookings tab,
// A-11 Bookings, A-13 Suppliers and Settings > Payments share. Money arrives as keyed objects (cost, sell,
// commission_expected, margin_base); the API's margin serializer removes cost / commission / margin for the assistant,
// and lines carry no `money` block at all for her, so a missing key always means "not shown", never "zero".

import type { AaBlock } from "@/lib/aa";
import type { LineCommission } from "@/lib/commissions";

export type LineStatus = "planning" | "confirmed" | "cancelled";
export type Level = "off" | "basic" | "strict";

export interface Fx {
  rate: string;
  rate_date: string;
  source: "platform_daily" | "manual" | "aa_quote_lock";
  stale_days: number;
  attribution?: { text: string; url: string };
}

export interface Money {
  amount: string;
  currency: string;
  amount_base?: string;
  base_currency?: string;
  fx?: Fx;
}

export interface LineMoney {
  cost?: Money | null;
  sell?: Money | null;
  commission_expected?: Money | null;
  margin_base?: string;
  /** AA-957 (C12, card E): the fee AA charged for cancelling this AA booking (a cost of a cancelled line). */
  cancellation_cost?: Money | null;
}

export interface Line {
  booking_line_id: string;
  trip_id: string;
  supplier_kind: "aa" | "agency";
  agency_supplier_id: string;
  supplier_name: string;
  status: LineStatus;
  aa_sub_status: string | null;
  title: string;
  confirmation_no: string | null;
  service_from: string | null;
  service_to: string | null;
  collect: "agency" | "on_arrival";
  confirm_class: string | null;
  visible_to_travelers: boolean;
  confirmed_at: string | null;
  read_only: boolean;
  /** AA-947: Adventure Asia lines only (status, "AA pending", the "from"-price warning, the asks allowed now). */
  aa?: AaBlock;
  money?: LineMoney;
  /** AA-957: the commission follow-up of an agency line (owner / agent). */
  commission?: LineCommission | null;
  trip_title?: string;
  trip_stage?: string;
}

export interface TripLines {
  trip_id: string;
  lines: Line[];
  base_currency: string;
  booking_validation_level: Level;
  /** AA-947: the Lost reason the dialog pre-selects from the AA lines (declined / sold out); null without a signal. */
  lost_reason_hint?: string | null;
}

export interface LedgerTotals {
  confirmed_sell_base: string;
  confirmed_cost_base?: string;
  confirmed_margin_base?: string;
  confirmed_commission_expected_base?: string;
  pipeline_sell_base: string;
  pipeline_unconverted: number;
  /** Confirmed lines frozen in an earlier base currency: not in the sums above (never re-converted). */
  confirmed_other_base?: number;
  /** AA-957: the fees AA charged for cancelled AA bookings. */
  cancellation_cost_base?: string;
}

export interface LedgerView {
  lines: Line[];
  base_currency: string;
  truncated: boolean;
  totals?: LedgerTotals;
}

export interface Supplier {
  agency_supplier_id: string;
  kind: "aa" | "own";
  builtin: boolean;
  name: string;
  category: string;
  default_commission_pct?: string | null;
  /** AA-957: days after the service the supplier pays its commission (null = 30). */
  commission_due_days?: number | null;
  confirmation_unique: boolean;
  has_contact_name: boolean;
  has_contact_email: boolean;
  line_count: number;
}

export interface SuppliersView {
  suppliers: Supplier[];
  categories: string[];
}

export interface PaymentSettings {
  base_currency: string;
  booking_validation_level: Level;
  fx_mode: "automatic" | "manual";
  levels: Level[];
  fx_modes: string[];
  attribution: { text: string; url: string };
}

export interface FxOverride {
  fx_rate_override_id: string;
  rate_date: string;
  base: string;
  quote: string;
  rate: string;
  created_at: string;
}

export const MONEY_KINDS = ["cost", "sell", "commission_expected"] as const;
export type MoneyKind = (typeof MONEY_KINDS)[number];
export const CURRENCY = /^[A-Z]{3}$/;

/** The roles that see cost, commission and margin (the API's view_margin: owner and agent). */
export function seesMargin(role: string): boolean {
  return role === "owner" || role === "agent";
}

/** "2,540,000 VND" in the page's locale; amounts come as decimal strings and are never re-computed here. */
export function formatMoney(value: string | null | undefined, currency: string, locale: string): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return `${value} ${currency}`;
  const digits = ["VND", "JPY", "KRW", "IDR", "LAK", "KHR", "MMK"].includes(currency) ? 0 : 2;
  return `${n.toLocaleString(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits })} ${currency}`;
}

/** The frozen rate as people read it: "1 VND = 0.00004 USD", or null while the line is planning. */
export function fxReading(money: Money | null | undefined): string | null {
  if (!money || !money.fx || !money.base_currency) return null;
  if (money.currency === money.base_currency) return null;
  return `1 ${money.currency} = ${money.fx.rate} ${money.base_currency}`;
}

/** The API's refusal codes of the ledger routes -> message keys (lib/i18n.ts ledger.error.*). */
const ERRORS = new Set([
  "line_incomplete",
  "duplicate_confirmation_no",
  "aa_line_read_only",
  "fx_rate_missing",
  "line_not_editable",
  "supplier_in_use",
  "aa_supplier_builtin",
  "role_forbidden",
]);

export function errorKey(detail: string): string {
  return ERRORS.has(detail) ? `ledger.error.${detail}` : "ledger.error.other";
}

/** The `missing` codes of a 409 line_incomplete (confirmation_no, cost, sell, commission), read safely. */
export function missingCodes(body: unknown): string[] {
  const missing = (body as { missing?: unknown } | null)?.missing;
  return Array.isArray(missing) ? missing.filter((m): m is string => typeof m === "string") : [];
}

/** A money body for PUT lines/{id}/money from form text; empty amounts are left out. Null when nothing is valid. */
export function moneyBody(
  values: Partial<Record<MoneyKind, string>>,
  currency: string,
): Partial<Record<MoneyKind, { amount: string; currency: string }>> | null {
  if (!CURRENCY.test(currency)) return null;
  const out: Partial<Record<MoneyKind, { amount: string; currency: string }>> = {};
  for (const kind of MONEY_KINDS) {
    const raw = (values[kind] ?? "").replace(/[\s,]/g, "");
    if (raw === "") continue;
    if (!/^\d+(\.\d{1,2})?$/.test(raw)) return null;
    out[kind] = { amount: raw, currency };
  }
  return Object.keys(out).length ? out : null;
}

/** Day of a line for the table's first column: the service start, else "—". */
export function lineDay(line: Line): string | null {
  return line.service_from;
}
