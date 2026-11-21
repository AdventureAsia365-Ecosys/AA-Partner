// web/lib/ledger.ts
// AA-944 (card CRM-16) — the shapes of the ledger API (api/app/ledger) and the small pure helpers the Bookings tab,
// A-11 Bookings, A-13 Suppliers and Settings > Payments share. Money arrives as keyed objects (cost, sell,
// commission_expected, margin_base); the API's margin serializer removes cost / commission / margin for the assistant,
// and lines carry no `money` block at all for her, so a missing key always means "not shown", never "zero".

import type { AaBlock } from "@/lib/aa";
import { canBundle } from "@/lib/agencyItems";
import type { SupplierThread } from "@/lib/catalogue";
import type { LineCommission } from "@/lib/commissions";

/** AA-1069 (crm-053): "requested" = asked from its supplier, waiting for the confirmation number. */
export type LineStatus = "planning" | "requested" | "confirmed" | "cancelled";
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
  /** AA-1147 (crm-083): null on an agency line the agency handles itself ("Handled by the agency"). */
  agency_supplier_id: string | null;
  supplier_name: string | null;
  handled_by_agency?: boolean;
  /** AA-1144: flight | visa | insurance | fee (agency lines). */
  category?: string | null;
  /** AA-1147: the item of the accepted proposal it came from, its deadline, folded into the tour price. */
  option_id?: string | null;
  due_at?: string | null;
  bundled?: boolean;
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
  /** AA-1069: when the line was asked from its supplier and the request e-mail; the supplier's thread on the trip. */
  requested_at?: string | null;
  request_ref?: string | null;
  supplier_thread?: SupplierThread | null;
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
  /** AA-1096 S280: an agent approves a refund up to this amount in the base currency (an older API omits it). */
  refund_owner_threshold_base?: string;
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
/** The refund approval limit as typed: a decimal 0 to 10,000,000 with at most 2 decimals (the crm-008 column CHECK);
 * "" or anything else → null. Spaces and thousands separators are ignored. */
export function thresholdInput(value: string): string | null {
  const v = value.replace(/[\s,]/g, "");
  if (!/^\d{1,8}(\.\d{1,2})?$/.test(v) || Number(v) > 10_000_000) return null;
  return v;
}

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

/** AA-1086: whether a member may show the line to the trip's travellers or hide it (the portal and the released
 * voucher). Not on a closed trip or a cancelled line (always hidden); an Adventure Asia line only once AA confirmed it
 * (it is shown by default then; the API refuses it before). */
export function canToggleShown(line: Pick<Line, "status" | "supplier_kind">, closed: boolean): boolean {
  if (closed || line.status === "cancelled") return false;
  return line.supplier_kind !== "aa" || line.status === "confirmed";
}

/** AA-1147: the trip's agency service lines a file can be attached to (an e-ticket, a visa letter, a certificate). */
export function serviceLines(result: { ok: true; data: TripLines } | { ok: false }): { booking_line_id: string; title: string }[] {
  if (!result.ok) return [];
  return result.data.lines.filter((l) => l.supplier_kind === "agency").map((l) => ({ booking_line_id: l.booking_line_id, title: l.title }));
}

/** AA-1147 (crm-083): the body of "Add line": no supplier = handled by the agency itself; a deadline (a task opens
 * before it); "fold into the tour price" (flight or fee only: the API answers 422 bundle_not_allowed otherwise). */
export function newLineBody(form: FormData): Record<string, unknown> {
  const text = (name: string) => String(form.get(name) ?? "").trim();
  const orNull = (value: string) => (value === "" ? null : value);
  return {
    agency_supplier_id: orNull(text("agency_supplier_id")),
    category: text("category"),
    title: text("title"),
    confirmation_no: orNull(text("confirmation_no")),
    service_from: orNull(text("service_from")),
    service_to: orNull(text("service_to")),
    collect: text("collect") || "agency",
    due_at: orNull(text("due_on")),
    bundled: form.get("bundled") === "on",
  };
}

/** AA-1147: an agency line's deadline, supplier and fold change on the Lines tab while it is planned or requested (the
 * lines an accepted proposal writes start without a deadline). Not on a closed trip or an Adventure Asia line. */
export function canEditService(line: Pick<Line, "supplier_kind" | "status" | "read_only">, closed: boolean): boolean {
  if (closed || line.read_only || line.supplier_kind !== "agency") return false;
  return line.status === "planning" || line.status === "requested";
}

/** AA-1147: the PATCH body of an agency line's "Deadline and supplier" form: only what changed. An empty deadline field
 * keeps the deadline ("Clear deadline" sends null); the supplier moves on a planning line only (the API answers 422
 * line_not_editable otherwise); the fold is sent for a flight or the fee only. */
export function lineChangeBody(
  form: FormData,
  line: Pick<Line, "status" | "due_at" | "agency_supplier_id" | "bundled" | "category">,
): Record<string, unknown> {
  return { ...dueChange(form, line), ...supplierChange(form, line), ...bundledChange(form, line) };
}

function dueChange(form: FormData, line: Pick<Line, "due_at">): Record<string, unknown> {
  const due = String(form.get("due_on") ?? "").trim();
  return due && due !== (line.due_at ?? "").slice(0, 10) ? { due_at: due } : {};
}

function supplierChange(form: FormData, line: Pick<Line, "status" | "agency_supplier_id">): Record<string, unknown> {
  if (line.status !== "planning" || !form.has("agency_supplier_id")) return {};
  const supplier = String(form.get("agency_supplier_id") ?? "").trim() || null;
  return supplier === (line.agency_supplier_id ?? null) ? {} : { agency_supplier_id: supplier };
}

function bundledChange(form: FormData, line: Pick<Line, "bundled" | "category">): Record<string, unknown> {
  if (!canBundle(line.category)) return {};
  const bundled = form.get("bundled") === "on";
  return bundled === Boolean(line.bundled) ? {} : { bundled };
}
