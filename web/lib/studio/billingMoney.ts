// lib/studio/billingMoney.ts — AA-1116 (option B): the Content studio Billing tab and Dashboard show the amounts and the
// currency CIS GET /v1/billing returns (contract tenant.entitlements.v1 1.9), never a literal "$".
//
// /v1/billing (CIS api/routers/v1_tours.py get_my_billing) answers:
//   billing_currency  the agency's invoice currency (USD | VND, set on CIS Tenant 360);
//   plans[]           each sellable content plan with `currency` + `price_monthly` (decimal, null = "Custom": a
//                     contract plan, or no price in that currency) next to the old `price_usd_monthly`;
//   quote             what the agency pays a month in its currency (option B: every product, the seat minimum, an
//                     agreed price, the founding / combo discount, a free pilot): {currency, lines, subtotal,
//                     discount_kind, discount_pct, discount, pilot_free, monthly_total, annual_paid_months,
//                     annual_total}, amounts as decimal strings;
//   overage_usd       the rewrite overage so far: always USD (the per-tour overage rate is a USD price).
// An older CIS (before 438) sends none of the three: everything falls back to the USD fields.

export type BillingCurrency = "USD" | "VND" | string;

export interface BillingPlanPrice {
  plan_name: string;
  price_usd_monthly?: number | null;
  currency?: string | null;
  price_monthly?: string | number | null;
}

/** One product on the monthly quote (CIS services/tenant_account/quote.py Line.out), amounts in the quote currency. */
export interface BillingQuoteLine {
  kind?: string | null;
  product?: string | null;
  plan_key?: string | null;
  name?: string | null;
  quantity?: number | null;
  extra_seats?: number | null;
  extra_seat_price?: string | number | null;
  unit_price?: string | number | null;
  amount?: string | number | null;
  discount?: string | number | null;
  agreed?: boolean | null;
  price_missing?: boolean | null;
  billing_interval?: string | null;
  trialing?: boolean | null;
}

export interface BillingQuote {
  currency?: string | null;
  monthly_total?: string | number | null;
  pilot_free?: boolean | null;
  discount?: string | number | null;
  discount_kind?: string | null;
  // AA-1129 (S283 rows 18, 24): the lines and the discount the agency pays for, shown on Billing and the Dashboard.
  lines?: BillingQuoteLine[] | null;
  subtotal?: string | number | null;
  discount_pct?: string | number | null;
  annual_paid_months?: number | null;
  annual_total?: string | number | null;
}

/** The /v1/billing fields this module reads (the rest of the record is the usage view). */
export interface BillingMoneyRecord {
  billing_currency?: string | null;
  price_usd_monthly?: number | string | null;
  overage_usd?: number | string | null;
  plan_tier?: string | null;
  plans?: BillingPlanPrice[] | null;
  quote?: BillingQuote | null;
}

function toNumber(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/**
 * An amount in its currency, the way the agency reads its invoice: VND whole with "₫" and dot thousands ("3.790.000 ₫"),
 * USD with "$" ("$149", "$19.50"); another code "1,234.50 EUR". The format follows the currency, not the UI language,
 * so the same price reads the same in both. Unreadable: "—".
 */
export function fmtMoney(amount: string | number | null | undefined, currency: BillingCurrency | null | undefined): string {
  const n = toNumber(amount);
  if (n === null) return "—";
  const code = (currency || "USD").toUpperCase().trim();
  if (code === "VND") {
    return n.toLocaleString("vi-VN", { style: "currency", currency: "VND", maximumFractionDigits: 0 });
  }
  if (code === "USD") {
    const cents = Number.isInteger(n) ? 0 : 2;  // "$149" for a whole price, "$19.50" with cents
    return n.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: cents, maximumFractionDigits: cents });
  }
  return `${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${code}`;
}

/** The agency's invoice currency: `billing_currency`, else the quote's, else USD (an older CIS). */
export function billingCurrency(b: BillingMoneyRecord | null | undefined): string {
  return (b?.billing_currency || b?.quote?.currency || "USD").toUpperCase();
}

/** A plan's monthly list price in the agency's currency ({amount, currency}); amount null = "Custom". A row from an
 * older CIS (no `currency`) is its USD price. */
export function planPrice(p: BillingPlanPrice): { amount: number | null; currency: string } {
  if (p.currency) return { amount: toNumber(p.price_monthly), currency: p.currency.toUpperCase() };
  return { amount: toNumber(p.price_usd_monthly), currency: "USD" };
}

/** The current content plan's monthly price: its row in `plans` (agency currency), else the usage view's USD price. */
export function currentPlanPrice(b: BillingMoneyRecord): { amount: number | null; currency: string } {
  const row = (b.plans ?? []).find((p) => p.plan_name === b.plan_tier);
  if (row) return planPrice(row);
  return { amount: toNumber(b.price_usd_monthly), currency: "USD" };
}

