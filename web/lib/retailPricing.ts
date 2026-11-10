// web/lib/retailPricing.ts
// AA-1066 (crm-048) — the shapes and pure helpers of retail pricing: the price per traveller type of a builder option
// (api/app/proposals/pricing.py), the discount box and its owner approval (api/app/proposals/discount.py) and
// Settings > Pricing (GET / PUT /settings/pricing, the price rules). Amounts are decimal strings, never re-computed
// here; the API computes every sum, suggestion and rule.

import { fmt, type MessageKey } from "@/lib/i18n";

export type PaxType = "adult" | "child" | "infant" | "single_supp";
export const PAX_TYPES: readonly PaxType[] = ["adult", "child", "infant", "single_supp"];
export type RuleKind = "season" | "group_size" | "early_booking" | "last_minute";
export const RULE_KINDS: readonly RuleKind[] = ["season", "group_size", "early_booking", "last_minute"];
export const RULE_SCOPES = ["all", "trip_plan", "hotel_room", "transfer", "guide", "activity", "entrance", "meal"] as const;
export type RuleScope = (typeof RULE_SCOPES)[number];
export type Stacking = "add" | "best_only";

export interface PriceRow {
  pax_type: PaxType;
  unit_sell: string;
  qty: number;
  /** Owner / agent only, agency items only (an AA item's net is the quote's). */
  unit_net?: string | null;
}

export interface AppliedRule {
  rule_id: string;
  name: string;
  adjust_pct: string;
}

export interface DiscountApproval {
  discount_approval_id: string;
  proposal_id: string;
  discount_pct: string;
  below_cost?: boolean;
  limit_pct: string;
  status: "pending" | "approved" | "rejected" | "superseded";
  requested_by: string;
  requested_at: string | null;
  decided_by: string | null;
  decided_at: string | null;
}

export interface DiscountState {
  discount_pct: string | null;
  limit_pct: string;
  above_limit: boolean;
  /** Margin data: present for owner / agent only. */
  below_cost?: boolean;
  needs_approval: boolean;
  approved_pct: string | null;
  pending: DiscountApproval | null;
  history: DiscountApproval[];
}

export interface PricingBlock {
  bands: { child_age_max: number; infant_age_max: number };
  party: Record<PaxType, number>;
  discount: DiscountState;
}

export interface PriceRule {
  retail_price_rule_id: string;
  name: string;
  kind: RuleKind;
  product_scope: RuleScope;
  aa_tour_source_id: string | null;
  date_from: string | null;
  date_to: string | null;
  min_pax: number | null;
  days_before: number | null;
  adjust_pct: string;
  priority: number;
  stacking: Stacking;
  active: boolean;
}

export interface PricingSettings {
  child_age_max: number;
  infant_age_max: number;
  discount_owner_limit_pct: string;
  rules: PriceRule[];
  kinds: RuleKind[];
  scopes: RuleScope[];
  max_rules: number;
}

/** One row the editor holds while the agent types (strings as typed). */
export interface DraftRow {
  pax_type: PaxType;
  qty: string;
  unit_sell: string;
  unit_net: string;
}

const MONEY = /^\d{1,8}(\.\d{1,2})?$/;

/** "Adult × 2 · Child × 1 · Single room × 1" — who an option's price is for (rows with nobody are left out). */
export function priceLines(rows: { pax_type: PaxType; qty: number }[], tr: (key: string) => string): string {
  return rows
    .filter((r) => r.qty > 0)
    .map((r) => `${tr(`pricing.line.${r.pax_type}`)} × ${r.qty}`)
    .join(" · ");
}

/** The editor's starting rows: the saved rows, else the API's suggested grid. */
export function draftRows(saved: PriceRow[], suggested: PriceRow[]): DraftRow[] {
  const from = saved.length ? saved : suggested;
  return from.map((r) => ({ pax_type: r.pax_type, qty: String(r.qty), unit_sell: r.unit_sell, unit_net: r.unit_net ?? "" }));
}

/** The PUT body of the rows, or null when a row is not valid (quantity 0-99, money with at most 2 decimals; a unit
 *  net only when ``withNet`` and then on every row or none: the API refuses a partial set). */
export function pricesBody(rows: DraftRow[], withNet: boolean): { pax_type: PaxType; qty: number; unit_sell: string; unit_net?: string }[] | null {
  if (!rows.length) return null;
  const seen = new Set<PaxType>();
  const out: { pax_type: PaxType; qty: number; unit_sell: string; unit_net?: string }[] = [];
  for (const r of rows) {
    if (!PAX_TYPES.includes(r.pax_type) || seen.has(r.pax_type)) return null;
    seen.add(r.pax_type);
    const qty = Number(r.qty.trim());
    const sell = r.unit_sell.replace(/[\s,]/g, "");
    if (!/^\d{1,2}$/.test(r.qty.trim()) || qty > 99 || !MONEY.test(sell)) return null;
    const row: { pax_type: PaxType; qty: number; unit_sell: string; unit_net?: string } = { pax_type: r.pax_type, qty, unit_sell: sell };
    const net = r.unit_net.replace(/[\s,]/g, "");
    if (withNet && net !== "") {
      if (!MONEY.test(net)) return null;
      row.unit_net = net;
    }
    out.push(row);
  }
  const nets = out.filter((r) => r.unit_net !== undefined).length;
  if (nets !== 0 && nets !== out.length) return null;
  return out;
}

/** A discount typed by the agent: "" -> null (none), "12.5" -> "12.5"; invalid -> undefined. */
export function discountInput(raw: string): string | null | undefined {
  const v = raw.trim().replace(",", ".");
  if (v === "") return null;
  if (!/^\d{1,3}(\.\d{1,2})?$/.test(v) || Number(v) > 100) return undefined;
  return v;
}

/** The words of a refusal code of these routes (EN / VI in lib/i18nRetailPricing.ts), or null for another code. */
export function pricingErrorKey(detail: string): MessageKey | null {
  const known: Record<string, MessageKey> = {
    amount_from_prices: "pricing.err.amount_from_prices",
    no_suggestion: "pricing.err.no_suggestion",
    unit_net_partial: "pricing.err.unit_net_partial",
    prices_nothing_charged: "pricing.err.prices_nothing_charged",
    invalid_prices: "pricing.err.invalid_prices",
    discount_needs_approval: "pricing.err.discount_needs_approval",
    discount_approval_not_needed: "pricing.err.discount_approval_not_needed",
    discount_approval_not_pending: "pricing.err.discount_approval_not_pending",
    invalid_discount_pct: "pricing.err.invalid_discount_pct",
  };
  return known[detail] ?? null;
}

/** The condition of a rule in words ("2027-01-01 to 2027-01-31", "6 travellers or more", ...). */
export function ruleSummary(rule: Pick<PriceRule, "kind" | "date_from" | "date_to" | "min_pax" | "days_before">, tr: (key: string) => string, date: (iso: string) => string): string {
  if (rule.kind === "season") return fmt(tr("pricingSettings.summary.season"), { from: date(rule.date_from ?? ""), to: date(rule.date_to ?? "") });
  if (rule.kind === "group_size") return fmt(tr("pricingSettings.summary.group_size"), { n: rule.min_pax ?? 0 });
  return fmt(tr(`pricingSettings.summary.${rule.kind}`), { n: rule.days_before ?? 0 });
}

/** A signed percentage for a rule line: "+20%", "−10%". */
export function signedPct(pct: string): string {
  const n = Number(pct);
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n)}%`;
}

export interface RuleForm {
  name: string;
  kind: RuleKind;
  product_scope: RuleScope;
  aa_tour_source_id: string;
  date_from: string;
  date_to: string;
  min_pax: string;
  days_before: string;
  adjust_pct: string;
  priority: string;
  stacking: Stacking;
}

export const EMPTY_RULE: RuleForm = {
  name: "",
  kind: "season",
  product_scope: "all",
  aa_tour_source_id: "",
  date_from: "",
  date_to: "",
  min_pax: "",
  days_before: "",
  adjust_pct: "",
  priority: "100",
  stacking: "add",
};

/** The POST body of a rule (only the kind's own condition), or null when the form is not valid. */
export function ruleBody(f: RuleForm): Record<string, unknown> | null {
  const name = f.name.trim();
  const adjust = f.adjust_pct.trim().replace(",", ".");
  const priority = Number(f.priority);
  if (!name || name.length > 80 || !/^-?\d{1,3}(\.\d{1,2})?$/.test(adjust)) return null;
  const pct = Number(adjust);
  if (pct === 0 || pct < -90 || pct > 300 || !Number.isInteger(priority) || priority < 1 || priority > 999) return null;
  const body: Record<string, unknown> = { name, kind: f.kind, product_scope: f.product_scope, adjust_pct: adjust, priority, stacking: f.stacking };
  if (f.aa_tour_source_id.trim()) body.aa_tour_source_id = f.aa_tour_source_id.trim();
  if (f.kind === "season") {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(f.date_from) || !/^\d{4}-\d{2}-\d{2}$/.test(f.date_to) || f.date_from > f.date_to) return null;
    body.date_from = f.date_from;
    body.date_to = f.date_to;
  } else if (f.kind === "group_size") {
    const n = Number(f.min_pax);
    if (!Number.isInteger(n) || n < 2 || n > 99) return null;
    body.min_pax = n;
  } else {
    const n = Number(f.days_before);
    const [low, high] = f.kind === "early_booking" ? [1, 730] : [0, 365];
    if (f.days_before.trim() === "" || !Number.isInteger(n) || n < low || n > high) return null;
    body.days_before = n;
  }
  return body;
}