/**
 * "Amount due so far" this month: the quote's monthly total in the agency's currency (0 for a free pilot), plus the
 * USD overage. Same currency: one amount. A VND agency with USD overage: the VND total, the overage named apart
 * (`overageApart`). No quote (an older CIS): the USD plan price plus the overage, as before.
 */
export function dueSoFar(b: BillingMoneyRecord): { amount: number; currency: string; overageUsd: number; overageApart: boolean } {
  const overageUsd = toNumber(b.overage_usd) ?? 0;
  const total = toNumber(b.quote?.monthly_total);
  if (total === null) {
    return { amount: (toNumber(b.price_usd_monthly) ?? 0) + overageUsd, currency: "USD", overageUsd, overageApart: false };
  }
  const currency = (b.quote?.currency || billingCurrency(b)).toUpperCase();
  if (currency === "USD") return { amount: total + overageUsd, currency, overageUsd, overageApart: false };
  return { amount: total, currency, overageUsd, overageApart: overageUsd > 0 };
}

/** The Dashboard's two money figures as text: the current plan fee ("—" when none / custom) and the amount due so far
 * (the overage included when it is in the same currency). No record yet: both "—". */
export function monthMoney(b: BillingMoneyRecord | null | undefined): { planFee: string; due: string } {
  if (!b) return { planFee: "—", due: "—" };
  const plan = currentPlanPrice(b);
  const due = dueSoFar(b);
  return {
    planFee: plan.amount !== null && plan.amount > 0 ? fmtMoney(plan.amount, plan.currency) : "—",
    due: fmtMoney(due.amount, due.currency),
  };
}

/** What a quote line says beside its name, as codes the page words: a count, extra seats, a trial, an agreed price,
 *  a price still missing, a yearly line. */
export type QuoteNote =
  | { kind: "quantity"; n: number }
  | { kind: "extraSeats"; n: number; price: string | null }
  | { kind: "trial" }
  | { kind: "agreed" }
  | { kind: "priceMissing" }
  | { kind: "yearly" };

export interface QuoteRow {
  key: string;
  name: string;
  notes: QuoteNote[];
  amount: string;
}

/** The monthly bill as rows the Billing tab and the Dashboard show (AA-1129, S283 rows 18 and 24: the amount due was
 *  shown next to the plan fee alone, so a total with add-ons and a combo discount did not add up on the page). Null
 *  when CIS sent no quote lines (an older CIS): the pages keep the plan fee alone. */
export interface QuoteView {
  currency: string;
  rows: QuoteRow[];
  subtotal: string;
  /** The discount, when there is one: its kind (founding | combo | another code), percent and amount (positive). */
  discount: { kind: string | null; pct: number | null; amount: string } | null;
  pilotFree: boolean;
  total: string;
  annual: { months: number; total: string } | null;
}

export function quoteView(b: BillingMoneyRecord | null | undefined): QuoteView | null {
  const q = b?.quote;
  const lines = q?.lines ?? [];
  if (!q || lines.length === 0) return null;
  const currency = (q.currency || billingCurrency(b)).toUpperCase();
  const money = (v: unknown) => fmtMoney(toNumber(v), currency);
  const rows = lines.map((ln, i) => {
    const notes: QuoteNote[] = [];
    const qty = toNumber(ln.quantity) ?? 1;
    if (qty > 1) notes.push({ kind: "quantity", n: qty });
    const extra = toNumber(ln.extra_seats) ?? 0;
    if (extra > 0) {
      const price = toNumber(ln.extra_seat_price);
      notes.push({ kind: "extraSeats", n: extra, price: price === null ? null : fmtMoney(price, currency) });
    }
    if (ln.trialing) notes.push({ kind: "trial" });
    if (ln.agreed) notes.push({ kind: "agreed" });
    if (ln.price_missing) notes.push({ kind: "priceMissing" });
    if (ln.billing_interval === "year") notes.push({ kind: "yearly" });
    return { key: `${ln.plan_key ?? ln.product ?? "line"}-${i}`, name: ln.name || ln.plan_key || ln.product || "—", notes, amount: money(ln.amount) };
  });
  const sum = lines.reduce((acc, ln) => acc + (toNumber(ln.amount) ?? 0), 0);
  const discountAmount = Math.abs(toNumber(q.discount) ?? 0);
  const pct = toNumber(q.discount_pct);
  const months = toNumber(q.annual_paid_months);
  const annualTotal = toNumber(q.annual_total);
  return {
    currency,
    rows,
    subtotal: money(q.subtotal ?? sum),
    discount: discountAmount > 0 ? { kind: q.discount_kind ?? null, pct: pct !== null && pct > 0 ? pct : null, amount: fmtMoney(discountAmount, currency) } : null,
    pilotFree: q.pilot_free === true,
    total: money(q.monthly_total),
    annual: months !== null && months > 0 && annualTotal !== null && !q.pilot_free ? { months, total: fmtMoney(annualTotal, currency) } : null,
  };
}
